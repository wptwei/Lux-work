"""第 2A 步回归：聊天上下文、登录锁定、启动提示、手机号变体、事件响应配置、周报文案。"""
import json
import tempfile
import unittest
from pathlib import Path

from agents.incident_agent import IncidentAgent
from core.auth import UserStore
from core.chat_context import (
    FRAME_HEAD, build_contextual_prompt, clean_reply, history_char_budget,
)
from core.incident_response_config import (
    DRAFT_NOTE, get_response_config, load_response_config,
)
from core.redline_guard import RedlineGuard
from core.startup_checks import security_warnings
from core.ui_labels import weekly_copy

ROOT = Path(__file__).resolve().parent.parent


def turn(user, reply):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]


class ChatContextTests(unittest.TestCase):
    def test_first_turn_is_unchanged(self):
        self.assertEqual(("你好", 0), build_contextual_prompt([], "你好"))

    def test_previous_turns_are_included_in_order(self):
        history = turn("门禁是什么", "门禁是出入控制系统") + turn("它有哪些类型", "刷卡、人脸")
        prompt, used = build_contextual_prompt(history, "那哪种更安全")
        self.assertEqual(2, used)
        self.assertTrue(prompt.startswith(FRAME_HEAD))
        self.assertLess(prompt.index("门禁是什么"), prompt.index("它有哪些类型"))
        self.assertTrue(prompt.endswith("那哪种更安全"))

    def test_blocked_and_failed_turns_are_not_carried_over(self):
        # 回归点：被拦截那轮含手机号，带进去会让之后每一轮都被拦
        history = (
            turn("联系13800001234", "🚫 **已拦截，未送公网。发现手机号**")
            + turn("再试一次", "⚠️ 本地模型服务未连接")
            + [{"role": "user", "content": "半途"}, {"role": "assistant", "content": "⏳ 正在生成…"}]
            + [{"role": "assistant", "content": "📌 **已进入专项：周报**"}]
            + turn("什么是尾随", "尾随是跟随他人进入")
        )
        prompt, used = build_contextual_prompt(history, "举个例子")
        self.assertEqual(1, used)
        self.assertNotIn("13800001234", prompt)
        guard = RedlineGuard(log_dir=tempfile.mkdtemp())
        self.assertNotEqual("block", guard.scan(prompt)[0])

    def test_ui_decorations_are_stripped_from_replies(self):
        decorated = "🔒 _本次已自动脱敏 2 处敏感信息_\n\n---\n\n📌 _专项技能：周报（估算本次节省约20分钟）_\n\n---\n\n正文内容 ▌"
        self.assertEqual("正文内容", clean_reply(decorated))

    def test_turn_and_char_limits(self):
        history = sum((turn(f"问题{i}", f"回答{i}") for i in range(6)), [])
        prompt, used = build_contextual_prompt(history, "x", max_turns=2)
        self.assertEqual(2, used)
        self.assertNotIn("问题3", prompt)
        self.assertIn("问题5", prompt)
        long_turn = turn("甲" * 5000, "乙" * 5000)
        prompt, used = build_contextual_prompt(long_turn, "x", max_chars=400)
        self.assertLessEqual(len(prompt), 400 + len(FRAME_HEAD) + 40)

    def test_zero_turns_disables_history(self):
        self.assertEqual(("问", 0), build_contextual_prompt(turn("a", "b"), "问", max_turns=0))

    def test_local_budget_shrinks_with_long_input_but_external_does_not(self):
        common = dict(configured_chars=1800, num_ctx=4096, num_predict=1024)
        roomy = history_char_budget(system_chars=300, message_chars=200, external=False, **common)
        crowded = history_char_budget(system_chars=900, message_chars=2800, external=False, **common)
        external = history_char_budget(system_chars=900, message_chars=9000, external=True, **common)
        self.assertEqual(1800, roomy)
        self.assertEqual(0, crowded)
        self.assertEqual(1800, external)


class LoginLockoutTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = UserStore(
            Path(tempfile.mkdtemp()) / "users.json",
            max_failures=3, window_seconds=60, lock_seconds=120, clock=lambda: self.now,
        )
        self.store.add_user("zhangsan", "Str0ng!Passw0rd")

    def test_locks_after_repeated_failures_even_for_correct_password(self):
        for _ in range(3):
            self.assertFalse(self.store.authenticate("zhangsan", "wrong"))
        self.assertTrue(self.store.is_locked("zhangsan"))
        self.assertFalse(self.store.authenticate("zhangsan", "Str0ng!Passw0rd"))

    def test_lock_expires(self):
        for _ in range(3):
            self.store.authenticate("zhangsan", "wrong")
        self.now += 121
        self.assertTrue(self.store.authenticate("zhangsan", "Str0ng!Passw0rd"))

    def test_success_resets_the_counter(self):
        self.store.authenticate("zhangsan", "wrong")
        self.store.authenticate("zhangsan", "wrong")
        self.assertTrue(self.store.authenticate("zhangsan", "Str0ng!Passw0rd"))
        self.store.authenticate("zhangsan", "wrong")
        self.store.authenticate("zhangsan", "wrong")
        self.assertFalse(self.store.is_locked("zhangsan"))

    def test_failures_outside_window_do_not_accumulate(self):
        self.store.authenticate("zhangsan", "wrong")
        self.store.authenticate("zhangsan", "wrong")
        self.now += 61
        self.store.authenticate("zhangsan", "wrong")
        self.assertFalse(self.store.is_locked("zhangsan"))

    def test_unknown_accounts_are_throttled_the_same_way(self):
        for _ in range(3):
            self.store.authenticate("nobody", "x")
        self.assertTrue(self.store.is_locked("nobody"))

    def test_admin_can_unlock(self):
        for _ in range(3):
            self.store.authenticate("zhangsan", "wrong")
        self.store.unlock("zhangsan")
        self.assertTrue(self.store.authenticate("zhangsan", "Str0ng!Passw0rd"))


