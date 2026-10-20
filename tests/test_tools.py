import asyncio
from datetime import UTC, date, datetime, time, timedelta
from typing import ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel

from neoson_core.engine import (
    AgentLoop,
    Budget,
    PolicyEngine,
    RiskLevel,
    Role,
    RunContext,
    RunState,
    RunStatus,
    ToolCall,
    ToolContext,
    ToolRejected,
    ToolResultStatus,
    UserMessage,
    Verdict,
)
from neoson_core.engine.loop import TOOL_DENIED
from neoson_core.engine.tools import fingerprint
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.calendar_fake import CalendarEvent, FakeCalendar
from neoson_core.tools.registry import TOOL_FAILED, Tool, ToolOutput, ToolRegistry

DAY = date(2026, 10, 21)
CONTEXT = ToolContext(run_id=uuid4(), session_id=uuid4())
CALENDAR = FakeCalendar.fixed(
    {
        DAY: (
            CalendarEvent(time(15, 0), "Dentista", 60),
            CalendarEvent(time(9, 0), "Reunião", 30),
        )
    }
)


class Note(BaseModel):
    text: str


class SendEmail(Tool[Note]):
    name = "email_send"
    description = "Envia um e-mail."
    risk = RiskLevel.SENSITIVE_WRITE
    arguments = Note
    sent: ClassVar[list[str]] = []

    async def run(self, arguments: Note, context: ToolContext) -> ToolOutput:
        SendEmail.sent.append(arguments.text)
        return ToolOutput(summary="enviado")


class Shell(Tool[Note]):
    name = "shell"
    description = "Executa um comando."
    risk = RiskLevel.FORBIDDEN
    arguments = Note

    async def run(self, arguments: Note, context: ToolContext) -> ToolOutput:
        raise AssertionError("nunca deveria executar")


class Broken(Tool[Note]):
    name = "broken"
    description = "Sempre falha."
    risk = RiskLevel.READ
    arguments = Note

    async def run(self, arguments: Note, context: ToolContext) -> ToolOutput:
        raise RuntimeError("segredo interno")


def calendar_call(day: object = "2026-10-21", call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="calendar_list", arguments={"day": day})


# --- Policy Engine ---


def test_policy_follows_the_risk_level() -> None:
    policy = PolicyEngine()

    assert policy.evaluate(calendar_call(), RiskLevel.READ) is Verdict.ALLOW
    assert (
        policy.evaluate(calendar_call(), RiskLevel.SENSITIVE_WRITE)
        is Verdict.REQUIRE_APPROVAL
    )
    assert policy.evaluate(calendar_call(), RiskLevel.FORBIDDEN) is Verdict.DENY


def test_always_mode_requires_approval_even_for_reads() -> None:
    policy = PolicyEngine("always")

    assert policy.evaluate(calendar_call(), RiskLevel.READ) is Verdict.REQUIRE_APPROVAL


def test_approval_is_bound_to_arguments_used_once_and_expires() -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    policy = PolicyEngine()
    call = ToolCall(id="a", name="email_send", arguments={"text": "oi"})
    policy.grant(call, ttl=timedelta(minutes=5), now=now)

    other_args = ToolCall(id="b", name="email_send", arguments={"text": "outro"})
    assert (
        policy.evaluate(other_args, RiskLevel.SENSITIVE_WRITE, now)
        is Verdict.REQUIRE_APPROVAL
    )
    same_action_new_id = ToolCall(id="z", name="email_send", arguments={"text": "oi"})
    assert (
        policy.evaluate(same_action_new_id, RiskLevel.SENSITIVE_WRITE, now)
        is Verdict.ALLOW
    )
    assert (
        policy.evaluate(call, RiskLevel.SENSITIVE_WRITE, now)
        is Verdict.REQUIRE_APPROVAL
    )

    policy.grant(call, ttl=timedelta(minutes=5), now=now)
    later = now + timedelta(minutes=6)
    assert (
        policy.evaluate(call, RiskLevel.SENSITIVE_WRITE, later)
        is Verdict.REQUIRE_APPROVAL
    )


def test_fingerprint_ignores_call_id_and_key_order() -> None:
    first = ToolCall(id="1", name="t", arguments={"a": 1, "b": 2})
    second = ToolCall(id="2", name="t", arguments={"b": 2, "a": 1})

    assert fingerprint(first) == fingerprint(second)


def test_unknown_approval_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        PolicyEngine("never")  # type: ignore[arg-type]


# --- Tool Registry ---


def test_registry_publishes_the_schema_from_the_arguments_model() -> None:
    (spec,) = ToolRegistry([CALENDAR]).specs()

    assert spec.name == "calendar_list"
    assert spec.parameters["required"] == ["day"]
    assert spec.parameters["additionalProperties"] is False


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (ToolCall(id="x", name="nao_existe"), "Ferramenta desconhecida"),
        (calendar_call(day="amanhã"), "Argumentos inválidos para calendar_list: day"),
        (ToolCall(id="x", name="calendar_list"), "day: Field required"),
        (
            ToolCall(
                id="x", name="calendar_list", arguments={"day": "2026-10-21", "x": 1}
            ),
            "x: Extra inputs",
        ),
    ],
)
def test_registry_rejects_unknown_tools_and_invalid_arguments(
    call: ToolCall, message: str
) -> None:
    with pytest.raises(ToolRejected) as caught:
        ToolRegistry([CALENDAR]).check(call)

    assert message in caught.value.summary
    assert "amanhã" not in caught.value.summary


