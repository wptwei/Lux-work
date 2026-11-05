"""Small, deterministic incident follow-ups; no model guesses or account-wide targets."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class FactQuestion:
    key: str
    prompt: str


QUESTIONS = {
    "entry": FactQuestion("entry", "人员是在进入管制区前被拦住，还是进入后被发现？"),
    "escort": FactQuestion("escort", "当时是否有对接部门人员陪同？"),
    "history": FactQuestion("history", "是否已确认这是首次发生，还是有同类历史记录？不清楚可以跳过。"),
}


def choose_question(text: str, result: dict | None = None) -> FactQuestion | None:
    """Ask about a material ambiguity, not a mandatory time/place questionnaire."""
    entry_known = re.search(
        r"未进入|没有进入|没进去|未进去|已进入|已经进入|进入后|进入前|在.+区域内|"
        r"进入(?:了)?(?:管制区|受限区|受控区)|进入情况：", text,
    )
    if (re.search(r"供应商|人员|访客|外协|承包商", text)
            and re.search(r"拦住|拦截|劝阻|阻拦|制止", text) and not entry_known):
        return QUESTIONS["entry"]
    matches = (result or {}).get("matches") or []
    missing = (matches[0].get("missing_conditions") or []) if matches else []
    if "是否由对接部门人员陪同" in missing and not re.search(r"陪同|带领|随同", text):
        return QUESTIONS["escort"]
    if "是否为首次发生或已有历史记录" in missing and not re.search(r"首次|第一次|再次|多次|历史记录", text):
        return QUESTIONS["history"]
    return None


def question_answer(question: FactQuestion, text: str) -> str | None:
    """Only consume explicit answers; an unrelated question must remain ordinary chat."""
    value = re.sub(r"[\s，,。.!！：:]", "", text or "")
    if re.fullmatch(r"(?:先|暂时)?(?:跳过|不补充|不知道|不清楚|暂不清楚|以后再说)", value):
        return ""  # skip; never invent facts from an unknown answer
    answers = {
        "entry": (
            (r"(?:人员)?(?:没进去|没有进去|未进去|未进入|没有进入|还没进去|进入前被拦住|在外面被拦住)(?:管制区|受限区域)?(?:就被拦住了|被拦住了|了)?", "进入情况：人员未进入受限区域。"),
            (r"(?:人员)?(?:已经进去|已进入|已经进入|进去了|进入后被发现)(?:管制区|受限区域)?(?:了)?", "进入情况：人员已进入受限区域。"),
        ),
        "escort": (
            (r"(?:当时)?(?:没有|无人|没人|没有人)(?:陪同|带领)", "陪同情况：当时没有对接部门人员陪同。"),
            (r"(?:当时)?有(?:对接部门人员|ICT人员|人)?陪同", "陪同情况：当时有对接部门人员陪同。"),
        ),
        "history": (
            (r"(?:已确认|确认|是)?(?:首次|第一次)(?:发生)?", "历史情况：报告人确认是首次发生。"),
            (r"(?:已确认|确认)?(?:有同类历史记录|有历史记录|再次发生|多次发生)", "历史情况：报告人确认有同类历史记录。"),
        ),
    }
    for pattern, fact in answers.get(question.key, ()):
        if re.fullmatch(pattern, value, re.IGNORECASE):
            return fact
    return None


@dataclass(frozen=True)
class CurrentIncident:
    incident_id: str
    version: int
    question: FactQuestion | None
    asked: bool
    expires: float


class IncidentConversationStore:
    """Per authenticated user AND browser session, expiring without storing raw text."""
    def __init__(self, ttl_seconds: int = 900):
        self.ttl = ttl_seconds
        self._lock = threading.RLock()
        self._items: dict[tuple[str, str], CurrentIncident] = {}

    def get(self, actor: str, session: str) -> CurrentIncident | None:
        if not actor or not session:
            return None
        with self._lock:
            key = (actor, session)
            item = self._items.get(key)
            if item and item.expires <= time.monotonic():
                self._items.pop(key, None)
                return None
            return item

    def bind(self, actor: str, session: str, incident_id: str, version: int,
             question: FactQuestion | None = None, *, answered: bool = False):
        if not actor or not session:
            return
        with self._lock:
            now = time.monotonic()
            self._items = {key: item for key, item in self._items.items() if item.expires > now}
            old = self.get(actor, session)
            same = old is not None and old.incident_id == incident_id
            asked = answered or (same and old.asked)
            pending = None if answered else (old.question if same and old.asked else question)
            self._items[(actor, session)] = CurrentIncident(
                incident_id, version, pending, bool(asked or pending), now + self.ttl,
            )

    def offer(self, actor: str, session: str, incident_id: str, version: int, question):
        with self._lock:
            item = self.get(actor, session)
            if item and item.incident_id == incident_id and item.version == version and not item.asked:
                self._items[(actor, session)] = replace(item, question=question, asked=bool(question))

    def skip(self, actor: str, session: str):
        with self._lock:
            item = self.get(actor, session)
            if item:
                self._items[(actor, session)] = replace(item, question=None, asked=True)

    def clear(self, actor: str, session: str):
        with self._lock:
            self._items.pop((actor, session), None)


def relative_change(text: str) -> dict | None:
    """Conservative commands about the current record, including spoken punctuation."""
    value = (text or "").strip()
    supplement = re.fullmatch(
        r"(?:请|帮我)?(?:再补充(?:一句|一下)?|补充(?:刚才|刚刚|这)(?:那|这)?(?:一)?条(?:记录)?)[，,：:\s]*(.*)", value,
    )
    if supplement:
        return {"kind": "supplement", "value": supplement.group(1).strip()}
    field = re.fullmatch(
        r"(?:请|帮我)?(?:把)?(?:刚才|刚刚|这)(?:(?:那|这)?(?:一)?条(?:记录)?)?(?:的)?"
        r"(地点|位置|时间)(?:说错了)?[，,\s]*(?:改成|改为|更正为)[：:\s]*(.+?)[。！!]?", value,
    )
    if field:
        return {"kind": "field", "field": "时间" if field.group(1) == "时间" else "地点",
                "value": field.group(2).strip()}
    full = re.fullmatch(r"(?:把)?(?:刚才|刚刚)(?:那)?条(?:记录)?全文(?:改为|改成)[：:\s]*(.*)", value)
    if full:
        return {"kind": "correct", "value": full.group(1).strip()}
    if re.match(r"(?:请|帮我)?(?:把)?(?:更正|修改|补充)?(?:刚才|刚刚)(?:那)?条", value):
        return {"kind": "clarify", "value": ""}
    return None


def replace_field(text: str, field: str, value: str) -> str:
    """Modify a single unambiguous fact, preserving the rest of the report."""
    value = value.strip(" \n，,。")
    if not value or len(value) > 80 or re.search(r"[\n；;。！？?]", value):
        raise ValueError("请只说需要更正的时间或地点；也可以打开记录编辑完整正文。")
    label = "发生" + field
    explicit = re.findall(label + r"[：:]\s*([^；;。\n]+)", text)
    if explicit:
        old_values = set(item.strip() for item in explicit)
    elif field == "地点":
        old_values = set(re.findall(r"[东南西北]门|[A-Z]\d+\s*(?:[一二三四五六七八九十\d]+楼)?(?:入口|出口)?", text))
    else:
        old_values = set(re.findall(r"(?:今天|昨天)?\s*\d{1,2}[:：]\d{2}", text))
    if len(old_values) > 1:
        raise ValueError("记录里有多个时间或地点，无法确定要改哪一个。请打开这条记录编辑完整正文。")
    if old_values:
        old = next(iter(old_values))
        return text.replace(old, value)
    return f"{label}：{value}。\n{text}"
