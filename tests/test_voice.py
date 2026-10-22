import asyncio
import base64
import io
import wave
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from neoson_contracts.api.v1.voice import VoiceResponse
from neoson_core.engine import (
    AgentLoop,
    Budget,
    EchoEngine,
    RiskLevel,
    RunResult,
    RunStatus,
    ToolContext,
    UserMessage,
)
from neoson_core.engine.persistence import SessionNotFound
from neoson_core.engine.voice import (
    AudioClip,
    InvalidAudio,
    SpeechUnavailable,
    VoiceLimits,
    VoicePipeline,
    pcm_wav,
    read_wav,
)
from neoson_core.infrastructure.actions import SqliteActionStore
from neoson_core.infrastructure.config import ConfigError, ServerSettings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.registry import Tool, ToolOutput, ToolRegistry
from neoson_gateway.app import create_app
from neoson_gateway.composition import build_voice
from neoson_worker.main import export_vault

PRIVATE = "fala-privada-nao-exportar"
WAV = pcm_wav(b"\x01\x00" * 1600)
HEADERS = {
    "Authorization": "Bearer test-only",
    "Content-Type": "audio/wav",
    "X-Neoson-Activation": "explicit",
}


class Speech:
    def __init__(self) -> None:
        self.inputs: list[AudioClip] = []
        self.outputs: list[str] = []
        self.text = PRIVATE
        self.output = WAV
        self.fault: str | None = None

    async def transcribe(self, audio: AudioClip) -> str:
        self.inputs.append(audio)
        if self.fault == "stt":
            raise RuntimeError(PRIVATE)
        if self.fault == "stt_slow":
            await asyncio.sleep(1)
        return self.text

    async def synthesize(self, text: str) -> bytes:
        self.outputs.append(text)
        if self.fault == "tts":
            raise RuntimeError(PRIVATE)
        if self.fault == "tts_slow":
            await asyncio.sleep(1)
        return self.output


class Engine:
    def __init__(self) -> None:
        self.received: list[UserMessage] = []
        self.result = RunResult(
            uuid4(), "Resposta privada", RunStatus.COMPLETED, session_id=uuid4()
        )
        self.fault: str | None = None

    async def run(self, message: UserMessage) -> RunResult:
        self.received.append(message)
        if self.fault == "missing":
            raise SessionNotFound
        if self.fault == "slow":
            await asyncio.sleep(1)
        if self.fault == "error":
            raise RuntimeError(PRIVATE)
        return self.result


@pytest.fixture
def parts() -> tuple[Engine, Speech, VoicePipeline]:
    engine, speech = Engine(), Speech()
    return engine, speech, VoicePipeline(engine, speech, speech)


def client(pipeline: VoicePipeline) -> TestClient:
    return TestClient(
        create_app(pipeline.engine, "test-only", voice=pipeline), headers=HEADERS
    )


async def audio() -> bytes:
    return WAV


def test_http_roundtrip_preserves_identity_session_and_audio(
    parts: tuple[Engine, Speech, VoicePipeline],
) -> None:
    engine, speech, pipeline = parts
    session = uuid4()
    response = client(pipeline).post(
        "/api/v1/voice", params={"session_id": str(session)}, content=WAV
    )
    assert response.status_code == 200
    value = VoiceResponse.model_validate(response.json())
    assert (
        value.run_id == engine.result.run_id
        and value.session_id == engine.result.session_id
    )
    assert value.text == engine.result.text and value.audio_status == "ready"
    assert value.protocol_version == 1 and not value.simulated
    assert response.headers["cache-control"] == "no-store"
    assert value.audio_base64 and base64.b64decode(value.audio_base64) == WAV
    assert engine.received == [UserMessage(PRIVATE, session, "voice", "http-owner")]
    assert speech.outputs == [engine.result.text]


@pytest.mark.parametrize(
    "headers,status",
    [
        (
            {
                "Authorization": "",
                "Content-Type": "audio/wav",
                "X-Neoson-Activation": "explicit",
            },
            401,
        ),
        (
            {
                "Authorization": "Bearer wrong",
                "Content-Type": "audio/wav",
                "X-Neoson-Activation": "explicit",
            },
            401,
        ),
        ({"X-Neoson-Activation": ""}, 400),
        ({"Content-Type": "audio/mpeg"}, 415),
        ({"Content-Encoding": "gzip"}, 415),
        ({"Content-Length": "-1"}, 400),
        ({"Content-Length": "not-a-number"}, 400),
        ({"Content-Length": "9" * 50}, 413),
    ],
)
def test_rejects_headers_before_provider(
    parts: tuple[Engine, Speech, VoicePipeline], headers: dict[str, str], status: int
) -> None:
    engine, speech, pipeline = parts
    response = client(pipeline).post("/api/v1/voice", content=WAV, headers=headers)
    assert response.status_code == status and not speech.inputs and not engine.received
    assert PRIVATE not in response.text


