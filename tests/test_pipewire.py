import io
import signal
import subprocess
import wave
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from neoson_contracts.api.v1.voice import VoiceResponse
from neoson_contracts.events.v1.robot import RobotState, VoiceCompleted, VoiceEvent
from neoson_robot import voice_main
from neoson_robot.fake_devices import FakeDisplay, silent_wav
from neoson_robot.pipewire import DeviceError, PipeWireCapture, PipeWirePlayback
from neoson_robot.voice_controller import Cancelled, Feedback, VoiceController


def wav(channels: int = 1, seconds: float = 0.5) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0" * int(16000 * seconds) * 2 * channels)
    return buffer.getvalue()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    """Simula `pw-record`/`pw-play`: grava o WAV ao receber SIGINT, como o real."""

    def __init__(
        self,
        args: Sequence[str],
        *,
        audio: bytes | None,
        exit_after: int | None,
        code: int,
        ignore: bool,
    ) -> None:
        self.args, self.audio = list(args), audio
        self.exit_after, self.code, self.ignore = exit_after, code, ignore
        self.polls = 0
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.killed = False

    def poll(self) -> int | None:
        self.polls += 1
        if self.returncode is None and self.exit_after is not None:
            if self.polls >= self.exit_after:
                self.returncode = self.code
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if self.ignore:
            return
        if sig == signal.SIGINT and self.audio is not None:
            Path(self.args[-1]).write_bytes(self.audio)
        self.returncode = 1 if sig == signal.SIGINT else -sig

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        return self.returncode


class Spawner:
    def __init__(
        self,
        *,
        audio: bytes | None = None,
        exit_after: int | None = None,
        code: int = 0,
        ignore: bool = False,
        missing: bool = False,
    ) -> None:
        self.audio, self.exit_after, self.code, self.ignore = (
            audio,
            exit_after,
            code,
            ignore,
        )
        self.missing = missing
        self.processes: list[FakeProcess] = []
        self.files: list[Path] = []

    def __call__(self, args: Sequence[str]) -> FakeProcess:
        if self.missing:
            raise FileNotFoundError(args[0])
        self.files.append(Path(args[-1]))
        process = FakeProcess(
            args,
            audio=self.audio,
            exit_after=self.exit_after,
            code=self.code,
            ignore=self.ignore,
        )
        self.processes.append(process)
        return process


def capture(spawner: Spawner, tmp_path: Path, seconds: float = 1) -> PipeWireCapture:
    clock = Clock()
    return PipeWireCapture(
        seconds, spawn=spawner, clock=clock, sleep=clock.sleep, workdir=tmp_path
    )


def playback(spawner: Spawner, tmp_path: Path) -> PipeWirePlayback:
    clock = Clock()
    return PipeWirePlayback(
        spawn=spawner, clock=clock, sleep=clock.sleep, workdir=tmp_path
    )


def test_capture_records_for_the_limit_then_sends_sigint(tmp_path: Path) -> None:
    spawner = Spawner(audio=wav())
    device = capture(spawner, tmp_path)
    assert device.record(lambda: False) == wav()
    (process,) = spawner.processes
    assert process.args[:7] == [
        "pw-record",
        "--rate",
        "16000",
        "--channels",
        "1",
        "--format",
        "s16",
    ]
    assert process.signals == [signal.SIGINT]
    assert device.simulated is False
    assert not list(tmp_path.iterdir())  # WAV temporário apagado
    device.stop()
    device.stop()  # idempotente


def test_cancelled_capture_stops_the_process_and_removes_the_file(
    tmp_path: Path,
) -> None:
    spawner = Spawner(audio=wav())
    calls = iter([False, False, True])
    with pytest.raises(Cancelled):
        capture(spawner, tmp_path).record(lambda: next(calls, True))
    assert spawner.processes[0].signals == [signal.SIGTERM]
    assert not list(tmp_path.iterdir())
    with pytest.raises(Cancelled):
        capture(Spawner(), tmp_path).record(lambda: True)


@pytest.mark.parametrize(
    ("spawner", "message"),
    [
        (Spawner(missing=True), "pw-record indisponível"),
        (Spawner(audio=wav(), exit_after=3), "terminou antes"),
        (Spawner(audio=wav(channels=2)), "inválido"),
        (Spawner(audio=b"RIFF lixo"), "inválido"),
    ],
)
def test_capture_failures_are_fixed_messages(
    tmp_path: Path, spawner: Spawner, message: str
) -> None:
    with pytest.raises(DeviceError, match=message) as error:
        capture(spawner, tmp_path).record(lambda: False)
    assert str(tmp_path) not in str(error.value)
    assert not list(tmp_path.iterdir())


