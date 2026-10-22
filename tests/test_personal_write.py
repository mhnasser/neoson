import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from neoson_core.engine import (
    AgentLoop,
    Budget,
    PolicyEngine,
    RunStatus,
    ToolCall,
    ToolContext,
    UserMessage,
)
from neoson_core.engine.actions import ActionConflict, ActionService, action_context
from neoson_core.engine.integrations import IntegrationError, PersonalEffect
from neoson_core.engine.messages import ToolResultStatus
from neoson_core.engine.tools import RiskLevel, ToolRejected, Verdict
from neoson_core.infrastructure.actions import SqliteActionStore
from neoson_core.infrastructure.config import ConfigError, ServerSettings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.integration_http import ClientFactory
from neoson_core.tools.microsoft_oauth import WRITE_SCOPES, MicrosoftOAuth
from neoson_core.tools.microsoft_write import SEAL_PROPERTY, MicrosoftPersonalWriter
from neoson_core.tools.oauth_store import LocalTokenStore, OAuthTokens
from neoson_core.tools.personal_sandbox import SqlitePersonalSandbox
from neoson_core.tools.personal_write import CalendarCreate, EmailDraft
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.app import create_app
from neoson_gateway.composition import personal_writers
from neoson_gateway.routing_runtime import runtime_hash, write_binding

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
RECIPIENT = "person" + chr(64) + "example.invalid"
PRIVATE = "conteudo-privado-do-rascunho"
CLIENT = "00000000-0000-4000-8000-000000000001"
AUTH = {"Authorization": "Bearer ficticio-local"}
EVENT: dict[str, object] = {
    "title": PRIVATE,
    "start": NOW.isoformat(),
    "end": (NOW + timedelta(hours=1)).isoformat(),
}
DRAFT: dict[str, object] = {"recipient": RECIPIENT, "subject": PRIVATE, "body": PRIVATE}


def factory(handler: Callable[[httpx2.Request], httpx2.Response]) -> ClientFactory:
    return lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


class Tokens:
    async def access_token(self) -> str:
        return "access-ficticio"


def setup(
    tmp_path: Path,
    name: str,
    arguments: dict[str, object],
    writer: Any = None,
) -> tuple[AgentLoop, ActionService, SqlitePersonalSandbox, UUID]:
    sandbox = SqlitePersonalSandbox(tmp_path / "sandbox.db")
    tools = ToolRegistry(
        [
            CalendarCreate(writer or sandbox, "sandbox local simulado"),
            EmailDraft(writer or sandbox, "sandbox local simulado"),
        ]
    )
    engine = AgentLoop(
        ScriptedProvider([call_tool(name, arguments)]),
        RulesRouter("balanced"),
        Budget(3, 5),
        tools=tools,
        store=SqliteConversationStore(tmp_path / "conversations.db"),
        approval_runtime="personal-write-v1",
    )
    service = ActionService(
        SqliteActionStore(tmp_path / "conversations.db"),
        tools,
        PolicyEngine(),
        action_context("personal-write-v1", PolicyEngine(), tools),
        5,
    )
    result = asyncio.run(
        engine.run(UserMessage("Crie um item.", principal="http-owner"))
    )
    assert result.status is RunStatus.NEEDS_APPROVAL
    action = service.store.for_run(result.run_id, "http-owner")[0].action
    return engine, service, sandbox, action.id


