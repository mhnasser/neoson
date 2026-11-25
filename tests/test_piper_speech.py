import asyncio
import io
import json
import os
import sys
import wave
from pathlib import Path
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from neoson_core.engine import EchoEngine
from neoson_core.engine.voice import VoiceLimits, VoicePipeline, pcm_wav, read_wav
from neoson_core.infrastructure.config import ServerSettings
from neoson_core.providers import piper_speech
from neoson_core.providers.openai_speech import OpenAISpeech
from neoson_core.providers.piper_speech import PiperError, PiperSpeech
from neoson_core.providers.speech_effects import EffectSynthesizer
from neoson_gateway.composition import build_voice

PRIVATE = "texto-ficticio-privado"


@pytest.fixture
def model(tmp_path: Path) -> Path:
    path = tmp_path / "voice.onnx"
    path.touch()
    Path(str(path) + ".json").write_text(json.dumps({"audio": {"sample_rate": 22050}}))
    return path


# Faz o papel do Piper: mesmo protocolo (linha JSON → tamanho + PCM), 1 s de áudio.
FAKE_HELPER = """
import json, struct, sys, time
rate = json.load(open(sys.argv[1] + ".json"))["audio"]["sample_rate"]
out = sys.stdout.buffer
for line in sys.stdin.buffer:
    text = json.loads(line)["text"]
    if text.startswith("__morre__"):
        sys.exit(1)
    if text.startswith("__trava__"):
        time.sleep(30)
    if text.startswith("__enorme__"):
        out.write(struct.pack("<I", 10**8))
        out.flush()
        continue
    pcm = b"" if text.startswith("__vazio__") else b"\\x01\\x00" * rate
    if text.startswith("__impar__"):
        pcm = b"\\x00" * 3
    out.write(struct.pack("<I", len(pcm)) + pcm)
    out.flush()
"""