def test_process_that_ignores_signals_is_killed(tmp_path: Path) -> None:
    spawner = Spawner(audio=wav(), ignore=True)
    with pytest.raises((DeviceError, OSError)):
        capture(spawner, tmp_path).record(lambda: False)
    assert spawner.processes[0].killed


def test_playback_runs_until_exit_and_cleans_up(tmp_path: Path) -> None:
    spawner = Spawner(exit_after=4)
    device = playback(spawner, tmp_path)
    device.play(silent_wav(), lambda: False)
    (process,) = spawner.processes
    assert process.args[0] == "pw-play" and process.signals == []
    assert not list(tmp_path.iterdir())
    device.stop()


@pytest.mark.parametrize(
    ("spawner", "cancel_at", "error"),
    [
        (Spawner(exit_after=3, code=1), None, DeviceError),
        (Spawner(), 3, Cancelled),
        (Spawner(), None, DeviceError),  # nunca termina: passa do prazo
        (Spawner(missing=True), None, DeviceError),
    ],
)
def test_playback_failures_cancel_and_timeout(
    tmp_path: Path, spawner: Spawner, cancel_at: int | None, error: type[Exception]
) -> None:
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return cancel_at is not None and calls["n"] >= cancel_at

    with pytest.raises(error):
        playback(spawner, tmp_path).play(silent_wav(), cancelled)
    assert not list(tmp_path.iterdir())
    for process in spawner.processes:
        assert process.returncode is not None  # nenhum processo fica rodando


def test_playback_rejects_invalid_audio_without_spawning(tmp_path: Path) -> None:
    spawner = Spawner()
    with pytest.raises(DeviceError, match="inválido"):
        playback(spawner, tmp_path).play(b"x" * 100, lambda: False)
    assert not spawner.processes


class Gateway:
    def __init__(self) -> None:
        self.simulated: list[bool] = []

    def events(
        self, audio: bytes, session_id: object, simulated_capture: bool
    ) -> Iterator[VoiceEvent]:
        self.simulated.append(simulated_capture)
        yield VoiceCompleted(
            result=VoiceResponse(
                run_id=uuid4(),
                session_id=uuid4(),
                text="privado",
                status="completed",
                audio_base64=__import__("base64").b64encode(silent_wav()).decode(),
                audio_seconds=0.2,
                simulated=False,
            )
        )


def test_controller_with_real_devices_declares_real_capture(tmp_path: Path) -> None:
    gateway, display = Gateway(), FakeDisplay()
    player = Spawner(exit_after=2)
    controller = VoiceController(
        capture(Spawner(audio=wav()), tmp_path),
        playback(player, tmp_path),
        display,
        gateway,
    )
    outcome = controller.interact(True)
    assert (outcome.state, outcome.feedback) == (RobotState.IDLE, Feedback.NONE)
    assert gateway.simulated == [False] and len(player.processes) == 1
    assert [s for s, _ in display.frames] == [
        RobotState.IDLE,
        RobotState.LISTENING,
        RobotState.THINKING,
        RobotState.SPEAKING,
        RobotState.IDLE,
    ]
    assert "privado" not in repr(display.frames)


def test_cli_requires_one_explicit_device_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    state = ["--state-file", str(tmp_path / "s.json")]
    for argv in ([], ["--simulate", "--device", "pipewire"]):
        with pytest.raises(SystemExit):
            voice_main.main(argv + state)
    with pytest.raises(SystemExit):
        voice_main.main(["--device", "pipewire", "--seconds", "20", *state])

    def forbidden(args: Sequence[str]) -> Any:
        raise AssertionError("não pode abrir dispositivo sem ativação")

    monkeypatch.setattr("neoson_robot.pipewire.spawn", forbidden)
    assert voice_main.main(["--device", "pipewire", *state]) == 0
    assert "[robot] idle" in capsys.readouterr().out
    # A troca vale de fato: com ativação, a captura chega ao executor proibido.
    assert voice_main.main(["--device", "pipewire", "--activate", *state]) == 1
    assert "[robot] error" in capsys.readouterr().out
