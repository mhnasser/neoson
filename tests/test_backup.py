import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from neoson_core.engine import Message, Role, RunRecord, RunStatus
from neoson_core.infrastructure import backup
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_worker.backup_main import main

WHEN = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def seed(db: Path, vault: Path) -> UUID:
    store = SqliteConversationStore(db)
    session = asyncio.run(store.create_session("http"))
    asyncio.run(
        store.record_run(
            RunRecord(
                run_id=uuid4(),
                session_id=session,
                status=RunStatus.COMPLETED,
                started_at=WHEN,
                ended_at=WHEN,
                messages=(
                    Message(role=Role.USER, content="oi"),
                    Message(role=Role.ASSISTANT, content="olá"),
                ),
            )
        )
    )
    (vault / "Dashboard").mkdir(parents=True)
    (vault / "Dashboard" / "Home.md").write_text("# Home\n", encoding="utf-8")
    (vault / "Dashboard" / ".Home.md.tmp").write_text("parcial", encoding="utf-8")
    return session


@pytest.fixture
def data(tmp_path: Path) -> tuple[Path, Path, UUID]:
    db, vault = tmp_path / "dados" / "neoson.db", tmp_path / "dados" / "vault"
    return db, vault, seed(db, vault)


def history(db: Path, session: UUID) -> list[str]:
    store = SqliteConversationStore(db)
    return [m.content for m in asyncio.run(store.load_history(session, 10))]


def test_backup_with_the_database_in_use_is_complete_and_verified(
    data: tuple[Path, Path, UUID], tmp_path: Path
) -> None:
    db, vault, _ = data
    in_use = connect(db)  # conexão aberta, como a de um gateway em execução
    try:
        info = backup.create(db, vault, tmp_path / "backups")
    finally:
        in_use.close()

    manifest = json.loads((info.path / backup.MANIFEST).read_text(encoding="utf-8"))
    assert info.path.name.startswith("neoson-backup-")
    assert info.counts["sessions"] == 1 and info.counts["messages"] == 2
    assert info.schema_version == manifest["schema_version"] >= 5
    assert list(manifest["vault"]) == ["Dashboard/Home.md"]  # sem o .tmp
    assert backup.verify(info.path).vault_files == 1


@pytest.mark.parametrize("target", ["db", "vault", "manifest"])
def test_verify_detects_a_changed_or_missing_file(
    data: tuple[Path, Path, UUID], tmp_path: Path, target: str
) -> None:
    db, vault, _ = data
    info = backup.create(db, vault, tmp_path / "backups")
    if target == "db":
        with (info.path / backup.DB_FILE).open("ab") as stream:
            stream.write(b"lixo")
    elif target == "vault":
        (info.path / "vault" / "Dashboard" / "Home.md").write_text(
            "mudou", encoding="utf-8"
        )
    else:
        (info.path / backup.MANIFEST).unlink()

    with pytest.raises(backup.BackupError):
        backup.verify(info.path)


def test_restore_into_an_empty_place_brings_the_history_back(
    data: tuple[Path, Path, UUID], tmp_path: Path
) -> None:
    db, vault, session = data
    info = backup.create(db, vault, tmp_path / "backups")
    new_db, new_vault = tmp_path / "nova" / "neoson.db", tmp_path / "nova" / "vault"

    kept = backup.restore(info.path, new_db, new_vault)

    assert kept == []
    assert history(new_db, session) == ["oi", "olá"]
    assert (new_vault / "Dashboard" / "Home.md").read_text(
        encoding="utf-8"
    ) == "# Home\n"


def test_restore_refuses_existing_data_and_force_keeps_it_aside(
    data: tuple[Path, Path, UUID], tmp_path: Path
) -> None:
    db, vault, session = data
    info = backup.create(db, vault, tmp_path / "backups")
    after_backup = asyncio.run(SqliteConversationStore(db).create_session("http"))

    with pytest.raises(backup.BackupError, match="--force"):
        backup.restore(info.path, db, vault)
    kept = backup.restore(info.path, db, vault, force=True)

    assert {p.name.split(".antes")[0] for p in kept} == {"neoson.db", "vault"}
    assert history(db, session) == ["oi", "olá"]
    assert not asyncio.run(SqliteConversationStore(db).session_exists(after_backup))
    aside = next(p for p in kept if p.name.startswith("neoson.db.antes"))
    # O que só existia depois do backup continua na cópia guardada de lado.
    assert asyncio.run(SqliteConversationStore(aside).session_exists(after_backup))


def test_backup_without_a_database_fails(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupError):
        backup.create(tmp_path / "nao-existe.db", tmp_path / "vault", tmp_path / "b")


def test_command_line_create_verify_and_restore(
    data: tuple[Path, Path, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db, vault, session = data
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_DB_PATH", str(db))
    monkeypatch.setenv("NEOSON_VAULT_PATH", str(vault))
    monkeypatch.setenv("NEOSON_BACKUP_PATH", str(tmp_path / "backups"))

    assert main(["create"]) == 0
    (created,) = (tmp_path / "backups").iterdir()
    assert main(["verify", str(created)]) == 0
    assert main(["restore", str(created)]) == 1  # já há dados no destino
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "teste" / "neoson.db"))
    monkeypatch.setenv("NEOSON_VAULT_PATH", str(tmp_path / "teste" / "vault"))
    assert main(["restore", str(created)]) == 0

    output = capsys.readouterr()
    assert "Backup íntegro." in output.out
    assert "--force" in output.err
    assert history(tmp_path / "teste" / "neoson.db", session) == ["oi", "olá"]
