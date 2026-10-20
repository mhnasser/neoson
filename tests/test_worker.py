import asyncio
import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from neoson_core.engine import (
    Message,
    OutboxEvent,
    Role,
    RunRecord,
    RunStatus,
    Usage,
)
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.telemetry import usage
from neoson_worker.main import build_handlers, main
from neoson_worker.worker import Handler, RetryPolicy, Worker

START = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "neoson.db"


def add_run(db: Path, event_type: str = "neoson.run.completed") -> UUID:
    """Grava um run com seu evento, como o Agent Loop faz, e devolve o id do evento."""
    store = SqliteConversationStore(db)
    session = asyncio.run(store.create_session("http"))
    run_id = uuid4()
    event = OutboxEvent(
        type=event_type,
        run_id=run_id,
        session_id=session,
        payload={
            "model": "modelo-x",
            "usage": {"input_tokens": 30, "output_tokens": 7, "cached_tokens": 4},
        },
    )
    asyncio.run(
        store.record_run(
            RunRecord(
                run_id=run_id,
                session_id=session,
                status=RunStatus.COMPLETED,
                started_at=START,
                ended_at=START,
                messages=(
                    Message(role=Role.USER, content="oi"),
                    Message(role=Role.ASSISTANT, content="olá"),
                ),
                usage=Usage(30, 7, 4),
                events=(event,),
            )
        )
    )
    return event.event_id


def query(db: Path, sql: str) -> list[tuple[object, ...]]:
    connection = connect(db)
    try:
        return [tuple(row) for row in connection.execute(sql).fetchall()]
    finally:
        connection.close()


def event_state(db: Path, event_id: UUID) -> tuple[object, ...]:
    connection = connect(db)
    try:
        row = connection.execute(
            "SELECT status, attempts, worker_id, retry_at, last_error"
            " FROM outbox_events WHERE id = ?",
            (str(event_id),),
        ).fetchone()
        return tuple(row)
    finally:
        connection.close()


def worker(
    db: Path,
    clock: Clock,
    name: str = "w1",
    handlers: Mapping[str, Sequence[Handler]] | None = None,
    retry: RetryPolicy | None = None,
) -> Worker:
    return Worker(
        db_path=db,
        handlers=build_handlers() if handlers is None else handlers,
        worker_id=name,
        lease_seconds=30,
        retry=retry,
        clock=clock,
        jitter=lambda: 0.0,
    )


def test_completed_run_event_becomes_a_usage_record(db: Path) -> None:
    event_id = add_run(db)

    assert worker(db, Clock()).run_once() == 1

    assert query(
        db,
        "SELECT model, input_tokens, output_tokens, cached_tokens FROM usage_records",
    ) == [("modelo-x", 30, 7, 4)]
    assert event_state(db, event_id)[:3] == ("completed", 1, "w1")
    assert query(db, "SELECT handler FROM worker_effects") == [(usage.HANDLER_NAME,)]
    assert worker(db, Clock()).run_once() == 0


def test_events_without_handler_are_completed_without_effects(db: Path) -> None:
    event_id = add_run(db, "neoson.tool.executed")

    worker(db, Clock()).run_once()

    assert event_state(db, event_id)[0] == "completed"
    assert query(db, "SELECT COUNT(*) FROM worker_effects") == [(0,)]


def failing(connection: sqlite3.Connection, event: OutboxEvent) -> None:
    raise RuntimeError("serviço fora do ar")


def test_failures_are_retried_with_backoff_then_dead_lettered(db: Path) -> None:
    event_id = add_run(db)
    clock = Clock()
    handlers = {"neoson.run.completed": [Handler("falha", failing)]}
    flaky = worker(db, clock, handlers=handlers, retry=RetryPolicy(max_attempts=3))

    flaky.run_once()
    status, attempts, _, retry_at, error = event_state(db, event_id)
    assert (status, attempts) == ("pending", 1)
    assert retry_at == outbox.iso(START + timedelta(seconds=1))  # 2 s × 2⁰ × 0,5
    assert error == "RuntimeError: serviço fora do ar"

    assert flaky.run_once() == 0  # antes do retry_at
    clock.advance(1)
    flaky.run_once()
    assert event_state(db, event_id)[:2] == ("pending", 2)
    clock.advance(2)
    flaky.run_once()

    assert event_state(db, event_id)[:2] == ("dead_letter", 3)
    clock.advance(3600)
    assert flaky.run_once() == 0


def test_backoff_is_exponential_with_jitter_and_capped() -> None:
    policy = RetryPolicy(base_seconds=2, max_seconds=60)

    assert policy.delay(1, 0.0) == timedelta(seconds=1)
    assert policy.delay(1, 1.0) == timedelta(seconds=2)
    assert policy.delay(3, 1.0) == timedelta(seconds=8)
    assert policy.delay(10, 1.0) == timedelta(seconds=60)


def test_event_of_a_crashed_worker_is_taken_over_after_the_lease(db: Path) -> None:
    event_id = add_run(db)
    clock = Clock()
    connection = connect(db)
    try:
        (crashed,) = outbox.lease(connection, "caiu", clock(), lease_seconds=30)
    finally:
        connection.close()

    survivor = worker(db, clock, name="w2")
    assert survivor.run_once() == 0  # lease ainda válido
    clock.advance(31)
    assert survivor.run_once() == 1

    assert crashed.event.event_id == event_id
    assert event_state(db, event_id)[:3] == ("completed", 2, "w2")
    assert query(db, "SELECT COUNT(*) FROM usage_records") == [(1,)]
    connection = connect(db)
    try:
        assert not outbox.complete(connection, event_id, "caiu")
    finally:
        connection.close()


def test_redelivered_event_does_not_repeat_the_effect(db: Path) -> None:
    event_id = add_run(db)
    worker(db, Clock()).run_once()
    connection = connect(db)
    try:
        connection.execute(
            "UPDATE outbox_events SET status = 'pending' WHERE id = ?", (str(event_id),)
        )
    finally:
        connection.close()

    worker(db, Clock()).run_once()

    assert event_state(db, event_id)[:2] == ("completed", 2)
    assert query(db, "SELECT COUNT(*) FROM usage_records") == [(1,)]


def test_usage_handler_rejects_events_without_usage(db: Path) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError):
            usage.record_usage(connection, OutboxEvent(type="neoson.run.completed"))
        with pytest.raises(ValueError):
            usage.record_usage(
                connection, OutboxEvent(type="neoson.run.completed", run_id=uuid4())
            )
    finally:
        connection.close()


def test_main_once_processes_the_configured_database(
    db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_DB_PATH", str(db))
    add_run(db)

    assert main(["--once"]) == 0

    assert query(db, "SELECT COUNT(*) FROM usage_records") == [(1,)]
