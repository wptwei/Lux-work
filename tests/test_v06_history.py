from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as webapp
from core.auth import UserStore
from core.db import PromptDB
from core.redline_guard import RedlineGuard
from core.task_queue import TaskQueue
from core.incident_store import IncidentStore
from core.incident_intake import IncidentIntake


class _FakeRequest:
    def __init__(self, username: str):
        self.username = username


def _req(username: str) -> _FakeRequest:
    return _FakeRequest(username)


class V06TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_queue = webapp.task_queue
        self.old_db = webapp.db
        self.old_guard = webapp.guard
        self.old_stream = webapp.configured_llm_stream
        self.old_router = webapp._router_model
        self.old_user_store = webapp.user_store
        self.old_incident_store = webapp.incident_store
        self.old_incident_intake = webapp.incident_intake
        webapp.task_queue = TaskQueue(root / "tasks.db")
        webapp.db = PromptDB(root / "prompts.db")
        webapp.guard = RedlineGuard(log_dir=root / "logs")
        webapp.user_store = UserStore(root / "users.json")
        webapp.incident_store = IncidentStore(root / "incidents.db")
        webapp.incident_intake = IncidentIntake(root / "incidents.db")
        webapp.user_store.add_user("zhangsan", "Passw0rd123!", display_name="张三", enforce_password_policy=False)
        webapp.user_store.add_user("lisi", "Passw0rd123!", display_name="李四", enforce_password_policy=False)
        webapp.user_store.add_user(
            "boss", "Passw0rd123!", display_name="主管", role="supervisor", enforce_password_policy=False
        )

        # 意图路由固定为闲聊，避免测试连本地模型而结果不稳定。
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
        webapp.user_store = self.old_user_store
        webapp.incident_store = self.old_incident_store
        webapp.incident_intake = self.old_incident_intake
        webapp.configured_llm_stream = self.old_stream
        webapp._router_model = self.old_router
        self.tmp.cleanup()

    def _seed(self, username, task_type="chat", title="测试任务", status="COMPLETED", category="通用"):
        task_id = webapp.task_queue.create(
            task_type,
            {"prompt_sha256_12": "abc123456789"},
            title=title,
            category=category,
            skill=title,
            input_chars=10,
            created_by=username,
        )
        webapp.task_queue.update(task_id, status)
        return task_id

    def test_list_for_user_only_returns_own_tasks(self):
        self._seed("zhangsan", title="张三的任务")
        self._seed("lisi", title="李四的任务")

        rows = webapp.task_queue.list_for_user("zhangsan")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "张三的任务")
        self.assertNotIn("李四的任务", [r["title"] for r in rows])

    def test_workspace_overview_is_role_aware_and_does_not_leak_other_users(self):
        self._seed("zhangsan", title="张三最近任务")
        self._seed("lisi", title="李四私有任务")

        overview = webapp.render_workspace_overview(_req("zhangsan"))

        self.assertIn("普通用户", overview)
        self.assertIn("张三最近任务", overview)
        self.assertNotIn("李四私有任务", overview)

    def test_current_task_context_uses_real_task_metadata(self):
        task_id = self._seed("zhangsan", title="动态任务上下文", category="巡逻岗")

        context = webapp.render_current_task_context(task_id)

        self.assertIn("动态任务上下文", context)
        self.assertIn("巡逻岗", context)
        self.assertIn("已完成", context)

    def test_management_hub_hides_privileged_modules_from_operator(self):
        operator_hub = webapp.render_management_hub(_req("zhangsan"))
        supervisor_hub = webapp.render_management_hub(_req("boss"))

        self.assertNotIn("效能看板", operator_hub)
        self.assertNotIn("账号权限", operator_hub)
        self.assertIn("任务历史", operator_hub)
        self.assertIn("效能看板", supervisor_hub)

    def test_dashboard_requires_supervisor_permission(self):
        denied = webapp.render_dashboard_authorized(7, _req("zhangsan"))
        allowed = webapp.render_dashboard_authorized(7, _req("boss"))

        self.assertIn("无权查看团队效能", denied)
        self.assertIn("dashboard-wrap", allowed)

    def test_count_and_filter(self):
        self._seed("zhangsan", task_type="chat", title="对话任务")
        self._seed("zhangsan", task_type="incident", title="事件任务")
        self._seed("zhangsan", task_type="chat", title="第二个对话")

        self.assertEqual(webapp.task_queue.count_for_user("zhangsan"), 3)
        self.assertEqual(webapp.task_queue.count_for_user("zhangsan", task_type="chat"), 2)
        self.assertEqual(webapp.task_queue.count_for_user("zhangsan", task_type="incident"), 1)

    def test_render_history_list_category_all_returns_everything(self):
        self._seed("zhangsan", task_type="chat", title="对话任务", category="通用")
        self._seed("zhangsan", task_type="incident", title="事件任务", category="事件处置")

        table, _, info, page = webapp.render_history_list(_req("zhangsan"), "", "", "", 0)
        self.assertIn("对话任务", table)
        self.assertIn("事件任务", table)

        filtered, _, _, _ = webapp.render_history_list(_req("zhangsan"), "", "", "事件处置", 0)
        self.assertIn("事件任务", filtered)
        self.assertNotIn("对话任务", filtered)

    def test_detail_rejects_other_users_task(self):
        task_id = self._seed("zhangsan", title="张三的任务")

        denied, *_ = webapp.render_history_detail(task_id, _req("lisi"))
        self.assertIn("无权", denied)

        allowed, *_ = webapp.render_history_detail(task_id, _req("zhangsan"))
        self.assertIn("张三的任务", allowed)

    def test_chat_task_does_not_store_plaintext(self):
        prompt = "这是不应写入任务台账的业务测试原文"
        updates = list(webapp.respond(prompt, [], "chat", request=_req("zhangsan")))
        task_id = updates[-1][-1]
        task = webapp.task_queue.get(task_id)
        self.assertEqual("COMPLETED", task["status"])
        self.assertNotIn(prompt, task["payload"])
        self.assertNotIn(prompt, task["result"])
        self.assertEqual("zhangsan", task["created_by"])

    def test_history_feedback_roundtrip(self):
        task_id = self._seed("zhangsan", title="待评价任务")
        tip, _ = webapp.history_feedback(task_id, "可用", 3, "无需修改", request=_req("zhangsan"))
        self.assertIn("已记录", tip)
        task = webapp.task_queue.get(task_id)
        self.assertEqual("可用", task["feedback"])
        self.assertEqual(3, task["actual_minutes"])

    def test_history_feedback_rejects_other_users_task(self):
        task_id = self._seed("zhangsan", title="张三的任务")
        tip, _ = webapp.history_feedback(task_id, "可用", 3, "", request=_req("lisi"))
        self.assertIn("无权", tip)
        self.assertEqual("", webapp.task_queue.get(task_id)["feedback"])

    def test_render_header_status_reflects_model_health(self):
        old_health = webapp.system_health
        try:
            webapp.system_health = lambda: {
                "model": {"ok": False, "label": "Ollama / qwen2.5:3b", "detail": "连接失败", "latency_ms": 0}
            }
            offline_html = webapp.render_header_status(_req("zhangsan"))
            self.assertIn("OFFLINE", offline_html)
            self.assertIn("status-dot offline", offline_html)

            webapp.system_health = lambda: {
                "model": {"ok": True, "label": "Ollama / qwen2.5:3b", "detail": "在线", "latency_ms": 5}
            }
            online_html = webapp.render_header_status(_req("zhangsan"))
            self.assertIn("ONLINE", online_html)
            self.assertNotIn("offline", online_html.split("status-pill")[1].split("header-meta")[0])
        finally:
            webapp.system_health = old_health

    def test_clear_incident_form_resets_fields_and_placeholder(self):
        values = webapp.clear_incident_form()
        self.assertEqual(7, len(values))
        event_type, region, status, event_time, event_desc, result, incident_id = values
        self.assertEqual("人员闯入", event_type["value"])
        self.assertEqual("事发中", status["value"])
        self.assertEqual("", event_time["value"])
        self.assertEqual("", event_desc["value"])
        self.assertIn("等待事件接报", result)
        self.assertEqual("", incident_id["value"])

    def test_incident_advice_waits_for_human_confirmation(self):
        fake_result = {
            "agent": "incident",
            "risk": {"level": "critical", "score": 12, "confidence": 0.86, "reasons": ["critical:闯入"]},
            "dispatched": {
                "suggestion": "立即核实并上报值班主管。",
                "response_plan": {
                    "sla": "立即响应 · 0–3 分钟",
                    "owner": "现场负责人 + 值班主管",
                    "actions": ["隔离现场", "上报主管", "保全证据"],
                },
            },
            "action": {"label": "立即上报值班主管并按应急预案处置"},
            "trace": [
                {"stage": "identify", "agent": "incident"},
                {"stage": "risk", "level": "critical", "score": 12},
                {"stage": "action", "code": "escalate"},
                {"stage": "archive", "status": "draft_ready"},
                {"stage": "human_confirm", "status": "waiting"},
            ],
        }
        with patch.object(webapp.orchestrator, "run", return_value=fake_result):
            output, incident_id, cases_html = webapp.run_agent_task(
                "东门发现未经授权人员闯入并持续滞留，现场正在核查。",
                "人员闯入",
                "H区",
                "事发中",
                "2026-10-20 10:30",
                request=_req("zhangsan"),
            )
        self.assertIn("人工确认", output)
        self.assertTrue(incident_id.startswith("INC-"))
        self.assertIn(incident_id, cases_html)
        task = webapp.task_queue.list_for_user("zhangsan", task_type="incident", limit=1)[0]
        self.assertEqual("AWAITING_CONFIRMATION", task["status"])

        denied, _ = webapp.transition_incident_case(
            incident_id, "确认处置", "当班班长", "", request=_req("zhangsan")
        )
        self.assertIn("无权", denied)

        accepted, _ = webapp.transition_incident_case(
            incident_id, "确认处置", "当班班长", "", request=_req("boss")
        )
        self.assertIn("处置中", accepted)
        self.assertEqual("RUNNING", webapp.task_queue.get(task["id"])["status"])

        closed, _ = webapp.transition_incident_case(
            incident_id, "确认结案", "当班班长", "现场已恢复并完成核查", request=_req("boss")
        )
        self.assertIn("已结案", closed)
        self.assertEqual("COMPLETED", webapp.task_queue.get(task["id"])["status"])


if __name__ == "__main__":
    unittest.main()
