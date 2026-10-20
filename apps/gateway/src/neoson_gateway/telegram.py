"""Canal Telegram por long polling (RF-01, §12): só remetentes permitidos, em privado.

O token do bot faz parte das URLs da Bot API, então nenhum log inclui URL ou corpo
de erro do cliente HTTP.
"""

import asyncio
import logging
from collections.abc import Collection, Sequence
from typing import Any, Protocol
from uuid import UUID

import httpx2

from neoson_core.engine import Engine, SessionNotFound, UserMessage

logger = logging.getLogger(__name__)

CHANNEL = "telegram"
MAX_MESSAGE_CHARS = 4096
POLL_TIMEOUT_SECONDS = 30
GREETING = (
    "Olá! Sou o NeoSon. Mande uma mensagem para conversar. "
    "Use /nova para começar uma conversa nova."
)
NEW_CONVERSATION = "Certo, começamos uma conversa nova."
TEXT_ONLY = "Por enquanto eu só entendo mensagens de texto."


class TelegramError(Exception):
    def __init__(self, description: str, status_code: int | None = None) -> None:
        super().__init__(description)
        self.status_code = status_code

    @property
    def fatal(self) -> bool:
        """Token inválido ou outro consumidor ativo: não adianta tentar de novo."""
        return self.status_code in (401, 404, 409)


class TelegramApi:
    def __init__(self, http: httpx2.AsyncClient, token: str) -> None:
        self._http = http
        self._base = f"https://api.telegram.org/bot{token}"

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS}
        if offset is not None:
            params["offset"] = offset
        result = await self._call("getUpdates", params, POLL_TIMEOUT_SECONDS + 10)
        return list(result)

    async def send_message(self, chat_id: int, text: str) -> None:
        await self._call("sendMessage", {"chat_id": chat_id, "text": text}, 30)

    async def _call(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        try:
            response = await self._http.post(
                f"{self._base}/{method}", json=payload, timeout=timeout
            )
        except httpx2.HTTPError as error:
            raise TelegramError(f"{method}: {type(error).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(
                f"{method}: resposta não JSON", response.status_code
            ) from None
        if not body.get("ok"):
            description = str(body.get("description", "erro desconhecido"))[:200]
            raise TelegramError(f"{method}: {description}", response.status_code)
        return body.get("result")


class ChannelSessions(Protocol):
    async def get(self, channel: str, external_id: str) -> UUID | None: ...

    async def bind(self, channel: str, external_id: str, session_id: UUID) -> None: ...

    async def forget(self, channel: str, external_id: str) -> None: ...


class TelegramBot:
    def __init__(
        self,
        api: TelegramApi,
        engine: Engine,
        sessions: ChannelSessions,
        allowed_user_ids: Collection[int],
    ) -> None:
        self._api = api
        self._engine = engine
        self._sessions = sessions
        self._allowed = frozenset(allowed_user_ids)

    async def poll_once(self, offset: int | None) -> int | None:
        """Busca e trata as atualizações; devolve o próximo offset (confirmação)."""
        for update in await self._api.get_updates(offset):
            offset = int(update["update_id"]) + 1
            await self.handle(update)
        return offset

    async def run_forever(self, stop: asyncio.Event, retry_seconds: float = 5) -> None:
        offset: int | None = None
        while not stop.is_set():
            try:
                offset = await self.poll_once(offset)
            except TelegramError as error:
                if error.fatal:
                    raise
                logger.warning("Telegram indisponível (%s); nova tentativa", error)
                await asyncio.sleep(retry_seconds)

    async def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private":
            return
        if sender.get("id") not in self._allowed:
            logger.warning(
                "Telegram: remetente não autorizado ignorado (%s)", sender.get("id")
            )
            return

        chat_id = int(chat["id"])
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            await self._reply(chat_id, TEXT_ONLY)
        elif text.strip() == "/start":
            await self._reply(chat_id, GREETING)
        elif text.strip() == "/nova":
            await self._sessions.forget(CHANNEL, str(chat_id))
            await self._reply(chat_id, NEW_CONVERSATION)
        else:
            await self._reply(chat_id, await self._answer(chat_id, text))

    async def _answer(self, chat_id: int, text: str) -> str:
        key = str(chat_id)
        session_id = await self._sessions.get(CHANNEL, key)
        try:
            result = await self._engine.run(
                UserMessage(text=text, session_id=session_id, channel=CHANNEL)
            )
        except SessionNotFound:
            await self._sessions.forget(CHANNEL, key)
            result = await self._engine.run(UserMessage(text=text, channel=CHANNEL))
        if result.session_id is not None and result.session_id != session_id:
            await self._sessions.bind(CHANNEL, key, result.session_id)
        return result.text

    async def _reply(self, chat_id: int, text: str) -> None:
        for part in split_message(text):
            await self._api.send_message(chat_id, part)


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> Sequence[str]:
    """Divide em partes de até `limit` caracteres, preferindo quebras de linha."""
    parts = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    parts.append(rest)
    return parts