@pytest.fixture
def fake_piper(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    monkeypatch.setattr(piper_speech, "HELPER", FAKE_HELPER)
    spawned: list[tuple[Any, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        spawned.append(args)
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert "shell" not in kwargs
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return spawned


def piper(model: Path, **options: Any) -> PiperSpeech:
    return PiperSpeech(model, python=sys.executable, **options)


def pid(speech: PiperSpeech) -> int:
    assert speech._process is not None
    return speech._process.pid


@pytest.mark.parametrize("rate", [16000, 22050, 48000])
def test_one_helper_serves_every_sentence_and_audio_is_resampled(
    model: Path, fake_piper: list[tuple[Any, ...]], rate: int
) -> None:
    Path(str(model) + ".json").write_text(json.dumps({"audio": {"sample_rate": rate}}))
    speech = piper(model)

    async def run() -> list[tuple[bytes, int]]:
        try:
            return [(await speech.synthesize(PRIVATE), pid(speech)) for _ in range(2)]
        finally:
            await speech.close()

    (first, first_pid), (second, second_pid) = asyncio.run(run())

    assert first_pid == second_pid and len(fake_piper) == 1
    for wav in (first, second):
        assert read_wav(wav, VoiceLimits()).seconds == pytest.approx(1)
        with wave.open(io.BytesIO(wav)) as source:
            assert (
                source.getframerate(),
                source.getnchannels(),
                source.getsampwidth(),
            ) == (16000, 1, 2)
    assert PRIVATE not in " ".join(str(arg) for arg in fake_piper[0])
    assert fake_piper[0][-2:] == (str(model), "1")


@pytest.mark.parametrize("bad", ["__vazio__", "__impar__", "__enorme__", "__morre__"])
def test_bad_frames_fail_safely_and_the_next_sentence_gets_a_new_helper(
    model: Path, fake_piper: list[tuple[Any, ...]], bad: str
) -> None:
    speech = piper(model)

    async def run() -> tuple[str, bytes]:
        try:
            await speech.synthesize("aquecer")
            with pytest.raises(PiperError) as caught:
                await speech.synthesize(bad + " " + PRIVATE)
            return str(caught.value), await speech.synthesize("depois")
        finally:
            await speech.close()

    message, after = asyncio.run(run())

    assert PRIVATE not in message
    assert read_wav(after, VoiceLimits()).seconds == pytest.approx(1)
    assert len(fake_piper) == 2


def test_helper_is_recreated_after_crash_timeout_and_cancel(
    model: Path, fake_piper: list[tuple[Any, ...]]
) -> None:
    speech = piper(model, timeout_seconds=1)

    async def run() -> None:
        try:
            await speech.synthesize("aquecer")
            stuck = speech._process
            with pytest.raises(TimeoutError):
                await speech.synthesize("__trava__")
            assert stuck is not None and stuck.returncode is not None
            await speech.synthesize("depois do prazo")

            task = asyncio.create_task(speech.synthesize("__trava__"))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await speech.synthesize("depois do cancelamento")
        finally:
            await speech.close()

    asyncio.run(run())

    assert len(fake_piper) == 3


@pytest.mark.parametrize("rate", [None, "22050", True, 100000])
def test_invalid_model_configuration_is_sanitized(model: Path, rate: object) -> None:
    Path(str(model) + ".json").write_text(json.dumps({"audio": {"sample_rate": rate}}))
    with pytest.raises(PiperError, match="configuração inválidos"):
        PiperSpeech(model)


def test_composition_combines_openai_stt_with_piper_and_the_chosen_effect(
    model: Path,
) -> None:
    settings = ServerSettings(
        _env_file=None,
        llm_provider="echo",
        voice_provider="openai",
        openai_api_key=SecretStr("key-for-isolated-test"),
        voice_tts_provider="piper",
        piper_model=model,
        piper_python=Path("/opt/piper/python"),
        voice_effect="squirrel_robot",
    )
    pipeline = build_voice(settings, EchoEngine())
    assert pipeline is not None
    assert isinstance(pipeline.transcriber, OpenAISpeech)
    assert isinstance(pipeline.synthesizer, EffectSynthesizer)
    assert pipeline.synthesizer.factor == 1.45
    assert isinstance(pipeline.synthesizer.base, PiperSpeech)
    assert pipeline.synthesizer.base.scale == 1.45
    assert pipeline.synthesizer.base.python == str(Path("/opt/piper/python"))
    unset = settings.model_copy(update={"piper_python": None})
    default = build_voice(unset, EchoEngine())
    assert default is not None and isinstance(default.synthesizer, EffectSynthesizer)
    assert isinstance(default.synthesizer.base, PiperSpeech)
    assert default.synthesizer.base.python == "python3"


def test_missing_process_has_a_sanitized_error(model: Path) -> None:
    speech = PiperSpeech(model, python="python-que-nao-existe-" + PRIVATE)
    with pytest.raises(PiperError) as caught:
        asyncio.run(speech.synthesize(PRIVATE))
    assert PRIVATE not in str(caught.value)


def test_openai_transcription_and_piper_synthesis_work_together(
    model: Path, fake_piper: list[tuple[Any, ...]]
) -> None:
    requests: list[str] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        return httpx2.Response(200, json={"text": "Olá, NeoSon"})

    async def run() -> None:
        client = AsyncOpenAI(
            api_key="isolated-test",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(reply)),
        )
        transcriber = OpenAISpeech(
            client, stt_model="test-stt", tts_model="unused", voice="unused"
        )

        async def load() -> bytes:
            return pcm_wav(b"\x00\x00" * 3200)

        speech = piper(model)
        try:
            pipeline = VoicePipeline(EchoEngine(), transcriber, speech)
            result = await pipeline.process(load, None, "test")
            assert result.audio is not None and result.audio.seconds == pytest.approx(1)
            assert result.audio_status == "ready"
        finally:
            await client.close()
            await speech.close()

    asyncio.run(run())
    assert requests == ["/v1/audio/transcriptions"]


@pytest.mark.skipif(
    not os.environ.get("NEOSON_TEST_PIPER_PYTHON")
    or not os.environ.get("NEOSON_TEST_PIPER_MODEL"),
    reason="Piper real só na máquina com o Python do Piper e um modelo.",
)
def test_real_piper_helper_when_available() -> None:
    speech = PiperSpeech(
        Path(os.environ["NEOSON_TEST_PIPER_MODEL"]),
        python=os.environ["NEOSON_TEST_PIPER_PYTHON"],
        timeout_seconds=60,
    )

    async def run() -> list[float]:
        try:
            return [
                read_wav(await speech.synthesize(text), VoiceLimits()).seconds
                for text in ("Olá, tudo bem?", "Amanhã vai fazer sol.")
            ]
        finally:
            await speech.close()

    assert all(seconds > 0.3 for seconds in asyncio.run(run()))
