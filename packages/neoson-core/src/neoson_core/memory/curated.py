"""Memória curada em SQLite: cada mudança grava também `neoson.memory.updated`."""

import asyncio
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from neoson_core.engine import OutboxEvent
from neoson_core.engine.curated import MemoryFact, Namespace
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction

EVENT_TYPE = "neoson.memory.updated"


class SqliteCuratedMemory:
    def __init__(self, path: Path) -> None:
        self._path = path
        with self._connection() as connection:
            migrate(connection)

    async def save(
        self,
        namespace: Namespace,
        content: str,
        session_id: UUID | None,
        run_id: UUID | None,
    ) -> MemoryFact:
        return await asyncio.to_thread(
            self._save, namespace, content, session_id, run_id
        )

    async def forget(self, fact_id: int) -> bool:
        return await asyncio.to_thread(self._forget, fact_id)

    async def active(self, limit: int) -> Sequence[MemoryFact]:
        return await asyncio.to_thread(self._active, limit)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self._path)
        try:
            yield connection
        finally:
            connection.close()

    def _save(
        self,
        namespace: Namespace,
        content: str,
        session_id: UUID | None,
        run_id: UUID | None,
    ) -> MemoryFact:
        text = " ".join(content.split())
        if not text:
            raise ValueError("Fato vazio.")
        created = now_iso()
        with self._connection() as connection, transaction(connection):
            cursor = connection.execute(
                "INSERT INTO memory_facts (namespace, content, source_session_id,"
                " source_run_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (namespace, text, _text(session_id), _text(run_id), created),
            )
            fact_id = int(cursor.lastrowid or 0)
            outbox.insert(
                connection,
                # Sem `session_id` no evento: a origem fica no fato, e a sessão pode
                # não estar no banco (motor sem armazenamento de conversa).
                OutboxEvent(
                    type=EVENT_TYPE,
                    payload={"action": "saved", "fact_id": fact_id},
                ),
            )
        return MemoryFact(
            id=fact_id,
            namespace=namespace,
            content=text,
            created_at=datetime.fromisoformat(created),
            source_session_id=session_id,
            source_run_id=run_id,
        )

    def _forget(self, fact_id: int) -> bool:
        with self._connection() as connection, transaction(connection):
            cursor = connection.execute(
                "UPDATE memory_facts SET status = 'forgotten', forgotten_at = ?"
                " WHERE id = ? AND status = 'active'",
                (now_iso(), fact_id),
            )
            if cursor.rowcount != 1:
                return False
            outbox.insert(
                connection,
                OutboxEvent(
                    type=EVENT_TYPE,
                    payload={"action": "forgotten", "fact_id": fact_id},
                ),
            )
        return True

    def _active(self, limit: int) -> list[MemoryFact]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_facts WHERE status = 'active'"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [fact_from_row(row) for row in rows]


def fact_from_row(row: sqlite3.Row) -> MemoryFact:
    return MemoryFact(
        id=row["id"],
        namespace=row["namespace"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
        source_session_id=_uuid(row["source_session_id"]),
        source_run_id=_uuid(row["source_run_id"]),
    )


def _text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _uuid(value: str | None) -> UUID | None:
    return None if value is None else UUID(value)
