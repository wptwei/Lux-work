"""自动记录与自动奖惩匹配的端到端应用层测试（虚构文本，不执行奖惩）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as webapp
from core.incident_intake import IncidentIntake
from core.reward_penalty_store import RewardPenaltyStore
from core.task_queue import TaskQueue


class AllowGuard:
    def scan(self, text, **kwargs):  # noqa: ARG002
        return "allow", [], {}

    def desensitize(self, text, mapping):  # noqa: ARG002
        return text


SAMPLE = "帮我记录：一名供应商未由ICT人员陪同，在NPI区域内独自行走。"


class AutoIncidentRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (
            webapp.task_queue, webapp.guard, webapp.incident_intake,
            webapp.reward_penalty_store, webapp.record_last_answer,
        )
        webapp.task_queue = TaskQueue(root / "tasks.db")
        webapp.guard = AllowGuard()
        webapp.incident_intake = IncidentIntake(root / "incidents.db")
        webapp.reward_penalty_store = RewardPenaltyStore(root / "incidents.db")
        webapp.incident_intake.match_sink = webapp._sync_reward_log
        webapp.record_last_answer = Mock()
        webapp.conversation_store.reset("system")

    def tearDown(self):
        (webapp.task_queue, webapp.guard, webapp.incident_intake,
         webapp.reward_penalty_store, webapp.record_last_answer) = self._saved
        self.tmp.cleanup()

    def _hint(self, submission_id, channel="manual", record=True):
        return json.dumps({
            "channel": channel, "record": record, "submission_id": submission_id,
            "conversation_id": "conv-test", "message_id": "msg-" + submission_id,
            "edited": False,
        })

    def _run(self, message, hint):
        updates = list(webapp.respond(
            message, [], "chat", None, None, "local", intake_hint=hint,
        ))
        self.assertTrue(all(len(item) == 5 for item in updates))
        return updates

    def test_chat_record_saves_then_updates_match_result(self):
        updates = self._run(SAMPLE, self._hint("chat-1"))
        self.assertEqual(2, len(updates))
        first = updates[0][0][-1]["content"]
        last = updates[-1][0][-1]["content"]
        self.assertIn("已记录", first)
        self.assertIn("正在匹配奖惩条款", first)
        self.assertIn("INC-", last)
        self.assertIn("系统匹配建议", last)
        self.assertIn("条款原文", last)
        self.assertEqual(1, len(webapp.incident_intake.list_recent(username="system")))
        rows = webapp.reward_penalty_store.list_recent(username="system", all_users=True)
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["incident_id"].startswith("INC-"))
        incident = webapp.incident_intake.get(rows[0]["incident_id"])
        self.assertEqual("registered", incident["intake_status"])
        self.assertEqual("RECORDED", incident["workflow_status"])
        self.assertEqual("candidate", incident["match_status"])

    def test_duplicate_chat_submission_does_not_create_second_event(self):
        hint = self._hint("chat-dup")
        self._run(SAMPLE, hint)
        self._run(SAMPLE, hint)
        rows = webapp.incident_intake.list_recent(username="system")
        self.assertEqual(1, len(rows))
        self.assertEqual(1, len(webapp.reward_penalty_store.list_recent(
            username="system", all_users=True)))

    def test_chat_supplement_creates_new_version_and_rematches(self):
        self._run(SAMPLE, self._hint("chat-v1"))
        incident_id = webapp.incident_intake.list_recent(username="system")[0]["id"]
        update = f"补充记录 {incident_id}：保安已到场劝阻，人员未进入管制区。"
        updates = self._run(update, self._hint("chat-v2"))
        last = updates[-1][0][-1]["content"]
        self.assertIn("已补充", last)
        self.assertIn("v2", last)
        incident = webapp.incident_intake.get(incident_id)
        self.assertEqual(2, incident["current_version"])
        self.assertIn("【补充】", incident["submission"]["text_snapshot"])
        self.assertEqual(2, len(webapp.incident_intake.match_history(incident_id)))
        rows = webapp.reward_penalty_store.list_recent(username="system", all_users=True)
        self.assertEqual(1, len(rows))
        self.assertEqual(incident_id, rows[0]["incident_id"])

    def test_detail_and_amend_form_show_versions_and_clauses(self):
        self._run(SAMPLE, self._hint("chat-detail"))
        incident_id = webapp.incident_intake.list_recent(username="system")[0]["id"]
        detail = webapp.render_incident_detail(incident_id)
        self.assertIn(incident_id, detail)
        self.assertIn("条款原文", detail)
        self.assertIn("匹配历史", detail)
        self.assertIn("当前版本", detail)
        amended = webapp.amend_incident(
            incident_id, "更正", "更正：供应商在NPI区域内无人陪同独自游走。",
        )
        self.assertIn(incident_id, amended)
        self.assertIn("v2", amended)
        self.assertIn("更正登记", amended)
        incident = webapp.incident_intake.get(incident_id)
        self.assertEqual(2, incident["current_version"])
        self.assertNotIn("【补充】", incident["submission"]["text_snapshot"])

    def test_match_failure_keeps_event_and_does_not_claim_success(self):
        class FailingMatcher:
            ruleset_version = "rules-test"
            matcher_version = "matcher-test"

            def match(self, text):  # noqa: ARG002
                raise RuntimeError("unavailable")

        with patch.object(webapp, "reward_penalty_matcher", FailingMatcher()):
            updates = self._run(SAMPLE, self._hint("chat-fail"))
        last = updates[-1][0][-1]["content"]
        self.assertIn("INC-", last)
        self.assertIn("匹配失败", last)
        self.assertEqual(1, len(webapp.incident_intake.list_recent(username="system")))
        incident = webapp.incident_intake.list_recent(username="system")[0]
        self.assertEqual("failed", incident["match_status"])


if __name__ == "__main__":
    unittest.main()
