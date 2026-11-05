-- deliverables.db 基线：与 core/deliverable_store.py 一致。
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
);
