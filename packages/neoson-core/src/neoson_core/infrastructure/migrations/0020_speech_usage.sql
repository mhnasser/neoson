CREATE TABLE speech_usage_records (
    run_id TEXT PRIMARY KEY REFERENCES runs (id),
    stt_provider TEXT NOT NULL,
    tts_provider TEXT NOT NULL,
    stt_seconds REAL NOT NULL CHECK (stt_seconds >= 0),
    tts_characters INTEGER NOT NULL CHECK (tts_characters >= 0),
    cost_usd REAL CHECK (cost_usd >= 0),
    usage_complete INTEGER NOT NULL CHECK (usage_complete IN (0, 1)),
    recorded_at TEXT NOT NULL
);
