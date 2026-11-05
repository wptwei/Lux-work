-- prompts.db 基线：与 core/db.py 的建表语句保持一致。
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT DEFAULT '通用',
    author TEXT DEFAULT '',
    use_count INTEGER DEFAULT 0,
    created_at TEXT
);

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
);
