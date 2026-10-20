"""Liga uma conversa externa (ex.: chat do Telegram) à sessão atual do NeoSon."""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction


class SqliteChannelSessions:
    def __init__(self, path: Path) -> None:
        self._path = path
        with self._connection() as connection:
            migrate(connection)

    async def get(self, channel: str, external_id: str) -> UUID | None:
        return await asyncio.to_thread(self._get, channel, external_id)

    async def bind(self, channel: str, external_id: str, session_id: UUID) -> None:
        await asyncio.to_thread(self._bind, channel, external_id, session_id)

    async def forget(self, channel: str, external_id: str) -> None:
        await asyncio.to_thread(self._forget, channel, external_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self._path)
        try:
            yield connection
        finally:
            connection.close()

    def _get(self, channel: str, external_id: str) -> UUID | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT session_id FROM channel_sessions"
                " WHERE channel = ? AND external_id = ?",
                (channel, external_id),
            ).fetchone()
        return None if row is None else UUID(row["session_id"])

    def _bind(self, channel: str, external_id: str, session_id: UUID) -> None:
        with self._connection() as connection, transaction(connection):
            connection.execute(
                "INSERT INTO channel_sessions (channel, external_id, session_id,"
                " updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (channel, external_id) DO UPDATE SET"
                " session_id = excluded.session_id, updated_at = excluded.updated_at",
                (channel, external_id, str(session_id), now_iso()),
            )

    def _forget(self, channel: str, external_id: str) -> None:
        with self._connection() as connection, transaction(connection):
            connection.execute(
                "DELETE FROM channel_sessions WHERE channel = ? AND external_id = ?",
                (channel, external_id),
            )
