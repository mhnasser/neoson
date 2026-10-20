CREATE TABLE curation_jobs (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES outbox_events(id),
    skill_id TEXT NOT NULL,
    base_version TEXT NOT NULL,
    base_checksum TEXT NOT NULL,
    run_id TEXT REFERENCES runs(id),
    evaluation_id TEXT REFERENCES skill_evaluations(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','leased','completed','failed','stale')),
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at TEXT,
    lease_until TEXT,
    claim_id TEXT,
    last_error TEXT,
    proposal_id TEXT REFERENCES skill_proposals(id),
    CHECK ((run_id IS NULL) != (evaluation_id IS NULL)),
    UNIQUE(event_id, skill_id),
    FOREIGN KEY(skill_id, base_version) REFERENCES skills(id, version)
);
CREATE UNIQUE INDEX curation_active ON curation_jobs(skill_id)
    WHERE status IN ('pending','leased');
CREATE TABLE curation_attempts (
    claim_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES curation_jobs(id),
    started_at TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    reserved_cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    cost_usd REAL,
    error_code TEXT
);
