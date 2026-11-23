"""Escuta contínua: "Ei NeoSon" acorda o robô e o silêncio encerra o pedido.

O microfone fica aberto o tempo todo, mas o áudio não sai da Pi: cada trecho de
80 ms passa pelo detector local (pipeline do openWakeWord em ONNX: melspectrograma,
embedding e o modelo da frase) e é descartado. Só o que for dito depois da wake
word vira WAV e segue para o gateway.
"""

import importlib
import io
import math
import subprocess
import threading
import wave
from array import array
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any, Protocol

from neoson_robot.pipewire import DeviceError
from neoson_robot.voice_controller import Cancelled

RATE = 16000
FRAME = 1280  # 80 ms: o passo do openWakeWord
FRAME_BYTES = FRAME * 2
MEL_CONTEXT = 480  # amostras anteriores que o melspectrograma usa em cada trecho
MEL_BANDS, MEL_WINDOW, MEL_STEP, EMBEDDINGS = 32, 76, 8, 16
MODEL_FILES = ("melspectrogram.onnx", "embedding_model.onnx", "ei_neoson.onnx")

# Fim da fala. O microfone da Pi capta a fala só ~8-9 dB acima do ruído, então o
# limiar segue o ruído medido antes da wake word, e não um valor fixo.
SPEECH_RATIO = 2.0
MIN_SPEECH_RMS = 250.0
FLOOR_FRAMES = 62  # ~5 s de ruído de fundo
MIN_SPEECH_FRAMES = 3  # 240 ms: menos que isso é estalo, não pedido
END_SILENCE_FRAMES = 12  # ~1 s de silêncio encerra o pedido
NO_SPEECH_FRAMES = 50  # ~4 s sem fala depois da wake word: volta ao repouso
MAX_REQUEST_SECONDS = 15.0


class Session(Protocol):
    def get_inputs(self) -> Sequence[Any]: ...
    def run(self, outputs: Any, feeds: dict[str, Any]) -> list[Any]: ...


def _run(session: Session, value: Any) -> Any:
    return session.run(None, {session.get_inputs()[0].name: value})[0]


def mel_frames(mel: Session, clips: Any) -> Any:
    """(N, amostras) int16 -> (N, quadros, 32), com a escala do openWakeWord."""
    np = importlib.import_module("numpy")
    out = _run(mel, np.asarray(clips, dtype=np.float32))
    return out.reshape(len(clips), -1, MEL_BANDS) / 10 + 2


def embeddings(embed: Session, windows: Any) -> Any:
    """(B, 76, 32) -> (B, 96)."""
    np = importlib.import_module("numpy")
    out = _run(embed, np.asarray(windows, dtype=np.float32)[..., None])
    return out.reshape(len(windows), -1)


def clip_features(mel: Session, embed: Session, clips: Any) -> Any:
    """Recursos de clipes inteiros (treino): (N, amostras) -> (N, janelas, 96)."""
    np = importlib.import_module("numpy")
    mels = mel_frames(mel, clips)
    starts = range(0, mels.shape[1] - MEL_WINDOW + 1, MEL_STEP)
    windows = np.stack([mels[:, s : s + MEL_WINDOW] for s in starts], axis=1)
    flat = embeddings(embed, windows.reshape(-1, MEL_WINDOW, MEL_BANDS))
    return flat.reshape(len(clips), len(starts), -1)


class WakeWord:
    """Detector em fluxo: um trecho de 80 ms por vez, sem guardar o áudio."""

    def __init__(
        self, mel: Session, embed: Session, head: Session, threshold: float = 0.5
    ) -> None:
        self._np = importlib.import_module("numpy")
        self._mel, self._embed, self._head = mel, embed, head
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        np = self._np
        self._tail = np.zeros(MEL_CONTEXT, np.int16)
        self._mels = np.zeros((0, MEL_BANDS), np.float32)
        self._features: deque[Any] = deque(maxlen=EMBEDDINGS)

    def score(self, frame: bytes) -> float:
        np = self._np
        audio = np.frombuffer(frame, np.int16)
        chunk = np.concatenate((self._tail, audio))
        self._tail = chunk[-MEL_CONTEXT:]
        self._mels = np.vstack((self._mels, mel_frames(self._mel, chunk[None])[0]))
        self._mels = self._mels[-MEL_WINDOW:]
        if len(self._mels) < MEL_WINDOW:
            return 0.0  # ~0,8 s depois do reset: ainda sem janela completa
        self._features.append(embeddings(self._embed, self._mels[None])[0])
        if len(self._features) < EMBEDDINGS:
            return 0.0
        value = _run(self._head, np.stack(self._features)[None].astype(np.float32))
        return float(value.reshape(-1)[0])

    def hit(self, frame: bytes) -> bool:
        return self.score(frame) >= self.threshold


def load_wake_word(folder: Path, threshold: float) -> WakeWord:
    ort = importlib.import_module("onnxruntime")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1  # o rosto e o gateway dividem a mesma CPU
    mel, embed, head = (
        ort.InferenceSession(
            str(folder / name), options, providers=["CPUExecutionProvider"]
        )
        for name in MODEL_FILES
    )
    return WakeWord(mel, embed, head, threshold)


