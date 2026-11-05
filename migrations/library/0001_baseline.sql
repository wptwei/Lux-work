-- file_library/library.db 基线：与 core/pdf_library.py 一致。
CREATE TABLE IF NOT EXISTS documents(
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    tags TEXT NOT NULL,
    owner TEXT NOT NULL,
    visibility TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions(
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    filename TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created TEXT NOT NULL,
    UNIQUE(document_id,version)
);

CREATE TABLE IF NOT EXISTS pages(
    version_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY(version_id,page_number)
);

CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    created TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS versions_doc ON versions(document_id,version);
