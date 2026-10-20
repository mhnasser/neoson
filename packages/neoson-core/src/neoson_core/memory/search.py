"""Busca FTS5 no histórico de mensagens (implementa `HistorySearch`)."""

import asyncio
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

from neoson_core.engine import Role
from neoson_core.engine.recall import HistoryHit
from neoson_core.infrastructure.db import connect, migrate

_WORD = re.compile(r"\w{3,}", re.UNICODE)
MAX_TERMS = 12


def fts_query(text: str) -> str | None:
    """Consulta FTS5 segura: palavras de 3+ letras entre aspas, unidas por OR.

    Operadores e pontuação do texto original nunca chegam ao FTS5.
    """
    terms = list(dict.fromkeys(word.casefold() for word in _WORD.findall(text)))
    if not terms:
        return None
    return " OR ".join(f'"{term}"' for term in terms[:MAX_TERMS])


class SqliteHistorySearch:
    def __init__(self, path: Path) -> None:
        self._path = path
        with self._connection() as connection:
            migrate(connection)

    async def search(
        self, query: str, limit: int, exclude_session: UUID | None = None
    ) -> Sequence[HistoryHit]:
        return await asyncio.to_thread(self._search, query, limit, exclude_session)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self._path)
        try:
            yield connection
        finally:
            connection.close()

    def _search(
        self, query: str, limit: int, exclude_session: UUID | None
    ) -> list[HistoryHit]:
        match = fts_query(query)
        if match is None:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT m.session_id, m.role, m.created_at,"
                " snippet(messages_fts, 0, '«', '»', '…', 16) AS snippet"
                " FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
                " WHERE messages_fts MATCH ? AND m.session_id IS NOT ?"
                " ORDER BY bm25(messages_fts) LIMIT ?",
                (
                    match,
                    None if exclude_session is None else str(exclude_session),
                    limit,
                ),
            ).fetchall()
        return [
            HistoryHit(
                session_id=UUID(row["session_id"]),
                role=Role(row["role"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                snippet=row["snippet"],
            )
            for row in rows
        ]
