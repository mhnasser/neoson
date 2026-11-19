import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from neoson_core.engine import EchoEngine, RunRecord, RunStatus
from neoson_core.engine.voice import SpeechUsage, VoicePipeline, pcm_wav
from neoson_core.infrastructure.config import ServerSettings
from neoson_core.infrastructure.db import connect
from neoson_core.infrastructure.speech_usage import (
    SPEECH_USAGE_EVENT,
    SqliteSpeechUsage,
)
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_worker.main import build_handlers
from neoson_worker.worker import Worker

PRIVATE = "NeoSon responde com texto privado fictício"


@pytest.fixture
def recorded_run(tmp_path: Path) -> tuple[Path, UUID]:
    db, identifier = tmp_path / "test.db", uuid4()
    store = SqliteConversationStore(db)
    session = asyncio.run(store.create_session("voice"))
    now = datetime.now(UTC)
    asyncio.run(
        store.record_run(
            RunRecord(identifier, session, RunStatus.COMPLETED, now, now, ())
        )
    )
    return db, identifier


@pytest.mark.parametrize(
    "stt_price,tts_price,complete,expected",
    [
        (0.06, 2, True, 0.0066),
        (None, 2, True, None),
        (0.06, None, True, None),
        (0.06, 0, True, 0.006),
        (0.06, 2, False, None),
    ],
)
def test_costs_are_optional_idempotent_and_exported_without_content(
    recorded_run: tuple[Path, UUID],
    tmp_path: Path,
    stt_price: float | None,
    tts_price: float | None,
    complete: bool,
    expected: float | None,
) -> None:
    db, identifier = recorded_run
    recorder = SqliteSpeechUsage(db, stt_price, tts_price)
    usage = SpeechUsage("openai", "piper", 6, 300, complete)
    asyncio.run(recorder(identifier, usage))
    asyncio.run(recorder(identifier, usage))
    with closing(connect(db)) as connection:
        row = connection.execute("SELECT * FROM speech_usage_records").fetchone()
        assert row["stt_seconds"] == 6 and row["tts_characters"] == 300
        assert row["usage_complete"] == int(complete)
        assert (
            row["cost_usd"] is None
            if expected is None
            else row["cost_usd"] == pytest.approx(expected)
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE type = ?",
                (SPEECH_USAGE_EVENT,),
            ).fetchone()[0]
            == 1
        )
        assert PRIVATE not in repr(dict(row))
    vault = tmp_path / "vault"
    settings = ServerSettings(
        _env_file=None, llm_provider="echo", db_path=db, vault_path=vault
    )
    Worker(db, build_handlers(settings), "speech-test").run_once()
    panel = (vault / "Dashboard/Voz-Custos.md").read_text(encoding="utf-8")
    assert "| openai | piper | 1 | 6.00 | 300 |" in panel
    assert PRIVATE not in panel and "custo desconhecido" in panel


@pytest.mark.parametrize("fails", [False, True])
def test_pipeline_records_submitted_characters_and_marks_failed_synthesis_incomplete(
    fails: bool,
) -> None:
    captured: list[SpeechUsage] = []
    submitted: list[str] = []

    class Speech:
        async def transcribe(self, audio: object) -> str:
            return PRIVATE

        async def synthesize(self, text: str) -> bytes:
            submitted.append(text)
            if fails:
                raise RuntimeError(PRIVATE)
            return pcm_wav(b"\0\0" * 1600)

    async def record(identifier: UUID, usage: SpeechUsage) -> None:
        captured.append(usage)

    async def load() -> bytes:
        return pcm_wav(b"\0\0" * 3200)

    speech = Speech()
    pipeline = VoicePipeline(
        EchoEngine(),
        speech,
        speech,
        max_speech_chars=30,
        stt_provider="openai",
        tts_provider="piper",
        record_usage=record,
    )
    result = asyncio.run(pipeline.process(load, None, "test"))
    assert len(captured) == 1
    assert captured[0].stt_seconds == pytest.approx(0.2)
    assert captured[0].tts_characters == len(submitted[0]) <= 30
    assert captured[0].complete is not fails
    assert PRIVATE not in repr(captured[0])
    assert PRIVATE in result.result.text
    assert result.audio_status == ("synthesis_failed" if fails else "ready")


def test_cancellation_records_incomplete_usage_and_releases_admission() -> None:
    captured: list[SpeechUsage] = []

    async def run() -> None:
        started = asyncio.Event()

        class Speech:
            hang = True

            async def transcribe(self, audio: object) -> str:
                return PRIVATE

            async def synthesize(self, text: str) -> bytes:
                started.set()
                if self.hang:
                    await asyncio.Event().wait()
                return pcm_wav(b"\0\0" * 1600)

        async def record(identifier: UUID, usage: SpeechUsage) -> None:
            captured.append(usage)

        async def load() -> bytes:
            return pcm_wav(b"\0\0" * 1600)

        speech = Speech()
        pipeline = VoicePipeline(EchoEngine(), speech, speech, record_usage=record)
        task = asyncio.create_task(pipeline.process(load, None, "test"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(captured) == 1 and not captured[0].complete
        speech.hang = False
        result = await pipeline.process(load, None, "test")
        assert result.audio is not None and captured[-1].complete

    asyncio.run(run())


def test_unlinked_usage_rolls_back_without_export_event(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    recorder = SqliteSpeechUsage(db, None, None)
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(recorder(uuid4(), SpeechUsage("openai", "piper", 1, 20, True)))
    with closing(connect(db)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM speech_usage_records").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0
        )


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_voice_prices_must_be_finite_and_nonnegative(value: float) -> None:
    with pytest.raises(ValidationError):
        ServerSettings(_env_file=None, voice_stt_price_per_minute_usd=value)
    with pytest.raises(ValidationError):
        ServerSettings(_env_file=None, voice_tts_price_per_million_chars_usd=value)
