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
    describe,
    gateway_ready,
)
from neoson_robot.voice_controller import Feedback, Outcome, VoiceController
from neoson_robot.wake import (
    FOLLOW_UP_FRAMES,
    NO_SPEECH_FRAMES,
    SpokenRequest,
    WakeListener,
)


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
    assert robot.display.text() == ""  # em repouso o peito volta ao uso
    assert capture.calls == gateway.calls == len(jobs) == 0
    connected[0] = False
    assert robot.check() == 1


class Mic:
    def __init__(self, frames: int) -> None:
        self.frames, self.cleared = frames, 0

    def frame(self, timeout: float = 1.0) -> bytes | None:
        if not self.frames:
            return None
        self.frames -= 1
        return b"\0" * 2560

    def clear(self) -> None:
        self.cleared += 1


class Detector:
    def __init__(self) -> None:
        self.resets = 0

    def hit(self, frame: bytes) -> bool:
        return True  # toda escuta "ouviu" a wake word

    def reset(self) -> None:
        self.resets += 1


def listener(robot: ContinuousRobot) -> tuple[WakeListener, Mic, Detector]:
    mic, detector = Mic(10), Detector()
    return WakeListener(mic, detector, SpokenRequest(mic), robot), mic, detector


def test_one_turn_at_a_time_no_queue_and_no_retry_after_connection_loss() -> None:
    robot, capture, gateway, jobs = runtime(lambda: True)
    robot.check()
    wake, mic, detector = listener(robot)
    assert wake.step()
    assert len(jobs) == 1
    assert not wake.step()  # turno em andamento: a wake word não enfileira outro
    assert len(jobs) == 1
    jobs.pop()()
    assert capture.calls == gateway.calls == 1
    assert not robot.online.is_set()
    assert not wake.step()  # offline: ouvir a frase não grava nem reenvia
    assert not jobs and gateway.calls == 1
    robot.check()
    assert wake.step() and len(jobs) == 1
    # Depois de cada turno o que o microfone ouviu (inclusive a resposta) é jogado fora.
    assert mic.cleared == detector.resets == 2


class Turns:
    """Controlador falso: cada turno devolve o próximo resultado do roteiro."""

    def __init__(self, request: SpokenRequest, *feedback: Feedback) -> None:
        self.request, self.feedback = request, list(feedback)
        self.patience: list[int] = []

    def interact(self, activated: bool, cancelled: Any = None) -> Outcome:
        self.patience.append(self.request.patience)
        return Outcome(RobotState.IDLE, self.feedback.pop(0))


def test_after_an_answer_it_keeps_listening_without_the_wake_word() -> None:
    mic, detector = Mic(10), Detector()
    request = SpokenRequest(mic)
    turns = Turns(request, Feedback.NONE, Feedback.NONE, Feedback.CANCELLED)
    robot = ContinuousRobot(turns, StatusDisplay(), lambda: True, start=lambda w: w())  # type: ignore[arg-type]
    robot.check()
    wake = WakeListener(mic, detector, request, robot)

    assert wake.step()

    # 1 pedido pela wake word, 2 pela janela de conversa; o 3º não ouviu ninguém.
    assert turns.patience == [NO_SPEECH_FRAMES, FOLLOW_UP_FRAMES, FOLLOW_UP_FRAMES]
    assert request.patience == NO_SPEECH_FRAMES and not robot.answered
    assert mic.cleared == detector.resets == 3  # a própria resposta nunca é ouvida

    # Falha ou pedido de aprovação não abrem a janela.
    for feedback in (Feedback.ERROR, Feedback.APPROVAL):
        turns.feedback = [feedback]
        assert wake.step() and not turns.feedback and not robot.answered


def test_offline_or_stopped_robot_never_wakes() -> None:
    robot, capture, gateway, jobs = runtime(lambda: False)
    wake, mic, detector = listener(robot)
    robot.check()
    assert not wake.step()
    robot.stop.set()
    wake.run()  # sai na hora, sem ler o microfone
    assert mic.frames == 9
    assert not jobs and capture.calls == gateway.calls == 0
    assert detector.resets == 0  # o detector não é zerado só por estar ocupado


@pytest.mark.parametrize("state", list(RobotState))
@pytest.mark.parametrize("feedback", list(Feedback))
def test_chest_gets_short_fixed_ascii_states_with_expiry(
    state: RobotState, feedback: Feedback, tmp_path: Path
) -> None:
    from neoson_robot.chest_display import WIDTH, encode, read_status

    path = tmp_path / "status"
    display = StatusDisplay(path, clock=lambda: 1000.0)
    display.show(state, feedback)
    text = display.text()
    assert text.isascii() and len(text) <= WIDTH and b"?" not in encode(text)
    if feedback is Feedback.APPROVAL:
        assert text == "Aprove na tela"
    shown = read_status(path, 1000.5)
    assert shown == (text or None)  # repouso: nada, o peito mostra o uso
    assert read_status(path, 1000.0 + 121) is None  # nenhum estado fica preso


