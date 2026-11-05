"""异常事件闭环台账。

任务台账仍只保存模型任务元数据；本模块单独保存事件分类、状态、责任人与
人工确认记录，避免把“AI 已生成建议”误认为“现场事件已结案”。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core.paths import data_path
from core.sqlite_util import connect_db


DB = data_path("incidents.db")

STATUS_LABELS = {
    "RECORDED": "已记录",
    "PENDING_CONFIRMATION": "待主管确认",
    "IN_PROGRESS": "处置中",
    "NEEDS_INFO": "待补充",
    "CLOSED": "已结案",
}

TRANSITIONS = {
    "PENDING_CONFIRMATION": {"确认处置": "IN_PROGRESS", "退回补充": "NEEDS_INFO"},
    "NEEDS_INFO": {"重新提交": "PENDING_CONFIRMATION"},
    "IN_PROGRESS": {"退回补充": "NEEDS_INFO", "确认结案": "CLOSED"},
    "CLOSED": {},
}

SLA_MINUTES = {"critical": 3, "high": 5, "medium": 15, "low": 480, "unknown": 0}


class IncidentStore:
    def __init__(self, db: str | Path = DB):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        return connect_db(self.db)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents(
                    id TEXT PRIMARY KEY,
                    task_id TEXT UNIQUE NOT NULL,
                    event_type TEXT NOT NULL,
                    region TEXT NOT NULL,
                    reported_status TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_score INTEGER NOT NULL,
                    workflow_status TEXT NOT NULL,
                    owner TEXT DEFAULT '',
                    source_sha256_12 TEXT NOT NULL,
                    source_chars INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT DEFAULT '',
                    decision_note TEXT DEFAULT '',
                    due_at TEXT DEFAULT '',
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL
                )
                """
            )
            # 向后兼容旧 incidents.db：仅追加制度匹配与现场紧急程度字段。
            existing = {row[1] for row in conn.execute("PRAGMA table_info(incidents)").fetchall()}
            additions = {
                "urgency_level": "TEXT DEFAULT 'unknown'",
                "urgency_score": "INTEGER DEFAULT 0",
                "extracted_facts_json": "TEXT DEFAULT '{}'",
                "policy_matches_json": "TEXT DEFAULT '[]'",
                "source_references_json": "TEXT DEFAULT '[]'",
                "missing_info_json": "TEXT DEFAULT '[]'",
                "policy_confidence": "REAL DEFAULT 0",
                "manual_review": "INTEGER DEFAULT 1",
            }
            for name, declaration in additions.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE incidents ADD COLUMN {name} {declaration}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incident_actions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    note TEXT DEFAULT '',
                    created TEXT NOT NULL
                )
                """
            )

    def create(
        self,
        *,
        task_id: str,
        event_type: str,
        region: str,
        reported_status: str,
        risk_level: str,
        risk_score: int,
        source_text: str,
        created_by: str,
        urgency_level: str = "unknown",
        urgency_score: int = 0,
        extracted_facts: dict | None = None,
        policy_matches: list | None = None,
        source_references: list | None = None,
        missing_info: list | None = None,
        policy_confidence: float = 0.0,
        manual_review: bool = True,
    ) -> dict:
        now = datetime.now()
        level = risk_level if risk_level in SLA_MINUTES else "unknown"
        minutes = SLA_MINUTES[level]
        due_at = (now + timedelta(minutes=minutes)).isoformat(timespec="minutes") if minutes else ""
        incident_id = f"INC-{now:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        # 事实结果可以落库，但原始/自由文本描述继续遵守既有隐私边界，不进入台账。
        unsafe_fact_keys = {"description", "source_text", "raw_text", "event"}
        safe_extracted_facts = {
            str(key): value for key, value in (extracted_facts or {}).items()
            if str(key) not in unsafe_fact_keys
        }
        record = {
            "id": incident_id,
            "task_id": task_id,
            "event_type": (event_type or "其他")[:40],
            "region": (region or "待确认")[:40],
            "reported_status": (reported_status or "待确认")[:30],
            "risk_level": level,
            "risk_score": max(0, int(risk_score or 0)),
            "workflow_status": "PENDING_CONFIRMATION",
            "owner": "",
            "source_sha256_12": hashlib.sha256((source_text or "").encode("utf-8", errors="replace")).hexdigest()[:12],
            "source_chars": len(source_text or ""),
            "created_by": (created_by or "system")[:80],
            "confirmed_by": "",
            "decision_note": "",
            "due_at": due_at,
            "created": now.isoformat(timespec="seconds"),
            "updated": now.isoformat(timespec="seconds"),
            "urgency_level": str(urgency_level or "unknown")[:30],
            "urgency_score": max(0, int(urgency_score or 0)),
            "extracted_facts_json": json.dumps(safe_extracted_facts, ensure_ascii=False, separators=(",", ":")),
            "policy_matches_json": json.dumps(policy_matches or [], ensure_ascii=False, separators=(",", ":")),
            "source_references_json": json.dumps(source_references or [], ensure_ascii=False, separators=(",", ":")),
            "missing_info_json": json.dumps(missing_info or [], ensure_ascii=False, separators=(",", ":")),
            "policy_confidence": max(0.0, min(1.0, float(policy_confidence or 0.0))),
            "manual_review": 1 if manual_review else 0,
        }
        with self._conn() as conn:
            columns = ",".join(record)
            placeholders = ",".join("?" for _ in record)
            conn.execute(f"INSERT INTO incidents({columns}) VALUES({placeholders})", tuple(record.values()))
        return self.get(incident_id) or {}

    def get(self, incident_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM incidents WHERE id=?", ((incident_id or "").strip(),)).fetchone()
        return dict(row) if row else None

    def transition(self, incident_id: str, decision: str, *, actor: str, owner: str = "", note: str = "") -> dict:
        item = self.get(incident_id)
        if not item:
            raise ValueError("未找到该事件编号")
        current = item["workflow_status"]
        target = TRANSITIONS.get(current, {}).get((decision or "").strip())
        if not target:
            raise ValueError(f"事件当前为“{STATUS_LABELS.get(current, current)}”，不能执行“{decision}”")
        if decision == "确认处置" and not owner.strip():
            raise ValueError("确认处置前请填写责任人或责任岗位")
        if decision in {"退回补充", "确认结案"} and not note.strip():
            raise ValueError(f"“{decision}”必须填写说明")

        now = datetime.now().isoformat(timespec="seconds")
        next_owner = owner.strip()[:80] if owner.strip() else item["owner"]
        safe_note = note.strip()[:1000]
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE incidents
                SET workflow_status=?, owner=?, confirmed_by=?, decision_note=?, updated=?
                WHERE id=? AND workflow_status=?
                """,
                (target, next_owner, actor.strip()[:80], safe_note, now, incident_id, current),
            )
            if cur.rowcount != 1:
                raise ValueError("事件状态已变化，请刷新后重试")
            conn.execute(
                "INSERT INTO incident_actions(incident_id,action,from_status,to_status,actor,note,created) VALUES(?,?,?,?,?,?,?)",
                (incident_id, decision, current, target, actor.strip()[:80], safe_note, now),
            )
        return self.get(incident_id) or {}

    def list_recent(self, *, username: str = "", all_users: bool = False, limit: int = 30) -> list[dict]:
        safe_limit = max(1, min(int(limit), 200))
        with self._conn() as conn:
            if all_users:
                rows = conn.execute("SELECT * FROM incidents ORDER BY created DESC LIMIT ?", (safe_limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM incidents WHERE created_by=? ORDER BY created DESC LIMIT ?",
                    ((username or "system")[:80], safe_limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def actions(self, incident_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM incident_actions WHERE incident_id=? ORDER BY id",
                ((incident_id or "").strip(),),
            ).fetchall()
        return [dict(row) for row in rows]
