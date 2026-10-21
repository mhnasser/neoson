"""Projeção do laboratório: SQL somente leitura e campos explicitamente públicos."""

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

EVENT_TYPES = tuple(
    f"neoson.router.{name}"
    for name in (
        "benchmark.started",
        "benchmark.completed",
        "benchmark.failed",
        "policy.promoted",
        "policy.rolled_back",
    )
)
LIMIT = 100
Scalar = int | float | bool | None


@dataclass(frozen=True)
class Arm:
    metrics: dict[str, Scalar]
    confusion: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True)
class BenchmarkMetrics:
    id: str
    dataset: str
    version: str
    split: str
    policy: str
    state: str
    created_at: str
    integrity: str
    mode: str | None = None
    baseline: Arm | None = None
    candidate: Arm | None = None
    regressions: int | None = None
    savings: float | None = None


@dataclass(frozen=True)
class PolicyEvent:
    action: str
    version: str
    created_at: str


@dataclass(frozen=True)
class RouterMetrics:
    lab_state: str
    policy_state: str
    total: int
    runs: tuple[BenchmarkMetrics, ...]
    active: str | None
    events: tuple[PolicyEvent, ...]


def _label(value: object, pattern: str = r"[a-z][a-z0-9_]{1,63}") -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError
    return value


def _stamp(value: str) -> str:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError
    return stamp.astimezone(UTC).isoformat()


