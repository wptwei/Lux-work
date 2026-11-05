"""奖惩匹配专用后台记录，写入现有 data/incidents.db 的独立表。"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from core.paths import data_path
from core.sqlite_util import connect_db

DB = data_path("incidents.db")


class RewardPenaltyStore:
    def __init__(self, path: str | Path = DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS reward_penalty_records (
                id TEXT PRIMARY KEY, created TEXT NOT NULL, created_by TEXT NOT NULL,
                event_description TEXT NOT NULL, match_status TEXT NOT NULL,
                top_document TEXT DEFAULT '', top_clause TEXT DEFAULT '',
                result_json TEXT NOT NULL)""")
            # 关联表由统一受理创建；独立使用本类时也保证可用。
            db.execute("""CREATE TABLE IF NOT EXISTS reward_penalty_links (
                record_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                match_batch_id INTEGER NOT NULL DEFAULT 0)""")

    def connect(self):
        return connect_db(self.path)

    def create(
        self, *, created_by: str, event_description: str, result: dict,
        incident_id: str = "", match_batch_id: int = 0,
    ) -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        code = f"RP-{datetime.now():%Y%m%d}-{uuid4().hex[:6].upper()}"
        first = (result.get("matches") or [{}])[0]
        row = (code,now,(created_by or "system")[:80],event_description[:1500],result["status"],
               first.get("document_code",""),first.get("clause_no",""),json.dumps(result,ensure_ascii=False))
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO reward_penalty_records(id,created,created_by,event_description,"
                "match_status,top_document,top_clause,result_json) VALUES (?,?,?,?,?,?,?,?)", row)
            if incident_id:
                conn.execute(
                    "INSERT OR REPLACE INTO reward_penalty_links(record_id,incident_id,match_batch_id) "
                    "VALUES (?,?,?)", (code, incident_id[:40], int(match_batch_id or 0)))
        return {"id":code,"created":now,"incident_id":(incident_id or "")}

    def upsert_for_incident(
        self, *, incident_id: str, match_batch_id: int, created_by: str,
        event_description: str, result: dict, incident_version: int | None = None,
    ) -> dict:
        """按事件维护一条可追溯的匹配记录；新版本匹配覆盖展示副本，历史留在事件批次表。

        传入 incident_version 时，在同一写事务内校验事件当前版本；迟到的旧版本回调
        不能把已更新的正文或奖惩结果退回旧值。
        """
        first = (result.get("matches") or [{}])[0]
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            if incident_version is not None:
                state = conn.execute(
                    "SELECT current_version FROM incident_state WHERE incident_id=?",
                    (incident_id[:40],),
                ).fetchone()
                if state is not None and int(state["current_version"] or 1) != int(incident_version):
                    return {"id": "", "created": now, "incident_id": incident_id, "skipped": True}
            row = conn.execute(
                "SELECT r.id FROM reward_penalty_records r "
                "JOIN reward_penalty_links l ON l.record_id=r.id "
                "WHERE l.incident_id=? ORDER BY r.created DESC LIMIT 1",
                (incident_id[:40],),
            ).fetchone()
            values = (
                (created_by or "system")[:80], event_description[:1500],
                result.get("status", ""), first.get("document_code", ""),
                first.get("clause_no", ""), json.dumps(result, ensure_ascii=False),
            )
            if row:
                code = row["id"]
                conn.execute(
                    "UPDATE reward_penalty_records SET created=?, created_by=?, event_description=?, "
                    "match_status=?, top_document=?, top_clause=?, result_json=? WHERE id=?",
                    (now, *values, code),
                )
                conn.execute(
                    "UPDATE reward_penalty_links SET match_batch_id=? WHERE record_id=?",
                    (int(match_batch_id or 0), code),
                )
                return {"id": code, "created": now, "incident_id": incident_id}
            code = f"RP-{datetime.now():%Y%m%d}-{uuid4().hex[:6].upper()}"
            conn.execute(
                "INSERT INTO reward_penalty_records(id,created,created_by,event_description,"
                "match_status,top_document,top_clause,result_json) VALUES (?,?,?,?,?,?,?,?)",
                (code, now, *values),
            )
            conn.execute(
                "INSERT INTO reward_penalty_links(record_id,incident_id,match_batch_id) VALUES (?,?,?)",
                (code, incident_id[:40], int(match_batch_id or 0)),
            )
        return {"id": code, "created": now, "incident_id": incident_id}

    def find_by_incident(self, incident_id: str) -> dict | None:
        if not incident_id:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT r.*, l.incident_id FROM reward_penalty_records r "
                "JOIN reward_penalty_links l ON l.record_id=r.id "
                "WHERE l.incident_id=? ORDER BY r.created DESC LIMIT 1",
                (incident_id[:40],),
            ).fetchone()
        return dict(row) if row else None

    def list_recent(self, *, username: str, all_users: bool=False, limit:int=25) -> list[dict]:
        base = ("SELECT r.*, COALESCE(l.incident_id,'') AS incident_id "
                "FROM reward_penalty_records r "
                "LEFT JOIN reward_penalty_links l ON l.record_id=r.id ")
        with self.connect() as conn:
            if all_users:
                rows=conn.execute(base+"ORDER BY r.created DESC LIMIT ?",(limit,)).fetchall()
            else:
                rows=conn.execute(base+"WHERE r.created_by=? ORDER BY r.created DESC LIMIT ?",(username,limit)).fetchall()
        return [dict(r) for r in rows]
