"""统一事件受理：手动、听写、语音助手与事件中心共用一个入口。

设计要点（对应 docs/CLAUDE_UNIFIED_INCIDENT_INTAKE_AND_REWARD_PLAN.md 第一批）：

* 先保存、后匹配：事件、来源提交、时间线和待办任务在同一个短事务内写入；
  模型/规则匹配由持久任务 worker 在事务外执行，失败不丢事件。
* 幂等：`incident_submissions` 对 (actor, submission_id) 唯一。同一提交重试
  返回同一受理结果；同一 submission_id 携带不同内容时报告版本冲突。
* 事务边界：调用方不直接顺序调用多个 Store；本模块用共享连接完成跨表原子写入。
* 持久任务：任务在 SQLite 中，带租约、重试次数与退避；进程重启后仍可恢复。
* 原文边界：只持久化脱敏后的叙述快照，不保存 ASR 中间结果或原录音。

兼容性：现有 `incidents.task_id` 是 UNIQUE NOT NULL，继续作为“一次受理任务”的
兼容映射使用（新登记写入 `intake-<随机>`），不修改已执行的 baseline。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from core.paths import data_path


DB = data_path("incidents.db")

SOURCE_CHANNELS = ("manual", "dictation", "voice_assistant", "incident_form")
SOURCE_LABELS = {
    "manual": "手动输入",
    "dictation": "语音听写",
    "voice_assistant": "语音助手",
    "incident_form": "事件中心表单",
}
OPERATIONS = ("report", "supplement", "correct", "retract_request")

INTAKE_PENDING = "pending_review"  # 仅保留给历史记录识别
INTAKE_REGISTERED = "registered"
INTAKE_VOID = "void"

INTAKE_LABELS = {
    INTAKE_REGISTERED: "已记录",
    INTAKE_PENDING: "待核对",
    INTAKE_VOID: "已作废",
}

MATCH_LABELS = {
    "pending": "待处理（后台匹配中）",
    "processing": "匹配中",
    "candidate": "系统匹配建议·需核对",
    "no_match": "未匹配到条款",
    "needs_information": "信息不足",
    "rules_missing": "规则未配置",
    "failed": "匹配失败，可重试",
    "stale": "已被新版本取代",
}

MAX_TEXT_CHARS = 2000
MAX_SNAPSHOT_CHARS = 1500
MAX_ATTEMPTS = 5
LEASE_SECONDS = 60
WORKER_POLL_SECONDS = 4.0

_SUBMISSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{3,80}$")

# 明确记录口令。只在句首附近出现时生效，避免普通对话里提到“记录”就入台账。
_RECORD_COMMANDS = (
    "帮我记录", "帮我记一下", "帮我登记", "帮我上报", "帮我报备",
    "现场记录", "记录现场", "登记事件", "上报事件",
)
_RECORD_PREFIX_RE = re.compile(
    r"^\s*(?:请|麻烦你?|那个|你好[，,]?)?\s*(?:"
    + "|".join(re.escape(item) for item in _RECORD_COMMANDS)
    + r")"
)
_BODY_STRIP_RE = re.compile(r"^[\s：:，,。.、\-—]+")

# 补充/更正口令必须带真实事件编号，避免普通句子被误当作修改记录。
_AMEND_RE = re.compile(
    r"^\s*(?:请|麻烦你?)?\s*(补充|更正)\s*(?:事件|记录)?\s*"
    r"(INC-\d{8}-[A-Za-z0-9]{6})\s*[：:,，]?\s*(.*)$",
    re.IGNORECASE,
)


class SubmissionError(Exception):
    """受理失败；消息可直接展示给用户，不包含原文。"""

    def __init__(self, message: str, *, code: str = "rejected"):
        super().__init__(message)
        self.code = code


class SubmissionConflict(SubmissionError):
    def __init__(self, message: str = "同一提交编号已用于不同内容，请刷新后重新提交。"):
        super().__init__(message, code="conflict")


@dataclass
class Submission:
    """规范化输入协议；actor/region 不在此处，必须由认证上下文注入。"""

    submission_id: str = ""
    text: str = ""
    source_channel: str = "manual"
    operation: str = "report"
    incident_id: str = ""
    conversation_id: str = ""
    source_message_id: str = ""
    source_origin: str = ""
    source_edited: bool = False
    attachments: list[str] = field(default_factory=list)
    client_captured_at: str = ""
    expected_version: int | None = None

    def normalized(self) -> "Submission":
        channel = (self.source_channel or "manual").strip()
        if channel not in SOURCE_CHANNELS:
            raise SubmissionError("未知的提交来源。", code="bad_channel")
        operation = (self.operation or "report").strip()
        if operation not in OPERATIONS:
            raise SubmissionError("未知的操作类型。", code="bad_operation")
        origin = (self.source_origin or "").strip()
        if origin not in {"typed", "asr", ""}:
            origin = "typed" if channel in ("manual", "incident_form") else "asr"
        if not origin:
            origin = "typed" if channel in ("manual", "incident_form") else "asr"
        submission_id = (self.submission_id or "").strip()
        if not submission_id:
            submission_id = uuid.uuid4().hex
        if not _SUBMISSION_ID_RE.match(submission_id):
            raise SubmissionError("提交编号格式不正确。", code="bad_submission_id")
        return Submission(
            submission_id=submission_id,
            text=(self.text or "").strip(),
            source_channel=channel,
            operation=operation,
            incident_id=(self.incident_id or "").strip(),
            conversation_id=(self.conversation_id or "").strip()[:120],
            source_message_id=(self.source_message_id or "").strip()[:120],
            source_origin=origin,
            source_edited=bool(self.source_edited),
            attachments=[str(item)[:120] for item in (self.attachments or [])][:20],
            client_captured_at=(self.client_captured_at or "").strip()[:40],
            expected_version=int(self.expected_version) if self.expected_version is not None else None,
        )


def parse_hint(raw: str) -> dict:
    """解析前端随消息提交的来源提示；不接受用户名/角色等身份字段。"""
    if not raw:
        return {}
    value = raw.strip()
    if not value:
        return {}
    if value.startswith("{"):
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        channel = str(data.get("channel") or "").strip()
        if channel not in SOURCE_CHANNELS and not data.get("record"):
            # 未知来源且不是显式记录模式：不采用，避免客户端自造来源。
            return {}
        if channel not in SOURCE_CHANNELS:
            channel = ""
        return {
            "channel": channel,
            "record": bool(data.get("record")),
            "edited": bool(data.get("edited")),
            "conversation_id": str(data.get("conversation_id") or "").strip()[:120],
            "message_id": str(data.get("message_id") or "").strip()[:120],
            "submission_id": str(data.get("submission_id") or "").strip()[:80],
        }
    if value in SOURCE_CHANNELS:
        return {"channel": value}
    return {}


def extract_record_request(text: str) -> tuple[bool, str]:
    """识别“帮我记录……”一类明确口令，返回 (是否口令, 事件正文)。

    普通咨询即使包含“异常/处罚/记录”等词也不会命中。
    """
    value = (text or "").strip()
    match = _RECORD_PREFIX_RE.match(value)
    if not match:
        return False, value
    body = _BODY_STRIP_RE.sub("", value[match.end():]).strip()
    return True, body


def extract_amend_request(text: str) -> dict | None:
    """识别“补充/更正记录 INC-...”口令；没有真实事件编号时不命中。"""
    match = _AMEND_RE.match((text or "").strip())
    if not match:
        return None
    verb, incident_id, body = match.groups()
    return {
        "operation": "supplement" if verb == "补充" else "correct",
        "incident_id": incident_id.upper(),
        "text": _BODY_STRIP_RE.sub("", body or "").strip(),
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _shift(seconds: float) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _date_bound(value: str) -> datetime | None:
    """校验 YYYY-MM-DD；无效日期交给调用方忽略，不拼进 SQL。"""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


@contextmanager
def transaction(path: str | Path):
    """共享连接工作单元：显式 BEGIN IMMEDIATE，异常整体回滚。"""
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _rows(rows) -> list[dict]:
    return [dict(row) for row in rows]


def _set_state(conn, incident_id: str, *, now: str, **fields) -> None:
    """在共享事务内更新登记状态；旧事件没有 state 行时先补一行。"""
    exists = conn.execute(
        "SELECT 1 FROM incident_state WHERE incident_id=?", (incident_id,)
    ).fetchone()
    if exists is None:
        conn.execute(
            "INSERT INTO incident_state(incident_id, updated) VALUES(?,?)",
            (incident_id, now),
        )
    if not fields:
        return
    assignments = ", ".join(f"{name}=?" for name in fields)
    conn.execute(
        f"UPDATE incident_state SET {assignments}, updated=? WHERE incident_id=?",
        (*fields.values(), now, incident_id),
    )


class IncidentIntake:
    def __init__(self, db: str | Path = DB, *, max_text_chars: int = MAX_TEXT_CHARS):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.max_text_chars = max_text_chars
        # 可选回调：应用层用它把结果同步到既有 RewardPenaltyStore；失败不影响主流程。
        self.match_sink = None
        self.ensure_schema()

    # ---------------- schema ----------------
    def ensure_schema(self) -> None:
        """增量迁移：只新增关系表与字段，不修改已执行的 baseline。"""
        with transaction(self.db) as conn:
            has_base = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidents'"
            ).fetchone()
        if has_base is None:
            # 独立使用本模块时补建现有基线；生产环境由 IncidentStore 先建好。
            from core.incident_store import IncidentStore
            IncidentStore(self.db)
        with transaction(self.db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_submissions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    incident_version INTEGER NOT NULL DEFAULT 1,
                    conversation_id TEXT NOT NULL DEFAULT '',
                    source_channel TEXT NOT NULL,
                    source_origin TEXT NOT NULL DEFAULT '',
                    source_edited INTEGER NOT NULL DEFAULT 0,
                    source_message_id TEXT NOT NULL DEFAULT '',
                    operation TEXT NOT NULL DEFAULT 'report',
                    text_sha256_12 TEXT NOT NULL,
                    text_chars INTEGER NOT NULL,
                    text_snapshot TEXT NOT NULL DEFAULT '',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    client_captured_at TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    UNIQUE(actor, submission_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_incident_submissions_incident "
                "ON incident_submissions(incident_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_tasks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    incident_id TEXT NOT NULL,
                    incident_version INTEGER NOT NULL,
                    ruleset_version TEXT NOT NULL DEFAULT '',
                    matcher_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires TEXT NOT NULL DEFAULT '',
                    next_attempt_at TEXT NOT NULL,
                    last_error_category TEXT NOT NULL DEFAULT '',
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_incident_tasks_ready "
                "ON incident_tasks(status, next_attempt_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_incident_tasks_incident "
                "ON incident_tasks(incident_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_match_batches(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL,
                    incident_version INTEGER NOT NULL,
                    task_id INTEGER NOT NULL DEFAULT 0,
                    ruleset_version TEXT NOT NULL DEFAULT '',
                    matcher_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    candidates_count INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    safe_error_category TEXT NOT NULL DEFAULT '',
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    UNIQUE(incident_id, incident_version, ruleset_version, matcher_version)
                )
                """
            )
            # 登记状态、版本与匹配状态放在关系表里，不修改已执行的 incidents baseline。
            # 旧事件没有 state 行时按 v1 / 历史记录展示。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_state(
                    incident_id TEXT PRIMARY KEY,
                    current_version INTEGER NOT NULL DEFAULT 1,
                    intake_status TEXT NOT NULL DEFAULT 'registered',
                    match_status TEXT NOT NULL DEFAULT 'pending',
                    source_channel TEXT NOT NULL DEFAULT '',
                    last_submission_id TEXT NOT NULL DEFAULT '',
                    match_batch_id INTEGER NOT NULL DEFAULT 0,
                    updated TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reward_penalty_links(
                    record_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    match_batch_id INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reward_penalty_links_incident "
                "ON reward_penalty_links(incident_id)"
            )
            # 事件版本账本：补充/更正追加版本，旧版本标记 superseded，匹配历史据此标为已过期。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_versions(
                    incident_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    submission_id TEXT NOT NULL,
                    operation TEXT NOT NULL DEFAULT 'report',
                    source_channel TEXT NOT NULL DEFAULT '',
                    source_edited INTEGER NOT NULL DEFAULT 0,
                    text_sha256_12 TEXT NOT NULL,
                    text_chars INTEGER NOT NULL,
                    created TEXT NOT NULL,
                    superseded INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(incident_id, version)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_incident_versions_submission "
                "ON incident_versions(incident_id, submission_id)"
            )

    # ---------------- 受理 ----------------
    def submit(
        self,
        submission: Submission,
        *,
        actor: str,
        region: str,
        guard=None,
        ruleset_version: str = "",
        matcher_version: str = "",
        workflow_status: str = "RECORDED",
    ) -> dict:
        """在一个短事务里写入事件、来源、时间线与待办任务，返回真实受理回执。

        operation=supplement/correct 时追加版本并重新入队匹配；旧版本和旧匹配结果
        保留，不会覆盖既有事实或历史结果。身份只取自调用方传入的认证上下文。
        """
        actor = (actor or "").strip()[:80]
        if not actor:
            raise SubmissionError("未获取到登录身份，不能登记事件。", code="unauthenticated")
        sub = submission.normalized()
        text = sub.text
        if len(text) < 5:
            raise SubmissionError("请至少描述 5 个字的现场事实后再登记。", code="too_short")
        if len(text) > self.max_text_chars:
            raise SubmissionError(f"描述超过 {self.max_text_chars} 字，请精简后再登记。", code="too_long")
        if sub.operation == "report" and sub.incident_id:
            raise SubmissionError("新事件登记不应携带事件编号。", code="bad_incident")
        if sub.operation in ("supplement", "correct") and not sub.incident_id:
            raise SubmissionError("补充或更正必须提供事件编号。", code="bad_incident")

        level = "allow"
        mapping = {}
        if guard is not None:
            level, _hits, mapping = guard.scan(text)
        if level in ("block", "review"):
            # 命中红线时只记录拦截元数据，不保存原文。
            raise SubmissionError("当前描述命中数据红线，请去除敏感内容后再登记。",
                                  code="redline_blocked")
        safe_text = guard.desensitize(text, mapping) if (guard is not None and mapping) else text
        safe_text = (safe_text or "").strip()[: self.max_text_chars]
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
        region = (region or "待确认").strip()[:40] or "待确认"
        received_at = _now()

        with transaction(self.db) as conn:
            existing = conn.execute(
                "SELECT * FROM incident_submissions WHERE actor=? AND submission_id=?",
                (actor, sub.submission_id),
            ).fetchone()
            if existing is not None:
                if (existing["text_sha256_12"] != digest or existing["operation"] != sub.operation
                        or (sub.incident_id and existing["incident_id"] != sub.incident_id)):
                    raise SubmissionConflict()
                incident = conn.execute(
                    "SELECT i.*, COALESCE(s.current_version,1) AS current_version, "
                    "COALESCE(s.intake_status,'registered') AS intake_status, "
                    "COALESCE(s.match_status,'pending') AS match_status "
                    "FROM incidents i LEFT JOIN incident_state s ON s.incident_id=i.id WHERE i.id=?",
                    (existing["incident_id"],),
                ).fetchone()
                if incident is None:
                    raise SubmissionError("原受理记录缺失，请使用新的提交编号重新登记。",
                                          code="incident_missing")
                return self._receipt(incident, existing, idempotent=True)

            if sub.operation in ("supplement", "correct"):
                return self._submit_amendment(
                    conn, sub, safe_text=safe_text, digest=digest, actor=actor,
                    received_at=received_at, ruleset_version=ruleset_version,
                    matcher_version=matcher_version,
                )
            if sub.operation != "report":
                raise SubmissionError("暂不支持该操作类型。", code="operation_unsupported")

            incident_id = f"INC-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
            task_id = f"intake-{uuid.uuid4().hex}"  # 兼容 incidents.task_id UNIQUE NOT NULL
            now = datetime.now().isoformat(timespec="seconds")
            workflow_status = (workflow_status or "RECORDED").strip()
            if workflow_status not in ("RECORDED", "PENDING_CONFIRMATION"):
                workflow_status = "RECORDED"
            conn.execute(
                """
                INSERT INTO incidents(
                    id,task_id,event_type,region,reported_status,risk_level,risk_score,
                    workflow_status,owner,source_sha256_12,source_chars,created_by,
                    confirmed_by,decision_note,due_at,created,updated
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    incident_id, task_id, "其他", region, "待确认", "unknown", 0,
                    workflow_status, "", digest, len(text), actor,
                    "", "", "", now, now,
                ),
            )
            conn.execute(
                "INSERT INTO incident_state(incident_id,current_version,intake_status,match_status,"
                "source_channel,last_submission_id,match_batch_id,updated) VALUES(?,?,?,?,?,?,?,?)",
                (incident_id, 1, INTAKE_REGISTERED, "pending", sub.source_channel,
                 sub.submission_id, 0, now),
            )
            conn.execute(
                """
                INSERT INTO incident_submissions(
                    submission_id,actor,incident_id,incident_version,conversation_id,
                    source_channel,source_origin,source_edited,source_message_id,operation,
                    text_sha256_12,text_chars,text_snapshot,attachments_json,
                    client_captured_at,received_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sub.submission_id, actor, incident_id, 1, sub.conversation_id,
                    sub.source_channel, sub.source_origin, 1 if sub.source_edited else 0,
                    sub.source_message_id, sub.operation,
                    digest, len(text), safe_text,
                    json.dumps(sub.attachments, ensure_ascii=False, separators=(",", ":")),
                    sub.client_captured_at, received_at,
                ),
            )
            conn.execute(
                "INSERT INTO incident_versions(incident_id,version,submission_id,operation,"
                "source_channel,source_edited,text_sha256_12,text_chars,created,superseded) "
                "VALUES(?,?,?,?,?,?,?,?,?,0)",
                (incident_id, 1, sub.submission_id, sub.operation, sub.source_channel,
                 1 if sub.source_edited else 0, digest, len(text), received_at),
            )
            conn.execute(
                "INSERT INTO incident_actions(incident_id,action,from_status,to_status,actor,note,created) "
                "VALUES(?,?,?,?,?,?,?)",
                (incident_id, "登记受理", "", workflow_status, actor,
                 f"来源：{SOURCE_LABELS.get(sub.source_channel, sub.source_channel)}"
                 f"｜来源版本：{'已编辑' if sub.source_edited else '原始'}", received_at),
            )
            self._enqueue_match(
                conn, incident_id, 1, received_at, ruleset_version, matcher_version,
            )
            incident = conn.execute(
                "SELECT i.*, COALESCE(s.current_version,1) AS current_version, "
                "COALESCE(s.intake_status,'registered') AS intake_status, "
                "COALESCE(s.match_status,'pending') AS match_status "
                "FROM incidents i LEFT JOIN incident_state s ON s.incident_id=i.id WHERE i.id=?",
                (incident_id,),
            ).fetchone()
            submission_row = conn.execute(
                "SELECT * FROM incident_submissions WHERE actor=? AND submission_id=?",
                (actor, sub.submission_id),
            ).fetchone()
            return self._receipt(incident, submission_row, idempotent=False)

    def _submit_amendment(
        self, conn, sub: Submission, *, safe_text: str, digest: str, actor: str,
        received_at: str, ruleset_version: str, matcher_version: str,
    ) -> dict:
        """在既有事务内追加版本；旧版本标为已过期，并创建当前版本的匹配任务。"""
        incident = conn.execute(
            "SELECT i.*, COALESCE(s.current_version,1) AS current_version, "
            "COALESCE(s.intake_status,'registered') AS intake_status, "
            "COALESCE(s.match_status,'pending') AS match_status "
            "FROM incidents i LEFT JOIN incident_state s ON s.incident_id=i.id WHERE i.id=?",
            (sub.incident_id,),
        ).fetchone()
        if incident is None:
            raise SubmissionError("未找到该事件编号。", code="incident_missing")
        if incident["created_by"] != actor:
            raise SubmissionError("只能补充或更正本人登记的事件。", code="forbidden")
        if (incident["intake_status"] or "") == INTAKE_VOID:
            raise SubmissionError("该事件已作废，不能补充或更正。", code="incident_void")
        current_version = int(incident["current_version"] or 1)
        if sub.expected_version is not None and sub.expected_version != current_version:
            raise SubmissionConflict("这条记录已被其他操作更新，请重新打开记录后再修改。")
        next_version = current_version + 1
        latest = conn.execute(
            "SELECT text_snapshot FROM incident_submissions WHERE incident_id=? "
            "ORDER BY incident_version DESC, id DESC LIMIT 1",
            (sub.incident_id,),
        ).fetchone()
        base_text = (latest["text_snapshot"] or "").strip() if latest else ""
        if sub.operation == "supplement":
            snapshot = (base_text + "\n【补充】\n" + safe_text).strip()
        else:
            snapshot = safe_text
        if len(snapshot) > self.max_text_chars:
            raise SubmissionError(
                f"补充或更正后正文超过 {self.max_text_chars} 字，请精简后再提交。",
                code="too_long",
            )
        snapshot_digest = hashlib.sha256(
            snapshot.encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        now = received_at
        conn.execute(
            "UPDATE incidents SET source_sha256_12=?, source_chars=?, policy_matches_json='[]', "
            "policy_confidence=0, updated=? WHERE id=?",
            (snapshot_digest, len(snapshot), now, sub.incident_id),
        )
        conn.execute(
            "UPDATE incident_versions SET superseded=1 WHERE incident_id=?",
            (sub.incident_id,),
        )
        conn.execute(
            "INSERT INTO incident_versions(incident_id,version,submission_id,operation,"
            "source_channel,source_edited,text_sha256_12,text_chars,created,superseded) "
            "VALUES(?,?,?,?,?,?,?,?,?,0)",
            (sub.incident_id, next_version, sub.submission_id, sub.operation,
             sub.source_channel, 1 if sub.source_edited else 0,
             snapshot_digest, len(snapshot), now),
        )
        conn.execute(
            """
            INSERT INTO incident_submissions(
                submission_id,actor,incident_id,incident_version,conversation_id,
                source_channel,source_origin,source_edited,source_message_id,operation,
                text_sha256_12,text_chars,text_snapshot,attachments_json,
                client_captured_at,received_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sub.submission_id, actor, sub.incident_id, next_version, sub.conversation_id,
                sub.source_channel, sub.source_origin, 1 if sub.source_edited else 0,
                sub.source_message_id, sub.operation,
                digest, len(sub.text), snapshot,
                json.dumps(sub.attachments, ensure_ascii=False, separators=(",", ":")),
                sub.client_captured_at, now,
            ),
        )
        _set_state(
            conn, sub.incident_id, now=now, current_version=next_version,
            intake_status=INTAKE_REGISTERED, match_status="pending",
            source_channel=sub.source_channel, last_submission_id=sub.submission_id,
            match_batch_id=0,
        )
        action = "补充登记" if sub.operation == "supplement" else "更正登记"
        conn.execute(
            "INSERT INTO incident_actions(incident_id,action,from_status,to_status,actor,note,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (sub.incident_id, action, incident["workflow_status"], incident["workflow_status"],
             actor, f"版本 v{next_version}｜来源：{SOURCE_LABELS.get(sub.source_channel, sub.source_channel)}",
             now),
        )
        self._enqueue_match(
            conn, sub.incident_id, next_version, now, ruleset_version, matcher_version,
        )
        refreshed = conn.execute(
            "SELECT i.*, COALESCE(s.current_version,1) AS current_version, "
            "COALESCE(s.intake_status,'registered') AS intake_status, "
            "COALESCE(s.match_status,'pending') AS match_status "
            "FROM incidents i LEFT JOIN incident_state s ON s.incident_id=i.id WHERE i.id=?",
            (sub.incident_id,),
        ).fetchone()
        submission_row = conn.execute(
            "SELECT * FROM incident_submissions WHERE actor=? AND submission_id=?",
            (actor, sub.submission_id),
        ).fetchone()
        return self._receipt(refreshed, submission_row, idempotent=False)

    @staticmethod
    def _enqueue_match(
        conn, incident_id: str, version: int, now: str,
        ruleset_version: str, matcher_version: str,
    ) -> None:
        dedupe_key = f"reward_match:{incident_id}:{version}:{ruleset_version}:{matcher_version}"
        conn.execute(
            """
            INSERT OR IGNORE INTO incident_tasks(
                task_type,dedupe_key,incident_id,incident_version,ruleset_version,
                matcher_version,status,attempts,max_attempts,lease_owner,lease_expires,
                next_attempt_at,last_error_category,created,updated
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "reward_match", dedupe_key, incident_id, version, ruleset_version,
                matcher_version, "pending", 0, MAX_ATTEMPTS, "", "",
                now, "", now, now,
            ),
        )

    def _receipt(self, incident, submission_row, *, idempotent: bool) -> dict:
        item = dict(incident)
        sub = dict(submission_row)
        return {
            "incident_id": item.get("id", ""),
            "submission_id": sub.get("submission_id", ""),
            "version": int(sub.get("incident_version") or item.get("current_version") or 1),
            "status": item.get("intake_status") or INTAKE_REGISTERED,
            "workflow_status": item.get("workflow_status", ""),
            "match_status": item.get("match_status") or "pending",
            "source_channel": sub.get("source_channel", ""),
            "source_origin": sub.get("source_origin", ""),
            "source_edited": bool(sub.get("source_edited")),
            "operation": sub.get("operation", "report"),
            "event_type": item.get("event_type", "其他"),
            "region": item.get("region", ""),
            "reported_status": item.get("reported_status", ""),
            "received_at": sub.get("received_at", ""),
            "created": item.get("created", ""),
            "idempotent": bool(idempotent),
        }

    def link_task(self, incident_id: str, task_id: str) -> bool:
        """模型任务建立后，把兼容的 task_id 指向真实任务，保留旧时间线更新逻辑。"""
        if not task_id:
            return False
        with transaction(self.db) as conn:
            cur = conn.execute(
                "UPDATE incidents SET task_id=? WHERE id=? AND task_id LIKE 'intake-%'",
                (str(task_id)[:80], (incident_id or "").strip()),
            )
            changed = cur.rowcount
        return changed == 1

    # ---------------- 查询 ----------------
    def get(self, incident_id: str) -> dict | None:
        with transaction(self.db) as conn:
            row = conn.execute(
                "SELECT i.*, COALESCE(s.current_version,1) AS current_version, "
                "COALESCE(s.intake_status,'registered') AS intake_status, "
                "COALESCE(s.match_status,'pending') AS match_status, "
                "COALESCE(s.source_channel,'') AS source_channel, "
                "COALESCE(s.match_batch_id,0) AS match_batch_id "
                "FROM incidents i LEFT JOIN incident_state s ON s.incident_id=i.id WHERE i.id=?",
                ((incident_id or "").strip(),),
            ).fetchone()
            if row is None:
                return None
            sub = conn.execute(
                "SELECT * FROM incident_submissions WHERE incident_id=? "
                "ORDER BY incident_version DESC, id DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
        item = dict(row)
        item["submission"] = dict(sub) if sub else None
        return item

    def receipt(self, incident_id: str, *, actor: str, can_view_all: bool = False) -> dict | None:
        """按认证身份返回回执；无权用户拿不到他人的事件。"""
        item = self.get(incident_id)
        if item is None:
            return None
        if not can_view_all and item.get("created_by") != (actor or "").strip()[:80]:
            return None
        sub = item.get("submission") or {}
        return self._receipt(item, sub, idempotent=True)

    def list_recent(self, *, username: str = "", all_users: bool = False, limit: int = 30) -> list[dict]:
        """事件台账视图：合并登记状态；权限过滤与现有台账一致。"""
        safe_limit = max(1, min(int(limit), 200))
        return self.search_recent(
            username=username, all_users=all_users, page=1, page_size=safe_limit,
        )["rows"]

    def search_recent(
        self, *,
        username: str = "",
        all_users: bool = False,
        query: str = "",
        date_from: str = "",
        date_to: str = "",
        source_channel: str = "",
        match_status: str = "",
        created_by: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """按当前版本正文、来源、状态、日期和记录人筛选台账，并分页。

        正文关键词只匹配当前版本快照；旧版本仍保留在修改历史中，但不会让
        已更正的记录出现在当前搜索结果里。
        """
        limit = max(1, min(int(page_size or 20), 100))
        try:
            wanted_page = max(1, int(page or 1))
        except (TypeError, ValueError):
            wanted_page = 1
        base = (
            "SELECT i.*, COALESCE(s.current_version,1) AS current_version, "
            "COALESCE(s.intake_status,'registered') AS intake_status, "
            "COALESCE(s.match_status,'pending') AS match_status, "
            "COALESCE(s.source_channel,'') AS source_channel, "
            "COALESCE(s.match_batch_id,0) AS match_batch_id, "
            "(SELECT sub.text_snapshot FROM incident_submissions sub "
            " WHERE sub.incident_id=i.id ORDER BY sub.incident_version DESC, sub.id DESC LIMIT 1) "
            "AS text_preview, "
            "(SELECT sub.received_at FROM incident_submissions sub "
            " WHERE sub.incident_id=i.id ORDER BY sub.incident_version DESC, sub.id DESC LIMIT 1) "
            "AS received_at, "
            "(SELECT b.result_json FROM incident_match_batches b "
            " WHERE b.incident_id=i.id AND b.incident_version=COALESCE(s.current_version,1) "
            " ORDER BY b.id DESC LIMIT 1) AS current_match_json, "
            "(SELECT b.ruleset_version FROM incident_match_batches b "
            " WHERE b.incident_id=i.id AND b.incident_version=COALESCE(s.current_version,1) "
            " ORDER BY b.id DESC LIMIT 1) AS current_ruleset "
            "FROM incidents i LEFT JOIN incident_state s ON s.incident_id=i.id "
        )
        where: list[str] = []
        params: list = []
        if not all_users:
            where.append("i.created_by=?")
            params.append((username or "system")[:80])
        elif (created_by or "").strip():
            where.append("i.created_by=?")
            params.append(created_by.strip()[:80])
        keyword = (query or "").strip()
        if keyword:
            like = f"%{keyword}%"
            where.append(
                "(i.id LIKE ? OR EXISTS (SELECT 1 FROM incident_submissions sub "
                "WHERE sub.incident_id=i.id "
                "AND sub.incident_version=COALESCE(s.current_version,1) "
                "AND sub.text_snapshot LIKE ?))"
            )
            params.extend([like, like])
        start = _date_bound(date_from)
        if start:
            where.append("i.created>=?")
            params.append(start.isoformat())
        end = _date_bound(date_to)
        if end:
            where.append("i.created<?")
            params.append((end + timedelta(days=1)).isoformat())
        source = (source_channel or "").strip()
        if source:
            where.append("COALESCE(s.source_channel,'')=?")
            params.append(source[:40])
        status = (match_status or "").strip()
        if status:
            where.append("COALESCE(s.match_status,'pending')=?")
            params.append(status[:40])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        with transaction(self.db) as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM incidents i "
                "LEFT JOIN incident_state s ON s.incident_id=i.id" + where_sql,
                params,
            ).fetchone()[0])
            pages = max(1, (total + limit - 1) // limit)
            current = min(wanted_page, pages)
            rows = conn.execute(
                base + where_sql + " ORDER BY i.created DESC, i.id DESC LIMIT ? OFFSET ?",
                [*params, limit, (current - 1) * limit],
            ).fetchall()
        return {
            "rows": [dict(row) for row in rows],
            "total": total,
            "page": current,
            "pages": pages,
            "page_size": limit,
        }

    def latest_match(self, incident_id: str, *, version: int | None = None) -> dict | None:
        """按事件版本取匹配批次；默认只取当前版本，旧结果不能冒充本轮结果。"""
        with transaction(self.db) as conn:
            state = conn.execute(
                "SELECT current_version FROM incident_state WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            current = int(state["current_version"] or 1) if state else 1
            target = current if version is None else int(version)
            row = conn.execute(
                "SELECT * FROM incident_match_batches WHERE incident_id=? AND incident_version=? "
                "ORDER BY id DESC LIMIT 1",
                (incident_id, target),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["result"] = json.loads(item["result_json"])
        except (TypeError, ValueError):
            item["result"] = {}
        item["current_version"] = current
        item["stale"] = target < current
        return item

    def submissions(self, incident_id: str) -> list[dict]:
        """版本历史（含脱敏正文快照）；只应由已授权调用方读取。"""
        with transaction(self.db) as conn:
            rows = conn.execute(
                "SELECT * FROM incident_submissions WHERE incident_id=? "
                "ORDER BY incident_version ASC, id ASC",
                ((incident_id or "").strip(),),
            ).fetchall()
        return _rows(rows)

    def match_history(self, incident_id: str) -> list[dict]:
        """全部匹配批次；按当前版本标记过期，旧结果保留不删除。"""
        with transaction(self.db) as conn:
            rows = conn.execute(
                "SELECT * FROM incident_match_batches WHERE incident_id=? "
                "ORDER BY incident_version ASC, id ASC",
                ((incident_id or "").strip(),),
            ).fetchall()
            state = conn.execute(
                "SELECT current_version FROM incident_state WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
        current = int(state["current_version"] or 1) if state else 1
        history = []
        for row in rows:
            item = dict(row)
            try:
                item["result"] = json.loads(item["result_json"])
            except (TypeError, ValueError):
                item["result"] = {}
            item["stale"] = int(item.get("incident_version") or 1) < current
            history.append(item)
        return history

    def detail(self, incident_id: str, *, actor: str, can_view_all: bool = False) -> dict | None:
        """受权限保护的事件详情：正文快照、版本、匹配历史与时间线。"""
        item = self.get(incident_id)
        if item is None:
            return None
        if not can_view_all and item.get("created_by") != (actor or "").strip()[:80]:
            return None
        versions = self.submissions(incident_id)
        item["versions"] = versions
        item["received_at"] = (
            versions[-1].get("received_at") if versions else item.get("created", "")
        )
        item["matches"] = self.match_history(incident_id)
        with transaction(self.db) as conn:
            rows = conn.execute(
                "SELECT * FROM incident_actions WHERE incident_id=? ORDER BY id ASC",
                (incident_id,),
            ).fetchall()
        item["actions"] = _rows(rows)
        return item

    def retry_match(self, incident_id: str, matcher, *, limit: int = 3) -> int:
        """把当前版本的匹配重新排队并立即处理；旧规则任务作废，旧批次保留。"""
        incident = self.get(incident_id)
        if incident is None:
            raise SubmissionError("未找到该事件编号。", code="incident_missing")
        version = int(incident.get("current_version") or 1)
        ruleset = str(getattr(matcher, "ruleset_version", "") or "")
        matcher_version = str(getattr(matcher, "matcher_version", "") or "")
        now = _now()
        with transaction(self.db) as conn:
            rows = conn.execute(
                "SELECT id,status,ruleset_version,matcher_version FROM incident_tasks "
                "WHERE task_type='reward_match' AND incident_id=? AND incident_version=? "
                "ORDER BY id DESC",
                (incident_id, version),
            ).fetchall()
            matching = [
                row for row in rows
                if str(row["ruleset_version"] or "") == ruleset
                and str(row["matcher_version"] or "") == matcher_version
            ]
            # 旧规则任务不能改跑新规则后仍带着旧版本标签；标记作废并新建当前规则任务。
            for row in rows:
                if row not in matching and row["status"] in ("pending", "processing", "failed", "done"):
                    conn.execute(
                        "UPDATE incident_tasks SET status='stale', lease_owner='', lease_expires='', "
                        "last_error_category='ruleset_changed', updated=? WHERE id=?",
                        (now, int(row["id"])),
                    )
            if matching:
                row = matching[0]
                if row["status"] == "processing":
                    return 0
                # 手动重试要绕过失败退避窗口：立刻可领取，并恢复完整重试次数。
                conn.execute(
                    "UPDATE incident_tasks SET status='pending', attempts=0, "
                    "lease_owner='', lease_expires='', next_attempt_at=?, "
                    "last_error_category='', updated=? WHERE id=?",
                    (now, now, int(row["id"])),
                )
            else:
                self._enqueue_match(conn, incident_id, version, now, ruleset, matcher_version)
        return self.process_incident(incident_id, matcher, limit=limit)

    # ---------------- 持久任务 worker ----------------
    def claim_task(
        self, *, worker_id: str = "", lease_seconds: int = LEASE_SECONDS,
        incident_id: str = "",
    ) -> dict | None:
        now = _now()
        expires = _shift(lease_seconds)
        worker_id = (worker_id or f"worker-{uuid.uuid4().hex[:8]}")[:80]
        with transaction(self.db) as conn:
            # 租约到期：视为 worker 中途退出，任务重新排队。
            conn.execute(
                "UPDATE incident_tasks SET status='pending', lease_owner='', lease_expires='', "
                "next_attempt_at=?, updated=? "
                "WHERE status='processing' AND lease_expires<>'' AND lease_expires<?",
                (now, now, now),
            )
            if incident_id:
                row = conn.execute(
                    "SELECT * FROM incident_tasks WHERE status='pending' AND next_attempt_at<=? "
                    "AND incident_id=? ORDER BY id LIMIT 1",
                    (now, incident_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM incident_tasks WHERE status='pending' AND next_attempt_at<=? "
                    "ORDER BY id LIMIT 1",
                    (now,),
                ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                "UPDATE incident_tasks SET status='processing', lease_owner=?, lease_expires=?, "
                "attempts=attempts+1, updated=? WHERE id=? AND status='pending'",
                (worker_id, expires, now, row["id"]),
            )
            if cur.rowcount != 1:
                return None
            task = dict(row)
            task["attempts"] = int(task.get("attempts") or 0) + 1
            task["lease_owner"] = worker_id
            return task

    def process_task(self, task: dict, matcher) -> str:
        """执行一次匹配；异常转为安全错误类别，不丢失事件和任务。"""
        incident_id = task.get("incident_id", "")
        version = int(task.get("incident_version") or 1)
        incident = self.get(incident_id)
        if not incident or int(incident.get("current_version") or 1) != version:
            self._finish_task(task, "stale", "version_mismatch")
            return "stale"
        if (incident.get("intake_status") or "") == INTAKE_VOID:
            self._finish_task(task, "stale", "incident_void")
            return "stale"
        # 任务标签必须与本次实际使用的规则一致；制度更新后旧任务改跑新规则时，
        # 不能把新计算结果写成旧规则版本，改为重新入队当前版本任务并保留旧批次。
        task_ruleset = str(task.get("ruleset_version") or "")
        task_matcher = str(task.get("matcher_version") or "")
        current_ruleset = str(getattr(matcher, "ruleset_version", "") or "")
        current_matcher = str(getattr(matcher, "matcher_version", "") or "")
        if task_ruleset != current_ruleset or task_matcher != current_matcher:
            self._finish_task(task, "stale", "ruleset_changed")
            self._enqueue_current_task(incident_id, version, current_ruleset, current_matcher)
            return "requeued"
        submission = incident.get("submission") or {}
        text = (submission.get("text_snapshot") or "").strip()
        try:
            if not text:
                raise ValueError("empty_snapshot")
            result = matcher.match(text)
            status = str(result.get("status") or "no_match")
            if status not in ("candidate", "no_match", "needs_information", "rules_missing"):
                status = "no_match"
            batch_id = self._save_match(task, incident, result, status)
            self._finish_task(task, "done", "")
            self._notify_match(task, incident, batch_id, result, status, text)
            return status
        except Exception as exc:  # noqa: BLE001 - 任务层必须吞掉匹配异常
            # 只保存异常类别，不把异常文本（可能含原文）写入任务表或日志。
            category = type(exc).__name__[:60] or "Exception"
            attempts = int(task.get("attempts") or 1)
            max_attempts = int(task.get("max_attempts") or MAX_ATTEMPTS)
            if attempts >= max_attempts:
                self._finish_task(task, "dead", category, match_status="failed")
                return "failed"
            backoff = min(300, int(15 * math.pow(2, max(0, attempts - 1))))
            self._retry_task(task, category, backoff, match_status="failed")
            return "retry"

    def _enqueue_current_task(
        self, incident_id: str, version: int, ruleset_version: str, matcher_version: str,
    ) -> None:
        """按当前规则版本补一条任务；与既有同版本任务靠 dedupe_key 去重。"""
        now = _now()
        with transaction(self.db) as conn:
            self._enqueue_match(
                conn, incident_id, version, now, ruleset_version, matcher_version,
            )

    def _save_match(self, task: dict, incident: dict, result: dict, status: str) -> int:
        matches = list(result.get("matches") or [])
        now = _now()
        first = matches[0] if matches else {}
        confidence = float(first.get("score") or 0.0) if first else 0.0
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        batch_id = 0
        with transaction(self.db) as conn:
            current = conn.execute(
                "SELECT COALESCE(s.current_version,1) AS current_version FROM incidents i "
                "LEFT JOIN incident_state s ON s.incident_id=i.id WHERE i.id=?",
                (incident["id"],),
            ).fetchone()
            if current is None or int(current["current_version"] or 1) != int(task["incident_version"]):
                return 0
            existing = conn.execute(
                "SELECT id FROM incident_match_batches WHERE incident_id=? AND incident_version=? "
                "AND ruleset_version=? AND matcher_version=?",
                (incident["id"], int(task["incident_version"]),
                 task.get("ruleset_version") or "", task.get("matcher_version") or ""),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO incident_match_batches(
                    incident_id,incident_version,task_id,ruleset_version,matcher_version,
                    status,candidates_count,result_json,safe_error_category,created,updated
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(incident_id,incident_version,ruleset_version,matcher_version)
                DO UPDATE SET task_id=excluded.task_id,status=excluded.status,
                    candidates_count=excluded.candidates_count,result_json=excluded.result_json,
                    safe_error_category='',updated=excluded.updated
                """,
                (
                    incident["id"], int(task["incident_version"]), int(task["id"]),
                    task.get("ruleset_version") or "", task.get("matcher_version") or "",
                    status, len(matches), payload, "", now, now,
                ),
            )
            batch = conn.execute(
                "SELECT id FROM incident_match_batches WHERE incident_id=? AND incident_version=? "
                "AND ruleset_version=? AND matcher_version=?",
                (incident["id"], int(task["incident_version"]),
                 task.get("ruleset_version") or "", task.get("matcher_version") or ""),
            ).fetchone()
            batch_id = int(batch["id"]) if batch else 0
            conn.execute(
                "UPDATE incidents SET policy_matches_json=?, policy_confidence=?, updated=? "
                "WHERE id=?",
                (
                    json.dumps(matches, ensure_ascii=False, separators=(",", ":")),
                    confidence, now, incident["id"],
                ),
            )
            _set_state(
                conn, incident["id"], now=now, match_status=status,
                match_batch_id=batch_id,
            )
            if existing is None:
                conn.execute(
                    "INSERT INTO incident_actions(incident_id,action,from_status,to_status,actor,note,created) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (incident["id"], "奖惩匹配完成", incident.get("workflow_status", ""),
                     incident.get("workflow_status", ""), "system",
                     f"版本 v{int(task['incident_version'])}｜结果：{MATCH_LABELS.get(status, status)}",
                     now),
                )
        return batch_id

    def _notify_match(
        self, task: dict, incident: dict, batch_id: int, result: dict, status: str, text: str,
    ) -> None:
        """把结果同步给应用层（例如既有奖惩记录页）；回调失败不影响已保存的匹配。"""
        sink = self.match_sink
        if sink is None or not batch_id:
            return
        try:
            sink({
                "incident_id": incident.get("id", ""),
                "incident_version": int(task.get("incident_version") or 1),
                "batch_id": int(batch_id),
                "status": status,
                "result": result,
                "text": text,
                "created_by": incident.get("created_by", "system"),
                "created": _now(),
            })
        except Exception:  # noqa: BLE001 - 结果已在批次表，回调失败只影响展示副本
            pass

    def _finish_task(self, task: dict, status: str, category: str, *, match_status: str | None = None) -> None:
        """结束任务：只有仍持有租约的执行者可以写回，且状态只作用于任务对应版本。"""
        now = _now()
        with transaction(self.db) as conn:
            cur = conn.execute(
                "UPDATE incident_tasks SET status=?, lease_owner='', lease_expires='', "
                "last_error_category=?, updated=? WHERE id=? AND status='processing' "
                "AND lease_owner=?",
                (status, category[:60], now, int(task["id"]),
                 str(task.get("lease_owner") or "")),
            )
            if match_status and cur.rowcount == 1:
                self._set_state_for_version(
                    conn, task.get("incident_id", ""), int(task.get("incident_version") or 1),
                    now=now, match_status=match_status,
                )

    def _retry_task(self, task: dict, category: str, backoff_seconds: int, *, match_status: str | None = None) -> None:
        now = _now()
        with transaction(self.db) as conn:
            cur = conn.execute(
                "UPDATE incident_tasks SET status='pending', lease_owner='', lease_expires='', "
                "next_attempt_at=?, last_error_category=?, updated=? WHERE id=? "
                "AND status='processing' AND lease_owner=?",
                (_shift(backoff_seconds), category[:60], now, int(task["id"]),
                 str(task.get("lease_owner") or "")),
            )
            if match_status and cur.rowcount == 1:
                self._set_state_for_version(
                    conn, task.get("incident_id", ""), int(task.get("incident_version") or 1),
                    now=now, match_status=match_status,
                )

    @staticmethod
    def _set_state_for_version(
        conn, incident_id: str, version: int, *, now: str, **fields,
    ) -> bool:
        """事件版本已前进时，迟到任务不得改写当前状态。"""
        current = conn.execute(
            "SELECT current_version FROM incident_state WHERE incident_id=?",
            (incident_id,),
        ).fetchone()
        if current is None or int(current["current_version"] or 1) != int(version):
            return False
        _set_state(conn, incident_id, now=now, **fields)
        return True

    def has_ready_tasks(self, *, incident_id: str = "") -> bool:
        """只读探测：空闲轮询不反复占用写锁。"""
        now = _now()
        conn = sqlite3.connect(str(self.db), timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            sql = (
                "SELECT 1 FROM incident_tasks WHERE "
                "((status='pending' AND next_attempt_at<=?) OR "
                "(status='processing' AND lease_expires<>'' AND lease_expires<?))"
            )
            params: tuple = (now, now)
            if incident_id:
                sql += " AND incident_id=?"
                params += (incident_id,)
            return conn.execute(sql + " LIMIT 1", params).fetchone() is not None
        finally:
            conn.close()

    def run_pending(self, matcher, *, limit: int = 5, incident_id: str = "") -> int:
        done = 0
        while done < limit:
            if done == 0 and not self.has_ready_tasks(incident_id=incident_id):
                break
            task = self.claim_task(incident_id=incident_id)
            if task is None:
                break
            self.process_task(task, matcher)
            done += 1
        return done

    def process_incident(self, incident_id: str, matcher, *, limit: int = 3) -> int:
        """同步处理某个事件的待办任务（前台需要即时结果时使用）。"""
        return self.run_pending(matcher, limit=limit, incident_id=incident_id)

    def task_counts(self) -> dict:
        with transaction(self.db) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM incident_tasks GROUP BY status").fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    # ---------------- 事件研判结果回填（事件中心表单） ----------------
    def attach_analysis(
        self,
        incident_id: str,
        *,
        expected_version: int,
        event_type: str,
        region: str,
        reported_status: str,
        risk_level: str,
        risk_score: int,
        extracted_facts: dict | None = None,
        policy_matches: list | None = None,
        source_references: list | None = None,
        missing_info: list | None = None,
        policy_confidence: float = 0.0,
        due_at: str = "",
        urgency_level: str = "unknown",
        urgency_score: int = 0,
        actor: str = "",
    ) -> bool:
        """把模型研判结果写回同一事件；只更新版本未变化的记录。"""
        unsafe = {"description", "source_text", "raw_text", "event"}
        safe_facts = {str(k): v for k, v in (extracted_facts or {}).items() if str(k) not in unsafe}
        now = _now()
        incident_id = (incident_id or "").strip()
        with transaction(self.db) as conn:
            current = conn.execute(
                "SELECT COALESCE(s.current_version,1) AS current_version FROM incidents i "
                "LEFT JOIN incident_state s ON s.incident_id=i.id WHERE i.id=?",
                (incident_id,),
            ).fetchone()
            if current is None or int(current["current_version"] or 1) != int(expected_version):
                return False
            cur = conn.execute(
                """
                UPDATE incidents SET event_type=?, region=?, reported_status=?, risk_level=?,
                    risk_score=?, extracted_facts_json=?, policy_matches_json=?,
                    source_references_json=?, missing_info_json=?, policy_confidence=?,
                    due_at=?, urgency_level=?, urgency_score=?, updated=?
                WHERE id=?
                """,
                (
                    (event_type or "其他")[:40], (region or "待确认")[:40],
                    (reported_status or "待确认")[:30],
                    risk_level if risk_level in {"critical", "high", "medium", "low"} else "unknown",
                    max(0, int(risk_score or 0)),
                    json.dumps(safe_facts, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(list(policy_matches or []), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(list(source_references or []), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(list(missing_info or []), ensure_ascii=False, separators=(",", ":")),
                    max(0.0, min(1.0, float(policy_confidence or 0.0))),
                    (due_at or "")[:40], str(urgency_level or "unknown")[:30],
                    max(0, int(urgency_score or 0)), now, incident_id,
                ),
            )
            if cur.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO incident_actions(incident_id,action,from_status,to_status,actor,note,created) "
                "VALUES(?,?,?,?,?,?,?)",
                (incident_id, "研判完成", "PENDING_CONFIRMATION", "PENDING_CONFIRMATION",
                 (actor or "system")[:80], f"风险等级：{risk_level}｜匹配条款：{len(policy_matches or [])}", now),
            )
        return True


class IncidentWorker:
    """轮询持久任务表；重启后依靠数据库恢复，不依赖内存队列。"""

    def __init__(self, intake: IncidentIntake, matcher, *, poll_seconds: float = WORKER_POLL_SECONDS):
        self.intake = intake
        self.matcher = matcher
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "IncidentWorker":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._loop, name="incident-intake-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.intake.run_pending(self.matcher, limit=5)
            except Exception:  # noqa: BLE001 - worker 不因单次数据库忙退出
                pass
            self._stop.wait(self.poll_seconds)


def start_worker(intake: IncidentIntake, matcher, *, poll_seconds: float = WORKER_POLL_SECONDS) -> IncidentWorker:
    return IncidentWorker(intake, matcher, poll_seconds=poll_seconds).start()
