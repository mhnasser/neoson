"""Grafo de conhecimento no Neo4j: a mesma porta do SQLite, com consultas fixas.

Cada fato é um nó `NeosonFact` ligado ao sujeito e ao objeto, para ter unicidade por
ID também na edição Community. Nenhum texto do modelo vira Cypher: tipos, relações e
nomes vão sempre como parâmetros. O driver oficial é opcional (`neoson-core[neo4j]`) e
só é importado em `open_neo4j`.
"""

import asyncio
import importlib
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

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

SCHEMA = (
    "CREATE CONSTRAINT neoson_entity_id IF NOT EXISTS"
    " FOR (e:NeosonEntity) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT neoson_fact_id IF NOT EXISTS"
    " FOR (x:NeosonFact) REQUIRE x.id IS UNIQUE",
    "CREATE INDEX neoson_entity_name IF NOT EXISTS"
    " FOR (e:NeosonEntity) ON (e.name_key)",
    "CREATE INDEX neoson_fact_source IF NOT EXISTS FOR (x:NeosonFact) ON (x.source_id)",
)
# Só quem foi criado nesta chamada recebe o `batch` dela: assim se contam os novos.
UPSERT = """
UNWIND $facts AS f
MERGE (s:NeosonEntity {id: f.subject.id})
  ON CREATE SET s.kind = f.subject.kind, s.name = f.subject.name,
    s.name_key = f.subject.key
MERGE (o:NeosonEntity {id: f.object.id})
  ON CREATE SET o.kind = f.object.kind, o.name = f.object.name,
    o.name_key = f.object.key
MERGE (x:NeosonFact {id: f.id})
  ON CREATE SET x.relation = f.relation, x.source_id = f.source_id,
    x.observed_at = f.observed_at, x.confidence = f.confidence,
    x.valid_from = f.valid_from, x.valid_to = f.valid_to, x.status = f.status,
    x.created_at = $now, x.updated_at = $now, x.batch = $batch
MERGE (x)-[:SUBJECT]->(s)
MERGE (x)-[:OBJECT]->(o)
RETURN count(CASE WHEN x.batch = $batch THEN 1 END) AS added
"""
STATUS = "MATCH (x:NeosonFact {id: $id}) RETURN x.status AS status"
SET_STATUS = """
MATCH (x:NeosonFact {id: $id}) WHERE x.status = $current
SET x.status = $target, x.reason = $reason, x.updated_at = $now
RETURN count(x) AS changed
"""
_RETURN = """
MATCH (x)-[:SUBJECT]->(s:NeosonEntity), (x)-[:OBJECT]->(o:NeosonEntity)
RETURN x.relation AS relation, x.source_id AS source_id,
  x.observed_at AS observed_at, x.confidence AS confidence,
  x.valid_from AS valid_from, x.valid_to AS valid_to, x.status AS status,
  s.kind AS subject_kind, s.name AS subject_name,
  o.kind AS object_kind, o.name AS object_name
"""
FACTS_ABOUT = (
    """
MATCH (:NeosonEntity {id: $entity})<-[:SUBJECT|OBJECT]-(x:NeosonFact)
WHERE x.status IN $statuses
"""
    + _RETURN
    + "ORDER BY confidence DESC, observed_at DESC, x.id LIMIT $limit"
)
LIST = (
    """
MATCH (x:NeosonFact)
WHERE x.status IN $statuses AND ($source IS NULL OR x.source_id = $source)
"""
    + _RETURN
    + "ORDER BY observed_at DESC, x.id LIMIT $limit"
)
FIND = """
MATCH (e:NeosonEntity) WHERE e.name_key CONTAINS $key
RETURN e.kind AS kind, e.name AS name
ORDER BY size(e.name_key), e.name_key, e.id LIMIT $limit
"""
QUERIES = frozenset({*SCHEMA, UPSERT, STATUS, SET_STATUS, FACTS_ABOUT, LIST, FIND})


class CypherRunner(Protocol):
    async def read(
        self, query: str, parameters: Mapping[str, object]
    ) -> list[dict[str, Any]]: ...

    async def write(
        self, query: str, parameters: Mapping[str, object]
    ) -> list[dict[str, Any]]: ...


class GraphBackendError(RuntimeError):
    """Driver ausente ou servidor indisponível; a mensagem não traz credenciais."""


