-- Busca textual no histórico (§11): índice FTS5 do conteúdo das mensagens do
-- usuário e do assistente, sem diferenciar acentos. Mensagens antigas entram aqui;
-- as novas, pelos triggers.

CREATE VIRTUAL TABLE messages_fts USING fts5 (
    content,
    content = 'messages',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

INSERT INTO messages_fts (rowid, content)
    SELECT id, content FROM messages
    WHERE role IN ('user', 'assistant') AND content <> '';

CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages
WHEN new.role IN ('user', 'assistant') AND new.content <> ''
BEGIN
    INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages
WHEN old.role IN ('user', 'assistant') AND old.content <> ''
BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;
