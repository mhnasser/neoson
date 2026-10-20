import asyncio
import json
import os
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMError,
    LLMRequest,
    LLMResponse,
    OutboxEvent,
    Usage,
    UserMessage,
)
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.config import ServerSettings, load_settings
from neoson_core.infrastructure.db import connect, transaction
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.curation import CurationLimits, Curator, Scheduler
from neoson_core.skills.loader import SkillPackage, load_skill
from neoson_core.skills.proposals import SqliteProposalStore
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.telemetry.pricing import Price
from neoson_worker.curation_main import build_curator
from neoson_worker.main import build_handlers
from neoson_worker.worker import Worker

LIBRARY = Path(__file__).resolve().parents[1] / "skill_library"
NOW = datetime(2026, 10, 20, 15, tzinfo=UTC)


def seed(db: Path, *, channel: str = "http") -> SkillPackage:
    package = load_skill(LIBRARY / "planejar_dia")
    registry = SqliteSkillRegistry(db)
    registry.approve(package)
    loop = AgentLoop(
        ScriptedProvider(
            [
                call_tool(
                    "skill_load", {"skill_id": "planejar_dia", "version": "1.1.0"}
                ),
                LLMError("falha simulada", retryable=False),
            ]
        ),
        RulesRouter("balanced"),
        Budget(5, 5),
        skills=registry,
        store=SqliteConversationStore(db),
    )
    asyncio.run(loop.run(UserMessage("dado-privado-nao-propagar", channel=channel)))
    settings = ServerSettings(
        _env_file=None, db_path=db, curation_enabled=True, metrics_export_enabled=False
    )
    Worker(db, build_handlers(settings), "test-worker").run_once()
    return package


def answer(base: SkillPackage) -> LLMResponse:
    return reply(
        json.dumps(
            {
                "instructions": base.instructions
                + "\nConfira as datas antes de ordenar.\n"
            }
        ),
        Usage(40, 12, 0),
    )


def rows(db: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(connect(db)) as c:
        return [tuple(r) for r in c.execute(sql)]


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, SkillPackage]:
    db = tmp_path / "neoson.db"
    return db, seed(db)


def test_worker_only_schedules_and_provider_runs_without_write_lock(
    prepared: tuple[Path, SkillPackage],
) -> None:
    db, base = prepared
    assert rows(db, "SELECT status FROM curation_jobs") == [("pending",)]
    assert SqliteProposalStore(db).list() == []

    class Provider:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            with closing(connect(db)) as c, transaction(c):
                c.execute("UPDATE sessions SET updated_at=updated_at")
            assert not request.tools and request.max_output_tokens == 3000
            assert "não confiáveis" in request.messages[0].content
            assert "dado-privado-nao-propagar" not in request.messages[1].content
            return answer(base)

    curator = Curator(db, Provider())
    assert asyncio.run(curator.run_once())
    assert not asyncio.run(curator.run_once())
    proposals = SqliteProposalStore(db).list()
    assert len(proposals) == 1 and proposals[0].candidate.metadata.version == "1.2.0"
    assert rows(db, "SELECT status FROM curation_jobs") == [("completed",)]
    assert rows(
        db, "SELECT input_tokens,output_tokens,cost_usd FROM curation_attempts"
    ) == [(40, 12, None)]
    assert asyncio.run(SqliteSkillRegistry(db).index()) == [base.skill().ref]


@pytest.mark.parametrize(
    "invalid", ["nao-json", '{"instructions": 3}', '{"instructions":"invalido"}']
)
def test_invalid_output_never_creates_proposal(
    prepared: tuple[Path, SkillPackage], invalid: str
) -> None:
    db, _ = prepared
    asyncio.run(Curator(db, ScriptedProvider([reply(invalid)])).run_once())
    assert rows(db, "SELECT status,last_error FROM curation_jobs") == [
        ("failed", "invalid_candidate")
    ]
    assert SqliteProposalStore(db).list() == []


