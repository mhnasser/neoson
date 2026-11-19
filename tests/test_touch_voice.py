import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx2
import pytest

from neoson_contracts.api.v1.actions import ActionResponse
from neoson_contracts.events.v1.robot import RobotState, VoiceEvent
from neoson_robot import face
from neoson_robot.client import GatewayError
from neoson_robot.fake_devices import FakeCapture, FakePlayback
from neoson_robot.touch_voice import (
    FETCH_FAILED,
    TOO_LONG,
    ApprovalPanel,
    ContinuousRobot,
    StatusDisplay,
    TapToTalk,
    gateway_ready,
)
from neoson_robot.voice_controller import Feedback, VoiceController


class OfflineGateway:
    calls = 0

    def events(self, *args: Any) -> Iterator[VoiceEvent]:
        self.calls += 1
        raise GatewayError("Erro de teste", code="offline")
        yield


def runtime(
    ready: Callable[[], bool],
) -> tuple[ContinuousRobot, FakeCapture, OfflineGateway, list[Callable[[], None]]]:
    capture, play, display, gateway = (
        FakeCapture(),
        FakePlayback(),
        StatusDisplay(),
        OfflineGateway(),
    )
    control = VoiceController(capture, play, display, gateway)
    jobs: list[Callable[[], None]] = []
    return (
        ContinuousRobot(control, display, ready, start=jobs.append),
        capture,
        gateway,
        jobs,
    )


