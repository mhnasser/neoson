"""Conexão SQLite, migrations versionadas e backup consistente (ADR-002)."""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


def migrations() -> list[Migration]:
    """Migrations do pacote, em ordem de versão."""
    found = []
    for resource in files("neoson_core.infrastructure.migrations").iterdir():
        match = _MIGRATION_NAME.match(resource.name)
        if match:
            found.append(
                Migration(
                    int(match.group(1)), resource.name, resource.read_text("utf-8")
                )
            )
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise RuntimeError(f"Migrations fora de sequência: {versions}")
    return found


def connect(path: Path) -> sqlite3.Connection:
    """Conexão com chaves estrangeiras e WAL. O chamador fecha a conexão."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE` … `COMMIT`; qualquer exceção desfaz tudo."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def migrate(connection: sqlite3.Connection) -> list[int]:
    """Aplica as migrations pendentes e devolve as versões aplicadas agora."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {
        row[0] for row in connection.execute("SELECT version FROM schema_migrations")
    }
    newly_applied = []
    for migration in migrations():
        if migration.version in applied:
            continue
        with transaction(connection):
            for statement in _statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at)"
                " VALUES (?, ?, ?)",
                (migration.version, migration.name, now_iso()),
            )
        newly_applied.append(migration.version)
    return newly_applied


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def backup(source: Path, target: Path) -> None:
    """Cópia consistente do banco, mesmo em uso (API de backup do SQLite)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(source)
    destination = sqlite3.connect(target)
    try:
        origin.backup(destination)
    finally:
        destination.close()
        origin.close()


def integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        result: str = connection.execute("PRAGMA integrity_check").fetchone()[0]
        return result == "ok"
    finally:
        connection.close()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _statements(sql: str) -> list[str]:
    """Divide o arquivo em comandos completos (inclusive triggers com `BEGIN … END`)."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines():
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError("Migration termina com um comando incompleto.")
    return statements
