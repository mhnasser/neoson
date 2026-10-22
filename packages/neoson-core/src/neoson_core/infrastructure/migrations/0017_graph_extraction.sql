-- Extração de fatos para o grafo: um job por fonte e tipo; tentativas com reserva.
-- O texto da memória não é copiado aqui; o executor lê a fonte na hora.

CREATE TABLE graph_jobs (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('extract', 'retract')),
    source_id       TEXT NOT NULL,
    memory_fact_id  INTEGER REFERENCES memory_facts (id),
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'leased', 'completed', 'failed', 'stale')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    retry_at        TEXT,
    lease_until     TEXT,
    claim_id        TEXT,
    last_error      TEXT,
    facts_added     INTEGER,
    UNIQUE (source_id, kind)
);
CREATE INDEX graph_jobs_ready ON graph_jobs (status, created_at);

CREATE TABLE graph_attempts (
    claim_id           TEXT PRIMARY KEY,
    job_id             TEXT NOT NULL REFERENCES graph_jobs (id),
    started_at         TEXT NOT NULL,
    reserved_tokens    INTEGER NOT NULL,
    reserved_cost_usd  REAL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cached_tokens      INTEGER,
    cost_usd           REAL,
    error_code         TEXT,
    discarded_json     TEXT
);
CREATE INDEX graph_attempts_day ON graph_attempts (started_at);
