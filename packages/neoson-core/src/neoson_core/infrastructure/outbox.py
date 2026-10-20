"""Fila durável sobre `outbox_events` (§10, D12): lease, conclusão, retry e dead letter.

Entrega pelo menos uma vez: um evento pode ser processado de novo depois de uma
queda; os handlers garantem efeito único com `worker_effects`.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from neoson_core.engine import OutboxEvent


@dataclass(frozen=True, slots=True)
class LeasedEvent:
    event: OutboxEvent
    attempts: int


def iso(moment: datetime) -> str:
    """Formato fixo, para que a comparação de texto no SQLite siga a ordem do tempo."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def lease(
    connection: sqlite3.Connection,
    worker_id: str,
    now: datetime,
    lease_seconds: float,
    limit: int = 10,
) -> list[LeasedEvent]:
    """Reserva eventos prontos: pendentes no horário ou com lease vencido (queda)."""
    stamp = iso(now)
    until = iso(now + timedelta(seconds=lease_seconds))
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT id FROM outbox_events"
            " WHERE (status = 'pending' AND (retry_at IS NULL OR retry_at <= ?))"
            "    OR (status = 'leased' AND lease_until <= ?)"
            " ORDER BY occurred_at LIMIT ?",
            (stamp, stamp, limit),
        ).fetchall()
        ids = [row["id"] for row in rows]
        for event_id in ids:
            connection.execute(
                "UPDATE outbox_events SET status = 'leased', lease_until = ?,"
                " worker_id = ?, attempts = attempts + 1 WHERE id = ?",
                (until, worker_id, event_id),
            )
        leased = [_leased(connection, event_id) for event_id in ids]
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    return leased


def complete(connection: sqlite3.Connection, event_id: UUID, worker_id: str) -> bool:
    """Marca como concluído se o lease ainda for deste worker (use numa transação)."""
    cursor = connection.execute(
        "UPDATE outbox_events SET status = 'completed', lease_until = NULL,"
        " last_error = NULL WHERE id = ? AND status = 'leased' AND worker_id = ?",
        (str(event_id), worker_id),
    )
    return cursor.rowcount == 1


def fail(
    connection: sqlite3.Connection,
    event_id: UUID,
    worker_id: str,
    error: str,
    retry_at: datetime | None,
) -> None:
    """Agenda nova tentativa em `retry_at`; `None` manda para `dead_letter`."""
    status = "pending" if retry_at is not None else "dead_letter"
    connection.execute(
        "UPDATE outbox_events SET status = ?, retry_at = ?, lease_until = NULL,"
        " last_error = ? WHERE id = ? AND status = 'leased' AND worker_id = ?",
        (
            status,
            iso(retry_at) if retry_at else None,
            error[:500],
            str(event_id),
            worker_id,
        ),
    )


def has_effect(connection: sqlite3.Connection, event_id: UUID, handler: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM worker_effects WHERE event_id = ? AND handler = ?",
        (str(event_id), handler),
    ).fetchone()
    return row is not None


def record_effect(
    connection: sqlite3.Connection, event_id: UUID, handler: str, now: datetime
) -> None:
    connection.execute(
        "INSERT INTO worker_effects (event_id, handler, completed_at) VALUES (?, ?, ?)",
        (str(event_id), handler, iso(now)),
    )


def _leased(connection: sqlite3.Connection, event_id: str) -> LeasedEvent:
    row = connection.execute(
        "SELECT * FROM outbox_events WHERE id = ?", (event_id,)
    ).fetchone()
    event = OutboxEvent(
        type=row["type"],
        payload=json.loads(row["payload"]),
        run_id=_uuid(row["run_id"]),
        session_id=_uuid(row["session_id"]),
        correlation_id=_uuid(row["correlation_id"]),
        event_id=UUID(row["id"]),
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        schema_version=row["schema_version"],
    )
    return LeasedEvent(event=event, attempts=row["attempts"])


def _uuid(value: str | None) -> UUID | None:
    return None if value is None else UUID(value)
