-- Grafo de conhecimento (§9.2): entidades com ID estável e fatos com proveniência.
-- Nomes podem ser pessoais: ficam só neste banco, fora de eventos e do vault.

CREATE TABLE graph_entities (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('Person', 'Project', 'Document',
                    'Concept', 'Decision', 'Task', 'Event', 'Skill', 'Source')),
    name        TEXT NOT NULL,
    name_key    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (kind, name_key)
);
CREATE INDEX graph_entities_name ON graph_entities (name_key);

CREATE TABLE graph_facts (
    id           TEXT PRIMARY KEY,
    subject_id   TEXT NOT NULL REFERENCES graph_entities (id),
    relation     TEXT NOT NULL CHECK (relation IN ('WORKS_ON', 'MENTIONS', 'DECIDED',
                     'DEPENDS_ON', 'DERIVED_FROM', 'RELATES_TO', 'USES_SKILL')),
    object_id    TEXT NOT NULL REFERENCES graph_entities (id),
    source_id    TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    confidence   REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    valid_from   TEXT,
    valid_to     TEXT,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'active', 'retracted')),
    reason       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (subject_id <> object_id)
);
CREATE INDEX graph_facts_subject ON graph_facts (subject_id, status);
CREATE INDEX graph_facts_object ON graph_facts (object_id, status);
CREATE INDEX graph_facts_source ON graph_facts (source_id);
