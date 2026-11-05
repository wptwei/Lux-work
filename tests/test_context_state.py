"""第一阶段回归：带角色的消息、尾部约束保留、会话任务状态、专项切换不清历史。"""
import json
import tempfile
import unittest
from pathlib import Path

from core.chat_context import build_messages, build_contextual_prompt, plan_context
from core.conversation_state import (
    ConversationStore, TaskState, extract_constraints, state_brief,
)
from core.redline_guard import RedlineGuard
from core.providers import _chat_messages


def turn(user, reply):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]


class MessageRoleTests(unittest.TestCase):
    def test_messages_keep_roles_instead_of_one_user_blob(self):
        history = turn("门禁是什么", "门禁是出入控制系统") + turn("它有哪些类型", "刷卡、人脸")
        messages, used = build_messages(history, "那哪种更安全", system="你是助手")
        self.assertEqual(2, used)
        self.assertEqual(["system", "user", "assistant", "user", "assistant", "user"],
                         [m["role"] for m in messages])
        self.assertEqual("你是助手", messages[0]["content"])
        self.assertEqual("那哪种更安全", messages[-1]["content"])

    def test_current_question_is_never_truncated(self):
        long_question = "甲" * 5000
        messages, _ = build_messages([], long_question, max_chars=100)
        self.assertEqual(long_question, messages[-1]["content"])

    def test_blocked_and_failed_turns_stay_out_of_messages(self):
        history = (turn("联系13800001234", "🚫 **已拦截，未送公网。发现手机号**")
                   + turn("再试一次", "⚠️ 本地模型服务未连接")
                   + [{"role": "user", "content": "半途"},
                      {"role": "assistant", "content": "⏳ 正在生成…"}]
                   + turn("什么是尾随", "尾随是跟随他人进入"))
        messages, used = build_messages(history, "举个例子")
        self.assertEqual(1, used)
        self.assertNotIn("13800001234", json.dumps(messages, ensure_ascii=False))
        self.assertNotIn("⏳", json.dumps(messages, ensure_ascii=False))

    def test_providers_reject_unknown_roles(self):
        with self.assertRaises(Exception):
            _chat_messages("", messages=[{"role": "tool", "content": "x"}])
        with self.assertRaises(Exception):
            _chat_messages("", messages=[])

    def test_providers_accept_role_sequence(self):
        payload = _chat_messages("", messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ])
        self.assertEqual(["system", "user", "assistant"], [m["role"] for m in payload])


class TailConstraintTests(unittest.TestCase):
    def test_tail_constraint_survives_truncation(self):
        # 约束在 600 字之后，旧实现只留开头会丢掉它
        long_user = "背景" * 400 + "最后要求：不要表格，只保留第二段。"
        history = turn(long_user, "好的")
        prompt, _ = build_contextual_prompt(history, "继续", max_chars=600)
        self.assertIn("不要表格", prompt)
        messages, _ = build_messages(history, "继续", max_chars=600)
        self.assertIn("不要表格", json.dumps(messages, ensure_ascii=False))

    def test_truncated_message_is_bounded(self):
        messages, _ = build_messages(turn("甲" * 3000, "乙" * 3000), "x",
                                     max_chars=600, per_message_chars=200)
        for item in messages:
            if item["role"] != "user" or item["content"] != "x":
                self.assertLessEqual(len(item["content"]), 201)

    def test_plan_context_reports_shrink(self):
        plan = plan_context(system_chars=900, message_chars=2800, external=False,
                            configured_chars=1800, num_ctx=4096, num_predict=1024, max_turns=3)
        self.assertTrue(plan.truncated)
        self.assertEqual(0, plan.history_turns)
        external = plan_context(system_chars=900, message_chars=9000, external=True,
                                configured_chars=1800, num_ctx=4096, num_predict=1024, max_turns=3)
        self.assertEqual(1800, external.history_chars)


class ConversationStateTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = ConversationStore(ttl_seconds=600, clock=lambda: self.now)

    class Route:
        def __init__(self, action="draft", object_type="notice", relation="new"):
            self.action, self.object_type, self.relation = action, object_type, relation

    def test_constraints_are_extracted_and_overwritten(self):
        self.assertEqual({"max_chars": 300, "format": "paragraph"},
                         extract_constraints("写一份 300 字的情况说明，不要用表格"))
        self.assertEqual({"max_chars": 100}, extract_constraints("改成 100 字"))
        self.assertEqual({"format": "table"}, extract_constraints("用表格呈现"))

    def test_new_task_clears_old_task_constraints(self):
        self.store.apply_route("u1", self.Route(relation="new"), "写一份 300 字通知")
        self.assertEqual({"max_chars": 300}, self.store.get("u1").constraints)
        self.store.apply_route("u1", self.Route(action="chat", object_type="",
                                                relation="switch"), "换个问题")
        state = self.store.get("u1")
        self.assertEqual({}, state.constraints)
        self.assertEqual("", state.object_type)

    def test_continue_keeps_object_and_updates_constraint(self):
        self.store.apply_route("u1", self.Route(), "写一份 300 字通知")
        self.store.apply_route("u1", self.Route(action="revise", relation="continue"),
                               "改成 100 字")
        state = self.store.get("u1")
        self.assertEqual("notice", state.object_type)
        self.assertEqual(100, state.constraints["max_chars"])

    def test_model_inference_never_becomes_a_confirmed_fact(self):
        state = TaskState(task_id="t")
        state.add_fact("model", "模型猜的对象是通知")
        state.add_fact("user", "对象是情况说明")
        self.assertEqual(["对象是情况说明"], state.facts())

    def test_states_are_isolated_per_account(self):
        self.store.apply_route("alice", self.Route(), "写一份 300 字通知")
        self.assertEqual({}, self.store.get("bob").constraints)
        self.assertTrue(state_brief(self.store.get("bob")) == "")

    def test_state_expires_after_ttl(self):
        self.store.apply_route("u1", self.Route(), "写一份 300 字通知")
        self.now += 601
        state = self.store.get("u1")
        self.assertEqual({}, state.constraints)

    def test_brief_lists_object_and_constraints(self):
        self.store.apply_route("u1", self.Route(), "写一份 300 字的通知")
        brief = state_brief(self.store.get("u1"))
        self.assertIn("notice", brief)
        self.assertIn("max_chars=300", brief)


class SkillSwitchTests(unittest.TestCase):
    def test_entering_a_skill_keeps_previous_history(self):
        import app as webapp
        history = turn("上周巡检正常", "已收到")
        result, mode, _ = webapp.enter_skill("[管理岗] H区周报（动态创作）", history)
        self.assertEqual("H区周报（动态创作）", mode)
        self.assertEqual(["上周巡检正常", "已收到"], [m["content"] for m in result[:2]])
        self.assertTrue(result[-1]["content"].startswith("📌 **已进入专项"))

    def test_skill_notice_is_not_carried_into_the_model_context(self):
        import app as webapp
        result, _, _ = webapp.enter_skill("[管理岗] H区周报（动态创作）",
                                          turn("先前的素材", "已记录"))
        messages, used = build_messages(result, "继续")
        self.assertEqual(1, used)  # 只有“先前的素材”那一轮，进入提示被排除
        self.assertNotIn("已进入专项", json.dumps(messages, ensure_ascii=False))


class GuardMessageTests(unittest.TestCase):
    def setUp(self):
        self.guard = RedlineGuard(log_dir=tempfile.mkdtemp())

    def test_roles_survive_scan_and_masking(self):
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "联系 13800001234 确认"},
        ]
        events = list(self.guard.call_llm_stream_messages(
            messages, task_desc="测试", llm_stream=lambda messages: iter([]), external=False))
        self.assertEqual("rejected", events[-1]["result"]["status"])

    def test_masking_keeps_role_order(self):
        guard = RedlineGuard(log_dir=tempfile.mkdtemp())
        guard.config["sensitive_terms"] = {"production_lines": ["东区甲岗"]}
        captured = {}

        def stream(messages=None):
            captured["messages"] = messages
            yield "完成"

        messages = [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "东区甲岗今天情况"},
        ]
        events = list(guard.call_llm_stream_messages(
            messages, task_desc="测试", llm_stream=stream, external=True))
        self.assertEqual("ok", events[-1]["result"]["status"])
        sent = captured["messages"]
        self.assertEqual(["system", "user"], [m["role"] for m in sent])
        self.assertNotIn("东区甲岗", sent[1]["content"])
        self.assertIn("【区域", sent[1]["content"])

    def test_masked_placeholder_is_restored_in_output(self):
        guard = RedlineGuard(log_dir=tempfile.mkdtemp())
        guard.config["sensitive_terms"] = {"production_lines": ["东区甲岗"]}

        def stream(messages=None):
            # 模型只能看到占位符，复述时也只会复述占位符
            return iter(["【区域_1】今天正常"])

        events = list(guard.call_llm_stream_messages(
            [{"role": "user", "content": "东区甲岗今天情况"}],
            task_desc="测试", llm_stream=stream, external=True))
        self.assertEqual("ok", events[-1]["result"]["status"])
        self.assertIn("东区甲岗", events[-1]["result"]["output"])


if __name__ == "__main__":
    unittest.main()
