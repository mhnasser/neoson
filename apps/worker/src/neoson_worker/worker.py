"""Laço do worker (D12): reserva, processa com efeito único e conclui ou reagenda."""

import logging
import random
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from neoson_core.engine import OutboxEvent
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, migrate, transaction

logger = logging.getLogger(__name__)

HandlerFunction = Callable[[sqlite3.Connection, OutboxEvent], None]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class Handler:
    """Efeito de um tipo de evento. Roda dentro da transação que conclui o evento."""

    name: str
    function: HandlerFunction


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_seconds: float = 2.0
    max_seconds: float = 300.0

    def delay(self, attempts: int, jitter: float) -> timedelta:
        """Backoff exponencial com jitter: metade fixa, metade aleatória."""
        ceiling = min(self.max_seconds, self.base_seconds * 2 ** (attempts - 1))
        return timedelta(seconds=ceiling * (0.5 + jitter / 2))


class Worker:
    def __init__(
        self,
        db_path: Path,
        handlers: Mapping[str, Sequence[Handler]],
        worker_id: str,
        lease_seconds: float = 60.0,
        retry: RetryPolicy | None = None,
        clock: Clock | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._db_path = db_path
        self._handlers = {kind: tuple(items) for kind, items in handlers.items()}
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry = retry or RetryPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._jitter = jitter
        with self._connection() as connection:
            migrate(connection)

    def run_once(self) -> int:
        """Processa os eventos prontos agora e devolve quantos foram reservados."""
        with self._connection() as connection:
            leased = outbox.lease(
                connection, self._worker_id, self._clock(), self._lease_seconds
            )
            for item in leased:
                self._process(connection, item)
        return len(leased)

    def run_forever(self, stop: threading.Event, poll_seconds: float) -> None:
        while not stop.is_set():
            if self.run_once() == 0:
                stop.wait(poll_seconds)

    def _process(
        self, connection: sqlite3.Connection, item: outbox.LeasedEvent
    ) -> None:
        event = item.event
        try:
            with transaction(connection):
                for handler in self._handlers.get(event.type, ()):
                    if outbox.has_effect(connection, event.event_id, handler.name):
                        continue
                    handler.function(connection, event)
                    outbox.record_effect(
                        connection, event.event_id, handler.name, self._clock()
                    )
                outbox.complete(connection, event.event_id, self._worker_id)
        except Exception as error:
            retry_at = None
            if item.attempts < self._retry.max_attempts:
                retry_at = self._clock() + self._retry.delay(
                    item.attempts, self._jitter()
                )
            logger.warning(
                "evento %s (%s) falhou na tentativa %d: %s",
                event.event_id,
                event.type,
                item.attempts,
                type(error).__name__,
            )
            with transaction(connection):
                outbox.fail(
                    connection,
                    event.event_id,
                    self._worker_id,
                    f"{type(error).__name__}: {error}",
                    retry_at,
                )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self._db_path)
        try:
            yield connection
        finally:
            connection.close()