def test_backoff_is_bounded_and_recovery_does_not_send_voice() -> None:
    connected = [False]
    robot, capture, gateway, jobs = runtime(lambda: connected[0])
    assert [robot.check() for _ in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert not robot.activate()
    connected[0] = True
    assert robot.check() == 10
    assert robot.display.text() == "Toque para falar"
    assert capture.calls == gateway.calls == len(jobs) == 0
    connected[0] = False
    assert robot.check() == 1


def test_one_turn_at_a_time_no_queue_and_no_retry_after_connection_loss() -> None:
    robot, capture, gateway, jobs = runtime(lambda: True)
    robot.check()
    touch = TapToTalk(robot)
    touch.action("feliz", 0)
    touch.status(0.4)
    assert not jobs
    touch.status(0.6)
    assert len(jobs) == 1
    touch.action("feliz", 0.7)
    assert touch.pending is None
    assert not robot.activate()
    jobs.pop()()
    assert capture.calls == gateway.calls == 1
    assert not robot.online.is_set()
    robot.check()
    touch.status(10)
    assert not jobs and gateway.calls == 1
    assert robot.activate()


def test_camera_gesture_and_offline_taps_do_not_activate_voice() -> None:
    robot, capture, gateway, jobs = runtime(lambda: True)
    touch = TapToTalk(robot)
    touch.action("feliz", 0)
    robot.check()
    touch.status(1)
    assert not jobs
    touch.action("feliz", 2)
    touch.action("feliz", 2.2)
    touch.action("camera", 2.4)
    touch.status(3)
    touch.action("rosto", 4)
    touch.status(5)
    assert not jobs and capture.calls == gateway.calls == 0


@pytest.mark.parametrize("state", list(RobotState))
def test_display_only_contains_fixed_states_and_feedback(state: RobotState) -> None:
    display = StatusDisplay()
    display.show(state)
    assert display.text()
    display.show(state, Feedback.APPROVAL)
    assert "aprovação" in display.text()


def test_stop_cancels_pending_turn_without_capture() -> None:
    robot, capture, gateway, jobs = runtime(lambda: True)
    robot.check()
    robot.activate()
    robot.stop.set()
    jobs.pop()()
    assert capture.calls == gateway.calls == 0
    assert not robot.activate()


def test_tk_adapter_delivers_taps_and_draws_status_on_a_fake_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, canvas = Mock(), Mock()
    canvas.winfo_width.return_value = 800
    canvas.winfo_height.return_value = 480
    canvas.create_text.return_value = 99
    callbacks: list[Callable[[], None]] = []
    root.after.side_effect = lambda delay, work: callbacks.append(work)
    action, status = Mock(), Mock(return_value="Ouvindo")

    def loop() -> None:
        bindings = {call.args[0]: call.args[1] for call in canvas.bind.call_args_list}
        bindings["<ButtonPress-1>"](None)
        bindings["<ButtonRelease-1>"](None)
        callbacks.pop(0)()

    root.mainloop.side_effect = loop
    monkeypatch.setitem(
        sys.modules,
        "tkinter",
        SimpleNamespace(
            Tk=lambda: root,
            Canvas=lambda *args, **kwargs: canvas,
            TclError=RuntimeError,
        ),
    )
    monkeypatch.setattr("neoson_robot.face.signal.signal", lambda *args: None)
    face.run_window(face.Animator(), 30, False, 0, on_action=action, status=status)
    assert action.call_args.args[0] == "feliz"
    canvas.itemconfigure.assert_called_once_with(99, text="Ouvindo")


@pytest.mark.parametrize(
    "content, status_code, expected",
    [
        (b'{"status":"ready"}', 200, True),
        (b'{"status":"ready"}', 503, False),
        (b"[]", 200, False),
        (b" " * 5000 + b'{"status":"ready"}', 200, False),
    ],
)
def test_health_response_is_bounded_and_does_not_send_audio(
    content: bytes,
    status_code: int,
    expected: bool,
) -> None:
    def reply(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/health/ready"
        assert not request.content
        return httpx2.Response(status_code, content=content)

    with httpx2.Client(
        base_url="http://gateway.test", transport=httpx2.MockTransport(reply)
    ) as http:
        assert gateway_ready(http) == expected


def pending_action(**changes: object) -> ActionResponse:
    data: dict[str, object] = {
        "id": uuid4(),
        "run_id": uuid4(),
        "session_id": uuid4(),
        "tool": "calendar_create",
        "arguments": {"title": "Dentista", "start": "2026-11-20T10:00:00+00:00"},
        "fingerprint": "f" * 64,
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        "state": "pending",
        "tool_description": "Propõe compromisso sem convidados. Destino: sandbox",
    }
    data.update(changes)
    return ActionResponse.model_validate(data)


class FakeApprovals:
    def __init__(self, *actions: ActionResponse, failing: bool = False) -> None:
        self.actions = list(actions)
        self.failing = failing
        self.calls: list[object] = []

    def open(self) -> list[ActionResponse]:
        self.calls.append("open")
        if self.failing:
            raise GatewayError("Não foi possível falar com o gateway.", code="offline")
        return list(self.actions)

    def decide(self, action_id: UUID, approve: bool) -> ActionResponse:
        self.calls.append(("decide", approve))
        (action,) = [a for a in self.actions if a.id == action_id]
        self.actions.remove(action)
        state = "approved" if approve else "denied"
        return action.model_copy(update={"state": state})

    def resume(self, action_id: UUID) -> ActionResponse:
        self.calls.append("resume")
        return pending_action(id=action_id, state="completed")


def panel(
    client: FakeApprovals,
) -> tuple[ApprovalPanel, list[float], list[Callable[[], None]]]:
    clock = [0.0]
    jobs: list[Callable[[], None]] = []
    return ApprovalPanel(client, start=jobs.append, clock=lambda: clock[0]), clock, jobs


def test_panel_shows_the_request_and_an_approval_runs_once() -> None:
    client = FakeApprovals(pending_action())
    screen, clock, jobs = panel(client)
    assert screen.view() is None and not screen.active()
    screen.refresh()
    shown = screen.view()
    assert shown is not None and shown[1]
    for part in ("calendar_create", "Destino: sandbox", "title: Dentista", "Vale até"):
        assert part in shown[0]
    screen.choose(True)  # logo que aparece: toque acidental não decide
    assert not jobs
    clock[0] = 1.5
    screen.choose(True)
    screen.choose(True)
    screen.choose(False)
    assert len(jobs) == 1
    assert screen.view() == ("Enviando sua decisão…", False)
    jobs.pop()()
    assert client.calls == ["open", ("decide", True), "resume", "open"]
    assert screen.view() == ("Pronto, feito.", False)
    screen.choose(True)
    assert not jobs
    clock[0] = 10
    assert screen.view() is None


def test_deny_never_resumes_and_the_next_request_is_shown() -> None:
    first, second = pending_action(), pending_action(tool="email_draft")
    client = FakeApprovals(first, second)
    screen, clock, jobs = panel(client)
    screen.refresh()
    clock[0] = 2
    screen.choose(False)
    jobs.pop()()
    assert "resume" not in client.calls and ("decide", False) in client.calls
    shown = screen.view()
    assert shown is not None and shown[1] and "email_draft" in shown[0]
    screen.choose(True)  # a próxima também espera o primeiro segundo
    assert not jobs


def test_long_expired_or_unreachable_requests_get_no_buttons() -> None:
    long = FakeApprovals(pending_action(arguments={"body": "x" * 2000}))
    screen, clock, jobs = panel(long)
    screen.refresh()
    clock[0] = 2
    assert screen.view() == (TOO_LONG, False)
    screen.choose(True)
    assert not jobs and long.calls == ["open"]

    gone = pending_action(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    screen, _, _ = panel(FakeApprovals(gone))
    screen.refresh()
    assert screen.view() is None

    screen, clock, _ = panel(FakeApprovals(failing=True))
    screen.refresh()
    assert screen.view() == (FETCH_FAILED, False)
    clock[0] = 10
    assert screen.view() is None


def test_gateway_error_on_decision_becomes_a_note() -> None:
    class Refusing(FakeApprovals):
        def resume(self, action_id: UUID) -> ActionResponse:
            raise GatewayError("Ação incompatível com as permissões atuais.")

    screen, clock, jobs = panel(Refusing(pending_action()))
    screen.refresh()
    clock[0] = 2
    screen.choose(True)
    jobs.pop()()
    shown = screen.view()
    assert shown is not None and "Ação incompatível" in shown[0] and not shown[1]


def test_voice_is_blocked_while_the_panel_is_on_screen() -> None:
    robot, capture, gateway, jobs = runtime(lambda: True)
    screen, clock, _ = panel(FakeApprovals(pending_action()))
    robot.approvals = screen
    robot.check()
    screen.refresh()
    assert not robot.available() and not robot.activate()
    touch = TapToTalk(robot)
    touch.action("feliz", 0)
    assert touch.pending is None
    screen._action = None
    assert robot.activate() and len(jobs) == 1


@pytest.mark.parametrize(
    "x, y, expected",
    [(600, 420, True), (200, 420, False), (400, 420, None), (600, 100, None)],
)
def test_buttons_are_large_and_separated(x: int, y: int, expected: bool | None) -> None:
    assert face.choice_at(x, y, 800, 480) is expected


def test_tk_panel_takes_the_positioned_tap_and_blocks_gestures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, canvas = Mock(), Mock()
    canvas.winfo_width.return_value = 800
    canvas.winfo_height.return_value = 480
    callbacks: list[Callable[[], None]] = []
    root.after.side_effect = lambda delay, work: callbacks.append(work)
    action, choice = Mock(), Mock()
    shown: list[tuple[str, bool] | None] = [("Pedido", True)]

    class SyncThread:
        def __init__(self, target: Callable[..., None], args: tuple[Any, ...]) -> None:
            self.target, self.args, self.daemon = target, args, False

        def start(self) -> None:
            self.target(*self.args)

    def taps(path: Path, sink: Callable[[float, bool], None]) -> None:
        sink(0.0, True)
        sink(0.1, False)

    def loop() -> None:
        bindings = {call.args[0]: call.args[1] for call in canvas.bind.call_args_list}
        callbacks.pop(0)()  # toque do evdev com o painel: não vira gesto
        bindings["<ButtonPress-1>"](None)
        bindings["<ButtonRelease-1>"](SimpleNamespace(x=600, y=420))
        bindings["<ButtonRelease-1>"](SimpleNamespace(x=400, y=100))

    root.mainloop.side_effect = loop
    monkeypatch.setitem(
        sys.modules,
        "tkinter",
        SimpleNamespace(
            Tk=lambda: root,
            Canvas=lambda *args, **kwargs: canvas,
            TclError=RuntimeError,
        ),
    )
    monkeypatch.setattr("neoson_robot.face.signal.signal", lambda *args: None)
    monkeypatch.setattr("neoson_robot.face.read_touches", taps)
    monkeypatch.setattr("neoson_robot.face.threading.Thread", SyncThread)
    face.run_window(
        face.Animator(),
        30,
        False,
        0,
        touch=tmp_path / "event0",
        on_action=action,
        prompt=lambda: shown[0],
        on_choice=choice,
    )
    action.assert_not_called()
    choice.assert_called_once_with(True)
    hidden = [c for c in canvas.itemconfigure.call_args_list if "state" in c.kwargs]
    assert {c.kwargs["state"] for c in hidden} == {"normal"}
