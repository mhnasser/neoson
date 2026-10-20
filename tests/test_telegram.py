import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest

from neoson_core.engine import (
    RunResult,
    RunStatus,
    SessionNotFound,
    UserMessage,
)
from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_core.memory.channel_sessions import SqliteChannelSessions
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_gateway.telegram import (
    GREETING,
    NEW_CONVERSATION,
    TEXT_ONLY,
    TelegramApi,
    TelegramBot,
    TelegramError,
    split_message,
)
from neoson_gateway.telegram_main import telegram_settings

TOKEN = "123:token-ficticio"
ALLOWED = 1001


class FakeBotApi:
    """Bot API simulada: devolve as atualizações dadas e guarda o que foi enviado."""

    def __init__(self, updates: list[dict[str, Any]], status: int = 200) -> None:
        self.updates = updates
        self.status = status
        self.sent: list[tuple[int, str]] = []
        self.offsets: list[int | None] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if self.status != 200:
            return httpx2.Response(
                self.status, json={"ok": False, "description": "Unauthorized"}
            )
        if method == "getUpdates":
            self.offsets.append(body.get("offset"))
            result, self.updates = self.updates, []
            return httpx2.Response(200, json={"ok": True, "result": result})
        self.sent.append((body["chat_id"], body["text"]))
        return httpx2.Response(200, json={"ok": True, "result": {}})


class FakeEngine:
    def __init__(self, sessions_exist: bool = True, text: str = "resposta") -> None:
        self.received: list[UserMessage] = []
        self.sessions_exist = sessions_exist
        self.text = text
        self.session = uuid4()

    async def run(self, message: UserMessage) -> RunResult:
        self.received.append(message)
        if message.session_id is not None and not self.sessions_exist:
            raise SessionNotFound(str(message.session_id))
        return RunResult(
            run_id=uuid4(),
            text=self.text,
            status=RunStatus.COMPLETED,
            session_id=message.session_id or self.session,
        )


class MemorySessions:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], UUID] = {}

    async def get(self, channel: str, external_id: str) -> UUID | None:
        return self.data.get((channel, external_id))

    async def bind(self, channel: str, external_id: str, session_id: UUID) -> None:
        self.data[(channel, external_id)] = session_id

    async def forget(self, channel: str, external_id: str) -> None:
        self.data.pop((channel, external_id), None)


def update(
    update_id: int,
    text: str | None = "oi",
    user_id: int = ALLOWED,
    chat_type: str = "private",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "from": {"id": user_id, "is_bot": False, "first_name": "Teste"},
        "chat": {"id": user_id, "type": chat_type},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def bot(
    api: FakeBotApi, engine: FakeEngine | None = None, sessions: object = None
) -> tuple[TelegramBot, FakeEngine, MemorySessions]:
    engine = engine or FakeEngine()
    store = sessions if isinstance(sessions, MemorySessions) else MemorySessions()
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(api))
    return (
        TelegramBot(TelegramApi(http, TOKEN), engine, store, [ALLOWED]),
        engine,
        store,
    )


def test_allowed_private_message_is_answered_and_continues_the_session() -> None:
    api = FakeBotApi([update(10, "Olá"), update(11, "E agora?")])
    telegram, engine, sessions = bot(api)

    offset = asyncio.run(telegram.poll_once(None))

    assert offset == 12
    assert api.sent == [(ALLOWED, "resposta"), (ALLOWED, "resposta")]
    first, second = engine.received
    assert (first.text, first.session_id, first.channel) == ("Olá", None, "telegram")
    assert second.session_id == engine.session
    assert sessions.data == {("telegram", str(ALLOWED)): engine.session}


@pytest.mark.parametrize(
    "item",
    [
        update(1, user_id=999),
        update(1, chat_type="group"),
        {"update_id": 1, "edited_message": {}},
    ],
)
def test_unknown_senders_groups_and_other_updates_are_ignored(
    item: dict[str, Any],
) -> None:
    api = FakeBotApi([item])
    telegram, engine, _ = bot(api)

    asyncio.run(telegram.poll_once(None))

    assert engine.received == []
    assert api.sent == []


def test_commands_and_non_text_messages() -> None:
    sessions = MemorySessions()
    sessions.data[("telegram", str(ALLOWED))] = uuid4()
    api = FakeBotApi([update(1, "/start"), update(2, "/nova"), update(3, None)])
    telegram, engine, _ = bot(api, sessions=sessions)

    asyncio.run(telegram.poll_once(None))

    assert [text for _, text in api.sent] == [GREETING, NEW_CONVERSATION, TEXT_ONLY]
    assert sessions.data == {}
    assert engine.received == []


