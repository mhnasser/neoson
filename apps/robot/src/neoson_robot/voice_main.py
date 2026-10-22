"""Robot de voz: simulação ou dispositivos PipeWire; nunca imprime transcrições ou
respostas, só estado e aviso."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx2

from neoson_contracts.events.v1.robot import RobotState
from neoson_robot.config import api_token, gateway_url
from neoson_robot.fake_devices import FakeCapture, FakeDisplay, FakePlayback
from neoson_robot.pipewire import PipeWireCapture, PipeWirePlayback
from neoson_robot.session_state import FileSessionState
from neoson_robot.voice_client import HttpVoiceGateway
from neoson_robot.voice_controller import (
    Capture,
    Display,
    Feedback,
    Playback,
    VoiceController,
)


class ConsoleDisplay:
    """Mostra cada estado no terminal, sem conteúdo da conversa."""

    def show(self, state: RobotState, feedback: Feedback = Feedback.NONE) -> None:
        suffix = "" if feedback is Feedback.NONE else f" (aviso: {feedback.value})"
        print(f"[robot] {state.value}{suffix}", flush=True)


def main(argv: Sequence[str] | None = None, http: httpx2.Client | None = None) -> int:
    parser = argparse.ArgumentParser(description="Robot de voz.")
    parser.add_argument(
        "--simulate", action="store_true", help="captura/playback/display fake"
    )
    parser.add_argument(
        "--device",
        choices=["pipewire"],
        help="microfone e saída de som reais pelo PipeWire (pw-record/pw-play)",
    )
    parser.add_argument(
        "--seconds", type=float, default=5, help="duração da captura (até 15 s)"
    )
    parser.add_argument(
        "--activate", action="store_true", help="ativar um único pedido"
    )
    parser.add_argument("--gateway-url")
    parser.add_argument(
        "--state-file", type=Path, default=Path("data/robot-session.json")
    )
    args = parser.parse_args(argv)
    if args.simulate == (args.device is not None):
        parser.error("Escolha --simulate ou --device pipewire.")
    devices: tuple[Capture, Playback, Display]
    try:
        devices = (
            (FakeCapture(), FakePlayback(), FakeDisplay())
            if args.simulate
            else (PipeWireCapture(args.seconds), PipeWirePlayback(), ConsoleDisplay())
        )
    except ValueError:
        parser.error("--seconds deve ficar entre 0 e 15.")
    try:
        url = gateway_url(args.gateway_url)
    except ValueError:
        parser.error("URL do gateway inválida.")
    transport = http
    try:
        transport = transport or httpx2.Client(
            base_url=url, timeout=35, follow_redirects=False
        )
        capture, playback, display = devices
        controller = VoiceController(
            capture,
            playback,
            display,
            HttpVoiceGateway(transport, api_token()),
            FileSessionState(args.state_file, url),
        )
        outcome = controller.interact(args.activate)
        print("estado: " + outcome.state.value + "; aviso: " + outcome.feedback.value)
        if outcome.feedback == Feedback.CANCELLED:
            return 130
        return 1 if outcome.state in (RobotState.ERROR, RobotState.OFFLINE) else 0
    except Exception:
        print("Falha ao iniciar o Robot de voz.", file=sys.stderr)
        return 1
    finally:
        if http is None and transport is not None:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
