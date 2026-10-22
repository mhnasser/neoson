import asyncio
import math
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from neoson_core.engine.graph import (
    Entity,
    EntityKind,
    Fact,
    FactStatus,
    Provenance,
    Relation,
    stable_id,
)
from neoson_core.infrastructure import backup
from neoson_core.infrastructure.db import connect
from neoson_core.memory.graph import SqliteGraphStore

SEEN = datetime(2026, 10, 22, 9, tzinfo=timezone(timedelta(hours=-3)))
ANA = Entity(EntityKind.PERSON, "Ana Souza")
NEOSON = Entity(EntityKind.PROJECT, "NeoSon")


def fact(
    subject: Entity = ANA,
    obj: Entity = NEOSON,
    *,
    source: str = "memory_fact:1",
    confidence: float = 0.9,
    status: FactStatus = FactStatus.ACTIVE,
    relation: Relation = Relation.WORKS_ON,
) -> Fact:
    return Fact(subject, relation, obj, Provenance(source, SEEN, confidence), status)


def test_entities_have_stable_ids_independent_of_spacing_and_case() -> None:
    spaced = Entity(EntityKind.PERSON, "  ana   SOUZA ")
    assert spaced.name == "ana SOUZA" and spaced.id == ANA.id
    assert Entity(EntityKind.CONCEPT, "Ana Souza").id != ANA.id
    assert len(ANA.id) == 64 and ANA.id == stable_id("entity", "Person", "ana souza")
    for name in ("", "   ", "x" * 201):
        with pytest.raises(ValueError):
            Entity(EntityKind.PERSON, name)
    with pytest.raises(ValueError):
        Entity("Person", "Ana")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": ""},
        {"source_id": "memoria sem tipo"},
        {"source_id": "Memory:1"},
        {"source_id": "memory_fact:" + "1" * 129},
        {"observed_at": datetime(2026, 10, 22, 9)},
        {"confidence": -0.1},
        {"confidence": 1.01},
        {"confidence": math.nan},
        {"confidence": True},
        {"valid_from": SEEN, "valid_to": SEEN - timedelta(days=1)},
        {"valid_from": datetime(2026, 1, 1)},
    ],
)
def test_provenance_is_mandatory_and_validated(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "source_id": "memory_fact:1",
        "observed_at": SEEN,
        "confidence": 0.5,
        **changes,
    }
    with pytest.raises(ValueError):
        Provenance(**values)


def test_facts_reject_self_loops_and_unknown_types() -> None:
    with pytest.raises(ValueError, match="ela mesma"):
        fact(ANA, Entity(EntityKind.PERSON, "ana souza"))
    with pytest.raises(ValueError):
        Fact(ANA, "WORKS_ON", NEOSON, Provenance("x:1", SEEN, 1))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Fact(ANA, Relation.WORKS_ON, NEOSON, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Relation("KNOWS")
    assert fact().id != fact(source="message:abc").id
    assert fact().id == fact(confidence=0.1, status=FactStatus.PENDING).id


def test_upsert_is_idempotent_and_each_source_is_a_separate_fact(
    tmp_path: Path,
) -> None:
    store = SqliteGraphStore(tmp_path / "graph.db")

    async def scenario() -> None:
        assert await store.upsert([fact(), fact()]) == 1
        assert await store.upsert([fact()]) == 0
        assert await store.upsert([fact(source="message:m-1", confidence=0.4)]) == 1
        found = await store.facts_about(ANA.id, limit=10)
        assert [f.provenance.source_id for f in found] == [
            "memory_fact:1",
            "message:m-1",
        ]
        assert found[0] == fact()
        assert found[0].provenance.observed_at == SEEN
        # Pelo objeto também: a relação aparece para as duas entidades.
        assert len(await store.facts_about(NEOSON.id, limit=10)) == 2
        with pytest.raises(ValueError):
            await store.upsert([fact(), "texto livre"])  # type: ignore[list-item]

    asyncio.run(scenario())
    with closing(connect(tmp_path / "graph.db")) as connection:
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("graph_entities", "graph_facts")
        ]
    assert counts == [2, 2]


