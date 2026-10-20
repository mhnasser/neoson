-- Candidatos privados e imutáveis; não fazem parte do catálogo aprovado.
CREATE TABLE skill_proposals (
    id                TEXT PRIMARY KEY,
    request_id        TEXT NOT NULL UNIQUE,
    skill_id          TEXT NOT NULL,
    base_version      TEXT NOT NULL,
    base_checksum     TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    candidate_checksum TEXT NOT NULL,
    files_json        TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (
        status IN ('pending', 'evaluated', 'approved', 'rejected', 'stale')
    ),
    created_at        TEXT NOT NULL,
    UNIQUE (skill_id, candidate_version),
    FOREIGN KEY (skill_id, base_version) REFERENCES skills (id, version)
);

CREATE TABLE skill_proposal_runs (
    proposal_id TEXT NOT NULL REFERENCES skill_proposals (id),
    run_id      TEXT NOT NULL REFERENCES runs (id),
    PRIMARY KEY (proposal_id, run_id)
);

CREATE TABLE skill_proposal_evaluations (
    proposal_id  TEXT NOT NULL REFERENCES skill_proposals (id),
    evaluation_id TEXT NOT NULL REFERENCES skill_evaluations (id),
    PRIMARY KEY (proposal_id, evaluation_id)
);
