"""Configuração do robô. Não usa o `neoson-core`: o robô roda na Pi, sem o servidor."""

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8000"


def gateway_url(
    cli_value: str | None = None, environ: Mapping[str, str] = os.environ
) -> str:
    """URL do gateway: opção de linha de comando > `NEOSON_GATEWAY_URL` > padrão."""
    url = cli_value or environ.get("NEOSON_GATEWAY_URL") or DEFAULT_GATEWAY_URL
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(
            "NEOSON_GATEWAY_URL precisa ser uma URL http(s), "
            f"por exemplo {DEFAULT_GATEWAY_URL}"
        )
    return url.rstrip("/")
