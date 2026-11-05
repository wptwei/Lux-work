"""本地人工审核队列。审核原件和元数据均不离开本机。"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from core.paths import data_path
from core.sqlite_util import connect_db



class ReviewQueue:
    def __init__(self, db_path: str | Path = None, file_dir: str | Path = None):
        self.db_path = Path(db_path) if db_path else data_path("reviews.db")
        self.file_dir = Path(file_dir) if file_dir else data_path("review_files")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        return connect_db(self.db_path)

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reviews(
                    id TEXT PRIMARY KEY, created TEXT, updated TEXT, status TEXT,
                    region TEXT, asset_type TEXT, original_name TEXT, stored_path TEXT,
                    sha256 TEXT, risk_level TEXT, confidence REAL, evidence TEXT,
                    reviewer TEXT DEFAULT '', note TEXT DEFAULT ''
                )
            """)

    def submit(self, file_path: str | Path, *, region: str, asset_type: str, risk_level: str,
               confidence: float = 0.0, evidence: list[str] | tuple[str, ...] = ()) -> str:
        source = Path(file_path)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with self._conn() as conn:
            old = conn.execute(
                "SELECT id FROM reviews WHERE sha256=? AND region=? AND status='待审核' ORDER BY created DESC LIMIT 1",
                (digest, region),
            ).fetchone()
            if old:
                return old["id"]
        review_id = f"RV-{uuid.uuid4().hex[:8].upper()}"
        stored = self.file_dir / f"{review_id}{source.suffix.lower()}"
        shutil.copy2(source, stored)
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, now, now, "待审核", region, asset_type, source.name, str(stored), digest,
                 risk_level, float(confidence), json.dumps(list(evidence), ensure_ascii=False), "", ""),
            )
        return review_id

    def list_recent(self, status: str = "全部", limit: int = 100) -> list[dict]:
        query = "SELECT * FROM reviews"
        params: list[object] = []
        if status != "全部":
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get(self, review_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id.strip(),)).fetchone()
        return dict(row) if row else None

    def decide(self, review_id: str, decision: str, reviewer: str, note: str = "") -> bool:
        if decision not in {"已批准", "已拒绝"}:
            raise ValueError("审核结论无效")
        if not reviewer.strip():
            raise ValueError("请填写审核人")
        with self._conn() as conn:
            result = conn.execute(
                "UPDATE reviews SET status=?, reviewer=?, note=?, updated=? WHERE id=? AND status='待审核'",
                (decision, reviewer.strip()[:80], note.strip()[:500], datetime.now().isoformat(timespec="seconds"), review_id.strip()),
            )
            return result.rowcount > 0
