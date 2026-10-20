-- Memória curada (§9.1): fatos e preferências com a origem. A fonte é texto livre
-- (sem chave estrangeira): o fato é salvo durante a execução, antes de o run existir.

CREATE TABLE memory_facts (
    id                 INTEGER PRIMARY KEY,
    namespace          TEXT NOT NULL CHECK (namespace IN ('user', 'neoson')),
    content            TEXT NOT NULL,
    source_session_id  TEXT,
    source_run_id      TEXT,
    created_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'forgotten')),
    forgotten_at       TEXT
);
CREATE INDEX memory_facts_active ON memory_facts (status, created_at);
