import asyncio
import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request

from neoson_contracts.api.v1.voice import VoiceResponse
from neoson_contracts.events.v1.robot import (
    VOICE_EVENT,
    RobotState,
    VoiceCompleted,
    VoiceEvent,
    VoiceProgress,
)
from neoson_core.engine import AgentLoop, Budget, EchoEngine
from neoson_core.engine.voice import VoicePipeline
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.providers.fake_speech import FakeSpeech
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.calendar_fake import FakeCalendar
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.app import create_app
from neoson_gateway.voice import voice_router
from neoson_robot.client import GatewayError
from neoson_robot.fake_devices import FakeCapture, FakeDisplay, FakePlayback, silent_wav
from neoson_robot.session_state import FileSessionState
from neoson_robot.voice_client import HttpVoiceGateway
from neoson_robot.voice_controller import Feedback, VoiceController
from neoson_robot.voice_main import main

AUTH = {
    "Authorization": "Bearer test-only",
    "Content-Type": "audio/wav",
    "X-Neoson-Activation": "explicit",
}
PRIVATE = "resposta-privada-fora-do-display"


def result(**changes: Any) -> VoiceResponse:
    params: dict[str, Any] = {
        "run_id": uuid4(),
        "session_id": uuid4(),
        "text": PRIVATE,
        "status": "completed",
        "audio_base64": base64.b64encode(silent_wav()).decode(),
        "audio_seconds": 0.2,
        "simulated": True,
    }
    params.update(changes)
    return VoiceResponse(**params)


class Gateway:
    def __init__(self, value: VoiceResponse | None = None) -> None:
        self.value = value or result()
        self.sessions: list[object] = []
        self.closed = False
        self.fault: GatewayError | None = None

    def events(
        self, audio: bytes, session_id: object, simulated_capture: bool
    ) -> Iterator[VoiceEvent]:
        self.sessions.append(session_id)
        try:
            if self.fault:
                raise self.fault
            for state in ("transcribing", "thinking", "using_tool", "thinking"):
                yield VoiceProgress(state=state)
            yield VoiceCompleted(result=self.value)
        finally:
            self.closed = True


def controller(
    gateway: Gateway,
) -> tuple[VoiceController, FakeCapture, FakePlayback, FakeDisplay]:
    capture, play, display = FakeCapture(), FakePlayback(), FakeDisplay()
    return VoiceController(capture, play, display, gateway), capture, play, display


def test_no_capture_or_network_without_activation() -> None:
    gateway = Gateway()
    control, capture, play, display = controller(gateway)
    assert control.interact(False).state is RobotState.IDLE
    assert capture.calls == play.calls == 0 and not gateway.sessions
    assert display.frames == [(RobotState.IDLE, Feedback.NONE)]


def test_states_follow_gateway_and_speaking_only_during_playback() -> None:
    gateway = Gateway()
    control, capture, play, display = controller(gateway)
    outcome = control.interact(True)
    assert outcome.state is RobotState.IDLE and outcome.feedback is Feedback.SIMULATED
    assert [state.value for state, _ in display.frames] == [
        "idle",
        "listening",
        "thinking",
        "transcribing",
        "thinking",
        "using_tool",
        "thinking",
        "speaking",
        "idle",
    ]
    assert (
        capture.calls == play.calls == 1
        and capture.stopped
        and play.stopped
        and gateway.closed
    )
    assert PRIVATE not in repr(display.frames) and PRIVATE not in repr(outcome)


@pytest.mark.parametrize(
    "status,feedback,state",
    [
        ("needs_approval", Feedback.APPROVAL, RobotState.IDLE),
        ("failed", Feedback.ERROR, RobotState.ERROR),
    ],
)
def test_feedback_matches_server_result(
    status: str, feedback: Feedback, state: RobotState
) -> None:
    control, _, _, display = controller(Gateway(result(status=status)))
    outcome = control.interact(True)
    assert (outcome.state, outcome.feedback) == (state, feedback)
    assert display.frames[-1] == (state, feedback)


def test_no_speaking_when_audio_is_unavailable() -> None:
    control, _, play, display = controller(
        Gateway(result(audio_status="synthesis_failed", audio_base64=None))
    )
    outcome = control.interact(True)
    assert outcome.feedback is Feedback.NO_AUDIO and play.calls == 0
    assert RobotState.SPEAKING not in [state for state, _ in display.frames]


@pytest.mark.parametrize("when", ["before", "capture", "network", "playback"])
def test_cancellation_stops_devices_and_never_replays(when: str) -> None:
    gateway = Gateway()
    control, capture, play, display = controller(gateway)

    def cancelled() -> bool:
        return (
            when == "before"
            or when == "capture"
            and capture.calls > 0
            or when == "network"
            and bool(gateway.sessions)
            or when == "playback"
            and play.calls > 0
        )

    outcome = control.interact(True, cancelled)
    assert outcome.feedback is Feedback.CANCELLED
    assert capture.stopped and play.stopped
    assert len(gateway.sessions) <= 1
    assert display.frames[-1] == (RobotState.IDLE, Feedback.CANCELLED)