def test_retry_and_limits_survive_restart(prepared: tuple[Path, SkillPackage]) -> None:
    db, base = prepared
    now = [datetime.now(UTC)]
    provider = ScriptedProvider(
        [LLMError("segredo-nao-registrar", retryable=True), answer(base)]
    )

    def clock() -> datetime:
        return now[0]

    assert asyncio.run(Curator(db, provider, clock=clock).run_once())
    assert rows(db, "SELECT status,last_error FROM curation_jobs") == [
        ("pending", "provider_error")
    ]
    assert not asyncio.run(Curator(db, provider, clock=clock).run_once())
    now[0] += timedelta(seconds=5)
    assert asyncio.run(Curator(db, provider, clock=clock).run_once())
    assert rows(db, "SELECT COUNT(*) FROM curation_attempts") == [(2,)]
    assert len(SqliteProposalStore(db).list()) == 1


def test_timeout_and_attempt_exhaustion(prepared: tuple[Path, SkillPackage]) -> None:
    db, _ = prepared

    class Slow:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            await asyncio.sleep(1)
            raise AssertionError("deveria cancelar")

    limits = replace(CurationLimits(), timeout_seconds=0.01, max_attempts=1)
    asyncio.run(Curator(db, Slow(), limits=limits).run_once())
    assert rows(db, "SELECT status,last_error FROM curation_jobs") == [
        ("failed", "timeout")
    ]


@pytest.mark.parametrize(
    "limit",
    [
        CurationLimits(max_daily_tokens=1),
        CurationLimits(max_input_chars=1),
        CurationLimits(max_daily_cost_usd=0.001),
    ],
)
def test_budget_prevents_network(
    prepared: tuple[Path, SkillPackage], limit: CurationLimits
) -> None:
    db, base = prepared
    provider = ScriptedProvider([answer(base)])
    assert not asyncio.run(Curator(db, provider, limits=limit).run_once())
    assert provider.requests == []
    assert rows(db, "SELECT COUNT(*) FROM curation_attempts") == [(0,)]


def test_cost_reservation_is_persisted(prepared: tuple[Path, SkillPackage]) -> None:
    db, base = prepared
    limits = replace(CurationLimits(), max_daily_cost_usd=1)
    asyncio.run(
        Curator(
            db,
            ScriptedProvider([answer(base)]),
            limits=limits,
            model="fake-model",
            prices={"fake-model": Price(1, 2)},
        ).run_once()
    )
    assert rows(db, "SELECT status FROM curation_jobs") == [("completed",)]
    reserved = rows(db, "SELECT reserved_cost_usd,cost_usd FROM curation_attempts")[0]
    assert isinstance(reserved[0], float) and reserved[0] > 0
    assert reserved[1] is not None


def test_stale_base_and_fixture_runs_do_not_generate(
    prepared: tuple[Path, SkillPackage], tmp_path: Path
) -> None:
    db, _ = prepared
    SqliteSkillRegistry(db).retire("planejar_dia")
    provider = ScriptedProvider([])
    assert not asyncio.run(Curator(db, provider).run_once())
    assert rows(db, "SELECT status FROM curation_jobs") == [("stale",)]
    isolated = tmp_path / "eval.db"
    seed(isolated, channel="skill_eval")
    assert rows(isolated, "SELECT COUNT(*) FROM curation_jobs") == [(0,)]


def test_lease_expiry_fences_late_response(prepared: tuple[Path, SkillPackage]) -> None:
    db, base = prepared
    now = [datetime.now(UTC)]

    class Late:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            now[0] += timedelta(seconds=50)
            assert await Curator(
                db, ScriptedProvider([answer(base)]), clock=lambda: now[0]
            ).run_once()
            return answer(base)

    asyncio.run(Curator(db, Late(), clock=lambda: now[0]).run_once())
    assert len(SqliteProposalStore(db).list()) == 1
    assert rows(db, "SELECT attempts,status FROM curation_jobs") == [(2, "completed")]


