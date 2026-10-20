CREATE TABLE skill_evaluations (
    id               TEXT PRIMARY KEY,
    skill_id         TEXT NOT NULL,
    skill_version    TEXT NOT NULL,
    checksum         TEXT NOT NULL,
    evaluated_at     TEXT NOT NULL,
    total_cases      INTEGER NOT NULL,
    passed_cases     INTEGER NOT NULL,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,
    cost_usd         REAL,
    duration_seconds REAL NOT NULL,
    cases_json       TEXT NOT NULL,
    FOREIGN KEY (skill_id, skill_version) REFERENCES skills (id, version)
);
CREATE INDEX skill_evaluations_version
    ON skill_evaluations (skill_id, skill_version, evaluated_at);
