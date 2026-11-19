"""Busca FTS5 somente em trechos de documentos ativos, com proveniência."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from neoson_core.engine.recall import DocumentHit
from neoson_core.infrastructure.db import connect, migrate
from neoson_core.memory.search import fts_query


class SqliteDocumentSearch:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        with closing(connect(path)) as connection:
            migrate(connection)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=ro", uri=True, timeout=0.25
        )
        connection.row_factory = sqlite3.Row
        return connection

    def has_documents(self) -> bool:
        try:
            with closing(self._connection()) as connection:
                return (
                    connection.execute(
                        "SELECT 1 FROM documents WHERE status = 'active' LIMIT 1"
                    ).fetchone()
                    is not None
                )
        except sqlite3.Error:
            return False

    async def search(self, query: str, limit: int) -> list[DocumentHit]:
        if not 1 <= len(query) <= 300 or type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Consulta de documentos fora dos limites.")
        return await asyncio.to_thread(self._search, query, limit)

    def _search(self, query: str, limit: int) -> list[DocumentHit]:
        match = fts_query(query)
        if match is None:
            return []
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT d.id, d.title, d.ingested_at, c.n,"
                " snippet(document_chunks_fts, 0, '«', '»', '…', 24) AS snippet"
                " FROM document_chunks_fts JOIN document_chunks c"
                " ON c.rowid = document_chunks_fts.rowid"
                " JOIN documents d ON d.id = c.document_id"
                " WHERE document_chunks_fts MATCH ? AND d.status = 'active'"
                " ORDER BY bm25(document_chunks_fts), d.id, c.n LIMIT ?",
                (match, limit),
            ).fetchall()
        return [
            DocumentHit(
                f"document:{row['id']}:{row['n']}",
                row["title"],
                datetime.fromisoformat(row["ingested_at"]),
                row["snippet"][:512],
            )
            for row in rows
        ]
