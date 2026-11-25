"""Fala frase a frase enquanto o modelo escreve."""

import asyncio
from collections.abc import Sequence
from typing import get_args
from uuid import UUID, uuid4

import pytest

from neoson_contracts.events.v1.robot import Expression
from neoson_core.engine import RunResult, RunStatus, UserMessage
from neoson_core.engine.voice import (
    FACES,
    SentenceSplitter,
    SpeechUsage,
    VoiceLimits,
    VoicePipeline,
    VoiceResult,
    expression,
    for_speech,
    pcm_wav,
)

PRIVATE = "fala-privada-nao-exportar"
CLIP = pcm_wav(b"\x01\x00" * 1600)  # 0,1 s em 16 kHz
WAIT = object()


class Speech:
    def __init__(self, fail_at: int | None = None, slow_at: int | None = None) -> None:
        self.spoken: list[str] = []
        self.first = asyncio.Event()
        self.fail_at, self.slow_at = fail_at, slow_at

    async def transcribe(self, audio: object) -> str:
        return "pergunta"

    async def synthesize(self, text: str) -> bytes:
        self.spoken.append(text)
        self.first.set()
        if len(self.spoken) == self.fail_at:
            raise RuntimeError(PRIVATE)
        if len(self.spoken) == self.slow_at:
            await asyncio.sleep(5)
        return CLIP


class Engine:
    def __init__(
        self,
        speech: Speech,
        pieces: Sequence[object] = (),
        text: str = "",
        status: RunStatus = RunStatus.COMPLETED,
    ) -> None:
        self.speech, self.pieces, self.text, self.status = speech, pieces, text, status
        self.received: list[UserMessage] = []

    async def run(self, message: UserMessage) -> RunResult:
        self.received.append(message)
        for piece in self.pieces:
            if piece is WAIT:
                await asyncio.wait_for(self.speech.first.wait(), 1)
            elif message.on_text is not None:
                assert isinstance(piece, str)
                message.on_text(piece)
        text = self.text or "".join(p for p in self.pieces if isinstance(p, str))
        return RunResult(uuid4(), text, self.status, session_id=uuid4())


async def audio() -> bytes:
    return CLIP


def speak(
    pipeline: VoicePipeline, faces: list[str | None] | None = None
) -> tuple[VoiceResult, list[tuple[int, bytes]]]:
    received: list[tuple[int, bytes]] = []

    async def sink(index: int, wav: bytes, face: str | None) -> None:
        received.append((index, wav))
        if faces is not None:
            faces.append(face)

    result = asyncio.run(pipeline.process(audio, None, "dono", on_speech=sink))
    return result, received


# --- divisor de frases ---


def test_splitter_cuts_sentences_merges_short_and_keeps_decimals() -> None:
    splitter = SentenceSplitter()

    assert splitter.feed("Claro! ") == []
    assert splitter.feed("O valor é 3.5 reais hoje. E amanhã") == [
        "Claro! O valor é 3.5 reais hoje."
    ]
    assert splitter.flush() == ["E amanhã"]
    assert splitter.flush() == []

    lists = SentenceSplitter()
    pieces = lists.feed("1. Arroz\n2. Feijão\n") + lists.flush()
    assert "1." not in pieces
    assert pieces == ["1. Arroz\n2. Feijão"]


def test_splitter_keeps_a_trailing_emoji_with_its_sentence() -> None:
    splitter = SentenceSplitter()

    # Depois do ponto, espera ver o que vem: um emoji ainda é desta frase.
    assert splitter.feed("Tudo certo por aqui hoje. ") == []
    assert splitter.feed("😊") == []
    assert splitter.flush() == ["Tudo certo por aqui hoje. 😊"]

    assert splitter.feed("Que pena que deu errado. 😢 Mas amanhã") == [
        "Que pena que deu errado. 😢"
    ]


def test_emojis_and_emoticons_are_dropped_from_speech() -> None:
    def spoken(text: str) -> str:
        return " ".join(for_speech(text).split())

    assert spoken("Oi ❤️ tudo bem :-) Até 10:30 em http://x.y ;P") == (
        "Oi tudo bem Até 10:30 em http://x.y"
    )
    assert expression("Valeu 👍🏽 de verdade 😢") == "feliz"
    assert expression("Sem expressão conhecida :P 🚀") is None
    # O rosto do Robot conhece todas as expressões que o núcleo manda.
    assert set(FACES.values()) == set(get_args(Expression))


def test_splitter_cuts_long_run_on_at_comma() -> None:
    text = "a" * 150 + ", " + "b" * 108
    splitter = SentenceSplitter()

    first = splitter.feed(text)

    assert first == ["a" * 150 + ","]
    assert splitter.flush() == ["b" * 108]


# --- pipeline em streaming ---


def test_first_sentence_is_spoken_before_the_run_ends() -> None:
    speech = Speech()
    engine = Engine(
        speech,
        ["Primeira frase completa aqui. Se", WAIT, "gunda frase também completa."],
    )

    result, received = speak(VoicePipeline(engine, speech, speech))

    assert [index for index, _ in received] == [0, 1]
    assert all(wav == CLIP for _, wav in received)
    assert speech.spoken == [
        "Primeira frase completa aqui.",
        "Segunda frase também completa.",
    ]
    assert result.audio_status == "streamed" and result.audio is None


