"""Graph v1.0: apenas GET em agenda padrão e metadados da caixa de entrada."""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from neoson_core.engine.integrations import (
    CalendarItem,
    IntegrationError,
    MailItem,
    ReadPage,
)
from neoson_core.tools.integration_http import (
    ClientFactory,
    client_factory,
    request_json,
)

BASE = "https://graph.microsoft.com/v1.0"
CALENDAR_PATH = "/me/calendar/calendarView"
MAIL_PATH = "/me/mailFolders/inbox/messages"
READ_TIMEOUT_SECONDS = 20.0


class AccessTokenSource(Protocol):
    async def access_token(self) -> str: ...


def text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise IntegrationError("response")
    return value


def timestamp(value: object, *, utc_if_naive: bool = False) -> datetime:
    raw = text(value, 64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if not utc_if_naive:
                raise ValueError
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        raise IntegrationError("response") from None


class MicrosoftPersonalReader:
    def __init__(
        self,
        oauth: AccessTokenSource,
        zone: ZoneInfo,
        *,
        factory: ClientFactory = client_factory,
    ) -> None:
        self.oauth, self.zone, self.factory = oauth, zone, factory

    async def _pages(
        self,
        path: str,
        params: dict[str, str],
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not 1 <= limit <= 50:
            raise IntegrationError("request")
        try:
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                bearer = await self.oauth.access_token()
                url = BASE + path
                original_params = params
                query: dict[str, str] | None = params
                seen: set[str] = set()
                items: list[dict[str, Any]] = []
                for _ in range(3):
                    if url in seen:
                        raise IntegrationError("response")
                    seen.add(url)
                    body = await request_json(
                        self.factory,
                        "GET",
                        url,
                        headers={"Authorization": "Bearer " + bearer},
                        params=query,
                    )
                    rows = body.get("value")
                    if not isinstance(rows, list) or not all(
                        isinstance(r, dict) for r in rows
                    ):
                        raise IntegrationError("response")
                    remaining = limit - len(items)
                    items.extend(rows[:remaining])
                    next_link = body.get("@odata.nextLink")
                    if len(rows) > remaining or (next_link and len(items) >= limit):
                        return items, True
                    if next_link is None:
                        return items, False
                    self._next_link(next_link, path, original_params)
                    url, query = next_link, None
                return items, True
        except TimeoutError:
            raise IntegrationError("timeout") from None

    @staticmethod
    def _next_link(link: object, path: str, params: dict[str, str]) -> None:
        link = text(link, 8192)
        parts = urlsplit(link)
        # Somente o mesmo endpoint recebe bearer; parâmetros fixos não podem mudar.
        if (
            parts.scheme != "https"
            or parts.netloc != "graph.microsoft.com"
            or parts.path != "/v1.0" + path
            or parts.fragment
        ):
            raise IntegrationError("response")
        values = parse_qsl(parts.query, keep_blank_values=True)
        keys = [key for key, _ in values]
        if len(keys) != len(set(keys)) or not values:
            raise IntegrationError("response")
        for key, value in values:
            if key in ("$skiptoken", "$skip"):
                continue
            if key not in params or value != params[key]:
                raise IntegrationError("response")
        if any(dict(values).get(key) != value for key, value in params.items()):
            raise IntegrationError("response")

    async def list_events(self, day: date, limit: int) -> ReadPage[CalendarItem]:
        start = datetime.combine(day, time(), self.zone)
        end = datetime.combine(day + timedelta(days=1), time(), self.zone)
        rows, truncated = await self._pages(
            CALENDAR_PATH,
            {
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$select": "id,subject,start,end",
                "$orderby": "start/dateTime",
                "$top": str(limit),
            },
            limit,
        )
        try:
            if any(
                row[part].get("timeZone") != "UTC"
                for row in rows
                for part in ("start", "end")
            ):
                raise IntegrationError("response")
            events = tuple(
                CalendarItem(
                    text(row["id"], 1024),
                    text(row.get("subject", ""), 1024),
                    timestamp(row["start"]["dateTime"], utc_if_naive=True),
                    timestamp(row["end"]["dateTime"], utc_if_naive=True),
                )
                for row in rows
            )
            if any(e.end < e.start for e in events):
                raise IntegrationError("response")
            return ReadPage(events, truncated)
        except (KeyError, TypeError, AttributeError):
            raise IntegrationError("response") from None

    async def list_mail(self, limit: int) -> ReadPage[MailItem]:
        rows, truncated = await self._pages(
            MAIL_PATH,
            {
                "$select": "id,subject,from,receivedDateTime",
                "$orderby": "receivedDateTime desc",
                "$top": str(limit),
            },
            limit,
        )
        try:
            mail = tuple(
                MailItem(
                    text(row["id"], 1024),
                    text(row.get("subject", ""), 1024),
                    text(
                        row.get("from", {}).get("emailAddress", {}).get("name", ""), 256
                    ),
                    timestamp(row["receivedDateTime"]),
                )
                for row in rows
            )
            return ReadPage(mail, truncated)
        except (KeyError, TypeError, AttributeError):
            raise IntegrationError("response") from None
