"""Supervisor Agent：按领域关键词分类并分发到具体 Agent。"""
from __future__ import annotations

from agents.incident_agent import IncidentAgent
from agents.patrol_agent import PatrolAgent


class SupervisorAgent:
    # 巡检优先于事件：既含“巡检”又含“异常”的，通常仍是巡检记录整理任务
    PRIORITY = ("patrol", "incident")

    def __init__(self, agents=None, default=None, priority=None):
        self.agents = agents or {}
        self.default = default
        self.priority = list(priority) if priority is not None else list(self.PRIORITY)

    @classmethod
    def build(cls, guard=None, llm_fn=None, external: bool = False):
        return cls(
            agents={
                "incident": IncidentAgent(guard=guard, llm_fn=llm_fn, external=external),
                "patrol": PatrolAgent(guard=guard, llm_fn=llm_fn, external=external),
            }
        )

    def classify(self, task: str):
        candidates = []
        for name, agent in self.agents.items():
            count = agent.match_count(str(task or ""))
            if count:
                candidates.append((name, agent, count))
        for name in self.priority:
            match = next((item for item in candidates if item[0] == name), None)
            if match:
                return match
        if candidates:
            return max(candidates, key=lambda item: item[2])
        return "general", self.default, 0

    def dispatch(self, task: str) -> dict:
        name, agent, match_count = self.classify(task)
        route = {
            "target": name,
            "method": "keyword_rules",
            "matched_keyword_count": match_count,
            "confidence": round(min(0.95, 0.45 + match_count * 0.15), 2) if match_count else 0.2,
        }
        if agent is None:
            return {
                "agent": "general",
                "status": "unknown",
                "message": "需要进一步分类",
                "route": route,
                "information_gaps": ["任务类型或业务目标"],
                "requires_human_confirmation": True,
                "task": task,
            }
        result = agent.run(task)
        result.setdefault("agent", name)
        result.setdefault("status", "ok")
        result["route"] = route
        return result