def test_pronunciation_applies_per_sentence() -> None:
    speech = Speech()
    engine = Engine(speech, ["O NeoSon chegou em casa agora.\n"])

    speak(VoicePipeline(engine, speech, speech))

    assert speech.spoken == ["O Néusson chegou em casa agora."]


def test_emoji_is_not_spoken_and_goes_with_its_sentence_as_expression() -> None:
    speech = Speech()
    engine = Engine(
        speech,
        [
            "Que pena que deu errado. 😢 Mas",
            " amanhã a gente tenta de novo. :) Até",
            " logo, tudo vai dar certo! 😊",
        ],
    )
    faces: list[str | None] = []

    speak(VoicePipeline(engine, speech, speech), faces)

    assert speech.spoken == [
        "Que pena que deu errado.",
        "Mas amanhã a gente tenta de novo.",
        "Até logo, tudo vai dar certo!",
    ]
    assert faces == ["triste", None, "feliz"]


def test_engine_without_streaming_falls_back_to_result_text() -> None:
    speech = Speech()
    engine = Engine(
        speech, text="Primeira frase bem completa aqui. Segunda frase também ok."
    )
    engine.pieces = ()

    result, received = speak(VoicePipeline(engine, speech, speech))

    assert speech.spoken == [
        "Primeira frase bem completa aqui.",
        "Segunda frase também ok.",
    ]
    assert len(received) == 2 and result.audio_status == "streamed"


def test_streaming_speaks_preamble_then_approval_text() -> None:
    speech = Speech()
    engine = Engine(
        speech,
        ["Vou criar o evento agora.", "\n"],
        text="Preciso da sua aprovação para criar o evento.",
        status=RunStatus.NEEDS_APPROVAL,
    )

    result, _ = speak(VoicePipeline(engine, speech, speech))

    assert speech.spoken == [
        "Vou criar o evento agora.",
        "Preciso da sua aprovação para criar o evento.",
    ]
    assert result.result.status is RunStatus.NEEDS_APPROVAL


def test_streaming_stops_at_char_and_seconds_budget() -> None:
    sentences = [f"Frase número {n} com bastante conteúdo aqui. " for n in range(3)]
    speech = Speech()
    engine = Engine(speech, sentences)

    result, received = speak(VoicePipeline(engine, speech, speech, max_speech_chars=60))

    assert len(speech.spoken) == 2
    assert sum(len(text) for text in speech.spoken) <= 61
    assert result.result.text == "".join(sentences)

    speech = Speech()
    engine = Engine(speech, sentences)
    limits = VoiceLimits(max_output_seconds=0.25)

    result, received = speak(VoicePipeline(engine, speech, speech, limits))

    assert [index for index, _ in received] == [0, 1]
    assert result.audio_status == "streamed"


def test_synthesis_failure_stops_speech_and_keeps_text() -> None:
    sentences = [f"Frase número {n} com bastante conteúdo aqui. " for n in range(3)]
    speech = Speech(fail_at=2)
    engine = Engine(speech, sentences)

    result, received = speak(VoicePipeline(engine, speech, speech))

    assert [index for index, _ in received] == [0]
    assert result.audio_status == "synthesis_failed"
    assert len(engine.received) == 1 and len(speech.spoken) == 2

    speech = Speech(slow_at=1)
    engine = Engine(speech, sentences)
    limits = VoiceLimits(timeout_seconds=0.3)

    result, received = speak(VoicePipeline(engine, speech, speech, limits))

    assert received == [] and result.audio_status == "synthesis_timeout"


@pytest.mark.parametrize("fail_at", [None, 2])
def test_streaming_usage_counts_spoken_characters(fail_at: int | None) -> None:
    sentences = [f"Frase número {n} com bastante conteúdo aqui. " for n in range(2)]
    usage: list[SpeechUsage] = []

    async def record(run_id: UUID, value: SpeechUsage) -> None:
        usage.append(value)

    speech = Speech(fail_at=fail_at)
    engine = Engine(speech, sentences)
    pipeline = VoicePipeline(
        engine, speech, speech, tts_provider="piper", record_usage=record
    )

    speak(pipeline)

    assert usage[0].tts_characters == sum(len(text) for text in speech.spoken)
    assert usage[0].complete is (fail_at is None)


def test_without_sink_pipeline_is_unchanged() -> None:
    speech = Speech()
    engine = Engine(speech, ["Frase que não vai em streaming. "])

    result = asyncio.run(
        VoicePipeline(engine, speech, speech).process(audio, None, "x")
    )

    assert engine.received[0].on_text is None
    assert result.audio is not None and result.audio_status == "ready"


def test_stalled_sink_cannot_hold_the_voice_slot() -> None:
    sentences = [f"Frase número {n} com bastante conteúdo aqui. " for n in range(2)]
    speech = Speech()
    engine = Engine(speech, sentences)
    pipeline = VoicePipeline(engine, speech, speech, VoiceLimits(timeout_seconds=0.3))

    async def stalled(index: int, wav: bytes, face: str | None) -> None:
        await asyncio.Event().wait()  # cliente que parou de ler o stream

    async def scenario() -> tuple[VoiceResult, VoiceResult]:
        first = await asyncio.wait_for(
            pipeline.process(audio, None, "dono", on_speech=stalled), 2
        )
        return first, await pipeline.process(audio, None, "dono")

    first, second = asyncio.run(scenario())

    assert first.audio_status == "synthesis_timeout"
    assert second.audio is not None
