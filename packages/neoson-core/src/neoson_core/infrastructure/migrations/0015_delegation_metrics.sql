-- Uso incompleto e análises delegadas por execução: somente contadores, nunca texto.

ALTER TABLE usage_records ADD COLUMN usage_complete INTEGER NOT NULL DEFAULT 1
    CHECK (usage_complete IN (0, 1));

CREATE TABLE delegation_records (
    run_id          TEXT PRIMARY KEY REFERENCES runs (id),
    attempts        INTEGER NOT NULL CHECK (attempts >= 0),
    completed       INTEGER NOT NULL CHECK (completed >= 0),
    failed          INTEGER NOT NULL CHECK (failed >= 0),
    rejected        INTEGER NOT NULL CHECK (rejected >= 0),
    input_tokens    INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens   INTEGER NOT NULL CHECK (output_tokens >= 0),
    cached_tokens   INTEGER NOT NULL CHECK (cached_tokens >= 0),
    usage_complete  INTEGER NOT NULL CHECK (usage_complete IN (0, 1)),
    recorded_at     TEXT NOT NULL
);
