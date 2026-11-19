"""Rosto com toque para falar; somente health é repetido, nunca um pedido de voz."""

import argparse
import json
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx2

from neoson_contracts.api.v1.actions import ActionResponse
from neoson_contracts.events.v1.robot import RobotState
from neoson_robot.client import ApprovalClient, GatewayError
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


# Linhas que cabem acima dos botões na tela de 7" (fonte 16); maior que isso não
# ganha botões: a tela só aprova o que mostra inteiro.
PANEL_ROWS, PANEL_COLUMNS = 11, 60
TAP_GUARD_SECONDS = 1.0  # toque logo que o painel aparece não decide nada
NOTE_SECONDS = 5.0
TOO_LONG = (
    "O pedido é longo demais para revisar nesta tela. "
    "Aprove ou negue pelo canal autenticado."
)
FETCH_FAILED = "Não consegui buscar o pedido de aprovação."
UNSURE = "Não sei se concluiu. Confira no destino antes de pedir de novo."
RESULTS = {
    "completed": "Pronto, feito.",
    "denied": "Negado. Nada foi feito.",
    "unknown": UNSURE,
    "executing": UNSURE,
}
NOT_DONE = "Não executado: o pedido venceu ou não vale mais."


class Approvals(Protocol):
    def for_run(self, run_id: UUID) -> list[ActionResponse]: ...
    def decide(self, action_id: UUID, approve: bool) -> ActionResponse: ...
    def resume(self, action_id: UUID) -> ActionResponse: ...


def visible(text: str) -> str:
    """Escapa controles, formatação e separadores Unicode; só `\\n` quebra a linha."""
    return "".join(
        ch
        if ch == "\n" or unicodedata.category(ch)[0] not in "CZ" or ch == " "
        else f"\\u{ord(ch):04x}"
        for ch in text
    )


def columns(line: str) -> int:
    # Largos (CJK, emoji) ocupam duas colunas; na dúvida, estimar a mais é seguro.
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line)


def describe(action: ActionResponse) -> tuple[str, bool]:
    """Texto do painel e se ele cabe inteiro na tela (só então há botões)."""
    lines = [
        "O NeoSon pede sua aprovação para:",
        f"{action.tool}: {action.tool_description or 'sem descrição disponível'}",
        f"Vale até {action.expires_at.astimezone():%H:%M}.",
        "",
    ]
    for key, value in action.arguments.items():
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {value}")
    text = visible("\n".join(lines))
    rows = sum(max(1, -(-columns(line) // PANEL_COLUMNS)) for line in text.split("\n"))
    return (text, True) if rows <= PANEL_ROWS else (TOO_LONG, False)


class ApprovalPanel:
    """Ação pendente na tela; decide e retoma pela API, uma vez por toque válido."""

    def __init__(
        self,
        client: Approvals,
        *,
        start: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client, self.clock = client, clock
        self.start = start or background
        self._lock = threading.Lock()
        self._run_id: UUID | None = None
        self._action: ActionResponse | None = None
        self._shown_at = 0.0
        self._busy = False
        self._note: tuple[str, float] | None = None

    def _pending(self, run_id: UUID | None) -> ActionResponse | None:
        # Só o pedido que acabou de ser falado; aprovadas sem execução ficam para o
        # canal autenticado.
        if run_id is None:
            return None
        actions = self.client.for_run(run_id)
        return next((a for a in actions if a.state == "pending"), None)

    def refresh(self, run_id: UUID) -> None:
        """Busca a ação pendente do pedido; roda na thread do turno, não na da tela."""
        with self._lock:
            self._run_id = run_id
        try:
            action, note = self._pending(run_id), None
        except GatewayError:
            action, note = None, (FETCH_FAILED, self.clock() + NOTE_SECONDS)
        with self._lock:
            self._action, self._shown_at, self._note = action, self.clock(), note

    def view(self) -> tuple[str, bool] | None:
        with self._lock:
            if self._busy:
                return "Enviando sua decisão…", False
            action = self._action
            if action is not None and action.expires_at <= datetime.now(UTC):
                action = self._action = None
            if action is not None:
                return describe(action)
            if self._note is not None and self.clock() < self._note[1]:
                return self._note[0], False
            return None

    def active(self) -> bool:
        return self.view() is not None

    def choose(self, approve: bool) -> None:
        with self._lock:
            action, run_id = self._action, self._run_id
            if (
                action is None
                or self._busy
                or self.clock() - self._shown_at < TAP_GUARD_SECONDS
                or not describe(action)[1]
            ):
                return
            self._busy = True
        try:
            self.start(lambda: self._send(action, run_id, approve))
        except Exception:
            with self._lock:
                self._busy = False
            raise

    def _send(self, action: ActionResponse, run_id: UUID | None, approve: bool) -> None:
        try:
            result = self.client.decide(action.id, approve)
            if approve:
                result = self.client.resume(action.id)
            note = RESULTS.get(result.state, NOT_DONE)
        except GatewayError as error:
            note = f"Não consegui concluir: {error}"
        try:
            following = self._pending(run_id)  # outra ação do mesmo pedido
        except GatewayError:
            following = None
        with self._lock:
            self._busy = False
            self._action, self._shown_at = following, self.clock()
            self._note = (note, self.clock() + NOTE_SECONDS)


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
        approvals: ApprovalPanel | None = None,
    ) -> None:
        self.controller, self.display, self.ready, self.start = (
            controller,
            display,
            ready,
            start,
        )
        self.approvals = approvals
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

    def _reviewing(self) -> bool:
        return self.approvals is not None and self.approvals.active()

    def available(self) -> bool:
        return (
            self.online.is_set()
            and not self.stop.is_set()
            and not self._work.locked()
            and not self._reviewing()
        )

    def activate(self) -> bool:
        if (
            self.stop.is_set()
            or not self.online.is_set()
            or self._reviewing()
            or not self._work.acquire(blocking=False)
        ):
            return False

        def turn() -> None:
            try:
                outcome = self.controller.interact(True, self.stop.is_set)
                if outcome.state == RobotState.OFFLINE:
                    self.online.clear()
                elif (
                    outcome.feedback == Feedback.APPROVAL
                    and outcome.response is not None
                    and self.approvals is not None
                ):
                    self.approvals.refresh(outcome.response.run_id)
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
        token = api_token()
        with httpx2.Client(base_url=url, timeout=35, follow_redirects=False) as http:
            controller = VoiceController(
                capture,
                PipeWirePlayback(),
                display,
                HttpVoiceGateway(http, token),
                FileSessionState(args.state_file, url),
            )

            def ready() -> bool:
                return gateway_ready(http)

            approvals = ApprovalPanel(ApprovalClient(http, token))
            runtime = ContinuousRobot(controller, display, ready, approvals=approvals)
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
                    prompt=approvals.view,
                    on_choice=approvals.choose,
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
