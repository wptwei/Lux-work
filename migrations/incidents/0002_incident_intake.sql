-- 统一事件受理（第一批）：只新增关系表，不修改 0001 基线。
-- 事件主表 incidents 保持原结构；登记状态/版本/匹配状态放在 incident_state。

CREATE TABLE IF NOT EXISTS incident_submissions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    incident_version INTEGER NOT NULL DEFAULT 1,
    conversation_id TEXT NOT NULL DEFAULT '',
    source_channel TEXT NOT NULL,
    source_origin TEXT NOT NULL DEFAULT '',
    source_edited INTEGER NOT NULL DEFAULT 0,
    source_message_id TEXT NOT NULL DEFAULT '',
    operation TEXT NOT NULL DEFAULT 'report',
    text_sha256_12 TEXT NOT NULL,
    text_chars INTEGER NOT NULL,
    text_snapshot TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    client_captured_at TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    UNIQUE(actor, submission_id)
);

CREATE INDEX IF NOT EXISTS idx_incident_submissions_incident
    ON incident_submissions(incident_id);

CREATE TABLE IF NOT EXISTS incident_state(
    incident_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL DEFAULT 1,
    intake_status TEXT NOT NULL DEFAULT 'registered',
    match_status TEXT NOT NULL DEFAULT 'pending',
    source_channel TEXT NOT NULL DEFAULT '',
    last_submission_id TEXT NOT NULL DEFAULT '',
    match_batch_id INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS incident_tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    incident_id TEXT NOT NULL,
    incident_version INTEGER NOT NULL,
    ruleset_version TEXT NOT NULL DEFAULT '',
    matcher_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires TEXT NOT NULL DEFAULT '',
    next_attempt_at TEXT NOT NULL,
    last_error_category TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incident_tasks_ready
    ON incident_tasks(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_incident_tasks_incident
    ON incident_tasks(incident_id);

CREATE TABLE IF NOT EXISTS incident_match_batches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    incident_version INTEGER NOT NULL,
    task_id INTEGER NOT NULL DEFAULT 0,
    ruleset_version TEXT NOT NULL DEFAULT '',
    matcher_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    candidates_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    safe_error_category TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    UNIQUE(incident_id, incident_version, ruleset_version, matcher_version)
);

CREATE TABLE IF NOT EXISTS reward_penalty_links(
    record_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    match_batch_id INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_reward_penalty_links_incident
    ON reward_penalty_links(incident_id);
