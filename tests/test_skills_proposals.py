import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from neoson_core.engine import AgentLoop, Budget, LLMError, OutboxEvent, UserMessage
from neoson_core.infrastructure import db as database
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, schema_version
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.loader import (
    SkillInvalid,
    SkillPackage,
    load_skill,
    package_from_texts,
)
from neoson_core.skills.proposals import (
    ProposalConflict,
    ProposalInvalid,
    ProposalNotFound,
    ProposalSource,
    ProposalStatus,
    SqliteProposalStore,
)
from neoson_core.skills.registry import SkillIntegrityError, SqliteSkillRegistry

LIBRARY = Path(__file__).resolve().parents[1] / "skill_library"
PRIVATE = "dado-privado-do-snapshot"


def candidate(base: SkillPackage, version: str = "1.2.0") -> SkillPackage:
    texts = dict(base.contents)
    texts["metadata.yaml"] = texts["metadata.yaml"].replace(
        base.metadata.version, version
    )
    texts["SKILL.md"] += f"\nValide a ordem antes de concluir. {PRIVATE}\n"
    return package_from_texts(base.path, texts)


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Path, SkillPackage, tuple[ProposalSource, ...]]:
    db = tmp_path / "neoson.db"
    base = load_skill(LIBRARY / "planejar_dia")
    registry = SqliteSkillRegistry(db)
    registry.approve(base)
    loop = AgentLoop(
        ScriptedProvider(
            [
                call_tool(
                    "skill_load", {"skill_id": "planejar_dia", "version": "1.1.0"}
                ),
                LLMError("erro simulado", retryable=True),
            ]
        ),
        RulesRouter("balanced"),
        Budget(5, 5),
        skills=registry,
        store=SqliteConversationStore(db),
    )
    result = asyncio.run(loop.run(UserMessage(PRIVATE)))
    evaluation = uuid4()
    with closing(connect(db)) as c:
        c.execute(
            "INSERT INTO skill_evaluations VALUES"
            " (?, 'planejar_dia', '1.1.0', ?, ?, 1, 0, 20, 5, 0, NULL, 0.1, '[]')",
            (str(evaluation), base.checksum, datetime.now(UTC).isoformat()),
        )
    return (
        db,
        base,
        (
            ProposalSource("run", result.run_id),
            ProposalSource("evaluation", evaluation),
        ),
    )


def create(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
    *,
    request: UUID | None = None,
    package: SkillPackage | None = None,
    sources: tuple[ProposalSource, ...] | None = None,
) -> UUID:
    db, base, references = prepared
    return (
        SqliteProposalStore(db)
        .create(
            package or candidate(base),
            base_version=base.metadata.version,
            base_checksum=base.checksum,
            request_id=request or uuid4(),
            sources=references if sources is None else sources,
        )
        .id
    )


def count(db: Path, table: str) -> int:
    with closing(connect(db)) as c:
        return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_snapshot_sources_restart_and_catalog_isolation(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
) -> None:
    db, base, sources = prepared
    original = (base.path / "SKILL.md").read_bytes()
    proposal_id = create(prepared)
    loaded = SqliteProposalStore(db).get(proposal_id)
    assert loaded.status == ProposalStatus.PENDING
    assert loaded.base_checksum == base.checksum and loaded.base_version == "1.1.0"
    assert loaded.candidate.checksum == candidate(base).checksum
    assert loaded.candidate.metadata.version == "1.2.0"
    assert set(loaded.sources) == set(sources) and loaded.created_at.tzinfo is not None
    assert SqliteProposalStore(db).list() == [loaded]
    assert count(db, "skills") == 1
    registry = SqliteSkillRegistry(db)
    assert asyncio.run(registry.index()) == [base.skill().ref]
    assert registry.approved_package("planejar_dia").checksum == base.checksum
    assert (base.path / "SKILL.md").read_bytes() == original
    with closing(connect(db)) as c:
        events = c.execute(
            "SELECT payload FROM outbox_events WHERE type LIKE '%proposal%'"
        ).fetchall()
    assert len(events) == 1 and PRIVATE not in events[0][0]
    assert json.loads(events[0][0])["proposal_id"] == str(proposal_id)


