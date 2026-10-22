import asyncio
import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from neoson_core.engine import (
    AgentLoop,
    Budget,
    OutboxEvent,
    PolicyEngine,
    RiskLevel,
    Role,
    RunStatus,
    ToolContext,
    ToolRejected,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
    UserMessage,
)
from neoson_core.engine.delegation import (
    DELEGATE,
    DelegationFailure,
    DelegationLimits,
    DelegationManager,
)
from neoson_core.engine.llm import (
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Usage,
)
from neoson_core.engine.loop import TOOL_DENIED
from neoson_core.engine.messages import ToolCall
from neoson_core.engine.skill_context import SKILL_TOOL_DENIED
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import FAKE_MODEL, ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.loader import SkillInvalid, package_from_texts
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.telemetry.metrics import daily_metrics
from neoson_core.telemetry.pricing import Price
from neoson_core.telemetry.usage import DELEGATION_COUNTERS, UsageRecorder
from neoson_core.telemetry.vault import render_daily
from neoson_gateway.composition import TOOL_NAMES

PRIVATE = "contexto-privado-selecionado"
CALL = ToolCall("d1", DELEGATE, {"task": "Resuma os dados.", "context": PRIVATE})


async def delegate(
    manager: DelegationManager, provider: ScriptedProvider, **options: object
) -> str:
    # Opções específicas são testadas por chamadas diretas abaixo.
    return await manager.execute(
        provider,
        CALL,
        model_profile="session-bound",
        expected_model=FAKE_MODEL,
        remaining_seconds=10,
    )


def test_context_is_reduced_no_tools_and_usage_aggregates_per_model() -> None:
    provider = ScriptedProvider(
        [
            reply("Resumo.", Usage(100, 20, 30)),
            reply("Outra análise.", Usage(200, 40, 50)),
        ]
    )
    manager = DelegationManager(DelegationLimits())

    async def scenario() -> None:
        assert await delegate(manager, provider) == "Resumo."
        assert await delegate(manager, provider) == "Outra análise."

    asyncio.run(scenario())
    assert manager.ledger.usage == Usage(300, 60, 80)
    assert manager.ledger.by_model == {FAKE_MODEL: Usage(300, 60, 80)}
    assert manager.ledger.attempts == manager.ledger.completed == 2
    assert manager.ledger.usage_complete
    assert PRIVATE not in str(manager.ledger.metrics())
    for request in provider.requests:
        assert request.tools == () and len(request.messages) == 2
        assert request.model_profile == "session-bound"
        assert PRIVATE in request.messages[1].content
        assert request.max_output_tokens == 1000


@pytest.mark.parametrize(
    "arguments",
    [
        {"task": "x", "context": "", "approve": True},
        {"task": "", "context": ""},
        {"task": "x", "context": "x" * 8001},
        {"task": "x", "context": False},
        {"task": "x" * 1001, "context": ""},
    ],
)
def test_invalid_context_never_calls_provider(arguments: dict[str, object]) -> None:
    manager = DelegationManager(DelegationLimits())
    provider = ScriptedProvider([])
    with pytest.raises(DelegationFailure, match="arguments"):
        asyncio.run(
            manager.execute(
                provider,
                ToolCall("d", DELEGATE, arguments),
                model_profile="p",
                expected_model=FAKE_MODEL,
                remaining_seconds=10,
            )
        )
    assert not provider.requests and manager.ledger.attempts == 0


def test_depth_attempts_and_parent_token_allowance_are_enforced() -> None:
    manager = DelegationManager(DelegationLimits(max_attempts=1))
    provider = ScriptedProvider([reply("Resumo.")])
    with pytest.raises(DelegationFailure, match="depth"):
        asyncio.run(
            manager.execute(
                provider,
                CALL,
                model_profile="p",
                expected_model=FAKE_MODEL,
                remaining_seconds=10,
                depth=2,
            )
        )
    with pytest.raises(DelegationFailure, match="tokens"):
        asyncio.run(
            manager.execute(
                provider,
                CALL,
                model_profile="p",
                expected_model=FAKE_MODEL,
                remaining_seconds=10,
                remaining_tokens=10,
            )
        )
    assert not provider.requests
    asyncio.run(delegate(manager, provider))
    with pytest.raises(DelegationFailure, match="attempts"):
        asyncio.run(delegate(manager, provider))
    assert len(provider.requests) == 1


