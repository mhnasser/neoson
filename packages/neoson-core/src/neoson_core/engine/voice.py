"""Portas de fala e pipeline limitado: áudio fica somente na memória da requisição."""

import asyncio
import io
import logging
import math
import re
import struct
import wave
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal, Protocol
from uuid import UUID

from neoson_core.engine.conversation import Engine, RunResult, RunStatus, UserMessage
from neoson_core.engine.persistence import SessionNotFound

logger = logging.getLogger(__name__)
SAMPLE_RATE = 16000
# Grafia só para a síntese: o nome se pronuncia "Néuson"; "ss" evita que o "s" soe "z".
# O texto continua "NeoSon".
PRONUNCIATIONS = ((re.compile(r"\bneo ?son\b", re.IGNORECASE), "Néusson"),)
AudioStatus = Literal[
    "ready", "streamed", "synthesis_timeout", "synthesis_failed", "text_too_long"
]
VoiceActivity = Literal["transcribing", "thinking", "using_tool"]
# Recebe cada frase sintetizada (WAV 16 kHz), na ordem, com índice crescente, e a
# expressão do rosto pedida pelo primeiro emoji conhecido da frase.
SpeechSink = Callable[[int, bytes, str | None], Awaitable[None]]
# Emojis, símbolos e seus modificadores (tom de pele, variação, junção, bandeiras).
EMOJI = "\U0001f000-\U0001faff⌀-⏿☀-➿⬀-⯿️‍⃣\U000e0020-\U000e007f"
# Um emoji por vez, ou um emoticon ASCII solto entre espaços (não pega "10:30").
EMOTES = re.compile(rf"[{EMOJI}]|(?<!\S)(?:[:;=]-?[()DPpOo/*3|]|\^_?\^|<3)(?!\S)")
FACES = {
    emote: face
    for face, emotes in (
        (
            "feliz",
            "😀 😃 😄 😁 😆 😂 🤣 😅 😊 🙂 ☺ 😉 😎 🥳 🎉 👍 "
            ":) :-) :D :-D =) ;) ;-) ^^ ^_^",
        ),
        ("surpreso", "😮 😯 😲 😳 🤯 😱 😧 😦 😵 :O :o :-O"),
        ("triste", "😢 😭 😞 😔 🙁 ☹ 😟 😥 😓 💔 :( :-( =("),
        ("carinho", "🥰 😍 😘 😚 🤗 ❤ 💙 💜 💖 💕 <3 :*"),
        ("pensando", "🤔 🧐 🤨 😕 :/ :-/"),
    )
    for emote in emotes.split()
}
# Fim de frase seguido de espaço (não corta "3.5") ou quebra de linha; o emoji logo
# depois fica com a frase, então só corta ao ver o começo da próxima.
SENTENCE_END = re.compile(
    rf"[.!?…][\s{EMOJI}]*?(?=\s+[^\s{EMOJI}])|\n[\s{EMOJI}]*?(?=[^\s{EMOJI}])"
)


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
    # Áudio sintetizado; o Robot reproduz no máximo 30 s.
    max_output_seconds: float = 30

    def __post_init__(self) -> None:
        for value, upper in (
            (self.max_bytes, 2097152),
            (self.max_concurrency, 4),
            (self.max_text_chars, 8000),
        ):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("Limite de voz inválido.")
        for duration, ceiling in (
            (self.max_seconds, 30),
            (self.timeout_seconds, 120),
            (self.max_output_seconds, 30),
        ):
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


def read_wav(
    data: bytes, limits: VoiceLimits, max_seconds: float | None = None
) -> AudioClip:
    """`max_seconds` substitui o limite de entrada (ex.: áudio sintetizado)."""
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
            if not 0 < seconds <= (max_seconds or limits.max_seconds):
                raise InvalidAudio("Duração fora do limite.")
            pcm = source.readframes(frames)
            if len(pcm) != frames * 2:
                raise InvalidAudio("WAV truncado.")
    except (wave.Error, EOFError, struct.error, OverflowError):
        raise InvalidAudio("WAV inválido.") from None
    # Remove chunks auxiliares/metadados antes de entregar o áudio ao adaptador.
    return AudioClip(pcm_wav(pcm), seconds)


def for_speech(text: str) -> str:
    """Texto entregue à síntese: sem emojis (o rosto mostra) e com a grafia que soa
    como a pronúncia certa."""
    text = EMOTES.sub("", text)
    for pattern, spoken in PRONUNCIATIONS:
        text = pattern.sub(spoken, text)
    return text


def expression(text: str) -> str | None:
    """Expressão do rosto pedida pelo primeiro emoji conhecido do texto."""
    return next((FACES[e] for e in EMOTES.findall(text) if e in FACES), None)


class Transcriber(Protocol):
    async def transcribe(self, audio: AudioClip) -> str: ...


