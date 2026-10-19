import uvicorn

from neoson_core.engine import EchoEngine
from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_gateway.app import create_app


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as error:
        raise SystemExit(str(error)) from None

    uvicorn.run(
        create_app(EchoEngine()),
        host=settings.gateway_host,
        port=settings.gateway_port,
        log_level=settings.log_level.lower(),
    )
