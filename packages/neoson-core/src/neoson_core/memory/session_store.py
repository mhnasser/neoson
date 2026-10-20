"""Histórico de sessões e transactional outbox em SQLite (ADR-002)."""

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from neoson_core.engine import Message, Role, ToolCall
from neoson_core.engine.persistence import OutboxEvent, RunRecord, SessionNotFound
from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction


class SqliteConversationStore:
    """Implementa `ConversationStore`. Aplica as migrations ao ser criado."""

    def __init__(self, path: Path) -> None:
        self._path = path
        with self._connection() as connection:
            migrate(connection)

    async def create_session(self, channel: str) -> UUID:
        return await asyncio.to_thread(self._create_session, channel)

    async def session_exists(self, session_id: UUID) -> bool:
        return await asyncio.to_thread(self._session_exists, session_id)

    async def load_history(
        self, session_id: UUID, max_messages: int
    ) -> tuple[Message, ...]:
        return await asyncio.to_thread(self._load_history, session_id, max_messages)

    async def record_run(self, record: RunRecord) -> None:
        await asyncio.to_thread(self._record_run, record)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self._path)
        try:
            yield connection
        finally:
            connection.close()

    def _create_session(self, channel: str) -> UUID:
        session_id = uuid4()
        now = now_iso()
        with self._connection() as connection, transaction(connection):
            connection.execute(
                "INSERT INTO sessions (id, channel, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (str(session_id), channel, now, now),
            )
        return session_id

    def _session_exists(self, session_id: UUID) -> bool:
        with self._connection() as connection:
            return _exists(connection, session_id)

    def _load_history(self, session_id: UUID, max_messages: int) -> tuple[Message, ...]:
        sid = str(session_id)
        with self._connection() as connection:
            if not _exists(connection, session_id):
                raise SessionNotFound(sid)
            oldest = connection.execute(
                "SELECT id FROM messages WHERE session_id = ?"
                " ORDER BY id DESC LIMIT 1 OFFSET ?",
                (sid, max(max_messages, 1) - 1),
            ).fetchone()
            start = 0
            if oldest is not None:
                # Recua até a mensagem do usuário que abriu a troca, para não deixar
                # pedidos ou resultados de ferramenta órfãos (pode passar do limite).
                user = connection.execute(
                    "SELECT MAX(id) FROM messages"
                    " WHERE session_id = ? AND role = 'user' AND id <= ?",
                    (sid, oldest["id"]),
                ).fetchone()[0]
                start = user if user is not None else oldest["id"]
            rows = connection.execute(
                "SELECT role, content, tool_calls, tool_call_id FROM messages"
                " WHERE session_id = ? AND id >= ? ORDER BY id",
                (sid, start),
            ).fetchall()
        messages = [_message(row) for row in rows]
        while messages and messages[0].role is not Role.USER:
            messages.pop(0)
        return tuple(messages)

    def _record_run(self, record: RunRecord) -> None:
        with self._connection() as connection, transaction(connection):
            if not _exists(connection, record.session_id):
                raise SessionNotFound(str(record.session_id))
            connection.execute(
                "INSERT INTO runs (id, session_id, status, route_version, model,"
                " input_tokens, output_tokens, cached_tokens, started_at, ended_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record.run_id),
                    str(record.session_id),
                    record.status.value,
                    record.route_version,
                    record.model,
                    record.usage.input_tokens,
                    record.usage.output_tokens,
                    record.usage.cached_tokens,
                    record.started_at.isoformat(),
                    record.ended_at.isoformat(),
                ),
            )
            for message in record.messages:
                connection.execute(
                    "INSERT INTO messages (session_id, run_id, role, content,"
                    " tool_calls, tool_call_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(record.session_id),
                        str(record.run_id),
                        message.role.value,
                        message.content,
                        _tool_calls_json(message),
                        message.tool_call_id,
                        record.ended_at.isoformat(),
                    ),
                )
            for event in record.events:
                _insert_event(connection, event)
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (record.ended_at.isoformat(), str(record.session_id)),
            )


def _exists(connection: sqlite3.Connection, session_id: UUID) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sessions WHERE id = ?", (str(session_id),)
    ).fetchone()
    return row is not None


def _insert_event(connection: sqlite3.Connection, event: OutboxEvent) -> None:
    connection.execute(
        "INSERT INTO outbox_events (id, type, schema_version, occurred_at, run_id,"
        " session_id, correlation_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(event.event_id),
            event.type,
            event.schema_version,
            event.occurred_at.isoformat(),
            _text(event.run_id),
            _text(event.session_id),
            _text(event.correlation_id),
            json.dumps(dict(event.payload), ensure_ascii=False),
        ),
    )


def _tool_calls_json(message: Message) -> str | None:
    if not message.tool_calls:
        return None
    return json.dumps(
        [
            {"id": c.id, "name": c.name, "arguments": dict(c.arguments)}
            for c in message.tool_calls
        ],
        ensure_ascii=False,
    )


def _message(row: sqlite3.Row) -> Message:
    calls = tuple(
        ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
        for c in json.loads(row["tool_calls"] or "[]")
    )
    return Message(
        role=Role(row["role"]),
        content=row["content"],
        tool_calls=calls,
        tool_call_id=row["tool_call_id"],
    )


def _text(value: UUID | None) -> str | None:
    return None if value is None else str(value)