@pytest.mark.parametrize(
    "name,arguments", [("calendar_create", EVENT), ("email_draft", DRAFT)]
)
def test_review_exact_arguments_then_restart_resume_does_not_duplicate(
    tmp_path: Path,
    name: str,
    arguments: dict[str, object],
) -> None:
    engine, service, sandbox, action_id = setup(tmp_path, name, arguments)
    assert not sandbox.path.exists()
    with TestClient(
        create_app(engine, "ficticio-local", service), headers=AUTH
    ) as client:
        url = "/api/v1/actions/" + str(action_id)
        reviewed = client.get(url)
        assert reviewed.status_code == 200 and reviewed.json()["arguments"] == arguments
        assert reviewed.headers["cache-control"] == "no-store"
        assert "sandbox local simulado" in reviewed.json()["tool_description"]
        assert client.post(url + "/resume").status_code == 409
        assert (
            client.post(
                url + "/decision", json={"approve": True, "arguments": {}}
            ).status_code
            == 422
        )
        assert (
            client.post(url + "/decision", json={"approve": True}).json()["state"]
            == "approved"
        )
    # Nova composição e nova instância de sandbox, usando somente estados persistidos.
    replacement = SqlitePersonalSandbox(sandbox.path)
    tools = ToolRegistry([CalendarCreate(replacement), EmailDraft(replacement)])
    restarted = ActionService(service.store, tools, PolicyEngine(), service.context, 5)
    assert asyncio.run(restarted.resume(action_id, "http-owner")).state == "completed"
    assert asyncio.run(restarted.resume(action_id, "http-owner")).state == "completed"
    with closing(connect(sandbox.path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sandbox_effects").fetchone()[0]
            == 1
        )
        row = connection.execute(
            "SELECT action_id,payload FROM sandbox_effects"
        ).fetchone()
        assert row[0] == str(action_id) and PRIVATE in row[1]
    with closing(connect(tmp_path / "conversations.db")) as connection:
        assert all(
            PRIVATE not in r[0]
            for r in connection.execute("SELECT payload FROM outbox_events")
        )


@pytest.mark.parametrize(
    "name,arguments", [("calendar_create", EVENT), ("email_draft", DRAFT)]
)
def test_model_or_memory_grant_cannot_execute_without_persistent_action(
    tmp_path: Path,
    name: str,
    arguments: dict[str, object],
) -> None:
    sandbox = SqlitePersonalSandbox(tmp_path / "sandbox.db")
    tools = ToolRegistry([CalendarCreate(sandbox), EmailDraft(sandbox)])
    call = ToolCall("c", name, arguments)
    assert tools.check(call) is RiskLevel.SENSITIVE_WRITE
    policy = PolicyEngine()
    assert policy.evaluate(call, tools.check(call)) is Verdict.REQUIRE_APPROVAL
    policy.grant(call, timedelta(minutes=1))
    assert policy.evaluate(call, tools.check(call)) is Verdict.ALLOW
    result = asyncio.run(tools.execute(call, ToolContext(uuid4(), uuid4())))
    assert result.status is ToolResultStatus.ERROR and not sandbox.path.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {**EVENT, "attendees": ["x"]},
        {**EVENT, "end": NOW.isoformat()},
        {**EVENT, "start": "2026-10-22T12:00:00"},
        {**EVENT, "end": (NOW + timedelta(days=2)).isoformat()},
    ],
)
def test_event_bounds_and_no_invitees(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    tools = ToolRegistry(
        [CalendarCreate(SqlitePersonalSandbox(tmp_path / "sandbox.db"))]
    )
    with pytest.raises(ToolRejected):
        tools.check(ToolCall("c", "calendar_create", arguments))


@pytest.mark.parametrize(
    "change",
    [
        {"recipient": RECIPIENT + ";" + RECIPIENT},
        {"recipient": "bad\r\nBcc:x"},
        {"body": "x" * 8001},
        {"subject": ""},
        {"send": True},
        {"attachments": []},
    ],
)
def test_draft_bounds_and_no_send_or_attachments(
    tmp_path: Path, change: dict[str, object]
) -> None:
    tools = ToolRegistry([EmailDraft(SqlitePersonalSandbox(tmp_path / "sandbox.db"))])
    with pytest.raises(ToolRejected):
        tools.check(ToolCall("c", "email_draft", {**DRAFT, **change}))


def test_sandbox_replay_and_changed_arguments(tmp_path: Path) -> None:
    sandbox = SqlitePersonalSandbox(tmp_path / "sandbox.db")
    action_id = uuid4()
    first = asyncio.run(sandbox.create_draft(RECIPIENT, PRIVATE, PRIVATE, action_id))
    second = asyncio.run(
        SqlitePersonalSandbox(sandbox.path).create_draft(
            RECIPIENT, PRIVATE, PRIVATE, action_id
        )
    )
    assert first == second and first.simulated
    with pytest.raises(IntegrationError, match="idempotency"):
        asyncio.run(sandbox.create_draft(RECIPIENT, PRIVATE, "changed", action_id))


def test_draft_preserves_exact_reviewed_whitespace(tmp_path: Path) -> None:
    arguments = {**DRAFT, "body": "  parágrafo privado\n\n"}
    _, service, sandbox, action_id = setup(tmp_path, "email_draft", arguments)
    service.store.decide(action_id, "http-owner", True)
    assert asyncio.run(service.resume(action_id, "http-owner")).state == "completed"
    with closing(connect(sandbox.path)) as connection:
        payload = json.loads(
            connection.execute("SELECT payload FROM sandbox_effects").fetchone()[0]
        )
        assert payload["payload"]["body"] == arguments["body"]


def test_ambiguous_failure_after_effect_never_repeats(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sandbox = SqlitePersonalSandbox(tmp_path / "effects.db")

    class Ambiguous(SqlitePersonalSandbox):
        async def create_draft(
            self, recipient: str, subject: str, body: str, action_id: UUID
        ) -> Any:
            await super().create_draft(recipient, subject, body, action_id)
            raise IntegrationError("timeout")

    _, service, _, action_id = setup(
        tmp_path, "email_draft", DRAFT, Ambiguous(sandbox.path)
    )
    service.store.decide(action_id, "http-owner", True)
    assert asyncio.run(service.resume(action_id, "http-owner")).state == "unknown"
    with pytest.raises(ActionConflict):
        asyncio.run(service.resume(action_id, "http-owner"))
    with closing(connect(sandbox.path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sandbox_effects").fetchone()[0]
            == 1
        )
    assert PRIVATE not in caplog.text and RECIPIENT not in caplog.text


@pytest.mark.parametrize("kind", ["calendar", "draft"])
def test_graph_posts_only_exact_supported_effect_and_recovers_existing(
    kind: str,
) -> None:
    created: dict[str, Any] = {}
    methods: list[str] = []
    action_id = uuid4()

    def handle(request: httpx2.Request) -> httpx2.Response:
        methods.append(request.method)
        assert request.url.host == "graph.microsoft.com"
        assert request.url.path == (
            "/v1.0/me/calendar/events" if kind == "calendar" else "/v1.0/me/messages"
        )
        assert "send" not in request.url.path.lower()
        if request.method == "GET":
            assert str(action_id) in request.url.params["$filter"]
            return httpx2.Response(200, json={"value": [created] if created else []})
        assert request.method == "POST"
        body = json.loads(request.content)
        if kind == "calendar":
            assert body["attendees"] == [] and body["transactionId"] == str(action_id)
            assert set(body) == {
                "subject",
                "start",
                "end",
                "attendees",
                "isReminderOn",
                "transactionId",
                "singleValueExtendedProperties",
            }
        else:
            assert body["body"]["contentType"] == "Text"
            assert len(body["toRecipients"]) == 1
            assert set(body) == {
                "subject",
                "body",
                "toRecipients",
                "singleValueExtendedProperties",
            }
        created.update(
            {
                "id": "effect-1",
                "isDraft": True,
                "singleValueExtendedProperties": [
                    p
                    for p in body["singleValueExtendedProperties"]
                    if p["id"] == SEAL_PROPERTY
                ],
            }
        )
        return httpx2.Response(201, json=created)

    writer = MicrosoftPersonalWriter(Tokens(), factory=factory(handle))

    async def scenario() -> None:
        for _ in range(2):
            result = await (
                writer.create_event(PRIVATE, NOW, NOW + timedelta(hours=1), action_id)
                if kind == "calendar"
                else writer.create_draft(RECIPIENT, PRIVATE, PRIVATE, action_id)
            )
            assert result.id == "effect-1" and not result.simulated

    asyncio.run(scenario())
    assert methods == ["GET", "POST", "GET"]


@pytest.mark.parametrize("status", [429, 503, 302])
def test_graph_write_failure_never_retries(status: int) -> None:
    methods: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx2.Response(200, json={"value": []})
        return httpx2.Response(
            status,
            json={"error": PRIVATE},
            headers={"Location": "https://example.invalid"},
        )

    writer = MicrosoftPersonalWriter(Tokens(), factory=factory(handle))
    with pytest.raises(IntegrationError) as failure:
        asyncio.run(writer.create_draft(RECIPIENT, PRIVATE, PRIVATE, uuid4()))
    assert PRIVATE not in str(failure.value) and methods == ["GET", "POST"]


def test_write_oauth_scope_cache_and_binding_are_separate(tmp_path: Path) -> None:
    binding = str(uuid4())
    store = LocalTokenStore(tmp_path / "write.bin")
    tokens = OAuthTokens(
        CLIENT,
        "consumers",
        "access-ficticio",
        "refresh-ficticio",
        NOW.timestamp() + 3600,
        tuple(WRITE_SCOPES),
        binding,
    )
    store.save(tokens)
    write = MicrosoftOAuth(
        CLIENT,
        "consumers",
        store,
        write=True,
        expected_binding=binding,
        clock=lambda: NOW.timestamp(),
    )
    assert (
        "Mail.Send" not in write.login_scopes and "Mail.ReadWrite" in write.login_scopes
    )
    assert asyncio.run(write.access_token()) == "access-ficticio"
    read = MicrosoftOAuth(CLIENT, "consumers", store, clock=lambda: NOW.timestamp())
    with pytest.raises(IntegrationError):
        asyncio.run(read.access_token())
    settings = ServerSettings(
        _env_file=None,
        llm_model="model-ficticio",
        personal_write_provider="microsoft",
        microsoft_write_token_path=store.path,
    )
    before = runtime_hash(settings)
    store.save(replace(tokens, binding=str(uuid4())))
    assert runtime_hash(settings) != before and write_binding(settings) != binding
    with pytest.raises(IntegrationError):
        asyncio.run(write.access_token())


def test_write_provider_defaults_and_sandbox_isolation(tmp_path: Path) -> None:
    settings = ServerSettings(_env_file=None)
    assert settings.personal_write_provider == "disabled"
    assert "email_send" not in {
        s.name
        for s in ToolRegistry(
            [
                CalendarCreate(personal_writers(settings)[0]),
                EmailDraft(personal_writers(settings)[1]),
            ]
        ).specs()
    }
    fake = settings.model_copy(
        update={
            "personal_write_provider": "fake",
            "personal_sandbox_db_path": tmp_path / "sandbox.db",
        }
    )
    assert isinstance(personal_writers(fake)[0], SqlitePersonalSandbox)
    with pytest.raises(ConfigError):
        personal_writers(fake.model_copy(update={"env": "production"}))
    with pytest.raises(ConfigError):
        personal_writers(
            fake.model_copy(update={"personal_sandbox_db_path": fake.db_path})
        )


def test_timeout_after_effect_preserves_unknown_without_repeating(
    tmp_path: Path,
) -> None:
    class Slow(SqlitePersonalSandbox):
        async def create_draft(
            self,
            recipient: str,
            subject: str,
            body: str,
            action_id: UUID,
        ) -> PersonalEffect:
            effect = await super().create_draft(recipient, subject, body, action_id)
            await asyncio.Event().wait()
            return effect

    writer = Slow(tmp_path / "effects.db")
    _, service, _, action_id = setup(tmp_path, "email_draft", DRAFT, writer)
    service.timeout_seconds = 0.01
    service.store.decide(action_id, "http-owner", True)
    assert asyncio.run(service.resume(action_id, "http-owner")).state == "unknown"
    with pytest.raises(ActionConflict):
        asyncio.run(service.resume(action_id, "http-owner"))
    with closing(connect(writer.path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sandbox_effects").fetchone()[0]
            == 1
        )


def test_sandbox_concurrency_preserves_one_effect(tmp_path: Path) -> None:
    path = tmp_path / "effects.db"
    action_id = uuid4()
    # Inicializar tabela antes das duas conexões concorrentes.
    asyncio.run(
        SqlitePersonalSandbox(path).create_draft(RECIPIENT, PRIVATE, PRIVATE, action_id)
    )

    def execute(_: int) -> PersonalEffect:
        return asyncio.run(
            SqlitePersonalSandbox(path).create_draft(
                RECIPIENT, PRIVATE, PRIVATE, action_id
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert results[0] == results[1]
    with closing(connect(path)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM sandbox_effects").fetchone()[0]
            == 1
        )


def test_refresh_preserves_write_login_binding(tmp_path: Path) -> None:
    store = LocalTokenStore(tmp_path / "write.bin")
    binding = str(uuid4())
    store.save(
        OAuthTokens(
            CLIENT,
            "consumers",
            "old",
            "refresh-ficticio",
            0,
            tuple(WRITE_SCOPES),
            binding,
        )
    )

    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST" and request.url.path.endswith("/token")
        return httpx2.Response(
            200,
            json={
                "access_token": "new",
                "refresh_token": "rotated-ficticio",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": " ".join(sorted(WRITE_SCOPES)),
            },
        )

    client = MicrosoftOAuth(
        CLIENT,
        "consumers",
        store,
        write=True,
        expected_binding=binding,
        factory=factory(handle),
    )
    assert asyncio.run(client.access_token()) == "new"
    assert store.load().binding == binding


@pytest.mark.parametrize(
    "row",
    [
        {"id": "effect", "isDraft": False, "singleValueExtendedProperties": []},
        {"id": "effect", "isDraft": True, "singleValueExtendedProperties": []},
    ],
)
def test_preflight_mismatch_or_existing_sent_message_never_posts(
    row: dict[str, object],
) -> None:
    methods: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        methods.append(request.method)
        return httpx2.Response(200, json={"value": [row]})

    writer = MicrosoftPersonalWriter(Tokens(), factory=factory(handle))
    with pytest.raises(IntegrationError, match="idempotency"):
        asyncio.run(writer.create_draft(RECIPIENT, PRIVATE, PRIVATE, uuid4()))
    assert methods == ["GET"]
