"""Agregados por versão de skill; não lê instruções, fixtures nem conversas."""

import sqlite3
from dataclasses import dataclass

EVENT_TYPES = (
    "neoson.skill.approved",
    "neoson.skill.rolled_back",
    "neoson.skill.retired",
    "neoson.skill.evaluated",
)


@dataclass(frozen=True, slots=True)
class SkillMetrics:
    id: str
    version: str
    status: str
    runs: int
    completed: int
    failed: int
    waiting_approval: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float | None
    runs_without_cost: int
    evaluated_at: str | None
    total_cases: int | None
    passed_cases: int | None
    evaluation_input_tokens: int | None
    evaluation_output_tokens: int | None
    evaluation_cached_tokens: int | None
    evaluation_cost_usd: float | None
    evaluation_seconds: float | None

    @property
    def success_pct(self) -> float | None:
        return round(100 * self.completed / self.runs, 1) if self.runs else None


def skill_metrics(connection: sqlite3.Connection) -> list[SkillMetrics]:
    """Histórico integral; avaliação mais recente da mesma versão e checksum.

    O uso vem do run (uma skill por run). Avaliações são separadas das execuções
    para não duplicar custo/tokens nem misturar fixtures ao uso normal.
    """
    rows = connection.execute(
        "SELECT s.id, s.version, s.status, COALESCE(x.runs, 0) AS runs,"
        " COALESCE(x.completed, 0) AS completed, COALESCE(x.failed, 0) AS failed,"
        " COALESCE(x.waiting, 0) AS waiting_approval,"
        " COALESCE(x.input_tokens, 0) AS input_tokens,"
        " COALESCE(x.output_tokens, 0) AS output_tokens,"
        " COALESCE(x.cached_tokens, 0) AS cached_tokens, x.cost_usd,"
        " COALESCE(x.missing_cost, 0) AS runs_without_cost,"
        " e.evaluated_at, e.total_cases, e.passed_cases,"
        " e.input_tokens AS evaluation_input_tokens,"
        " e.output_tokens AS evaluation_output_tokens,"
        " e.cached_tokens AS evaluation_cached_tokens,"
        " e.cost_usd AS evaluation_cost_usd,"
        " e.duration_seconds AS evaluation_seconds"
        " FROM skills s LEFT JOIN ("
        " SELECT x.skill_id, x.skill_version, COUNT(*) AS runs,"
        " SUM(x.outcome = 'completed') AS completed,"
        " SUM(x.outcome = 'failed') AS failed,"
        " SUM(x.outcome = 'needs_approval') AS waiting,"
        " SUM(r.input_tokens) AS input_tokens, SUM(r.output_tokens) AS output_tokens,"
        " SUM(r.cached_tokens) AS cached_tokens, ROUND(SUM(u.cost_usd), 6) AS cost_usd,"
        " SUM(u.cost_usd IS NULL) AS missing_cost"
        " FROM skill_executions x JOIN runs r ON r.id = x.run_id"
        " LEFT JOIN usage_records u ON u.run_id = r.id"
        " GROUP BY x.skill_id, x.skill_version"
        " ) x ON x.skill_id = s.id AND x.skill_version = s.version"
        " LEFT JOIN skill_evaluations e ON e.id = ("
        " SELECT latest.id FROM skill_evaluations latest"
        " WHERE latest.skill_id = s.id AND latest.skill_version = s.version"
        " AND latest.checksum = s.checksum"
        " ORDER BY julianday(latest.evaluated_at) DESC, latest.id DESC LIMIT 1"
        " ) ORDER BY s.id, s.approval_id"
    )
    return [SkillMetrics(**dict(row)) for row in rows]
