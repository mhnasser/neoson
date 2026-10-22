import asyncio
import io
import json
import math
import os
import wave
from array import array
from pathlib import Path
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from neoson_core.engine import EchoEngine
from neoson_core.engine.voice import VoiceLimits, VoicePipeline, pcm_wav, read_wav
from neoson_core.infrastructure.config import ConfigError, ServerSettings
from neoson_core.providers.openai_speech import (
    MAX_TTS_BYTES,
    OpenAISpeech,
    SpeechError,
    resample_24k_to_16k,
    speakable,
)
from neoson_gateway.composition import build_voice

PRIVATE = "conteudo-privado-da-fala"


def tone(samples: int, rate: int = 24000) -> bytes:
    pcm = array(
        "h",
        (int(8000 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(samples)),
    )
    return pcm.tobytes()


class Recorder:
    def __init__(self, status: int = 200, speech: bytes | None = None) -> None:
        self.status = status
        self.speech = tone(24000) if speech is None else speech
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": PRIVATE}})
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx2.Response(200, json={"text": f"  {PRIVATE}  "})
        return httpx2.Response(200, content=self.speech)


def speech(recorder: Recorder, **options: Any) -> OpenAISpeech:
    client = AsyncOpenAI(
        api_key="chave-de-teste",
        base_url="https://api.test/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
        max_retries=0,
    )
    values: dict[str, Any] = {
        "stt_model": "stt-x",
        "tts_model": "tts-x",
        "voice": "voz-x",
        **options,
    }
    return OpenAISpeech(client, **values)


def test_transcription_sends_wav_model_and_language_only() -> None:
    recorder = Recorder()
    clip = read_wav(pcm_wav(b"\x00" * 6400), VoiceLimits())
    text = asyncio.run(speech(recorder).transcribe(clip))
    assert text == PRIVATE
    (request,) = recorder.requests
    body = request.content
    assert request.url.path.endswith("/audio/transcriptions")
    assert b"stt-x" in body and b'name="language"' in body and b"RIFF" in body


def test_synthesis_converts_pcm_24k_to_wav_16k_accepted_by_the_pipeline() -> None:
    recorder = Recorder(speech=tone(24000))  # 1 s
    wav = asyncio.run(speech(recorder).synthesize("Olá."))
    clip = read_wav(wav, VoiceLimits(), VoiceLimits().max_output_seconds)
    assert clip.seconds == pytest.approx(1.0, abs=0.001)
    with wave.open(io.BytesIO(wav)) as source:
        assert (source.getframerate(), source.getnchannels()) == (16000, 1)
    sent = json.loads(recorder.requests[0].content)
    assert sent == {
        "input": "Olá.",
        "model": "tts-x",
        "voice": "voz-x",
        "response_format": "pcm",
    }


def test_resampling_keeps_shape_and_odd_tails() -> None:
    source = array("h", [0, 30, 60, 90, 120, 150, 180])
    output = array("h")
    output.frombytes(resample_24k_to_16k(source.tobytes()))
    assert list(output) == [0, 45, 90, 135, 180]
    assert resample_24k_to_16k(b"") == b""


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("Curto.", 400, "Curto."),
        ("  Muitos   espaços.  ", 400, "Muitos espaços."),
        ("Primeira frase. Segunda frase bem mais longa.", 20, "Primeira frase."),
        ("Versão 3.5 ok. Fim da frase longa aqui.", 15, "Versão 3.5 ok."),
        ("palavras sem pontuação nenhuma aqui", 20, "palavras sem…"),
    ],
)
def test_speech_is_cut_at_a_sentence_end(text: str, limit: int, expected: str) -> None:
    assert speakable(text, limit) == expected


@pytest.mark.parametrize(
    ("recorder", "match"),
    [
        (Recorder(status=500), "InternalServerError"),
        (Recorder(status=401), "AuthenticationError"),
        (Recorder(speech=b""), "inválido"),
        (Recorder(speech=b"\x00" * 3), "inválido"),
        (Recorder(speech=b"\x00" * (MAX_TTS_BYTES + 2)), "longo"),
    ],
)
def test_provider_failures_never_carry_private_text(
    recorder: Recorder, match: str
) -> None:
    provider = speech(recorder)
    with pytest.raises(SpeechError, match=match) as error:
        asyncio.run(provider.synthesize(PRIVATE))
    assert PRIVATE not in str(error.value)