@pytest.mark.parametrize(
    "error,state",
    [
        (GatewayError("offline", code="offline"), RobotState.OFFLINE),
        (GatewayError("protocol"), RobotState.ERROR),
        (GatewayError("server", 503), RobotState.ERROR),
    ],
)
def test_disconnect_and_protocol_errors_are_distinct(
    error: GatewayError, state: RobotState
) -> None:
    gateway = Gateway()
    gateway.fault = error
    control, capture, play, _ = controller(gateway)
    assert control.interact(True).state is state
    assert capture.stopped and play.stopped and play.calls == 0 and gateway.closed


def test_indicator_failure_prevents_capture() -> None:
    class Display(FakeDisplay):
        def show(self, state: RobotState, feedback: Feedback = Feedback.NONE) -> None:
            if state is RobotState.LISTENING:
                raise RuntimeError(PRIVATE)
            super().show(state, feedback)

    capture, play = FakeCapture(), FakePlayback()
    outcome = VoiceController(capture, play, Display(), Gateway()).interact(True)
    assert outcome.state is RobotState.ERROR and capture.calls == play.calls == 0


def test_cleanup_failure_is_reported_without_leaking_private_exception(
    caplog: Any,
) -> None:
    class Playback(FakePlayback):
        def stop(self) -> None:
            raise RuntimeError(PRIVATE)

    outcome = VoiceController(
        FakeCapture(), Playback(), FakeDisplay(), Gateway()
    ).interact(True)
    assert outcome.state is RobotState.ERROR and PRIVATE not in caplog.text


def test_restart_loads_only_session_and_never_replays_audio(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    url = "http://gateway.test"
    first = Gateway()
    control = VoiceController(
        FakeCapture(), FakePlayback(), FakeDisplay(), first, FileSessionState(path, url)
    )
    control.interact(True)
    text = path.read_text(encoding="utf-8")
    assert PRIVATE not in text and "audio" not in text and url not in text
    second = Gateway()
    capture, play = FakeCapture(), FakePlayback()
    restored = VoiceController(
        capture, play, FakeDisplay(), second, FileSessionState(path, url)
    )
    assert restored.session_id == first.value.session_id
    assert capture.calls == play.calls == 0 and not second.sessions
    restored.interact(True)
    assert second.sessions == [first.value.session_id]
    with pytest.raises(ValueError):
        FileSessionState(path, "http://different.test").load()


def test_corrupt_state_file_is_safe(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(PRIVATE, encoding="utf-8")
    with pytest.raises(ValueError, match="Estado local de sessão inválido") as error:
        FileSessionState(path, "http://gateway.test").load()
    assert PRIVATE not in str(error.value)


def stream_client(content: bytes, status: int = 200) -> HttpVoiceGateway:
    return HttpVoiceGateway(
        httpx2.Client(
            base_url="http://gateway.test",
            transport=httpx2.MockTransport(
                lambda r: httpx2.Response(
                    status,
                    content=content,
                    headers={"content-type": "application/x-ndjson"},
                )
            ),
        ),
        "test-only",
    )


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json\n",
        b'{"type":"progress","state":"secret"}\n',
        b'{"type":"progress","state":"thinking"}\n',
        b'{"type":"progress","state":"thinking"}',
    ],
)
def test_client_rejects_invalid_or_interrupted_stream(body: bytes) -> None:
    with pytest.raises(GatewayError):
        list(stream_client(body).events(silent_wav(), None, True))


def test_client_checks_frame_limit_and_duplicate_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = VOICE_EVENT.dump_json(VoiceCompleted(result=result())) + b"\n"
    with pytest.raises(GatewayError):
        list(stream_client(frame + frame).events(silent_wav(), None, True))
    monkeypatch.setattr("neoson_robot.voice_client.MAX_FRAME_BYTES", 16)
    with pytest.raises(GatewayError):
        list(stream_client(b"x" * 17 + b"\n").events(silent_wav(), None, True))


def test_real_gateway_stream_has_only_observed_states_and_no_private_progress() -> None:
    speech = FakeSpeech()
    engine = AgentLoop(
        ScriptedProvider(
            [call_tool("calendar_list", {"day": "2026-10-22"}), reply(PRIVATE)]
        ),
        RulesRouter("balanced"),
        Budget(3, 5),
        tools=ToolRegistry([FakeCalendar.fixed({})]),
    )
    pipeline = VoicePipeline(engine, speech, speech, simulated=True)
    c = TestClient(create_app(engine, "test-only", voice=pipeline), headers=AUTH)
    response = c.post("/api/v1/voice/stream", content=silent_wav())
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    events = [VOICE_EVENT.validate_json(line) for line in response.content.splitlines()]
    progress = [e for e in events if isinstance(e, VoiceProgress)]
    assert [e.state for e in progress] == [
        "transcribing",
        "thinking",
        "using_tool",
        "thinking",
    ]
    assert all(PRIVATE not in e.model_dump_json() for e in progress)
    assert isinstance(events[-1], VoiceCompleted) and events[-1].result.text == PRIVATE


