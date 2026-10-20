import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMError,
    LLMResponse,
    Message,
    PolicyEngine,
    RiskLevel,
    Role,
    RunStatus,
    ToolCall,
    ToolContext,
    ToolRejected,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
    Usage,
    UserMessage,
)
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.loader import package_from_texts
from neoson_core.skills.registry import SqliteSkillRegistry

INSTRUCTIONS = """# Planejar
## Quando usar
Para planejar o dia.
## Passos
Consulte a agenda e ordene os compromissos.
## Restrições
Não invente compromissos.
## Saída
Plano do dia.
"""
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class Catalog:
    def __init__(self) -> None:
        self.called: list[str] = []
        self.risks = {
            "calendar_list": RiskLevel.READ,
            "other": RiskLevel.READ,
            "forbidden": RiskLevel.FORBIDDEN,
            "sensitive": RiskLevel.SENSITIVE_WRITE,
        }

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(name=name, description=name, parameters={"type": "object"})
            for name in self.risks
        )

    def check(self, call: ToolCall) -> RiskLevel:
        if call.name not in self.risks:
            raise ToolRejected("Ferramenta desconhecida.")
        return self.risks[call.name]

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        self.called.append(call.name)
        return ToolResult(call.id, ToolResultStatus.OK, "Reunião às 09:00.")


def approve(
    registry: SqliteSkillRegistry,
    name: str = "planejar",
    *,
    approval: bool = False,
    tools: str = "calendar_list",
) -> None:
    registry.approve(
        package_from_texts(
            Path(name),
            {
                "SKILL.md": INSTRUCTIONS,
                "metadata.yaml": f"id: {name}\nversion: 1.0.0\n"
                "description: Planeja o dia com a agenda.\ntriggers: [planejar dia]\n"
                f"allowed_tools: [{tools}]\n"
                f"requires_approval: {str(approval).lower()}\n"
                "risk_level: low\nowner: neoson\n",
            },
        )
    )


def load(name: str = "planejar", version: str = "1.0.0") -> LLMResponse:
    return call_tool("skill_load", {"skill_id": name, "version": version}, "skill-1")


def agent(
    db: Path,
    script: list[LLMResponse | LLMError],
    *,
    approval: bool = False,
    tools: str = "calendar_list",
    policy: PolicyEngine | None = None,
) -> tuple[AgentLoop, ScriptedProvider, Catalog, SqliteSkillRegistry]:
    registry = SqliteSkillRegistry(db)
    approve(registry, approval=approval, tools=tools)
    provider = ScriptedProvider(script)
    catalog = Catalog()
    loop = AgentLoop(
        provider,
        RulesRouter("balanced"),
        Budget(max_steps=6, timeout_seconds=5),
        tools=catalog,
        policy=policy,
        skills=registry,
        store=SqliteConversationStore(db),
        clock=lambda: NOW,
    )
    return loop, provider, catalog, registry


def rows(db: Path, sql: str) -> list[tuple[object, ...]]:
    connection = connect(db)
    try:
        return [tuple(row) for row in connection.execute(sql)]
    finally:
        connection.close()


def test_skill_procedure_is_system_content_and_execution_is_atomic(
    tmp_path: Path,
) -> None:
    db = tmp_path / "neoson.db"
    loop, provider, catalog, _ = agent(
        db,
        [load(), call_tool("calendar_list", call_id="agenda"), reply("Plano pronto.")],
    )
    result = asyncio.run(loop.run(UserMessage("Planeje meu dia.")))
    assert result.status is RunStatus.COMPLETED
    assert catalog.called == ["calendar_list"]
    assert "planejar" in provider.requests[0].messages[0].content
    assert "skill_load" in {spec.name for spec in provider.requests[0].tools}
    assert {spec.name for spec in provider.requests[1].tools} == {"calendar_list"}
    instruction = provider.requests[1].messages[-1]
    assert (
        instruction.role is Role.SYSTEM and INSTRUCTIONS.strip() in instruction.content
    )
    assert INSTRUCTIONS.strip() not in provider.requests[1].messages[-2].content
    assert rows(
        db, "SELECT skill_id, skill_version, outcome FROM skill_executions"
    ) == [("planejar", "1.0.0", "completed")]
    assert rows(db, "SELECT COUNT(*) FROM messages WHERE role = 'system'") == [(0,)]
    ((payload,),) = rows(
        db, "SELECT payload FROM outbox_events WHERE type = 'neoson.run.completed'"
    )
    assert json.loads(str(payload))["skill_ids"] == ["planejar"]


def test_index_is_stable_and_skill_state_does_not_leak_to_next_run(
    tmp_path: Path,
) -> None:
    loop, provider, catalog, registry = agent(
        tmp_path / "neoson.db", [load(), reply("Ok."), call_tool("other"), reply("Ok.")]
    )
    approve(registry, "zebra")
    approve(registry, "antes")
    first = asyncio.run(loop.run(UserMessage("Planeje.")))
    second = asyncio.run(
        loop.run(UserMessage("Outra tarefa.", session_id=first.session_id))
    )
    assert second.status is RunStatus.COMPLETED
    assert catalog.called == ["other"]
    initial, next_run = provider.requests[0], provider.requests[2]
    assert initial.messages[0] == next_run.messages[0]
    index = initial.messages[0].content
    assert (
        index.index('"id": "antes"')
        < index.index('"id": "planejar"')
        < index.index('"id": "zebra"')
    )
    assert not any(INSTRUCTIONS.strip() in msg.content for msg in next_run.messages)


