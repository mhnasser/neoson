import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMResponse,
    Message,
    PolicyEngine,
    RiskLevel,
    Role,
    RunStatus,
    Skill,
    SkillRef,
    SkillRisk,
    ToolCall,
    ToolContext,
    Usage,
    UserMessage,
    Verdict,
)
from neoson_core.engine.actions import (
    ActionConflict,
    ActionNotFound,
    ActionService,
    PendingAction,
    action_context,
    checksum,
    skill_binding,
)
from neoson_core.infrastructure.actions import SqliteActionStore
from neoson_core.infrastructure.config import ServerSettings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.registry import Tool, ToolOutput, ToolRegistry
from neoson_gateway.app import create_app
from neoson_worker.main import export_vault

PRIVATE = "conteudo-privado-do-pedido"
TOKEN = "test-token-only"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class FakeWrite(Tool[Arguments]):
    name = "fake_write"
    description = "Registra uma ação somente no teste."
    risk = RiskLevel.SENSITIVE_WRITE
    arguments = Arguments

    def __init__(self) -> None:
        self.effects: list[ToolContext] = []
        self.fail = False
        self.slow = False

    async def run(self, arguments: Arguments, context: ToolContext) -> ToolOutput:
        self.effects.append(context)
        if self.slow:
            await asyncio.sleep(1)
        if self.fail:
            raise RuntimeError(PRIVATE)
        return ToolOutput("Efeito fake concluído.")


@dataclass
class Setup:
    path: Path
    tool: FakeWrite
    catalog: ToolRegistry
    engine: AgentLoop
    run: UUID
    action: PendingAction

    def service(self, **changes: Any) -> ActionService:
        args: dict[str, Any] = {
            "store": SqliteActionStore(self.path),
            "tools": self.catalog,
            "policy": PolicyEngine(),
            "context": action_context("runtime-v1", PolicyEngine(), self.catalog),
            "timeout_seconds": 5,
        }
        args.update(changes)
        return ActionService(**args)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    path = tmp_path / "actions.db"
    tool = FakeWrite()
    catalog = ToolRegistry([tool])
    engine = AgentLoop(
        ScriptedProvider([call_tool(tool.name, {"text": PRIVATE})]),
        RulesRouter("balanced"),
        Budget(3, 5),
        tools=catalog,
        store=SqliteConversationStore(path),
        approval_runtime="runtime-v1",
    )
    result = asyncio.run(
        engine.run(UserMessage("Pedido privado", principal="http-owner"))
    )
    assert result.status is RunStatus.NEEDS_APPROVAL and not tool.effects
    actions = SqliteActionStore(path).for_run(result.run_id, "http-owner")
    assert len(actions) == 1
    return Setup(path, tool, catalog, engine, result.run_id, actions[0].action)


def rewrite(setup: Setup, **changes: Any) -> PendingAction:
    action = replace(setup.action, **changes)
    text = action.serialize()
    with closing(connect(setup.path)) as c:
        c.execute(
            "UPDATE pending_actions SET payload=?,checksum=?,expires_at=? WHERE id=?",
            (text, checksum(text), action.expires_at.isoformat(), str(action.id)),
        )
    return action


def test_restart_decision_and_resume_are_durable_and_single_use(setup: Setup) -> None:
    first = setup.service()
    first.store.decide(setup.action.id, "http-owner", True)
    restarted = setup.service()
    assert restarted.store.get(setup.action.id, "http-owner").state == "approved"
    assert (
        asyncio.run(restarted.resume(setup.action.id, "http-owner")).state
        == "completed"
    )
    assert (
        asyncio.run(setup.service().resume(setup.action.id, "http-owner")).state
        == "completed"
    )
    assert len(setup.tool.effects) == 1
    effect = setup.tool.effects[0]
    assert effect.action_id == setup.action.id and effect.run_id == setup.run
    assert effect.session_id == setup.action.session_id


@pytest.mark.parametrize("operation", ["get", "decide", "resume"])
def test_other_identity_cannot_see_or_execute(setup: Setup, operation: str) -> None:
    service = setup.service()
    with pytest.raises(ActionNotFound):
        if operation == "get":
            service.store.get(setup.action.id, "other-owner")
        elif operation == "decide":
            service.store.decide(setup.action.id, "other-owner", True)
        else:
            asyncio.run(service.resume(setup.action.id, "other-owner"))
    assert not service.store.for_run(setup.run, "other-owner")
    assert not setup.tool.effects


