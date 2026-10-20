"""Comando `neoson-telegram`: o bot usa o mesmo motor e o mesmo banco do gateway."""

import asyncio
import logging
import signal

import httpx2

from neoson_core.infrastructure.config import ConfigError, ServerSettings, load_settings
from neoson_core.memory.channel_sessions import SqliteChannelSessions
from neoson_gateway.composition import build_engine
from neoson_gateway.telegram import TelegramApi, TelegramBot, TelegramError


def telegram_settings(settings: ServerSettings) -> tuple[str, list[int]]:
    if settings.telegram_bot_token is None:
        raise ConfigError(
            "NEOSON_TELEGRAM_BOT_TOKEN é obrigatório para o neoson-telegram."
        )
    if not settings.telegram_allowed_users:
        raise ConfigError(
            "NEOSON_TELEGRAM_ALLOWED_USERS é obrigatório: IDs numéricos separados por "
            "vírgula (o bot ignora qualquer outro remetente)."
        )
    return (
        settings.telegram_bot_token.get_secret_value(),
        settings.telegram_allowed_users,
    )


async def serve(settings: ServerSettings) -> None:
    token, allowed = telegram_settings(settings)
    engine = build_engine(settings)
    sessions = SqliteChannelSessions(settings.db_path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C chega como KeyboardInterrupt
            pass
    async with httpx2.AsyncClient() as http:
        bot = TelegramBot(TelegramApi(http, token), engine, sessions, allowed)
        await bot.run_forever(stop)


def main() -> None:
    try:
        settings = load_settings()
        telegram_settings(settings)
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # O httpx2 registra URLs em INFO; a URL da Bot API contém o token.
    for name in ("httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        asyncio.run(serve(settings))
    except TelegramError as error:
        raise SystemExit(f"Telegram recusou o bot: {error}") from None
    except KeyboardInterrupt:
        pass
