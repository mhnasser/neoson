"""Jornada isolada: voz simulada → análise delegada → aprovação concreta → efeito no
sandbox → feedback no Robot. Cliente, gateway, motor e banco são reais; fala,
dispositivos, modelo e destino da escrita são simulados."""

import asyncio
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request

from neoson_contracts.events.v1.robot import RobotState
from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    PolicyEngine,
    Usage,
)
from neoson_core.engine.actions import ActionService, action_context
from neoson_core.engine.delegation import DELEGATE, DelegationLimits
from neoson_core.engine.voice import VoicePipeline
from neoson_core.infrastructure.actions import SqliteActionStore
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.providers.fake_speech import FakeSpeech
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.personal_sandbox import SqlitePersonalSandbox
from neoson_core.tools.personal_write import CalendarCreate, EmailDraft
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.app import create_app
from neoson_gateway.voice import voice_router
from neoson_robot.fake_devices import FakeCapture, FakeDisplay, FakePlayback, silent_wav
from neoson_robot.session_state import FileSessionState
from neoson_robot.voice_client import HttpVoiceGateway
from neoson_robot.voice_controller import Feedback, Outcome, VoiceController

TOKEN = "token-ficticio-local"
URL = "http://gateway.test"
RUNTIME = "voice-journey-v1"
TITLE = "titulo-privado-do-compromisso"
START = datetime(2026, 10, 23, 10, tzinfo=UTC)
EVENT: dict[str, object] = {
    "title": TITLE,
    "start": START.isoformat(),
    "end": (START + timedelta(hours=1)).isoformat(),
}
DRAFT: dict[str, object] = {
    "recipient": "person" + chr(64) + "example.invalid",
    "subject": TITLE,
    "body": TITLE,
}
ANALYSIS = {"task": "Extraia data e horário.", "context": "Teste simulado de voz."}


