"""现场紧急程度研判，与事件风险等级分开计算。"""
from __future__ import annotations

from agents.negation import is_negated


class UrgencyAgent:
    """根据事件当前状态判断响应紧迫性，不判断潜在后果严重度。"""

    ACTIVE = ("事发中", "正在", "持续", "尚未控制", "仍在现场", "风险扩大")
    IMMEDIATE = ("明火", "浓烟", "人员被困", "持械", "流血", "昏迷", "呼救")
    CONTROLLED = ("已控制", "已解除", "已离场", "已扑灭", "事后补录")

    @staticmethod
    def _first_affirmed(text: str, markers: tuple[str, ...]) -> str:
        """返回第一个没有被否定的标记；“确认无人员被困”不应触发“人员被困”。"""
        for marker in markers:
            start = text.find(marker)
            while start != -1:
                if not is_negated(text, start):
                    return marker
                start = text.find(marker, start + 1)
        return ""

    def score(self, event) -> dict:
        text = str(event or "")
        reasons: list[str] = []
        score = 0
        active = self._first_affirmed(text, self.ACTIVE)
        immediate = self._first_affirmed(text, self.IMMEDIATE)
        controlled = next((item for item in self.CONTROLLED if item in text), "")
        if active:
            score += 4
            reasons.append(f"active:{active}")
        if immediate:
            score += 5
            reasons.append(f"immediate:{immediate}")
        if controlled:
            score = max(0, score - 4)
            reasons.append(f"controlled:{controlled}")
        if not text.strip():
            level = "unknown"
        elif score >= 8:
            level = "emergency"
        elif score >= 4:
            level = "urgent"
        elif controlled:
            level = "routine"
        else:
            level = "unknown"
        return {
            "score": score,
            "level": level,
            "reasons": reasons,
            "requires_human_review": level == "unknown",
        }
