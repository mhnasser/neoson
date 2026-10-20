import asyncio
import json
from datetime import date, time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMError,
    LLMResponse,
    RiskLevel,
    Role,
    RunStatus,
    SessionNotFound,
    ToolContext,
    UserMessage,
)
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.calendar_fake import CalendarEvent, FakeCalendar
from neoson_core.tools.registry import Tool, ToolOutput, ToolRegistry
from neoson_gateway.app import create_app

TOKEN = "token-de-teste"


class Note(BaseModel):
    text: str


class SendEmail(Tool[Note]):
    name = "email_send"
    description = "Envia um e-mail."
    risk = RiskLevel.SENSITIVE_WRITE
    arguments = Note

    async def run(self, arguments: Note, context: ToolContext) -> ToolOutput:
        raise AssertionError("não deveria executar sem aprovação")


def agent(
    db: Path, script: list[LLMResponse | LLMError]
) -> tuple[AgentLoop, ScriptedProvider]:
    provider = ScriptedProvider(script)
    calendar = FakeCalendar.fixed(
        {date(2026, 10, 21): (CalendarEvent(time(10, 0), "Dentista", 60),)}
    )
    loop = AgentLoop(
        provider=provider,
        router=RulesRouter("balanced"),
        budget=Budget(max_steps=4, timeout_seconds=5),
        tools=ToolRegistry([calendar, SendEmail()]),
        store=SqliteConversationStore(db),
        history_max_messages=40,
    )
    return loop, provider


def rows(db: Path, sql: str) -> list[tuple[object, ...]]:
    connection = connect(db)
    try:
        return [tuple(r) for r in connection.execute(sql).fetchall()]
    finally:
        connection.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "neoson.db"


def test_conversation_continues_with_history_and_survives_a_restart(db: Path) -> None:
    first_loop, _ = agent(db, [reply("Anotado: 42.")])
    first = asyncio.run(first_loop.run(UserMessage(text="Meu número favorito é 42.")))

    # "Reinício": novo armazenamento e novo motor sobre o mesmo banco.
    second_loop, provider = agent(db, [reply("É 42.")])
    second = asyncio.run(
        second_loop.run(
            UserMessage(text="Qual é meu número favorito?", session_id=first.session_id)
        )
    )

    assert first.session_id is not None
    assert second.session_id == first.session_id
    sent = [(m.role, m.content) for m in provider.requests[0].messages[1:]]
    assert sent == [
        (Role.USER, "Meu número favorito é 42."),
        (Role.ASSISTANT, "Anotado: 42."),
        (Role.USER, "Qual é meu número favorito?"),
    ]


def test_each_run_is_recorded_with_its_event(db: Path) -> None:
    loop, _ = agent(
        db,
        [
            call_tool("calendar_list", {"day": "2026-10-21"}, "c1"),
            reply("Dentista às 10h."),
        ],
    )

    result = asyncio.run(loop.run(UserMessage(text="Agenda de 2026-10-21?")))

    assert rows(db, "SELECT role FROM messages ORDER BY id") == [
        ("user",),
        ("assistant",),
        ("tool",),
        ("assistant",),
    ]
    ((status, route, model),) = rows(
        db, "SELECT status, route_version, model FROM runs"
    )
    assert (status, route, model) == ("completed", "rules-v1", "fake-model")
    ((kind, run_id, payload),) = rows(
        db, "SELECT type, run_id, payload FROM outbox_events"
    )
    assert (kind, run_id) == ("neoson.run.completed", str(result.run_id))
    assert json.loads(str(payload))["usage"] == {
        "input_tokens": 20,
        "output_tokens": 10,
        "cached_tokens": 0,
    }


def test_suspended_run_keeps_the_history_without_orphan_tool_calls(db: Path) -> None:
    loop, _ = agent(db, [call_tool("email_send", {"text": "oi"}, "e1")])
    suspended = asyncio.run(loop.run(UserMessage(text="Mande um e-mail")))

    next_loop, provider = agent(db, [reply("Ok.")])
    asyncio.run(
        next_loop.run(UserMessage(text="Deixa pra lá", session_id=suspended.session_id))
    )

    assert suspended.status is RunStatus.NEEDS_APPROVAL
    history = provider.requests[0].messages[1:-1]
    assert [(m.role, bool(m.tool_calls)) for m in history] == [
        (Role.USER, False),
        (Role.ASSISTANT, False),
    ]
    assert rows(db, "SELECT type FROM outbox_events ORDER BY rowid")[0] == (
        "neoson.run.waiting_approval",
    )


def test_failed_run_is_recorded_with_the_safe_message(db: Path) -> None:
    loop, _ = agent(db, [LLMError("interno", retryable=True)])

    result = asyncio.run(loop.run(UserMessage(text="oi")))

    assert result.status is RunStatus.FAILED
    assert rows(db, "SELECT role, content FROM messages ORDER BY id") == [
        ("user", "oi"),
        ("assistant", result.text),
    ]
    assert rows(db, "SELECT type FROM outbox_events") == [("neoson.run.failed",)]


def test_unknown_session_is_rejected_before_calling_the_model(db: Path) -> None:
    loop, provider = agent(db, [reply("nunca")])

    with pytest.raises(SessionNotFound):
        asyncio.run(loop.run(UserMessage(text="oi", session_id=uuid4())))

    assert provider.requests == []
    assert rows(db, "SELECT COUNT(*) FROM runs") == [(0,)]


# --- pelo gateway ---


def test_gateway_returns_the_session_and_continues_it(db: Path) -> None:
    loop, provider = agent(db, [reply("Olá!"), reply("De novo, olá!")])
    client = TestClient(
        create_app(loop, TOKEN), headers={"Authorization": f"Bearer {TOKEN}"}
    )

    first = client.post("/api/v1/messages", json={"text": "oi"}).json()
    second = client.post(
        "/api/v1/messages",
        json={"text": "oi de novo", "session_id": first["session_id"]},
    ).json()
    missing = client.post(
        "/api/v1/messages", json={"text": "oi", "session_id": str(uuid4())}
    )

    assert second["session_id"] == first["session_id"]
    assert [m.content for m in provider.requests[1].messages[1:]] == [
        "oi",
        "Olá!",
        "oi de novo",
    ]
    assert missing.status_code == 404
    assert missing.json()["code"] == "session_not_found"
