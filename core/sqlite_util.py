"""SQLite 连接样板：WAL + busy_timeout + Row 工厂，退出时提交并关闭。

sqlite3 连接自带的 `with` 只负责提交事务、不关闭连接，直接用会在 Windows 上
累积句柄并锁住库文件，所以各台账统一走这里。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect_db(path: str | Path):
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
        conn.commit()
    finally:
        conn.close()
