CREATE TABLE voice_requests (
    request_id TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE INDEX voice_requests_expiry ON voice_requests (expires_at);
