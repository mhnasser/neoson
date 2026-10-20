"""Configuração do robô. Não usa o `neoson-core`: o robô roda na Pi, sem o servidor."""

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8000"
INVALID_GATEWAY_URL = (
    "NEOSON_GATEWAY_URL precisa ser uma URL http(s) com host e porta válidos, "
    f"por exemplo {DEFAULT_GATEWAY_URL}"
)


def gateway_url(
    cli_value: str | None = None, environ: Mapping[str, str] = os.environ
) -> str:
    """URL do gateway: opção de linha de comando > `NEOSON_GATEWAY_URL` > padrão.

    O erro não repete o valor recebido.
    """
    url = cli_value or environ.get("NEOSON_GATEWAY_URL") or DEFAULT_GATEWAY_URL
    try:
        parts = urlsplit(url)
        port = parts.port  # ValueError se não for inteiro ou estiver fora de 0-65535
    except ValueError:
        raise ValueError(INVALID_GATEWAY_URL) from None
    if parts.scheme not in {"http", "https"} or not parts.hostname or port == 0:
        raise ValueError(INVALID_GATEWAY_URL)
    return url.rstrip("/")


def api_token(environ: Mapping[str, str] = os.environ) -> str | None:
    """Token de API do gateway (`NEOSON_API_TOKEN`), só de variável do processo."""
    return environ.get("NEOSON_API_TOKEN") or None