def _numeric(value: object, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    if not math.isfinite(value) or value < 0 or (integer and type(value) is not int):
        raise ValueError
    return value


def _arm(data: dict[str, object]) -> Arm:
    metrics: dict[str, Scalar] = {}
    for name in (
        "cases",
        "passed",
        "safety_failures",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "attempts",
    ):
        metrics[name] = _numeric(data[name], integer=True)
    for name in (
        "quality_rate",
        "routing_accuracy",
        "intent_accuracy",
        "complexity_accuracy",
        "mode_accuracy",
        "escalation_rate",
    ):
        value = _numeric(data[name])
        if value > 1:
            raise ValueError
        metrics[name] = value
    for name in ("cost_usd", "cache_hit_ratio"):
        optional = data[name]
        metrics[name] = None if optional is None else _numeric(optional)
    metrics["seconds"] = _numeric(data["seconds"])
    if type(data["usage_complete"]) is not bool:
        raise ValueError
    metrics["usage_complete"] = data["usage_complete"]
    cases, passed = _numeric(data["cases"]), _numeric(data["passed"])
    if (
        not cases
        or passed > cases
        or abs(_numeric(data["quality_rate"]) - passed / cases) > 1e-8
    ):
        raise ValueError
    if _numeric(data["cached_tokens"]) > _numeric(data["input_tokens"]):
        raise ValueError
    if (
        metrics["cache_hit_ratio"] is not None
        and _numeric(metrics["cache_hit_ratio"]) > 1
    ):
        raise ValueError
    matrix = data["confusion_matrix"]
    if not isinstance(matrix, dict):
        raise ValueError
    cells = []
    for expected, row in sorted(matrix.items()):
        expected = _label(expected)
        if not isinstance(row, dict):
            raise ValueError
        for actual, count in sorted(row.items()):
            cells.append((expected, _label(actual), int(_numeric(count, integer=True))))
    if sum(c[2] for c in cells) != cases:
        raise ValueError
    return Arm(metrics, tuple(cells))


def read_runs(
    connection: sqlite3.Connection,
) -> tuple[int, tuple[BenchmarkMetrics, ...]]:
    total = connection.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0]
    result = []
    for row in connection.execute(
        "SELECT id,dataset_id,dataset_version,dataset_checksum,split,policy_version,"
        "status,created_at,metrics_json,evidence_checksum FROM eval_runs"
        " ORDER BY created_at DESC,id DESC LIMIT ?",
        (LIMIT,),
    ):
        run_id = str(UUID(row["id"]))
        dataset = _label(row["dataset_id"])
        version = _label(row["dataset_version"], r"[0-9]+\.[0-9]+\.[0-9]+")
        policy = _label(row["policy_version"], r"rules-v[1-9][0-9]*")
        if row["split"] not in ("train", "validation", "test") or row["status"] not in (
            "running",
            "completed",
            "failed",
        ):
            raise ValueError
        common = (
            run_id,
            dataset,
            version,
            row["split"],
            policy,
            row["status"],
            _stamp(row["created_at"]),
        )
        if row["status"] != "completed":
            result.append(BenchmarkMetrics(*common, "none"))
            continue
        try:
            text = row["metrics_json"]
            checksum = hashlib.sha256(
                json.dumps(
                    text,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            if checksum != row["evidence_checksum"]:
                raise ValueError
            data = json.loads(text)
            if (
                data["id"] != row["id"]
                or data["dataset_id"] != dataset
                or data["dataset_version"] != version
                or data["split"] != row["split"]
                or data["dataset_checksum"] != row["dataset_checksum"]
                or data["candidate"]["policy_version"] != policy
                or data["baseline"]["policy_version"] != "single-model-v1"
                or data["mode"] not in ("scripted", "openai")
            ):
                raise ValueError
            baseline, candidate = (
                _arm(data[name]["metrics"]) for name in ("baseline", "candidate")
            )
            regressions = data["quality_regressions"]
            if (
                not isinstance(regressions, list)
                or baseline.metrics["cases"] != candidate.metrics["cases"]
            ):
                raise ValueError
            savings = None
            base_cost, candidate_cost = (
                baseline.metrics["cost_usd"],
                candidate.metrics["cost_usd"],
            )
            if (
                data["mode"] == "openai"
                and data["comparable_for_savings"] is True
                and not regressions
                and all(
                    a.metrics["quality_rate"] == 1
                    and a.metrics["safety_failures"] == 0
                    and a.metrics["usage_complete"]
                    for a in (baseline, candidate)
                )
                and base_cost is not None
                and candidate_cost is not None
                and base_cost > 0
            ):
                savings = round((base_cost - candidate_cost) / base_cost * 100, 4)
            result.append(
                BenchmarkMetrics(
                    *common,
                    "valid",
                    data["mode"],
                    baseline,
                    candidate,
                    len(regressions),
                    savings,
                )
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
            AttributeError,
            RecursionError,
        ):
            result.append(BenchmarkMetrics(*common, "invalid"))
    return total, tuple(result)


def read_policy(
    connection: sqlite3.Connection,
) -> tuple[str | None, tuple[PolicyEvent, ...]]:
    row = connection.execute(
        "SELECT version FROM routing_policy_state WHERE singleton=1"
    ).fetchone()
    active = None if row is None else _label(row[0], r"rules-v[1-9][0-9]*")
    events = []
    for action, version, created in connection.execute(
        "SELECT action,version,created_at FROM routing_policy_events"
        " ORDER BY seq DESC LIMIT ?",
        (LIMIT,),
    ):
        if action not in ("bootstrap", "promote", "rollback"):
            raise ValueError
        events.append(
            PolicyEvent(action, _label(version, r"rules-v[1-9][0-9]*"), _stamp(created))
        )
    return active, tuple(events)


def _read[T](
    path: Path, table: str, reader: Callable[[sqlite3.Connection], T]
) -> tuple[str, T | None]:
    if not path.exists():
        return "missing", None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                return "unsupported", None
            return "ready", reader(connection)
        finally:
            connection.close()
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        return "unavailable", None


def load_metrics(policy_path: Path, lab_path: Path) -> RouterMetrics:
    policy_state, p = _read(policy_path, "routing_policy_state", read_policy)
    lab_state, r = _read(lab_path, "eval_runs", read_runs)
    active, events = (None, ()) if p is None else p
    total, runs = (0, ()) if r is None else r
    return RouterMetrics(lab_state, policy_state, total, runs, active, events)
