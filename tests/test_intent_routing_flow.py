"""路由在 app.respond 里的实际行为：写作用不触发文件检索，检索仅由路由决定。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import app as webapp
from core.redline_guard import RedlineGuard
from core.db import PromptDB
from core.task_queue import TaskQueue


class RespondRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (webapp.task_queue, webapp.db, webapp.guard,
                       webapp.configured_llm_stream, webapp.retrieve_files)
        webapp.task_queue = TaskQueue(root / "tasks.db")
        webapp.db = PromptDB(root / "prompts.db")
        webapp.guard = RedlineGuard(log_dir=root / "logs")
        webapp.conversation_store.reset("anonymous")

        def fake_stream(prompt="", system="", messages=None):
            yield "处理完成"

        webapp.configured_llm_stream = fake_stream
        webapp.retrieve_files = Mock(return_value=None)

    def tearDown(self):
        (webapp.task_queue, webapp.db, webapp.guard,
         webapp.configured_llm_stream, webapp.retrieve_files) = self._saved
        self.tmp.cleanup()

    def _run(self, message):
        updates = list(webapp.respond(message, [], "chat", None, None, False))
        self.assertTrue(all(len(u) == 5 for u in updates))
        return updates

    def test_draft_request_never_opens_file_retrieval(self):
        self._run("帮我写一份巡检制度")
        webapp.retrieve_files.assert_not_called()

    def test_explain_request_with_negation_never_opens_file_retrieval(self):
        self._run("不要找文件，解释一下这个概念")
        webapp.retrieve_files.assert_not_called()

    def test_explicit_file_request_does_open_retrieval(self):
        webapp.retrieve_files.return_value = "找到 1 份文件"
        self._run("找出我上传的巡检制度")
        webapp.retrieve_files.assert_called_once()

    def test_task_metadata_records_intent_without_plaintext(self):
        updates = self._run("帮我写一份巡检制度")
        task = webapp.task_queue.get(updates[-1][-1])
        meta = json.loads(task["result"])
        self.assertEqual("draft", meta.get("intent_action"))
        self.assertIn("intent_reason", meta)
        self.assertNotIn("巡检制度", json.dumps(meta, ensure_ascii=False))

    def test_long_draft_message_reaches_model_without_history(self):
        # 上一轮被拦截的内容不得带进本轮
        history = [{"role": "user", "content": "联系 13800001234"},
                   {"role": "assistant", "content": "🚫 **已拦截，未送公网。发现手机号**"}]
        updates = list(webapp.respond("帮我写一份通知", history, "chat", None, None, False))
        task = webapp.task_queue.get(updates[-1][-1])
        self.assertEqual("COMPLETED", task["status"])
        self.assertEqual(0, json.loads(task["result"]).get("history_turns"))


if __name__ == "__main__":
    unittest.main()
