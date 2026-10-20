-- Sessões, mensagens, execuções e outbox (especificação §10 e §11).

CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE runs (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions (id),
    status         TEXT NOT NULL CHECK (status IN ('completed', 'needs_approval', 'failed')),
    route_version  TEXT,
    model          TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL,
    ended_at       TEXT NOT NULL
);
CREATE INDEX runs_session ON runs (session_id, started_at);

CREATE TABLE messages (
    id            INTEGER PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions (id),
    run_id        TEXT REFERENCES runs (id),
    role          TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content       TEXT NOT NULL,
    tool_calls    TEXT,
    tool_call_id  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX messages_session ON messages (session_id, id);

CREATE TABLE outbox_events (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    occurred_at     TEXT NOT NULL,
    run_id          TEXT REFERENCES runs (id),
    session_id      TEXT REFERENCES sessions (id),
    correlation_id  TEXT,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'leased', 'completed', 'dead_letter')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    lease_until     TEXT,
    retry_at        TEXT,
    worker_id       TEXT,
    last_error      TEXT
);
CREATE INDEX outbox_ready ON outbox_events (status, retry_at);