class Synthesizer(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class FallbackTranscriber:
    """Usa a reserva quando o principal falha (processo ou modelo indisponível).

    O prazo é o do pipeline: se o principal o esgota, não sobra tempo para a reserva.
    """

    def __init__(self, primary: Transcriber, fallback: Transcriber) -> None:
        self.primary, self.fallback = primary, fallback

    async def transcribe(self, audio: AudioClip) -> str:
        try:
            return await self.primary.transcribe(audio)
        except Exception as error:
            logger.warning("Transcrição principal falhou (%s).", type(error).__name__)
            return await self.fallback.transcribe(audio)


def speakable(text: str, limit: int) -> str:
    """Corta a fala num fim de frase ou palavra, sem mudar o texto da resposta."""
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    head = clean[:limit]
    cut = max(head.rfind(ending) for ending in (". ", "! ", "? "))
    if head[-1] in ".!?" and clean[limit] == " ":
        cut = limit - 1
    if cut >= limit // 3:
        return head[: cut + 1]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head[:-1]).rstrip(",;:") + "…"


class SentenceSplitter:
    """Junta os pedaços do modelo e solta frases inteiras para a síntese."""

    def __init__(self, min_chars: int = 20, max_chars: int = 200) -> None:
        self.min_chars, self.max_chars = min_chars, max_chars
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        sentences: list[str] = []
        while cut := self._cut():
            sentences.append(self._buffer[:cut].strip())
            self._buffer = self._buffer[cut:]
        return sentences

    def flush(self) -> list[str]:
        rest, self._buffer = self._buffer.strip(), ""
        return [rest] if rest else []

    def _cut(self) -> int:
        # Frase curta ("Claro!", "1.") junta com a próxima: entonação melhor.
        for match in SENTENCE_END.finditer(self._buffer):
            if len(self._buffer[: match.end()].strip()) >= self.min_chars:
                return match.end()
        if len(self._buffer) <= self.max_chars:
            return 0
        head = self._buffer[: self.max_chars]
        cut = max(head.rfind(mark) for mark in ",;:")
        if cut <= 0:
            cut = head.rfind(" ")
        return cut + 1 if cut > 0 else self.max_chars


class _Streamer:
    """Sintetiza as frases em ordem, numa tarefa própria, e entrega cada WAV."""

    def __init__(
        self, pipeline: "VoicePipeline", sink: SpeechSink, deadline: float
    ) -> None:
        self.pipeline, self.sink, self.deadline = pipeline, sink, deadline
        self.splitter = SentenceSplitter()
        self.queue: asyncio.Queue[tuple[str, str | None] | None] = asyncio.Queue()
        self.budget = pipeline.max_speech_chars
        self.queued = self.finished = False
        self.characters = self.index = 0
        self.seconds = 0.0
        self.status: AudioStatus = "streamed"
        self.task = asyncio.create_task(self._run())

    def text(self, piece: str) -> None:
        for sentence in self.splitter.feed(piece):
            self._put(sentence)

    def flush(self) -> None:
        for sentence in self.splitter.flush():
            self._put(sentence)

    def say(self, text: str) -> None:
        self.text(text)
        self.flush()

    def _put(self, sentence: str) -> None:
        spoken = " ".join(for_speech(sentence).split())
        # Só emoji ou pontuação: nada a falar.
        if self.budget <= 0 or not any(ch.isalnum() for ch in spoken):
            return
        if len(spoken) > self.budget:
            # Frase cortada encerra a fala: nada de pedaços soltos depois dela.
            spoken, self.budget = speakable(spoken, self.budget), 0
        self.budget -= len(spoken)
        self.queued = True
        self.queue.put_nowait((spoken, expression(sentence)))

    async def finish(self) -> AudioStatus:
        self.flush()
        self.queue.put_nowait(None)
        await self.task
        self.finished = True
        return self.status

    async def _run(self) -> None:
        limits = self.pipeline.limits
        while (item := await self.queue.get()) is not None:
            text, face = item
            if self.status != "streamed" or self.seconds >= limits.max_output_seconds:
                continue
            self.characters += len(text)
            try:
                wav = await asyncio.wait_for(
                    self.pipeline.synthesizer.synthesize(text),
                    timeout=max(self.deadline - monotonic(), 0),
                )
                clip = read_wav(wav, limits, limits.max_output_seconds)
            except TimeoutError:
                self.status = "synthesis_timeout"
                continue
            except Exception:
                self.status = "synthesis_failed"
                continue
            if self.seconds + clip.seconds > limits.max_output_seconds:
                # Limite total de fala: para aqui, como `speakable` corta o texto.
                self.seconds = limits.max_output_seconds
                continue
            self.seconds += clip.seconds
            try:
                # Cliente que parou de ler não pode prender a vaga de voz além do prazo.
                await asyncio.wait_for(
                    self.sink(self.index, clip.wav, face),
                    timeout=max(self.deadline - monotonic(), 0),
                )
            except TimeoutError:
                self.status = "synthesis_timeout"
                continue
            self.index += 1