def test_atomic_proposal_and_job_completion(
    prepared: tuple[Path, SkillPackage], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, base = prepared
    original = outbox.insert

    def fail(c: object, event: object) -> None:
        raise RuntimeError("evento indisponivel")

    monkeypatch.setattr(outbox, "insert", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(Curator(db, ScriptedProvider([answer(base)])).run_once())
    assert SqliteProposalStore(db).list() == []
    assert rows(db, "SELECT status FROM curation_jobs") == [("leased",)]
    monkeypatch.setattr(outbox, "insert", original)


def test_disabled_by_default_and_daily_volume(
    prepared: tuple[Path, SkillPackage],
) -> None:
    db, _ = prepared
    settings = ServerSettings(_env_file=None)
    assert not settings.curation_enabled
    assert all(
        h.name != "skills.curator.schedule"
        for hs in build_handlers(settings).values()
        for h in hs
    )
    with pytest.raises(Exception, match="CURATION_ENABLED"):
        build_curator(settings)
    # Reentrega da outbox não cria outro job, inclusive após trocar o worker.
    Worker(db, build_handlers(settings), "second-worker").run_once()
    assert rows(db, "SELECT COUNT(*) FROM curation_jobs") == [(1,)]


def test_failed_evaluation_volume_deduplication_and_provenance(
    prepared: tuple[Path, SkillPackage],
) -> None:
    db, base = prepared
    evaluation_id = str(uuid4())
    event = OutboxEvent(
        type="neoson.skill.evaluated", payload={"evaluation_id": evaluation_id}
    )
    with closing(connect(db)) as c, transaction(c):
        c.execute("UPDATE curation_jobs SET status='failed'")
        c.execute(
            "INSERT INTO skill_evaluations VALUES"
            " (?, 'planejar_dia','1.1.0',?, ?,2,0,20,5,0,NULL,0.1,'[]')",
            (evaluation_id, base.checksum, datetime.now(UTC).isoformat()),
        )
        outbox.insert(c, event)
        Scheduler(CurationLimits(max_daily_jobs=1))(c, event)
        assert c.execute("SELECT COUNT(*) FROM curation_jobs").fetchone()[0] == 1
        scheduler = Scheduler(CurationLimits())
        scheduler(c, event)
        scheduler(c, event)
        assert c.execute("SELECT COUNT(*) FROM curation_jobs").fetchone()[0] == 2
    assert asyncio.run(Curator(db, ScriptedProvider([answer(base)])).run_once())
    proposal = SqliteProposalStore(db).list()[0]
    assert proposal.sources[0].kind == "evaluation"
    assert str(proposal.sources[0].id) == evaluation_id


def test_base_changes_during_generation_preserve_catalog(
    prepared: tuple[Path, SkillPackage],
) -> None:
    db, base = prepared

    class Retiring:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            SqliteSkillRegistry(db).retire("planejar_dia")
            return answer(base)

    assert asyncio.run(Curator(db, Retiring()).run_once())
    assert SqliteProposalStore(db).list() == []
    assert rows(db, "SELECT status FROM curation_jobs") == [("failed",)]


def test_expired_final_attempt_does_not_call_provider(
    prepared: tuple[Path, SkillPackage],
) -> None:
    db, base = prepared
    now = datetime.now(UTC)
    provider = ScriptedProvider([answer(base)])
    with closing(connect(db)) as c:
        c.execute(
            "UPDATE curation_jobs SET status='leased',attempts=1,lease_until=?",
            (now.isoformat(),),
        )
    limits = CurationLimits(max_attempts=1)
    assert not asyncio.run(Curator(db, provider, limits=limits).run_once())
    assert not provider.requests
    assert rows(db, "SELECT status,last_error FROM curation_jobs") == [
        ("failed", "attempt_limit")
    ]


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("NEOSON_LIVE_TESTS") != "1", reason="requer API real autorizada"
)
def test_live_curator_creates_isolated_candidate(tmp_path: Path) -> None:
    settings = load_settings()
    db = tmp_path / "live.db"
    base = seed(db)
    isolated = settings.model_copy(
        update={
            "db_path": db,
            "curation_enabled": True,
            "curation_timeout_seconds": 60,
            "curation_max_output_tokens": 5000,
        }
    )
    curator = build_curator(isolated)
    assert asyncio.run(curator.run_once())
    proposals = SqliteProposalStore(db).list()
    assert len(proposals) == 1
    assert proposals[0].candidate.metadata.version == "1.2.0"
    assert asyncio.run(SqliteSkillRegistry(db).index()) == [base.skill().ref]
    assert rows(db, "SELECT status FROM curation_jobs") == [("completed",)]
