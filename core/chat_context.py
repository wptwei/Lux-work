"""对话上下文：把最近几轮有效对话带进模型，并严格控制长度。

原则：
1. 只带“用户提问 + 正常回答”完整成对的轮次；被拦截（🚫）、出错（⚠）、生成中（⏳）
   和“已进入专项”提示都不带——否则上一轮被拦截的手机号会让之后每一轮都被拦。
2. 优先用带角色的 messages（system / user / assistant），让模型分得清谁说了什么；
   `build_contextual_prompt` 保留给只接受单个字符串的旧调用方。
3. 超长消息不再只留开头：结尾常放“不要表格”“仅修改第二段”这类约束，
   必须保留尾部。裁剪后仍受整体字符预算约束。
4. 本地模型上下文只有 num_ctx（默认 4096）个 token，历史预算会随系统提示词和
   当前问题的长度自动收缩，宁可少带，也不能把系统提示词挤出上下文。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

FRAME_HEAD = "【对话上下文（仅供理解，不要复述，也不要重复回答已答过的问题）】"
FRAME_TAIL = "【当前问题】"

_FAILURE_PREFIXES = ("⏳", "🚫", "⚠")
_NOTICE_PREFIXES = ("📌 **已进入专项",)
# 回复前面由界面添加的“已自动脱敏 / 专项技能”说明，不属于模型原话。
_DECORATION = re.compile(r"\A(?:(?:🔒|📌) _[^\n]*_\n\n---\n\n)+")

# 裁剪时给结尾留的比例：约束类语句通常出现在最后一句。
_TAIL_RATIO = 0.35


def _text(content) -> str:
    return content.strip() if isinstance(content, str) else ""


def clean_reply(text: str) -> str:
    text = (text or "").replace(" ▌", "").strip()
    return _DECORATION.sub("", text).strip()


def _is_usable_reply(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(stripped) and not stripped.startswith(_FAILURE_PREFIXES + _NOTICE_PREFIXES)


def _pairs(history) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for item in history or []:
        if isinstance(item, dict):
            role, content = item.get("role"), _text(item.get("content"))
            if role == "user":
                pending = content
            elif role == "assistant":
                if pending and _is_usable_reply(content):
                    pairs.append((pending, clean_reply(content)))
                pending = None
        elif isinstance(item, (list, tuple)) and len(item) == 2:  # 旧版 [用户, 助手] 格式
            user, reply = _text(item[0]), _text(item[1])
            if user and _is_usable_reply(reply):
                pairs.append((user, clean_reply(reply)))
    return pairs


def _clip(text: str, limit: int, tail_ratio: float = _TAIL_RATIO) -> str:
    """超长时同时保留开头与结尾；结尾往往承载“不要表格”这类约束。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 8:
        return text[:limit]
    tail = max(16, min(int(limit * tail_ratio), limit - 8))
    head = max(8, limit - tail - 1)
    return text[:head].rstrip() + "…" + text[-tail:].lstrip()


def _fit_turns(pairs, max_turns: int, max_chars: int, per_message_chars: int):
    """从最近一轮往前取，返回 [(user, reply)] 与占用字数。"""
    limit = max(60, min(per_message_chars, max_chars // 2 - 6))
    kept: list[tuple[str, str]] = []
    used = 0
    for user, reply in reversed(pairs[-max_turns:] if max_turns > 0 else []):
        block = f"用户：{_clip(user, limit)}\n助手：{_clip(reply, limit)}"
        if used + len(block) + 1 > max_chars:
            break  # 只保留最近的连续几轮，不跳着带
        kept.append((user, reply))
        used += len(block) + 1
    kept.reverse()
    return kept, used, limit


def build_contextual_prompt(
    history,
    message: str,
    *,
    max_turns: int = 3,
    max_chars: int = 1800,
    per_message_chars: int = 600,
) -> tuple[str, int]:
    """返回 (提示词, 带入的轮数)。没有可带的历史时原样返回 message。"""
    if max_turns <= 0 or max_chars < 160:
        return message, 0
    kept, _, limit = _fit_turns(_pairs(history), max_turns, max_chars, per_message_chars)
    if not kept:
        return message, 0
    blocks = [f"用户：{_clip(u, limit)}\n助手：{_clip(r, limit)}" for u, r in kept]
    return f"{FRAME_HEAD}\n" + "\n".join(blocks) + f"\n\n{FRAME_TAIL}\n{message}", len(kept)


def build_messages(
    history,
    message: str,
    *,
    system: str = "",
    max_turns: int = 3,
    max_chars: int = 1800,
    per_message_chars: int = 600,
) -> tuple[list[dict[str, str]], int]:
    """返回 (messages, 带入轮数)。历史保留 user / assistant 角色，当前问题原样保留。

    当前用户原文不裁剪：它是最新指令，裁剪会直接改需求。
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    turns = 0
    if max_turns > 0 and max_chars >= 160:
        kept, _, limit = _fit_turns(_pairs(history), max_turns, max_chars, per_message_chars)
        for user, reply in kept:
            messages.append({"role": "user", "content": _clip(user, limit)})
            messages.append({"role": "assistant", "content": _clip(reply, limit)})
        turns = len(kept)
    messages.append({"role": "user", "content": message})
    return messages, turns


def history_char_budget(
    *,
    system_chars: int,
    message_chars: int,
    external: bool,
    configured_chars: int,
    num_ctx: int = 4096,
    num_predict: int = 1024,
) -> int:
    """历史最多能占多少字。本地模型要给系统提示词、当前问题和输出留足位置。"""
    if external:
        return max(0, configured_chars)
    # 中文约 1.3 字/token 的保守估计，再留 300 token 给格式开销。
    context_chars = int(max(0, num_ctx - num_predict - 300) * 1.3)
    remaining = context_chars - system_chars - message_chars - 120
    return max(0, min(configured_chars, remaining))


@dataclass(frozen=True)
class ContextPlan:
    """一次请求的预算分解，便于日志与回归对比，不含任何业务原文。"""

    num_ctx: int
    num_predict: int
    system_chars: int
    message_chars: int
    history_chars: int
    history_turns: int
    truncated: bool


def plan_context(
    *,
    system_chars: int,
    message_chars: int,
    external: bool,
    configured_chars: int,
    num_ctx: int,
    num_predict: int,
    max_turns: int,
) -> ContextPlan:
    """统一预算：固定系统要求 + 当前输入 + 最近完整轮次 + 输出预留。"""
    budget = history_char_budget(
        system_chars=system_chars,
        message_chars=message_chars,
        external=external,
        configured_chars=configured_chars,
        num_ctx=num_ctx,
        num_predict=num_predict,
    )
    return ContextPlan(
        num_ctx=num_ctx,
        num_predict=num_predict,
        system_chars=system_chars,
        message_chars=message_chars,
        history_chars=budget,
        history_turns=max_turns if budget >= 160 else 0,
        truncated=budget < configured_chars,
    )
