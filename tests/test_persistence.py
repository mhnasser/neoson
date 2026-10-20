import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from neoson_core.engine import (
    Message,
    OutboxEvent,
    Role,
    RunRecord,
    RunStatus,
    SessionNotFound,
    ToolCall,
    Usage,
)
from neoson_core.infrastructure.db import (
    backup,
    connect,
    integrity_ok,
    migrate,
    migrations,
    schema_version,
)
from neoson_core.memory.session_store import SqliteConversationStore

STARTED = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "dados" / "neoson.db"


@pytest.fixture
def store(db_path: Path) -> SqliteConversationStore:
    return SqliteConversationStore(db_path)


def count(db_path: Path, table: str) -> int:
    connection = connect(db_path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def record(session_id: object, *messages: Message, **overrides: object) -> RunRecord:
    run_id = uuid4()
    values: dict[str, object] = {
        "run_id": run_id,
        "session_id": session_id,
        "status": RunStatus.COMPLETED,
        "started_at": STARTED,
        "ended_at": STARTED + timedelta(seconds=2),
        "messages": messages
        or (
            Message(role=Role.USER, content="oi"),
            Message(role=Role.ASSISTANT, content="olá"),
        ),
        "usage": Usage(20, 5, 3),
        "model": "modelo-x",
        "route_version": "rules-v1",
        "events": (
            OutboxEvent(
                type="neoson.run.completed",
                payload={"outcome": "success"},
                run_id=run_id,
                session_id=session_id,  # type: ignore[arg-type]
            ),
        ),
    }
    return RunRecord(**(values | overrides))  # type: ignore[arg-type]


# --- migrations ---


def test_migrations_are_sequential_and_applied_once(db_path: Path) -> None:
    connection = connect(db_path)
    try:
        first = migrate(connection)
        second = migrate(connection)
        version = schema_version(connection)
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()

    assert first == [m.version for m in migrations()] == [1]
    assert second == []
    assert version == 1
    assert (journal, foreign_keys) == ("wal", 1)
    assert {
        "sessions",
        "runs",
        "messages",
        "outbox_events",
        "schema_migrations",
    } <= tables


def test_existing_database_keeps_its_data_when_migrated_again(
    db_path: Path,
) -> None:
    store = SqliteConversationStore(db_path)
    session = asyncio.run(store.create_session("cli"))

    reopened = SqliteConversationStore(db_path)

    assert asyncio.run(reopened.session_exists(session))


# --- sessões e histórico ---


def test_history_round_trip_with_tool_calls(store: SqliteConversationStore) -> None:
    session = asyncio.run(store.create_session("http"))
    call = ToolCall(id="call-1", name="calendar_list", arguments={"day": "amanhã"})
    messages = (
        Message(role=Role.USER, content="Agenda de amanhã?"),
        Message(role=Role.ASSISTANT, content="", tool_calls=(call,)),
        Message(role=Role.TOOL, content="2 eventos", tool_call_id="call-1"),
        Message(role=Role.ASSISTANT, content="Você tem 2 eventos."),
    )

    asyncio.run(store.record_run(record(session, *messages)))

    assert asyncio.run(store.load_history(session, 50)) == messages


def test_history_is_limited_and_starts_at_a_user_message(
    store: SqliteConversationStore,
) -> None:
    session = asyncio.run(store.create_session("http"))
    call = ToolCall(id="c", name="t")
    asyncio.run(store.record_run(record(session)))
    asyncio.run(
        store.record_run(
            record(
                session,
                Message(role=Role.USER, content="segunda"),
                Message(role=Role.ASSISTANT, content="", tool_calls=(call,)),
                Message(role=Role.TOOL, content="r", tool_call_id="c"),
                Message(role=Role.ASSISTANT, content="fim"),
            )
        )
    )

    def contents(limit: int) -> list[str]:
        return [m.content for m in asyncio.run(store.load_history(session, limit))]

    second_exchange = ["segunda", "", "r", "fim"]
    # As 3 últimas começam no pedido de ferramenta: o recorte recua até "segunda".
    assert contents(3) == second_exchange
    assert contents(4) == second_exchange
    # O recorte de 5 começa em "olá": recua até "oi", que abriu aquela troca.
    assert contents(5) == ["oi", "olá", *second_exchange]
    assert contents(50) == ["oi", "olá", *second_exchange]


def test_unknown_session_is_reported(store: SqliteConversationStore) -> None:
    with pytest.raises(SessionNotFound):
        asyncio.run(store.load_history(uuid4(), 10))
    assert not asyncio.run(store.session_exists(uuid4()))


# --- gravação atômica (transactional outbox) ---


def test_run_messages_and_event_are_stored_together(
    store: SqliteConversationStore, db_path: Path
) -> None:
    session = asyncio.run(store.create_session("http"))

    asyncio.run(store.record_run(record(session)))

    assert (count(db_path, "runs"), count(db_path, "messages")) == (1, 2)
    connection = connect(db_path)
    try:
        event = connection.execute(
            "SELECT type, status, attempts, payload FROM outbox_events"
        ).fetchone()
        run = connection.execute(
            "SELECT input_tokens, cached_tokens, model, route_version FROM runs"
        ).fetchone()
    finally:
        connection.close()
    assert tuple(event) == (
        "neoson.run.completed",
        "pending",
        0,
        '{"outcome": "success"}',
    )
    assert tuple(run) == (20, 3, "modelo-x", "rules-v1")


def test_failure_while_writing_the_event_rolls_everything_back(
    store: SqliteConversationStore, db_path: Path
) -> None:
    session = asyncio.run(store.create_session("http"))
    broken = OutboxEvent(type="neoson.run.completed", payload={"x": object()})

    with pytest.raises(TypeError):
        asyncio.run(store.record_run(record(session, events=(broken,))))

    assert (
        count(db_path, "runs"),
        count(db_path, "messages"),
        count(db_path, "outbox_events"),
    ) == (0, 0, 0)


def test_duplicate_run_is_rejected_without_partial_writes(
    store: SqliteConversationStore, db_path: Path
) -> None:
    session = asyncio.run(store.create_session("http"))
    first = record(session)
    asyncio.run(store.record_run(first))

    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(store.record_run(record(session, run_id=first.run_id)))

    assert (count(db_path, "runs"), count(db_path, "messages")) == (1, 2)
    assert count(db_path, "outbox_events") == 1


def test_run_for_an_unknown_session_writes_nothing(
    store: SqliteConversationStore, db_path: Path
) -> None:
    with pytest.raises(SessionNotFound):
        asyncio.run(store.record_run(record(uuid4())))

    assert count(db_path, "runs") == 0


# --- backup e restauração ---


def test_backup_is_consistent_and_restorable(
    store: SqliteConversationStore, db_path: Path, tmp_path: Path
) -> None:
    session = asyncio.run(store.create_session("http"))
    asyncio.run(store.record_run(record(session)))
    target = tmp_path / "backup" / "neoson-backup.db"

    backup(db_path, target)

    assert integrity_ok(target)
    restored = SqliteConversationStore(target)
    assert asyncio.run(restored.load_history(session, 10)) == asyncio.run(
        store.load_history(session, 10)
    )


# --- tipos da porta ---


def test_outbox_event_payload_is_read_only_and_time_aware() -> None:
    event = OutboxEvent(type="neoson.run.completed", payload={"a": 1})

    with pytest.raises(TypeError):
        event.payload["a"] = 2  # type: ignore[index]
    with pytest.raises(ValueError):
        OutboxEvent(type="x", occurred_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        OutboxEvent(type=" ")
