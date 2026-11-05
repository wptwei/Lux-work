"""风险研判 Agent：可解释的规则评分、语境修正与人工复核提示。"""
from __future__ import annotations

import re

from agents.negation import is_negated


class RiskAgent:
    KEYWORDS: dict[str, tuple[str, ...]] = {
        "critical": ("闯入", "持械", "明火", "火灾", "爆炸", "伤亡", "斗殴", "冲突", "劫持"),
        "high": (
            "破坏", "盗窃", "偷盗", "尾随", "翻越", "威胁", "打斗",
            "消防报警", "强行开门", "非法开门", "强开",
        ),
        "medium": (
            "隐患", "未锁", "可疑", "滞留", "故障", "占道", "门禁异常",
            "离线", "黑屏", "遮挡", "失灵", "报警失效",
        ),
    }
    WEIGHTS: dict[str, int] = {"critical": 10, "high": 5, "medium": 2}
    CONTEXT_WEIGHTS: dict[str, tuple[int, tuple[str, ...]]] = {
        "事发中": (2, ("事发中", "正在", "持续", "尚未控制")),
        "多人涉及": (2, ("多人", "聚集", "围堵")),
        "人员受伤": (5, ("受伤", "流血", "昏迷")),
        "已控制": (-2, ("已控制", "已解除", "已离场", "已扑灭")),
    }
    @staticmethod
    def _negated(text: str, index: int) -> bool:
        """判断关键词是否被否定（“无人闯入/未见异常”）。

        “未经授权闯入”“没有刷卡强行开门”描述的是缺少合规条件，不是事件未发生，
        不算否定；细则见 agents/negation.py。
        """
        return is_negated(text, index)

    def score(self, event) -> dict:
        text = str(event or "")
        reasons: list[str] = []
        factors: list[dict] = []
        negated: list[str] = []
        score = 0
        seen: set[str] = set()

        for level in ("critical", "high", "medium"):
            for keyword in self.KEYWORDS[level]:
                if not keyword or keyword in seen:
                    continue
                hits = [m.start() for m in re.finditer(re.escape(keyword), text)]
                if not hits:
                    continue
                if all(self._negated(text, i) for i in hits):
                    negated.append(keyword)
                    continue
                score += self.WEIGHTS[level]
                reasons.append(f"{level}:{keyword}")
                factors.append({"type": "keyword", "label": keyword, "weight": self.WEIGHTS[level]})
                seen.add(keyword)

        for label, (weight, markers) in self.CONTEXT_WEIGHTS.items():
            matched = next((marker for marker in markers if marker in text), "")
            if not matched:
                continue
            # “未受伤/无人受伤”等否定表达不作为升级依据。
            index = text.find(matched)
            if weight > 0 and self._negated(text, index):
                negated.append(matched)
                continue
            score = max(0, score + weight)
            reasons.append(f"context:{label}")
            factors.append({"type": "context", "label": label, "evidence": matched, "weight": weight})

        if not text.strip():
            level = "unknown"
        elif score >= 10:
            level = "critical"
        elif score >= 5:
            level = "high"
        elif score >= 2:
            level = "medium"
        else:
            level = "low"

        positive_evidence = sum(1 for factor in factors if factor.get("weight", 0) > 0)
        confidence = min(0.95, 0.38 + positive_evidence * 0.12)
        if level == "unknown":
            confidence = 0.2
        elif level == "low" and not factors:
            confidence = 0.45

        return {
            "score": score,
            "level": level,
            "event": event,
            "reasons": reasons,
            "factors": factors,
            "negated_terms": sorted(set(negated)),
            "confidence": round(confidence, 2),
            "requires_human_review": level in {"critical", "unknown"} or confidence < 0.5,
        }