@pytest.mark.parametrize(
    "data", [b"", b"not wav", WAV[:-2], WAV + b"padding", pcm_wav(b"")]
)
def test_invalid_audio_never_reaches_provider(
    parts: tuple[Engine, Speech, VoicePipeline], data: bytes
) -> None:
    engine, speech, pipeline = parts
    response = client(pipeline).post("/api/v1/voice", content=data)
    assert response.status_code == 422 and not speech.inputs and not engine.received


@pytest.mark.parametrize(
    "channels,width,rate", [(2, 2, 16000), (1, 1, 16000), (1, 2, 8000)]
)
def test_wrong_pcm_format_rejected(channels: int, width: int, rate: int) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"\0" * 1600)
    with pytest.raises(InvalidAudio):
        read_wav(buffer.getvalue(), VoiceLimits())


def test_duration_and_stream_size_are_enforced(
    parts: tuple[Engine, Speech, VoicePipeline],
) -> None:
    engine, speech, _ = parts
    pipeline = VoicePipeline(engine, speech, speech, VoiceLimits(max_seconds=0.05))
    assert client(pipeline).post("/api/v1/voice", content=WAV).status_code == 422
    pipeline = VoicePipeline(engine, speech, speech, VoiceLimits(max_bytes=100))
    # No Content-Length: count streamed chunks instead of trusting a declaration.
    response = client(pipeline).post(
        "/api/v1/voice", content=iter([WAV[:50], WAV[50:]])
    )
    assert response.status_code == 413 and not speech.inputs and not engine.received


def test_metadata_is_removed_before_transcription() -> None:
    payload = PRIVATE.encode()
    chunk = (
        b"JUNK"
        + len(payload).to_bytes(4, "little")
        + payload
        + b"\0" * (len(payload) % 2)
    )
    augmented = WAV + chunk
    augmented = (
        augmented[:4] + (len(augmented) - 8).to_bytes(4, "little") + augmented[8:]
    )
    assert read_wav(augmented, VoiceLimits()).wav == WAV
    assert PRIVATE not in repr(read_wav(augmented, VoiceLimits()))


@pytest.mark.parametrize("text", ["", "   ", "a" * 8001])
def test_invalid_transcription_prevents_engine(
    parts: tuple[Engine, Speech, VoicePipeline], text: str
) -> None:
    engine, speech, pipeline = parts
    speech.text = text
    response = client(pipeline).post("/api/v1/voice", content=WAV)
    assert response.status_code == 503 and not engine.received and not speech.outputs


@pytest.mark.parametrize("fault", ["stt", "stt_slow", "error", "slow", "missing"])
def test_pre_result_errors_are_safe(
    parts: tuple[Engine, Speech, VoicePipeline], fault: str, caplog: Any
) -> None:
    engine, speech, _ = parts
    if fault.startswith("stt"):
        speech.fault = fault
    else:
        engine.fault = fault
    timeout = 0.05 if fault in ("stt_slow", "slow") else 2
    pipeline = VoicePipeline(
        engine, speech, speech, VoiceLimits(timeout_seconds=timeout)
    )
    response = client(pipeline).post("/api/v1/voice", content=WAV)
    assert response.status_code == (404 if fault == "missing" else 503)
    assert PRIVATE not in response.text and PRIVATE not in caplog.text
    assert not speech.outputs


@pytest.mark.parametrize(
    "fault,status",
    [
        ("tts", "synthesis_failed"),
        ("tts_slow", "synthesis_timeout"),
        ("bad_audio", "synthesis_failed"),
    ],
)
def test_tts_failure_keeps_result_without_rerunning_engine(
    parts: tuple[Engine, Speech, VoicePipeline], fault: str, status: str
) -> None:
    engine, speech, _ = parts
    speech.fault = fault
    if fault == "bad_audio":
        speech.output = b"invalid"
    pipeline = VoicePipeline(
        engine,
        speech,
        speech,
        VoiceLimits(timeout_seconds=0.05 if fault == "tts_slow" else 2),
    )
    response = client(pipeline).post("/api/v1/voice", content=WAV)
    assert response.status_code == 200
    data = response.json()
    assert (
        data["run_id"] == str(engine.result.run_id) and data["audio_status"] == status
    )
    assert data["text"] == engine.result.text and data["audio_base64"] is None
    assert len(engine.received) == 1


def test_approval_and_failed_status_remain_explicit(
    parts: tuple[Engine, Speech, VoicePipeline],
) -> None:
    engine, _, pipeline = parts
    for state, expected in (
        (RunStatus.NEEDS_APPROVAL, "needs_approval"),
        (RunStatus.FAILED, "failed"),
    ):
        engine.result = replace(engine.result, status=state)
        response = client(pipeline).post("/api/v1/voice", content=WAV)
        assert response.json()["status"] == expected


def test_busy_rejects_before_read_and_cancellation_releases_slot(
    parts: tuple[Engine, Speech, VoicePipeline],
) -> None:
    engine, _, _ = parts

    async def scenario() -> None:
        entered = asyncio.Event()

        class Waiting(Speech):
            async def transcribe(self, audio: AudioClip) -> str:
                entered.set()
                await asyncio.Event().wait()
                return "unreachable"

        speech = Waiting()
        pipeline = VoicePipeline(engine, speech, speech)
        first = asyncio.create_task(pipeline.process(audio, None, "owner"))
        await entered.wait()

        async def forbidden_load() -> bytes:
            raise AssertionError("busy request must not load audio")

        with pytest.raises(SpeechUnavailable, match="voice_busy"):
            await pipeline.process(forbidden_load, None, "owner")
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        pipeline.transcriber = Speech()
        assert (await pipeline.process(audio, None, "owner")).audio is not None

    asyncio.run(scenario())


