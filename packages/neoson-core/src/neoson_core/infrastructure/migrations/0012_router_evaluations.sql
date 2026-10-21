-- Evidência do laboratório; sem snapshots de conversas ou ativação de políticas.
CREATE TABLE eval_datasets (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    checksum TEXT NOT NULL,
    PRIMARY KEY (id, version)
);

CREATE TABLE eval_runs (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    dataset_checksum TEXT NOT NULL,
    split TEXT NOT NULL CHECK (split IN ('train', 'validation', 'test')),
    policy_version TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    metrics_json TEXT,
    evidence_checksum TEXT,
    FOREIGN KEY (dataset_id, dataset_version) REFERENCES eval_datasets(id, version)
);
