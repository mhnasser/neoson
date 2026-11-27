-- foreign_keys: off
-- Reconstrução preserva IDs, trechos e referências do grafo; o runner confere FKs.
DROP TRIGGER document_chunks_fts_insert;
DROP TRIGGER document_chunks_fts_delete;
DROP TRIGGER document_chunks_fts_update;
DROP TRIGGER documents_fts_status;
CREATE TABLE documents_new (
    id TEXT PRIMARY KEY CHECK (length(id) = 64),
    original_name TEXT NOT NULL,
    extension TEXT NOT NULL CHECK (extension IN ('md','markdown','txt','pdf','png')),
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    chars INTEGER NOT NULL CHECK (chars >= 0),
    title TEXT NOT NULL,
    note_path TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','forgotten')),
    forgotten_at TEXT,
    extraction_state TEXT NOT NULL DEFAULT 'completed'
        CHECK (extraction_state IN ('pending','completed','failed')),
    quality_json TEXT NOT NULL DEFAULT '{}',
    stored_sha256 TEXT CHECK (stored_sha256 IS NULL OR length(stored_sha256)=64)
);
INSERT INTO documents_new (id, original_name, extension, size_bytes, chars, title,
    note_path, ingested_at, status, forgotten_at, extraction_state, quality_json)
SELECT id, original_name, extension, size_bytes, chars, title, note_path,
    ingested_at, status, forgotten_at, extraction_state, quality_json FROM documents;
DROP TABLE documents;
ALTER TABLE documents_new RENAME TO documents;
CREATE INDEX documents_status ON documents(status);
CREATE TABLE document_pages_new (
    document_id TEXT NOT NULL REFERENCES documents(id),
    page INTEGER NOT NULL CHECK (page > 0),
    route TEXT NOT NULL CHECK (route IN ('text','empty','ocr')),
    chars INTEGER NOT NULL CHECK (chars >= 0),
    needs_ocr INTEGER NOT NULL DEFAULT 0 CHECK (needs_ocr IN (0,1)),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 100)),
    warning_code TEXT,
    PRIMARY KEY(document_id, page)
);
INSERT INTO document_pages_new(document_id,page,route,chars,needs_ocr)
SELECT document_id,page,route,chars,needs_ocr FROM document_pages;
DROP TABLE document_pages;
ALTER TABLE document_pages_new RENAME TO document_pages;
CREATE TRIGGER document_chunks_fts_insert AFTER INSERT ON document_chunks BEGIN
    INSERT INTO document_chunks_fts (rowid, text)
    SELECT NEW.rowid, NEW.text
    WHERE EXISTS (SELECT 1 FROM documents WHERE id = NEW.document_id AND status = 'active');
END;

CREATE TRIGGER document_chunks_fts_delete AFTER DELETE ON document_chunks BEGIN
    DELETE FROM document_chunks_fts WHERE rowid = OLD.rowid;
END;

CREATE TRIGGER document_chunks_fts_update AFTER UPDATE ON document_chunks BEGIN
    DELETE FROM document_chunks_fts WHERE rowid = OLD.rowid;
    INSERT INTO document_chunks_fts (rowid, text)
    SELECT NEW.rowid, NEW.text
    WHERE EXISTS (SELECT 1 FROM documents WHERE id = NEW.document_id AND status = 'active');
END;

CREATE TRIGGER documents_fts_status AFTER UPDATE OF status ON documents
WHEN NEW.status <> OLD.status BEGIN
    DELETE FROM document_chunks_fts
    WHERE rowid IN (SELECT rowid FROM document_chunks WHERE document_id = NEW.id);
    INSERT INTO document_chunks_fts (rowid, text)
    SELECT rowid, text FROM document_chunks WHERE document_id = NEW.id AND NEW.status = 'active';
END;