def test_hidden_tool_is_denied_even_if_model_requests_it(tmp_path: Path) -> None:
    loop, provider, catalog, _ = agent(
        tmp_path / "neoson.db", [load(), call_tool("other"), reply("Sem executar.")]
    )
    assert asyncio.run(loop.run(UserMessage("Planeje."))).status is RunStatus.COMPLETED
    assert catalog.called == []
    assert "não permitida pela skill" in provider.requests[-1].messages[-1].content


def test_skill_requires_approval_even_for_read_only_tools(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    loop, _, catalog, _ = agent(db, [load(), call_tool("calendar_list")], approval=True)
    result = asyncio.run(loop.run(UserMessage("Planeje.")))
    assert result.status is RunStatus.NEEDS_APPROVAL and catalog.called == []
    assert rows(db, "SELECT outcome FROM skill_executions") == [("needs_approval",)]


def test_explicit_approval_allows_the_exact_tool_action(tmp_path: Path) -> None:
    call = call_tool("calendar_list").message.tool_calls[0]
    policy = PolicyEngine()
    policy.grant(call, timedelta(seconds=30))
    loop, _, catalog, _ = agent(
        tmp_path / "neoson.db",
        [load(), call_tool("calendar_list"), reply("Ok.")],
        approval=True,
        policy=policy,
    )
    assert asyncio.run(loop.run(UserMessage("Planeje."))).status is RunStatus.COMPLETED
    assert catalog.called == ["calendar_list"]


@pytest.mark.parametrize(
    "tool,expected",
    [("forbidden", RunStatus.COMPLETED), ("sensitive", RunStatus.NEEDS_APPROVAL)],
)
@pytest.mark.parametrize("approval", [False, True])
def test_skill_cannot_bypass_global_policy(
    tmp_path: Path, tool: str, expected: RunStatus, approval: bool
) -> None:
    loop, _, catalog, _ = agent(
        tmp_path / "neoson.db",
        [load(), call_tool(tool), reply("Não executada.")],
        tools=tool,
        approval=approval,
    )
    assert asyncio.run(loop.run(UserMessage("Planeje."))).status is expected
    assert catalog.called == []


def test_batch_load_applies_restrictions_before_any_tool_executes(
    tmp_path: Path,
) -> None:
    response = LLMResponse(
        Message(
            Role.ASSISTANT,
            "",
            tool_calls=(
                ToolCall("other", "other"),
                load().message.tool_calls[0],
                ToolCall("agenda", "calendar_list"),
            ),
        ),
        Usage(),
        "fake",
    )
    loop, _, catalog, _ = agent(tmp_path / "neoson.db", [response, reply("Ok.")])
    assert asyncio.run(loop.run(UserMessage("Planeje."))).status is RunStatus.COMPLETED
    assert catalog.called == ["calendar_list"]


@pytest.mark.parametrize("name,version", [("ausente", "1.0.0"), ("planejar", "9.0.0")])
def test_failed_load_does_not_execute_other_tools_in_same_batch(
    tmp_path: Path, name: str, version: str
) -> None:
    response = LLMResponse(
        Message(
            Role.ASSISTANT,
            "",
            tool_calls=(
                ToolCall("other", "other"),
                load(name, version).message.tool_calls[0],
            ),
        ),
        Usage(),
        "fake",
    )
    db = tmp_path / "neoson.db"
    loop, _, catalog, _ = agent(db, [response, reply("Indisponível.")])
    asyncio.run(loop.run(UserMessage("Planeje.")))
    assert catalog.called == [] and rows(db, "SELECT * FROM skill_executions") == []


def test_retired_skill_cannot_be_loaded(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    loop, provider, catalog, registry = agent(db, [load(), reply("Indisponível.")])
    registry.retire("planejar")
    asyncio.run(loop.run(UserMessage("Planeje.")))
    assert catalog.called == [] and rows(db, "SELECT * FROM skill_executions") == []
    assert "skill_load" not in {spec.name for spec in provider.requests[0].tools}


def test_only_one_skill_per_run_and_repeat_load_does_not_duplicate_prompt(
    tmp_path: Path,
) -> None:
    loop, provider, _, registry = agent(
        tmp_path / "neoson.db", [load(), load(), load("outra"), reply("Ok.")]
    )
    approve(registry, "outra")
    asyncio.run(loop.run(UserMessage("Planeje.")))
    messages = provider.requests[-1].messages
    assert sum(INSTRUCTIONS.strip() in msg.content for msg in messages) == 1
    assert "Só uma skill" in messages[-1].content


def test_corrupt_catalog_fails_closed_before_calling_model(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    loop, provider, catalog, _ = agent(db, [reply("Nunca.")])
    connection = connect(db)
    try:
        connection.execute("UPDATE skills SET checksum = 'alterado'")
    finally:
        connection.close()
    assert asyncio.run(loop.run(UserMessage("Planeje."))).status is RunStatus.FAILED
    assert provider.requests == [] and catalog.called == []


def test_model_failure_records_the_loaded_skill_as_failed(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    loop, _, _, _ = agent(db, [load(), LLMError("falha", retryable=True)])
    assert asyncio.run(loop.run(UserMessage("Planeje."))).status is RunStatus.FAILED
    assert rows(db, "SELECT outcome FROM skill_executions") == [("failed",)]


def test_outbox_failure_rolls_back_run_and_skill_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "neoson.db"
    loop, _, _, _ = agent(db, [load(), reply("Ok.")])

    def fail(*args: object) -> None:
        raise RuntimeError("outbox indisponível")

    monkeypatch.setattr(outbox, "insert", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(loop.run(UserMessage("Planeje.")))
    assert rows(db, "SELECT COUNT(*) FROM runs") == [(0,)]
    assert rows(db, "SELECT COUNT(*) FROM messages") == [(0,)]
    assert rows(db, "SELECT COUNT(*) FROM skill_executions") == [(0,)]