def test_reported_failure_usage_is_charged_without_retry() -> None:
    manager = DelegationManager(DelegationLimits(max_attempts=1))
    provider = ScriptedProvider(
        [LLMError(PRIVATE, retryable=True, usage=Usage(100, 5, 20), model=FAKE_MODEL)]
    )
    with pytest.raises(DelegationFailure, match="provider") as failure:
        asyncio.run(delegate(manager, provider))
    assert PRIVATE not in str(failure.value)
    assert manager.ledger.usage == Usage(100, 5, 20) and manager.ledger.failed == 1
    assert manager.ledger.usage_complete
    with pytest.raises(DelegationFailure, match="attempts"):
        asyncio.run(delegate(manager, provider))
    assert len(provider.requests) == 1


def test_unknown_failure_blocks_later_calls_instead_of_claiming_zero_cost() -> None:
    manager = DelegationManager(DelegationLimits())
    provider = ScriptedProvider(
        [LLMError(PRIVATE, retryable=True), reply("Não deve chamar.")]
    )
    with pytest.raises(DelegationFailure) as failure:
        asyncio.run(delegate(manager, provider))
    assert failure.value.fatal and not manager.ledger.usage_complete
    with pytest.raises(DelegationFailure, match="usage_unknown"):
        asyncio.run(delegate(manager, provider))
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "requested_tool",
    ["delegate", "calendar_create", "email_draft", "approve", "publish_code"],
)
def test_child_cannot_recurse_approve_write_or_publish(requested_tool: str) -> None:
    manager = DelegationManager(DelegationLimits())
    provider = ScriptedProvider([call_tool(requested_tool, usage=Usage(123, 45, 20))])
    with pytest.raises(DelegationFailure, match="tools"):
        asyncio.run(delegate(manager, provider))
    assert manager.ledger.usage == Usage(123, 45, 20)
    assert manager.ledger.failed == 1 and manager.ledger.completed == 0
    assert provider.requests[0].tools == ()


def test_model_mismatch_is_rejected_and_usage_keeps_actual_model() -> None:
    manager = DelegationManager(DelegationLimits())
    provider = ScriptedProvider([replace(reply("Resposta."), model="other-model")])
    with pytest.raises(DelegationFailure, match="model") as failure:
        asyncio.run(delegate(manager, provider))
    assert failure.value.fatal and manager.ledger.by_model == {
        "other-model": Usage(10, 5)
    }


def test_timeout_cancels_child_and_marks_unmeasured_usage() -> None:
    cleaned: list[bool] = []

    class Slow:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            try:
                await asyncio.Event().wait()
                return reply("Nunca.")
            finally:
                cleaned.append(True)

    manager = DelegationManager(DelegationLimits(timeout_seconds=0.01))
    with pytest.raises(DelegationFailure, match="timeout"):
        asyncio.run(
            manager.execute(
                Slow(),
                CALL,
                model_profile="p",
                expected_model=FAKE_MODEL,
                remaining_seconds=10,
            )
        )
    assert cleaned == [True] and manager.ledger.failed == 1
    assert not manager.ledger.usage_complete