@dataclass(frozen=True, slots=True)
class VoiceResult:
    result: RunResult = field(repr=False)
    audio: AudioClip | None = field(default=None, repr=False)
    audio_status: AudioStatus = "ready"


@dataclass(frozen=True, slots=True)
class SpeechUsage:
    stt_provider: str
    tts_provider: str
    stt_seconds: float
    tts_characters: int
    complete: bool

    def __post_init__(self) -> None:
        if (
            not all(
                re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
                for name in (self.stt_provider, self.tts_provider)
            )
            or not math.isfinite(self.stt_seconds)
            or not 0 <= self.stt_seconds <= 30
            or type(self.tts_characters) is not int
            or not 0 <= self.tts_characters <= 8000
            or type(self.complete) is not bool
        ):
            raise ValueError("Métricas de voz inválidas.")


class VoicePipeline:
    def __init__(
        self,
        engine: Engine,
        transcriber: Transcriber,
        synthesizer: Synthesizer,
        limits: VoiceLimits | None = None,
        *,
        simulated: bool = False,
        max_speech_chars: int = 8000,
        stt_provider: str = "unknown",
        tts_provider: str = "unknown",
        record_usage: Callable[[UUID, SpeechUsage], Awaitable[None]] | None = None,
    ) -> None:
        self.engine, self.transcriber, self.synthesizer = (
            engine,
            transcriber,
            synthesizer,
        )
        self.limits, self.simulated = limits or VoiceLimits(), simulated
        if not 1 <= max_speech_chars <= 8000:
            raise ValueError("Limite de caracteres de fala inválido.")
        self.max_speech_chars = max_speech_chars
        self.stt_provider, self.tts_provider, self.record_usage = (
            stt_provider,
            tts_provider,
            record_usage,
        )
        self._slots = asyncio.Semaphore(self.limits.max_concurrency)

    async def process(
        self,
        load: Callable[[], Awaitable[bytes]],
        session_id: UUID | None,
        principal: str,
        on_activity: Callable[[VoiceActivity], None] | None = None,
        on_speech: SpeechSink | None = None,
    ) -> VoiceResult:
        """Com `on_speech`, fala frase a frase enquanto o modelo escreve."""
        if self._slots.locked():
            raise SpeechUnavailable("voice_busy")
        await self._slots.acquire()
        deadline = monotonic() + self.limits.timeout_seconds
        result: RunResult | None = None
        streamer: _Streamer | None = None
        seconds, characters, complete = 0.0, 0, False
        try:
            try:
                raw = await asyncio.wait_for(
                    load(), timeout=max(deadline - monotonic(), 0)
                )
            except TimeoutError:
                raise SpeechUnavailable("voice_upload_timeout") from None
            audio = read_wav(raw, self.limits)
            seconds = audio.seconds
            if on_activity:
                on_activity("transcribing")
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
                if on_activity:
                    on_activity("thinking")
                if on_speech is not None:
                    streamer = _Streamer(self, on_speech, deadline)
                result = await asyncio.wait_for(
                    self.engine.run(
                        UserMessage(
                            text,
                            session_id,
                            channel="voice",
                            principal=principal,
                            on_activity=on_activity,
                            on_text=None if streamer is None else streamer.text,
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
            complete = True
            if streamer is not None:
                streamer.flush()
                # Modelo sem streaming, ou texto que não veio dele (aprovação, falha).
                if not streamer.queued or result.status is not RunStatus.COMPLETED:
                    streamer.say(result.text)
                return VoiceResult(result, audio_status=await streamer.finish())
            if len(result.text) > self.limits.max_text_chars:
                return VoiceResult(result, audio_status="text_too_long")
            speech_text = speakable(for_speech(result.text), self.max_speech_chars)
            characters, complete = len(speech_text), False
            try:
                response = await asyncio.wait_for(
                    self.synthesizer.synthesize(speech_text),
                    timeout=max(deadline - monotonic(), 0),
                )
                output = read_wav(response, self.limits, self.limits.max_output_seconds)
                complete = True
            except TimeoutError:
                return VoiceResult(result, audio_status="synthesis_timeout")
            except Exception:
                return VoiceResult(result, audio_status="synthesis_failed")
            return VoiceResult(result, output)
        finally:
            if streamer is not None:
                if not streamer.task.done():
                    streamer.task.cancel()
                    with suppress(asyncio.CancelledError):
                        await streamer.task
                characters = streamer.characters
                complete = (
                    complete and streamer.finished and streamer.status == "streamed"
                )
            try:
                if result is not None and self.record_usage is not None:
                    await self.record_usage(
                        result.run_id,
                        SpeechUsage(
                            self.stt_provider,
                            self.tts_provider,
                            seconds,
                            characters,
                            complete,
                        ),
                    )
            finally:
                self._slots.release()
