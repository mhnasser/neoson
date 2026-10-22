"""Mesma suíte para todo adaptador de `GraphStore`.

O Neo4j roda com um executor simulado que só entende as consultas fixas do
adaptador; com `NEOSON_NEO4J_TESTS=1` e um servidor descartável, roda de verdade.
"""

import asyncio
import os
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from neoson_core.engine.graph import (
    Entity,
    EntityKind,
    Fact,
    FactStatus,
    GraphStore,
    Provenance,
    Relation,
)
from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_core.memory import neo4j_graph
from neoson_core.memory.graph import SqliteGraphStore
from neoson_core.memory.graph_backend import graph_store
from neoson_core.memory.neo4j_graph import (
    DriverRunner,
    GraphBackendError,
    Neo4jGraphStore,
    open_neo4j,
)

SEEN = datetime(2026, 10, 22, 9, tzinfo=UTC)


class FakeRunner:
    """Interpreta só as consultas constantes do adaptador, em memória."""

    def __init__(self) -> None:
        self.entities: dict[str, dict[str, str]] = {}
        self.facts: dict[str, dict[str, Any]] = {}
        self.queries: list[str] = []

    async def read(
        self, query: str, parameters: Mapping[str, object]
    ) -> list[dict[str, Any]]:
        return self._run(query, dict(parameters))

    async def write(
        self, query: str, parameters: Mapping[str, object]
    ) -> list[dict[str, Any]]:
        return self._run(query, dict(parameters))

    def _run(self, query: str, p: dict[str, Any]) -> list[dict[str, Any]]:
        assert query in neo4j_graph.QUERIES, "consulta montada fora do conjunto fixo"
        self.queries.append(query)
        if query in neo4j_graph.SCHEMA:
            return []
        if query == neo4j_graph.UPSERT:
            added = 0
            for f in p["facts"]:
                for side in ("subject", "object"):
                    e = f[side]
                    self.entities.setdefault(
                        e["id"], {"kind": e["kind"], "name": e["name"], "key": e["key"]}
                    )
                if f["id"] not in self.facts:
                    self.facts[f["id"]] = {
                        **f,
                        "subject_id": f["subject"]["id"],
                        "object_id": f["object"]["id"],
                    }
                    added += 1
            return [{"added": added}]
        if query == neo4j_graph.STATUS:
            fact = self.facts.get(p["id"])
            return [] if fact is None else [{"status": fact["status"]}]
        if query == neo4j_graph.SET_STATUS:
            fact = self.facts.get(p["id"])
            if fact is None or fact["status"] != p["current"]:
                return [{"changed": 0}]
            fact.update(status=p["target"], reason=p["reason"])
            return [{"changed": 1}]
        if query == neo4j_graph.FIND:
            found = [
                (e["key"], eid, e)
                for eid, e in self.entities.items()
                if p["key"] in e["key"]
            ]
            found.sort(key=lambda t: (len(t[0]), t[0], t[1]))
            return [{"kind": e["kind"], "name": e["name"]} for _, _, e in found][
                : p["limit"]
            ]
        rows = [f for f in self.facts.values() if f["status"] in p["statuses"]]
        if query == neo4j_graph.FACTS_ABOUT:
            rows = [r for r in rows if p["entity"] in (r["subject_id"], r["object_id"])]
            rows.sort(key=lambda r: r["id"])
            rows.sort(key=lambda r: r["observed_at"], reverse=True)
            rows.sort(key=lambda r: r["confidence"], reverse=True)
        else:
            assert query == neo4j_graph.LIST
            if p["source"] is not None:
                rows = [r for r in rows if r["source_id"] == p["source"]]
            rows.sort(key=lambda r: r["id"])
            rows.sort(key=lambda r: r["observed_at"], reverse=True)
        return [self._record(r) for r in rows[: p["limit"]]]

    def _record(self, r: dict[str, Any]) -> dict[str, Any]:
        s, o = self.entities[r["subject_id"]], self.entities[r["object_id"]]
        return {
            **{
                k: r[k]
                for k in (
                    "relation",
                    "source_id",
                    "observed_at",
                    "confidence",
                    "valid_from",
                    "valid_to",
                    "status",
                )
            },
            "subject_kind": s["kind"],
            "subject_name": s["name"],
            "object_kind": o["kind"],
            "object_name": o["name"],
        }


