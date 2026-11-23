import io
import threading
from array import array
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import numpy as np
import pytest

from neoson_robot.chest_display import (
    Lcd1602,
    read_status,
    run,
    write_status,
)
from neoson_robot.pipewire import DeviceError
from neoson_robot.voice_client import check_audio
from neoson_robot.voice_controller import Cancelled
from neoson_robot.wake import (
    EMBEDDINGS,
    END_SILENCE_FRAMES,
    FRAME,
    FRAME_BYTES,
    MEL_CONTEXT,
    MEL_STEP,
    MEL_WINDOW,
    NO_SPEECH_FRAMES,
    MicStream,
    SpokenRequest,
    WakeWord,
    clip_features,
)


def tone(level: int) -> bytes:
    """Trecho de 80 ms com RMS = level."""
    return array("h", [level, -level] * (FRAME // 2)).tobytes()


class Frames:
    def __init__(self, frames: Sequence[bytes]) -> None:
        self.frames = list(frames)

    def frame(self, timeout: float = 1.0) -> bytes | None:
        return self.frames.pop(0) if self.frames else None


def request(frames: Sequence[bytes], noise: int = 100) -> SpokenRequest:
    spoken = SpokenRequest(Frames(frames), max_seconds=12)
    for _ in range(20):
        spoken.observe(tone(noise))
    return spoken


def test_request_ends_on_silence_and_is_a_valid_wav() -> None:
    frames = [tone(100)] * 5 + [tone(900)] * 10 + [tone(100)] * 30
    audio = request(frames).record(lambda: False)
    check_audio(audio)
    assert (len(audio) - 44) // FRAME_BYTES == 5 + 10 + END_SILENCE_FRAMES


@pytest.mark.parametrize(
    "frames",
    [
        [tone(100)] * (NO_SPEECH_FRAMES + 5),  # acordou e ninguém falou
        [tone(900)] * 2 + [tone(100)] * (NO_SPEECH_FRAMES + 3),  # só um estalo
    ],
)
def test_no_request_after_the_wake_word_goes_back_to_rest(
    frames: list[bytes],
) -> None:
    spoken = request(frames)
    with pytest.raises(Cancelled):
        spoken.record(lambda: False)
    assert len(spoken.mic.frames) == 5  # type: ignore[attr-defined]


def test_threshold_follows_the_room_noise() -> None:
    # Fala ~8 dB acima do ruído, como no microfone da Pi, ainda conta como fala.
    assert request([], noise=180).threshold() == pytest.approx(360)
    assert request([], noise=0).threshold() == 250  # piso para sala silenciosa
    noisy = request([tone(400)] * (NO_SPEECH_FRAMES + 1), noise=300)
    with pytest.raises(Cancelled):  # 400 é ruído numa sala com 300 de fundo
        noisy.record(lambda: False)


def test_long_request_is_cut_at_the_limit_and_cancel_wins() -> None:
    spoken = SpokenRequest(Frames([tone(900)] * 400), max_seconds=2)
    audio = spoken.record(lambda: False)
    assert (len(audio) - 44) // FRAME_BYTES == 25
    with pytest.raises(Cancelled):
        request([tone(900)] * 10).record(lambda: True)
    with pytest.raises(DeviceError):
        request([]).record(lambda: False)  # microfone parado não vira pedido
    with pytest.raises(ValueError):
        SpokenRequest(Frames([]), max_seconds=16)


class Process:
    def __init__(self, data: bytes) -> None:
        self.stdout: IO[bytes] | None = io.BytesIO(data)
        self.terminated = False

    def poll(self) -> int | None:
        return None if not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_mic_streams_raw_frames_and_fails_loudly_when_it_stops() -> None:
    started: list[Sequence[str]] = []
    process = Process(tone(1) + tone(2) + b"\0" * 100)  # resto incompleto descartado

    def spawn(args: Sequence[str]) -> Process:
        started.append(args)
        return process

    mic = MicStream(spawn=spawn)
    mic.start()
    assert mic.frame() == tone(1)
    assert mic.frame() == tone(2)
    with pytest.raises(DeviceError):
        mic.frame()
    assert list(started[0][-2:]) == ["--raw", "-"]
    assert "16000" in started[0]
    mic.close()
    assert process.terminated


def test_mic_keeps_only_recent_audio_and_clear_drops_it() -> None:
    mic = MicStream(spawn=lambda args: Process(tone(1) * 10), keep_seconds=0.24)
    mic.start()
    while not mic._ended:  # o leitor termina sozinho no fim do fluxo simulado
        threading.Event().wait(0.01)
    assert len(mic._frames) == 3
    mic.clear()
    with pytest.raises(DeviceError):
        mic.frame()


class Session:
    """Sessão ONNX simulada que registra as formas recebidas."""

    def __init__(self, name: str, out: Any) -> None:
        self.name, self.out = name, out
        self.shapes: list[Any] = []

    def get_inputs(self) -> list[Any]:
        return [SimpleNamespace(name=self.name)]

    def run(self, outputs: Any, feeds: dict[str, Any]) -> list[Any]:
        value = feeds[self.name]
        self.shapes.append(value.shape)
        return [self.out(value)]


def fake_models() -> tuple[Session, Session, Session]:
    def mel(x: Any) -> Any:  # 1 quadro a cada 160 amostras, menos 3 (como o real)
        return np.zeros((len(x), 1, x.shape[1] // 160 - 3, 32), np.float32)

    def embed(x: Any) -> Any:
        return np.ones((len(x), 1, 1, 96), np.float32)

    return (
        Session("input", mel),
        Session("input_1", embed),
        Session("x", lambda x: np.full((1, 1), 0.9, np.float32)),
    )


def test_wake_word_needs_full_context_before_scoring_and_after_reset() -> None:
    mel, embed, head = fake_models()
    detector = WakeWord(mel, embed, head, threshold=0.5)
    first = -(-MEL_WINDOW // MEL_STEP)  # trecho da 1ª janela de 76 quadros
    warmup = first + EMBEDDINGS - 2  # o 25º trecho (~2 s) é o primeiro avaliado
    frame = tone(500)
    assert [detector.hit(frame) for _ in range(warmup)] == [False] * warmup
    assert detector.hit(frame)
    assert mel.shapes[0] == (1, FRAME + MEL_CONTEXT)
    assert embed.shapes[0] == (1, MEL_WINDOW, 32, 1)
    assert head.shapes == [(1, EMBEDDINGS, 96)]
    detector.reset()
    assert not detector.hit(frame)


def test_clip_features_match_the_model_input() -> None:
    mel, embed, _ = fake_models()
    clips = np.zeros((3, 32000), np.int16)  # 2 s
    assert clip_features(mel, embed, clips).shape == (3, EMBEDDINGS, 96)


def test_status_file_round_trip_expiry_and_garbage(tmp_path: Path) -> None:
    path = tmp_path / "status"
    assert read_status(path, 0) is None
    write_status(path, "Escutando...", 120, now=10)
    assert read_status(path, 129) == "Escutando..."
    assert read_status(path, 131) is None
    write_status(path, "", 120, now=10)
    assert read_status(path, 11) is None
    path.write_bytes(b"\xff\xfe")
    assert read_status(path, 0) is None
    path.write_text("sem validade")
    assert read_status(path, 0) is None


class Bus:
    def __init__(self) -> None:
        self.writes: list[int] = []

    def write_byte(self, value: int) -> None:
        self.writes.append(value)

    def close(self) -> None:
        pass


class Screen(Lcd1602):
    def __init__(self) -> None:
        super().__init__(Bus(), sleep=lambda _: None)
        self.shown: list[tuple[str, ...]] = []

    def show(self, lines: Sequence[str]) -> None:
        self.shown.append(tuple(lines))
        super().show(lines)


class Steps(threading.Event):
    def __init__(self, rounds: int) -> None:
        super().__init__()
        self.rounds = rounds
        self.timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.timeouts.append(timeout)
        return len(self.timeouts) >= self.rounds


def test_chest_shows_the_robot_state_and_returns_to_usage_at_once() -> None:
    from neoson_robot.chest_display import Memory, Snapshot

    states = iter([None, "Escutando...", "Escutando...", "Pensando...", None, None])
    samples: list[int] = []

    def sample() -> Snapshot:
        samples.append(1)
        return Snapshot(10.0, 40.0, Memory(8 * 1024 * 1024, 6 * 1024 * 1024))

    lcd = Screen()
    stop = Steps(rounds=7)
    run(lcd, sample, stop, interval=2.0, status=lambda: next(states))
    assert lcd.shown == [
        ("NeoSon", "monitor..."),
        ("CPU  10%  40.0°C", "RAM  25% 2.0G/8G"),
        ("NeoSon", "Escutando..."),  # repetido não reescreve
        ("NeoSon", "Pensando..."),
        ("CPU  10%  40.0°C", "RAM  25% 2.0G/8G"),  # fim do estado: uso na hora
    ]
    assert len(samples) == 2  # a última volta ainda está no intervalo do uso
    assert stop.timeouts[1:] == [0.25] * 6
