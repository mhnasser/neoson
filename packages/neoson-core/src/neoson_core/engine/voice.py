"""Portas de fala e pipeline limitado: áudio fica somente na memória da requisição."""

import asyncio
import io
import math
import struct
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal, Protocol
from uuid import UUID

from neoson_core.engine.conversation import Engine, RunResult, UserMessage
from neoson_core.engine.persistence import SessionNotFound

SAMPLE_RATE = 16000
AudioStatus = Literal["ready", "synthesis_timeout", "synthesis_failed", "text_too_long"]


class InvalidAudio(ValueError):
    pass


class SpeechUnavailable(RuntimeError):
    def __init__(self, code: str = "speech_unavailable") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VoiceLimits:
    max_bytes: int = 1048576
    max_seconds: float = 15
    timeout_seconds: float = 30
    max_concurrency: int = 1
    max_text_chars: int = 8000

    def __post_init__(self) -> None:
        for value, upper in (
            (self.max_bytes, 2097152),
            (self.max_concurrency, 4),
            (self.max_text_chars, 8000),
        ):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("Limite de voz inválido.")
        for duration, ceiling in ((self.max_seconds, 30), (self.timeout_seconds, 120)):
            if not math.isfinite(duration) or not 0 < duration <= ceiling:
                raise ValueError("Limite de voz inválido.")


def pcm_wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return buffer.getvalue()


@dataclass(frozen=True, slots=True)
class AudioClip:
    wav: bytes = field(repr=False)
    seconds: float


def read_wav(data: bytes, limits: VoiceLimits) -> AudioClip:
    if not isinstance(data, bytes) or len(data) > limits.max_bytes or len(data) < 44:
        raise InvalidAudio("Áudio inválido ou acima do limite.")
    if (
        data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or int.from_bytes(data[4:8], "little") + 8 != len(data)
    ):
        raise InvalidAudio("WAV inválido.")
    offset = 12
    chunks: dict[bytes, int] = {}
    while offset + 8 <= len(data):
        tag = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        end = offset + 8 + size
        if end + (size % 2) > len(data):
            raise InvalidAudio("Chunk WAV truncado.")
        chunks[tag] = chunks.get(tag, 0) + 1
        if tag == b"data" and size % 2:
            raise InvalidAudio("Frame PCM incompleto.")
        if tag == b"fmt ":
            if size < 16 or struct.unpack_from("<HHIIHH", data, offset + 8) != (
                1,
                1,
                16000,
                32000,
                2,
                16,
            ):
                raise InvalidAudio("Formato PCM inválido.")
        offset = end + size % 2
    if offset != len(data) or chunks.get(b"fmt ") != 1 or chunks.get(b"data") != 1:
        raise InvalidAudio("Estrutura WAV inválida.")
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != SAMPLE_RATE
                or source.getcomptype() != "NONE"
            ):
                raise InvalidAudio("Use WAV PCM16 mono de 16 kHz.")
            frames = source.getnframes()
            seconds = frames / SAMPLE_RATE
            if not 0 < seconds <= limits.max_seconds:
                raise InvalidAudio("Duração fora do limite.")
            pcm = source.readframes(frames)
            if len(pcm) != frames * 2:
                raise InvalidAudio("WAV truncado.")
    except (wave.Error, EOFError, struct.error, OverflowError):
        raise InvalidAudio("WAV inválido.") from None
    # Remove chunks auxiliares/metadados antes de entregar o áudio ao adaptador.
    return AudioClip(pcm_wav(pcm), seconds)


class Transcriber(Protocol):
    async def transcribe(self, audio: AudioClip) -> str: ...


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class VoiceResult:
    result: RunResult = field(repr=False)
    audio: AudioClip | None = field(default=None, repr=False)
    audio_status: AudioStatus = "ready"


class VoicePipeline:
    def __init__(
        self,
        engine: Engine,
        transcriber: Transcriber,
        synthesizer: Synthesizer,
        limits: VoiceLimits | None = None,
        *,
        simulated: bool = False,
    ) -> None:
        self.engine, self.transcriber, self.synthesizer = (
            engine,
            transcriber,
            synthesizer,
        )
        self.limits, self.simulated = limits or VoiceLimits(), simulated
        self._slots = asyncio.Semaphore(self.limits.max_concurrency)

    async def process(
        self,
        load: Callable[[], Awaitable[bytes]],
        session_id: UUID | None,
        principal: str,
    ) -> VoiceResult:
        if self._slots.locked():
            raise SpeechUnavailable("voice_busy")
        await self._slots.acquire()
        deadline = monotonic() + self.limits.timeout_seconds
        try:
            try:
                raw = await asyncio.wait_for(
                    load(), timeout=max(deadline - monotonic(), 0)
                )
            except TimeoutError:
                raise SpeechUnavailable("voice_upload_timeout") from None
            audio = read_wav(raw, self.limits)
            try:
                text = await asyncio.wait_for(
                    self.transcriber.transcribe(audio),
                    timeout=max(deadline - monotonic(), 0),
                )
            except TimeoutError:
                raise SpeechUnavailable("transcription_timeout") from None
            except Exception:
                raise SpeechUnavailable() from None
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > self.limits.max_text_chars
            ):
                raise SpeechUnavailable("invalid_transcription")
            try:
                result = await asyncio.wait_for(
                    self.engine.run(
                        UserMessage(
                            text, session_id, channel="voice", principal=principal
                        )
                    ),
                    timeout=max(deadline - monotonic(), 0),
                )
            except SessionNotFound:
                raise
            except TimeoutError:
                raise SpeechUnavailable("voice_execution_timeout") from None
            except Exception:
                raise SpeechUnavailable("voice_execution_failed") from None
            if len(result.text) > self.limits.max_text_chars:
                return VoiceResult(result, audio_status="text_too_long")
            try:
                response = await asyncio.wait_for(
                    self.synthesizer.synthesize(result.text),
                    timeout=max(deadline - monotonic(), 0),
                )
                output = read_wav(response, self.limits)
            except TimeoutError:
                return VoiceResult(result, audio_status="synthesis_timeout")
            except Exception:
                return VoiceResult(result, audio_status="synthesis_failed")
            return VoiceResult(result, output)
        finally:
            self._slots.release()
