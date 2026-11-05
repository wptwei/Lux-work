"""Real SQLite workflow checks; browser mic and ASR remain separate manual checks."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app as webapp
from core.incident_conversation import (
    IncidentConversationStore, choose_question, question_answer, relative_change, replace_field,
)
from core.incident_intake import IncidentIntake, Submission, SubmissionConflict
from core.reward_penalty_store import RewardPenaltyStore
from core.task_queue import TaskQueue
from tests.test_incident_regressions import AllowGuard, FakeUsers


REPORT = "14:20，东门发现一名供应商没人陪同，保安已经拦住了。"


class IncidentFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patchers = [
            patch.object(webapp, "guard", AllowGuard()),
            patch.object(webapp, "incident_intake", IncidentIntake(root / "incidents.db")),
            patch.object(webapp, "reward_penalty_store", RewardPenaltyStore(root / "incidents.db")),
            patch.object(webapp, "task_queue", TaskQueue(root / "tasks.db")),
            patch.object(webapp, "user_store", FakeUsers(allow=False)),
            patch.object(webapp, "record_last_answer", Mock()),
            patch.object(webapp, "incident_conversations", IncidentConversationStore()),
        ]
        for p in self.patchers:
            p.start()
        self.intake = webapp.incident_intake
        self.req = SimpleNamespace(username="alice", session_hash="tab-a")
        self.seq = 0

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.temp.cleanup()

    def chat(self, text, request=None, channel="voice_assistant"):
        self.seq += 1
        hint = json.dumps({"channel": channel, "submission_id": f"voice-{self.seq}"})
        return list(webapp.respond(text, [], "chat", None, request or self.req, "local", hint))

    def create(self):
        return list(webapp.record_incident_stream(REPORT, submission_id="report-1", request=self.req))[-1][4]

    def test_first_yield_commits_before_matcher_runs(self):
        with patch.object(webapp.reward_penalty_matcher, "match", wraps=webapp.reward_penalty_matcher.match) as match:
            stream = webapp.record_incident_stream(REPORT, submission_id="report-1", request=self.req)
            first = next(stream)
            self.assertIn("已记录", first[0])
            self.assertIn("正在匹配", first[0])
            match.assert_not_called()
            self.assertIsNotNone(self.intake.get(first[4]))
            self.assertEqual({"pending": 1}, self.intake.task_counts())
            self.assertIn("进入管制区前", first[2])
            list(stream)
            self.assertTrue(match.called)

    def test_client_disconnect_after_ack_keeps_durable_task(self):
        stream = webapp.record_incident_stream(REPORT, request=self.req)
        first = next(stream)
        stream.close()
        self.assertIsNotNone(self.intake.get(first[4]))
        self.assertEqual({"pending": 1}, self.intake.task_counts())
        self.intake.process_incident(first[4], webapp.reward_penalty_matcher)
        self.assertIsNotNone(self.intake.latest_match(first[4]))

    def test_database_failure_never_acknowledges_saved(self):
        with patch.object(self.intake, "submit", side_effect=RuntimeError("db unavailable")):
            frames = list(webapp.record_incident_stream(REPORT, request=self.req))
        self.assertEqual(1, len(frames))
        self.assertIn("未保存", frames[0][0])
        self.assertIsNone(webapp.incident_conversations.get("alice", "tab-a"))

    def test_match_failure_preserves_saved_event(self):
        with patch.object(webapp.reward_penalty_matcher, "match", side_effect=RuntimeError("matcher down")):
            frames = list(webapp.record_incident_stream(REPORT, request=self.req))
        self.assertIn("已记录", frames[0][0])
        self.assertIn("匹配失败", frames[-1][0])
        self.assertIsNotNone(self.intake.get(frames[-1][4]))

    def test_voice_answer_amends_same_event_and_does_not_ask_again(self):
        self.chat("帮我记录：" + REPORT)
        before = self.intake.list_recent(username="alice")[0]
        frames = self.chat("没进去")
        after = self.intake.get(before["id"])
        self.assertEqual(1, len(self.intake.list_recent(username="alice")))
        self.assertEqual(2, after["current_version"])
        self.assertIn("人员未进入受限区域", after["submission"]["text_snapshot"])
        self.assertEqual("voice_assistant", after["submission"]["source_channel"])
        self.assertNotIn("只补充一个关键事实", frames[-1][0][-1]["content"])
        self.assertIsNone(webapp.incident_conversations.get("alice", "tab-a").question)

    def test_skip_keeps_version_and_question_does_not_repeat(self):
        event = self.create()
        self.chat("不清楚")
        self.assertEqual(1, self.intake.get(event)["current_version"])
        self.assertIsNone(webapp.incident_conversations.get("alice", "tab-a").question)
        self.assertIsNone(webapp._resolve_incident_followup("今天天气怎么样", self.req))

    def test_form_answer_saves_first_then_rematches(self):
        event = self.create()
        stream = webapp.answer_incident_question(event, "没进去", "answer-1", self.req)
        first = next(stream)
        self.assertEqual(2, self.intake.get(event)["current_version"])
        self.assertEqual("pending", self.intake.get(event)["match_status"])
        self.assertEqual(event, first[4])
        list(stream)
        self.assertEqual(2, len(self.intake.match_history(event)))

    def test_relative_location_keeps_other_facts_and_history(self):
        event = self.create()
        self.chat("刚才地点说错了，改成北门")
        item = self.intake.get(event)
        text = item["submission"]["text_snapshot"]
        self.assertIn("北门", text)
        self.assertNotIn("东门", text)
        self.assertIn("14:20", text)
        self.assertIn("保安已经拦住了", text)
        self.assertEqual(REPORT, self.intake.submissions(event)[0]["text_snapshot"])
        self.assertEqual(2, item["current_version"])
        self.chat("再补充一句，现场已经清理")
        self.assertIn("现场已经清理", self.intake.get(event)["submission"]["text_snapshot"])
        self.assertEqual(3, self.intake.get(event)["current_version"])

    def test_user_and_tab_isolation(self):
        event = self.create()
        for req in (SimpleNamespace(username="alice", session_hash="tab-b"),
                    SimpleNamespace(username="bob", session_hash="tab-a")):
            reply = self.chat("刚才地点说错了，改成北门", req)[-1][0][-1]["content"]
            self.assertIn("没有明确", reply)
        self.assertEqual(1, self.intake.get(event)["current_version"])

    def test_stale_current_version_never_overwrites_another_edit(self):
        event = self.create()
        self.intake.submit(Submission(submission_id="outside-edit", text="北门现场已经清理完成。",
                                     incident_id=event, operation="correct"), actor="alice", region="H")
        reply = self.chat("刚才地点说错了，改成南门")[-1][0][-1]["content"]
        self.assertIn("已经更新", reply)
        self.assertEqual(2, self.intake.get(event)["current_version"])

    def test_atomic_expected_version_check_and_idempotent_retry(self):
        event = self.create()
        sub = Submission(submission_id="correct-v2", text="北门现场已经清理完成。",
                         incident_id=event, operation="correct", expected_version=1)
        first = self.intake.submit(sub, actor="alice", region="H")
        second = self.intake.submit(sub, actor="alice", region="H")
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["version"], second["version"])
        sub.submission_id = "late-correction"
        with self.assertRaises(SubmissionConflict):
            self.intake.submit(sub, actor="alice", region="H")

    def test_clear_chat_forgets_current_event(self):
        event = self.create()
        webapp.clear_incident_chat(self.req)
        self.chat("刚才地点说错了，改成北门")
        self.assertEqual(1, self.intake.get(event)["current_version"])

    def test_expired_context_cannot_be_used(self):
        self.create()
        with patch("core.incident_conversation.time.monotonic", return_value=10**15):
            self.assertIsNone(webapp.incident_conversations.get("alice", "tab-a"))

    def test_selecting_uneditable_record_does_not_leave_previous_target(self):
        original = self.create()
        other = self.intake.submit(Submission(submission_id="bob-record", text=REPORT), actor="bob", region="H")
        webapp.open_incident_record(SimpleNamespace(index=(0, 0)), [other["incident_id"]], self.req)
        reply = self.chat("刚才地点说错了，改成北门")[-1][0][-1]["content"]
        self.assertIn("没有明确", reply)
        self.assertEqual(1, self.intake.get(original)["current_version"])

    def test_latest_explicit_selection_is_the_only_target(self):
        original = self.create()
        other = self.intake.submit(Submission(submission_id="second-record", text=REPORT), actor="alice", region="H")
        webapp.open_incident_record(SimpleNamespace(index=(0, 0)), [other["incident_id"]], self.req)
        self.chat("刚才地点说错了，改成北门")
        self.assertEqual(1, self.intake.get(original)["current_version"])
        self.assertEqual(2, self.intake.get(other["incident_id"])["current_version"])

    def test_ambiguous_locations_require_explicit_edit(self):
        with self.assertRaises(ValueError):
            replace_field("东门发现异常，南门人员增援。", "地点", "北门")
        self.assertEqual("发生地点：北门。\n现场有异常情况。", replace_field("现场有异常情况。", "地点", "北门"))

    def test_only_material_questions_and_explicit_answers(self):
        self.assertIsNone(choose_question("人员未进入管制区，已被保安拦截。"))
        self.assertIsNone(choose_question("监控离线，需要检修。"))
        question = choose_question(REPORT)
        self.assertEqual("entry", question.key)
        self.assertIsNone(question_answer(question, "怎么处罚？"))
        self.assertEqual("", question_answer(question, "跳过"))
        self.assertIsNone(relative_change("怎么修改记录？"))


if __name__ == "__main__":
    unittest.main()
