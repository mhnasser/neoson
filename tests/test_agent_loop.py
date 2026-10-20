import asyncio
from uuid import uuid4

import pytest

from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Role,
    RunContext,
    RunResult,
    RunState,
    RunStatus,
    Usage,
    UserMessage,
)
from neoson_core.engine.loop import SYSTEM_PROMPT, TOOL_UNAVAILABLE
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter

BUDGET = Budget(max_steps=4, timeout_seconds=5)


def loop(
    provider: LLMProvider, budget: Budget = BUDGET
) -> tuple[AgentLoop, list[RunState]]:
    states: list[RunState] = []

    def record(context: RunContext, state: RunState) -> None:
        states.append(state)

    agent = AgentLoop(
        provider=provider,
        router=RulesRouter("balanced"),
        budget=budget,
        on_state=record,
    )
    return agent, states


def run(agent: AgentLoop, text: str = "Olá") -> RunResult:
    return asyncio.run(agent.run(UserMessage(text=text)))


def test_direct_reply_completes_with_usage_and_model() -> None:
    provider = ScriptedProvider([reply("Oi!", Usage(20, 4, 8))])
    agent, states = loop(provider)

    result = asyncio.run(agent.run(UserMessage(text="Olá", session_id=uuid4())))

    assert (result.text, result.status) == ("Oi!", RunStatus.COMPLETED)
    assert result.usage == Usage(20, 4, 8)
    assert result.model == "fake-model"
    assert states == [
        RunState.RECEIVED,
        RunState.ROUTING,
        RunState.CONTEXT_READY,
        RunState.RUNNING,
        RunState.COMPLETED,
    ]
    (sent,) = provider.requests
    assert sent.model_profile == "balanced"
    system, user = sent.messages
    assert system.role is Role.SYSTEM
    assert system.content.startswith(SYSTEM_PROMPT)
    assert "Hoje é " in system.content
    assert (user.role, user.content) == (Role.USER, "Olá")


def test_tool_request_gets_an_unavailable_result_and_the_loop_continues() -> None:
    provider = ScriptedProvider(
        [
            call_tool("calendar_list", {"day": "2026-10-21"}, "call-9"),
            reply("Sem agenda."),
        ]
    )
    agent, _ = loop(provider)

    result = run(agent)

    assert result.text == "Sem agenda."
    assert result.usage == Usage(20, 10, 0)
    second = provider.requests[1]
    assert second.messages[-2].tool_calls[0].id == "call-9"
    assert second.messages[-1].role is Role.TOOL
    assert second.messages[-1].tool_call_id == "call-9"
    assert second.messages[-1].content == TOOL_UNAVAILABLE


def test_step_limit_ends_the_run_as_failed() -> None:
    provider = ScriptedProvider([call_tool("x", call_id=f"c{i}") for i in range(4)])
    agent, states = loop(provider)

    result = run(agent)

    assert result.status is RunStatus.FAILED
    assert "limite de passos" in result.text
    assert len(provider.requests) == BUDGET.max_steps
    assert states[-1] is RunState.FAILED


def test_token_budget_stops_before_the_next_call() -> None:
    provider = ScriptedProvider([call_tool("x", usage=Usage(900, 200)), reply("nunca")])
    agent, _ = loop(provider, Budget(max_steps=4, timeout_seconds=5, max_tokens=1000))

    result = run(agent)

    assert result.status is RunStatus.FAILED
    assert "limite de uso" in result.text
    assert len(provider.requests) == 1


class SlowProvider:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(5)
        raise AssertionError("não deveria terminar")


def test_deadline_interrupts_a_slow_model() -> None:
    agent, states = loop(SlowProvider(), Budget(max_steps=4, timeout_seconds=0.05))

    result = run(agent)

    assert result.status is RunStatus.FAILED
    assert "tempo limite" in result.text
    assert states[-1] is RunState.FAILED


@pytest.mark.parametrize(
    ("retryable", "expected"),
    [(True, "Tente de novo"), (False, "Não consegui gerar uma resposta")],
)
def test_model_errors_end_the_run_with_a_safe_message(
    retryable: bool, expected: str
) -> None:
    error = LLMError("detalhe interno do fornecedor", retryable=retryable)
    agent, _ = loop(ScriptedProvider([error]))

    result = run(agent)

    assert result.status is RunStatus.FAILED
    assert expected in result.text
    assert "detalhe interno" not in result.text


def test_session_id_is_kept_or_created() -> None:
    session = uuid4()
    seen: list[RunContext] = []
    agent = AgentLoop(
        provider=ScriptedProvider([reply("a"), reply("b")]),
        router=RulesRouter("balanced"),
        budget=BUDGET,
        on_state=lambda context, state: seen.append(context),
    )

    asyncio.run(agent.run(UserMessage(text="1", session_id=session)))
    asyncio.run(agent.run(UserMessage(text="2")))

    assert seen[0].session_id == session
    assert seen[-1].session_id != session
    assert seen[0].run_id != seen[-1].run_id