def test_stale_session_starts_a_new_one() -> None:
    sessions = MemorySessions()
    sessions.data[("telegram", str(ALLOWED))] = uuid4()
    engine = FakeEngine(sessions_exist=False)
    api = FakeBotApi([update(1, "oi")])
    telegram, _, _ = bot(api, engine=engine, sessions=sessions)

    asyncio.run(telegram.poll_once(None))

    assert [m.session_id is None for m in engine.received] == [False, True]
    assert sessions.data == {("telegram", str(ALLOWED)): engine.session}


def test_long_replies_are_split_within_the_telegram_limit() -> None:
    engine = FakeEngine(text=("linha\n" * 1000).strip())
    api = FakeBotApi([update(1, "conte")])
    telegram, _, _ = bot(api, engine=engine)

    asyncio.run(telegram.poll_once(None))

    assert len(api.sent) == 2
    assert all(len(text) <= 4096 for _, text in api.sent)
    assert "".join(text + "\n" for _, text in api.sent).count("linha") == 1000


def test_split_without_line_breaks_cuts_at_the_limit() -> None:
    assert split_message("a" * 10, limit=4) == ["aaaa", "aaaa", "aa"]


def test_offset_confirms_processed_updates() -> None:
    api = FakeBotApi([update(5)])
    telegram, _, _ = bot(api)

    next_offset = asyncio.run(telegram.poll_once(None))
    asyncio.run(telegram.poll_once(next_offset))

    assert api.offsets == [None, 6]


@pytest.mark.parametrize(("status", "fatal"), [(401, True), (409, True), (502, False)])
def test_api_errors_are_classified_without_leaking_the_token(
    status: int, fatal: bool
) -> None:
    telegram, _, _ = bot(FakeBotApi([], status=status))

    with pytest.raises(TelegramError) as caught:
        asyncio.run(telegram.poll_once(None))

    assert caught.value.fatal is fatal
    assert TOKEN not in str(caught.value)


def test_network_errors_do_not_leak_the_token() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"falhou {request.url}", request=request)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    api = TelegramApi(http, TOKEN)

    with pytest.raises(TelegramError) as caught:
        asyncio.run(api.get_updates(None))

    assert TOKEN not in str(caught.value)
    assert not caught.value.fatal


def test_run_forever_retries_temporary_errors_and_stops_on_fatal() -> None:
    calls: list[int] = []

    def flaky(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        status = 502 if len(calls) == 1 else 401
        return httpx2.Response(status, json={"ok": False, "description": "x"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(flaky))
    telegram = TelegramBot(
        TelegramApi(http, TOKEN), FakeEngine(), MemorySessions(), [1]
    )

    with pytest.raises(TelegramError) as caught:
        asyncio.run(telegram.run_forever(asyncio.Event(), retry_seconds=0))

    assert caught.value.status_code == 401
    assert len(calls) == 2


# --- sessões por chat no SQLite e configuração ---


def test_channel_sessions_are_persisted(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    session = asyncio.run(SqliteConversationStore(db).create_session("telegram"))
    sessions = SqliteChannelSessions(db)

    asyncio.run(sessions.bind("telegram", "42", session))
    reopened = SqliteChannelSessions(db)

    assert asyncio.run(reopened.get("telegram", "42")) == session
    asyncio.run(reopened.forget("telegram", "42"))
    assert asyncio.run(reopened.get("telegram", "42")) is None


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("clean_env")
def test_allowed_users_are_comma_separated_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEOSON_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("NEOSON_TELEGRAM_ALLOWED_USERS", "1001, 2002")

    token, allowed = telegram_settings(load_settings(env_file=None))

    assert (token, allowed) == (TOKEN, [1001, 2002])


@pytest.mark.usefixtures("clean_env")
def test_telegram_requires_token_and_allowed_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigError, match="NEOSON_TELEGRAM_BOT_TOKEN"):
        telegram_settings(load_settings(env_file=None))
    monkeypatch.setenv("NEOSON_TELEGRAM_BOT_TOKEN", TOKEN)
    with pytest.raises(ConfigError, match="NEOSON_TELEGRAM_ALLOWED_USERS"):
        telegram_settings(load_settings(env_file=None))
    monkeypatch.setenv("NEOSON_TELEGRAM_ALLOWED_USERS", "abc")
    with pytest.raises(ConfigError, match="NEOSON_TELEGRAM_ALLOWED_USERS"):
        load_settings(env_file=None)
