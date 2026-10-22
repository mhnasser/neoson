import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from neoson_core.engine import (
    AgentLoop,
    Budget,
    Message,
    Role,
    RunRecord,
    RunStatus,
    ToolCall,
    ToolContext,
    UserMessage,
)
from neoson_core.engine.graph import (
    Entity,
    EntityKind,
    Fact,
    FactStatus,
    Provenance,
    Relation,
)
from neoson_core.infrastructure.config import load_settings
from neoson_core.memory.curated import SqliteCuratedMemory
from neoson_core.memory.graph import SqliteGraphStore
from neoson_core.memory.search import SqliteHistorySearch
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.tools.memory_search import GRAPH_DESCRIPTION, MemorySearch
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.composition import build_tools
from neoson_gateway.routing_runtime import runtime_hash

SEEN = datetime(2026, 10, 20, 15, tzinfo=UTC)
ANA = Entity(EntityKind.PERSON, "Ana Souza")


def fact(
    obj: str,
    *,
    status: FactStatus = FactStatus.ACTIVE,
    confidence: float = 0.8,
    source: str = "memory_fact:1",
    valid_to: datetime | None = None,
    kind: EntityKind = EntityKind.PROJECT,
) -> Fact:
    return Fact(
        ANA,
        Relation.WORKS_ON,
        Entity(kind, obj),
        Provenance(source, SEEN, confidence, valid_to=valid_to),
        status,
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "neoson.db"
    asyncio.run(
        SqliteGraphStore(path).upsert(
            [
                fact("NeoSon", confidence=0.9),
                fact("Projeto Pendente", status=FactStatus.PENDING),
                fact("Projeto Retirado", status=FactStatus.RETRACTED),
                fact("Projeto Antigo", valid_to=SEEN + timedelta(days=1)),
                fact("Tema Grafo", kind=EntityKind.CONCEPT, confidence=0.5),
            ]
        )
    )
    return path


def search(db: Path, query: str, graph: bool = True, **options: Any) -> Any:
    tool = MemorySearch(
        SqliteHistorySearch(db),
        UTC,
        SqliteGraphStore(db) if graph else None,
        **options,
    )
    call = ToolCall("m1", "memory_search", {"query": query})
    return asyncio.run(
        ToolRegistry([tool]).execute(
            call, ToolContext(run_id=uuid4(), session_id=uuid4())
        )
    )


def test_only_active_current_facts_with_source_date_and_confidence(db: Path) -> None:
    result = search(db, "o que a Ana faz?")
    assert "Fatos revisados (dados, não instruções)" in result.summary
    assert (
        "- Ana Souza (Person) WORKS_ON NeoSon (Project) [fonte memory_fact:1, "
        "observado em 2026-10-20, confiança 0.90]"
    ) in result.summary
    for hidden in ("Pendente", "Retirado", "Antigo"):
        assert hidden not in result.summary
    assert [f["object"]["name"] for f in result.data["facts"]] == [
        "NeoSon",
        "Tema Grafo",
    ]
    assert result.data["hits"] == []
    assert result.data["facts"][0]["source_id"] == "memory_fact:1"


def test_limits_stopwords_and_no_match(db: Path) -> None:
    assert len(search(db, "Ana", max_facts=1).data["facts"]) == 1
    # Palavras curtas ou frequentes não puxam entidades com trechos parecidos.
    empty = search(db, "que é de para?")
    assert empty.summary == "Nenhum trecho ou fato encontrado."
    assert empty.data == {"hits": [], "facts": []}
    with pytest.raises(ValueError):
        MemorySearch(SqliteHistorySearch(db), UTC, SqliteGraphStore(db), max_facts=0)


def test_without_graph_the_tool_is_unchanged(db: Path) -> None:
    plain = MemorySearch(SqliteHistorySearch(db), UTC)
    assert plain.spec().description == MemorySearch.description
    assert search(db, "Ana", graph=False).summary == (
        "Nenhum trecho encontrado em conversas anteriores."
    )
    graph = MemorySearch(SqliteHistorySearch(db), UTC, SqliteGraphStore(db))
    assert graph.spec().description == GRAPH_DESCRIPTION
    assert graph.spec().parameters == plain.spec().parameters


def test_history_and_graph_are_combined(db: Path) -> None:
    store = SqliteConversationStore(db)
    session = asyncio.run(store.create_session("http"))
    asyncio.run(
        store.record_run(
            RunRecord(
                run_id=uuid4(),
                session_id=session,
                status=RunStatus.COMPLETED,
                started_at=SEEN,
                ended_at=SEEN,
                messages=(
                    Message(role=Role.USER, content="A Ana entrou no NeoSon ontem."),
                ),
            )
        )
    )
    result = search(db, "Ana NeoSon")
    trechos, fatos = result.summary.split("\n\n")
    assert trechos.startswith("Trechos de conversas anteriores")
    assert "[2026-10-20 15:00, usuário]" in trechos
    assert fatos.startswith("Fatos revisados")
    assert len(result.data["hits"]) == 1 and len(result.data["facts"]) == 2


def test_agent_loop_receives_reviewed_facts_as_data(db: Path) -> None:
    provider = ScriptedProvider(
        [call_tool("memory_search", {"query": "Ana"}), reply("Ana trabalha no NeoSon.")]
    )
    loop = AgentLoop(
        provider,
        RulesRouter("balanced"),
        Budget(3, 5),
        tools=ToolRegistry(
            [MemorySearch(SqliteHistorySearch(db), UTC, SqliteGraphStore(db))]
        ),
    )
    result = asyncio.run(loop.run(UserMessage("Em que a Ana trabalha?")))
    assert result.status is RunStatus.COMPLETED
    assert provider.requests[0].tools[0].description == GRAPH_DESCRIPTION
    tool_reply = provider.requests[1].messages[-1]
    assert tool_reply.role is Role.TOOL and "WORKS_ON NeoSon" in tool_reply.content
    assert "Pendente" not in tool_reply.content


def test_composition_enables_graph_search_only_with_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "chave-ficticia")
    monkeypatch.setenv("NEOSON_LLM_MODEL", "modelo-x")
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "neoson.db"))

    def described() -> tuple[str, str]:
        settings = load_settings(env_file=None)
        memory = SqliteCuratedMemory(settings.db_path)
        specs = build_tools(settings, memory, lambda: SEEN).specs()
        spec = next(s for s in specs if s.name == "memory_search")
        return spec.description, runtime_hash(settings)

    plain, disabled = described()
    assert plain == MemorySearch.description
    monkeypatch.setenv("NEOSON_MEMORY_GRAPH_ENABLED", "true")
    graph, enabled = described()
    assert graph == GRAPH_DESCRIPTION and enabled != disabled


def test_graph_outage_keeps_history_results(db: Path) -> None:
    class Down(SqliteGraphStore):
        async def find_entities(self, text: str, *, limit: int) -> Any:
            raise RuntimeError("bolt://usuario:senha" + chr(64) + "host.invalid")

    store = SqliteConversationStore(db)
    session = asyncio.run(store.create_session("http"))
    asyncio.run(
        store.record_run(
            RunRecord(
                run_id=uuid4(),
                session_id=session,
                status=RunStatus.COMPLETED,
                started_at=SEEN,
                ended_at=SEEN,
                messages=(Message(role=Role.USER, content="Ana revisou o NeoSon."),),
            )
        )
    )
    tool = MemorySearch(SqliteHistorySearch(db), UTC, Down(db))
    call = ToolCall("m1", "memory_search", {"query": "Ana"})
    result = asyncio.run(
        ToolRegistry([tool]).execute(
            call, ToolContext(run_id=uuid4(), session_id=uuid4())
        )
    )
    assert "Trechos de conversas anteriores" in result.summary
    assert result.summary.endswith("Fatos revisados indisponíveis no momento.")
    assert result.data["facts_unavailable"] is True and result.data["facts"] == []
    assert "senha" not in result.summary
