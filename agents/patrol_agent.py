"""巡检处置 Agent：巡检记录规范化 + 风险评分。"""
from __future__ import annotations

from agents.base import BaseAgent
from agents.risk_agent import RiskAgent


class PatrolAgent(BaseAgent):
    name = "patrol"
    keywords = ("巡逻", "巡检")
    system_prompt = (
        "你是安保巡检助手。把用户给的巡检内容整理成规范条目：时间、区域、巡检情况、处置结果。"
        "不遗漏异常，不编造内容。"
    )

    def __init__(self, guard=None, llm_fn=None, external: bool = False):
        super().__init__(guard=guard, llm_fn=llm_fn, external=external)
        self.risk_agent = RiskAgent()

    def run(self, task: str) -> dict:
        risk = self.risk_agent.score(task)
        output = self._llm(task, task_desc="巡检记录整理")
        missing = [label for label, markers in {
            "巡检时间": ("时间", "时", "日"),
            "巡检区域": ("区域", "区", "门", "岗"),
            "处置结果": ("已处理", "已恢复", "已上报", "待处理", "无异常"),
        }.items() if not any(marker in str(task or "") for marker in markers)]
        return {
            "agent": self.name,
            "status": "ok",
            "risk": risk,
            "output": output or task,
            "information_gaps": missing,
            "requires_human_confirmation": bool(risk.get("requires_human_review")) or risk["level"] != "low",
            "decision_status": "待确认" if risk["level"] != "low" else "记录草案",
            "task": task,
        }
