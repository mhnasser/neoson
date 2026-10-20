"""Chamadas reais à OpenAI. Só rodam com NEOSON_LIVE_TESTS=1 e a chave configurada."""

import asyncio
import os
from datetime import UTC, date, datetime, time
from pathlib import Path
from uuid import uuid4

import pytest
from openai import AsyncOpenAI

from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMRequest,
    Message,
    Role,
    RunRecord,
    RunStatus,
    ToolSpec,
    UserMessage,
)
from neoson_core.infrastructure.config import load_settings
from neoson_core.memory.search import SqliteHistorySearch
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.calendar_fake import CalendarEvent, FakeCalendar
from neoson_core.tools.memory_search import MemorySearch
from neoson_core.tools.registry import ToolRegistry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("NEOSON_LIVE_TESTS") != "1",
        reason="defina NEOSON_LIVE_TESTS=1 para chamar a API real",
    ),
]


def live_provider() -> OpenAIProvider:
    settings = load_settings()
    if settings.openai_api_key is None or settings.llm_model is None:
        pytest.skip("OPENAI_API_KEY ou NEOSON_LLM_MODEL não configurados")
    client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
    return OpenAIProvider(client, models={"balanced": settings.llm_model})


def test_live_text_reply() -> None:
    response = asyncio.run(
        live_provider().complete(
            LLMRequest(
                model_profile="balanced",
                messages=(
                    Message(role=Role.SYSTEM, content="Responda em uma frase curta."),
                    Message(role=Role.USER, content="Diga olá ao NeoSon."),
                ),
                max_output_tokens=300,
            )
        )
    )

    assert response.message.content
    assert response.usage.input_tokens > 0


def test_live_tool_call() -> None:
    calendar = ToolSpec(
        name="calendar_list",
        description="Lista os compromissos de um dia (formato AAAA-MM-DD).",
        parameters={
            "type": "object",
            "properties": {"day": {"type": "string"}},
            "required": ["day"],
            "additionalProperties": False,
        },
    )

    response = asyncio.run(
        live_provider().complete(
            LLMRequest(
                model_profile="balanced",
                messages=(
                    Message(
                        role=Role.USER,
                        content="Quais são meus compromissos em 2026-10-21?",
                    ),
                ),
                tools=(calendar,),
                max_output_tokens=300,
            )
        )
    )

    (call,) = response.message.tool_calls
    assert call.name == "calendar_list"
    assert call.arguments["day"] == "2026-10-21"


def test_live_agent_loop_answers_through_the_model() -> None:
    agent = AgentLoop(
        provider=live_provider(),
        router=RulesRouter("balanced"),
        budget=Budget(max_steps=3, timeout_seconds=60),
    )

    result = asyncio.run(agent.run(UserMessage(text="Quanto é 2 + 3? Só o número.")))

    assert result.status is RunStatus.COMPLETED
    assert "5" in result.text
    assert result.usage.input_tokens > 0


def test_live_agent_uses_the_calendar_tool() -> None:
    calendar = FakeCalendar.fixed(
        {date(2026, 10, 21): (CalendarEvent(time(15, 0), "Consulta no dentista", 60),)}
    )
    agent = AgentLoop(
        provider=live_provider(),
        router=RulesRouter("balanced"),
        budget=Budget(max_steps=4, timeout_seconds=90),
        tools=ToolRegistry([calendar]),
    )

    result = asyncio.run(
        agent.run(UserMessage(text="Quais são meus compromissos em 2026-10-21?"))
    )

    assert result.status is RunStatus.COMPLETED
    assert "dentista" in result.text.lower()
    assert "15" in result.text


def test_live_agent_recalls_a_decision_from_another_session(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    store = SqliteConversationStore(db)
    old_session = asyncio.run(store.create_session("http"))
    when = datetime(2026, 10, 18, 17, 30, tzinfo=UTC)
    asyncio.run(
        store.record_run(
            RunRecord(
                run_id=uuid4(),
                session_id=old_session,
                status=RunStatus.COMPLETED,
                started_at=when,
                ended_at=when,
                messages=(
                    Message(
                        role=Role.USER, content="Decidimos: o robô vai se chamar Atlas."
                    ),
                    Message(
                        role=Role.ASSISTANT, content="Combinado, o robô será Atlas."
                    ),
                ),
            )
        )
    )
    agent = AgentLoop(
        provider=live_provider(),
        router=RulesRouter("balanced"),
        budget=Budget(max_steps=4, timeout_seconds=90),
        tools=ToolRegistry([MemorySearch(SqliteHistorySearch(db), UTC)]),
        store=store,
    )

    result = asyncio.run(
        agent.run(UserMessage(text="Que nome decidimos para o robô? Diga quando."))
    )

    assert result.status is RunStatus.COMPLETED
    assert "atlas" in result.text.lower()
    assert "18" in result.text or "2026-10-18" in result.text
