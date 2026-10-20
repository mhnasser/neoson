-- Cópias aprovadas são imutáveis; apenas o estado de ativação muda (ADR-007).
CREATE TABLE skills (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL,
    version     TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    files_json  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active', 'approved', 'retired')),
    approved_at TEXT NOT NULL,
    UNIQUE (id, version)
);

CREATE UNIQUE INDEX skills_active ON skills (id) WHERE status = 'active';
