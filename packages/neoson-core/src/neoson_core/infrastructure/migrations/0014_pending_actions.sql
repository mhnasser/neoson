-- Autorizações concretas: reserva persistida antes de qualquer efeito.
CREATE TABLE pending_actions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    owner TEXT NOT NULL,
    payload TEXT NOT NULL,
    checksum TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN
        ('pending','approved','denied','expired','executing','completed','unknown','invalid')),
    decision TEXT CHECK (decision IN ('approve','deny')),
    updated_at TEXT NOT NULL
);
CREATE INDEX pending_actions_run_owner ON pending_actions(run_id,owner);
