import asyncio
import json
import os
import shutil
import sqlite3
import tomllib
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from neoson_core.infrastructure import backup
from neoson_core.infrastructure.db import connect, migrate
from neoson_core.infrastructure.maintenance import (
    MaintenanceError,
    check,
    relocate,
    synced_folder,
)
from neoson_core.memory.curated import SqliteCuratedMemory
from neoson_worker import backup_main, maintenance_main

NOW = datetime(2026, 10, 22, 20, tzinfo=UTC)


def database(path: Path) -> Path:
    asyncio.run(SqliteCuratedMemory(path).save("user", "fato", None, None))
    return path


def codes(findings: list[Any]) -> dict[str, str]:
    return {f.code: f.level for f in findings}


def test_check_reports_schema_sync_folder_usage_and_backups(tmp_path: Path) -> None:
    assert codes(check(tmp_path / "nada.db", tmp_path)) == {"database_missing": "error"}
    synced = tmp_path / "OneDrive" / "projeto" / "data"
    synced.mkdir(parents=True)
    db = database(synced / "neoson.db")
    with closing(connect(db)) as c, c:
        c.execute("DELETE FROM schema_migrations WHERE version >= 17")
    (synced / "neoson.db-wal").write_bytes(b"")
    found = check(db, tmp_path / "backups", synced / "docs", now=NOW)
    levels = codes(found)
    assert levels["migrations_pending"] == "warning"
    pending = next(f for f in found if f.code == "migrations_pending").message
    assert "17" in pending and "18" in pending
    assert levels["synced_folder"] == "warning" and levels["in_use"] == "warning"
    assert levels["no_backup"] == "warning"
    assert synced_folder(db) and not synced_folder(tmp_path / "D" / "dados")


def test_check_ages_backups_and_is_ok_when_current(tmp_path: Path) -> None:
    db = database(tmp_path / "dados" / "neoson.db")
    info = backup.create(db, tmp_path / "vault", tmp_path / "backups")
    ok = codes(
        check(db, tmp_path / "backups", now=info.created_at + timedelta(hours=1))
    )
    assert ok["migrations_current"] == "ok" and ok["backup_recent"] == "ok"
    late = check(db, tmp_path / "backups", now=info.created_at + timedelta(days=2))
    assert codes(late)["backup_old"] == "warning"
    assert "integrity" not in codes(late)


