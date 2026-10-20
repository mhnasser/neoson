import asyncio
import os
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import yaml

from neoson_core.engine import (
    Message,
    OutboxEvent,
    Role,
    RunRecord,
    RunStatus,
    Usage,
)
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.config import load_settings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.skills.loader import load_skill, package_from_texts
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.telemetry.skill_metrics import EVENT_TYPES, skill_metrics
from neoson_core.telemetry.vault import VaultExporter, write_atomic
from neoson_worker.main import build_handlers, main
from neoson_worker.worker import Worker

ZONE = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
DAY = date(2026, 10, 20)
PRIVATE = "conteudo-privado-nao-exportar"
LIBRARY = Path(__file__).resolve().parents[1] / "skill_library"


def approve(db: Path, version: str = "1.1.0") -> None:
    package = load_skill(LIBRARY / "planejar_dia")
    contents = dict(package.contents)
    contents["metadata.yaml"] = contents["metadata.yaml"].replace("1.1.0", version)
    SqliteSkillRegistry(db).approve(package_from_texts(package.path, contents))


def add_run(db: Path, version: str, status: RunStatus) -> None:
    store = SqliteConversationStore(db)
    session = asyncio.run(store.create_session("http"))
    run_id = uuid4()
    ref = SqliteSkillRegistry(db).approved_package("planejar_dia").skill().ref
    assert ref.version == version
    event = {
        RunStatus.COMPLETED: "neoson.run.completed",
        RunStatus.FAILED: "neoson.run.failed",
        RunStatus.NEEDS_APPROVAL: "neoson.run.waiting_approval",
    }[status]
    asyncio.run(
        store.record_run(
            RunRecord(
                run_id,
                session,
                status,
                NOW,
                NOW,
                (Message(Role.USER, PRIVATE),),
                usage=Usage(100, 20, 30),
                model="sem-preco",
                skills=(ref,),
                events=(
                    OutboxEvent(
                        type=event,
                        run_id=run_id,
                        session_id=session,
                        occurred_at=NOW,
                        payload={
                            "model": "sem-preco",
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "cached_tokens": 30,
                            },
                        },
                    ),
                ),
            )
        )
    )


def add_evaluation(
    db: Path, version: str, when: str, passed: int, *, checksum: str | None = None
) -> None:
    with closing(connect(db)) as connection:
        digest = (
            checksum
            or connection.execute(
                "SELECT checksum FROM skills WHERE id = 'planejar_dia' AND version = ?",
                (version,),
            ).fetchone()[0]
        )
        evaluation_id = str(uuid4())
        connection.execute(
            "INSERT INTO skill_evaluations VALUES"
            " (?, 'planejar_dia', ?, ?, ?, 2, ?, 200, 40, 10, NULL, 1.5, ?)",
            (evaluation_id, version, digest, when, passed, PRIVATE),
        )
        outbox.insert(connection, OutboxEvent(type="neoson.skill.evaluated"))


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    db = tmp_path / "neoson.db"
    approve(db)
    add_run(db, "1.1.0", RunStatus.COMPLETED)
    approve(db, "1.2.0")
    for status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.NEEDS_APPROVAL):
        add_run(db, "1.2.0", status)
    add_evaluation(db, "1.1.0", "2026-10-20T15:00:00+00:00", 2)
    add_evaluation(db, "1.2.0", "2026-10-20T12:00:00+00:00", 0)
    add_evaluation(db, "1.2.0", "2026-10-20T10:00:00-03:00", 1)
    # Uma avaliação com checksum diferente não representa a versão aprovada.
    add_evaluation(db, "1.2.0", "2026-10-20T16:00:00+00:00", 2, checksum="outro")
    return db


def export(db: Path, vault: Path) -> list[Path]:
    with closing(connect(db)) as connection:
        return VaultExporter(vault, ZONE).export(connection, DAY)


def metadata(path: Path) -> dict[str, object]:
    result: dict[str, object] = yaml.safe_load(
        path.read_text(encoding="utf-8").split("---\n", 2)[1]
    )
    return result


def test_metrics_separate_versions_and_latest_evaluation(populated: Path) -> None:
    with closing(connect(populated)) as connection:
        previous, active = skill_metrics(connection)
    assert (previous.version, previous.status, previous.runs) == (
        "1.1.0",
        "approved",
        1,
    )
    assert (previous.success_pct, previous.passed_cases) == (100, 2)
    assert (active.runs, active.completed, active.failed, active.waiting_approval) == (
        3,
        1,
        1,
        1,
    )
    assert active.success_pct == 33.3
    assert (active.input_tokens, active.output_tokens, active.cached_tokens) == (
        300,
        60,
        90,
    )
    assert active.cost_usd is None and active.runs_without_cost == 3
    assert (active.total_cases, active.passed_cases) == (2, 1)
    assert active.evaluation_input_tokens == 200
    assert active.evaluation_seconds == 1.5


def test_partial_cost_is_explicit(populated: Path) -> None:
    with closing(connect(populated)) as connection:
        connection.execute(
            "INSERT INTO usage_records"
            " (run_id, model, input_tokens, output_tokens, cached_tokens,"
            " cost_usd, recorded_at)"
            " SELECT run_id, 'sem-preco', 100, 20, 30, 0.01, ?"
            " FROM skill_executions WHERE skill_version = '1.2.0' LIMIT 1",
            (NOW.isoformat(),),
        )
        active = skill_metrics(connection)[1]
    assert active.cost_usd == 0.01 and active.runs_without_cost == 2