def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError):
        ToolRegistry([CALENDAR, CALENDAR])


def test_registry_executes_and_wraps_failures() -> None:
    registry = ToolRegistry([CALENDAR, Broken()])

    ok = asyncio.run(registry.execute(calendar_call(), CONTEXT))
    failed = asyncio.run(
        registry.execute(
            ToolCall(id="b", name="broken", arguments={"text": "x"}), CONTEXT
        )
    )

    assert (ok.call_id, ok.status) == ("c1", ToolResultStatus.OK)
    assert ok.summary == (
        "Compromissos em 2026-10-21: 09:00 Reunião (30 min); 15:00 Dentista (60 min)."
    )
    assert ok.data["day"] == "2026-10-21"
    assert (failed.status, failed.summary) == (ToolResultStatus.ERROR, TOOL_FAILED)
    assert "segredo" not in failed.summary


def test_demo_calendar_is_relative_to_the_clock() -> None:
    clock = lambda: datetime(2026, 10, 20, 8, 0, tzinfo=UTC)  # noqa: E731
    registry = ToolRegistry([FakeCalendar.demo(clock)])

    today = asyncio.run(registry.execute(calendar_call("2026-10-20"), CONTEXT))
    tomorrow = asyncio.run(registry.execute(calendar_call("2026-10-21"), CONTEXT))
    later = asyncio.run(registry.execute(calendar_call("2026-10-25"), CONTEXT))

    assert "Reunião de planejamento" in today.summary
    assert "dentista" in tomorrow.summary
    assert later.summary == "Nenhum compromisso em 2026-10-25."


# --- Agent Loop com ferramentas ---


def agent(
    provider: ScriptedProvider, policy: PolicyEngine | None = None
) -> tuple[AgentLoop, list[RunState]]:
    states: list[RunState] = []

    def record(context: RunContext, state: RunState) -> None:
        states.append(state)

    loop = AgentLoop(
        provider=provider,
        router=RulesRouter("balanced"),
        budget=Budget(max_steps=4, timeout_seconds=5),
        tools=ToolRegistry([CALENDAR, SendEmail(), Shell()]),
        policy=policy,
        clock=lambda: datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
        on_state=record,
    )
    return loop, states


def test_allowed_tool_runs_and_its_result_goes_back_to_the_model() -> None:
    provider = ScriptedProvider(
        [
            call_tool("calendar_list", {"day": "2026-10-21"}, "c1"),
            reply("Dentista às 15h."),
        ]
    )
    loop, states = agent(provider)

    result = asyncio.run(loop.run(UserMessage(text="Agenda de amanhã?")))

    assert (result.status, result.text) == (RunStatus.COMPLETED, "Dentista às 15h.")
    first, second = provider.requests
    assert [t.name for t in first.tools] == ["calendar_list", "email_send", "shell"]
    assert "Hoje é terça-feira, 2026-10-20." in first.messages[0].content
    tool_reply = second.messages[-1]
    assert (tool_reply.role, tool_reply.tool_call_id) == (Role.TOOL, "c1")
    assert "15:00 Dentista" in tool_reply.content
    assert states[-1] is RunState.COMPLETED


def test_sensitive_tool_suspends_the_run_without_executing() -> None:
    SendEmail.sent.clear()
    provider = ScriptedProvider([call_tool("email_send", {"text": "oi"}, "e1")])
    loop, states = agent(provider)

    result = asyncio.run(loop.run(UserMessage(text="Mande um e-mail")))

    assert result.status is RunStatus.NEEDS_APPROVAL
    assert result.text == "Preciso da sua aprovação para executar: email_send."
    assert SendEmail.sent == []
    assert states[-1] is RunState.WAITING_APPROVAL
    assert len(provider.requests) == 1


def test_granted_approval_lets_the_exact_action_run() -> None:
    SendEmail.sent.clear()
    policy = PolicyEngine()
    policy.grant(
        ToolCall(id="?", name="email_send", arguments={"text": "oi"}),
        timedelta(minutes=5),
    )
    provider = ScriptedProvider(
        [call_tool("email_send", {"text": "oi"}, "e1"), reply("Enviado.")]
    )
    loop, _ = agent(provider, policy)

    result = asyncio.run(loop.run(UserMessage(text="Mande um e-mail")))

    assert result.status is RunStatus.COMPLETED
    assert SendEmail.sent == ["oi"]


def test_forbidden_and_invalid_requests_are_reported_to_the_model() -> None:
    provider = ScriptedProvider(
        [
            call_tool("shell", {"text": "rm -rf /"}, "s1"),
            call_tool("calendar_list", {"day": "ontem"}, "c1"),
            reply("Não posso fazer isso."),
        ]
    )
    loop, _ = agent(provider)

    result = asyncio.run(loop.run(UserMessage(text="apague tudo")))

    assert result.status is RunStatus.COMPLETED
    assert provider.requests[1].messages[-1].content == TOOL_DENIED
    assert "Argumentos inválidos" in provider.requests[2].messages[-1].content
