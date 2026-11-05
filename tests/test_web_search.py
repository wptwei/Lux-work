from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import web_context
from core.web_search import (
    SearchResult,
    WebSearchError,
    _normalize,
    search_web,
    web_search_configured,
)

import app as webapp
from core.db import PromptDB
from core.redline_guard import RedlineGuard
from core.task_queue import TaskQueue


def _settings(**overrides):
    base = dict(web_search_enabled=False, web_search_provider="tavily",
                web_search_timeout=10, web_search_max_results=4)
    base.update(overrides)
    return SimpleNamespace(**base)


class WebSearchModuleTests(unittest.TestCase):
    def test_normalize_keeps_published_date_and_drops_missing_urls(self):
        data = {"results": [
            {"title": "甲", "url": "https://a.example/1", "content": "c1", "published_date": "2026-01-01"},
            {"title": "无链接", "url": "", "content": "c2"},
            "not-a-dict",
        ]}
        results = _normalize(data, "2026-10-30 10:00:00")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "甲")
        self.assertEqual(results[0].published_at, "2026-01-01")
        self.assertEqual(results[0].retrieved_at, "2026-10-30 10:00:00")

    def test_safe_url_rejects_non_http(self):
        self.assertEqual(web_context.safe_url("https://example.com/a"), "https://example.com/a")
        self.assertEqual(web_context.safe_url("http://example.com"), "http://example.com")
        self.assertEqual(web_context.safe_url("javascript:alert(1)"), "")
        self.assertEqual(web_context.safe_url("data:text/html,hi"), "")
        self.assertEqual(web_context.safe_url("ftp://example.com"), "")

    def test_deduplicate_by_url(self):
        a = SearchResult("", "a", "https://x/1", "c")
        b = SearchResult("", "b", "https://x/1", "c2")
        c = SearchResult("", "c", "https://x/2", "c3")
        self.assertEqual(web_context.deduplicate([a, b, c]), [a, c])

    def test_trim_evidence_caps_sources_and_chars(self):
        rs = [SearchResult("", f"t{i}", f"https://x/{i}", "字" * 900) for i in range(6)]
        trimmed = web_context.trim_evidence(rs, max_sources=4, max_total_chars=1000, per_source_chars=600)
        self.assertLessEqual(len(trimmed), 4)
        for r in trimmed:
            self.assertLessEqual(len(r.content), 601)  # 600 + ellipsis

    def test_build_web_prompt_contains_citations_and_instruction(self):
        rs = [SearchResult("S1", "来源标题", "https://example.com/a", "正文内容", "2026-01-01", "2026-10-30 10:00:00")]
        prompt = web_context.build_web_prompt("问题？", rs)
        self.assertIn("[S1]", prompt)
        self.assertIn("正文：正文内容", prompt)
        self.assertIn("不得编造来源或链接", prompt)
        sources = web_context.render_sources(rs)
        self.assertIn("example.com", sources)
        self.assertIn("来源标题", sources)

    def test_unknown_citations(self):
        self.assertEqual(web_context.unknown_citations("见 [S1] 与 [S9]", ["S1"]), {"S9"})
        self.assertEqual(web_context.unknown_citations("无引用", ["S1"]), set())

    def test_search_web_requires_enabled_and_key(self):
        with patch("core.web_search.settings", _settings(web_search_enabled=False)), \
             patch.dict("os.environ", {"TAVILY_API_KEY": "x"}):
            with self.assertRaisesRegex(WebSearchError, "LUX_WEB_SEARCH_ENABLED"):
                search_web("q")
        # Missing key, enabled: raise before any request.
        with patch("core.web_search.settings", _settings(web_search_enabled=True)), \
             patch.dict("os.environ", {"TAVILY_API_KEY": ""}):
            with self.assertRaisesRegex(WebSearchError, "TAVILY_API_KEY"):
                search_web("q")

    def test_search_web_normalizes_and_retries_once_on_transient(self):
        payload = {"results": [{"title": "t", "url": "https://x/1", "content": "c"}]}
        with patch("core.web_search.settings", _settings(web_search_enabled=True)), \
             patch.dict("os.environ", {"TAVILY_API_KEY": "k"}), \
             patch("core.web_search._post_json", side_effect=[WebSearchError("连接失败", retryable=True), payload]) as post:
            results = search_web("q")
        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)

    def test_search_web_does_not_retry_auth_error(self):
        with patch("core.web_search.settings", _settings(web_search_enabled=True)), \
             patch.dict("os.environ", {"TAVILY_API_KEY": "k"}), \
             patch("core.web_search._post_json", side_effect=WebSearchError("密钥无效", retryable=False)) as post:
            with self.assertRaisesRegex(WebSearchError, "密钥无效"):
                search_web("q")
        self.assertEqual(post.call_count, 1)


class WebSearchFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_queue, self.old_db, self.old_guard = webapp.task_queue, webapp.db, webapp.guard
        self.old_stream = webapp.configured_llm_stream
        self.old_configured = webapp.web_search_configured
        self.old_search = webapp.search_web
        self.old_router, self.old_library_model = webapp._router_model, webapp._library_model
        webapp.task_queue = TaskQueue(root / "tasks.db")
        webapp.db = PromptDB(root / "prompts.db")
        webapp.guard = RedlineGuard(log_dir=root / "logs")
        # 意图路由与文件选择用固定桩，测试不连本地模型。
        webapp._router_model = Mock(return_value=json.dumps(
            {"action": "chat", "object_type": "", "relation": "new",
             "source_scope": "none", "missing_fields": [], "needs_clarification": False}))
        webapp._library_model = Mock(return_value='{"intent":"other","ids":[]}')

        def fake_stream(prompt: str = "", system: str = "", messages=None):
            yield "据 [S1] 所述，答案是 X。"

        webapp.configured_llm_stream = fake_stream
        webapp.web_search_configured = Mock(return_value=(True, "已配置"))
        webapp.search_web = Mock(return_value=[
            SearchResult("", "公开来源", "https://example.com/a", "正文内容",
                         "2026-01-01", "2026-10-30 10:00:00")
        ])

    def tearDown(self):
        webapp.task_queue, webapp.db, webapp.guard = self.old_queue, self.old_db, self.old_guard
        webapp.configured_llm_stream = self.old_stream
        webapp.web_search_configured = self.old_configured
        webapp.search_web = self.old_search
        webapp._router_model, webapp._library_model = self.old_router, self.old_library_model
        self.tmp.cleanup()

    def _run(self, message, web_search=True):
        updates = list(webapp.respond(message, [], "chat", None, None, web_search))
        self.assertTrue(all(len(u) == 5 for u in updates))
        return updates

    def test_web_search_injects_real_sources_and_records_metadata(self):
        updates = self._run("最近有什么公开新规定")
        final_history = updates[-1][0]
        answer = final_history[-1]["content"]
        self.assertIn("联网来源", answer)
        self.assertIn("example.com", answer)
        self.assertIn("2026-01-01", answer)
        task = webapp.task_queue.get(updates[-1][-1])
        self.assertEqual(task["status"], "COMPLETED")
        meta = json.loads(task["result"])
        self.assertEqual(meta.get("web_sources"), 1)
        self.assertIn("web_unknown_citations", meta)

    def test_disabled_search_shows_reason_without_calling_search(self):
        webapp.web_search_configured.return_value = (False, "服务器未开启联网搜索（LUX_WEB_SEARCH_ENABLED=false）")
        updates = self._run("查询")
        answer = updates[-1][0][-1]["content"]
        self.assertIn("未开启", answer)
        webapp.search_web.assert_not_called()

    def test_outbound_block_stops_before_search(self):
        with patch.object(webapp.guard, "check_outbound",
                          return_value={"ok": False, "categories": ["手机号"], "hits": [], "message": ""}):
            updates = self._run("把手机号 13800138000 发到网上查一下")
        answer = updates[-1][0][-1]["content"]
        self.assertIn("已拦截", answer)
        webapp.search_web.assert_not_called()

    def test_empty_results_reports_no_evidence(self):
        webapp.search_web.return_value = []
        updates = self._run("查询")
        answer = updates[-1][0][-1]["content"]
        self.assertIn("未取得联网资料", answer)

    def test_search_failure_reports_and_fails_task(self):
        webapp.search_web.side_effect = WebSearchError("联网搜索超时，本次未取得联网资料。")
        updates = self._run("查询")
        answer = updates[-1][0][-1]["content"]
        self.assertIn("超时", answer)
        task = webapp.task_queue.get(updates[-1][-1])
        self.assertEqual(task["status"], "FAILED")

    def test_web_mode_keeps_web_answer_for_file_like_request(self):
        # 联网搜索现在是明确的来源选择：即使话里带“找文件”，也不能被本地检索吞掉。
        webapp.search_web.return_value = [
            SearchResult("", "公开来源", "https://example.com/a", "正文内容", "2026-01-01", "2026-10-30 10:00:00")
        ]
        updates = self._run("帮我找一下最近的文件规定")
        self.assertIn("联网来源", updates[-1][0][-1]["content"])

    def test_web_search_desensitizes_internal_names_before_sending(self):
        # 区域/客户/单位等脱敏级内容须在发往搜索服务前替换成占位符，内部专名不出网。
        webapp.guard.config["sensitive_terms"] = {"production_lines": ["东区甲岗"]}
        updates = self._run("东区甲岗 最近的公开动态")
        sent_query = webapp.search_web.call_args[0][0]
        self.assertNotIn("东区甲岗", sent_query)
        self.assertIn("【区域", sent_query)
        task = webapp.task_queue.get(updates[-1][-1])
        meta = json.loads(task["result"])
        self.assertEqual(meta.get("web_masked_terms"), 1)


if __name__ == "__main__":
    unittest.main()
