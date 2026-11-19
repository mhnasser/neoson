"""Rosto com toque para falar; somente health é repetido, nunca um pedido de voz."""

import argparse
import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx2

from neoson_contracts.events.v1.robot import RobotState
from neoson_robot.config import api_token, gateway_url
from neoson_robot.face import (
    MULTI_TAP,
    Animator,
    CameraView,
    find_touchscreen,
    run_window,
)
from neoson_robot.pipewire import PipeWireCapture, PipeWirePlayback
from neoson_robot.session_state import FileSessionState
from neoson_robot.voice_client import HttpVoiceGateway
from neoson_robot.voice_controller import Feedback, VoiceController

LABELS = {
    RobotState.IDLE: "Toque para falar",
    RobotState.LISTENING: "Ouvindo",
    RobotState.TRANSCRIBING: "Transcrevendo",
    RobotState.THINKING: "Pensando",
    RobotState.USING_TOOL: "Consultando ferramenta",
    RobotState.SPEAKING: "Falando",
    RobotState.ERROR: "Não consegui concluir. Toque para outro pedido",
    RobotState.OFFLINE: "Sem conexão com o servidor",
}


class StatusDisplay:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state, self.feedback = RobotState.OFFLINE, Feedback.NONE

    def show(self, state: RobotState, feedback: Feedback = Feedback.NONE) -> None:
        with self._lock:
            self.state, self.feedback = state, feedback

    def text(self) -> str:
        with self._lock:
            if self.feedback == Feedback.APPROVAL:
                return "Aguardando aprovação no canal autenticado"
            if self.feedback == Feedback.NO_AUDIO:
                return "Resposta sem áudio. Toque para outro pedido"
            return LABELS[self.state]


def background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def gateway_ready(http: httpx2.Client) -> bool:
    with http.stream("GET", "/api/v1/health/ready", timeout=3) as response:
        if response.status_code != 200:
            return False
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=4096):
            if len(body) + len(chunk) > 4096:
                return False
            body.extend(chunk)
        value = json.loads(body)
        return isinstance(value, dict) and value.get("status") == "ready"


class ContinuousRobot:
    def __init__(
        self,
        controller: VoiceController,
        display: StatusDisplay,
        ready: Callable[[], bool],
        *,
        start: Callable[[Callable[[], None]], None] = background,
    ) -> None:
        self.controller, self.display, self.ready, self.start = (
            controller,
            display,
            ready,
            start,
        )
        self.stop = threading.Event()
        self.online = threading.Event()
        self._work = threading.Lock()
        self._delay = 1.0
        self.display.show(RobotState.OFFLINE)

    def check(self) -> float:
        if not self._work.acquire(blocking=False):
            return 1.0
        try:
            try:
                ok = self.ready()
            except Exception:
                ok = False
            if ok:
                was_online = self.online.is_set()
                self.online.set()
                self._delay = 1.0
                if not was_online:
                    self.display.show(RobotState.IDLE)
                return 10.0
            self.online.clear()
            self.display.show(RobotState.OFFLINE)
            delay, self._delay = self._delay, min(30.0, self._delay * 2)
            return delay
        finally:
            self._work.release()

    def monitor(self) -> None:
        while not self.stop.is_set():
            self.stop.wait(self.check())

    def available(self) -> bool:
        return (
            self.online.is_set() and not self.stop.is_set() and not self._work.locked()
        )

    def activate(self) -> bool:
        if (
            self.stop.is_set()
            or not self.online.is_set()
            or not self._work.acquire(blocking=False)
        ):
            return False

        def turn() -> None:
            try:
                outcome = self.controller.interact(True, self.stop.is_set)
                if outcome.state == RobotState.OFFLINE:
                    self.online.clear()
            finally:
                self._work.release()

        try:
            self.start(turn)
        except Exception:
            self._work.release()
            raise
        return True


class TapToTalk:
    """Espera o fim dos toques rápidos para não confundir câmera com fala."""

    def __init__(self, runtime: ContinuousRobot) -> None:
        self.runtime = runtime
        self.pending: float | None = None

    def action(self, action: str, at: float) -> None:
        self.pending = at if action == "feliz" and self.runtime.available() else None

    def status(self, at: float) -> str:
        if self.pending is not None and at - self.pending > MULTI_TAP:
            self.pending = None
            self.runtime.activate()
        return self.runtime.display.text()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url")
    parser.add_argument(
        "--seconds", type=float, default=7, help="duração da captura, até 15 s"
    )
    parser.add_argument(
        "--state-file", type=Path, default=Path("data/robot-session.json")
    )
    parser.add_argument("--windowed", action="store_true")
    args = parser.parse_args(argv)
    runtime = None
    try:
        url = gateway_url(args.gateway_url)
        capture = PipeWireCapture(args.seconds)
        display = StatusDisplay()
        with httpx2.Client(base_url=url, timeout=35, follow_redirects=False) as http:
            controller = VoiceController(
                capture,
                PipeWirePlayback(),
                display,
                HttpVoiceGateway(http, api_token()),
                FileSessionState(args.state_file, url),
            )

            def ready() -> bool:
                return gateway_ready(http)

            runtime = ContinuousRobot(controller, display, ready)
            gestures = TapToTalk(runtime)
            background(runtime.monitor)
            touch = None if args.windowed else find_touchscreen()
            try:
                run_window(
                    Animator(),
                    30,
                    not args.windowed,
                    0,
                    touch=touch,
                    camera=CameraView() if touch else None,
                    on_action=gestures.action,
                    status=gestures.status,
                )
            finally:
                runtime.stop.set()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(
            "Não foi possível iniciar a interface de voz. "
            "Confira tela, áudio e configuração."
        )
        return 1
    finally:
        if runtime is not None:
            runtime.stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