def test_parent_cancellation_propagates_and_no_child_remains_running() -> None:
    cleaned: list[bool] = []

    async def scenario() -> None:
        started = asyncio.Event()

        class Slow:
            async def complete(self, request: LLMRequest) -> LLMResponse:
                started.set()
                try:
                    await asyncio.Event().wait()
                    return reply("Nunca.")
                finally:
                    cleaned.append(True)

        manager = DelegationManager(DelegationLimits())
        task = asyncio.create_task(
            manager.execute(
                Slow(),
                CALL,
                model_profile="p",
                expected_model=FAKE_MODEL,
                remaining_seconds=10,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.ledger.failed == 1 and not manager.ledger.usage_complete

    asyncio.run(scenario())
    assert cleaned == [True]


@pytest.mark.parametrize("usage", [Usage(-1, 1), Usage(1, 1, 2)])
def test_invalid_reported_usage_cannot_refill_budget(usage: Usage) -> None:
    manager = DelegationManager(DelegationLimits())
    provider = ScriptedProvider([reply("Inválido.", usage)])
    with pytest.raises(DelegationFailure, match="usage"):
        asyncio.run(delegate(manager, provider))
    assert manager.ledger.usage == Usage() and not manager.ledger.usage_complete


def test_parent_loop_aggregates_usage_without_sharing_full_context(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            call_tool(DELEGATE, CALL.arguments),
            reply("Síntese selecionada.", Usage(100, 20, 10)),
            reply("Resposta final."),
        ]
    )
    db = tmp_path / "delegation.db"
    loop = AgentLoop(
        provider,
        RulesRouter("session-bound"),
        Budget(4, 10),
        store=SqliteConversationStore(db),
        delegation_limits=DelegationLimits(),
    )
    result = asyncio.run(loop.run(UserMessage("pedido-pai-privado")))
    assert result.status is RunStatus.COMPLETED and result.model == FAKE_MODEL
    assert result.usage == Usage(120, 30, 10)
    child = provider.requests[1]
    assert "pedido-pai-privado" not in str(child.messages) and child.tools == ()
    assert [r.model_profile for r in provider.requests] == ["session-bound"] * 3
    with closing(connect(db)) as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM outbox_events").fetchone()[0]
        )
    assert payload["usage_complete"] is True
    assert payload["delegation"]["completed"] == 1
    assert PRIVATE not in json.dumps(payload)


def test_parent_stops_after_unmeasured_child_failure_and_marks_partial_cost(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            call_tool(DELEGATE, CALL.arguments),
            LLMError(PRIVATE, retryable=True),
            reply("Não deve continuar."),
        ]
    )
    db = tmp_path / "delegation.db"
    loop = AgentLoop(
        provider,
        RulesRouter("session-bound"),
        Budget(4, 10),
        store=SqliteConversationStore(db),
        delegation_limits=DelegationLimits(),
    )
    result = asyncio.run(loop.run(UserMessage("Pedido.")))
    assert result.status is RunStatus.FAILED and len(provider.requests) == 2
    with closing(connect(db)) as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM outbox_events").fetchone()[0]
        )
    assert payload["usage_complete"] is False
    assert payload["delegation"]["failed"] == 1 and PRIVATE not in json.dumps(payload)


class Noop:
    """Catálogo mínimo com uma leitura, para exercitar o pai sem delegação."""

    def specs(self) -> tuple[ToolSpec, ...]:
        return (ToolSpec("noop", "noop", {"type": "object"}),)

    def check(self, call: ToolCall) -> RiskLevel:
        if call.name != "noop":
            raise ToolRejected("Ferramenta desconhecida.")
        return RiskLevel.READ

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        return ToolResult(call.id, ToolResultStatus.OK, "ok")


def delegating_loop(
    db: Path,
    provider: LLMProvider,
    limits: DelegationLimits | None = None,
    **options: Any,
) -> AgentLoop:
    return AgentLoop(
        provider,
        RulesRouter("session-bound"),
        options.pop("budget", Budget(6, 10)),
        store=SqliteConversationStore(db),
        delegation_limits=limits or DelegationLimits(),
        **options,
    )


