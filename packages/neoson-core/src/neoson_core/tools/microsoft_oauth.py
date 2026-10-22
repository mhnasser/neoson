"""OAuth delegado por device code explícito; sem client secret ou escopo de escrita."""

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from neoson_core.engine.integrations import IntegrationError
from neoson_core.tools.integration_http import (
    ClientFactory,
    client_factory,
    request_json,
)
from neoson_core.tools.oauth_store import LocalTokenStore, OAuthTokens

READ_SCOPES = frozenset({"Calendars.ReadBasic", "Mail.ReadBasic"})
WRITE_SCOPES = frozenset({"Calendars.ReadWrite", "Mail.ReadWrite"})
LOGIN_SCOPES = " ".join(
    ["https://graph.microsoft.com/" + scope for scope in sorted(READ_SCOPES)]
    + ["offline_access"]
)


@dataclass(frozen=True, slots=True)
class DeviceLogin:
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    expires_in: int
    interval: int


class MicrosoftOAuth:
    def __init__(
        self,
        client_id: str,
        tenant: str,
        store: LocalTokenStore,
        *,
        factory: ClientFactory = client_factory,
        clock: Callable[[], float] = time.time,
        write: bool = False,
        expected_binding: str | None = None,
    ) -> None:
        # IDs nunca se tornam componentes arbitrários de URL.
        try:
            UUID(client_id)
            if tenant not in ("common", "consumers", "organizations"):
                UUID(tenant)
        except ValueError:
            raise IntegrationError("configuration") from None
        self.client_id, self.tenant, self.store = client_id, tenant, store
        self.factory, self.clock = factory, clock
        self.base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/"
        self._lock = asyncio.Lock()
        self.scopes = WRITE_SCOPES if write else READ_SCOPES
        self.login_scopes = " ".join(
            ["https://graph.microsoft.com/" + scope for scope in sorted(self.scopes)]
            + ["offline_access"]
        )
        self.expected_binding = expected_binding

    async def _post(self, method: str, data: dict[str, str]) -> dict[str, Any]:
        return await request_json(
            self.factory,
            "POST",
            self.base + method,
            data={"client_id": self.client_id, **data},
            oauth=True,
        )

    async def begin_login(self) -> DeviceLogin:
        async with asyncio.timeout(15):
            body = await self._post("devicecode", {"scope": self.login_scopes})
        try:
            code = body["device_code"]
            user_code = body["user_code"]
            expires, interval = body["expires_in"], body.get("interval", 5)
            if (
                not isinstance(code, str)
                or not 1 <= len(code) <= 8192
                or not isinstance(user_code, str)
                or not 1 <= len(user_code) <= 32
                or not user_code.replace("-", "").isalnum()
                or type(expires) is not int
                or not 1 <= expires <= 1800
                or type(interval) is not int
                or not 1 <= interval <= 60
            ):
                raise ValueError
            return DeviceLogin(code, user_code, expires, interval)
        except (KeyError, TypeError, ValueError):
            raise IntegrationError("credentials") from None

    async def finish_login(self, login: DeviceLogin) -> None:
        interval = login.interval
        try:
            async with asyncio.timeout(login.expires_in):
                while True:
                    await asyncio.sleep(interval)
                    body = await self._post(
                        "token",
                        {
                            "grant_type": (
                                "urn:ietf:params:oauth:grant-type:device_code"
                            ),
                            "device_code": login.device_code,
                        },
                    )
                    error = body.get("error")
                    if error == "authorization_pending":
                        continue
                    if error == "slow_down":
                        interval += 5
                        continue
                    tokens = self._tokens(body)
                    with self.store.exclusive():
                        self.store.save(tokens)
                    return
        except TimeoutError:
            raise IntegrationError("credentials") from None

    async def access_token(self) -> str:
        async with self._lock:
            with self.store.exclusive():
                return await self._access_token_locked()

    async def _access_token_locked(self) -> str:
        tokens = self.store.load()
        if (
            tokens.client_id != self.client_id
            or tokens.tenant != self.tenant
            or not self.scopes <= set(tokens.scopes)
            or not set(tokens.scopes) <= self.scopes | {"offline_access"}
            or (
                self.expected_binding is not None
                and tokens.binding != self.expected_binding
            )
            or not isinstance(tokens.access_token, str)
            or not tokens.access_token
            or not math.isfinite(tokens.expires_at)
        ):
            raise IntegrationError("credentials")
        if tokens.expires_at > self.clock() + 60:
            return tokens.access_token
        if not isinstance(tokens.refresh_token, str) or not tokens.refresh_token:
            raise IntegrationError("credentials")
        body = await self._post(
            "token",
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "scope": self.login_scopes,
            },
        )
        refreshed = self._tokens(body, tokens.refresh_token, tokens.binding)
        self.store.save(refreshed)
        return refreshed.access_token

    def _tokens(
        self,
        body: dict[str, Any],
        previous_refresh: str = "",
        previous_binding: str = "",
    ) -> OAuthTokens:
        try:
            access = body["access_token"]
            refresh = body.get("refresh_token", previous_refresh)
            expires = body["expires_in"]
            scopes = frozenset(
                scope.removeprefix("https://graph.microsoft.com/")
                for scope in body["scope"].split()
            )
            if (
                body.get("token_type", "").lower() != "bearer"
                or not self.scopes <= scopes
                or not scopes <= self.scopes | {"offline_access"}
                or not isinstance(access, str)
                or not 1 <= len(access) <= 32768
                or not isinstance(refresh, str)
                or not 1 <= len(refresh) <= 32768
                or type(expires) is not int
                or not 60 < expires <= 86400
            ):
                raise ValueError
            return OAuthTokens(
                self.client_id,
                self.tenant,
                access,
                refresh,
                self.clock() + expires,
                tuple(sorted(scopes)),
                previous_binding or str(uuid4()),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise IntegrationError("credentials") from None
