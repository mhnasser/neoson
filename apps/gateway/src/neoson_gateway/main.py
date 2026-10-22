import uvicorn

from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_core.infrastructure.logs import configure_logging
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

    configure_logging(
        settings.log_level,
        "gateway",
        fmt=settings.log_format,
        file=settings.log_file,
        max_bytes=settings.log_max_bytes,
        backups=settings.log_backups,
    )
    # Sem log_config próprio: os logs do uvicorn seguem o formato configurado acima.
    uvicorn.run(
        create_app(engine, token, actions=build_actions(settings), voice=voice),
        host=settings.gateway_host,
        port=settings.gateway_port,
        log_level=settings.log_level.lower(),
        log_config=None,
    )
