-- Sessão atual de cada conversa externa (por exemplo, um chat do Telegram).

CREATE TABLE channel_sessions (
    channel      TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    session_id   TEXT NOT NULL REFERENCES sessions (id),
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (channel, external_id)
);
