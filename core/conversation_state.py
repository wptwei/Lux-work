"""会话级活跃任务状态。

只保存“当前目标 + 用户明确约束 + 已确认事实 + 引用 ID + 待回答的问题”，
不保存业务原文，也不写进只记元数据的审计/任务台账。

设计要点：
- 每个登录账号一份状态，带超时；进程重启即清空，不做持久化。
- 事实区分来源：user（用户明确说出）/ tool（工具证据）/ model（模型推断）。
  只有 user 与 tool 能被后续轮次当作事实使用，model 推断永远不升级为事实。
- 切换任务时清掉旧任务特有约束，避免上一个专项继续支配新问题。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field

SRC_USER = "user"
SRC_TOOL = "tool"
SRC_MODEL = "model"
_CONFIDENT_SOURCES = {SRC_USER, SRC_TOOL}

# “200 字”“不超过 300 字”“150字左右”
_MAX_CHARS = re.compile(r"(?:不超过|不多于|最多|限制在?)?\s*(\d{1,5})\s*(?:个)?字")
_POINTS = re.compile(r"(?:分成|分为|分|列|写|总结成?)\s*(\d{1,2})\s*(?:点|条|项)")
_TABLE_OFF = re.compile(r"(?:不要|别|不用|无需|勿)(?:用|使用)?表格")
_TABLE_ON = re.compile(r"(?:用|使用|做成|整理成)?表格(?:形式|呈现)?")

# “改成 100 字”里的“改”表示修正已有约束，而不是新增。
_CONSTRAINT_ONLY = re.compile(r"改|调|换|缩短|精简|再简短|再长")


def extract_constraints(message: str) -> dict:
    """从一句话里抽取显式约束。只认用户明说的数字与格式要求。"""
    text = message or ""
    found: dict = {}
    match = _MAX_CHARS.search(text)
    if match:
        value = int(match.group(1))
        if 10 <= value <= 20000:
            found["max_chars"] = value
    if _TABLE_OFF.search(text):
        found["format"] = "paragraph"
    elif _TABLE_ON.search(text) and "table" not in text:
        found["format"] = "table"
    points = _POINTS.search(text)
    if points:
        found["points"] = int(points.group(1))
    return found


@dataclass
class TaskState:
    """一个会话里正在进行的任务。"""

    task_id: str = ""
    action: str = "chat"
    object_type: str = ""
    constraints: dict = field(default_factory=dict)
    # [(来源, 事实文本, 源消息序号)]，只允许 user / tool 来源被当作事实
    confirmed_facts: list = field(default_factory=list)
    active_artifact_id: str | None = None
    referenced_file_version_ids: list = field(default_factory=list)
    pending_question: str | None = None
    turn_index: int = 0
    updated_at: float = 0.0

    def add_fact(self, source: str, text: str) -> None:
        if source not in _CONFIDENT_SOURCES:
            return  # 模型推断不进事实区
        text = (text or "").strip()
        if not text:
            return
        if any(item[1] == text for item in self.confirmed_facts):
            return
        self.confirmed_facts.append((source, text, self.turn_index))

    def facts(self) -> list[str]:
        return [item[1] for item in self.confirmed_facts]

    def snapshot(self) -> dict:
        """供日志与提示词使用的状态摘要，不含原文以外的敏感内容。"""
        return {
            "task_id": self.task_id,
            "action": self.action,
            "object_type": self.object_type,
            "constraints": dict(self.constraints),
            "confirmed_facts": self.facts(),
            "active_artifact_id": self.active_artifact_id,
            "referenced_file_version_ids": list(self.referenced_file_version_ids),
            "pending_question": self.pending_question,
        }


class ConversationStore:
    """按账号维护会话任务状态；带超时，线程安全。"""

    def __init__(self, ttl_seconds: int = 3600, clock=time.time) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._states: dict[str, TaskState] = {}

    def get(self, username: str) -> TaskState:
        key = username or "anonymous"
        with self._lock:
            state = self._states.get(key)
            now = self._clock()
            if state is None or now - state.updated_at > self.ttl_seconds:
                state = TaskState(task_id=uuid.uuid4().hex[:12], updated_at=now)
                self._states[key] = state
            return state

    def reset(self, username: str) -> TaskState:
        key = username or "anonymous"
        with self._lock:
            state = TaskState(task_id=uuid.uuid4().hex[:12], updated_at=self._clock())
            self._states[key] = state
            return state

    def apply_route(self, username: str, route, message: str) -> TaskState:
        """按本轮路由结果更新状态槽位。

        - switch / new：开新任务，清掉旧对象与旧约束。
        - correct：更新对象或动作，保留其它有效约束。
        - continue：保留对象，用新约束覆盖同名旧值。
        """
        state = self.get(username)
        relation = getattr(route, "relation", "new")
        with self._lock:
            if relation in {"switch", "new"}:
                state.task_id = uuid.uuid4().hex[:12]
                state.action = getattr(route, "action", "chat")
                state.object_type = getattr(route, "object_type", "")
                state.constraints = {}
                state.confirmed_facts = []
                state.active_artifact_id = None
                state.referenced_file_version_ids = []
                state.pending_question = None
            else:
                action = getattr(route, "action", "")
                if action and action != "chat":
                    state.action = action
                object_type = getattr(route, "object_type", "")
                if object_type:
                    state.object_type = object_type
                if relation == "correct":
                    # 纠正时用户说的是新对象类型，旧约束仍然有效
                    pass
            state.constraints.update(extract_constraints(message))
            state.turn_index += 1
            state.updated_at = self._clock()
            return state


def state_brief(state: TaskState | None) -> str:
    """把状态压成一行提示词片段；没有有效状态时返回空串。"""
    if state is None:
        return ""
    parts = []
    if state.action and state.action != "chat":
        parts.append(f"当前动作：{state.action}")
    if state.object_type:
        parts.append(f"当前对象：{state.object_type}")
    if state.constraints:
        rendered = "，".join(f"{k}={v}" for k, v in sorted(state.constraints.items()))
        parts.append(f"已确认约束：{rendered}")
    facts = state.facts()
    if facts:
        parts.append("已确认事实：" + "；".join(facts[:4]))
    if state.pending_question:
        parts.append(f"待回答：{state.pending_question}")
    if not parts:
        return ""
    return "【当前任务状态（用户此前确认的信息，可直接沿用）】\n" + "\n".join(parts)
