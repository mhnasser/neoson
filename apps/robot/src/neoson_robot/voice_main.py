"""Demonstração explícita do Robot sem hardware; não imprime transcrições/respostas."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx2

from neoson_contracts.events.v1.robot import RobotState
from neoson_robot.config import api_token, gateway_url
from neoson_robot.fake_devices import FakeCapture, FakeDisplay, FakePlayback
from neoson_robot.session_state import FileSessionState
from neoson_robot.voice_client import HttpVoiceGateway
from neoson_robot.voice_controller import Feedback, VoiceController


def main(argv: Sequence[str] | None = None, http: httpx2.Client | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Robot de voz: demonstração sem hardware."
    )
    parser.add_argument(
        "--simulate", action="store_true", help="captura/playback/display fake"
    )
    parser.add_argument(
        "--activate", action="store_true", help="ativar um único pedido"
    )
    parser.add_argument("--gateway-url")
    parser.add_argument(
        "--state-file", type=Path, default=Path("data/robot-session.json")
    )
    args = parser.parse_args(argv)
    if not args.simulate:
        parser.error("Este incremento exige --simulate; hardware não está configurado.")
    try:
        url = gateway_url(args.gateway_url)
    except ValueError:
        parser.error("URL do gateway inválida.")
    transport = http
    try:
        transport = transport or httpx2.Client(
            base_url=url, timeout=35, follow_redirects=False
        )
        controller = VoiceController(
            FakeCapture(),
            FakePlayback(),
            FakeDisplay(),
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
