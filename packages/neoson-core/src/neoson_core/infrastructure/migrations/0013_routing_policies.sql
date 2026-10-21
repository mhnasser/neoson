CREATE TABLE routing_policies (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    approval_order INTEGER NOT NULL UNIQUE
);
CREATE TABLE routing_policy_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL CHECK (action IN ('bootstrap','promote','rollback')),
    version TEXT NOT NULL REFERENCES routing_policies(version),
    previous_revision TEXT NOT NULL,
    evidence_id TEXT,
    evidence_checksum TEXT,
    runtime_hash TEXT,
    review_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE routing_policy_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    version TEXT NOT NULL REFERENCES routing_policies(version),
    event_id TEXT NOT NULL REFERENCES routing_policy_events(id)
);
CREATE TABLE routing_session_bindings (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    version TEXT NOT NULL REFERENCES routing_policies(version),
    alias TEXT NOT NULL,
    model_json TEXT NOT NULL,
    checksum TEXT NOT NULL
);
CREATE INDEX routing_binding_alias ON routing_session_bindings(alias);