def test_pipeline_degrades_when_synthesis_fails_but_keeps_the_answer() -> None:
    failing = speech(Recorder(status=500))

    class Hears:
        async def transcribe(self, audio: Any) -> str:
            return "Oi"

    pipeline = VoicePipeline(EchoEngine(), Hears(), failing)

    async def load() -> bytes:
        return pcm_wav(b"\x00" * 6400)

    result = asyncio.run(pipeline.process(load, None, "dono"))
    assert result.audio is None and result.audio_status == "synthesis_failed"
    assert result.result.text


def test_openai_voice_is_explicit_and_needs_the_key(tmp_path: Path) -> None:
    settings = ServerSettings(
        _env_file=None,
        llm_provider="echo",
        env="test",
        db_path=tmp_path / "db",
        voice_provider="openai",
    )
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        build_voice(settings, EchoEngine())
    keyed = settings.model_copy(update={"openai_api_key": SecretStr("chave-ficticia")})
    pipeline = build_voice(keyed, EchoEngine())
    assert pipeline is not None and pipeline.simulated is False
    assert isinstance(pipeline.transcriber, OpenAISpeech)
    assert pipeline.limits.max_output_seconds == 30
    assert (
        build_voice(
            settings.model_copy(update={"voice_provider": "disabled"}), EchoEngine()
        )
        is None
    )


@pytest.mark.skipif(
    os.environ.get("NEOSON_LIVE_TESTS") != "1",
    reason="defina NEOSON_LIVE_TESTS=1 para chamar a API real",
)
def test_live_speech_round_trip() -> None:
    from neoson_core.infrastructure.config import load_settings

    settings = load_settings()
    if settings.openai_api_key is None:
        pytest.skip("OPENAI_API_KEY não configurada")
    provider = OpenAISpeech.from_api_key(
        settings.openai_api_key.get_secret_value(),
        timeout_seconds=60,
        stt_model=settings.voice_stt_model,
        tts_model=settings.voice_tts_model,
        voice=settings.voice_tts_voice,
    )

    async def round_trip() -> tuple[float, str]:
        wav = await provider.synthesize("Bom dia. A reunião do projeto Atlas é amanhã.")
        clip = read_wav(wav, VoiceLimits(), VoiceLimits().max_output_seconds)
        return clip.seconds, await provider.transcribe(clip)

    seconds, text = asyncio.run(round_trip())
    assert 0.5 < seconds <= 30
    assert "atlas" in text.casefold() and "reunião" in text.casefold()


@pytest.mark.skipif(
    os.environ.get("NEOSON_LIVE_TESTS") != "1",
    reason="defina NEOSON_LIVE_TESTS=1 para chamar a API real",
)
def test_live_voice_route_with_real_speech(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from neoson_core.infrastructure.config import load_settings
    from neoson_gateway.app import create_app

    settings = load_settings()
    key = settings.openai_api_key
    if key is None:
        pytest.skip("OPENAI_API_KEY não configurada")
    settings = settings.model_copy(
        update={"voice_provider": "openai", "db_path": tmp_path / "db"}
    )
    pipeline = build_voice(settings, EchoEngine())
    assert pipeline is not None
    # Outro cliente: o do pipeline não pode ser usado em dois loops de eventos.
    voice = OpenAISpeech.from_api_key(
        key.get_secret_value(),
        timeout_seconds=60,
        stt_model=settings.voice_stt_model,
        tts_model=settings.voice_tts_model,
        voice=settings.voice_tts_voice,
    )
    question = asyncio.run(voice.synthesize("Qual é a capital do Brasil?"))
    client = TestClient(create_app(EchoEngine(), "token-de-teste", voice=pipeline))
    response = client.post(
        "/api/v1/voice",
        content=question,
        headers={
            "Authorization": "Bearer token-de-teste",
            "Content-Type": "audio/wav",
            "X-Neoson-Activation": "explicit",
        },
    )
    body = response.json()
    assert response.status_code == 200, body
    assert body["simulated"] is False
    assert "capital" in body["text"].casefold()
    assert body["audio_status"] == "ready" and body["audio_seconds"] > 0.5
