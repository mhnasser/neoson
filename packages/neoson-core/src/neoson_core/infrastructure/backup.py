"""Backup e restauração do SQLite e do vault (§18, ADR-002).

Cada backup é uma pasta com `neoson.db` (cópia consistente pela API de backup),
`vault/` e `manifest.json` (hashes, versão do esquema e contagens). Segredos
(`neoson.env`) não entram no backup.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from neoson_core.infrastructure.db import backup as sqlite_backup
from neoson_core.infrastructure.db import integrity_ok, schema_version

MANIFEST = "manifest.json"
DB_FILE = "neoson.db"
VAULT_DIR = "vault"
COUNTED_TABLES = (
    "sessions",
    "messages",
    "runs",
    "outbox_events",
    "memory_facts",
    "graph_entities",
    "graph_facts",
)


class BackupError(Exception):
    """Backup ausente, incompleto ou alterado."""


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    created_at: datetime
    schema_version: int
    counts: Mapping[str, int]
    vault_files: int


def create(db_path: Path, vault_path: Path, destination: Path) -> BackupInfo:
    """Cria `destination/neoson-backup-AAAAMMDD-HHMMSS` e devolve o resumo."""
    if not db_path.exists():
        raise BackupError(f"Banco não encontrado: {db_path}")
    created = datetime.now(UTC)
    target = destination / f"neoson-backup-{created:%Y%m%d-%H%M%S}"
    if target.exists():
        raise BackupError(f"Já existe um backup em {target}")
    target.mkdir(parents=True)

    sqlite_backup(db_path, target / DB_FILE)
    if vault_path.exists():
        shutil.copytree(vault_path, target / VAULT_DIR, ignore=_ignore_temporary)
    else:
        (target / VAULT_DIR).mkdir()

    manifest = {
        "format": 1,
        "created_at": created.isoformat(),
        "schema_version": _schema_version(target / DB_FILE),
        "counts": _counts(target / DB_FILE),
        "database_sha256": _sha256(target / DB_FILE),
        "vault": _vault_hashes(target / VAULT_DIR),
    }
    (target / MANIFEST).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return verify(target)


def verify(path: Path) -> BackupInfo:
    """Confere manifesto, integridade do banco e hashes; `BackupError` se falhar."""
    manifest_file = path / MANIFEST
    if not manifest_file.exists():
        raise BackupError(f"Sem {MANIFEST} em {path}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    db_file = path / DB_FILE
    if not db_file.exists() or _sha256(db_file) != manifest["database_sha256"]:
        raise BackupError("Banco ausente ou diferente do manifesto.")
    if not integrity_ok(db_file):
        raise BackupError("Banco do backup falhou no integrity_check.")
    if _vault_hashes(path / VAULT_DIR) != manifest["vault"]:
        raise BackupError("Arquivos do vault diferentes do manifesto.")
    return BackupInfo(
        path=path,
        created_at=datetime.fromisoformat(manifest["created_at"]),
        schema_version=manifest["schema_version"],
        counts=manifest["counts"],
        vault_files=len(manifest["vault"]),
    )


def restore(
    path: Path, db_path: Path, vault_path: Path, force: bool = False
) -> list[Path]:
    """Restaura um backup verificado; devolve o que foi guardado de lado.

    Sem `force`, recusa se já houver banco ou vault no destino. Com `force`, os
    atuais são renomeados (`*.antes-da-restauracao-…`), nunca apagados. Pare o
    gateway e o worker antes.
    """
    info = verify(path)
    existing = [p for p in (db_path, vault_path) if p.exists()]
    if existing and not force:
        raise BackupError(
            "Já existem dados no destino: "
            + ", ".join(str(p) for p in existing)
            + ". Use --force para guardá-los de lado e restaurar."
        )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if db_path.exists():
        # Traz o conteúdo do WAL para o arquivo principal: com outro nome, o SQLite
        # não emparelharia a cópia guardada com o `-wal` dela.
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    kept = []
    for current in [*_db_files(db_path), vault_path]:
        if current.exists():
            aside = current.with_name(f"{current.name}.antes-da-restauracao-{stamp}")
            current.rename(aside)
            kept.append(aside)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_backup(path / DB_FILE, db_path)
    shutil.copytree(path / VAULT_DIR, vault_path)
    if not integrity_ok(db_path) or _schema_version(db_path) != info.schema_version:
        raise BackupError("O banco restaurado não confere com o backup.")
    return kept


def _db_files(db_path: Path) -> list[Path]:
    return [db_path, *(db_path.with_name(db_path.name + s) for s in ("-wal", "-shm"))]


def _schema_version(db_file: Path) -> int:
    connection = sqlite3.connect(db_file)
    try:
        return schema_version(connection)
    finally:
        connection.close()


def _counts(db_file: Path) -> dict[str, int]:
    connection = sqlite3.connect(db_file)
    try:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in COUNTED_TABLES
            if table in existing
        }
    finally:
        connection.close()


def _vault_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        file.relative_to(root).as_posix(): _sha256(file)
        for file in sorted(root.rglob("*"))
        if file.is_file() and not file.name.endswith(".tmp")
    }


def _sha256(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ignore_temporary(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name.endswith(".tmp")}