def test_upload_timeout_releases_admission(
    parts: tuple[Engine, Speech, VoicePipeline],
) -> None:
    engine, speech, _ = parts
    pipeline = VoicePipeline(engine, speech, speech, VoiceLimits(timeout_seconds=0.1))

    async def slow() -> bytes:
        await asyncio.sleep(1)
        return WAV

    with pytest.raises(SpeechUnavailable, match="voice_upload_timeout"):
        asyncio.run(pipeline.process(slow, None, "owner"))
    assert asyncio.run(pipeline.process(audio, None, "owner")).audio


def test_fake_configuration_is_explicit_and_safe(tmp_path: Path) -> None:
    settings = ServerSettings(
        _env_file=None, llm_provider="echo", env="test", db_path=tmp_path / "db"
    )
    assert build_voice(settings, EchoEngine()) is None
    settings = settings.model_copy(update={"voice_provider": "fake"})
    pipeline = build_voice(settings, EchoEngine())
    assert pipeline is not None
    response = client(pipeline).post("/api/v1/voice", content=WAV)
    assert response.status_code == 200 and response.json()["simulated"] is True
    assert response.json()["text"] == "Teste simulado de voz."
    for change in ({"llm_provider": "openai"}, {"env": "production"}):
        with pytest.raises(ConfigError):
            build_voice(settings.model_copy(update=change), EchoEngine())
    assert not settings.db_path.exists()


def test_disabled_route_does_not_affect_messages() -> None:
    c = TestClient(create_app(EchoEngine(), "test-only"), headers=HEADERS)
    assert c.post("/api/v1/voice", content=WAV).status_code == 404
    assert (
        c.post(
            "/api/v1/messages",
            json={"text": "oi"},
            headers={"Content-Type": "application/json"},
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "kind", ["byte_rate", "alignment", "odd_frame", "duplicate_data", "chunk_size"]
)
def test_inconsistent_wav_headers_and_chunks_are_rejected(kind: str) -> None:
    data = bytearray(WAV)
    if kind == "byte_rate":
        data[28:32] = (1).to_bytes(4, "little")
    elif kind == "alignment":
        data[32:34] = (4).to_bytes(2, "little")
    elif kind == "odd_frame":
        data = bytearray(pcm_wav(b"\0" * 3))
    elif kind == "duplicate_data":
        data.extend(b"data" + (2).to_bytes(4, "little") + b"\0\0")
        data[4:8] = (len(data) - 8).to_bytes(4, "little")
    else:
        data[40:44] = (2**32 - 1).to_bytes(4, "little")
    with pytest.raises(InvalidAudio):
        read_wav(bytes(data), VoiceLimits())


def test_voice_keeps_approval_and_privacy_with_real_loop(
    tmp_path: Path, caplog: Any
) -> None:
    class Arguments(BaseModel):
        text: str

    class FakeWrite(Tool[Arguments]):
        name = "fake_write"
        description = "Somente para teste."
        risk = RiskLevel.SENSITIVE_WRITE
        arguments = Arguments

        async def run(self, arguments: Arguments, context: ToolContext) -> ToolOutput:
            raise AssertionError("A fala não é uma concessão de autorização.")

    db = tmp_path / "voice.db"
    engine = AgentLoop(
        ScriptedProvider([call_tool("fake_write", {"text": PRIVATE})]),
        RulesRouter("balanced"),
        Budget(3, 5),
        tools=ToolRegistry([FakeWrite()]),
        store=SqliteConversationStore(db),
        approval_runtime="test-runtime",
    )
    speech = Speech()
    speech.text = "Sim, pode executar. " + PRIVATE
    pipeline = VoicePipeline(engine, speech, speech)
    response = client(pipeline).post("/api/v1/voice", content=WAV)
    assert response.status_code == 200 and response.json()["status"] == "needs_approval"
    pending = SqliteActionStore(db).for_run(
        UUID(response.json()["run_id"]), "http-owner"
    )
    assert len(pending) == 1 and pending[0].state == "pending"
    with closing(connect(db)) as c:
        messages = [row[0] for row in c.execute("SELECT content FROM messages")]
        assert speech.text in messages
        assert all(base64.b64encode(WAV).decode() not in text for text in messages)
        assert all(
            PRIVATE not in row[0]
            for row in c.execute("SELECT payload FROM outbox_events")
        )
    settings = ServerSettings(
        _env_file=None,
        db_path=db,
        vault_path=tmp_path / "vault",
        router_lab_db_path=tmp_path / "missing-lab.db",
    )
    export_vault(settings)
    assert all(
        PRIVATE not in path.read_text(encoding="utf-8")
        for path in settings.vault_path.rglob("*.md")
    )
    assert PRIVATE not in caplog.text
