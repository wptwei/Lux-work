"""
部门共用提示词库 —— SQLite 本地数据库（零依赖）。
对应主管要求：部门共用提示词库，按岗位归类，一人写好全员能用，不发群里沉底。
数据库文件：data/prompts.db（在当服务器的电脑上，浏览器访问者共享）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.paths import data_path
from core.sqlite_util import connect_db

DB_PATH = data_path("prompts.db")

# 岗位分类（可按需扩充）
CATEGORIES = ["通用", "门禁岗", "巡逻岗", "监控岗", "NPI管控", "管理岗"]

# 首次建库时预置的模板
SEED_PROMPTS = [
    ("写培训通知", "通用",
     "请帮我写一份【主题】培训通知，语气正式，300字左右，"
     "包含时间地点、参训人员、培训内容、相关要求。"),
    ("整理巡检记录", "巡逻岗",
     "请把以下巡检记录整理成条目清晰、按时间排序的文字：\n"),
    ("写整改通知", "通用",
     "请帮我写一份整改通知。事由：【】；要求：【】；限期：【】。"),
    ("润色公文", "通用",
     "请把以下文字润色得更正式、通顺，保持原意：\n"),
    ("H区周报素材整理", "管理岗",
     "请把以下本周零散素材整理成周报可用的条目：重点工作按M/D日格式"
     "（时间、人物、事件、结果），数据用×为单位，分本周工作/异常/下周计划：\n"),
]


class PromptDB:
    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        return connect_db(self.db_path)

    def _init_db(self):
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS prompts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    category TEXT DEFAULT '通用',
                    author TEXT DEFAULT '',
                    use_count INTEGER DEFAULT 0,
                    created_at TEXT
                )
                """
            )
            # 调用记录表（工时看板数据源）
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT,
                    user TEXT DEFAULT '匿名',
                    skill TEXT DEFAULT '普通对话',
                    category TEXT DEFAULT '通用',
                    level TEXT,
                    status TEXT,
                    masked_count INTEGER DEFAULT 0,
                    saving_minutes INTEGER DEFAULT 0
                )
                """
            )
            # 空库则预置
            n = c.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
            if n == 0:
                now = datetime.now().isoformat(timespec="seconds")
                c.executemany(
                    "INSERT INTO prompts(title, content, category, author, use_count, created_at)"
                    " VALUES (?,?,?,?,0,?)",
                    [(t, ct, cat, "系统预置", now) for t, cat, ct in SEED_PROMPTS],
                )

    def add(self, title: str, content: str, category: str = "通用", author: str = "") -> int:
        title, content = title.strip(), content.strip()
        if not title or not content:
            raise ValueError("标题和内容不能为空")
        if category not in CATEGORIES:
            raise ValueError("无效的岗位分类")
        if len(title) > 80 or len(content) > 20000:
            raise ValueError("标题或提示词内容过长")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO prompts(title, content, category, author, use_count, created_at)"
                " VALUES (?,?,?,?,0,?)",
                (title, content, category, author, datetime.now().isoformat(timespec="seconds")),
            )
            return cur.lastrowid

    def list(self, category: str = "全部") -> list[dict]:
        sql = "SELECT id, title, content, category, author, use_count FROM prompts"
        params = ()
        if category and category != "全部":
            sql += " WHERE category=?"
            params = (category,)
        sql += " ORDER BY category, use_count DESC, id DESC"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [
            {"id": r[0], "title": r[1], "content": r[2], "category": r[3],
             "author": r[4], "use_count": r[5]}
            for r in rows
        ]

    def get(self, prompt_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT id, title, content, category, author, use_count FROM prompts WHERE id=?",
                (prompt_id,),
            ).fetchone()
        if not r:
            return None
        return {"id": r[0], "title": r[1], "content": r[2], "category": r[3],
                "author": r[4], "use_count": r[5]}

    def use(self, prompt_id: int):
        """调用一次，使用次数+1。"""
        with self._conn() as c:
            c.execute("UPDATE prompts SET use_count=use_count+1 WHERE id=?", (prompt_id,))

    def delete(self, prompt_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))

    # ---------- 工时记录 ----------
    def log_usage(self, skill: str, category: str, level: str, status: str,
                  masked_count: int = 0, saving_minutes: int = 0, user: str = "匿名"):
        with self._conn() as c:
            c.execute(
                "INSERT INTO usage_log(ts,user,skill,category,level,status,masked_count,saving_minutes)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), user, skill, category,
                 level, status, masked_count, saving_minutes),
            )

    def usage_summary(self, days: int = 7) -> dict:
        """汇总近 N 天的用量与节省工时。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*),"
                "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END),"
                "SUM(CASE WHEN level='desensitize' THEN 1 ELSE 0 END),"
                "COALESCE(SUM(saving_minutes),0) "
                "FROM usage_log WHERE ts >= datetime('now', ?)",
                (f"-{days} days",),
            ).fetchone()
            by_skill = c.execute(
                "SELECT skill, COUNT(*) AS n, COALESCE(SUM(saving_minutes),0) AS mins "
                "FROM usage_log WHERE ts >= datetime('now', ?) AND status='ok' "
                "GROUP BY skill ORDER BY n DESC",
                (f"-{days} days",),
            ).fetchall()
            by_category = c.execute(
                "SELECT category, COUNT(*) AS n, COALESCE(SUM(saving_minutes),0) AS mins "
                "FROM usage_log WHERE ts >= datetime('now', ?) AND status='ok' "
                "GROUP BY category ORDER BY n DESC",
                (f"-{days} days",),
            ).fetchall()
            by_day = c.execute(
                "SELECT substr(ts,1,10) AS d, COUNT(*) AS n, COALESCE(SUM(saving_minutes),0) AS mins "
                "FROM usage_log WHERE ts >= datetime('now', ?) "
                "GROUP BY d ORDER BY d",
                (f"-{days} days",),
            ).fetchall()
        return {
            "total": row[0], "ok": row[1], "rejected": row[2],
            "desensitized": row[3], "saved_minutes": row[4],
            "by_skill": by_skill, "by_category": by_category, "by_day": by_day,
        }


if __name__ == "__main__":
    db = PromptDB()
    print(f"数据库：{DB_PATH}")
    for p in db.list():
        print(f"[{p['id']}] ({p['category']}) {p['title']} ×{p['use_count']}")
