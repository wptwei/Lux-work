"""Workflow：识别、研判、生成行动方案，并停在人工确认节点。"""
from __future__ import annotations

from agents.report_agent import ReportAgent
from agents.risk_agent import RiskAgent
from audit.audit_logger import AuditLogger


ACTION_BY_LEVEL = {
    "critical": {"code": "escalate", "label": "立即上报值班主管并按应急预案处置"},
    "high": {"code": "handle", "label": "现场处置并记录"},
    "medium": {"code": "watch", "label": "重点跟进"},
    "low": {"code": "observe", "label": "正常记录"},
    "unknown": {"code": "review", "label": "人工复核"},
}


class WorkflowEngine:
    STAGES = ("identify", "risk", "action", "archive", "human_confirm")

    def __init__(self, supervisor, risk_agent=None, report_agent=None, audit_logger=None):
        self.supervisor = supervisor
        self.risk_agent = risk_agent
        self.report_agent = report_agent
        self.audit = audit_logger or AuditLogger()

    def execute(self, event: str) -> dict:
        result = {"event": event, "stages": self.STAGES, "trace": []}

        # 1) identify：分类并分发
        dispatched = self.supervisor.dispatch(event)
        result["agent"] = dispatched.get("agent", "general")
        result["dispatched"] = dispatched
        result["trace"].append({"stage": "identify", "agent": result["agent"]})
        self.audit.log("workflow.identify", event, stage="identify", agent=result["agent"])

        # 2) risk：风险评分
        risk = self._risk(event, dispatched)
        result["risk"] = risk
        result["trace"].append({"stage": "risk", "level": risk.get("level"), "score": risk.get("score")})
        self.audit.log("workflow.risk", event, stage="risk", level=risk.get("level"), score=risk.get("score"))

        # 制度匹配已在事件 Agent 内完成；这里只透传可审计的结构化结果，不保存事件原文。
        result["urgency"] = dispatched.get("urgency") or {"level": "unknown", "score": 0, "reasons": []}
        result["policy_match"] = dispatched.get("policy_match") or {}

        # 3) action：按风险选择动作
        action = self._action(dispatched, risk)
        result["action"] = action
        result["trace"].append({"stage": "action", "code": action["code"]})
        self.audit.log("workflow.action", event, stage="action", action_code=action["code"])

        # 4) archive：这里只生成记录草案，不代表现场事件已经结案。
        report = self._archive(event, dispatched, risk, action)
        result["report"] = report
        result["trace"].append({"stage": "archive", "status": "draft_ready"})
        self.audit.log("workflow.archive", event, stage="archive", status="draft_ready")

        # 5) human_confirm：交由现场负责人或值班主管确认执行、补证与结案。
        result["requires_human_confirmation"] = True
        result["trace"].append({"stage": "human_confirm", "status": "waiting"})
        self.audit.log("workflow.human_confirm", event, stage="human_confirm", status="waiting")
        result["status"] = "awaiting_confirmation"
        return result

    def _risk(self, event: str, dispatched: dict) -> dict:
        if self.risk_agent is None:
            self.risk_agent = RiskAgent()
        return dispatched.get("risk") or self.risk_agent.score(event)

    def _action(self, dispatched: dict, risk: dict) -> dict:
        spec = ACTION_BY_LEVEL.get(risk.get("level"), ACTION_BY_LEVEL["unknown"])
        suggestion = dispatched.get("suggestion") or dispatched.get("output") or ""
        response_plan = dispatched.get("response_plan") or {}
        return {
            "code": spec["code"],
            "label": spec["label"],
            "suggestion": suggestion,
            "sla": response_plan.get("sla", "待人工确认"),
            "owner": response_plan.get("owner", "值班主管"),
            "checklist": list(response_plan.get("actions") or []),
        }

    def _archive(self, event: str, dispatched: dict, risk: dict, action: dict) -> str:
        if self.report_agent is None:
            self.report_agent = ReportAgent()
        return self.report_agent.generate({
            "title": "安保处置报告",
            "event": event,
            "risk": risk,
            "agent": dispatched.get("agent"),
            "action": action["label"],
            "detail": action["suggestion"],
            "status": "待值班主管确认",
        })
