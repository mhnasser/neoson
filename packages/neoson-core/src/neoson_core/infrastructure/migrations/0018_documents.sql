-- Documentos ingeridos (§9.3, D11): o original fica fora do banco, pelo hash.
-- O texto normalizado fica em trechos, para a extração de fatos com fonte.

CREATE TABLE documents (
    id             TEXT PRIMARY KEY CHECK (length(id) = 64),
    original_name  TEXT NOT NULL,
    extension      TEXT NOT NULL CHECK (extension IN ('md', 'markdown', 'txt')),
    size_bytes     INTEGER NOT NULL CHECK (size_bytes > 0),
    chars          INTEGER NOT NULL CHECK (chars > 0),
    title          TEXT NOT NULL,
    note_path      TEXT NOT NULL,
    ingested_at    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'forgotten')),
    forgotten_at   TEXT
);

CREATE TABLE document_chunks (
    document_id  TEXT NOT NULL REFERENCES documents (id),
    n            INTEGER NOT NULL CHECK (n >= 0),
    text         TEXT NOT NULL,
    PRIMARY KEY (document_id, n)
);

ALTER TABLE graph_jobs ADD COLUMN document_id TEXT REFERENCES documents (id);
ALTER TABLE graph_jobs ADD COLUMN chunk INTEGER;
