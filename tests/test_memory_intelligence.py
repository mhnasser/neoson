import asyncio
import os
from contextlib import closing
from datetime import UTC, datetime
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
)
from neoson_core.infrastructure.db import connect, migrate
from neoson_core.memory.curated import SqliteCuratedMemory
from neoson_core.memory.documents import DocumentStore
from neoson_core.memory.graph import SqliteGraphStore
from neoson_core.memory.vault_export import MemoryVaultExporter
from neoson_core.telemetry.memory_metrics import PANEL, memory_metrics, render_memory
from neoson_core.telemetry.vault import render_home
from neoson_worker import graph_main

SEEN = datetime(2026, 10, 22, 9, tzinfo=UTC)
PRIVATE = ["Lia Prado", "Projeto Atlas", "Bruno Reis", "memória privada", "Plano"]


def fact(subject: str, obj: str, source: str, status: FactStatus) -> Fact:
    return Fact(
        Entity(EntityKind.PERSON, subject),
        Relation.WORKS_ON,
        Entity(EntityKind.PROJECT, obj),
        Provenance(source, SEEN, 0.8),
        status,
    )


def seed(tmp_path: Path) -> Path:
    db = tmp_path / "neoson.db"
    memory = SqliteCuratedMemory(db)
    first = asyncio.run(memory.save("user", "memória privada um", None, None))
    asyncio.run(memory.save("user", "memória privada dois", None, None))
    asyncio.run(memory.forget(first.id))
    graph = SqliteGraphStore(db)
    asyncio.run(
        graph.upsert(
            [
                # Mesmo trio ativo numa fonte e retirado em outra: contradição.
                fact("Lia Prado", "Projeto Atlas", "memory_fact:1", FactStatus.ACTIVE),
                fact(
                    "Lia Prado", "Projeto Atlas", "memory_fact:2", FactStatus.RETRACTED
                ),
                # Mesmo trio ativo em duas fontes: confirmado.
                fact("Bruno Reis", "Projeto Atlas", "memory_fact:3", FactStatus.ACTIVE),
                fact(
                    "Bruno Reis",
                    "Projeto Atlas",
                    f"document:{'a' * 64}:0",
                    FactStatus.ACTIVE,
                ),
                fact("Lia Prado", "Bruno Reis Co", "memory_fact:4", FactStatus.PENDING),
            ]
        )
    )
    source = tmp_path / "plano.md"
    source.write_text("# Plano\nTexto.", encoding="utf-8")
    DocumentStore(db, tmp_path / "originais", tmp_path / "vault").ingest(
        source, extract=True
    )
    with closing(connect(db)) as c, c:
        c.execute(
            "INSERT INTO graph_jobs"
            " (id, kind, source_id, created_at, status, last_error)"
            " VALUES ('j1', 'extract', 'memory_fact:9', ?, 'failed', 'invalid_output')",
            (SEEN.isoformat(),),
        )
    return db


def test_metrics_count_states_sources_contradictions_and_jobs(tmp_path: Path) -> None:
    db = seed(tmp_path)
    with closing(connect(db)) as connection:
        metrics = memory_metrics(connection)
    assert metrics.curated == {"active": 1, "forgotten": 1}
    assert metrics.facts == {"active": 3, "retracted": 1, "pending": 1}
    assert metrics.facts_by_source == {
        "memory_fact": {"active": 2, "retracted": 1, "pending": 1},
        "document": {"active": 1},
    }
    assert (metrics.contradictions, metrics.corroborated) == (1, 1)
    assert metrics.jobs == {"extract:failed": 1, "extract:pending": 1}
    assert metrics.job_errors == {"invalid_output": 1}
    assert metrics.documents == {"active": 1}


def test_panel_has_only_counts(tmp_path: Path) -> None:
    db = seed(tmp_path)
    with closing(connect(db)) as connection:
        panel = render_memory(memory_metrics(connection))
    assert "| Contradições (mesmo trio ativo e retirado) | 1 |" in panel
    assert "| memory_fact | 1 | 2 | 1 |" in panel
    assert "| extract / failed | 1 |" in panel
    assert not [text for text in PRIVATE if text in panel]
    assert "memory_fact:1" not in panel and "a" * 64 not in panel
    with closing(connect(tmp_path / "vazio.db")) as connection:
        migrate(connection)
        empty = render_memory(memory_metrics(connection))
    assert "| — | 0 | 0 | 0 |" in empty


def test_exporter_writes_the_panel_idempotently_and_home_links_it(
    tmp_path: Path,
) -> None:
    db = seed(tmp_path)
    vault = tmp_path / "vault"
    exporter = MemoryVaultExporter(vault, UTC)
    with closing(connect(db)) as connection:
        assert vault / PANEL in exporter.export(connection)
        assert vault / PANEL not in exporter.export(connection)
    assert "Memory-Intelligence" in render_home([], _system(), UTC)


def _system() -> Any:
    from neoson_core.telemetry.metrics import SystemMetrics

    return SystemMetrics({}, 0, None, None, None)


def test_graph_review_refreshes_the_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    db = seed(tmp_path)
    monkeypatch.setenv("NEOSON_DB_PATH", str(db))
    monkeypatch.setenv("NEOSON_VAULT_PATH", str(tmp_path / "vault"))
    pending = fact("Lia Prado", "Bruno Reis Co", "memory_fact:4", FactStatus.PENDING)
    assert graph_main.main(["activate", pending.id]) == 0
    panel = (tmp_path / "vault" / PANEL).read_text(encoding="utf-8")
    assert "| Fatos pendentes de revisão | 0 |" in panel
