"""安保编排入口：Supervisor + Workflow + 红线守卫 + 审计 组成一个可调用整体。"""
from __future__ import annotations

from agents.report_agent import ReportAgent
from agents.risk_agent import RiskAgent
from audit.audit_logger import AuditLogger
from core.supervisor import SupervisorAgent
from core.workflow import WorkflowEngine


class SecurityOrchestrator:
    def __init__(self, guard=None, llm_fn=None, external: bool = False, audit_logger=None):
        self.guard = guard
        self.llm_fn = llm_fn
        self.external = external
        self.engine = WorkflowEngine(
            supervisor=SupervisorAgent.build(guard=guard, llm_fn=llm_fn, external=external),
            risk_agent=RiskAgent(),
            report_agent=ReportAgent(),
            audit_logger=audit_logger or AuditLogger(),
        )

    def run(self, event: str) -> dict:
        return self.engine.execute(event)
