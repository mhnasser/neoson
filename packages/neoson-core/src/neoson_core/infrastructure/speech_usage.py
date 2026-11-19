"""Métricas de fala e evento de exportação na mesma transação, sem conteúdo."""

import asyncio
import math
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from neoson_core.engine import OutboxEvent
from neoson_core.engine.voice import SpeechUsage
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, migrate, transaction

SPEECH_USAGE_EVENT = "neoson.voice.usage_recorded"


class SqliteSpeechUsage:
    def __init__(
        self,
        db: Path,
        stt_price_per_minute: float | None,
        tts_price_per_million_chars: float | None,
    ) -> None:
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in (stt_price_per_minute, tts_price_per_million_chars)
        ):
            raise ValueError("Preço de fala inválido.")
        self.db, self.stt_price, self.tts_price = (
            db,
            stt_price_per_minute,
            tts_price_per_million_chars,
        )
        with closing(connect(db)) as connection:
            migrate(connection)

    async def __call__(self, run_id: UUID, usage: SpeechUsage) -> None:
        await asyncio.to_thread(self._record, run_id, usage)

    def _record(self, run_id: UUID, usage: SpeechUsage) -> None:
        cost: float | None = 0.0 if usage.complete else None
        for amount, price in (
            (usage.stt_seconds / 60, self.stt_price),
            (usage.tts_characters / 1000000, self.tts_price),
        ):
            if amount and price is None:
                cost = None
            elif cost is not None and price is not None:
                cost += amount * price
        now = datetime.now(UTC)
        with closing(connect(self.db)) as connection, transaction(connection):
            inserted = connection.execute(
                "INSERT INTO speech_usage_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id) DO NOTHING",
                (
                    str(run_id),
                    usage.stt_provider,
                    usage.tts_provider,
                    usage.stt_seconds,
                    usage.tts_characters,
                    cost,
                    int(usage.complete),
                    now.isoformat(),
                ),
            )
            if inserted.rowcount:
                outbox.insert(
                    connection,
                    OutboxEvent(
                        type=SPEECH_USAGE_EVENT,
                        run_id=run_id,
                        occurred_at=now,
                        payload={"usage_complete": usage.complete},
                    ),
                )
