import uvicorn

from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_gateway.app import create_app
from neoson_gateway.composition import (
    api_token,
    build_actions,
    build_engine,
    build_voice,
)


def main() -> None:
    try:
        settings = load_settings()
        token = api_token(settings)
        engine = build_engine(settings)
        voice = build_voice(settings, engine)
    except ConfigError as error:
        raise SystemExit(str(error)) from None

    uvicorn.run(
        create_app(engine, token, actions=build_actions(settings), voice=voice),
        host=settings.gateway_host,
        port=settings.gateway_port,
        log_level=settings.log_level.lower(),
    )
