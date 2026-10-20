CREATE TABLE skill_executions (
    run_id       TEXT NOT NULL REFERENCES runs (id),
    skill_id     TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('completed', 'needs_approval', 'failed')),
    PRIMARY KEY (run_id, skill_id),
    FOREIGN KEY (skill_id, skill_version) REFERENCES skills (id, version)
);
CREATE INDEX skill_executions_version ON skill_executions (skill_id, skill_version);
