"""多 Agent 基类：统一注入 guard 与 llm_fn，离线时提供确定性回退。"""
from __future__ import annotations

from typing import Callable


class BaseAgent:
    name: str = "base"
    keywords: tuple[str, ...] = ()
    system_prompt: str = ""

    def __init__(self, guard=None, llm_fn: Callable[..., str] | None = None, external: bool = False):
        self.guard = guard
        self.llm_fn = llm_fn
        self.external = external

    def match_count(self, text: str) -> int:
        return sum(1 for k in self.keywords if k and k in text)

    def matches(self, text: str) -> bool:
        return self.match_count(text) > 0

    def _llm(self, prompt: str, task_desc: str | None = None) -> str | None:
        """所有模型调用必须走 RedlineGuard；未注入 guard/llm 时返回 None 走回退。"""
        if self.guard is None or self.llm_fn is None:
            return None
        try:
            result = self.guard.call_llm(
                prompt=prompt,
                task_desc=task_desc or self.name,
                system_prompt=self.system_prompt,
                llm_fn=self.llm_fn,
                external=self.external,
            )
            if result.get("status") == "ok":
                return result.get("output", "")
        except Exception:
            # 模型不可用时退回确定性逻辑，保证闭环仍能完成
            return None
        return None

    def run(self, task: str) -> dict:
        raise NotImplementedError
