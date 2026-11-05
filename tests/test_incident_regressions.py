"""异常模块精简后的回归测试：逐项覆盖审查报告问题与精简后的界面入口。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app as webapp
from core.incident_intake import IncidentIntake, Submission
from core.reward_penalty_engine import RewardPenaltyMatcher
from core.reward_penalty_store import RewardPenaltyStore
from core.task_queue import TaskQueue


class AllowGuard:
    def scan(self, text, **kwargs):  # noqa: ARG002
        return "allow", [], {}

    def desensitize(self, text, mapping):  # noqa: ARG002
        return text


class Router:
    """按文本前缀决定成功或失败；规则/匹配器版本沿用真实实现。"""

    def __init__(self, real, *, fail_marker: str = "", ruleset: str = "", matcher: str = ""):
        self._real = real
        self.ruleset_version = ruleset or real.ruleset_version
        self.matcher_version = matcher or real.matcher_version
        self.fail_marker = fail_marker

    def match(self, text):
        if self.fail_marker and self.fail_marker in text:
            raise RuntimeError("model unavailable")
        return self._real.match(text)


class FailingMatcher:
    def __init__(self, real):
        self.ruleset_version = real.ruleset_version
        self.matcher_version = real.matcher_version

    def match(self, text):  # noqa: ARG002
        raise RuntimeError("model unavailable")


class FakeUsers:
    def __init__(self, *, allow: bool):
        self.allow = allow

    def get_public(self, username):
        return {
            "username": username, "display_name": username, "role": "operator",
            "role_label": "操作员", "enabled": True,
        }

    def has_permission(self, username, permission):  # noqa: ARG002
        return self.allow


def _req(username: str):
    return SimpleNamespace(username=username)


A_TEXT = "一名供应商未由ICT人员陪同，在NPI区域内独自行走。"
B_TEXT = "更正：供应商在NPI区域由ICT人员陪同，未独自进入管制区。"
C_TEXT = "更正：一名供应商未由ICT人员陪同，在NPI区域独自行走，已被保安劝离。"


class IncidentRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (
            webapp.guard, webapp.incident_intake, webapp.reward_penalty_store,
            webapp.reward_penalty_matcher, webapp.task_queue,
            webapp.record_last_answer, webapp.user_store,
        )
        self.db = root / "incidents.db"
        self.real = RewardPenaltyMatcher()
        webapp.guard = AllowGuard()
        webapp.incident_intake = IncidentIntake(self.db)
        webapp.reward_penalty_store = RewardPenaltyStore(self.db)
        webapp.reward_penalty_matcher = self.real
        webapp.incident_intake.match_sink = webapp._sync_reward_log
        webapp.task_queue = TaskQueue(root / "tasks.db")
        webapp.record_last_answer = Mock()
        webapp.user_store = FakeUsers(allow=True)
        self.intake = webapp.incident_intake

    def tearDown(self):
        (webapp.guard, webapp.incident_intake, webapp.reward_penalty_store,
         webapp.reward_penalty_matcher, webapp.task_queue,
         webapp.record_last_answer, webapp.user_store) = self._saved
        self.tmp.cleanup()

    def submit(self, *, text=A_TEXT, submission_id="sub-1", operation="report",
               incident_id="", matcher=None, channel="manual"):
        matcher = matcher or self.real
        receipt = self.intake.submit(
            Submission(
                submission_id=submission_id, text=text, operation=operation,
                incident_id=incident_id, source_channel=channel,
            ),
            actor="system", region="H区", guard=webapp.guard,
            ruleset_version=matcher.ruleset_version,
            matcher_version=matcher.matcher_version,
        )
        return receipt

    # ---- 审查 P1：迟到的旧版本回调不能覆盖当前版本 ----
    def test_late_reward_sync_from_old_version_is_ignored(self):
        first = self.submit()
        self.intake.process_incident(first["incident_id"], self.real)
        old_batch = self.intake.latest_match(first["incident_id"], version=1)
        webapp.reward_penalty_store.upsert_for_incident(
            incident_id=first["incident_id"], match_batch_id=old_batch["id"],
            created_by="system", event_description=A_TEXT,
            result=old_batch["result"], incident_version=1,
        )
        second = self.submit(
            text=C_TEXT, submission_id="sub-2", operation="correct",
            incident_id=first["incident_id"],
        )
        self.assertEqual(second["version"], 2)
        self.intake.process_incident(first["incident_id"], self.real)
        new_batch = self.intake.latest_match(first["incident_id"], version=2)
        webapp.reward_penalty_store.upsert_for_incident(
            incident_id=first["incident_id"], match_batch_id=new_batch["id"],
            created_by="system", event_description=C_TEXT,
            result=new_batch["result"], incident_version=2,
        )
        late = webapp.reward_penalty_store.upsert_for_incident(
            incident_id=first["incident_id"], match_batch_id=old_batch["id"],
            created_by="system", event_description=A_TEXT,
            result=old_batch["result"], incident_version=1,
        )
        self.assertTrue(late.get("skipped"))
        stored = webapp.reward_penalty_store.find_by_incident(first["incident_id"])
        self.assertEqual(C_TEXT, stored["event_description"])
        self.assertEqual("candidate", json.loads(stored["result_json"])["status"])

    def test_old_version_task_cannot_overwrite_new_version_state(self):
        first = self.submit()
        old_task = self.intake.claim_task()
        self.assertIsNotNone(old_task)
        second = self.submit(
            text=C_TEXT, submission_id="sub-2", operation="correct",
            incident_id=first["incident_id"],
        )
        self.assertEqual(second["version"], 2)
        self.intake.process_incident(first["incident_id"], self.real)
        self.assertEqual("candidate", self.intake.get(first["incident_id"])["match_status"])
        # 旧 v1 任务迟到失败：只能结束自己，不能把 v2 的匹配状态改回 failed。
        self.intake._finish_task(old_task, "dead", "RuntimeError", match_status="failed")
        incident = self.intake.get(first["incident_id"])
        self.assertEqual(2, incident["current_version"])
        self.assertEqual("candidate", incident["match_status"])
        batch = self.intake.latest_match(first["incident_id"])
        self.assertEqual(2, batch["incident_version"])
        self.assertFalse(batch["stale"])

    # ---- 审查 P1：同一正文可以登记两件独立事件；网络重试不重复 ----
    def test_same_text_twice_creates_two_events_but_retry_dedupes(self):
        one = webapp.record_incident(A_TEXT)
        two = webapp.record_incident(A_TEXT)
        self.assertEqual("INC-", one[4][:4])
        self.assertEqual("INC-", two[4][:4])
        self.assertNotEqual(one[4], two[4])
        self.assertEqual(2, len(self.intake.list_recent(username="system")))
        # 网络重试沿用同一提交编号：不会多出一条事件。
        webapp.record_incident(A_TEXT, submission_id="retry-fixed-1")
        webapp.record_incident(A_TEXT, submission_id="retry-fixed-1")
        self.assertEqual(3, len(self.intake.list_recent(username="system")))

    # ---- 审查 P1：A→B→A 更正 ----
    def test_a_to_b_to_a_correction_creates_new_version_each_time(self):
        out = webapp.record_incident(A_TEXT)
        incident_id = out[4]
        webapp.amend_incident(incident_id, "更正", B_TEXT)
        webapp.amend_incident(incident_id, "更正", A_TEXT)
        incident = self.intake.get(incident_id)
        self.assertEqual(3, incident["current_version"])
        self.assertEqual(A_TEXT, incident["submission"]["text_snapshot"])
        versions = [row["incident_version"] for row in self.intake.submissions(incident_id)]
        self.assertEqual([1, 2, 3], versions)

    # ---- 审查 P1：匹配失败不能把旧条款冒充成本轮结果 ----
    def test_failed_correction_does_not_show_old_clause_as_current(self):
        selective = Router(self.real, fail_marker="管制区")
        first = self.submit(matcher=selective)
        self.intake.process_incident(first["incident_id"], selective)
        old_batch = self.intake.latest_match(first["incident_id"], version=1)
        old_clause = old_batch["result"]["matches"][0]["clause_no"]
        second = self.submit(
            text=B_TEXT, submission_id="sub-2", operation="correct",
            incident_id=first["incident_id"], matcher=selective,
        )
        self.intake.process_incident(first["incident_id"], selective)
        self.assertIsNone(self.intake.latest_match(first["incident_id"], version=2))
        self.assertEqual("failed", self.intake.get(first["incident_id"])["match_status"])
        detail = webapp.render_incident_detail(first["incident_id"])
        current = detail.split("当前匹配结果", 1)[1].split("修改历史", 1)[0]
        self.assertIn("匹配失败", current)
        self.assertNotIn(old_clause, current)
        self.assertEqual(2, second["version"])

    # ---- 审查 P2：规则更新不能错标/覆盖旧批次 ----
    def test_rules_change_keeps_old_batch_and_labels_new_batch(self):
        newer = Router(
            self.real, ruleset="rules-v2", matcher="matcher-v2",
        )
        receipt = self.submit()
        self.intake.process_incident(receipt["incident_id"], self.real)
        old = self.intake.latest_match(receipt["incident_id"])
        self.assertEqual(self.real.ruleset_version, old["ruleset_version"])
        self.intake.retry_match(receipt["incident_id"], newer)
        history = self.intake.match_history(receipt["incident_id"])
        labels = {row["ruleset_version"] for row in history}
        self.assertIn(self.real.ruleset_version, labels)
        self.assertIn("rules-v2", labels)
        latest = self.intake.latest_match(receipt["incident_id"])
        self.assertEqual("rules-v2", latest["ruleset_version"])
        self.assertEqual("matcher-v2", latest["matcher_version"])
        self.assertFalse(latest["stale"])

    def test_old_pending_task_is_invalidated_before_new_rules_run(self):
        newer = Router(self.real, ruleset="rules-v2", matcher="matcher-v2")
        receipt = self.submit()
        task = self.intake.claim_task()
        status = self.intake.process_task(task, newer)
        self.assertEqual("requeued", status)
        row = self.intake.task_counts()
        self.assertGreaterEqual(row.get("stale", 0), 1)
        self.intake.process_incident(receipt["incident_id"], newer)
        batch = self.intake.latest_match(receipt["incident_id"])
        self.assertEqual("rules-v2", batch["ruleset_version"])

    # ---- 审查 P2：空口令 / 空更正不建档 ----
    def _run(self, message, submission_id):
        hint = json.dumps({
            "channel": "manual", "record": True, "submission_id": submission_id,
            "conversation_id": "conv-test", "message_id": "msg-" + submission_id,
            "edited": False,
        })
        updates = list(webapp.respond(message, [], "chat", None, None, "local", intake_hint=hint))
        self.assertTrue(updates)
        return updates

    def test_empty_record_command_does_not_create_event(self):
        self._run("帮我记录：", "empty-1")
        self._run("记录一下", "empty-2")
        self.assertEqual(0, len(self.intake.list_recent(username="system")))

    def test_empty_amendment_does_not_create_version(self):
        receipt = self.submit()
        note = webapp.amend_incident(receipt["incident_id"], "更正", "")
        self.assertIn("请填写", note)
        updates = self._run(f"更正记录 {receipt['incident_id']}：", "empty-amend")
        self.assertIn("请补充", updates[-1][0][-1]["content"])
        self.assertEqual(1, self.intake.get(receipt["incident_id"])["current_version"])
        self.assertEqual(1, len(self.intake.submissions(receipt["incident_id"])))

    # ---- 权限：他人不可读、不可改、不可重试 ----
    def test_other_user_cannot_read_edit_or_retry(self):
        webapp.user_store = FakeUsers(allow=False)
        receipt = self.submit()
        self.intake.process_incident(receipt["incident_id"], self.real)
        bob = _req("bob")
        detail = webapp.render_incident_detail(receipt["incident_id"], bob)
        self.assertIn("无权", detail)
        note = webapp.save_incident_record(receipt["incident_id"], B_TEXT, "", bob)[0]
        self.assertIn("本人登记的事件", note)
        retry = webapp.retry_record_match(receipt["incident_id"], bob)[0]
        self.assertIn("无权", retry)
        self.assertEqual(1, self.intake.get(receipt["incident_id"])["current_version"])
        self.assertEqual([], webapp.render_incident_table(bob))

    # ---- 精简界面入口 ----
    def test_record_edit_and_open_flow(self):
        out = webapp.record_incident(A_TEXT)
        self.assertEqual(8, len(out))
        self.assertIn("已记录", out[0])
        self.assertEqual("", out[7])  # 成功后清空提交编号
        incident_id = out[4]
        self.assertEqual(1, len(webapp.render_incident_table()))
        self.assertEqual(7, len(webapp.render_incident_table()[0]))
        opened = webapp.open_incident_record(SimpleNamespace(index=0))
        self.assertEqual(5, len(opened))
        self.assertIn(incident_id, opened[0])
        self.assertIn("当前正文", opened[0])
        saved = webapp.save_incident_record(incident_id, B_TEXT, "")
        self.assertEqual(8, len(saved))
        self.assertIn("已保存为新版本", saved[0])
        self.assertEqual(B_TEXT, self.intake.get(incident_id)["submission"]["text_snapshot"])

    def test_incident_page_search_filters_and_pagination(self):
        for index in range(25):
            self.submit(
                text=f"供应商未陪同进入区域第{index}号，保安已到场核实。",
                submission_id=f"page-{index}",
                operation="report",
                matcher=self.real,
                channel="dictation" if index == 24 else "manual",
            )
        table, ids, info, page = webapp.render_incident_page(page=1)
        self.assertEqual(20, len(table))
        self.assertEqual(20, len(ids))
        self.assertEqual(1, page)
        self.assertIn("共 25 条", info)
        second_table, second_ids, second_info, second_page = webapp.render_incident_page(page=2)
        self.assertEqual(5, len(second_table))
        self.assertEqual(2, second_page)
        self.assertIn("第 2/2 页", second_info)
        self.assertTrue(set(ids).isdisjoint(second_ids))
        keyword_table, keyword_ids, keyword_info, keyword_page = webapp.render_incident_page(
            query="第7号",
        )
        self.assertEqual(1, len(keyword_table))
        self.assertEqual(1, keyword_page)
        self.assertIn("第7号", keyword_table[0][4])
        self.assertIn("共 1 条", keyword_info)
        self.assertTrue(keyword_ids)
        self.assertEqual(0, len(webapp.render_incident_page(
            query="不存在的关键词",
        )[0]))
        self.assertEqual(0, len(webapp.render_incident_page(
            date_from="2020-01-01", date_to="2020-01-02",
        )[0]))
        source_table = webapp.render_incident_page(source_label="语音听写")[0]
        self.assertEqual(1, len(source_table))
        self.assertIn("语音听写", source_table[0][3])
        self.assertEqual(1, webapp.apply_incident_filters()[3])
        self.assertEqual(2, webapp.incident_next_page(
            "", "", "", "全部", "全部", "", 1,
        )[3])
        self.assertEqual(1, webapp.incident_previous_page(
            "", "", "", "全部", "全部", "", 2,
        )[3])

    def test_invalid_date_filter_does_not_query(self):
        table, ids, info, page = webapp.render_incident_page(date_from="2026/11/04")
        self.assertEqual([], table)
        self.assertEqual([], ids)
        self.assertEqual(1, page)
        self.assertIn("YYYY-MM-DD", info)

    def test_open_row_uses_page_snapshot_ids(self):
        first = webapp.record_incident(A_TEXT)
        second = webapp.record_incident(A_TEXT)
        # 故意把快照顺序倒过来：点击第一行应打开快照里的第一条，而不是重新查询后的行号。
        opened = webapp.open_incident_record(
            SimpleNamespace(index=0), row_ids=[second[4], first[4]],
        )
        self.assertIn(second[4], opened[0])
        self.assertNotIn(first[4], opened[0])

    def test_retry_button_recovers_failed_match(self):
        failing = FailingMatcher(self.real)
        with patch.object(webapp, "reward_penalty_matcher", failing):
            out = webapp.record_incident(A_TEXT)
            incident_id = out[4]
            self.assertEqual("failed", self.intake.get(incident_id)["match_status"])
            self.assertIn("匹配失败", out[0])
        retry = webapp.retry_record_match(incident_id)
        self.assertEqual(4, len(retry))
        self.assertIn("已重新执行", retry[0])
        self.assertEqual("candidate", self.intake.get(incident_id)["match_status"])


if __name__ == "__main__":
    unittest.main()
