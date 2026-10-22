"""Handler do worker: registra o uso de tokens e o custo de cada execução."""

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from neoson_core.engine import OutboxEvent
from neoson_core.telemetry.pricing import Price, cost_usd

HANDLER_NAME = "telemetry.usage"
EVENT_TYPES = (
    "neoson.run.completed",
    "neoson.run.failed",
    "neoson.run.waiting_approval",
)
DELEGATION_COUNTERS = (
    "attempts",
    "completed",
    "failed",
    "rejected",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
)


class UsageRecorder:
    """Roda dentro da transação do worker; a idempotência fica com `worker_effects`."""

    def __init__(self, prices: Mapping[str, Price] | None = None) -> None:
        self._prices = dict(prices or {})

    def __call__(self, connection: sqlite3.Connection, event: OutboxEvent) -> None:
        if event.run_id is None:
            raise ValueError(f"{event.type} sem run_id")
        usage = event.payload.get("usage")
        if not isinstance(usage, dict):
            raise ValueError(f"{event.type} sem uso de tokens no payload")
        model = event.payload.get("model")
        model = model if isinstance(model, str) else None
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cached_tokens = int(usage.get("cached_tokens", 0))
        # Eventos anteriores ao campo não tinham consumo fora do uso informado.
        complete = event.payload.get("usage_complete") is not False
        cost = cost_usd(model, input_tokens, output_tokens, cached_tokens, self._prices)
        if not complete:
            # Timeout/erro sem medição não é uma chamada gratuita ou custo completo.
            cost = None
        recorded_at = datetime.now(UTC).isoformat(timespec="microseconds")
        connection.execute(
            "INSERT INTO usage_records (run_id, model, input_tokens, output_tokens,"
            " cached_tokens, cost_usd, recorded_at, usage_complete)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event.run_id),
                model,
                input_tokens,
                output_tokens,
                cached_tokens,
                cost,
                recorded_at,
                int(complete),
            ),
        )
        delegation = event.payload.get("delegation")
        if delegation is not None:
            _record_delegation(connection, event, delegation, recorded_at)


def _record_delegation(
    connection: sqlite3.Connection,
    event: OutboxEvent,
    delegation: object,
    recorded_at: str,
) -> None:
    if not isinstance(delegation, dict):
        raise ValueError(f"{event.type} com delegação inválida no payload")
    counters = [delegation.get(name) for name in DELEGATION_COUNTERS]
    complete = delegation.get("usage_complete")
    valid = all(type(n) is int and n >= 0 for n in counters)
    if not valid or type(complete) is not bool:
        raise ValueError(f"{event.type} com contadores de delegação inválidos")
    connection.execute(
        "INSERT INTO delegation_records (run_id, attempts, completed, failed, rejected,"
        " input_tokens, output_tokens, cached_tokens, usage_complete, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(event.run_id), *counters, int(complete), recorded_at),
    )


record_usage = UsageRecorder()
