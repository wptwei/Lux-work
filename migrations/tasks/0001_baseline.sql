-- tasks.db 基线：与 core/task_queue.py 的完整列一致（含历史 ALTER 追加的列）。
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task_type TEXT,
    payload TEXT,
    status TEXT,
    result TEXT DEFAULT '',
    retry INTEGER DEFAULT 0,
    created TEXT,
    title TEXT DEFAULT '',
    category TEXT DEFAULT '通用',
    skill TEXT DEFAULT '普通对话',
    input_chars INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    feedback TEXT DEFAULT '',
    feedback_notes TEXT DEFAULT '',
    actual_minutes INTEGER DEFAULT 0,
    updated TEXT DEFAULT '',
    created_by TEXT DEFAULT ''
);
