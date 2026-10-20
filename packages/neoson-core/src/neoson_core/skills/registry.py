"""Registro SQLite de cópias aprovadas; o motor recebe só a porta de leitura."""

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from neoson_core.engine import OutboxEvent
from neoson_core.engine.skills import Skill, SkillNotFound, SkillRef
from neoson_core.engine.skills import SkillIntegrityError as SkillIntegrityError
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction
from neoson_core.skills.loader import (
    SkillInvalid,
    SkillPackage,
    package_from_texts,
)


class SkillConflict(ValueError):
    """Uma versão já aprovada não pode receber outro conteúdo."""


@dataclass(frozen=True, slots=True)
class SkillVersion:
    ref: SkillRef
    checksum: str
    status: Literal["active", "approved", "retired"]
    approved_at: datetime


class SqliteSkillRegistry:
    """Aprovação explícita pela CLI; leitura assíncrona via `SkillRegistry`."""

    def __init__(self, path: Path) -> None:
        self._path = path
        with self._connection() as connection:
            migrate(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self._path)
        try:
            yield connection
        finally:
            connection.close()

    async def index(self) -> list[SkillRef]:
        return await asyncio.to_thread(self._index)

    async def load_approved(self, skill_id: str, version: str) -> Skill:
        return await asyncio.to_thread(self._load_approved, skill_id, version)

    def approve(self, package: SkillPackage) -> SkillVersion:
        # Valida os mesmos bytes que serão armazenados, sem reler a pasta.
        snapshot = package_from_texts(package.path, package.contents)
        if snapshot.checksum != package.checksum:
            raise SkillIntegrityError("A cópia da skill mudou após a validação.")
        skill_id, version = snapshot.metadata.id, snapshot.metadata.version
        with self._connection() as connection, transaction(connection):
            previous = connection.execute(
                "SELECT * FROM skills WHERE id = ? AND version = ?",
                (skill_id, version),
            ).fetchone()
            if previous is not None:
                _package(previous)
                if previous["checksum"] != snapshot.checksum:
                    raise SkillConflict(
                        "Versão já aprovada com outro conteúdo; use uma nova versão."
                    )
                if previous["status"] == "active":
                    return _version(previous)
            connection.execute(
                "UPDATE skills SET status = 'approved'"
                " WHERE id = ? AND status = 'active'",
                (skill_id,),
            )
            if previous is None:
                connection.execute(
                    "INSERT INTO skills (id, version, checksum, files_json, status,"
                    " approved_at) VALUES (?, ?, ?, ?, 'active', ?)",
                    (
                        skill_id,
                        version,
                        snapshot.checksum,
                        json.dumps(dict(snapshot.contents), ensure_ascii=False),
                        now_iso(),
                    ),
                )
            else:
                connection.execute(
                    "UPDATE skills SET status = 'active' WHERE id = ? AND version = ?",
                    (skill_id, version),
                )
            _event(connection, "approved", skill_id, version, snapshot.checksum)
            row = connection.execute(
                "SELECT * FROM skills WHERE id = ? AND version = ?", (skill_id, version)
            ).fetchone()
            return _version(row)

    def versions(self) -> list[SkillVersion]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM skills ORDER BY id, approval_id"
            ).fetchall()
        return [_version(row) for row in rows]

    def approved_package(self, skill_id: str) -> SkillPackage:
        """Snapshot integral ativo, incluindo as fixtures, validado pelo checksum."""
        with self._connection() as connection:
            return _package(_active(connection, skill_id))

    def rollback(self, skill_id: str) -> SkillVersion:
        """Volta à aprovação anterior à ativa, sem apagar ou editar versões."""
        with self._connection() as connection, transaction(connection):
            active = _active(connection, skill_id)
            previous = connection.execute(
                "SELECT * FROM skills WHERE id = ? AND approval_id < ?"
                " AND status = 'approved' ORDER BY approval_id DESC LIMIT 1",
                (skill_id, active["approval_id"]),
            ).fetchone()
            if previous is None:
                raise SkillNotFound("Não há versão aprovada anterior para rollback.")
            result = _version(previous)
            connection.execute(
                "UPDATE skills SET status = 'approved' WHERE approval_id = ?",
                (active["approval_id"],),
            )
            connection.execute(
                "UPDATE skills SET status = 'active' WHERE approval_id = ?",
                (previous["approval_id"],),
            )
            _event(
                connection, "rolled_back", skill_id, result.ref.version, result.checksum
            )
            return SkillVersion(
                result.ref, result.checksum, "active", result.approved_at
            )

    def retire(self, skill_id: str) -> None:
        with self._connection() as connection, transaction(connection):
            active = _active(connection, skill_id)
            connection.execute(
                "UPDATE skills SET status = 'retired' WHERE approval_id = ?",
                (active["approval_id"],),
            )
            _event(
                connection, "retired", skill_id, active["version"], active["checksum"]
            )

    def _index(self) -> list[SkillRef]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM skills WHERE status = 'active' ORDER BY id"
            ).fetchall()
        return [_package(row).skill().ref for row in rows]

    def _load_approved(self, skill_id: str, version: str) -> Skill:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM skills WHERE id = ? AND version = ?"
                " AND status = 'active'",
                (skill_id, version),
            ).fetchone()
        if row is None:
            raise SkillNotFound("Skill ou versão não está ativa no registro.")
        return _package(row).skill()


def _active(connection: sqlite3.Connection, skill_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM skills WHERE id = ? AND status = 'active'", (skill_id,)
    ).fetchone()
    if row is None:
        raise SkillNotFound("Skill não está ativa no registro.")
    return row


def _package(row: sqlite3.Row) -> SkillPackage:
    try:
        contents = json.loads(row["files_json"])
        if not isinstance(contents, dict) or not all(
            isinstance(name, str) and isinstance(text, str)
            for name, text in contents.items()
        ):
            raise ValueError("Formato inválido.")
        package = package_from_texts(Path(row["id"]), contents)
        if (package.metadata.id, package.metadata.version, package.checksum) != (
            row["id"],
            row["version"],
            row["checksum"],
        ):
            raise ValueError("Checksum ou identidade diferente.")
    except (ValueError, SkillInvalid):
        raise SkillIntegrityError(
            "Cópia aprovada inválida ou adulterada no registro."
        ) from None
    return package


def _version(row: sqlite3.Row) -> SkillVersion:
    return SkillVersion(
        _package(row).skill().ref,
        row["checksum"],
        row["status"],
        datetime.fromisoformat(row["approved_at"]),
    )


def _event(
    connection: sqlite3.Connection,
    action: str,
    skill_id: str,
    version: str,
    digest: str,
) -> None:
    outbox.insert(
        connection,
        OutboxEvent(
            type=f"neoson.skill.{action}",
            payload={"skill_id": skill_id, "version": version, "checksum": digest},
        ),
    )
