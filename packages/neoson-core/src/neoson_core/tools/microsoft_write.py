"""Efeitos Graph restritos a criar evento sem convidados e salvar draft sem send."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from neoson_core.engine.actions import canonical, checksum
from neoson_core.engine.integrations import IntegrationError, PersonalEffect
from neoson_core.tools.integration_http import (
    ClientFactory,
    client_factory,
    request_json,
)
from neoson_core.tools.microsoft_read import BASE, AccessTokenSource, text

PROPERTY_PREFIX = "String {667c7a29-d51d-4c23-838b-d24f3ed5f719} Name "
ACTION_PROPERTY = PROPERTY_PREFIX + "NeoSonAction"
SEAL_PROPERTY = PROPERTY_PREFIX + "NeoSonFingerprint"


class MicrosoftPersonalWriter:
    def __init__(
        self,
        oauth: AccessTokenSource,
        *,
        factory: ClientFactory = client_factory,
    ) -> None:
        self.oauth, self.factory = oauth, factory

    async def _create(
        self,
        kind: str,
        payload: dict[str, object],
        action_id: UUID,
    ) -> PersonalEffect:
        seal = checksum(canonical({"kind": kind, "payload": payload}))
        path = "/me/calendar/events" if kind == "calendar" else "/me/messages"
        try:
            async with asyncio.timeout(20):
                bearer = await self.oauth.access_token()
                headers = {"Authorization": "Bearer " + bearer}
                matches = await request_json(
                    self.factory,
                    "GET",
                    BASE + path,
                    headers=headers,
                    params={
                        "$filter": "singleValueExtendedProperties/Any(ep: ep/id eq '"
                        + ACTION_PROPERTY
                        + "' and ep/value eq '"
                        + str(action_id)
                        + "')",
                        "$expand": "singleValueExtendedProperties($filter=id eq '"
                        + SEAL_PROPERTY
                        + "')",
                        "$select": "id" if kind == "calendar" else "id,isDraft",
                        "$top": "2",
                    },
                )
                rows = matches.get("value")
                if (
                    not isinstance(rows, list)
                    or len(rows) > 1
                    or matches.get("@odata.nextLink")
                    or not all(isinstance(row, dict) for row in rows)
                ):
                    raise IntegrationError("idempotency")
                if rows:
                    properties = rows[0].get("singleValueExtendedProperties", [])
                    if (
                        not isinstance(properties, list)
                        or {"id": SEAL_PROPERTY, "value": seal} not in properties
                        or (kind == "draft" and rows[0].get("isDraft") is not True)
                    ):
                        raise IntegrationError("idempotency")
                    return PersonalEffect(text(rows[0].get("id"), 1024))
                body: dict[str, object] = {
                    **payload,
                    "singleValueExtendedProperties": [
                        {"id": ACTION_PROPERTY, "value": str(action_id)},
                        {"id": SEAL_PROPERTY, "value": seal},
                    ],
                }
                if kind == "calendar":
                    body["transactionId"] = str(action_id)
                created = await request_json(
                    self.factory,
                    "POST",
                    BASE + path,
                    headers=headers,
                    json_body=body,
                    success_statuses=(201,),
                )
                if kind == "draft" and created.get("isDraft") is not True:
                    raise IntegrationError("response")
                return PersonalEffect(text(created.get("id"), 1024))
        except TimeoutError:
            raise IntegrationError("timeout") from None

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        action_id: UUID,
    ) -> PersonalEffect:
        return await self._create(
            "calendar",
            {
                "subject": title,
                "start": {
                    "dateTime": start.astimezone(UTC).replace(tzinfo=None).isoformat(),
                    "timeZone": "UTC",
                },
                "end": {
                    "dateTime": end.astimezone(UTC).replace(tzinfo=None).isoformat(),
                    "timeZone": "UTC",
                },
                "attendees": [],
                "isReminderOn": False,
            },
            action_id,
        )

    async def create_draft(
        self,
        recipient: str,
        subject: str,
        body: str,
        action_id: UUID,
    ) -> PersonalEffect:
        return await self._create(
            "draft",
            {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            },
            action_id,
        )
