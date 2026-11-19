CREATE VIRTUAL TABLE document_chunks_fts USING fts5(
    text, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE INDEX documents_status ON documents (status);

INSERT INTO document_chunks_fts (rowid, text)
SELECT c.rowid, c.text FROM document_chunks c
JOIN documents d ON d.id = c.document_id WHERE d.status = 'active';

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