def test_status_transitions_keep_history_and_pending_is_not_returned(
    tmp_path: Path,
) -> None:
    store = SqliteGraphStore(tmp_path / "graph.db")
    pending = fact(status=FactStatus.PENDING)

    async def scenario() -> None:
        await store.upsert([pending])
        assert await store.facts_about(ANA.id, limit=5) == []
        everything = (FactStatus.PENDING, FactStatus.ACTIVE)
        assert len(await store.facts_about(ANA.id, limit=5, statuses=everything)) == 1
        assert await store.set_status(pending.id, FactStatus.ACTIVE)
        assert await store.set_status(pending.id, FactStatus.ACTIVE)
        assert not await store.set_status(pending.id, FactStatus.PENDING)
        assert await store.set_status(pending.id, FactStatus.RETRACTED, "fonte apagada")
        assert not await store.set_status(pending.id, FactStatus.ACTIVE)
        assert not await store.set_status("inexistente", FactStatus.ACTIVE)
        # Reenviar o mesmo fato não ressuscita o retirado.
        assert await store.upsert([fact()]) == 0
        assert await store.facts_about(ANA.id, limit=5) == []
        retracted = await store.facts_about(
            ANA.id, limit=5, statuses=[FactStatus.RETRACTED]
        )
        assert retracted[0].provenance == pending.provenance
        for reason in ("", "x" * 201):
            with pytest.raises(ValueError):
                await store.set_status(pending.id, FactStatus.RETRACTED, reason)

    asyncio.run(scenario())
    with closing(connect(tmp_path / "graph.db")) as connection:
        row = connection.execute("SELECT reason FROM graph_facts").fetchone()
    assert tuple(row) == ("fonte apagada",)


def test_queries_are_limited_ordered_and_escape_wildcards(tmp_path: Path) -> None:
    store = SqliteGraphStore(tmp_path / "graph.db")
    concepts = [Entity(EntityKind.CONCEPT, f"Tema {n}") for n in range(5)]

    async def scenario() -> None:
        await store.upsert(
            [
                fact(ANA, c, confidence=n / 10, relation=Relation.RELATES_TO)
                for n, c in enumerate(concepts)
            ]
            + [fact(ANA, Entity(EntityKind.CONCEPT, "100% certo"))]
        )
        top = await store.facts_about(ANA.id, limit=3)
        assert [f.provenance.confidence for f in top] == [0.9, 0.4, 0.3]
        assert [e.name for e in await store.find_entities("ana", limit=5)] == [
            "Ana Souza"
        ]
        assert [e.name for e in await store.find_entities("TEMA", limit=2)] == [
            "Tema 0",
            "Tema 1",
        ]
        assert [e.name for e in await store.find_entities("%", limit=10)] == [
            "100% certo"
        ]
        assert await store.find_entities("_", limit=10) == []
        for limit in (0, 101, True):
            with pytest.raises(ValueError):
                await store.facts_about(ANA.id, limit=limit)
            with pytest.raises(ValueError):
                await store.find_entities("ana", limit=limit)
        with pytest.raises(ValueError):
            await store.find_entities("   ", limit=5)
        with pytest.raises(ValueError):
            await store.facts_about(ANA.id, limit=5, statuses=[])

    asyncio.run(scenario())


def test_graph_survives_restart_and_is_counted_in_backups(tmp_path: Path) -> None:
    db, vault = tmp_path / "neoson.db", tmp_path / "vault"
    vault.mkdir()
    asyncio.run(SqliteGraphStore(db).upsert([fact()]))
    reopened = SqliteGraphStore(db)
    assert asyncio.run(reopened.facts_about(ANA.id, limit=1)) == [fact()]
    info = backup.create(db, vault, tmp_path / "backups")
    assert info.counts["graph_entities"] == 2 and info.counts["graph_facts"] == 1
    assert backup.verify(info.path).counts == info.counts
    assert info.created_at <= datetime.now(UTC)