def outbox_payload(db: Path) -> dict[str, Any]:
    with closing(connect(db)) as connection:
        (payload,) = connection.execute("SELECT payload FROM outbox_events").fetchone()
    return cast(dict[str, Any], json.loads(payload))


def tool_replies(request: LLMRequest) -> list[str]:
    return [m.content for m in request.messages if m.role is Role.TOOL]


def test_aggregate_budget_rejects_later_delegation_and_caps_output(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            call_tool(DELEGATE, CALL.arguments, "d1"),
            reply("Primeira análise.", Usage(600, 20)),
            call_tool(DELEGATE, CALL.arguments, "d2"),
            reply("Final."),
        ]
    )
    db = tmp_path / "budget.db"
    limits = DelegationLimits(max_attempts=3, max_tokens=1000, max_output_tokens=50)
    result = asyncio.run(delegating_loop(db, provider, limits).run(UserMessage("P.")))
    assert result.status is RunStatus.COMPLETED and len(provider.requests) == 4
    assert provider.requests[1].max_output_tokens == 50
    assert tool_replies(provider.requests[3])[-1].endswith("tokens.")
    assert result.usage == Usage(630, 35)
    payload = outbox_payload(db)
    delegation = payload["delegation"]
    assert (delegation["attempts"], delegation["completed"]) == (1, 1)
    assert delegation["rejected"] == 1 and payload["usage_complete"] is True


def test_reported_usage_above_allowance_is_charged_and_stops_the_parent(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            call_tool(DELEGATE, CALL.arguments),
            reply("Grande demais.", Usage(2000, 10)),
            reply("Não deve continuar."),
        ]
    )
    db = tmp_path / "over.db"
    limits = DelegationLimits(max_tokens=1000, max_output_tokens=50)
    result = asyncio.run(delegating_loop(db, provider, limits).run(UserMessage("P.")))
    assert result.status is RunStatus.FAILED and len(provider.requests) == 2
    assert result.usage == Usage(2010, 15)
    assert outbox_payload(db)["delegation"]["failed"] == 1


def test_attempt_limit_in_the_loop_has_no_retry(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            call_tool(DELEGATE, CALL.arguments, "d1"),
            LLMError(PRIVATE, retryable=True, usage=Usage(30, 0), model=FAKE_MODEL),
            call_tool(DELEGATE, CALL.arguments, "d2"),
            reply("Final."),
        ]
    )
    db = tmp_path / "attempts.db"
    limits = DelegationLimits(max_attempts=1)
    result = asyncio.run(delegating_loop(db, provider, limits).run(UserMessage("P.")))
    assert result.status is RunStatus.COMPLETED and len(provider.requests) == 4
    assert tool_replies(provider.requests[2]) == [
        "A análise delegada não foi concluída: provider."
    ]
    assert tool_replies(provider.requests[3])[-1].endswith("attempts.")
    assert result.usage == Usage(60, 15)
    payload = outbox_payload(db)
    assert payload["usage_complete"] is True and PRIVATE not in json.dumps(payload)
    assert payload["delegation"]["failed"] == payload["delegation"]["rejected"] == 1


