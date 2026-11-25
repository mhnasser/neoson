import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from neoson_core.engine import EchoEngine
from neoson_core.engine.voice import AudioClip, FallbackTranscriber, pcm_wav
from neoson_core.infrastructure.config import ConfigError, ServerSettings
from neoson_core.providers import whisper_speech
from neoson_core.providers.openai_speech import OpenAISpeech
from neoson_core.providers.piper_speech import PiperSpeech
from neoson_core.providers.whisper_speech import WhisperError, WhisperSpeech
from neoson_gateway.composition import build_transcriber, build_voice

PRIVATE = "texto-ficticio-privado"

# Faz o papel do faster-whisper com o mesmo protocolo. O comportamento vem do número
# de amostras do WAV, já que o texto falado não passa pelo processo.
FAKE_HELPER = """
import io, json, struct, sys, time, wave
out, source, calls = sys.stdout.buffer, sys.stdin.buffer, 0
while header := source.read(4):
    (size,) = struct.unpack("<I", header)
    with wave.open(io.BytesIO(source.read(size))) as clip:
        frames = clip.getnframes()
    calls += 1
    if frames == 3200:
        sys.exit(1)
    if frames == 4800:
        time.sleep(30)
    if frames == 6400:
        out.write(b"nao e json\\n")
        out.flush()
        continue
    reply = {"text": f"ouvi {calls} {sys.argv[1]} {sys.argv[2]}", "cpu": 0.25}
    if frames == 8000:
        reply = {"text": "x" * 8001, "cpu": 0.1}
    if frames == 9600:
        reply = ["lista"]
    if frames == 11200:
        reply = {"text": "sem cpu"}
    out.write(json.dumps(reply).encode() + b"\\n")
    out.flush()
"""


def clip(frames: int = 1600) -> AudioClip:
    return AudioClip(pcm_wav(b"\x01\x00" * frames), frames / 16000)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    monkeypatch.setattr(whisper_speech, "HELPER", FAKE_HELPER)
    calls: list[tuple[Any, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert "shell" not in kwargs
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return calls


def whisper(**options: Any) -> WhisperSpeech:
    return WhisperSpeech("small", python=sys.executable, **options)


def test_one_process_serves_every_clip_and_reports_cpu(
    spawned: list[tuple[Any, ...]],
) -> None:
    speech = whisper()

    async def run() -> list[tuple[str, int]]:
        try:
            results = []
            for _ in range(2):
                text = await speech.transcribe(clip())
                assert speech._process is not None
                results.append((text, speech._process.pid))
            return results
        finally:
            await speech.close()

    (first, first_pid), (second, second_pid) = asyncio.run(run())

    assert (first, second) == ("ouvi 1 small pt", "ouvi 2 small pt")
    assert first_pid == second_pid and len(spawned) == 1
    assert spawned[0][-2:] == ("small", "pt")
    assert speech.cpu_seconds == 0.25 and speech._process is None


@pytest.mark.parametrize("frames", [3200, 6400, 8000, 9600, 11200])
def test_bad_replies_fail_safely_and_the_next_clip_gets_a_new_process(
    spawned: list[tuple[Any, ...]], frames: int
) -> None:
    speech = whisper()

    async def run() -> tuple[str, str]:
        try:
            await speech.transcribe(clip())
            with pytest.raises(WhisperError) as caught:
                await speech.transcribe(clip(frames))
            return str(caught.value), await speech.transcribe(clip())
        finally:
            await speech.close()

    message, after = asyncio.run(run())

    assert "x" * 20 not in message and "lista" not in message
    assert after == "ouvi 1 small pt" and len(spawned) == 2


def test_timeout_and_cancel_kill_the_process(spawned: list[tuple[Any, ...]]) -> None:
    speech = whisper(timeout_seconds=1)

    async def run() -> None:
        try:
            await speech.transcribe(clip())
            stuck = speech._process
            with pytest.raises(TimeoutError):
                await speech.transcribe(clip(4800))
            assert stuck is not None and stuck.returncode is not None
            await speech.transcribe(clip())

            task = asyncio.create_task(speech.transcribe(clip(4800)))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await speech.transcribe(clip()) == "ouvi 1 small pt"
        finally:
            await speech.close()

    asyncio.run(run())

    assert len(spawned) == 3


def test_missing_python_has_a_sanitized_error() -> None:
    speech = WhisperSpeech("small", python="python-que-nao-existe-" + PRIVATE)
    with pytest.raises(WhisperError) as caught:
        asyncio.run(speech.transcribe(clip()))
    assert PRIVATE not in str(caught.value)


@pytest.mark.parametrize(
    "options",
    [
        {"model": ""},
        {"model": " "},
        {"model": "m" * 501},
        {"python": ""},
        {"language": "pt-BR"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 121},
        {"timeout_seconds": float("nan")},
    ],
)
def test_invalid_configuration_is_rejected(options: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {"model": "small", **options}
    model = arguments.pop("model")
    with pytest.raises(WhisperError):
        WhisperSpeech(model, **arguments)


class Scripted:
    def __init__(self, reply: str | Exception) -> None:
        self.reply, self.calls = reply, 0

    async def transcribe(self, audio: AudioClip) -> str:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_fallback_is_used_only_when_the_primary_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backup = Scripted("da reserva")
    healthy = FallbackTranscriber(Scripted("local"), backup)
    assert asyncio.run(healthy.transcribe(clip())) == "local" and backup.calls == 0

    broken = FallbackTranscriber(Scripted(WhisperError(PRIVATE)), backup)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(broken.transcribe(clip())) == "da reserva"
    assert backup.calls == 1 and "WhisperError" in caplog.text
    assert PRIVATE not in caplog.text

    both = FallbackTranscriber(Scripted(WhisperError()), Scripted(RuntimeError()))
    with pytest.raises(RuntimeError):
        asyncio.run(both.transcribe(clip()))


def test_composition_builds_local_stt_without_openai_key(tmp_path: Path) -> None:
    model = tmp_path / "voice.onnx"
    model.touch()
    Path(str(model) + ".json").write_text(json.dumps({"audio": {"sample_rate": 22050}}))
    settings = ServerSettings(
        _env_file=None,
        llm_provider="echo",
        voice_provider="openai",
        voice_stt_provider="whisper",
        whisper_model="medium",
        whisper_python=Path("/opt/whisper/python"),
        voice_tts_provider="piper",
        piper_model=model,
    )
    pipeline = build_voice(settings, EchoEngine())
    assert pipeline is not None and pipeline.stt_provider == "whisper"
    assert isinstance(pipeline.transcriber, WhisperSpeech)
    assert isinstance(pipeline.synthesizer, PiperSpeech)
    assert pipeline.transcriber.model == "medium"
    assert pipeline.transcriber.python == str(Path("/opt/whisper/python"))

    fallback = settings.model_copy(update={"voice_stt_fallback_openai": True})
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        build_transcriber(fallback)
    keyed = fallback.model_copy(update={"openai_api_key": SecretStr("chave-teste")})
    transcriber = build_transcriber(keyed)
    assert isinstance(transcriber, FallbackTranscriber)
    assert isinstance(transcriber.primary, WhisperSpeech)
    assert isinstance(transcriber.fallback, OpenAISpeech)

    with pytest.raises(ConfigError, match="NEOSON_WHISPER_MODEL"):
        build_transcriber(settings.model_copy(update={"whisper_model": " "}))
