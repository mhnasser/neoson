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
        cost = cost_usd(model, input_tokens, output_tokens, cached_tokens, self._prices)
        connection.execute(
            "INSERT INTO usage_records (run_id, model, input_tokens, output_tokens,"
            " cached_tokens, cost_usd, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(event.run_id),
                model,
                input_tokens,
                output_tokens,
                cached_tokens,
                cost,
                datetime.now(UTC).isoformat(timespec="microseconds"),
            ),
        )


record_usage = UsageRecorder()