class Process(Protocol):
    stdout: IO[bytes] | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def _spawn(args: Sequence[str]) -> Process:
    return subprocess.Popen(
        list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


class MicStream:
    """Um `pw-record` contínuo em PCM cru; guarda só os últimos segundos."""

    def __init__(
        self,
        *,
        command: str = "pw-record",
        spawn: Callable[[Sequence[str]], Process] = _spawn,
        keep_seconds: float = 3.0,
    ) -> None:
        self._command, self._spawn = command, spawn
        self._frames: deque[bytes] = deque(maxlen=round(keep_seconds * RATE / FRAME))
        self._ready = threading.Condition()
        self._process: Process | None = None
        self._ended = False

    def start(self) -> None:
        args = ["--rate", str(RATE), "--channels", "1", "--format", "s16"]
        try:
            self._process = self._spawn([self._command, *args, "--raw", "-"])
        except OSError:
            raise DeviceError(f"{self._command} indisponível.") from None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        stream = self._process.stdout if self._process is not None else None
        try:
            while stream is not None:
                chunk = stream.read(FRAME_BYTES)
                if len(chunk) < FRAME_BYTES:
                    break
                with self._ready:
                    self._frames.append(chunk)
                    self._ready.notify()
        finally:
            with self._ready:
                self._ended = True
                self._ready.notify_all()

    def frame(self, timeout: float = 1.0) -> bytes | None:
        """Próximo trecho de 80 ms; None se nada chegou no prazo."""
        with self._ready:
            if not self._frames and not self._ended:
                self._ready.wait(timeout)
            if self._frames:
                return self._frames.popleft()
            if self._ended:
                raise DeviceError("O microfone parou.")
            return None

    def clear(self) -> None:
        with self._ready:
            self._frames.clear()

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


def rms(frame: bytes) -> float:
    samples = array("h", frame)
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def wav(frames: Sequence[bytes]) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(b"".join(frames))
    return buffer.getvalue()


class Frames(Protocol):
    def frame(self, timeout: float = 1.0) -> bytes | None: ...


class SpokenRequest:
    """Captura do controlador: o que vem depois da wake word, até o silêncio."""

    simulated = False

    def __init__(self, mic: Frames, max_seconds: float = 12.0) -> None:
        if not 0 < max_seconds <= MAX_REQUEST_SECONDS:
            raise ValueError("O pedido deve durar entre 0 e 15 s.")
        self.mic = mic
        self.limit = round(max_seconds * RATE / FRAME)
        self.levels: deque[float] = deque(maxlen=FLOOR_FRAMES)

    def observe(self, frame: bytes) -> None:
        """Ruído de fundo, medido enquanto espera a wake word."""
        self.levels.append(rms(frame))

    def threshold(self) -> float:
        quiet = sorted(self.levels)[len(self.levels) // 5] if self.levels else 0.0
        return max(SPEECH_RATIO * quiet, MIN_SPEECH_RMS)

    def record(self, cancelled: Callable[[], bool]) -> bytes:
        threshold = self.threshold()
        frames: list[bytes] = []
        spoken = quiet = 0
        while len(frames) < self.limit:
            if cancelled():
                raise Cancelled
            frame = self.mic.frame()
            if frame is None:
                raise DeviceError("O microfone não entregou áudio.")
            frames.append(frame)
            if rms(frame) >= threshold:
                spoken, quiet = spoken + 1, 0
            else:
                quiet += 1
            if spoken >= MIN_SPEECH_FRAMES and quiet >= END_SILENCE_FRAMES:
                break
            if spoken < MIN_SPEECH_FRAMES and len(frames) >= NO_SPEECH_FRAMES:
                raise Cancelled  # acordou, mas ninguém pediu nada
        if spoken < MIN_SPEECH_FRAMES:
            raise Cancelled
        return wav(frames)

    def stop(self) -> None:
        pass  # o microfone é do WakeListener e continua aberto


class Detector(Protocol):
    def hit(self, frame: bytes) -> bool: ...
    def reset(self) -> None: ...


class Runtime(Protocol):
    stop: threading.Event

    def available(self) -> bool: ...
    def activate(self) -> bool: ...


class Mic(Frames, Protocol):
    def clear(self) -> None: ...


class WakeListener:
    """Laço da escuta: um pedido por vez, sem se acordar com a própria voz."""

    def __init__(
        self, mic: Mic, detector: Detector, request: SpokenRequest, runtime: Runtime
    ) -> None:
        self.mic, self.detector, self.request = mic, detector, request
        self.runtime = runtime

    def step(self) -> bool:
        """Processa um trecho; True quando a wake word abriu um pedido."""
        frame = self.mic.frame()
        if frame is None:
            return False
        self.request.observe(frame)
        # O detector segue o fluxo mesmo ocupado: zerar a cada verificação de saúde
        # do gateway perderia a frase dita naquele instante.
        if not self.detector.hit(frame) or not self.runtime.available():
            return False
        # O turno roda nesta mesma thread; o que o microfone ouviu enquanto isso,
        # inclusive a resposta falada pelo robô, é descartado.
        started = self.runtime.activate()
        self.mic.clear()
        self.detector.reset()
        return started

    def run(self) -> None:
        while not self.runtime.stop.is_set():
            self.step()
