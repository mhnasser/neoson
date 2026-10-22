"""HTTP restrito: sem redirect/proxy implícito; corpo e prazo limitados."""

import json
from collections.abc import Callable
from typing import Any

import httpx2

from neoson_core.engine.integrations import IntegrationError

ClientFactory = Callable[[], httpx2.AsyncClient]
MAX_RESPONSE_BYTES = 262144


def client_factory() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)


async def request_json(
    factory: ClientFactory,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    oauth: bool = False,
    json_body: dict[str, object] | None = None,
    success_statuses: tuple[int, ...] = (200,),
) -> dict[str, Any]:
    try:
        async with factory() as http:
            async with http.stream(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
                json=json_body,
                follow_redirects=False,
                timeout=10,
            ) as response:
                if response.status_code == 429:
                    raise IntegrationError("rate_limit")
                if response.status_code in (401, 403):
                    raise IntegrationError("credentials")
                if response.status_code not in success_statuses and not (
                    oauth and response.status_code == 400
                ):
                    raise IntegrationError("provider")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise IntegrationError("response")
                    content.extend(chunk)
        body = json.loads(content)
        if not isinstance(body, dict):
            raise IntegrationError("response")
        return body
    except httpx2.TimeoutException:
        raise IntegrationError("timeout") from None
    except (httpx2.HTTPError, ValueError):
        raise IntegrationError("provider") from None
