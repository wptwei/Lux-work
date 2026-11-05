-- 事件版本账本：补充/更正追加版本，旧版本标记 superseded。
-- 只新增关系表，不修改 0001/0002 已执行的表结构。
CREATE TABLE IF NOT EXISTS incident_versions(
    incident_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    submission_id TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'report',
    source_channel TEXT NOT NULL DEFAULT '',
    source_edited INTEGER NOT NULL DEFAULT 0,
    text_sha256_12 TEXT NOT NULL,
    text_chars INTEGER NOT NULL,
    created TEXT NOT NULL,
    superseded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(incident_id, version)
);

CREATE INDEX IF NOT EXISTS idx_incident_versions_submission
    ON incident_versions(incident_id, submission_id);
