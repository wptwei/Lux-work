from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import app as webapp
from core.db import PromptDB
from core.redline_guard import RedlineGuard
from core.task_queue import TaskQueue


class V06TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_queue = webapp.task_queue
        self.old_db = webapp.db
        self.old_guard = webapp.guard
        self.old_stream = webapp.configured_llm_stream
        self.old_router = webapp._router_model
        webapp.task_queue = TaskQueue(root / "tasks.db")
        webapp.db = PromptDB(root / "prompts.db")
        webapp.guard = RedlineGuard(log_dir=root / "logs")
        # 意图路由用固定桩：测试不应连本地模型，否则结果随模型响应而变。
        webapp._router_model = lambda prompt, system: (
            '{"action": "chat", "object_type": "", "relation": "new", '
            '"source_scope": "none", "missing_fields": [], "needs_clarification": false}')

        def fake_stream(prompt: str = "", system: str = "", messages=None):
            yield "处理完成"

        webapp.configured_llm_stream = fake_stream

    def tearDown(self):
        webapp.task_queue = self.old_queue
        webapp.db = self.old_db
        webapp.guard = self.old_guard
        webapp.configured_llm_stream = self.old_stream
        webapp._router_model = self.old_router
        self.tmp.cleanup()

    def test_chat_task_runs_to_completion_without_storing_plaintext(self):
        prompt = "这是不应写入任务台账的业务测试原文"
        updates = list(webapp.respond(prompt, [], "chat"))
        self.assertGreaterEqual(len(updates), 2)
        self.assertTrue(all(len(update) == 5 for update in updates))

        task_id = updates[-1][-1]
        task = webapp.task_queue.get(task_id)
        self.assertEqual("COMPLETED", task["status"])
        self.assertNotIn(prompt, task["payload"])
        self.assertNotIn(prompt, task["result"])

        tip = webapp.save_task_feedback(task_id, "可用", 3, "无需修改")
        self.assertIn("已记录", tip)
        self.assertEqual("可用", webapp.task_queue.get(task_id)["feedback"])

    def test_acceptance_feedback_records_issue_tags_without_manual_minutes(self):
        task_id = webapp.task_queue.create("chat", {}, title="验收测试", created_by="tester")
        webapp.task_queue.update(task_id, "COMPLETED")

        tip = webapp.save_acceptance_feedback(
            task_id,
            "需修改",
            ["内容有遗漏", "结构不合适"],
            "补充夜班数据",
        )

        task = webapp.task_queue.get(task_id)
        self.assertIn("调整后采用", tip)
        self.assertEqual("需修改", task["feedback"])
        self.assertEqual(0, task["actual_minutes"])
        self.assertIn("内容有遗漏", task["feedback_notes"])
        self.assertIn("补充夜班数据", task["feedback_notes"])

    def test_negative_acceptance_requires_a_reason(self):
        task_id = webapp.task_queue.create("chat", {}, title="验收测试", created_by="tester")
        webapp.task_queue.update(task_id, "COMPLETED")

        tip = webapp.save_acceptance_feedback(task_id, "不可用", [], "")

        self.assertIn("至少选择一个问题", tip)
        self.assertEqual("", webapp.task_queue.get(task_id)["feedback"])


if __name__ == "__main__":
    unittest.main()
