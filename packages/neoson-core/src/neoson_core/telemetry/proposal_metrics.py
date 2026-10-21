"""Projeção pública das propostas: somente metadados e agregados numéricos."""

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

EVENT_TYPES = tuple(
    f"neoson.skill.proposal.{action}"
    for action in (
        "created",
        "review_started",
        "evaluated",
        "approved",
        "rejected",
        "stale",
    )
)
STATES = ("pending", "evaluated", "approved", "rejected", "stale")


@dataclass(frozen=True, slots=True)
class ReportMetrics:
    cases: int
    passed: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float | None
    seconds: float


@dataclass(frozen=True, slots=True)
class ComparisonMetrics:
    id: str
    created_at: str
    current: bool
    base: ReportMetrics
    candidate: ReportMetrics
    regressions: int
    improvements: int


@dataclass(frozen=True, slots=True)
class ProposalMetrics:
    id: str
    skill_id: str
    base_version: str
    candidate_version: str
    status: str
    created_at: str
    source_runs: int
    source_evaluations: int
    candidate_active: bool
    evidence_state: str
    comparison: ComparisonMetrics | None


def proposal_metrics(connection: sqlite3.Connection) -> list[ProposalMetrics]:
    # Não ler files_json: pode conter conteúdo privado gerado pelo modelo.
    rows = connection.execute(
        "SELECT p.id,p.skill_id,p.base_version,p.base_checksum,p.candidate_version,"
        " p.candidate_checksum,p.status,p.created_at,p.review_token,"
        " (SELECT COUNT(*) FROM skill_proposal_runs r"
        " WHERE r.proposal_id=p.id) AS runs,"
        " (SELECT COUNT(*) FROM skill_proposal_evaluations e"
        " WHERE e.proposal_id=p.id) AS evals,"
        " EXISTS(SELECT 1 FROM skills s WHERE s.id=p.skill_id"
        " AND s.version=p.candidate_version"
        " AND s.checksum=p.candidate_checksum AND s.status='active') AS active,"
        " c.id AS comparison_id,c.created_at AS compared_at,"
        " c.base_revision,c.context_hash,"
        " c.base_report_json,c.candidate_report_json,c.evidence_checksum"
        " FROM skill_proposals p LEFT JOIN skill_proposal_comparisons c ON c.id=("
        " SELECT last.id FROM skill_proposal_comparisons last"
        " WHERE last.proposal_id=p.id"
        " ORDER BY last.rowid DESC LIMIT 1) ORDER BY p.created_at,p.id"
    )
    result = []
    for row in rows:
        try:
            proposal_id = str(UUID(row["id"]))
            if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", row["skill_id"]) is None:
                raise ValueError
            if any(
                re.fullmatch(r"\d+\.\d+\.\d+", row[k]) is None
                for k in ("base_version", "candidate_version")
            ):
                raise ValueError
            if row["status"] not in STATES:
                raise ValueError
            created_at = _timestamp(row["created_at"])
        except (ValueError, TypeError):
            raise ValueError(
                "Metadados de proposta inválidos para exportação."
            ) from None
        comparison = None
        state = "none"
        if row["comparison_id"] is not None:
            try:
                comparison = _comparison(row)
                state = "current" if comparison.current else "previous"
            except (ValueError, TypeError, KeyError, OverflowError):
                state = "invalid"
        result.append(
            ProposalMetrics(
                proposal_id,
                row["skill_id"],
                row["base_version"],
                row["candidate_version"],
                row["status"],
                created_at,
                row["runs"],
                row["evals"],
                bool(row["active"]),
                state,
                comparison,
            )
        )
    return result


def _timestamp(value: str) -> str:
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        raise ValueError
    return when.astimezone(UTC).isoformat()


def _comparison(row: sqlite3.Row) -> ComparisonMetrics:
    comparison_id = str(UUID(row["comparison_id"]))
    # Mesmo envelope de integridade, sem importar o adaptador de skills.
    values = (
        row["comparison_id"],
        row["id"],
        row["compared_at"],
        row["base_revision"],
        row["context_hash"],
        row["base_report_json"],
        row["candidate_report_json"],
    )
    digest = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
    if digest != row["evidence_checksum"]:
        raise ValueError
    base, before = _report(
        row["base_report_json"],
        row["skill_id"],
        row["base_version"],
        row["base_checksum"],
    )
    candidate, after = _report(
        row["candidate_report_json"],
        row["skill_id"],
        row["candidate_version"],
        row["candidate_checksum"],
    )
    if before.keys() != after.keys():
        raise ValueError
    return ComparisonMetrics(
        comparison_id,
        _timestamp(row["compared_at"]),
        row["review_token"] == row["comparison_id"]
        and row["status"] in ("evaluated", "approved"),
        base,
        candidate,
        sum(before[k] and not after[k] for k in before),
        sum(not before[k] and after[k] for k in before),
    )


def _number(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError
    return float(value)


def _report(
    text: str, skill_id: str, version: str, checksum: str
) -> tuple[ReportMetrics, dict[str, bool]]:
    data = json.loads(text)
    if not isinstance(data, dict) or (
        data.get("skill_id"),
        data.get("skill_version"),
        data.get("checksum"),
    ) != (skill_id, version, checksum):
        raise ValueError
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError
    outcomes: dict[str, bool] = {}
    tokens = [0, 0, 0]
    costs: list[float] = []
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("id"), str)
            or type(case.get("passed")) is not bool
        ):
            raise ValueError
        if case["id"] in outcomes:
            raise ValueError
        outcomes[case["id"]] = case["passed"]
        usage = case["usage"]
        for index, key in enumerate(("input_tokens", "output_tokens", "cached_tokens")):
            value = usage[key]
            if type(value) is not int or value < 0:
                raise ValueError
            tokens[index] += value
        if case.get("cost_usd") is not None:
            costs.append(_number(case["cost_usd"]))
    return ReportMetrics(
        len(cases),
        sum(outcomes.values()),
        tokens[0],
        tokens[1],
        tokens[2],
        round(sum(costs), 8) if len(costs) == len(cases) else None,
        _number(data["duration_seconds"]),
    ), outcomes
