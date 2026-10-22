"""Dispositivos sem hardware, sem captura/reprodução reais ou conteúdo no display."""

import io
import wave
from collections.abc import Callable

from neoson_contracts.events.v1.robot import RobotState
from neoson_robot.voice_controller import Cancelled, Feedback


def silent_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0" * 6400)
    return buffer.getvalue()


class FakeCapture:
    simulated = True

    def __init__(self) -> None:
        self.calls = 0
        self.stopped = True

    def record(self, cancelled: Callable[[], bool]) -> bytes:
        if cancelled():
            raise Cancelled
        self.calls += 1
        self.stopped = False
        return silent_wav()

    def stop(self) -> None:
        self.stopped = True


class FakePlayback:
    def __init__(self) -> None:
        self.calls = 0
        self.stopped = True

    def play(self, audio: bytes, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise Cancelled
        self.stopped = False
        self.calls += 1

    def stop(self) -> None:
        self.stopped = True


class FakeDisplay:
    def __init__(self) -> None:
        self.frames: list[tuple[RobotState, Feedback]] = []

    def show(self, state: RobotState, feedback: Feedback = Feedback.NONE) -> None:
        self.frames.append((state, feedback))