class StartupWarningTests(unittest.TestCase):
    def test_clean_local_setup_has_no_warnings(self):
        self.assertEqual([], security_warnings(host="127.0.0.1", share=False, app_password="", has_users=True))

    def test_lingering_weak_password_and_lan_http_are_reported(self):
        warnings = "\n".join(security_warnings(host="0.0.0.0", share=True, app_password="123456", has_users=True))
        for expected in ("明文 LUX_APP_PASSWORD", "密码策略", "明文 HTTP", "公网分享"):
            self.assertIn(expected, warnings)


class RedlineVariantTests(unittest.TestCase):
    def setUp(self):
        self.guard = RedlineGuard(log_dir=tempfile.mkdtemp())

    def level(self, text):
        return self.guard.scan(text)[0]

    def test_phone_variants_are_blocked(self):
        for text in ("13800001234", "+8613800001234", "+86 138 0000 1234", "8613800001234",
                     "138-0000-1234", "138 0000 1234", "１３８００００１２３４"):
            with self.subTest(text=text):
                self.assertEqual("block", self.level(f"联系{text}"))

    def test_ordinary_numbers_are_not_phones(self):
        for text in ("订单12345678901", "2026-10-23 12:30", "统计周期20261017-20261023", "金额 1,200,000 元"):
            with self.subTest(text=text):
                self.assertNotEqual("block", self.level(text))

    def test_id_card_variants_are_blocked(self):
        for text in ("110101199003071234", "11010119900307123X", "11010119900307123Ｘ", "110101 19900307 1234"):
            with self.subTest(text=text):
                self.assertEqual("block", self.level(text))


class IncidentResponseConfigTests(unittest.TestCase):
    def write(self, data):
        path = Path(tempfile.mkdtemp()) / "cfg.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def base(self):
        return json.loads((ROOT / "config" / "incident_response.json").read_text(encoding="utf-8"))

    def test_shipped_config_is_valid_and_still_a_draft(self):
        config = get_response_config()
        self.assertFalse(config.approved)
        self.assertEqual({"critical", "high", "medium", "low", "unknown"}, set(config.plans))

    def test_agent_uses_the_config(self):
        self.assertEqual(get_response_config().plans, IncidentAgent.RESPONSE_PLANS)

    def test_approved_requires_approver_and_date(self):
        data = self.base()
        data["approval"] = {"status": "approved", "approved_by": "", "approved_at": ""}
        with self.assertRaises(ValueError):
            load_response_config(self.write(data))
        data["approval"] = {"status": "approved", "approved_by": "张主管", "approved_at": "2026-11-01"}
        self.assertTrue(load_response_config(self.write(data)).approved)

    def test_missing_level_or_actions_is_rejected(self):
        data = self.base()
        del data["levels"]["high"]
        with self.assertRaises(ValueError):
            load_response_config(self.write(data))
        data = self.base()
        data["levels"]["low"]["actions"] = []
        with self.assertRaises(ValueError):
            load_response_config(self.write(data))

    def test_draft_note_text(self):
        self.assertIn("非制度条款", DRAFT_NOTE)


class WeeklyCopyTests(unittest.TestCase):
    def test_local_mode_does_not_claim_ai_decides_page_count(self):
        local = weekly_copy(False)
        joined = "".join(local.values())
        self.assertNotIn("自主策划页数", joined)
        self.assertNotIn("AI 会按素材决定页面数量", joined)
        self.assertIn("事实类别", joined)

    def test_external_mode_keeps_adaptive_wording(self):
        self.assertIn("AI 自适应页数", weekly_copy(True)["mode_label"])


class AppWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_chat_sends_role_preserving_history_to_the_model(self):
        # 历史必须以 system / user / assistant 角色发送，不再揉成单个 user 字符串。
        self.assertIn("build_messages(", self.src)
        self.assertIn("guard.call_llm_stream_messages(", self.src)
        self.assertNotIn("prompt=model_prompt,", self.src)

    def test_launch_uses_configurable_error_display_and_prints_warnings(self):
        self.assertIn("show_error=settings.show_errors,", self.src)
        self.assertNotIn("show_error=True", self.src)
        self.assertIn("security_warnings(", self.src)

    def test_incident_tables_come_from_config(self):
        self.assertIn("INCIDENT_RESPONSE = get_response_config()", self.src)
        self.assertNotIn('"sla": "立即响应', self.src)

    def test_weekly_ui_text_is_mode_aware(self):
        self.assertIn("WEEKLY_COPY['intro']", self.src)
        self.assertNotIn("自主策划页数与内容", self.src)
        self.assertNotIn("AI 会按素材决定页面数量", self.src)