def test_relocate_copies_checks_and_keeps_the_original(tmp_path: Path) -> None:
    db = database(tmp_path / "OneDrive" / "neoson.db")
    docs = tmp_path / "OneDrive" / "documents"
    docs.mkdir()
    (docs / "abc.md").write_text("original", encoding="utf-8")
    target = tmp_path / "D" / "NeoSon-Data"
    moved = relocate(db, target, docs)
    assert moved.db_path == target / "neoson.db" and db.exists()
    assert (target / "documents" / "abc.md").read_text(encoding="utf-8") == "original"
    with closing(sqlite3.connect(moved.db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (1,)
    with pytest.raises(MaintenanceError, match="Já existem dados"):
        relocate(db, target, docs)
    with pytest.raises(MaintenanceError, match="dentro dos documentos"):
        relocate(db, docs / "destino", docs)
    with pytest.raises(MaintenanceError, match="pasta atual"):
        relocate(db, db.parent)
    retired = relocate(db, tmp_path / "outro", retire_original=True, now=NOW)
    assert retired.retired is not None and retired.retired.exists()
    assert not db.exists() and retired.retired.name.endswith("movido-20261022-200000")


def test_check_flags_a_damaged_file_without_touching_it(tmp_path: Path) -> None:
    damaged = tmp_path / "neoson.db"
    damaged.write_bytes(b"isto nao e um banco SQLite" * 200)
    before = damaged.read_bytes()
    assert codes(check(damaged, tmp_path / "backups"))["integrity"] == "error"
    assert damaged.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["neoson.db"]


def test_relocate_removes_only_its_copies_when_documents_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database(tmp_path / "dados" / "neoson.db")
    docs = tmp_path / "dados" / "documents"
    docs.mkdir()
    (docs / "abc.md").write_text("original", encoding="utf-8")
    real_copytree = shutil.copytree

    def damaged_copy(source: Path, destination: Path) -> Path:
        real_copytree(source, destination)
        (destination / "abc.md").write_text("estragado", encoding="utf-8")
        return destination

    monkeypatch.setattr(shutil, "copytree", damaged_copy)
    target = tmp_path / "novo"
    with pytest.raises(MaintenanceError, match="documentos não confere"):
        relocate(db, target, docs)
    assert list(target.iterdir()) == []
    assert db.exists() and (docs / "abc.md").read_text(encoding="utf-8") == "original"


def test_relocate_refuses_a_database_being_written(tmp_path: Path) -> None:
    db = database(tmp_path / "neoson.db")
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(MaintenanceError, match="em uso"):
            relocate(db, tmp_path / "novo")
        writer.execute("ROLLBACK")
    assert not (tmp_path / "novo" / "neoson.db").exists()


def test_backup_includes_documents_and_stays_compatible(tmp_path: Path) -> None:
    db = database(tmp_path / "neoson.db")
    vault, docs = tmp_path / "vault", tmp_path / "documents"
    vault.mkdir()
    docs.mkdir()
    (docs / "a.md").write_text("um", encoding="utf-8")
    (docs / "original.tmp").write_text("conteudo", encoding="utf-8")
    info = backup.create(db, vault, tmp_path / "backups", docs)
    assert info.document_files == 2
    (info.path / "documents" / "a.md").write_text("alterado", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="documentos"):
        backup.verify(info.path)
    (info.path / "documents" / "a.md").write_text("um", encoding="utf-8")
    kept = backup.restore(info.path, db, vault, force=True, documents_path=docs)
    assert any("documents.antes-da-restauracao" in p.name for p in kept)
    assert (docs / "a.md").read_text(encoding="utf-8") == "um"
    assert (docs / "original.tmp").read_text(encoding="utf-8") == "conteudo"
    # Manifesto antigo, sem documentos: continua válido e não mexe na pasta.
    manifest = info.path / backup.MANIFEST
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data.pop("documents")
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert backup.verify(info.path).document_files == 0
    (docs / "a.md").write_text("atual", encoding="utf-8")
    backup.restore(info.path, db, vault, force=True, documents_path=docs)
    assert (docs / "a.md").read_text(encoding="utf-8") == "atual"


def test_cli_check_and_relocate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    db = database(tmp_path / "neoson.db")
    with closing(connect(db)) as c:
        migrate(c)
    monkeypatch.setenv("NEOSON_DB_PATH", str(db))
    monkeypatch.setenv("NEOSON_BACKUP_PATH", str(tmp_path / "backups"))
    monkeypatch.setenv("NEOSON_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("NEOSON_DOCUMENTS_PATH", str(tmp_path / "docs"))
    assert maintenance_main.main(["check"]) == 0
    assert "[atenção] Nenhum backup" in capsys.readouterr().out
    assert backup_main.main(["create"]) == 0
    assert "documentos: 0 original(is)" in capsys.readouterr().out
    target = tmp_path / "novo"
    assert maintenance_main.main(["relocate", "--to", str(target)]) == 0
    out = capsys.readouterr().out
    assert f"NEOSON_DB_PATH={target / 'neoson.db'}" in out
    assert maintenance_main.main(["relocate", "--to", str(target)]) == 1


def test_every_console_script_also_runs_with_python_m() -> None:
    # O roteiro da janela usa `python -m` enquanto o `uv sync` não roda.
    root = Path(__file__).resolve().parents[1]
    missing = []
    for project in sorted((root / "apps").glob("*/pyproject.toml")):
        scripts = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
        for target in scripts.get("scripts", {}).values():
            module = target.split(":")[0].replace(".", "/") + ".py"
            source = project.parent / "src" / module
            if '__name__ == "__main__"' not in source.read_text(encoding="utf-8"):
                missing.append(module)
    assert missing == []