class Journey:
    def __init__(self, tmp_path: Path, provider: LLMProvider) -> None:
        self.db = tmp_path / "neoson.db"
        self.sandbox = SqlitePersonalSandbox(tmp_path / "sandbox.db")
        destination = "sandbox local simulado"
        tools = ToolRegistry(
            [
                CalendarCreate(self.sandbox, destination),
                EmailDraft(self.sandbox, destination),
            ]
        )
        self.engine = AgentLoop(
            provider,
            RulesRouter("balanced"),
            Budget(8, 10),
            tools=tools,
            store=SqliteConversationStore(self.db),
            approval_runtime=RUNTIME,
            delegation_limits=DelegationLimits(),
        )
        policy = PolicyEngine()
        service = ActionService(
            SqliteActionStore(self.db),
            tools,
            policy,
            action_context(RUNTIME, policy, tools),
            5,
        )
        speech = FakeSpeech()
        pipeline = VoicePipeline(self.engine, speech, speech, simulated=True)
        self.server = TestClient(
            create_app(self.engine, TOKEN, actions=service, voice=pipeline)
        )
        self.http = httpx2.Client(
            base_url=URL, transport=httpx2.MockTransport(self._bridge)
        )
        self.state = FileSessionState(tmp_path / "robot-session.json", URL)

    def _bridge(self, request: httpx2.Request) -> httpx2.Response:
        answer = self.server.post(
            request.url.path,
            content=request.content,
            headers=dict(request.headers),
            params=dict(request.url.params),
        )
        return httpx2.Response(
            answer.status_code, content=answer.content, headers=dict(answer.headers)
        )

    def speak(self) -> tuple[Outcome, FakeDisplay, FakePlayback]:
        display, playback = FakeDisplay(), FakePlayback()
        controller = VoiceController(
            FakeCapture(),
            playback,
            display,
            HttpVoiceGateway(self.http, TOKEN),
            self.state,
        )
        return controller.interact(True), display, playback

    def review(
        self, method: str, path: str, token: str = TOKEN, **options: Any
    ) -> httpx2.Response:
        return self.server.request(
            method,
            "/api/v1" + path,
            headers={"Authorization": "Bearer " + token},
            **options,
        )

    def effects(self) -> int:
        if not self.sandbox.path.exists():
            return 0
        with closing(connect(self.sandbox.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM sandbox_effects")
            return int(count.fetchone()[0])

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        with closing(connect(self.db)) as connection:
            return [tuple(row) for row in connection.execute(sql)]


def test_voice_request_needs_approval_then_executes_once_and_session_continues(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            call_tool(DELEGATE, ANALYSIS, "d1"),
            reply("Amanhã, das 10h às 11h.", Usage(40, 8)),
            call_tool("calendar_create", EVENT, "c1"),
            reply("Compromisso registrado no sandbox."),
        ]
    )
    journey = Journey(tmp_path, provider)

    # 1. Voz: o modelo delega uma análise sem ferramentas e propõe a escrita.
    outcome, display, playback = journey.speak()
    assert (outcome.state, outcome.feedback) == (RobotState.IDLE, Feedback.APPROVAL)
    assert display.frames[-1] == (RobotState.IDLE, Feedback.APPROVAL)
    assert playback.calls == 1  # o pedido de aprovação é falado (áudio simulado)
    parent, child, proposal = provider.requests
    assert child.tools == () and "Teste simulado de voz." in child.messages[1].content
    assert parent.messages[0].content not in str(child.messages)
    assert journey.effects() == 0
    session = journey.state.load()
    assert session is not None

    # 2. Autorização: o proprietário encontra a ação sem o run_id do Robot.
    assert journey.review("GET", "/actions", token="outro").status_code == 401
    (listed,) = journey.review("GET", "/actions").json()
    assert (listed["tool"], listed["arguments"], listed["state"]) == (
        "calendar_create",
        EVENT,
        "pending",
    )
    assert "sandbox local simulado" in listed["tool_description"]
    action = "/actions/" + listed["id"]
    assert journey.review("POST", action + "/resume").status_code == 409
    approved = journey.review("POST", action + "/decision", json={"approve": True})
    assert approved.json()["state"] == "approved"
    assert [a["state"] for a in journey.review("GET", "/actions").json()] == [
        "approved"
    ]

    # 3. Ação: um único efeito, mesmo com resume repetido.
    assert journey.review("POST", action + "/resume").json()["state"] == "completed"
    assert journey.review("POST", action + "/resume").json()["state"] == "completed"
    assert journey.effects() == 1
    assert journey.review("GET", "/actions").json() == []

    # 4. Feedback: nova fala na mesma sessão, retomada do arquivo local do Robot.
    outcome, display, playback = journey.speak()
    assert (outcome.state, outcome.feedback) == (RobotState.IDLE, Feedback.SIMULATED)
    assert playback.calls == 1 and journey.state.load() == session
    follow_up = provider.requests[-1]
    assert any("aprovação" in m.content for m in follow_up.messages)
    assert journey.effects() == 1

    # Privacidade: display, estado local e eventos não carregam o conteúdo.
    assert TITLE not in repr(display.frames)
    assert TITLE not in (tmp_path / "robot-session.json").read_text(encoding="utf-8")
    payloads = [
        json.loads(p) for (p,) in journey.rows("SELECT payload FROM outbox_events")
    ]
    assert all(TITLE not in json.dumps(p) for p in payloads)
    runs = [p for p in payloads if "delegation" in p]
    assert runs[0]["delegation"]["completed"] == 1 and runs[0]["usage_complete"]
    assert proposal.messages[-1].content.startswith("Resultado de análise delegada")


def test_denied_voice_action_never_reaches_the_sandbox(tmp_path: Path) -> None:
    provider = ScriptedProvider([call_tool("email_draft", DRAFT, "e1")])
    journey = Journey(tmp_path, provider)
    outcome, _, _ = journey.speak()
    assert outcome.feedback is Feedback.APPROVAL
    (listed,) = journey.review("GET", "/actions").json()
    action = "/actions/" + listed["id"]
    denied = journey.review("POST", action + "/decision", json={"approve": False})
    assert denied.json()["state"] == "denied"
    assert (
        journey.review("POST", action + "/decision", json={"approve": True}).status_code
        == 409
    )
    assert journey.review("POST", action + "/resume").status_code == 409
    assert journey.review("GET", "/actions").json() == []
    assert journey.effects() == 0


def test_closing_voice_stream_during_delegation_records_cost_without_action(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        entered, stopped = asyncio.Event(), asyncio.Event()

        class Provider:
            calls = 0

            async def complete(self, request: LLMRequest) -> LLMResponse:
                self.calls += 1
                if self.calls == 1:
                    return call_tool(DELEGATE, ANALYSIS, usage=Usage(40, 4))
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
                return reply("Nunca.")

        journey = Journey(tmp_path, Provider())
        speech = FakeSpeech()
        pipeline = VoicePipeline(journey.engine, speech, speech, simulated=True)

        async def auth() -> None:
            pass

        endpoint = next(
            route.endpoint
            for route in voice_router(pipeline, auth).routes
            if isinstance(route, APIRoute) and route.path == "/api/v1/voice/stream"
        )

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": silent_wav(), "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/voice/stream",
                "headers": [
                    (b"content-type", b"audio/wav"),
                    (b"x-neoson-activation", b"explicit"),
                ],
            },
            receive,
        )
        response = await endpoint(request, None)
        await entered.wait()
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert journey.rows("SELECT status, input_tokens FROM runs") == [("failed", 40)]
        (payload,) = [
            json.loads(p) for (p,) in journey.rows("SELECT payload FROM outbox_events")
        ]
        assert payload["usage_complete"] is False
        assert payload["delegation"]["failed"] == 1
        assert journey.rows("SELECT COUNT(*) FROM pending_actions") == [(0,)]
        assert journey.effects() == 0

    asyncio.run(scenario())