def test_vault_has_only_aggregates_and_is_idempotent(
    populated: Path, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    changed = export(populated, vault)
    note = vault / "Skills/Ativas/planejar_dia.md"
    data = metadata(note)
    assert data["versao"] == "1.2.0" and data["sucesso_pct"] == 33.3
    assert data["avaliacao_casos_aprovados"] == 1
    assert data["avaliacao_custo_usd"] is None
    assert data["tokens_entrada"] == 300 and data["execucoes_sem_custo"] == 3
    panel = (vault / "Dashboard/Skills-Learning.md").read_text(encoding="utf-8")
    assert "1.1.0 | approved | 1 | 100.0%" in panel
    assert "1.2.0 | active | 3 | 33.3%" in panel
    assert "2026-10-20 13:00:00 UTC; 1/2" in panel
    assert "[[Dashboard/Skills-Learning|Skills & Learning]]" in (
        vault / "Dashboard/Home.md"
    ).read_text(encoding="utf-8")
    for path in vault.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert PRIVATE not in text
        assert "## Quando usar" not in text and "allowed_tools" not in text
        metadata(path)
    times = {path: path.stat().st_mtime_ns for path in changed}
    assert export(populated, vault) == []
    assert times == {path: path.stat().st_mtime_ns for path in changed}


def test_rollback_and_retirement_update_generated_notes(
    populated: Path, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    export(populated, vault)
    registry = SqliteSkillRegistry(populated)
    registry.rollback("planejar_dia")
    export(populated, vault)
    note = vault / "Skills/Ativas/planejar_dia.md"
    assert metadata(note)["versao"] == "1.1.0"
    assert metadata(note)["execucoes_total"] == 1
    registry.retire("planejar_dia")
    # Uma nota criada pelo usuário não pertence ao exporter.
    manual = vault / "Skills/Ativas/minhas-notas.md"
    manual.write_text("Anotações pessoais", encoding="utf-8")
    assert note in export(populated, vault)
    assert not note.exists() and manual.exists()
    assert metadata(vault / "Dashboard/Skills-Learning.md")["skills_ativas"] == 0
    assert export(populated, vault) == []


def test_unused_active_skill_has_no_fabricated_metrics(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    approve(db)
    vault = tmp_path / "vault"
    export(db, vault)
    data = metadata(vault / "Skills/Ativas/planejar_dia.md")
    assert data["execucoes_total"] == 0
    for name in (
        "sucesso_pct",
        "custo_usd",
        "ultima_avaliacao",
        "avaliacao_casos_total",
    ):
        assert data[name] is None


def test_atomic_failure_preserves_previous_note_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = tmp_path / "Skill.md"
    write_atomic(note, "anterior")
    replace = os.replace

    def fail(source: Path, target: Path) -> None:
        assert target == note
        assert source.read_text(encoding="utf-8") == "nova"
        assert target.read_text(encoding="utf-8") == "anterior"
        raise OSError("rename indisponível")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_atomic(note, "nova")
    assert note.read_text(encoding="utf-8") == "anterior"
    monkeypatch.setattr(os, "replace", replace)
    assert write_atomic(note, "nova")
    assert not write_atomic(note, "nova")
    assert list(tmp_path.iterdir()) == [note]


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("enabled", [True, False])
def test_skill_events_route_only_to_vault(
    settings_env: None, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setenv("NEOSON_METRICS_EXPORT_ENABLED", str(enabled).lower())
    routes = build_handlers(load_settings(env_file=None))
    for kind in EVENT_TYPES:
        assert [h.name for h in routes[kind]] == (
            ["telemetry.vault"] if enabled else []
        )


def test_worker_and_explicit_export_refresh_skills(
    populated: Path, settings_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    monkeypatch.setenv("NEOSON_DB_PATH", str(populated))
    monkeypatch.setenv("NEOSON_VAULT_PATH", str(vault))
    settings = load_settings(env_file=None)
    worker = Worker(populated, build_handlers(settings), "test-worker")
    assert worker.run_once() > 0
    assert metadata(vault / "Skills/Ativas/planejar_dia.md")["versao"] == "1.2.0"
    with closing(connect(populated)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE status != 'completed'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0] == 4
        )
    assert worker.run_once() == 0
    SqliteSkillRegistry(populated).rollback("planejar_dia")
    assert worker.run_once() == 1
    assert metadata(vault / "Skills/Ativas/planejar_dia.md")["versao"] == "1.1.0"
    SqliteSkillRegistry(populated).retire("planejar_dia")
    assert worker.run_once() == 1
    assert not (vault / "Skills/Ativas/planejar_dia.md").exists()
    approve(populated)
    assert main(["--export-vault"]) == 0
    assert metadata(vault / "Skills/Ativas/planejar_dia.md")["versao"] == "1.1.0"


def test_invalid_database_id_cannot_write_outside_vault(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    approve(db)
    with closing(connect(db)) as connection:
        connection.execute("UPDATE skills SET id = '../../fora'")
    with pytest.raises(ValueError, match="ID de skill"):
        export(db, tmp_path / "vault")
    assert not (tmp_path / "fora.md").exists()
