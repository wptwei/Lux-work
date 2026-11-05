"""第二阶段回归：上下文感知的意图路由。

覆盖计划第 6 节的验收场景。规则层不调用模型；模型层失败必须降级为
普通对话，不能默认落到文件检索，也不能声称已完成操作。
"""
import json
import unittest

from core.conversation_state import TaskState
from core.intent_router import (
    IntentRoute, route_intent, rule_route, validate_route,
)


def file_history(*version_ids):
    """模拟检索器写入的助手消息（含 data-version-id 标记）。"""
    cards = "".join(f'<article data-version-id="{vid}">文件</article>' for vid in version_ids)
    return [{"role": "user", "content": "帮我找文件"},
            {"role": "assistant", "content": cards}]


class RuleScenarioTests(unittest.TestCase):
    """计划 6 节验收表里的规则场景。"""

    def test_draft_does_not_return_file_cards(self):
        route = route_intent("帮我写一份巡检制度")
        self.assertEqual("draft", route.action)
        self.assertEqual("none", route.source_scope)

    def test_explicit_file_request_routes_to_library(self):
        route = route_intent("找出我上传的巡检制度")
        self.assertEqual("retrieve", route.action)
        self.assertEqual("local_library", route.source_scope)

    def test_negation_is_not_ignored(self):
        route = route_intent("不要找文件，解释一下这个概念")
        self.assertEqual("explain", route.action)
        self.assertEqual("none", route.source_scope)
        self.assertEqual("negated_retrieve", route.reason)

    def test_tail_constraint_is_a_revision(self):
        route = route_intent("改成 100 字，别用表格")
        self.assertEqual("revise", route.action)
        self.assertEqual("continue", route.relation)

    def test_correction_updates_object_type(self):
        route = route_intent("不是通知，是情况说明")
        self.assertEqual("correct", route.relation)
        self.assertEqual("statement", route.object_type)

    def test_task_switch_is_explicit(self):
        route = route_intent("换个问题，帮我翻译一下这段话")
        self.assertEqual("switch", route.relation)

    def test_plain_greeting_is_not_a_file_search(self):
        route = route_intent("你好")
        self.assertEqual("chat", route.action)
        self.assertEqual("rule_inconclusive", route.reason)

    def test_explaining_a_policy_is_not_a_file_delivery(self):
        route = route_intent("解释一下门禁制度")
        self.assertEqual("explain", route.action)
        self.assertEqual("none", route.source_scope)


class ReferenceTests(unittest.TestCase):
    def test_second_of_two_files_maps_to_a_real_candidate(self):
        route = route_intent("第二份", history=file_history("v-a", "v-b"))
        self.assertEqual("retrieve", route.action)
        self.assertEqual("v-b", route.target_id)
        self.assertEqual("ordinal_reference", route.reason)

    def test_ordinal_beyond_candidates_asks_once_instead_of_guessing(self):
        route = route_intent("第三份", history=file_history("v-a", "v-b"))
        self.assertTrue(route.needs_clarification)
        self.assertEqual("clarify", route.action)
        self.assertIsNone(route.target_id)

    def test_ordinal_without_any_candidates_does_not_invent_a_file(self):
        route = route_intent("第二份", history=[])
        self.assertNotEqual("retrieve", route.action)
        self.assertIsNone(route.target_id)

    def test_model_target_must_be_a_known_id(self):
        def model(prompt, system):
            return json.dumps({"action": "retrieve", "object_type": "policy",
                               "relation": "continue", "source_scope": "local_library",
                               "target_id": "made-up", "missing_fields": [],
                               "needs_clarification": False})

        route = route_intent("那个文件", model=model, allowed_file_ids=["v-a"])
        self.assertEqual("clarify", route.action)
        self.assertEqual("unknown_target", route.reason)


class ModelFallbackTests(unittest.TestCase):
    def test_invalid_json_falls_back_to_chat_not_retrieval(self):
        route = route_intent("那个东西", model=lambda p, s: "这不是 JSON")
        self.assertEqual("chat", route.action)
        self.assertEqual("router_unavailable", route.reason)

    def test_unknown_action_is_rejected(self):
        payload = {"action": "delete_everything", "object_type": "",
                   "relation": "new", "source_scope": "none"}
        with self.assertRaises(ValueError):
            validate_route(payload)

    def test_unsupported_scope_is_rejected(self):
        payload = {"action": "chat", "object_type": "", "relation": "new",
                   "source_scope": "internet_everything"}
        with self.assertRaises(ValueError):
            validate_route(payload)

    def test_valid_model_route_is_accepted(self):
        def model(prompt, system):
            return json.dumps({"action": "explain", "object_type": "policy",
                               "relation": "continue", "source_scope": "none",
                               "missing_fields": [], "needs_clarification": False})
        route = route_intent("那这个呢", model=model)
        self.assertEqual("explain", route.action)
        self.assertEqual("model", route.reason)

    def test_router_model_only_sees_bounded_state(self):
        seen = {}

        def model(prompt, system):
            seen["payload"] = json.loads(prompt)
            return json.dumps({"action": "chat", "object_type": "", "relation": "new",
                               "source_scope": "none", "missing_fields": [],
                               "needs_clarification": False})

        state = TaskState(task_id="t", action="draft", object_type="notice",
                          constraints={"max_chars": 300})
        route_intent("那再改改", state=state, model=model)
        self.assertEqual("notice", seen["payload"]["state"]["object_type"])
        self.assertEqual({"max_chars": 300}, seen["payload"]["state"]["constraints"])


class StateIntegrationTests(unittest.TestCase):
    def test_continue_with_state_keeps_object_between_turns(self):
        from core.conversation_state import ConversationStore
        store = ConversationStore()
        first = rule_route("帮我写一份通知", has_state=False)
        store.apply_route("u1", first, "帮我写一份通知")
        self.assertEqual("notice", store.get("u1").object_type)

        second = route_intent("再简短一点", state=store.get("u1"))
        self.assertEqual("continue", second.relation)
        store.apply_route("u1", second, "再简短一点")
        self.assertEqual("notice", store.get("u1").object_type)


if __name__ == "__main__":
    unittest.main()
