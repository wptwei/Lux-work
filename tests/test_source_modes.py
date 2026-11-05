"""知识来源三档：本地资料 / 联网搜索 / 综合检索。

验收点：
- 联网不再排斥本地检索，意图要找本地文件时照样查；
- 综合检索必须把本地正文送进模型，只有文件名或目录卡片时不得分析；
- 查询脱敏、文件权限、来源引用保持不变。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as webapp
from core.db import PromptDB
from core.library_retrieval import local_evidence
from core.pdf_library import PdfLibrary, LibraryError
from core.redline_guard import RedlineGuard
from core.task_queue import TaskQueue
from core.web_search import SearchResult, WebSearchError
from tests.test_pdf_library import OTHER, OWNER, VIEWER, pdf_bytes

CHINESE_PAGE = '巡检制度：夜间每两小时巡查一次，发现异常立即上报值班主管。'


def _web_result():
    return SearchResult('', '公开来源', 'https://example.com/a', '公开正文',
                        '2026-01-01', '2026-10-31 10:00:00')


class SourceModeFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (webapp.task_queue, webapp.db, webapp.guard,
                       webapp.configured_llm_stream, webapp.search_web,
                       webapp.web_search_configured, webapp.pdf_library,
                       webapp.pdf_user, webapp._router_model, webapp._library_model)
        # 路由与文件选择都用固定桩，避免测试真的去连本地模型。
        webapp._router_model = Mock(return_value=json.dumps(
            {"action": "chat", "object_type": "", "relation": "new",
             "source_scope": "none", "missing_fields": [], "needs_clarification": False}))
        webapp._library_model = Mock(return_value='{"intent":"retrieve","ids":[1]}')
        webapp.task_queue = TaskQueue(root / "tasks.db")
        webapp.db = PromptDB(root / "prompts.db")
        webapp.guard = RedlineGuard(log_dir=root / "logs")
        webapp.conversation_store.reset("anonymous")

        self.store = PdfLibrary(root / "library")
        self.pdf = root / "巡检制度.pdf"
        self.pdf.write_bytes(pdf_bytes(('',)))
        with patch("core.pdf_library.extract_pdf", return_value=([CHINESE_PAGE], "indexed")):
            self.record = self.store.upload(self.pdf, OWNER)
        webapp.pdf_library = self.store
        webapp.pdf_user = lambda request: VIEWER

        self.seen = []

        def fake_stream(prompt="", system="", messages=None):
            self.seen.append(list(messages or []))
            yield "据 [L1] 与 [S1]，答案是 X。"

        webapp.configured_llm_stream = fake_stream
        webapp.web_search_configured = Mock(return_value=(True, "已配置"))
        webapp.search_web = Mock(return_value=[_web_result()])

    def tearDown(self):
        (webapp.task_queue, webapp.db, webapp.guard,
         webapp.configured_llm_stream, webapp.search_web,
         webapp.web_search_configured, webapp.pdf_library,
         webapp.pdf_user, webapp._router_model, webapp._library_model) = self._saved
        self.tmp.cleanup()

    def _run(self, message, source):
        updates = list(webapp.respond(message, [], "chat", None, None, source))
        self.assertTrue(all(len(u) == 5 for u in updates))
        return updates

    def _task_meta(self, updates):
        return json.loads(webapp.task_queue.get(updates[-1][-1])["result"])

    def _prompt_text(self):
        return "\n".join(m.get("content", "") for m in self.seen[-1])

    # --- 本地资料 ---
    def test_local_mode_never_calls_web_search(self):
        updates = self._run("查找巡检制度", "local")
        webapp.search_web.assert_not_called()
        self.assertIn(self.record["version_id"], updates[-1][0][-1]["content"])

    # --- 联网搜索：不再排斥本地检索 ---
    def test_web_mode_also_reads_local_files_when_intent_is_retrieval(self):
        updates = self._run("查找巡检制度", "web")
        webapp.search_web.assert_called_once()
        prompt = self._prompt_text()
        self.assertIn(CHINESE_PAGE, prompt)          # 本地正文进了模型
        answer = updates[-1][0][-1]["content"]
        self.assertIn("本地来源", answer)
        self.assertIn("联网来源", answer)

    def test_both_mode_skips_local_selection_for_plain_chat(self):
        updates = self._run("你好", "both")
        webapp._library_model.assert_not_called()
        self.assertIn("据 [L1] 与 [S1]", updates[-1][0][-1]["content"])

    def test_web_mode_failure_stops_instead_of_silently_switching_source(self):
        webapp.search_web.side_effect = WebSearchError("联网搜索超时，本次未取得联网资料。")
        updates = self._run("查找巡检制度", "web")
        self.assertEqual([], self.seen)              # 没有生成回答
        self.assertIn("超时", updates[-1][0][-1]["content"])

    # --- 综合检索 ---
    def test_both_mode_injects_local_full_text_before_analysis(self):
        updates = self._run("查找巡检制度", "both")
        prompt = self._prompt_text()
        self.assertIn(CHINESE_PAGE, prompt)
        self.assertIn("[L1]", prompt)
        self.assertIn("[S1]", prompt)
        meta = self._task_meta(updates)
        self.assertEqual("both", meta["source_mode"])
        self.assertEqual(1, meta["local_files"])
        self.assertGreater(meta["local_text_chars"], 0)

    def test_both_mode_refuses_to_analyze_filenames_without_body(self):
        empty = Path(self.tmp.name) / "扫描件.pdf"
        empty.write_bytes(pdf_bytes(('',)))
        with patch("core.pdf_library.extract_pdf", return_value=([], "stored_only")):
            self.store.upload(empty, OWNER, tags="巡检")
        webapp.search_web.return_value = [_web_result()]
        updates = self._run("查找扫描件", "both")
        answer = updates[-1][0][-1]["content"]
        self.assertIn("没有可提取的正文", answer)
        self.assertNotIn("据 [L1]", answer)           # 未用小模型基于文件名分析
        self.assertEqual([], self.seen)

    def test_both_mode_continues_with_local_text_when_web_fails(self):
        webapp.search_web.side_effect = WebSearchError("联网搜索超时，本次未取得联网资料。")
        updates = self._run("查找巡检制度", "both")
        prompt = self._prompt_text()
        self.assertIn(CHINESE_PAGE, prompt)
        answer = updates[-1][0][-1]["content"]
        self.assertIn("超时", answer)
        self.assertIn("本地来源", answer)

    def test_both_mode_desensitizes_query_before_any_web_call(self):
        webapp.guard.config["sensitive_terms"] = {"production_lines": ["东区甲岗"]}
        self._run("东区甲岗 最近的公开动态", "both")
        sent = webapp.search_web.call_args[0][0]
        self.assertNotIn("东区甲岗", sent)
        self.assertIn("【区域", sent)


class SourceModeHelperTests(unittest.TestCase):
    def test_boolean_backward_compatibility(self):
        self.assertEqual("web", webapp.normalize_source(True))
        self.assertEqual("local", webapp.normalize_source(False))
        self.assertEqual("both", webapp.normalize_source("both"))
        self.assertEqual("local", webapp.normalize_source("unknown"))

    def test_cycle_visits_all_three_sources(self):
        state, button = webapp.cycle_source("local")
        self.assertEqual("web", state)
        self.assertIn("联网搜索", button["value"])
        state, button = webapp.cycle_source("web")
        self.assertEqual("both", state)
        self.assertIn("综合检索", button["value"])
        state, _ = webapp.cycle_source("both")
        self.assertEqual("local", state)


class LocalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = PdfLibrary(root / "library")
        pdf = root / "巡检制度.pdf"
        pdf.write_bytes(pdf_bytes(('',)))
        with patch("core.pdf_library.extract_pdf", return_value=([CHINESE_PAGE], "indexed")):
            self.record = self.store.upload(pdf, OWNER)

    def test_reads_page_text_with_page_numbers(self):
        evidence = local_evidence("查找巡检制度", self.store, VIEWER, lambda *a: "")
        self.assertEqual(1, len(evidence["blocks"]))
        block = evidence["blocks"][0]
        self.assertIn(CHINESE_PAGE, block["content"])
        self.assertEqual([1], block["pages"])
        self.assertEqual("L1", block["id"])

    def test_textless_file_is_reported_not_analyzed(self):
        pdf = Path(self.tmp.name) / "无正文.pdf"
        pdf.write_bytes(pdf_bytes(('',)))
        with patch("core.pdf_library.extract_pdf", return_value=([], "stored_only")):
            self.store.upload(pdf, OWNER, tags="巡检")
        evidence = local_evidence("查找无正文.pdf", self.store, VIEWER, lambda *a: "")
        self.assertEqual([], evidence["blocks"])
        self.assertIn("无正文", evidence["textless"])

    def test_read_text_respects_permissions(self):
        private = Path(self.tmp.name) / "别人的私有文件.pdf"
        private.write_bytes(pdf_bytes(('',)))
        with patch("core.pdf_library.extract_pdf", return_value=([CHINESE_PAGE], "indexed")):
            record = self.store.upload(private, OTHER, visibility="private")
        with self.assertRaises(LibraryError):
            self.store.read_text(record["version_id"], VIEWER)


if __name__ == "__main__":
    unittest.main()