def live_store() -> Neo4jGraphStore:
    if os.environ.get("NEOSON_NEO4J_TESTS") != "1":
        pytest.skip("defina NEOSON_NEO4J_TESTS=1 com um servidor Neo4j descartável")
    uri, password = (
        os.environ.get("NEOSON_NEO4J_URI"),
        os.environ.get("NEOSON_NEO4J_PASSWORD"),
    )
    if not uri or not password:
        pytest.skip("NEOSON_NEO4J_URI e NEOSON_NEO4J_PASSWORD são necessários")
    return open_neo4j(
        uri, os.environ.get("NEOSON_NEO4J_USER", "neo4j"), password, "neo4j", 10
    )


@pytest.fixture(params=["sqlite", "neo4j-fake", "neo4j-live"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[GraphStore]:
    if request.param == "sqlite":
        yield SqliteGraphStore(tmp_path / "graph.db")
    elif request.param == "neo4j-fake":
        yield Neo4jGraphStore(FakeRunner())
    else:
        yield live_store()


@pytest.fixture
def names() -> Callable[[str], str]:
    # Nomes únicos: o servidor real pode já ter dados de outra rodada.
    tag = uuid4().hex[:8]
    return lambda base: f"{base} {tag}"


def fact(
    subject: str,
    obj: str,
    *,
    source: str = "memory_fact:1",
    confidence: float = 0.9,
    status: FactStatus = FactStatus.ACTIVE,
    seen: datetime = SEEN,
    valid_to: datetime | None = None,
) -> Fact:
    return Fact(
        Entity(EntityKind.PERSON, subject),
        Relation.WORKS_ON,
        Entity(EntityKind.PROJECT, obj),
        Provenance(source, seen, confidence, valid_to=valid_to),
        status,
    )


def test_upsert_is_idempotent_per_source(
    store: GraphStore, names: Callable[[str], str]
) -> None:
    ana, neoson = names("Ana"), names("NeoSon")

    async def scenario() -> None:
        assert await store.upsert([fact(ana, neoson), fact(ana, neoson)]) == 1
        assert await store.upsert([fact(ana, neoson)]) == 0
        assert await store.upsert([fact(ana, neoson, source="message:m-1")]) == 1
        assert await store.upsert([]) == 0
        about = await store.facts_about(Entity(EntityKind.PERSON, ana).id, limit=10)
        # Empate em confiança e data: a ordem segue o ID, que não importa aqui.
        assert sorted(f.provenance.source_id for f in about) == [
            "memory_fact:1",
            "message:m-1",
        ]
        assert fact(ana, neoson) in about
        assert all(f.provenance.observed_at == SEEN for f in about)
        with pytest.raises(ValueError):
            await store.upsert(["texto"])  # type: ignore[list-item]

    asyncio.run(scenario())


def test_status_transitions_and_retraction_is_final(
    store: GraphStore, names: Callable[[str], str]
) -> None:
    pending = fact(names("Ana"), names("NeoSon"), status=FactStatus.PENDING)
    ana = pending.subject.id

    async def scenario() -> None:
        await store.upsert([pending])
        assert await store.facts_about(ana, limit=5) == []
        assert await store.set_status(pending.id, FactStatus.ACTIVE)
        assert await store.set_status(pending.id, FactStatus.ACTIVE)
        assert not await store.set_status(pending.id, FactStatus.PENDING)
        assert await store.set_status(pending.id, FactStatus.RETRACTED, "  errado  ")
        assert not await store.set_status(pending.id, FactStatus.ACTIVE)
        assert not await store.set_status("0" * 64, FactStatus.ACTIVE)
        assert await store.upsert([pending]) == 0
        gone = await store.facts_about(ana, limit=5, statuses=[FactStatus.RETRACTED])
        assert [f.status for f in gone] == [FactStatus.RETRACTED]
        for reason in ("", "x" * 201):
            with pytest.raises(ValueError):
                await store.set_status(pending.id, FactStatus.RETRACTED, reason)

    asyncio.run(scenario())


def test_queries_are_limited_ordered_and_literal(
    store: GraphStore, names: Callable[[str], str]
) -> None:
    ana = names("Ana")
    projects = [names(f"Projeto {n}") for n in range(4)]

    async def scenario() -> None:
        await store.upsert(
            [fact(ana, p, confidence=n / 10) for n, p in enumerate(projects)]
            + [fact(ana, names("100% certo"), confidence=0.05)]
        )
        top = await store.facts_about(Entity(EntityKind.PERSON, ana).id, limit=2)
        assert [f.provenance.confidence for f in top] == [0.3, 0.2]
        found = await store.find_entities(ana.split()[-1], limit=10)
        assert {e.name for e in found} >= {ana, *projects}
        assert [
            e.name for e in await store.find_entities(f"% certo {ana[-8:]}", limit=5)
        ] == [names("100% certo")]
        for limit in (0, 101, True):
            with pytest.raises(ValueError):
                await store.facts_about("x", limit=limit)
            with pytest.raises(ValueError):
                await store.find_entities("ana", limit=limit)
            with pytest.raises(ValueError):
                await store.list_facts(statuses=[FactStatus.ACTIVE], limit=limit)
        with pytest.raises(ValueError):
            await store.find_entities("  ", limit=5)
        with pytest.raises(ValueError):
            await store.list_facts(statuses=[], limit=5)

    asyncio.run(scenario())


def test_list_by_source_newest_first(
    store: GraphStore, names: Callable[[str], str]
) -> None:
    ana, source = names("Ana"), f"memory_fact:{uuid4().int % 10**9}"
    old = fact(ana, names("Antigo"), source=source, status=FactStatus.PENDING)
    new = fact(
        ana,
        names("Novo"),
        source=source,
        seen=SEEN + timedelta(days=1),
        valid_to=SEEN + timedelta(days=9),
    )

    async def scenario() -> None:
        await store.upsert([old, new, fact(ana, names("Outro"))])
        listed = await store.list_facts(
            statuses=[FactStatus.PENDING, FactStatus.ACTIVE], limit=10, source_id=source
        )
        assert [f.id for f in listed] == [new.id, old.id]
        assert listed[0].provenance.valid_to == SEEN + timedelta(days=9)

    asyncio.run(scenario())


class Driver:
    def __init__(self, fail: Exception | None = None, delay: float = 0) -> None:
        self.calls: list[tuple[str, dict[str, Any], str, Any]] = []
        self.fail, self.delay = fail, delay

    async def execute_query(
        self, query: str, parameters: dict[str, Any], *, database_: str, routing_: Any
    ) -> Any:
        self.calls.append((query, parameters, database_, routing_))
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail

        class Record:
            def data(self) -> dict[str, Any]:
                return {"status": "active"}

        class Result:
            records = [Record()]

        return Result()


def test_driver_runner_uses_fixed_queries_routing_and_safe_errors() -> None:
    driver = Driver()
    runner = DriverRunner(driver, "neo4j", 1, ("R", "W"))

    async def scenario() -> None:
        assert await runner.read(neo4j_graph.STATUS, {"id": "x"}) == [
            {"status": "active"}
        ]
        await runner.write(neo4j_graph.SET_STATUS, {"id": "x"})
        assert [c[2:] for c in driver.calls] == [("neo4j", "R"), ("neo4j", "W")]
        with pytest.raises(ValueError, match="conjunto fixo"):
            await runner.read("MATCH (n) RETURN n", {})
        secret = "bolt://usuario:senha" + chr(64) + "host.invalid"
        failing = DriverRunner(Driver(fail=RuntimeError(secret)), "neo4j", 1)
        with pytest.raises(GraphBackendError) as error:
            await failing.read(neo4j_graph.STATUS, {"id": "x"})
        assert secret not in str(error.value) and "RuntimeError" in str(error.value)
        slow = DriverRunner(Driver(delay=1), "neo4j", 0.01)
        with pytest.raises(GraphBackendError, match="prazo"):
            await slow.read(neo4j_graph.STATUS, {"id": "x"})

    asyncio.run(scenario())


def test_schema_runs_once_before_the_first_query() -> None:
    runner = FakeRunner()
    store = Neo4jGraphStore(runner)

    async def scenario() -> None:
        await store.find_entities("ana", limit=1)
        await store.find_entities("ana", limit=1)

    asyncio.run(scenario())
    assert runner.queries[: len(neo4j_graph.SCHEMA)] == list(neo4j_graph.SCHEMA)
    assert runner.queries.count(neo4j_graph.SCHEMA[0]) == 1


def test_backend_selection_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "neoson.db"))
    assert isinstance(graph_store(load_settings(env_file=None)), SqliteGraphStore)
    monkeypatch.setenv("NEOSON_GRAPH_BACKEND", "neo4j")
    with pytest.raises(ConfigError, match="NEOSON_NEO4J_URI"):
        graph_store(load_settings(env_file=None))
    monkeypatch.setenv("NEOSON_NEO4J_URI", "bolt://neo4j-host.invalid:7687")
    monkeypatch.setenv("NEOSON_NEO4J_PASSWORD", "senha-ficticia")

    def missing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr("importlib.import_module", missing)
    with pytest.raises(ConfigError, match="extra neo4j") as error:
        graph_store(load_settings(env_file=None))
    assert "senha-ficticia" not in str(error.value)
