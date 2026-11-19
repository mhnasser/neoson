import sys
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx2
import pytest

from neoson_contracts.events.v1.robot import RobotState, VoiceEvent
from neoson_robot import face
from neoson_robot.client import GatewayError
from neoson_robot.fake_devices import FakeCapture, FakePlayback
from neoson_robot.touch_voice import (
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
