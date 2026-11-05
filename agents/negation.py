"""否定语境判断：区分“事件没有发生”和“缺少授权/登记/陪同”。

旧实现只要关键词前 6 个字内出现“未”“无”“没”就视为否定，导致
“有人未经授权闯入”“没有刷卡强行开门”被当成没有风险。

规则：
1. 否定词必须与关键词处于同一分句（中间没有逗号、句号等）。
2. 强否定词（无人、未发现、未发生、没有发生……）允许间隔 4 个字以内；
   弱否定词（无、没有、未）只允许间隔 2 个字以内。
3. 若间隔里出现“经、授权、许可、登记、刷卡、陪同……”，说明描述的是
   “缺少合规条件”而不是“事件未发生”，不算否定。
"""
from __future__ import annotations

CLAUSE_BREAKS = "，,。.；;！!？?：:\n\r"

# (否定词, 允许的最大间隔字数)；长词在前。
NEGATION_RULES: tuple[tuple[str, int], ...] = (
    ("没有任何", 4), ("没有发生", 4), ("没有出现", 4), ("没有造成", 4),
    ("未发现", 4), ("未发生", 4), ("未检出", 4), ("未出现", 4), ("未造成", 4),
    ("未见", 4), ("未有", 4), ("无任何", 4), ("并无", 4), ("不存在", 4),
    ("没有人", 4), ("没人", 4), ("无人", 4), ("否认", 4), ("排除", 4),
    ("没有", 2), ("无", 2), ("未", 2),
)

# 出现在否定词与关键词之间，表示“缺少合规条件”而非“事件没发生”。
COMPLIANCE_WORDS: tuple[str, ...] = (
    "经", "授权", "许可", "批准", "获", "登记", "刷卡", "陪同", "佩戴",
    "按规定", "报备", "同意", "核实", "通过", "值守", "看管", "及时", "能",
)


def is_negated(text: str, index: int) -> bool:
    """判断 text[index:] 处的关键词是否被前面的否定词否定。"""
    prefix = text[:max(0, index)]
    cut = max((prefix.rfind(ch) for ch in CLAUSE_BREAKS), default=-1)
    clause = prefix[cut + 1:]
    for marker, max_gap in NEGATION_RULES:
        position = clause.rfind(marker)
        while position != -1:
            gap = clause[position + len(marker):]
            if len(gap) <= max_gap and not any(word in gap for word in COMPLIANCE_WORDS):
                return True
            position = clause.rfind(marker, 0, position)
    return False
