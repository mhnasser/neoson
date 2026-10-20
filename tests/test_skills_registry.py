import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from neoson_core.engine.skills import SkillNotFound, SkillRegistry
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, migrate, migrations, schema_version
from neoson_core.skills.loader import SkillPackage, load_skill
from neoson_core.skills.registry import (
    SkillConflict,
    SkillIntegrityError,
    SqliteSkillRegistry,
)
from neoson_gateway.skills_main import main

INSTRUCTIONS = """# Resumir
## Quando usar
Quando pedirem resumo.
## Passos
Leia e resuma.
## Restrições
Não invente.
## Saída
Até cinco pontos.
"""


def package(
    tmp_path: Path, version: str = "1.0.0", name: str = "resumir"
) -> SkillPackage:
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    (folder / "metadata.yaml").write_text(
        f"id: {name}\nversion: {version}\n"
        "description: Resume o texto em poucos pontos.\n"
        "triggers: [resumir]\nallowed_tools: [calendar_list]\n"
        "requires_approval: false\nrisk_level: low\nowner: neoson\n",
        encoding="utf-8",
    )
    (folder / "SKILL.md").write_text(INSTRUCTIONS, encoding="utf-8")
    return load_skill(folder, ["calendar_list"])


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "dados" / "neoson.db"


def test_approval_survives_restart_without_reading_source(
    tmp_path: Path, db: Path
) -> None:
    original = package(tmp_path)
    registry = SqliteSkillRegistry(db)
    assert asyncio.run(registry.index()) == []
    first = registry.approve(original)
    (original.path / "SKILL.md").write_text("alterado", encoding="utf-8")

    port: SkillRegistry = SqliteSkillRegistry(db)
    loaded = asyncio.run(port.load_approved("resumir", "1.0.0"))
    assert loaded == original.skill()
    assert first.status == "active" and first.approved_at.utcoffset() is not None
    assert asyncio.run(port.index()) == [original.skill().ref]
    connection = connect(db)
    try:
        event = connection.execute("SELECT * FROM outbox_events").fetchone()
    finally:
        connection.close()
    assert event["type"] == "neoson.skill.approved"
    assert json.loads(event["payload"]) == {
        "skill_id": "resumir",
        "version": "1.0.0",
        "checksum": original.checksum,
    }


def test_same_version_is_idempotent_but_changed_content_is_rejected(
    tmp_path: Path, db: Path
) -> None:
    registry = SqliteSkillRegistry(db)
    original = package(tmp_path)
    first = registry.approve(original)
    assert registry.approve(original) == first
    (original.path / "SKILL.md").write_text(
        INSTRUCTIONS + "Novo texto.\n", encoding="utf-8"
    )
    with pytest.raises(SkillConflict, match="nova versão"):
        registry.approve(load_skill(original.path))

    assert registry.versions() == [first]
    connection = connect(db)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 1
        )
    finally:
        connection.close()


def test_approval_uses_the_validated_snapshot_even_if_source_changes(
    tmp_path: Path, db: Path
) -> None:
    original = package(tmp_path)
    (original.path / "SKILL.md").write_text("alterado", encoding="utf-8")
    with pytest.raises(TypeError):
        original.contents["SKILL.md"] = "alterado"  # type: ignore[index]
    registry = SqliteSkillRegistry(db)
    registry.approve(original)
    assert asyncio.run(registry.load_approved("resumir", "1.0.0")) == original.skill()


def test_new_version_rollback_and_retirement_keep_history(
    tmp_path: Path, db: Path
) -> None:
    registry = SqliteSkillRegistry(db)
    registry.approve(package(tmp_path))
    registry.approve(package(tmp_path, "1.1.0"))
    assert [v.status for v in registry.versions()] == ["approved", "active"]
    with pytest.raises(SkillNotFound):
        asyncio.run(registry.load_approved("resumir", "1.0.0"))

    assert registry.rollback("resumir").ref.version == "1.0.0"
    assert asyncio.run(registry.index())[0].version == "1.0.0"
    with pytest.raises(SkillNotFound, match="anterior"):
        registry.rollback("resumir")
    registry.retire("resumir")
    assert asyncio.run(registry.index()) == []
    assert [v.status for v in registry.versions()] == ["retired", "approved"]
    with pytest.raises(SkillNotFound):
        asyncio.run(registry.load_approved("resumir", "1.0.0"))
    with pytest.raises(SkillNotFound):
        registry.retire("resumir")
    # Reativar uma versão retirada também exige aprovação explícita.
    registry.approve(package(tmp_path))
    assert asyncio.run(registry.index())[0].version == "1.0.0"


def test_index_is_stable_and_rollback_follows_approval_order(
    tmp_path: Path, db: Path
) -> None:
    registry = SqliteSkillRegistry(db)
    registry.approve(package(tmp_path, "9.0.0", "zebra"))
    registry.approve(package(tmp_path, "1.0.0", "zebra"))
    registry.approve(package(tmp_path, name="resumir"))
    assert [ref.id for ref in asyncio.run(registry.index())] == ["resumir", "zebra"]
    assert registry.rollback("zebra").ref.version == "9.0.0"


