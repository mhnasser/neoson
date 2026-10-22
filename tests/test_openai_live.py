"""Chamadas reais à OpenAI. Só rodam com NEOSON_LIVE_TESTS=1 e a chave configurada."""

import asyncio
import json
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
from neoson_core.engine.delegation import (
    CHILD_SYSTEM,
    DELEGATE,
    DelegationLimits,
    DelegationManager,
)
from neoson_core.engine.messages import ToolCall
from neoson_core.infrastructure.config import load_settings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.curated import SqliteCuratedMemory
from neoson_core.memory.search import SqliteHistorySearch
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.loader import load_skill
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.tools.calendar_fake import CalendarEvent, FakeCalendar
from neoson_core.tools.memory_facts import MemoryForget, MemorySave
from neoson_core.tools.memory_search import MemorySearch
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.skills_eval import evaluate_skill

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


def test_live_preference_is_remembered_in_another_session(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    memory = SqliteCuratedMemory(db)

    def agent() -> AgentLoop:
        return AgentLoop(
            provider=live_provider(),
            router=RulesRouter("balanced"),
            budget=Budget(max_steps=4, timeout_seconds=90),
            tools=ToolRegistry([MemorySave(memory), MemoryForget(memory)]),
            store=SqliteConversationStore(db),
            memory=memory,
        )

    first = asyncio.run(
        agent().run(
            UserMessage(
                text="Por favor, lembre para sempre: meu time do coração é o Vasco."
            )
        )
    )
    second = asyncio.run(agent().run(UserMessage(text="Qual é o meu time do coração?")))

    assert first.status is RunStatus.COMPLETED
    assert [f.content for f in asyncio.run(memory.active(10))]
    assert second.session_id != first.session_id
    assert "vasco" in second.text.lower()


def test_live_agent_loads_approved_skill_and_uses_calendar(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    registry = SqliteSkillRegistry(db)
    library = Path(__file__).resolve().parents[1] / "skill_library"
    # Aprovação só neste banco de teste, sem modificar o catálogo real.
    registry.approve(load_skill(library / "planejar_dia", ["calendar_list"]))
    calendar = FakeCalendar.fixed(
        {
            date(2026, 10, 21): (
                CalendarEvent(time(15, 0), "Reunião de planejamento", 60),
            )
        }
    )
    agent = AgentLoop(
        provider=live_provider(),
        router=RulesRouter("balanced"),
        budget=Budget(max_steps=5, timeout_seconds=90),
        tools=ToolRegistry([calendar]),
        store=SqliteConversationStore(db),
        skills=registry,
    )
    result = asyncio.run(
        agent.run(
            UserMessage(
                text="Use a skill planejar_dia para planejar meu dia 2026-10-21."
            )
        )
    )
    assert result.status is RunStatus.COMPLETED
    assert "15" in result.text and "planejamento" in result.text.lower()
    connection = connect(db)
    try:
        execution = connection.execute(
            "SELECT skill_id, outcome FROM skill_executions WHERE run_id = ?",
            (str(result.run_id),),
        ).fetchone()
        calls = connection.execute(
            "SELECT tool_calls FROM messages WHERE run_id = ?"
            " AND tool_calls IS NOT NULL",
            (str(result.run_id),),
        ).fetchall()
    finally:
        connection.close()
    assert tuple(execution) == ("planejar_dia", "completed")
    assert any('"name": "calendar_list"' in row[0] for row in calls)


@pytest.mark.parametrize("skill_id", ["planejar_dia", "retomar_assunto"])
def test_live_skill_evaluation_records_reference_cases(
    tmp_path: Path, skill_id: str
) -> None:
    settings = load_settings()
    if settings.openai_api_key is None or settings.llm_model is None:
        pytest.skip("OPENAI_API_KEY ou NEOSON_LLM_MODEL não configurados")
    settings = settings.model_copy(
        update={
            "db_path": tmp_path / "eval.db",
            "max_agent_steps": 6,
            "run_timeout_seconds": 60,
        }
    )
    library = Path(__file__).resolve().parents[1] / "skill_library"
    SqliteSkillRegistry(settings.db_path).approve(load_skill(library / skill_id))
    report = asyncio.run(evaluate_skill(settings, skill_id))
    assert report.passed_cases == len(report.cases), [
        (case.id, case.reasons) for case in report.cases
    ]
    assert report.usage.input_tokens > 0
    connection = connect(settings.db_path)
    try:
        row = connection.execute(
            "SELECT skill_id, total_cases, passed_cases FROM skill_evaluations"
        ).fetchone()
        conversations = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[
            0
        ]
    finally:
        connection.close()
    assert tuple(row) == (skill_id, 2, 2)
    assert conversations == 0


def test_live_delegation_stays_within_the_conservative_reserve() -> None:
    provider = live_provider()
    arguments = {
        "task": "Diga em uma frase quais são os dois compromissos.",
        "context": "09:00 reunião de equipe; 14:00 consulta no dentista.",
    }
    manager = DelegationManager(
        DelegationLimits(max_attempts=1, max_output_tokens=600, timeout_seconds=60)
    )

    async def scenario() -> tuple[str, str]:
        # Um único loop: o cliente HTTP não sobrevive a dois `asyncio.run`.
        parent = await provider.complete(
            LLMRequest(
                model_profile="balanced",
                messages=(Message(role=Role.USER, content="Responda só: ok"),),
            )
        )
        text = await manager.execute(
            provider,
            ToolCall("d1", DELEGATE, arguments),
            model_profile="balanced",
            expected_model=parent.model,
            remaining_seconds=60,
        )
        return parent.model, text

    model, text = asyncio.run(scenario())
    content = json.dumps(arguments, ensure_ascii=False)
    reserve = len((CHILD_SYSTEM + content).encode("utf-8")) + 256
    usage = manager.ledger.usage
    assert text.strip() and "14" in text
    assert manager.ledger.by_model == {model: usage}
    assert 0 < usage.input_tokens <= reserve and usage.output_tokens <= 600
