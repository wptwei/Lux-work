"""本地任务台账。

只保存任务元数据、摘要哈希、状态和用户反馈，不保存业务输入或模型输出原文。
旧版 tasks.db 会在启动时原地补列，不需要手工迁移。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from core.paths import data_path
from core.sqlite_util import connect_db


DB = data_path("tasks.db")
VALID_STATUSES = {
    "CREATED", "RUNNING", "AWAITING_CONFIRMATION", "COMPLETED",
    "REJECTED", "FAILED", "CANCELLED",
}
VALID_FEEDBACK = {"可用", "需修改", "不可用"}


class TaskQueue:
    def __init__(self, db: Path | str = DB):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        return connect_db(self.db)

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    task_type TEXT,
                    payload TEXT,
                    status TEXT,
                    result TEXT DEFAULT '',
                    retry INTEGER DEFAULT 0,
                    created TEXT
                )
                """
            )
            existing = {row[1] for row in c.execute("PRAGMA table_info(tasks)").fetchall()}
            migrations = {
                "title": "TEXT DEFAULT ''",
                "category": "TEXT DEFAULT '通用'",
                "skill": "TEXT DEFAULT '普通对话'",
                "input_chars": "INTEGER DEFAULT 0",
                "duration_ms": "INTEGER DEFAULT 0",
                "feedback": "TEXT DEFAULT ''",
                "feedback_notes": "TEXT DEFAULT ''",
                "actual_minutes": "INTEGER DEFAULT 0",
                "updated": "TEXT DEFAULT ''",
                "created_by": "TEXT DEFAULT ''",
            }
            for column, definition in migrations.items():
                if column not in existing:
                    c.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")

    def create(
        self,
        task_type: str,
        payload: dict | str = "",
        *,
        title: str = "",
        category: str = "通用",
        skill: str = "普通对话",
        input_chars: int = 0,
        created_by: str = "",
    ) -> str:
        """创建任务。payload 只能传元数据，调用方不得传业务原文。"""
        task_id = str(uuid.uuid4())
        now = datetime.now().isoformat(timespec="seconds")
        payload_text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload)
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO tasks(
                    id, task_type, payload, status, result, retry, created,
                    title, category, skill, input_chars, duration_ms, updated, created_by
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id, task_type[:40], payload_text[:2000], "CREATED", "", 0, now,
                    title[:120], category[:40], skill[:120], max(0, int(input_chars)), 0, now,
                    created_by[:80],
                ),
            )
        return task_id

    def update(
        self,
        task_id: str,
        status: str,
        result: dict | str = "",
        *,
        duration_ms: int | None = None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"无效任务状态：{status}")
        result_text = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
        fields = ["status=?", "result=?", "updated=?"]
        values: list[object] = [status, result_text[:2000], datetime.now().isoformat(timespec="seconds")]
        if duration_ms is not None:
            fields.append("duration_ms=?")
            values.append(max(0, int(duration_ms)))
        values.append(task_id)
        with self._conn() as c:
            c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", values)

    def add_feedback(
        self,
        task_id: str,
        feedback: str,
        actual_minutes: int = 0,
        notes: str = "",
    ) -> bool:
        if feedback not in VALID_FEEDBACK:
            raise ValueError("请选择有效的反馈结论")
        if not task_id:
            return False
        with self._conn() as c:
            cur = c.execute(
                """
                UPDATE tasks
                SET feedback=?, feedback_notes=?, actual_minutes=?, updated=?
                WHERE id=?
                """,
                (
                    feedback,
                    notes.strip()[:500],
                    max(0, min(int(actual_minutes or 0), 1440)),
                    datetime.now().isoformat(timespec="seconds"),
                    task_id,
                ),
            )
            return cur.rowcount > 0

    def get(self, task_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_recent(self, limit: int = 30) -> list[dict]:
        safe_limit = max(1, min(int(limit), 200))
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT id, title, task_type, category, skill, status, input_chars,
                       duration_ms, feedback, actual_minutes, created, updated, created_by
                FROM tasks ORDER BY created DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_for_user(
        self,
        username: str,
        *,
        status: str = "",
        task_type: str = "",
        category: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """按发起人查询任务元数据（不含原文），支持筛选与分页。"""
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))
        where = ["created_by=?"]
        params: list[object] = [username]
        if status:
            where.append("status=?")
            params.append(status)
        if task_type:
            where.append("task_type=?")
            params.append(task_type)
        if category:
            where.append("category=?")
            params.append(category)
        params += [safe_limit, safe_offset]
        with self._conn() as c:
            rows = c.execute(
                f"""
                SELECT id, title, task_type, category, skill, status, input_chars,
                       duration_ms, feedback, actual_minutes, created, updated, created_by
                FROM tasks WHERE {' AND '.join(where)}
                ORDER BY created DESC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_for_user(
        self,
        username: str,
        *,
        status: str = "",
        task_type: str = "",
        category: str = "",
    ) -> int:
        """按发起人统计任务数（供分页）。"""
        where = ["created_by=?"]
        params: list[object] = [username]
        if status:
            where.append("status=?")
            params.append(status)
        if task_type:
            where.append("task_type=?")
            params.append(task_type)
        if category:
            where.append("category=?")
            params.append(category)
        with self._conn() as c:
            row = c.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {' AND '.join(where)}",
                params,
            ).fetchone()
        return int(row[0])

    def pending(self) -> list[tuple]:
        with self._conn() as c:
            return c.execute(
                "SELECT id,task_type,payload,status FROM tasks WHERE status NOT IN ('COMPLETED','REJECTED','FAILED','CANCELLED')"
            ).fetchall()

    def summary(self, username: str = "") -> dict:
        where = " WHERE created_by=?" if username else ""
        params: tuple = (username,) if username else ()
        with self._conn() as c:
            row = c.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='RUNNING' THEN 1 ELSE 0 END) AS running,
                       SUM(CASE WHEN status='AWAITING_CONFIRMATION' THEN 1 ELSE 0 END) AS awaiting_confirmation,
                       SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN feedback!='' THEN 1 ELSE 0 END) AS feedback_count
                FROM tasks{where}
                """,
                params,
            ).fetchone()
        return {key: (row[key] or 0) for key in row.keys()}
