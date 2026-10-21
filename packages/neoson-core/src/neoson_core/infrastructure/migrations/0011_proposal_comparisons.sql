ALTER TABLE skill_proposals ADD COLUMN review_token TEXT;
CREATE TABLE skill_proposal_comparisons (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES skill_proposals(id),
    created_at TEXT NOT NULL,
    base_revision TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    base_report_json TEXT NOT NULL,
    candidate_report_json TEXT NOT NULL,
    evidence_checksum TEXT NOT NULL
);
CREATE INDEX proposal_comparisons_proposal ON skill_proposal_comparisons(proposal_id);
CREATE TABLE skill_proposal_approvals (
    proposal_id TEXT PRIMARY KEY REFERENCES skill_proposals(id),
    comparison_id TEXT NOT NULL REFERENCES skill_proposal_comparisons(id),
    approved_at TEXT NOT NULL,
    accept_unknown_cost INTEGER NOT NULL CHECK(accept_unknown_cost IN (0,1)),
    accept_resource_increase INTEGER NOT NULL CHECK(accept_resource_increase IN (0,1))
);
