"""Agregados para o vault (D14), lidos pela conexão recebida; sem texto de conversa."""

import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DailyMetrics:
    day: date
    runs_total: int = 0
    completed: int = 0
    failed: int = 0
    waiting_approval: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None
    runs_without_cost: int = 0
    models: Mapping[str, int] = field(default_factory=dict)

    @property
    def success_pct(self) -> float | None:
        if not self.runs_total:
            return None
        return round(100 * self.completed / self.runs_total, 1)

    @property
    def cache_pct(self) -> float | None:
        if not self.input_tokens:
            return None
        return round(100 * self.cached_tokens / self.input_tokens, 1)


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model: str
    runs: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float | None


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    outbox: Mapping[str, int]
    retrying: int
    oldest_retry_at: datetime | None
    avg_lag_seconds: float | None
    last_run_at: datetime | None

    @property
    def dead_letters(self) -> int:
        return self.outbox.get("dead_letter", 0)


@dataclass(frozen=True, slots=True)
class _Run:
    status: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    started_at: datetime
    cost_usd: float | None


def daily_metrics(
    connection: sqlite3.Connection, day: date, zone: tzinfo
) -> DailyMetrics:
    runs = _runs_between(connection, *_day_bounds(day, zone))
    costs = [r.cost_usd for r in runs if r.cost_usd is not None]
    statuses = Counter(r.status for r in runs)
    return DailyMetrics(
        day=day,
        runs_total=len(runs),
        completed=statuses["completed"],
        failed=statuses["failed"],
        waiting_approval=statuses["needs_approval"],
        input_tokens=sum(r.input_tokens for r in runs),
        output_tokens=sum(r.output_tokens for r in runs),
        cached_tokens=sum(r.cached_tokens for r in runs),
        cost_usd=round(sum(costs), 6) if costs else None,
        runs_without_cost=len(runs) - len(costs),
        models=dict(sorted(Counter(r.model for r in runs).items())),
    )


def recent_days(
    connection: sqlite3.Connection, today: date, zone: tzinfo, days: int = 7
) -> list[DailyMetrics]:
    return [
        daily_metrics(connection, today - timedelta(days=offset), zone)
        for offset in range(days)
    ]


def model_usage(
    connection: sqlite3.Connection, today: date, zone: tzinfo, days: int = 30
) -> list[ModelUsage]:
    start, _ = _day_bounds(today - timedelta(days=days - 1), zone)
    _, end = _day_bounds(today, zone)
    by_model: dict[str, list[_Run]] = {}
    for run in _runs_between(connection, start, end):
        by_model.setdefault(run.model, []).append(run)
    result = []
    for model, runs in sorted(by_model.items()):
        costs = [r.cost_usd for r in runs if r.cost_usd is not None]
        result.append(
            ModelUsage(
                model=model,
                runs=len(runs),
                input_tokens=sum(r.input_tokens for r in runs),
                output_tokens=sum(r.output_tokens for r in runs),
                cached_tokens=sum(r.cached_tokens for r in runs),
                cost_usd=round(sum(costs), 6) if costs else None,
            )
        )
    return result


def system_metrics(
    connection: sqlite3.Connection, completing: UUID | None = None
) -> SystemMetrics:
    """`completing`: evento que o worker está concluindo agora; conta como concluído."""
    outbox = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT CASE WHEN id = ? THEN 'completed' ELSE status END AS state,"
            " COUNT(*) FROM outbox_events GROUP BY state ORDER BY state",
            (None if completing is None else str(completing),),
        )
    }
    retrying, oldest = connection.execute(
        "SELECT COUNT(*), MIN(occurred_at) FROM outbox_events"
        " WHERE status = 'pending' AND attempts > 0"
    ).fetchone()
    lags = [
        (_parse(done) - _parse(occurred)).total_seconds()
        for occurred, done in connection.execute(
            "SELECT e.occurred_at, MIN(w.completed_at) FROM worker_effects w"
            " JOIN outbox_events e ON e.id = w.event_id"
            " GROUP BY e.id ORDER BY e.occurred_at DESC LIMIT 100"
        )
    ]
    last_run = connection.execute("SELECT MAX(started_at) FROM runs").fetchone()[0]
    return SystemMetrics(
        outbox=outbox,
        retrying=retrying,
        oldest_retry_at=_parse(oldest) if oldest else None,
        avg_lag_seconds=round(sum(lags) / len(lags), 1) if lags else None,
        last_run_at=_parse(last_run) if last_run else None,
    )


def _day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, zone),
        datetime.combine(day + timedelta(days=1), time.min, zone),
    )


def _runs_between(
    connection: sqlite3.Connection, start: datetime, end: datetime
) -> Sequence[_Run]:
    # Margem de um dia no SQL; o recorte exato é feito com datas de verdade.
    rows = connection.execute(
        "SELECT r.status, r.model, r.input_tokens, r.output_tokens, r.cached_tokens,"
        " r.started_at, u.cost_usd FROM runs r"
        " LEFT JOIN usage_records u ON u.run_id = r.id"
        " WHERE r.started_at >= ? AND r.started_at < ?",
        (
            (start - timedelta(days=1)).isoformat(),
            (end + timedelta(days=1)).isoformat(),
        ),
    ).fetchall()
    runs = [
        _Run(
            status=row[0],
            model=row[1] or "desconhecido",
            input_tokens=row[2],
            output_tokens=row[3],
            cached_tokens=row[4],
            started_at=_parse(row[5]),
            cost_usd=row[6],
        )
        for row in rows
    ]
    return [r for r in runs if start <= r.started_at < end]


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