class Neo4jGraphStore:
    def __init__(self, runner: CypherRunner) -> None:
        self._runner = runner
        self._ready = False
        self._lock = asyncio.Lock()

    async def upsert(self, facts: Sequence[Fact]) -> int:
        batch = tuple(facts)
        if not all(isinstance(fact, Fact) for fact in batch):
            raise ValueError("Somente fatos validados entram no grafo.")
        unique = list({fact.id: fact for fact in batch}.values())
        if not unique:
            return 0
        rows = await self._write(
            UPSERT,
            {
                "facts": [_params(f) for f in unique],
                "now": _now(),
                "batch": uuid4().hex,
            },
        )
        return int(rows[0]["added"]) if rows else 0

    async def set_status(
        self, fact_id: str, status: FactStatus, reason: str | None = None
    ) -> bool:
        target, text = FactStatus(status), clean_reason(reason)
        rows = await self._read(STATUS, {"id": fact_id})
        if not rows:
            return False
        current = FactStatus(rows[0]["status"])
        if current is target:
            return True
        if not allowed_status(current, target):
            return False
        changed = await self._write(
            SET_STATUS,
            {
                "id": fact_id,
                "current": current.value,
                "target": target.value,
                "reason": text,
                "now": _now(),
            },
        )
        # Outra escrita mudou o estado antes: nada foi alterado aqui.
        return bool(changed) and int(changed[0]["changed"]) == 1

    async def facts_about(
        self,
        entity_id: str,
        *,
        limit: int,
        statuses: Collection[FactStatus] = (FactStatus.ACTIVE,),
    ) -> Sequence[Fact]:
        check_limit(limit)
        rows = await self._read(
            FACTS_ABOUT,
            {
                "entity": entity_id,
                "statuses": list(status_values(statuses)),
                "limit": limit,
            },
        )
        return [_fact(row) for row in rows]

    async def find_entities(self, text: str, *, limit: int) -> Sequence[Entity]:
        check_limit(limit)
        rows = await self._read(FIND, {"key": search_key(text), "limit": limit})
        return [Entity(EntityKind(row["kind"]), row["name"]) for row in rows]

    async def list_facts(
        self,
        *,
        statuses: Collection[FactStatus],
        limit: int,
        source_id: str | None = None,
    ) -> Sequence[Fact]:
        check_limit(limit)
        rows = await self._read(
            LIST,
            {
                "statuses": list(status_values(statuses)),
                "source": source_id,
                "limit": limit,
            },
        )
        return [_fact(row) for row in rows]

    async def _schema(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                for statement in SCHEMA:
                    await self._runner.write(statement, {})
                self._ready = True

    async def _read(
        self, query: str, parameters: Mapping[str, object]
    ) -> list[dict[str, Any]]:
        await self._schema()
        return await self._runner.read(query, parameters)

    async def _write(
        self, query: str, parameters: Mapping[str, object]
    ) -> list[dict[str, Any]]:
        await self._schema()
        return await self._runner.write(query, parameters)


class DriverRunner:
    """Executa pelo `execute_query` do driver assíncrono, com prazo por consulta."""

    def __init__(
        self,
        driver: Any,
        database: str,
        timeout_seconds: float,
        routing: tuple[Any, Any] = ("r", "w"),
    ) -> None:
        self._driver, self._database = driver, database
        self._timeout, self._routing = timeout_seconds, routing

    async def read(
        self, query: str, parameters: Mapping[str, object]
    ) -> list[dict[str, Any]]:
        return await self._run(query, parameters, self._routing[0])

    async def write(
        self, query: str, parameters: Mapping[str, object]
    ) -> list[dict[str, Any]]:
        return await self._run(query, parameters, self._routing[1])

    async def _run(
        self, query: str, parameters: Mapping[str, object], routing: Any
    ) -> list[dict[str, Any]]:
        if query not in QUERIES:
            raise ValueError("Consulta fora do conjunto fixo do adaptador.")
        try:
            result = await asyncio.wait_for(
                self._driver.execute_query(
                    query, dict(parameters), database_=self._database, routing_=routing
                ),
                self._timeout,
            )
        except TimeoutError:
            raise GraphBackendError("Neo4j não respondeu no prazo.") from None
        except Exception as error:
            # Só o tipo: a mensagem do driver pode citar URI ou usuário.
            raise GraphBackendError(f"Neo4j: {type(error).__name__}.") from None
        return [dict(record.data()) for record in result.records]

    async def close(self) -> None:
        await self._driver.close()


def open_neo4j(
    uri: str, user: str, password: str, database: str, timeout_seconds: float
) -> Neo4jGraphStore:
    try:
        neo4j = importlib.import_module("neo4j")
    except ImportError:
        raise GraphBackendError(
            "Driver do Neo4j ausente: instale com `uv sync --extra neo4j`."
        ) from None
    driver = neo4j.AsyncGraphDatabase.driver(
        uri, auth=(user, password), connection_timeout=timeout_seconds
    )
    routing = (neo4j.RoutingControl.READ, neo4j.RoutingControl.WRITE)
    return Neo4jGraphStore(DriverRunner(driver, database, timeout_seconds, routing))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _params(fact: Fact) -> dict[str, object]:
    source = fact.provenance
    return {
        "id": fact.id,
        "subject": _entity(fact.subject),
        "object": _entity(fact.object),
        "relation": fact.relation.value,
        "source_id": source.source_id,
        "observed_at": _iso(source.observed_at),
        "confidence": float(source.confidence),
        "valid_from": _iso(source.valid_from),
        "valid_to": _iso(source.valid_to),
        "status": fact.status.value,
    }


def _entity(entity: Entity) -> dict[str, str]:
    return {
        "id": entity.id,
        "kind": entity.kind.value,
        "name": entity.name,
        "key": name_key(entity.name),
    }


def _date(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Data do fato inválida.")
    return datetime.fromisoformat(value)


def _fact(row: Mapping[str, Any]) -> Fact:
    observed = _date(row["observed_at"])
    if observed is None:
        raise ValueError("Fato sem data de observação.")
    return Fact(
        subject=Entity(EntityKind(row["subject_kind"]), row["subject_name"]),
        relation=Relation(row["relation"]),
        object=Entity(EntityKind(row["object_kind"]), row["object_name"]),
        provenance=Provenance(
            source_id=row["source_id"],
            observed_at=observed,
            confidence=row["confidence"],
            valid_from=_date(row["valid_from"]),
            valid_to=_date(row["valid_to"]),
        ),
        status=FactStatus(row["status"]),
    )
