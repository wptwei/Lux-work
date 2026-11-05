-- incidents.db 基线：core/incident_store.py 与 core/reward_penalty_store.py 共用此库。
CREATE TABLE IF NOT EXISTS incidents(
    id TEXT PRIMARY KEY,
    task_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    region TEXT NOT NULL,
    reported_status TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    risk_score INTEGER NOT NULL,
    workflow_status TEXT NOT NULL,
    owner TEXT DEFAULT '',
    source_sha256_12 TEXT NOT NULL,
    source_chars INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    confirmed_by TEXT DEFAULT '',
    decision_note TEXT DEFAULT '',
    due_at TEXT DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    urgency_level TEXT DEFAULT 'unknown',
    urgency_score INTEGER DEFAULT 0,
    extracted_facts_json TEXT DEFAULT '{}',
    policy_matches_json TEXT DEFAULT '[]',
    source_references_json TEXT DEFAULT '[]',
    missing_info_json TEXT DEFAULT '[]',
    policy_confidence REAL DEFAULT 0,
    manual_review INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS incident_actions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT DEFAULT '',
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reward_penalty_records (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    created_by TEXT NOT NULL,
    event_description TEXT NOT NULL,
    match_status TEXT NOT NULL,
    top_document TEXT DEFAULT '',
    top_clause TEXT DEFAULT '',
    result_json TEXT NOT NULL
);
