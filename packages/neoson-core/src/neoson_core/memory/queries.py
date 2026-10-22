"""Leituras para a API: execução, página de sessão, prontidão e resumo do dia."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path
from uuid import UUID

from neoson_core.infrastructure.db import connect, migrations, schema_version
from neoson_core.telemetry.metrics import DailyMetrics, daily_metrics, system_metrics

MAX_PAGE = 100


class NotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class RunView:
    run_id: UUID
    session_id: UUID
    status: str
    text: str | None
    model: str | None
    started_at: datetime
    ended_at: datetime
    input_tokens: int
    output_tokens: int
    cached_tokens: int


@dataclass(frozen=True, slots=True)
class MessageView:
    id: int
    role: str
    content: str
    created_at: datetime
    run_id: UUID | None


@dataclass(frozen=True, slots=True)
class SessionPage:
    session_id: UUID
    channel: str
    created_at: datetime
    messages: list[MessageView]
    next_before: int | None


@dataclass(frozen=True, slots=True)
class Summary:
    metrics: DailyMetrics
    outbox_pending: int
    outbox_dead_letter: int


def run_view(db: Path, run_id: UUID) -> RunView:
    with closing(connect(db)) as c:
        row = c.execute("SELECT * FROM runs WHERE id = ?", (str(run_id),)).fetchone()
        if row is None:
            raise NotFound
        # O resultado é a última resposta do assistente gravada na execução.
        text = c.execute(
            "SELECT content FROM messages WHERE run_id = ? AND role = 'assistant'"
            " AND content <> '' ORDER BY id DESC LIMIT 1",
            (str(run_id),),
        ).fetchone()
    return RunView(
        UUID(row["id"]),
        UUID(row["session_id"]),
        row["status"],
        None if text is None else text[0],
        row["model"],
        datetime.fromisoformat(row["started_at"]),
        datetime.fromisoformat(row["ended_at"]),
        row["input_tokens"],
        row["output_tokens"],
        row["cached_tokens"],
    )


def session_page(
    db: Path, session_id: UUID, *, before: int | None = None, limit: int = 50
) -> SessionPage:
    """Só mensagens do usuário e do assistente, mais recentes primeiro."""
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit deve estar entre 1 e {MAX_PAGE}.")
    with closing(connect(db)) as c:
        session = c.execute(
            "SELECT * FROM sessions WHERE id = ?", (str(session_id),)
        ).fetchone()
        if session is None:
            raise NotFound
        rows = c.execute(
            "SELECT id, role, content, created_at, run_id FROM messages"
            " WHERE session_id = ? AND role IN ('user', 'assistant') AND content <> ''"
            " AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
            (str(session_id), before, before, limit + 1),
        ).fetchall()
    page = rows[:limit]
    return SessionPage(
        session_id,
        session["channel"],
        datetime.fromisoformat(session["created_at"]),
        [
            MessageView(
                r["id"],
                r["role"],
                r["content"],
                datetime.fromisoformat(r["created_at"]),
                None if r["run_id"] is None else UUID(r["run_id"]),
            )
            for r in page
        ],
        page[-1]["id"] if len(rows) > limit else None,
    )


def readiness(db: Path) -> dict[str, bool]:
    """Banco abre, responde e está na versão das migrações deste código."""
    checks = {"database": False, "migrations": False}
    if not db.is_file():  # não criar um banco vazio só para responder
        return checks
    try:
        with closing(connect(db)) as c:
            c.execute("SELECT 1").fetchone()
            checks["database"] = True
            latest = max((m.version for m in migrations()), default=0)
            checks["migrations"] = schema_version(c) == latest
    except sqlite3.Error:
        pass
    return checks


def summary(db: Path, day: date, zone: tzinfo) -> Summary:
    with closing(connect(db)) as c:
        metrics = daily_metrics(c, day, zone)
        system = system_metrics(c)
    return Summary(metrics, system.outbox.get("pending", 0), system.dead_letters)
