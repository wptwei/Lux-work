from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents.report_agent import ReportAgent
from agents.risk_agent import RiskAgent
from agents.incident_agent import IncidentAgent
from audit.audit_logger import AuditLogger
from core.orchestrator import SecurityOrchestrator
from core.redline_guard import RedlineGuard
from core.supervisor import SupervisorAgent
from core.workflow import WorkflowEngine


class RiskAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = RiskAgent()

    def test_critical_event(self):
        result = self.agent.score("有人闯入H区并与保安发生冲突")
        self.assertEqual("critical", result["level"])
        self.assertGreaterEqual(result["score"], 10)

    def test_low_event(self):
        result = self.agent.score("巡检正常，无异常")
        self.assertEqual("low", result["level"])

    def test_empty_event_is_unknown(self):
        result = self.agent.score("")
        self.assertEqual("unknown", result["level"])

    def test_negation_does_not_escalate(self):
        self.assertEqual("low", self.agent.score("确认无人闯入，情况正常")["level"])
        self.assertEqual("low", self.agent.score("未见异常，巡检正常")["level"])

    def test_negated_but_real_fault_still_counts(self):
        self.assertEqual("medium", self.agent.score("门未锁")["level"])

    def test_context_is_explained_and_negated_injury_is_ignored(self):
        result = self.agent.score("现场状态：事发中，发现可疑人员，无人受伤")
        self.assertEqual("medium", result["level"])
        self.assertIn("context:事发中", result["reasons"])
        self.assertIn("受伤", result["negated_terms"])
        self.assertGreater(result["confidence"], 0.5)


class IncidentAgentTests(unittest.TestCase):
    def test_generates_structured_plan_and_information_gaps(self):
        result = IncidentAgent().run(
            "事件类别：人员闯入\n发生区域：H区\n现场状态：事发中\n"
            "发生时间：待补充\n事件描述：东门发现人员闯入并持续滞留"
        )
        self.assertEqual("incident", result["agent"])
        self.assertIn("发生时间", result["information_gaps"])
        self.assertTrue(result["response_plan"]["actions"])
        self.assertTrue(result["requires_human_confirmation"])

    def test_door_access_event_uses_dedicated_playbook(self):
        result = IncidentAgent().run(
            "事件类别：门禁异常\n发生区域：H区\n现场状态：已控制\n"
            "发生时间：10:20\n事件描述：东门门磁异常，已安排人员现场看守并通知维修"
        )
        self.assertEqual("门禁异常", result["event_profile"])
        self.assertIn("门禁事件记录", result["evidence_to_preserve"])
        self.assertTrue(any("门体" in item for item in result["verification_questions"]))

    def test_monitoring_event_identifies_blind_spot_escalation(self):
        result = IncidentAgent().run(
            "事件类别：监控异常\n发生区域：NPI区\n现场状态：事发中\n"
            "发生时间：11:05\n事件描述：两处摄像头离线黑屏，实时画面和录像均无法查看"
        )
        self.assertEqual("监控异常", result["event_profile"])
        self.assertTrue(any("监控盲区" in item for item in result["escalation_triggers"]))
        self.assertIn(result["risk"]["level"], {"medium", "high", "critical"})


class SupervisorTests(unittest.TestCase):
    def test_routes_incident(self):
        result = SupervisorAgent.build().dispatch("H区东门发生闯入事件")
        self.assertEqual("incident", result["agent"])
        self.assertGreater(result["route"]["confidence"], 0.5)

    def test_routes_patrol(self):
        result = SupervisorAgent.build().dispatch("请整理今天的巡检记录")
        self.assertEqual("patrol", result["agent"])

    def test_patrol_has_priority_over_incident(self):
        result = SupervisorAgent.build().dispatch("巡检发现门未锁异常")
        self.assertEqual("patrol", result["agent"])

    def test_unknown_task(self):
        result = SupervisorAgent.build().dispatch("你好")
        self.assertEqual("general", result["agent"])
        self.assertEqual("unknown", result["status"])


class AuditLoggerTests(unittest.TestCase):
    def test_log_writes_metadata_not_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = AuditLogger(log_dir=Path(tmp))
            logger.log(
                "workflow.risk",
                "有人闯入H区",
                stage="risk",
                level="critical",
                score=10,
            )
            content = next(Path(tmp).glob("audit_*.jsonl")).read_text("utf-8")
            self.assertNotIn("有人闯入H区", content)
            entry = json.loads(content)
            self.assertIn("detail_sha256_12", entry)
            self.assertEqual("critical", entry["level"])


class ReportAgentTests(unittest.TestCase):
    def test_generate_markdown(self):
        report = ReportAgent().generate({
            "title": "测试报告",
            "event": "有人闯入H区",
            "risk": {"level": "critical", "score": 10},
            "agent": "incident",
            "action": "立即上报",
            "status": "completed",
        })
        self.assertIn("# 测试报告", report)
        self.assertIn("**风险等级**：critical", report)
        self.assertIn("**处置动作**：立即上报", report)


class WorkflowEngineTests(unittest.TestCase):
    def test_execute_closes_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = WorkflowEngine(
                supervisor=SupervisorAgent.build(),
                risk_agent=RiskAgent(),
                report_agent=ReportAgent(),
                audit_logger=AuditLogger(log_dir=Path(tmp)),
            )
            result = engine.execute("有人闯入H区并与保安发生冲突")

            self.assertEqual("awaiting_confirmation", result["status"])
            self.assertTrue(result["requires_human_confirmation"])
            self.assertEqual(["identify", "risk", "action", "archive", "human_confirm"], list(result["stages"]))
            self.assertEqual("critical", result["risk"]["level"])
            self.assertIn("**风险等级**：critical", result["report"])
            self.assertIn("待值班主管确认", result["report"])
            self.assertEqual(5, len(result["trace"]))

            lines = [
                json.loads(x)
                for x in next(Path(tmp).glob("audit_*.jsonl")).read_text("utf-8").splitlines()
            ]
            self.assertEqual(5, len(lines))
            self.assertEqual(
                ["identify", "risk", "action", "archive", "human_confirm"],
                [line["stage"] for line in lines],
            )


class OrchestratorTests(unittest.TestCase):
    def test_run_with_guard_and_fake_llm(self):
        def fake(prompt, system=""):
            return "建议立即上报并封锁现场"

        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = SecurityOrchestrator(
                guard=RedlineGuard(log_dir=Path(tmp)),
                llm_fn=fake,
                audit_logger=AuditLogger(log_dir=Path(tmp)),
            )
            result = orchestrator.run("有人闯入H区")

            self.assertEqual("awaiting_confirmation", result["status"])
            self.assertIn("立即上报并封锁现场", result["dispatched"]["suggestion"])
            self.assertIn("立即上报并封锁现场", result["report"])

            audit_content = next(Path(tmp).glob("audit_*.jsonl")).read_text("utf-8")
            self.assertNotIn("有人闯入H区", audit_content)

    def test_run_falls_back_when_model_fails(self):
        def broken(prompt, system=""):
            raise RuntimeError("model down")

        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = SecurityOrchestrator(
                guard=RedlineGuard(log_dir=Path(tmp)),
                llm_fn=broken,
                audit_logger=AuditLogger(log_dir=Path(tmp)),
            )
            result = orchestrator.run("有人闯入H区")

            self.assertEqual("awaiting_confirmation", result["status"])
            self.assertIn("值班主管确认", result["dispatched"]["suggestion"])


if __name__ == "__main__":
    unittest.main()