@pytest.mark.parametrize("approve", [True, False])
def test_expired_action_never_executes(setup: Setup, approve: bool) -> None:
    rewrite(setup, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    service = setup.service()
    assert service.store.get(setup.action.id, "http-owner").state == "expired"
    with pytest.raises(ActionConflict):
        service.store.decide(setup.action.id, "http-owner", approve)
    with pytest.raises(ActionConflict):
        asyncio.run(service.resume(setup.action.id, "http-owner"))
    assert not setup.tool.effects


def test_denial_is_idempotent_and_cannot_be_overridden(setup: Setup) -> None:
    service = setup.service()
    assert service.store.decide(setup.action.id, "http-owner", False).state == "denied"
    assert service.store.decide(setup.action.id, "http-owner", False).state == "denied"
    with pytest.raises(ActionConflict):
        service.store.decide(setup.action.id, "http-owner", True)
    with pytest.raises(ActionConflict):
        asyncio.run(service.resume(setup.action.id, "http-owner"))
    assert not setup.tool.effects


@pytest.mark.parametrize("field", ["payload", "owner", "expires_at", "run_id"])
def test_tampered_record_never_executes(setup: Setup, field: str) -> None:
    service = setup.service()
    service.store.decide(setup.action.id, "http-owner", True)
    with closing(connect(setup.path)) as c:
        if field == "payload":
            value = json.loads(setup.action.serialize())
            value["call"]["arguments"]["text"] = "alterado"
            c.execute("UPDATE pending_actions SET payload=?", (json.dumps(value),))
        elif field == "owner":
            c.execute("UPDATE pending_actions SET owner='other-owner'")
        elif field == "expires_at":
            c.execute(
                "UPDATE pending_actions SET expires_at='2099-01-01T00:00:00+00:00'"
            )
        else:
            # Violação de vínculo com a execução existente, mesmo com FK válida.
            other = str(uuid4())
            c.execute(
                "INSERT INTO runs (id,session_id,status,started_at,ended_at) "
                "SELECT ?,session_id,status,started_at,ended_at FROM runs LIMIT 1",
                (other,),
            )
            c.execute("UPDATE pending_actions SET run_id=?", (other,))
    with pytest.raises((ActionConflict, ActionNotFound)):
        asyncio.run(service.resume(setup.action.id, "http-owner"))
    assert not setup.tool.effects


@pytest.mark.parametrize("kind", ["runtime", "missing", "forbidden", "risk", "schema"])
def test_changed_context_and_permissions_block_execution(
    setup: Setup, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = setup.service()
    service.store.decide(setup.action.id, "http-owner", True)
    if kind == "runtime":
        service.context = "0" * 64
    elif kind == "missing":
        service.tools = ToolRegistry([])
    elif kind in ("forbidden", "risk"):
        monkeypatch.setattr(
            setup.tool,
            "risk",
            RiskLevel.FORBIDDEN if kind == "forbidden" else RiskLevel.READ,
        )
    else:

        class NewArguments(BaseModel):
            required: int

        monkeypatch.setattr(setup.tool, "arguments", NewArguments)
    with pytest.raises(ActionConflict):
        asyncio.run(service.resume(setup.action.id, "http-owner"))
    assert service.store.get(setup.action.id, "http-owner").state == "invalid"
    assert not setup.tool.effects


def test_concurrent_resumes_cannot_duplicate_effect(setup: Setup) -> None:
    service = setup.service()
    service.store.decide(setup.action.id, "http-owner", True)
    barrier = Barrier(2)

    def execute(_: int) -> str:
        barrier.wait(timeout=5)
        try:
            return asyncio.run(
                setup.service().resume(setup.action.id, "http-owner")
            ).state
        except ActionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert "completed" in results and len(setup.tool.effects) == 1


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_uncertain_result_never_retries_and_does_not_log_private_content(
    setup: Setup, failure: str, caplog: Any
) -> None:
    setup.tool.fail = failure == "error"
    setup.tool.slow = failure == "timeout"
    service = setup.service(timeout_seconds=0.01 if failure == "timeout" else 5)
    service.store.decide(setup.action.id, "http-owner", True)
    assert asyncio.run(service.resume(setup.action.id, "http-owner")).state == "unknown"
    with pytest.raises(ActionConflict):
        asyncio.run(setup.service().resume(setup.action.id, "http-owner"))
    assert len(setup.tool.effects) == 1 and PRIVATE not in caplog.text


def test_crash_after_reservation_never_releases_action(setup: Setup) -> None:
    store = setup.service().store
    store.decide(setup.action.id, "http-owner", True)
    assert store.claim(setup.action)
    with pytest.raises(ActionConflict):
        asyncio.run(setup.service().resume(setup.action.id, "http-owner"))
    assert not setup.tool.effects


def test_decision_and_outbox_are_atomic(setup: Setup) -> None:
    store = setup.service().store
    with closing(connect(setup.path)) as c:
        c.execute(
            "CREATE TRIGGER reject_action_event BEFORE INSERT ON outbox_events "
            "WHEN NEW.type='neoson.action.approved' "
            "BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.decide(setup.action.id, "http-owner", True)
    assert store.get(setup.action.id, "http-owner").state == "pending"
    assert not setup.tool.effects


def test_http_discovery_decision_and_resume_after_restart(setup: Setup) -> None:
    client = TestClient(create_app(setup.engine, TOKEN, setup.service()), headers=AUTH)
    actions = client.get(f"/api/v1/runs/{setup.run}/actions")
    assert actions.status_code == 200 and actions.headers["cache-control"] == "no-store"
    body = actions.json()[0]
    assert body["arguments"] == {"text": PRIVATE} and "owner" not in body
    url = f"/api/v1/actions/{body['id']}"
    assert client.post(url + "/resume").status_code == 409
    assert (
        client.post(url + "/decision", json={"approve": True}).json()["state"]
        == "approved"
    )
    restarted = TestClient(
        create_app(setup.engine, TOKEN, setup.service()), headers=AUTH
    )
    assert restarted.post(url + "/resume").json()["state"] == "completed"
    assert restarted.post(url + "/resume").json()["state"] == "completed"
    assert len(setup.tool.effects) == 1


@pytest.mark.parametrize(
    "path,method",
    [
        ("/actions/{id}", "get"),
        ("/runs/{run}/actions", "get"),
        ("/actions/{id}/decision", "post"),
        ("/actions/{id}/resume", "post"),
    ],
)
def test_all_endpoints_require_auth(setup: Setup, path: str, method: str) -> None:
    client = TestClient(create_app(setup.engine, TOKEN, setup.service()))
    path = "/api/v1" + path.format(id=setup.action.id, run=setup.run)
    response = client.request(method, path, json={"approve": True})
    assert response.status_code == 401 and PRIVATE not in response.text
    assert not setup.tool.effects


@pytest.mark.parametrize(
    "body",
    [
        {"approve": True, "arguments": {"text": "alterado"}},
        {"approve": True, "principal": "other-owner"},
        {"approve": "yes"},
    ],
)
def test_decision_rejects_arguments_identity_and_coercion(
    setup: Setup, body: Any
) -> None:
    client = TestClient(create_app(setup.engine, TOKEN, setup.service()), headers=AUTH)
    result = client.post(f"/api/v1/actions/{setup.action.id}/decision", json=body)
    assert result.status_code == 422
    assert not setup.tool.effects


def test_vault_and_outbox_never_include_action_arguments(
    setup: Setup, tmp_path: Path
) -> None:
    service = setup.service()
    service.store.decide(setup.action.id, "http-owner", True)
    asyncio.run(service.resume(setup.action.id, "http-owner"))
    settings = ServerSettings(
        _env_file=None, db_path=setup.path, vault_path=tmp_path / "vault"
    )
    export_vault(settings)
    assert all(
        PRIVATE not in p.read_text(encoding="utf-8")
        for p in settings.vault_path.rglob("*.md")
    )
    with closing(connect(setup.path)) as c:
        assert all(
            PRIVATE not in r[0] for r in c.execute("SELECT payload FROM outbox_events")
        )


def test_run_messages_outbox_and_actions_are_one_transaction(setup: Setup) -> None:
    with closing(connect(setup.path)) as c:
        before = [
            c.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
            for t in ("runs", "messages", "outbox_events", "pending_actions")
        ]
        c.execute(
            "CREATE TRIGGER reject_pending BEFORE INSERT ON pending_actions "
            "BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    engine = AgentLoop(
        ScriptedProvider([call_tool("fake_write", {"text": PRIVATE})]),
        RulesRouter("balanced"),
        Budget(3, 5),
        tools=setup.catalog,
        store=SqliteConversationStore(setup.path),
        approval_runtime="runtime-v1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(engine.run(UserMessage("ação", principal="http-owner")))
    with closing(connect(setup.path)) as c:
        after = [
            c.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
            for t in ("runs", "messages", "outbox_events", "pending_actions")
        ]
    assert after == before and not setup.tool.effects


def test_failure_to_confirm_after_effect_preserves_reservation(setup: Setup) -> None:
    service = setup.service()
    service.store.decide(setup.action.id, "http-owner", True)
    with closing(connect(setup.path)) as c:
        c.execute(
            "CREATE TRIGGER reject_confirmation BEFORE INSERT ON outbox_events "
            "WHEN NEW.type='neoson.action.completed' "
            "BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(service.resume(setup.action.id, "http-owner"))
    assert setup.service().store.get(setup.action.id, "http-owner").state == "executing"
    with pytest.raises(ActionConflict):
        asyncio.run(setup.service().resume(setup.action.id, "http-owner"))
    assert len(setup.tool.effects) == 1


@pytest.mark.parametrize(
    "change", ["revoked", "permissions", "checksum", "version", "unchanged"]
)
def test_skill_is_revalidated_before_execution(setup: Setup, change: str) -> None:
    skill = Skill(
        SkillRef("example", "1.0.0", "Exemplo"),
        PRIVATE,
        frozenset({"fake_write"}),
        True,
        SkillRisk.LOW,
        "a" * 64,
    )
    rewrite(setup, skill=skill_binding(skill))

    class Registry:
        async def load_approved(self, skill_id: str, version: str) -> Skill:
            if change == "revoked":
                raise LookupError
            if change == "permissions":
                return replace(skill, allowed_tools=frozenset())
            if change == "checksum":
                return replace(skill, checksum="b" * 64)
            if change == "version":
                return replace(skill, ref=SkillRef("example", "2.0.0", "Exemplo"))
            return skill

        async def index(self) -> tuple[SkillRef, ...]:
            return (skill.ref,)

    service = setup.service(skills=Registry())
    service.store.decide(setup.action.id, "http-owner", True)
    if change == "unchanged":
        assert (
            asyncio.run(service.resume(setup.action.id, "http-owner")).state
            == "completed"
        )
        assert len(setup.tool.effects) == 1
    else:
        with pytest.raises(ActionConflict):
            asyncio.run(service.resume(setup.action.id, "http-owner"))
        assert not setup.tool.effects


def test_http_assigns_owner_and_does_not_trust_message_principal(setup: Setup) -> None:
    engine = AgentLoop(
        ScriptedProvider([call_tool("fake_write", {"text": PRIVATE})]),
        RulesRouter("balanced"),
        Budget(3, 5),
        tools=setup.catalog,
        store=SqliteConversationStore(setup.path),
        approval_runtime="runtime-v1",
    )
    service = setup.service()
    client = TestClient(create_app(engine, TOKEN, service), headers=AUTH)
    result = client.post(
        "/api/v1/messages", json={"text": "nova ação", "principal": "other-owner"}
    )
    assert result.status_code == 200 and result.json()["status"] == "needs_approval"
    assert PRIVATE not in result.text
    run_id = UUID(result.json()["run_id"])
    assert len(service.store.for_run(run_id, "http-owner")) == 1
    assert not service.store.for_run(run_id, "other-owner")


def test_exhausted_token_budget_does_not_create_resumable_action(setup: Setup) -> None:
    engine = AgentLoop(
        ScriptedProvider([call_tool("fake_write", {"text": PRIVATE})]),
        RulesRouter("balanced"),
        Budget(1, 5, max_tokens=15),
        tools=setup.catalog,
        store=SqliteConversationStore(setup.path),
        approval_runtime="runtime-v1",
    )
    result = asyncio.run(engine.run(UserMessage("ação", principal="http-owner")))
    assert result.status is RunStatus.FAILED
    assert not setup.service().store.for_run(result.run_id, "http-owner")
    assert not setup.tool.effects


@pytest.mark.parametrize("timeout", [0, -1, float("inf")])
def test_current_execution_budget_is_revalidated(setup: Setup, timeout: float) -> None:
    service = setup.service(timeout_seconds=timeout)
    service.store.decide(setup.action.id, "http-owner", True)
    with pytest.raises(ActionConflict):
        asyncio.run(service.resume(setup.action.id, "http-owner"))
    assert not setup.tool.effects


def test_status_without_approval_decision_cannot_execute(setup: Setup) -> None:
    with closing(connect(setup.path)) as c:
        c.execute("UPDATE pending_actions SET state='approved'")
    with pytest.raises(ActionConflict):
        asyncio.run(setup.service().resume(setup.action.id, "http-owner"))
    assert not setup.tool.effects


def test_resume_cannot_replace_approved_arguments(setup: Setup) -> None:
    service = setup.service()
    service.store.decide(setup.action.id, "http-owner", True)
    client = TestClient(create_app(setup.engine, TOKEN, service), headers=AUTH)
    result = client.post(
        f"/api/v1/actions/{setup.action.id}/resume",
        json={"arguments": {"text": "alterado"}},
    )
    assert result.status_code == 422 and not setup.tool.effects
    assert service.store.get(setup.action.id, "http-owner").state == "approved"


def test_batch_actions_require_separate_decisions_and_share_time_budget(
    setup: Setup,
) -> None:
    class FakeRead(FakeWrite):
        name = "fake_read"
        risk = RiskLevel.READ

    read = FakeRead()
    catalog = ToolRegistry([setup.tool, read])
    provider = ScriptedProvider(
        [
            LLMResponse(
                Message(
                    Role.ASSISTANT,
                    "",
                    tool_calls=(
                        ToolCall("a", "fake_write", {"text": "primeira"}),
                        ToolCall("b", "fake_write", {"text": "segunda"}),
                        ToolCall("c", "fake_read", {"text": "leitura"}),
                    ),
                ),
                Usage(10, 5),
                "fake-model",
            )
        ]
    )
    engine = AgentLoop(
        provider,
        RulesRouter("balanced"),
        Budget(1, 5),
        tools=catalog,
        store=SqliteConversationStore(setup.path),
        approval_runtime="runtime-v1",
    )
    result = asyncio.run(engine.run(UserMessage("lote", principal="http-owner")))
    store = SqliteActionStore(setup.path)
    actions = store.for_run(result.run_id, "http-owner")
    assert len(actions) == 2 and not setup.tool.effects and not read.effects
    assert sum(a.action.timeout_seconds for a in actions) <= 5
    service = ActionService(
        store,
        catalog,
        PolicyEngine(),
        action_context("runtime-v1", PolicyEngine(), catalog),
        5,
    )
    store.decide(actions[0].action.id, "http-owner", True)
    asyncio.run(service.resume(actions[0].action.id, "http-owner"))
    with pytest.raises(ActionConflict):
        asyncio.run(service.resume(actions[1].action.id, "http-owner"))
    assert len(setup.tool.effects) == 1 and not read.effects
    assert len(provider.requests) == 1


def test_concurrent_opposite_decisions_have_one_winner(setup: Setup) -> None:
    barrier = Barrier(2)

    def decide(approve: bool) -> str:
        barrier.wait(timeout=5)
        try:
            return (
                setup.service()
                .store.decide(setup.action.id, "http-owner", approve)
                .state
            )
        except ActionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, [True, False]))
    assert results.count("conflict") == 1
    assert setup.service().store.get(setup.action.id, "http-owner").state in (
        "approved",
        "denied",
    )
    assert not setup.tool.effects


def test_cancellation_keeps_uncertain_action_blocked(setup: Setup) -> None:
    setup.tool.slow = True
    service = setup.service()
    service.store.decide(setup.action.id, "http-owner", True)

    async def cancel() -> None:
        task = asyncio.create_task(service.resume(setup.action.id, "http-owner"))
        while not setup.tool.effects:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert setup.service().store.get(setup.action.id, "http-owner").state == "unknown"
    assert len(setup.tool.effects) == 1


def test_current_policy_denial_overrides_persisted_approval(setup: Setup) -> None:
    class DenyPolicy(PolicyEngine):
        def evaluate(
            self, call: ToolCall, risk: RiskLevel, now: datetime | None = None
        ) -> Verdict:
            return Verdict.DENY

    service = setup.service(policy=DenyPolicy())
    service.store.decide(setup.action.id, "http-owner", True)
    with pytest.raises(ActionConflict):
        asyncio.run(service.resume(setup.action.id, "http-owner"))
    assert not setup.tool.effects
    assert service.store.get(setup.action.id, "http-owner").state == "invalid"