def test_retry_deduplicates_sources_and_remains_idempotent_after_rejection(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
) -> None:
    db, base, sources = prepared
    request = uuid4()
    first = create(prepared, request=request, sources=sources + sources)
    assert create(prepared, request=request) == first
    assert count(db, "skill_proposals") == 1 and count(db, "skill_proposal_runs") == 1
    store = SqliteProposalStore(db)
    rejected = store.reject(first)
    assert (
        store.reject(first) == rejected and rejected.status == ProposalStatus.REJECTED
    )
    assert create(prepared, request=request) == first
    assert count(db, "skills") == 1
    SqliteSkillRegistry(db).approve(candidate(base, "1.3.0"))
    assert create(prepared, request=request) == first
    with closing(connect(db)) as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE type LIKE '%proposal%'"
            ).fetchone()[0]
            == 2
        )


def test_same_request_with_different_content_or_sources_is_conflict(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
) -> None:
    _, base, sources = prepared
    request = uuid4()
    create(prepared, request=request)
    with pytest.raises(ProposalConflict):
        create(prepared, request=request, package=candidate(base, "1.3.0"))
    with pytest.raises(ProposalConflict):
        create(prepared, request=request, sources=sources[:1])
    with pytest.raises(ProposalConflict):
        create(prepared)


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed_tools", "[calendar_list, memory_save]"),
        ("requires_approval", "true"),
        ("risk_level", "high"),
        ("owner", "outro"),
        ("description", "Outro procedimento diferente."),
        ("triggers", "[outro]"),
    ],
)
def test_cannot_change_metadata_except_version(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
    field: str,
    value: str,
) -> None:
    import yaml

    db, base, _ = prepared
    texts = dict(candidate(base).contents)
    metadata = yaml.safe_load(texts["metadata.yaml"])
    metadata[field] = yaml.safe_load(value)
    texts["metadata.yaml"] = yaml.safe_dump(metadata)
    changed = package_from_texts(base.path, texts)
    with pytest.raises(ProposalInvalid, match="metadados"):
        create(prepared, package=changed)
    assert count(db, "skill_proposals") == 0


@pytest.mark.parametrize(
    "change", ["fixture", "remove", "add", "script", "version-only", "older"]
)
def test_procedural_limits(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
    change: str,
) -> None:
    db, base, _ = prepared
    texts = dict(candidate(base).contents)
    fixture = next(name for name in texts if name.startswith("tests/fixtures/"))
    if change == "fixture":
        texts[fixture] += "\n# Mudança indevida\n"
    elif change == "remove":
        del texts[fixture]
    elif change == "add":
        texts["examples/novo.md"] = "Novo exemplo"
    elif change == "script":
        texts["scripts/executar.py"] = "print('indevido')"
    elif change == "version-only":
        texts["SKILL.md"] = base.contents["SKILL.md"]
    else:
        texts["metadata.yaml"] = texts["metadata.yaml"].replace("1.2.0", "1.0.0")
    with pytest.raises((ProposalInvalid, SkillInvalid)):
        create(prepared, package=package_from_texts(base.path, texts))
    assert count(db, "skill_proposals") == 0


@pytest.mark.parametrize("kind", ["run", "evaluation"])
def test_sources_must_exist_and_belong_to_base(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
    kind: str,
) -> None:
    db, base, sources = prepared
    missing = ProposalSource("run" if kind == "run" else "evaluation", uuid4())
    with pytest.raises(ProposalInvalid, match="Fonte"):
        create(prepared, sources=(missing,))
    newer = candidate(base)
    SqliteSkillRegistry(db).approve(newer)
    wrong_base = (db, newer, tuple(s for s in sources if s.kind == kind))
    with pytest.raises(ProposalInvalid, match="Fonte"):
        create(wrong_base, package=candidate(newer, "1.3.0"))