def test_always_mode_denies_delegation_without_resumable_approval(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([call_tool(DELEGATE, CALL.arguments), reply("Ok.")])
    db = tmp_path / "always.db"
    loop = delegating_loop(db, provider, policy=PolicyEngine("always"))
    result = asyncio.run(loop.run(UserMessage("P.")))
    assert result.status is RunStatus.COMPLETED and len(provider.requests) == 2
    assert tool_replies(provider.requests[1]) == [TOOL_DENIED]
    with closing(connect(db)) as connection:
        pending = connection.execute("SELECT COUNT(*) FROM pending_actions")
        assert pending.fetchone()[0] == 0


SKILL = """# Planejar
## Quando usar
Para planejar o dia.
## Passos
Ordene os compromissos.
## Restrições
Não invente compromissos.
## Saída
Plano do dia.
"""


def skill_texts(tools: str) -> dict[str, str]:
    return {
        "SKILL.md": SKILL,
        "metadata.yaml": "id: planejar\nversion: 1.0.0\n"
        "description: Planeja o dia.\ntriggers: [planejar dia]\n"
        f"allowed_tools: [{tools}]\nrequires_approval: false\n"
        "risk_level: low\nowner: neoson\n",
    }


def test_active_skill_cannot_grant_delegation(tmp_path: Path) -> None:
    with pytest.raises(SkillInvalid, match="delegate"):
        package_from_texts(Path("planejar"), skill_texts(DELEGATE), TOOL_NAMES)
    db = tmp_path / "skill.db"
    registry = SqliteSkillRegistry(db)
    registry.approve(package_from_texts(Path("planejar"), skill_texts("noop")))
    provider = ScriptedProvider(
        [
            call_tool("skill_load", {"skill_id": "planejar", "version": "1.0.0"}),
            call_tool(DELEGATE, CALL.arguments, "d1"),
            reply("Plano."),
        ]
    )
    loop = delegating_loop(db, provider, tools=Noop(), skills=registry)
    result = asyncio.run(loop.run(UserMessage("Planejar dia.")))
    assert result.status is RunStatus.COMPLETED and len(provider.requests) == 3
    assert DELEGATE in {spec.name for spec in provider.requests[0].tools}
    assert {spec.name for spec in provider.requests[1].tools} == {"noop"}
    assert tool_replies(provider.requests[2])[-1] == SKILL_TOOL_DENIED


@pytest.mark.parametrize(
    "failure",
    [
        LLMError(PRIVATE, retryable=True),
        LLMError(PRIVATE, retryable=True, usage=Usage(5, 1)),
        LLMError(PRIVATE, retryable=True, usage=Usage(-1, 0), model=FAKE_MODEL),
    ],
)
def test_parent_failure_without_complete_measurement_marks_cost_incomplete(
    tmp_path: Path, failure: LLMError
) -> None:
    db = tmp_path / "parent.db"
    provider = ScriptedProvider([failure])
    result = asyncio.run(delegating_loop(db, provider).run(UserMessage("P.")))
    assert result.status is RunStatus.FAILED
    assert outbox_payload(db)["usage_complete"] is False


def test_parent_failure_with_reported_usage_is_charged(tmp_path: Path) -> None:
    db = tmp_path / "parent.db"
    provider = ScriptedProvider(
        [LLMError(PRIVATE, retryable=False, usage=Usage(50, 5, 10), model=FAKE_MODEL)]
    )
    result = asyncio.run(delegating_loop(db, provider).run(UserMessage("P.")))
    assert result.status is RunStatus.FAILED and result.model == FAKE_MODEL
    assert result.usage == Usage(50, 5, 10)
    payload = outbox_payload(db)
    assert payload["usage_complete"] is True and payload["usage"]["input_tokens"] == 50


def test_parent_timeout_and_model_change_mark_cost_incomplete(tmp_path: Path) -> None:
    class Slow:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            await asyncio.Event().wait()
            return reply("Nunca.")

    slow = tmp_path / "slow.db"
    loop = delegating_loop(slow, Slow(), budget=Budget(2, 0.05))
    assert asyncio.run(loop.run(UserMessage("P."))).status is RunStatus.FAILED
    assert outbox_payload(slow)["usage_complete"] is False

    mixed = tmp_path / "mixed.db"
    provider = ScriptedProvider(
        [call_tool("noop"), replace(reply("Fim."), model="other-model")]
    )
    loop = delegating_loop(mixed, provider, tools=Noop())
    assert asyncio.run(loop.run(UserMessage("P."))).status is RunStatus.COMPLETED
    assert outbox_payload(mixed)["usage_complete"] is False


def test_external_cancellation_records_measured_usage_and_propagates(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cancel.db"
    cleaned: list[bool] = []

    async def scenario() -> None:
        started = asyncio.Event()

        class Provider:
            calls = 0

            async def complete(self, request: LLMRequest) -> LLMResponse:
                self.calls += 1
                if self.calls == 1:
                    return call_tool(DELEGATE, CALL.arguments, usage=Usage(40, 4))
                started.set()
                try:
                    await asyncio.Event().wait()
                    return reply("Nunca.")
                finally:
                    cleaned.append(True)

        task = asyncio.create_task(
            delegating_loop(db, Provider()).run(UserMessage("pedido-privado"))
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cleaned == [True]
    with closing(connect(db)) as connection:
        run = connection.execute(
            "SELECT status, input_tokens, output_tokens FROM runs"
        ).fetchone()
    assert tuple(run) == ("failed", 40, 4)
    payload = outbox_payload(db)
    assert payload["outcome"] == "failed" and payload["usage_complete"] is False
    assert payload["delegation"]["failed"] == 1
    text = json.dumps(payload)
    assert "pedido-privado" not in text and PRIVATE not in text


def test_usage_records_price_only_complete_runs_and_keep_delegation_counters(
    tmp_path: Path,
) -> None:
    db = tmp_path / "usage.db"
    complete = ScriptedProvider(
        [
            call_tool(DELEGATE, CALL.arguments),
            reply("Síntese.", Usage(100, 20)),
            reply("Final.", Usage(1000, 100)),
        ]
    )
    incomplete = ScriptedProvider(
        [call_tool(DELEGATE, CALL.arguments), LLMError(PRIVATE, retryable=True)]
    )
    for provider in (complete, incomplete):
        asyncio.run(delegating_loop(db, provider).run(UserMessage("P.")))

    recorder = UsageRecorder({FAKE_MODEL: Price(1.0, 2.0)})
    with closing(connect(db)) as connection:
        events = connection.execute(
            "SELECT type, run_id, payload FROM outbox_events"
        ).fetchall()
        with connection:
            for kind, run_id, payload in events:
                recorder(
                    connection,
                    OutboxEvent(kind, json.loads(payload), run_id=UUID(run_id)),
                )
        records = connection.execute(
            "SELECT u.input_tokens, u.cost_usd, u.usage_complete, d.attempts,"
            " d.failed, d.usage_complete FROM usage_records u"
            " JOIN delegation_records d ON d.run_id = u.run_id"
            " ORDER BY u.input_tokens DESC"
        ).fetchall()
        metrics = daily_metrics(connection, datetime.now(UTC).date(), UTC)
    assert [tuple(r) for r in records] == [
        (1110, pytest.approx((1110 * 1.0 + 125 * 2.0) / 1_000_000), 1, 1, 0, 1),
        (10, None, 0, 1, 1, 0),
    ]
    assert metrics.runs_usage_incomplete == 1
    assert (metrics.delegation_attempts, metrics.delegation_failed) == (2, 1)
    assert metrics.delegation_tokens == 120
    assert "| Delegações (tentativas / falhas / recusadas) | 2 / 1 / 0 |" in (
        render_daily(metrics)
    )


@pytest.mark.parametrize(
    "delegation",
    [
        "texto",
        {"attempts": -1},
        {**dict.fromkeys(DELEGATION_COUNTERS, 0), "usage_complete": 1},
    ],
)
def test_usage_recorder_rejects_invalid_delegation_counters(
    tmp_path: Path, delegation: object
) -> None:
    db = tmp_path / "invalid.db"
    loop = delegating_loop(db, ScriptedProvider([reply("Oi.")]))
    asyncio.run(loop.run(UserMessage("P.")))
    with closing(connect(db)) as connection:
        (run_id,) = connection.execute("SELECT id FROM runs").fetchone()
        event = OutboxEvent(
            "neoson.run.completed",
            {"usage": {}, "delegation": delegation},
            run_id=UUID(run_id),
        )
        with pytest.raises(ValueError, match="delegação"):
            UsageRecorder()(connection, event)
