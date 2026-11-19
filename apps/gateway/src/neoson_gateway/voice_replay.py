"""Recibos de voz sem áudio/texto; rejeição atômica entre processos e reinícios."""

import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException

from neoson_core.infrastructure.db import connect, migrate, transaction


class VoiceReplayGuard:
    def __init__(
        self,
        db: Path,
        window_seconds: float = 120,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db, self.window_seconds, self.clock = db, window_seconds, clock
        with closing(connect(db)) as connection:
            migrate(connection)

    def claim(self, request_id: str | None, requested_at: str | None) -> None:
        try:
            if request_id is None or requested_at is None or len(requested_at) > 64:
                raise ValueError
            if len(request_id) > 36:
                raise ValueError
            identifier, timestamp = str(UUID(request_id)), float(requested_at)
            now = self.clock()
            if (
                not math.isfinite(timestamp)
                or abs(now - timestamp) > self.window_seconds
            ):
                raise ValueError
        except (ValueError, OverflowError):
            raise HTTPException(
                400, "ID e horário de voz válidos e recentes são obrigatórios."
            ) from None
        with closing(connect(self.db)) as connection, transaction(connection):
            connection.execute(
                "DELETE FROM voice_requests WHERE expires_at < ?", (now,)
            )
            if connection.execute(
                "SELECT 1 FROM voice_requests WHERE request_id = ?", (identifier,)
            ).fetchone():
                raise HTTPException(
                    409, "Pedido de voz já recebido; não repetir automaticamente."
                )
            if (
                connection.execute("SELECT COUNT(*) FROM voice_requests").fetchone()[0]
                >= 10000
            ):
                raise HTTPException(
                    503, "Limite temporário de recibos de voz atingido."
                )
            try:
                connection.execute(
                    "INSERT INTO voice_requests VALUES (?, ?)",
                    (identifier, timestamp + self.window_seconds),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Pedido de voz já recebido.") from None
