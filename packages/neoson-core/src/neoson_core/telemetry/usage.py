"""Handler do worker: registra o uso de tokens de cada execução em `usage_records`."""

import sqlite3
from datetime import UTC, datetime

from neoson_core.engine import OutboxEvent

HANDLER_NAME = "telemetry.usage"
EVENT_TYPES = (
    "neoson.run.completed",
    "neoson.run.failed",
    "neoson.run.waiting_approval",
)


def record_usage(connection: sqlite3.Connection, event: OutboxEvent) -> None:
    """Roda dentro da transação do worker; a idempotência fica com `worker_effects`.

    `cost_usd` fica vazio até existir uma tabela de preços por modelo.
    """
    if event.run_id is None:
        raise ValueError(f"{event.type} sem run_id")
    usage = event.payload.get("usage")
    if not isinstance(usage, dict):
        raise ValueError(f"{event.type} sem uso de tokens no payload")
    connection.execute(
        "INSERT INTO usage_records (run_id, model, input_tokens, output_tokens,"
        " cached_tokens, cost_usd, recorded_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
        (
            str(event.run_id),
            event.payload.get("model"),
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            int(usage.get("cached_tokens", 0)),
            datetime.now(UTC).isoformat(timespec="microseconds"),
        ),
    )
