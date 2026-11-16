"""Checagens e mudança de lugar dos dados para a janela de manutenção (§18).

Nada aqui apaga dados: a mudança copia pela API de backup do SQLite, confere a
cópia e deixa o original onde está (ou renomeado, quando pedido).
"""

import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from neoson_core.infrastructure.backup import COUNTED_TABLES, MANIFEST, _sha256
from neoson_core.infrastructure.db import (
    backup,
    connect,
    integrity_ok,
    migrations,
    schema_version,
)

# Pastas que clientes de sincronização mantêm na nuvem.
SYNCED = ("onedrive", "dropbox", "google drive", "googledrive", "icloud")
BACKUP_MAX_AGE = timedelta(hours=24)

Level = Literal["ok", "warning", "error"]


@dataclass(frozen=True, slots=True)
class Finding:
    level: Level
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Relocation:
    db_path: Path
    documents_path: Path | None
    retired: Path | None


class MaintenanceError(Exception):
    pass


def synced_folder(path: Path) -> bool:
    return any(name in part.lower() for part in path.resolve().parts for name in SYNCED)


def check(
    db_path: Path,
    backup_path: Path,
    documents_path: Path | None = None,
    now: datetime | None = None,
) -> list[Finding]:
    """Estado antes da janela; nunca cria nem altera o banco."""
    moment = now or datetime.now(UTC)
    findings: list[Finding] = []
    if not db_path.is_file():
        return [
            Finding("error", "database_missing", f"Banco não encontrado: {db_path}")
        ]
    # Antes de abrir: fechar a última conexão faria o SQLite apagar o -wal.
    if db_path.with_name(db_path.name + "-wal").exists():
        findings.append(
            Finding(
                "warning",
                "in_use",
                "Há um arquivo -wal: algum processo pode estar usando o banco. "
                "Pare gateway, worker e bot antes de mudar ou restaurar.",
            )
        )
    latest = max((m.version for m in migrations()), default=0)
    with closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as c:
        try:
            current = schema_version(c)
        except sqlite3.Error:
            current = 0
        try:
            intact = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.Error:
            intact = False
    if current < latest:
        pending = ", ".join(str(m.version) for m in migrations() if m.version > current)
        findings.append(
            Finding(
                "warning",
                "migrations_pending",
                f"Esquema {current}; o código está em {latest}. As migrações {pending} "
                "rodam no próximo início: faça backup antes.",
            )
        )
    elif current > latest:
        findings.append(
            Finding(
                "error",
                "schema_newer",
                f"Esquema {current} é mais novo que o código ({latest}).",
            )
        )
    else:
        findings.append(Finding("ok", "migrations_current", f"Esquema {current}."))
    if not intact:
        findings.append(Finding("error", "integrity", "integrity_check falhou."))
    for label, path in (("do banco", db_path), ("dos documentos", documents_path)):
        if path is not None and synced_folder(path):
            findings.append(
                Finding(
                    "warning",
                    "synced_folder",
                    f"A pasta {label} é sincronizada com a nuvem ({path}). "
                    "Use `relocate` para tirá-la de lá.",
                )
            )
    findings.append(_last_backup(backup_path, moment))
    return findings


def relocate(
    db_path: Path,
    target: Path,
    documents_path: Path | None = None,
    *,
    retire_original: bool = False,
    now: datetime | None = None,
) -> Relocation:
    """Copia banco (e documentos) para `target` e confere a cópia."""
    new_db = target / db_path.name
    new_docs = target / "documents" if documents_path is not None else None
    if not db_path.is_file():
        raise MaintenanceError(f"Banco não encontrado: {db_path}")
    if target.resolve() == db_path.parent.resolve():
        raise MaintenanceError("O destino é a pasta atual do banco.")
    if documents_path is not None and target.resolve().is_relative_to(
        documents_path.resolve()
    ):
        raise MaintenanceError("O destino não pode ficar dentro dos documentos.")
    if new_db.exists() or (new_docs is not None and new_docs.exists()):
        raise MaintenanceError(f"Já existem dados em {target}; escolha outra pasta.")
    _not_locked(db_path)
    target.mkdir(parents=True, exist_ok=True)
    backup(db_path, new_db)
    if not integrity_ok(new_db) or _counts(new_db) != _counts(db_path):
        new_db.unlink(missing_ok=True)
        raise MaintenanceError("A cópia do banco não confere; nada foi mudado.")
    if documents_path is not None and new_docs is not None:
        if documents_path.exists():
            shutil.copytree(documents_path, new_docs)
            if _files(new_docs) != _files(documents_path):
                # Só as cópias recém-criadas saem; o original fica intacto.
                shutil.rmtree(new_docs)
                new_db.unlink(missing_ok=True)
                raise MaintenanceError(
                    "A cópia dos documentos não confere; nada foi mudado."
                )
        else:
            new_docs.mkdir()
    retired = None
    if retire_original:
        stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
        retired = db_path.with_name(f"{db_path.name}.movido-{stamp}")
        db_path.rename(retired)
    return Relocation(new_db, new_docs, retired)


def _not_locked(db_path: Path) -> None:
    """Uma escrita em curso por outro processo impede a mudança."""
    with closing(connect(db_path)) as c:
        c.execute("PRAGMA busy_timeout = 0")
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("ROLLBACK")
        except sqlite3.OperationalError:
            raise MaintenanceError(
                "O banco está em uso por outro processo; pare os serviços."
            ) from None


def _counts(db: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        return {
            t: int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in COUNTED_TABLES
            if t in tables
        }


def _files(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): _sha256(p)
        for p in root.rglob("*")
        if p.is_file()
    }


def _last_backup(backup_path: Path, now: datetime) -> Finding:
    newest: datetime | None = None
    if backup_path.is_dir():
        for folder in backup_path.iterdir():
            manifest = folder / MANIFEST
            if not manifest.is_file():
                continue
            try:
                created = datetime.fromisoformat(
                    json.loads(manifest.read_text(encoding="utf-8"))["created_at"]
                )
            except (ValueError, KeyError, TypeError):
                continue
            newest = created if newest is None or created > newest else newest
    if newest is None:
        return Finding("warning", "no_backup", f"Nenhum backup em {backup_path}.")
    age = now - newest
    if age > BACKUP_MAX_AGE:
        return Finding(
            "warning",
            "backup_old",
            f"Último backup de {newest:%Y-%m-%d %H:%M} UTC; faça um novo antes.",
        )
    return Finding(
        "ok", "backup_recent", f"Último backup de {newest:%Y-%m-%d %H:%M} UTC."
    )
