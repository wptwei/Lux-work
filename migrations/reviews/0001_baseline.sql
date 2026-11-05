-- reviews.db 基线：与 core/review_queue.py 一致。
CREATE TABLE IF NOT EXISTS reviews(
    id TEXT PRIMARY KEY,
    created TEXT,
    updated TEXT,
    status TEXT,
    region TEXT,
    asset_type TEXT,
    original_name TEXT,
    stored_path TEXT,
    sha256 TEXT,
    risk_level TEXT,
    confidence REAL,
    evidence TEXT,
    reviewer TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