@pytest.mark.parametrize("contents", ["{", "[]", '{"SKILL.md": 42}'])
def test_corrupt_snapshot_is_rejected_without_disclosing_contents(
    tmp_path: Path, db: Path, contents: str
) -> None:
    registry = SqliteSkillRegistry(db)
    registry.approve(package(tmp_path))
    connection = connect(db)
    try:
        connection.execute("UPDATE skills SET files_json = ?", (contents,))
    finally:
        connection.close()
    with pytest.raises(SkillIntegrityError, match="adulterada"):
        asyncio.run(registry.load_approved("resumir", "1.0.0"))


def test_changed_copy_or_checksum_is_rejected(tmp_path: Path, db: Path) -> None:
    registry = SqliteSkillRegistry(db)
    original = package(tmp_path)
    registry.approve(original)
    changed = dict(original.contents) | {"SKILL.md": INSTRUCTIONS + "Alterada."}
    connection = connect(db)
    try:
        connection.execute("UPDATE skills SET files_json = ?", (json.dumps(changed),))
    finally:
        connection.close()
    with pytest.raises(SkillIntegrityError):
        asyncio.run(registry.load_approved("resumir", "1.0.0"))
    with pytest.raises(SkillIntegrityError):
        asyncio.run(registry.index())


def test_corrupt_previous_version_cannot_be_reactivated(
    tmp_path: Path, db: Path
) -> None:
    registry = SqliteSkillRegistry(db)
    registry.approve(package(tmp_path))
    registry.approve(package(tmp_path, "1.1.0"))
    connection = connect(db)
    try:
        connection.execute(
            "UPDATE skills SET checksum = 'alterado' WHERE version = '1.0.0'"
        )
    finally:
        connection.close()
    with pytest.raises(SkillIntegrityError):
        registry.rollback("resumir")
    assert asyncio.run(registry.index())[0].version == "1.1.0"


@pytest.mark.parametrize("action", ["approve", "rollback", "retire"])
def test_event_failure_rolls_back_catalog_change(
    tmp_path: Path, db: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    registry = SqliteSkillRegistry(db)
    registry.approve(package(tmp_path))
    second = package(tmp_path, "1.1.0")
    if action != "approve":
        registry.approve(second)
    before = registry.versions()

    def fail(*args: object) -> None:
        raise sqlite3.IntegrityError("outbox indisponível")

    monkeypatch.setattr(outbox, "insert", fail)
    with pytest.raises(sqlite3.IntegrityError):
        if action == "approve":
            registry.approve(second)
        elif action == "rollback":
            registry.rollback("resumir")
        else:
            registry.retire("resumir")
    assert registry.versions() == before


def test_existing_database_is_migrated_without_losing_conversations(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect(db)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                "neoson_core.infrastructure.db.migrations", lambda: migrations()[:5]
            )
            migrate(connection)
        assert schema_version(connection) == 5
        connection.execute(
            "INSERT INTO sessions VALUES ('sessao', 'cli', 'agora', 'agora')"
        )
    finally:
        connection.close()
    SqliteSkillRegistry(db)
    connection = connect(db)
    try:
        assert schema_version(connection) >= 6
        assert connection.execute("SELECT id FROM sessions").fetchone()[0] == "sessao"
    finally:
        connection.close()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_DB_PATH", str(db))
    return tmp_path


def test_cli_manages_approved_versions(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list"]) == 0
    assert "Nenhuma versão" in capsys.readouterr().out
    first = package(cli_env)
    assert main(["approve", str(first.path)]) == 0
    assert "Aprovada resumir 1.0.0" in capsys.readouterr().out
    second = package(cli_env, "1.1.0")
    assert main(["approve", str(second.path)]) == 0
    assert main(["list"]) == 0
    output = capsys.readouterr().out
    assert "resumir 1.0.0 approved" in output and "resumir 1.1.0 active" in output
    assert main(["rollback", "resumir"]) == 0
    assert "Ativa resumir 1.0.0" in capsys.readouterr().out
    assert main(["retire", "resumir"]) == 0
    assert "Retirada resumir" in capsys.readouterr().out
    assert main(["rollback", "inexistente"]) == 1
    assert "não está ativa" in capsys.readouterr().err


def test_cli_invalid_skill_does_not_create_database(cli_env: Path, db: Path) -> None:
    assert main(["approve", str(cli_env / "ausente")]) == 1
    assert not db.exists()


def test_cli_rejects_reused_version(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = package(cli_env)
    assert main(["approve", str(first.path)]) == 0
    (first.path / "SKILL.md").write_text(INSTRUCTIONS + "Mudança.", encoding="utf-8")
    assert main(["approve", str(first.path)]) == 1
    assert "nova versão" in capsys.readouterr().err
