"""Sandbox local persistente, com chave de efeito única por ação aprovada."""

from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import UUID

from neoson_core.engine.actions import canonical, checksum
from neoson_core.engine.integrations import IntegrationError, PersonalEffect
from neoson_core.infrastructure.db import connect, transaction


class DisabledPersonalWriter:
    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        action_id: UUID,
    ) -> PersonalEffect:
        raise IntegrationError("disabled")

    async def create_draft(
        self,
        recipient: str,
        subject: str,
        body: str,
        action_id: UUID,
    ) -> PersonalEffect:
        raise IntegrationError("disabled")


class SqlitePersonalSandbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _create(
        self, action_id: UUID, kind: str, payload: dict[str, str]
    ) -> PersonalEffect:
        value = canonical({"kind": kind, "payload": payload})
        seal = checksum(value)
        with closing(connect(self.path)) as connection, transaction(connection):
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sandbox_effects ("
                "action_id TEXT PRIMARY KEY,kind TEXT NOT NULL,"
                "fingerprint TEXT NOT NULL,payload TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT fingerprint FROM sandbox_effects WHERE action_id=?",
                (str(action_id),),
            ).fetchone()
            if row is not None:
                if row[0] != seal:
                    raise IntegrationError("idempotency")
            else:
                connection.execute(
                    "INSERT INTO sandbox_effects VALUES(?,?,?,?)",
                    (str(action_id), kind, seal, value),
                )
        return PersonalEffect("sandbox-" + str(action_id), simulated=True)

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        action_id: UUID,
    ) -> PersonalEffect:
        return self._create(
            action_id,
            "calendar",
            {
                "title": title,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )

    async def create_draft(
        self,
        recipient: str,
        subject: str,
        body: str,
        action_id: UUID,
    ) -> PersonalEffect:
        return self._create(
            action_id,
            "draft",
            {
                "recipient": recipient,
                "subject": subject,
                "body": body,
            },
        )