def test_source_bounds_and_base_changed(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
) -> None:
    db, base, _ = prepared
    for sources in ((), tuple(ProposalSource("run", uuid4()) for _ in range(33))):
        with pytest.raises(ProposalInvalid):
            create(prepared, sources=sources)
    SqliteSkillRegistry(db).approve(candidate(base))
    with pytest.raises(ProposalConflict, match="ativa mudou"):
        create(prepared, package=candidate(base, "1.3.0"))


def test_evaluation_checksum_and_reserved_approved_version(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
) -> None:
    db, base, sources = prepared
    with closing(connect(db)) as c:
        c.execute("UPDATE skill_evaluations SET checksum = 'outro'")
    with pytest.raises(ProposalInvalid, match="Fonte"):
        create(prepared, sources=tuple(s for s in sources if s.kind == "evaluation"))
    registry = SqliteSkillRegistry(db)
    registry.approve(candidate(base))
    registry.rollback("planejar_dia")
    with pytest.raises(ProposalConflict, match="reservada"):
        create(prepared, sources=tuple(s for s in sources if s.kind == "run"))


def test_integrity_checked_at_creation_and_read(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
) -> None:
    db, base, _ = prepared
    with pytest.raises(SkillIntegrityError):
        create(prepared, package=replace(candidate(base), checksum="adulterado"))
    proposal_id = create(prepared)
    with closing(connect(db)) as c:
        c.execute("UPDATE skill_proposals SET candidate_checksum = 'adulterado'")
    with pytest.raises(SkillIntegrityError):
        SqliteProposalStore(db).get(proposal_id)


def test_source_foreign_keys_preserve_provenance(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
) -> None:
    db, _, sources = prepared
    create(prepared)
    evaluation = next(s for s in sources if s.kind == "evaluation")
    with closing(connect(db)) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM skill_evaluations WHERE id = ?", (str(evaluation.id),))


def test_atomic_event_and_concurrent_idempotency(
    prepared: tuple[Path, SkillPackage, tuple[ProposalSource, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _, _ = prepared
    insert = outbox.insert

    def fail(connection: sqlite3.Connection, event: OutboxEvent) -> None:
        raise RuntimeError("evento indisponível")

    monkeypatch.setattr(outbox, "insert", fail)
    with pytest.raises(RuntimeError):
        create(prepared)
    for table in (
        "skill_proposals",
        "skill_proposal_runs",
        "skill_proposal_evaluations",
    ):
        assert count(db, table) == 0
    monkeypatch.setattr(outbox, "insert", insert)
    request = uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(prepared, request=request), range(2)))
    assert results[0] == results[1] and count(db, "skill_proposals") == 1


def test_migration_from_previous_schema_and_missing_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "neoson.db"
    migrations = database.migrations()
    monkeypatch.setattr(database, "migrations", lambda: migrations[:8])
    SqliteSkillRegistry(db)
    legacy = load_skill(LIBRARY / "planejar_dia")
    with closing(connect(db)) as c:
        assert schema_version(c) == 8
        # Preparar o schema histórico sem usar a API atual, que exige schema novo.
        c.execute(
            "INSERT INTO skills(id,version,checksum,files_json,status,approved_at)"
            " VALUES(?,?,?,?, 'active', ?)",
            (
                legacy.metadata.id,
                legacy.metadata.version,
                legacy.checksum,
                json.dumps(dict(legacy.contents)),
                "2026-10-20T12:00:00+00:00",
            ),
        )
    monkeypatch.setattr(database, "migrations", lambda: migrations)
    store = SqliteProposalStore(db)
    assert store.list() == []
    with closing(connect(db)) as c:
        assert schema_version(c) >= 9
        assert c.execute("SELECT version FROM skills").fetchone()[0] == "1.1.0"
    with pytest.raises(ProposalNotFound):
        store.get(uuid4())
    with pytest.raises(ProposalNotFound):
        store.reject(uuid4())