def test_stream_invalid_input_keeps_http_error_and_simulated_capture_guard() -> None:
    speech = FakeSpeech()
    c = TestClient(
        create_app(
            EchoEngine(), "test-only", voice=VoicePipeline(EchoEngine(), speech, speech)
        ),
        headers=AUTH,
    )
    assert c.post("/api/v1/voice/stream", content=b"not wav").status_code == 422
    assert (
        c.post(
            "/api/v1/voice/stream",
            content=silent_wav(),
            headers={"X-Neoson-Simulated": "true"},
        ).status_code
        == 400
    )


def test_cli_never_activates_without_explicit_flag(tmp_path: Path, capsys: Any) -> None:
    http = httpx2.Client(
        base_url="http://gateway.test",
        transport=httpx2.MockTransport(lambda r: pytest.fail("request not authorized")),
    )
    assert main(["--simulate", "--state-file", str(tmp_path / "state.json")], http) == 0
    output = capsys.readouterr().out
    assert "idle" in output and PRIVATE not in output
    assert not (tmp_path / "state.json").exists()


def test_http_client_controller_and_cli_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    speech = FakeSpeech()
    engine = EchoEngine()
    server = TestClient(
        create_app(
            engine,
            "test-only",
            voice=VoicePipeline(engine, speech, speech, simulated=True),
        )
    )
    requests: list[str] = []

    def bridge(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        reply = server.post(
            str(request.url.path),
            content=request.content,
            headers=dict(request.headers),
            params=dict(request.url.params),
        )
        return httpx2.Response(
            reply.status_code, content=reply.content, headers=dict(reply.headers)
        )

    http = httpx2.Client(
        base_url="http://gateway.test", transport=httpx2.MockTransport(bridge)
    )
    display, play = FakeDisplay(), FakePlayback()
    control = VoiceController(
        FakeCapture(), play, display, HttpVoiceGateway(http, "test-only")
    )
    assert control.interact(True).feedback is Feedback.SIMULATED
    assert play.calls == 1 and RobotState.USING_TOOL not in [
        s for s, _ in display.frames
    ]
    monkeypatch.setenv("NEOSON_API_TOKEN", "test-only")
    assert (
        main(
            ["--simulate", "--activate", "--state-file", str(tmp_path / "cli.json")],
            http,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert (
        "simulated" in output
        and "Teste simulado" not in output
        and PRIVATE not in output
    )
    assert requests == ["/api/v1/voice/stream"] * 2


def test_stream_auth_and_late_provider_failure_do_not_leak() -> None:
    class Broken(FakeSpeech):
        async def transcribe(self, audio: Any) -> str:
            raise RuntimeError(PRIVATE)

    speech = Broken()
    pipeline = VoicePipeline(EchoEngine(), speech, speech, simulated=True)
    server = TestClient(create_app(EchoEngine(), "test-only", voice=pipeline))
    assert server.post("/api/v1/voice/stream", content=silent_wav()).status_code == 401
    response = server.post("/api/v1/voice/stream", content=silent_wav(), headers=AUTH)
    assert response.status_code == 200 and PRIVATE not in response.text
    events = [VOICE_EVENT.validate_json(line) for line in response.content.splitlines()]
    assert events[0].type == "progress" and events[-1].type == "error"


def test_closing_stream_cancels_server_and_releases_admission() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        stopped = asyncio.Event()

        class Waiting(FakeSpeech):
            async def transcribe(self, audio: Any) -> str:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
                return "unreachable"

        speech = Waiting()
        pipeline = VoicePipeline(EchoEngine(), speech, speech, simulated=True)

        async def auth() -> None:
            pass

        router = voice_router(pipeline, auth)
        endpoint = next(
            r.endpoint
            for r in router.routes
            if isinstance(r, APIRoute) and r.path == "/api/v1/voice/stream"
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
        pipeline.transcriber = FakeSpeech()

        async def load() -> bytes:
            return silent_wav()

        assert (await pipeline.process(load, None, "owner")).audio is not None

    asyncio.run(scenario())


def test_atomic_session_save_failure_preserves_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.json"
    state = FileSessionState(path, "http://gateway.test")
    first = uuid4()
    state.save(first)

    def fail(*args: Any) -> None:
        raise OSError(PRIVATE)

    with monkeypatch.context() as patch:
        patch.setattr("neoson_robot.session_state.os.replace", fail)
        with pytest.raises(OSError):
            state.save(uuid4())
    assert state.load() == first
    assert list(tmp_path.iterdir()) == [path]
