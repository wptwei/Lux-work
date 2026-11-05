"""第 1 步补丁回归：否定语境、工号误判、外发红线、Word 转换闸门、真实数据去向文案。"""
import json
import tempfile
import unittest
from pathlib import Path

import docx

from agents.negation import is_negated
from agents.risk_agent import RiskAgent
from agents.urgency_agent import UrgencyAgent
from core.ingest import ingest_word_for_conversion
from core.redline_guard import RedlineGuard
from core.ui_labels import boundary_labels

ROOT = Path(__file__).resolve().parent.parent


class NegationTests(unittest.TestCase):
    def setUp(self):
        self.risk = RiskAgent()
        self.urgency = UrgencyAgent()

    def test_missing_authorization_is_not_a_negation(self):
        # 回归：这些句子旧版全部被判成 low
        for text in (
            "有人未经授权闯入H区东门",
            "陌生人未经许可翻越围墙",
            "发现有人没有刷卡强行开门",
            "供应商无人陪同闯入专区",
            "未能阻止冲突升级",
        ):
            with self.subTest(text=text):
                result = self.risk.score(text)
                self.assertIn(result["level"], {"high", "critical"})
                self.assertEqual([], result["negated_terms"])

    def test_real_negations_still_work(self):
        for text, term in (
            ("确认无人闯入，情况正常", "闯入"),
            ("未发现任何人员受伤", "受伤"),
            ("没有发生火灾", "火灾"),
            ("并无闯入迹象", "闯入"),
        ):
            with self.subTest(text=text):
                result = self.risk.score(text)
                self.assertEqual("low", result["level"])
                self.assertIn(term, result["negated_terms"])

    def test_negation_does_not_cross_clause_boundary(self):
        self.assertEqual("critical", self.risk.score("没有人员受伤，但现场有明火")["level"])
        self.assertFalse(is_negated("设备无法确认身份，有人尾随进入", "设备无法确认身份，有人尾随进入".index("尾随")))

    def test_urgency_ignores_negated_markers(self):
        self.assertEqual("routine", self.urgency.score("确认无人员被困，已扑灭明火")["level"])
        self.assertEqual("emergency", self.urgency.score("有人员被困，正在发生")["level"])


class EmployeeIdTests(unittest.TestCase):
    def setUp(self):
        self.guard = RedlineGuard(log_dir=tempfile.mkdtemp())

    def categories(self, text):
        return {h["category"] for h in self.guard.scan(text)[1]}

    def test_dates_are_not_employee_ids(self):
        for text in ("统计周期20261017-20261023", "报告日期20261023", "19991231"):
            with self.subTest(text=text):
                self.assertNotIn("工号", self.categories(text))

    def test_real_eight_digit_ids_still_masked(self):
        for text in ("工号10234567", "订单12345678", "20260230"):  # 最后一个不是有效日期
            with self.subTest(text=text):
                self.assertIn("工号", self.categories(text))


class OutboundTests(unittest.TestCase):
    def setUp(self):
        self.log_dir = Path(tempfile.mkdtemp())
        self.guard = RedlineGuard(log_dir=self.log_dir)

    def test_blocks_phone_in_email_and_logs_no_raw_text(self):
        result = self.guard.check_outbound("请联系13800001234确认", "邮件外发")
        self.assertFalse(result["ok"])
        self.assertIn("手机号", result["message"])
        logged = "".join(p.read_text(encoding="utf-8") for p in self.log_dir.glob("*.jsonl"))
        self.assertNotIn("13800001234", logged)
        self.assertEqual("邮件外发", json.loads(logged.splitlines()[-1])["task"])

    def test_desensitize_level_content_is_allowed(self):
        self.assertTrue(self.guard.check_outbound("本周巡检正常。华东安保二部", "周报导出")["ok"])


class WordGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.guard = RedlineGuard(log_dir=self.tmp / "logs")

    def make_docx(self, name, text):
        path = self.tmp / name
        document = docx.Document()
        document.add_paragraph(text)
        document.save(path)
        return path

    def test_clean_docx_passes(self):
        path = self.make_docx("ok.docx", "本周无异常。")
        self.assertTrue(ingest_word_for_conversion(path, self.guard).ok)

    def test_docx_with_phone_is_stopped_before_conversion(self):
        path = self.make_docx("bad.docx", "联系电话13800001234")
        result = ingest_word_for_conversion(path, self.guard)
        self.assertFalse(result.ok)
        self.assertEqual("block", result.level)

    def test_legacy_doc_is_refused_because_it_cannot_be_checked(self):
        path = self.tmp / "old.doc"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        result = ingest_word_for_conversion(path, self.guard)
        self.assertFalse(result.ok)
        self.assertIn(".docx", result.message)


class BoundaryLabelTests(unittest.TestCase):
    def test_labels_follow_model_mode(self):
        local = boundary_labels(False)
        external = boundary_labels(True)
        self.assertIn("本机", local["header_meta"])
        self.assertIn("外发", external["header_meta"])
        for labels in (local, external):
            self.assertNotIn("AIR-GAPPED", labels["header_meta"])
            self.assertNotIn("不出域", "".join(labels.values()))

    def test_external_mode_never_claims_on_device_only(self):
        external = boundary_labels(True)
        self.assertNotIn("仅在本机", "".join(external.values()))


class AppWiringTests(unittest.TestCase):
    """app.py 依赖 gradio，这里只做源码级检查，保证接线没有被改回去。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_no_hardcoded_airgap_claims(self):
        for phrase in ("AIR-GAPPED", "业务数据不出域", "本地模型就绪"):
            self.assertNotIn(phrase, self.src)

    def test_outbound_paths_call_the_guard(self):
        self.assertEqual(4, self.src.count("guard.check_outbound("))  # 邮件 + PPT + Word 导出 + 联网搜索
        self.assertIn("ingest_word_for_conversion(source, guard", self.src)

    def test_incident_ledger_is_always_available(self):
        # 精简后异常记录只有“记录 + 台账 + 详情”一个入口，不再依赖事件中心开关，
        # 避免关闭开关时把需要保留的台账和详情一起隐藏。
        self.assertIn('with gr.Tab("异常记录", elem_id="tab-event"', self.src)
        self.assertIn("incident_record_btn.click(", self.src)
        self.assertNotIn("兼容旧接口，不对用户展示多Agent处置流程", self.src)

    def test_default_sla_note_depends_on_approval_status(self):
        # 第 2A 步修正：提示依据批准状态，而不是“计划里有没有时限”
        self.assertIn("defaults_note = \"\" if INCIDENT_RESPONSE.approved else DRAFT_NOTE", self.src)