def test_face_follows_each_state_for_as_long_as_the_chest_shows_it() -> None:
    calls: list[tuple[str, float]] = []
    display = StatusDisplay(face=lambda state, seconds: calls.append((state, seconds)))

    display.show(RobotState.LISTENING)
    display.show(RobotState.ERROR, Feedback.ERROR)
    display.show(RobotState.IDLE, Feedback.APPROVAL)  # aviso do peito, não do rosto

    assert calls == [("listening", 120.0), ("error", 5.0), ("idle", 0.0)]


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
        for x in (200, 440, 200, 440):  # esfregar com o mouse, em janela
            bindings["<B1-Motion>"](SimpleNamespace(x=x, y=240))
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
    assert action.call_args.args[0] == "carinho"
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


RUN = uuid4()


def pending_action(**changes: object) -> ActionResponse:
    data: dict[str, object] = {
        "id": uuid4(),
        "run_id": RUN,
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

    def for_run(self, run_id: UUID) -> list[ActionResponse]:
        self.calls.append("for_run")
        if self.failing:
            raise GatewayError("Não foi possível falar com o gateway.", code="offline")
        return [a for a in self.actions if a.run_id == run_id]

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
    screen.refresh(RUN)
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
    assert client.calls == ["for_run", ("decide", True), "resume", "for_run"]
    assert screen.view() == ("Pronto, feito.", False)
    screen.choose(True)
    assert not jobs
    clock[0] = 10
    assert screen.view() is None


def test_deny_never_resumes_and_the_next_request_is_shown() -> None:
    first, second = pending_action(), pending_action(tool="email_draft")
    client = FakeApprovals(first, second)
    screen, clock, jobs = panel(client)
    screen.refresh(RUN)
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
    screen.refresh(RUN)
    clock[0] = 2
    assert screen.view() == (TOO_LONG, False)
    screen.choose(True)
    assert not jobs and long.calls == ["for_run"]

    gone = pending_action(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    screen, _, _ = panel(FakeApprovals(gone))
    screen.refresh(RUN)
    assert screen.view() is None

    screen, clock, _ = panel(FakeApprovals(failing=True))
    screen.refresh(RUN)
    assert screen.view() == (FETCH_FAILED, False)
    clock[0] = 10
    assert screen.view() is None


def test_only_the_spoken_request_is_shown() -> None:
    other = pending_action(run_id=uuid4(), tool="email_draft")
    screen, clock, jobs = panel(FakeApprovals(other))
    screen.refresh(RUN)
    assert screen.view() is None
    clock[0] = 2
    screen.choose(True)
    assert not jobs


def test_hidden_characters_are_escaped_and_wide_text_counts_double() -> None:
    tricky = pending_action(
        arguments={"title": "a\rb\u2028c\td\u202ee\u00a0f", "body": "linha 1\nlinha 2"}
    )
    text, buttons = describe(tricky)
    assert buttons
    assert "a\\u000db\\u2028c\\u0009d\\u202ee\\u00a0f" in text
    assert "body: linha 1\nlinha 2" in text
    assert describe(pending_action(arguments={"body": "a" * 300}))[1]
    assert describe(pending_action(arguments={"body": "\u754c" * 300})) == (
        TOO_LONG,
        False,
    )


def test_gateway_error_on_decision_becomes_a_note() -> None:
    class Refusing(FakeApprovals):
        def resume(self, action_id: UUID) -> ActionResponse:
            raise GatewayError("Ação incompatível com as permissões atuais.")

    screen, clock, jobs = panel(Refusing(pending_action()))
    screen.refresh(RUN)
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
    screen.refresh(RUN)
    assert not robot.available() and not robot.activate()
    wake, _, _ = listener(robot)
    assert not wake.step() and not jobs
    screen._action = None
    assert robot.activate() and len(jobs) == 1


@pytest.mark.parametrize(
    "x, y, expected",
    [(600, 420, True), (200, 420, False), (400, 420, None), (600, 100, None)],
)
def test_buttons_are_large_and_separated(x: int, y: int, expected: bool | None) -> None:
    assert face.choice_at(x, y, 800, 480) is expected


@pytest.mark.parametrize("bottom, fits", [(200, True), (400, False)])
def test_tk_panel_takes_the_positioned_tap_and_blocks_gestures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bottom: int, fits: bool
) -> None:
    root, canvas = Mock(), Mock()
    canvas.winfo_width.return_value = 800
    canvas.winfo_height.return_value = 480
    canvas.bbox.return_value = (24, 24, 776, bottom)  # texto desenhado pelo Tk
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
    states = [c.kwargs["state"] for c in canvas.itemconfigure.call_args_list[-4:]]
    if fits:
        choice.assert_called_once_with(True)
        assert states == ["normal"] * 4
    else:  # texto passou dos botões: sem botões e sem decisão
        choice.assert_not_called()
        assert states == ["hidden"] * 4
        canvas.itemconfigure.assert_any_call(
            canvas.create_text.return_value, text=face.TOO_TALL
        )
