"""交付物版本台账：只保存文件元数据，不保存业务输入原文。"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from core.paths import data_path
from core.sqlite_util import connect_db


DB = data_path("deliverables.db")


class DeliverableStore:
    def __init__(self, db: str | Path = DB):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        return connect_db(self.db)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deliverables(
                    id TEXT PRIMARY KEY,
                    logical_name TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    file_sha256_12 TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created TEXT NOT NULL
                )
                """
            )

    def register(
        self,
        file_path: str | Path,
        *,
        kind: str,
        created_by: str,
        logical_name: str,
    ) -> dict:
        path = Path(file_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        safe_kind = (kind or "文件").strip()[:30]
        safe_user = (created_by or "system").strip()[:40]
        safe_name = (logical_name or path.stem).strip()[:100]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM deliverables "
                "WHERE created_by=? AND kind=? AND logical_name=?",
                (safe_user, safe_kind, safe_name),
            ).fetchone()
            version = int(row[0]) + 1
            record = {
                "id": f"DL-{uuid.uuid4().hex[:10].upper()}",
                "logical_name": safe_name,
                "file_name": path.name,
                "file_path": str(path),
                "kind": safe_kind,
                "version": version,
                "size_bytes": path.stat().st_size,
                "file_sha256_12": digest,
                "created_by": safe_user,
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            conn.execute(
                "INSERT INTO deliverables VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(record.values()),
            )
        return record

    def list_for_user(self, username: str, limit: int = 8) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM deliverables WHERE created_by=? ORDER BY created DESC LIMIT ?",
                ((username or "system").strip()[:40], max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]
