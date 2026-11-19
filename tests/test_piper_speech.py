import asyncio
import io
import json
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


class Input:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Output:
    def __init__(self, raw: bytes, delay: float) -> None:
        self.raw, self.delay = raw, delay

    async def read(self, size: int) -> bytes:
        if self.delay:
            await asyncio.sleep(self.delay)
        data, self.raw = self.raw[:size], self.raw[size:]
        return data


class Process:
    def __init__(self, raw: bytes, code: int = 0, delay: float = 0) -> None:
        self.stdin, self.stdout = Input(), Output(raw, delay)
        self.returncode: int | None = None
        self.code, self.killed = code, False

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode

    def kill(self) -> None:
        self.killed, self.returncode = True, -9

    async def communicate(self) -> tuple[bytes, None]:
        return b"", None


@pytest.mark.parametrize("rate", [16000, 22050, 48000])
def test_piper_uses_private_stdin_and_resamples_the_model_rate(
    model: Path,
    monkeypatch: pytest.MonkeyPatch,
    rate: int,
) -> None:
    Path(str(model) + ".json").write_text(json.dumps({"audio": {"sample_rate": rate}}))
    process = Process(b"\x01\x00" * rate)
    seen: list[tuple[Any, ...]] = []

    async def spawn(*args: Any, **kwargs: Any) -> Process:
        seen.append(args)
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert "shell" not in kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    wav = asyncio.run(PiperSpeech(model).synthesize(PRIVATE))
    assert read_wav(wav, VoiceLimits()).seconds == pytest.approx(1)
    with wave.open(io.BytesIO(wav)) as source:
        assert (
            source.getframerate(),
            source.getnchannels(),
            source.getsampwidth(),
        ) == (16000, 1, 2)
    assert PRIVATE not in " ".join(str(arg) for arg in seen[0])
    assert process.stdin.data == (PRIVATE + "\n").encode()
    assert process.stdin.closed and not process.killed


@pytest.mark.parametrize(
    "raw,code",
    [(b"", 0), (b"\x00", 0), (b"\x00\x00", 2), (b"\x00\x00" * 22051, 0)],
    ids=["empty", "odd", "nonzero", "limit"],
)
def test_piper_errors_and_output_limit_do_not_expose_text(
    model: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    code: int,
) -> None:
    process = Process(raw, code)

    async def spawn(*args: Any, **kwargs: Any) -> Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(PiperError) as caught:
        asyncio.run(PiperSpeech(model, max_output_seconds=1).synthesize(PRIVATE))
    assert PRIVATE not in str(caught.value)


def test_timeout_and_cancellation_kill_the_child(
    model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[Process] = []

    async def spawn(*args: Any, **kwargs: Any) -> Process:
        processes.append(Process(b"", delay=10))
        return processes[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        asyncio.run(PiperSpeech(model, timeout_seconds=0.01).synthesize(PRIVATE))

    async def cancel() -> None:
        task = asyncio.create_task(PiperSpeech(model).synthesize(PRIVATE))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert len(processes) == 2 and all(process.killed for process in processes)


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
    assert pipeline.synthesizer.base.command[-2:] == ("-m", "piper")


def test_missing_process_has_a_sanitized_error(
    model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(*args: Any, **kwargs: Any) -> Process:
        raise FileNotFoundError(PRIVATE)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    with pytest.raises(PiperError) as caught:
        asyncio.run(PiperSpeech(model).synthesize(PRIVATE))
    assert PRIVATE not in str(caught.value)


def test_openai_transcription_and_piper_synthesis_work_together(
    model: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        return httpx2.Response(200, json={"text": "Olá, NeoSon"})

    async def spawn(*args: Any, **kwargs: Any) -> Process:
        return Process(b"\x01\x00" * 22050)

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

        try:
            pipeline = VoicePipeline(EchoEngine(), transcriber, PiperSpeech(model))
            result = await pipeline.process(load, None, "test")
            assert result.audio is not None and result.audio.seconds == pytest.approx(1)
            assert result.audio_status == "ready"
        finally:
            await client.close()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    asyncio.run(run())
    assert requests == ["/v1/audio/transcriptions"]
