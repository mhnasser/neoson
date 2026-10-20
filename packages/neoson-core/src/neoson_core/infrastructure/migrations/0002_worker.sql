-- Idempotência dos handlers do worker e registro de uso por execução (§10.2, §11).

CREATE TABLE worker_effects (
    event_id      TEXT NOT NULL REFERENCES outbox_events (id),
    handler       TEXT NOT NULL,
    completed_at  TEXT NOT NULL,
    PRIMARY KEY (event_id, handler)
);

CREATE TABLE usage_records (
    run_id         TEXT PRIMARY KEY REFERENCES runs (id),
    model          TEXT,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    cached_tokens  INTEGER NOT NULL,
    cost_usd       REAL,
    recorded_at    TEXT NOT NULL
);
