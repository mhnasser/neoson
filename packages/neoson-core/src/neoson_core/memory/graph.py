"""Grafo de conhecimento em SQLite: implementação de referência da porta.

Sem servidor extra (§20); o Neo4j é um adaptador opcional (`neo4j_graph`).
"""

import asyncio
import sqlite3
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from neoson_core.engine.graph import (
    Entity,
    EntityKind,
    Fact,
    FactStatus,
    Provenance,
    Relation,
    allowed_status,
    check_limit,
    clean_reason,
    name_key,
    search_key,
    status_values,
)
from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction

_SELECT = (
    "SELECT f.relation, f.source_id, f.observed_at, f.confidence, f.valid_from,"
    " f.valid_to, f.status, s.kind, s.name, o.kind, o.name FROM graph_facts f"
    " JOIN graph_entities s ON s.id = f.subject_id"
    " JOIN graph_entities o ON o.id = f.object_id"
)


class SqliteGraphStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        with self._connection() as connection:
            migrate(connection)

    async def upsert(self, facts: Sequence[Fact]) -> int:
        batch = tuple(facts)
        if not all(isinstance(fact, Fact) for fact in batch):
            raise ValueError("Somente fatos validados entram no grafo.")
        return await asyncio.to_thread(self._upsert, batch)

    async def set_status(
        self, fact_id: str, status: FactStatus, reason: str | None = None
    ) -> bool:
        target, text = FactStatus(status), clean_reason(reason)
        return await asyncio.to_thread(self._set_status, fact_id, target, text)

    async def facts_about(
        self,
        entity_id: str,
        *,
        limit: int,
        statuses: Collection[FactStatus] = (FactStatus.ACTIVE,),
    ) -> Sequence[Fact]:
        check_limit(limit)
        wanted = status_values(statuses)
        return await asyncio.to_thread(self._facts_about, entity_id, limit, wanted)

    async def find_entities(self, text: str, *, limit: int) -> Sequence[Entity]:
        check_limit(limit)
        return await asyncio.to_thread(self._find, search_key(text), limit)

    async def list_facts(
        self,
        *,
        statuses: Collection[FactStatus],
        limit: int,
        source_id: str | None = None,
    ) -> Sequence[Fact]:
        check_limit(limit)
        wanted = status_values(statuses)
        return await asyncio.to_thread(self._list, wanted, limit, source_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self._path)
        try:
            yield connection
        finally:
            connection.close()

    def _upsert(self, facts: tuple[Fact, ...]) -> int:
        now = now_iso()
        added = 0
        with self._connection() as connection, transaction(connection):
            for fact in facts:
                for entity in (fact.subject, fact.object):
                    # O primeiro nome observado vira o de exibição.
                    connection.execute(
                        "INSERT INTO graph_entities (id, kind, name, name_key,"
                        " created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        (
                            entity.id,
                            entity.kind.value,
                            entity.name,
                            name_key(entity.name),
                            now,
                        ),
                    )
                source = fact.provenance
                cursor = connection.execute(
                    "INSERT INTO graph_facts (id, subject_id, relation, object_id,"
                    " source_id, observed_at, confidence, valid_from, valid_to,"
                    " status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (id) DO NOTHING",
                    (
                        fact.id,
                        fact.subject.id,
                        fact.relation.value,
                        fact.object.id,
                        source.source_id,
                        _iso(source.observed_at),
                        float(source.confidence),
                        _iso(source.valid_from),
                        _iso(source.valid_to),
                        fact.status.value,
                        now,
                        now,
                    ),
                )
                added += cursor.rowcount
        return added

    def _set_status(self, fact_id: str, target: FactStatus, reason: str | None) -> bool:
        with self._connection() as connection, transaction(connection):
            row = connection.execute(
                "SELECT status FROM graph_facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False
            current = FactStatus(row[0])
            if current is target:
                return True
            if not allowed_status(current, target):
                return False
            connection.execute(
                "UPDATE graph_facts SET status = ?, reason = ?, updated_at = ?"
                " WHERE id = ?",
                (target.value, reason, now_iso(), fact_id),
            )
            return True

    def _facts_about(
        self, entity_id: str, limit: int, statuses: tuple[str, ...]
    ) -> list[Fact]:
        marks = ", ".join("?" * len(statuses))
        with self._connection() as connection:
            rows = connection.execute(
                _SELECT + " WHERE (f.subject_id = ? OR f.object_id = ?)"
                f" AND f.status IN ({marks})"
                " ORDER BY f.confidence DESC, f.observed_at DESC, f.id LIMIT ?",
                (entity_id, entity_id, *statuses, limit),
            ).fetchall()
        return [_fact(row) for row in rows]

    def _list(
        self, statuses: tuple[str, ...], limit: int, source_id: str | None
    ) -> list[Fact]:
        marks = ", ".join("?" * len(statuses))
        where = f"f.status IN ({marks})"
        params: list[object] = [*statuses]
        if source_id is not None:
            where += " AND f.source_id = ?"
            params.append(source_id)
        with self._connection() as connection:
            rows = connection.execute(
                _SELECT + f" WHERE {where} ORDER BY f.observed_at DESC, f.id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_fact(row) for row in rows]

    def _find(self, key: str, limit: int) -> list[Entity]:
        escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT kind, name FROM graph_entities"
                " WHERE name_key LIKE ? ESCAPE '\\'"
                " ORDER BY length(name_key), name_key, id LIMIT ?",
                ("%" + escaped + "%", limit),
            ).fetchall()
        return [Entity(EntityKind(kind), name) for kind, name in rows]


def _iso(moment: datetime | None) -> str | None:
    # Em UTC, para que a ordenação textual siga o tempo.
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _date(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _fact(row: sqlite3.Row) -> Fact:
    observed = datetime.fromisoformat(row[2])
    return Fact(
        subject=Entity(EntityKind(row[7]), row[8]),
        relation=Relation(row[0]),
        object=Entity(EntityKind(row[9]), row[10]),
        provenance=Provenance(
            source_id=row[1],
            observed_at=observed,
            confidence=row[3],
            valid_from=_date(row[4]),
            valid_to=_date(row[5]),
        ),
        status=FactStatus(row[6]),
    )
