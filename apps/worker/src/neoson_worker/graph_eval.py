"""Avaliação reprodutível da recuperação com e sem grafo (critério da F7).

Os dois braços usam a mesma `memory_search`, os mesmos casos e um banco temporário
semeado a partir do dataset: só o grafo muda. O relatório e o painel trazem só
métricas e IDs de caso, nunca o texto das conversas ou dos fatos.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMProvider,
    Message,
    Role,
    RunRecord,
    RunStatus,
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
from neoson_core.memory.graph import SqliteGraphStore
from neoson_core.memory.search import SqliteHistorySearch
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.routing.rules import RulesRouter
from neoson_core.telemetry.pricing import Price, cost_usd
from neoson_core.telemetry.vault import frontmatter, write_atomic
from neoson_core.tools.memory_search import MemorySearch
from neoson_core.tools.registry import ToolRegistry

ARMS = ("history", "hybrid")
PANEL = Path("Dashboard") / "Grafo-Avaliacao.md"
_KEY = r"[a-z0-9_]{1,40}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetMessage(_Strict):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class DatasetSession(_Strict):
    key: str = Field(pattern=_KEY)
    started_at: datetime
    messages: list[DatasetMessage] = Field(min_length=1, max_length=20)

    @field_validator("started_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("data sem fuso horário")
        return value


class DatasetEntity(_Strict):
    kind: EntityKind
    name: str = Field(min_length=1, max_length=200)


class DatasetFact(_Strict):
    subject: DatasetEntity
    relation: Relation
    object: DatasetEntity
    source: str = Field(pattern=r"memory_fact:[0-9]{1,9}")
    observed_at: datetime
    confidence: float = Field(ge=0, le=1)
    status: FactStatus

    def fact(self) -> Fact:
        return Fact(
            Entity(self.subject.kind, self.subject.name),
            self.relation,
            Entity(self.object.kind, self.object.name),
            Provenance(self.source, self.observed_at, self.confidence),
            self.status,
        )


class DatasetCase(_Strict):
    id: str = Field(pattern=_KEY)
    query: str = Field(min_length=1, max_length=300)
    question: str = Field(min_length=1, max_length=500)
    expected_sources: list[str] = Field(max_length=10)
    forbidden_sources: list[str] = Field(default_factory=list, max_length=10)
    answer_keywords: list[str] = Field(default_factory=list, max_length=5)


class RetrievalDataset(_Strict):
    id: str = Field(pattern=r"[a-z0-9-]{1,60}")
    version: str = Field(pattern=r"[0-9]{1,4}")
    schema_version: Literal[1]
    description: str = Field(max_length=500)
    sessions: list[DatasetSession] = Field(max_length=200)
    facts: list[DatasetFact] = Field(max_length=500)
    cases: list[DatasetCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _references(self) -> Self:
        sessions = {s.key for s in self.sessions}
        facts = {f.source for f in self.facts}
        if len(sessions) != len(self.sessions) or len(facts) != len(self.facts):
            raise ValueError("chaves de sessão ou fontes de fato repetidas")
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("IDs de caso repetidos")
        known = {f"session:{key}" for key in sessions} | facts
        for case in self.cases:
            for source in (*case.expected_sources, *case.forbidden_sources):
                if source not in known:
                    raise ValueError(f"caso {case.id}: fonte desconhecida")
            if set(case.expected_sources) & set(case.forbidden_sources):
                raise ValueError(f"caso {case.id}: fonte esperada e proibida")
        return self


def load_dataset(path: Path) -> tuple[RetrievalDataset, str]:
    """Dataset validado e o SHA-256 do arquivo, para tornar a avaliação rastreável."""
    raw = path.read_bytes()
    return RetrievalDataset.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


async def seed(dataset: RetrievalDataset, db: Path) -> dict[UUID, str]:
    """Grava conversas e fatos num banco novo; devolve sessão → chave do dataset."""
    if db.exists():
        raise ValueError("A avaliação só semeia um banco novo.")
    store = SqliteConversationStore(db)
    sessions: dict[UUID, str] = {}
    for item in dataset.sessions:
        session_id = await store.create_session("graph_eval")
        sessions[session_id] = item.key
        await store.record_run(
            RunRecord(
                run_id=uuid4(),
                session_id=session_id,
                status=RunStatus.COMPLETED,
                started_at=item.started_at,
                ended_at=item.started_at,
                messages=tuple(
                    Message(role=Role(m.role), content=m.content) for m in item.messages
                ),
            )
        )
    await SqliteGraphStore(db).upsert([f.fact() for f in dataset.facts])
    return sessions


async def evaluate(
    dataset: RetrievalDataset,
    checksum: str,
    workdir: Path,
    *,
    provider: LLMProvider | None = None,
    profile: str = "balanced",
    prices: Mapping[str, Price] | None = None,
    max_cases: int | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Compara só histórico x híbrido nos mesmos casos. Com `provider`, também
    responde cada pergunta com o modelo e confere as palavras esperadas."""
    db = workdir / "graph-eval.db"
    sessions = await seed(dataset, db)
    cases = dataset.cases[:max_cases] if max_cases is not None else dataset.cases
    history = SqliteHistorySearch(db)
    tools = {
        "history": MemorySearch(history, UTC),
        "hybrid": MemorySearch(history, UTC, SqliteGraphStore(db)),
    }
    results = []
    for case in cases:
        entry: dict[str, Any] = {"id": case.id}
        for arm in ARMS:
            entry[arm] = await _retrieve(tools[arm], case, sessions)
            if provider is not None:
                entry[arm].update(
                    await _answer(provider, profile, tools[arm], case, prices or {})
                )
        results.append(entry)
    now = (clock or (lambda: datetime.now(UTC)))()
    return {
        "dataset": {"id": dataset.id, "version": dataset.version, "checksum": checksum},
        "mode": "live" if provider is not None else "deterministic",
        "created_at": now.astimezone(UTC).isoformat(),
        "cases_total": len(results),
        "arms": {arm: _summary(results, arm, provider is not None) for arm in ARMS},
        "cases": results,
    }


async def _retrieve(
    tool: MemorySearch, case: DatasetCase, sessions: Mapping[UUID, str]
) -> dict[str, Any]:
    output = await tool.run(
        tool.arguments(query=case.query, limit=5),
        ToolContext(run_id=uuid4(), session_id=uuid4()),
    )
    data: Mapping[str, Any] = output.data
    hits: list[dict[str, str]] = data.get("hits", [])
    facts: list[dict[str, Any]] = data.get("facts", [])
    retrieved = {
        f"session:{sessions[UUID(hit['session_id'])]}"
        for hit in hits
        if UUID(hit["session_id"]) in sessions
    } | {fact["source_id"] for fact in facts}
    expected = set(case.expected_sources)
    return {
        "retrieved": sorted(retrieved),
        "recall": None if not expected else len(expected & retrieved) / len(expected),
        "all_expected": expected <= retrieved,
        "forbidden": bool(set(case.forbidden_sources) & retrieved),
        "extra_sources": len(retrieved - expected),
        "context_chars": len(output.summary),
    }


async def _answer(
    provider: LLMProvider,
    profile: str,
    tool: MemorySearch,
    case: DatasetCase,
    prices: Mapping[str, Price],
) -> dict[str, Any]:
    # Sem armazenamento: a pergunta da avaliação não entra no histórico semeado.
    loop = AgentLoop(
        provider, RulesRouter(profile), Budget(4, 60), tools=ToolRegistry([tool])
    )
    result = await loop.run(UserMessage(case.question))
    text = result.text.casefold()
    usage = result.usage
    return {
        "answer_ok": all(k.casefold() in text for k in case.answer_keywords)
        if case.answer_keywords
        else None,
        "status": result.status.value,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": cost_usd(
            result.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_tokens,
            prices,
        ),
    }


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _summary(
    results: Sequence[Mapping[str, Any]], arm: str, live: bool
) -> dict[str, Any]:
    rows = [r[arm] for r in results]
    with_expected = [r for r in rows if r["recall"] is not None]
    summary: dict[str, Any] = {
        "source_recall": _mean([r["recall"] for r in with_expected]),
        "all_sources_rate": _mean([float(r["all_expected"]) for r in with_expected]),
        "forbidden_rate": _mean([float(r["forbidden"]) for r in rows]),
        "mean_extra_sources": _mean([float(r["extra_sources"]) for r in rows]),
        "mean_context_chars": _mean([float(r["context_chars"]) for r in rows]),
    }
    if live:
        scored = [r for r in rows if r["answer_ok"] is not None]
        costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        summary.update(
            answer_accuracy=_mean([float(r["answer_ok"]) for r in scored]),
            input_tokens=sum(r["input_tokens"] for r in rows),
            output_tokens=sum(r["output_tokens"] for r in rows),
            cost_usd=round(sum(costs), 6) if len(costs) == len(rows) else None,
        )
    return summary


_LABELS = {
    "source_recall": "Recall de fontes",
    "all_sources_rate": "Casos com todas as fontes",
    "forbidden_rate": "Casos com fonte pendente/retirada",
    "mean_extra_sources": "Fontes não esperadas por caso (ruído)",
    "mean_context_chars": "Contexto médio (caracteres)",
    "answer_accuracy": "Respostas com as palavras esperadas",
    "input_tokens": "Tokens de entrada",
    "output_tokens": "Tokens de saída",
    "cost_usd": "Custo estimado (USD)",
}


def render_panel(report: Mapping[str, Any]) -> str:
    """Painel só com agregados e IDs de caso; nada do conteúdo avaliado."""
    history, hybrid = report["arms"]["history"], report["arms"]["hybrid"]
    dataset = report["dataset"]
    rows = []
    for key, label in _LABELS.items():
        if key not in history:
            continue
        before, after = history[key], hybrid[key]
        delta = (
            "—"
            if not isinstance(before, (int, float))
            or not isinstance(after, (int, float))
            else f"{after - before:+.4g}"
        )
        rows.append(f"| {label} | {_cell(before)} | {_cell(after)} | {delta} |")
    cases = "\n".join(
        f"| {c['id']} | {_mark(c['history'])} | {_mark(c['hybrid'])} |"
        for c in report["cases"]
    )
    head = frontmatter(
        {
            "tipo": "neoson-dashboard",
            "painel": "Grafo",
            "dataset": f"{dataset['id']}@{dataset['version']}",
            "checksum": dataset["checksum"][:12],
            "modo": report["mode"],
            "avaliado_em": report["created_at"],
            "casos": report["cases_total"],
        }
    )
    return (
        f"{head}\n# Grafo — recuperação com e sem grafo\n\n"
        f"Dataset `{dataset['id']}` v{dataset['version']} "
        f"(`{dataset['checksum'][:12]}`), modo {report['mode']}, "
        f"{report['cases_total']} casos. Mesmos casos e regras nos dois braços; "
        "só o grafo muda.\n\n"
        "| Métrica | Só histórico | Híbrido | Diferença |\n|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n\n## Casos\n\n| Caso | Só histórico | Híbrido |\n|---|---|---|\n"
        + cases
        + "\n\n✓ todas as fontes esperadas; ✗ faltou alguma; ⚠ trouxe fonte pendente "
        "ou retirada; — caso sem fonte esperada.\n"
    )


def export_panel(report: Mapping[str, Any], vault: Path) -> bool:
    return write_atomic(vault / PANEL, render_panel(report))


def save_report(report: Mapping[str, Any], directory: Path) -> Path:
    stamp = re.sub(r"[^0-9]", "", str(report["created_at"]))[:14]
    path = directory / f"graph-eval-{stamp}.json"
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _cell(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _mark(row: Mapping[str, Any]) -> str:
    if row["forbidden"]:
        return "⚠"
    if row["recall"] is None:
        return "—"
    mark = "✓" if row["all_expected"] else "✗"
    if row.get("answer_ok") is not None:
        mark += " resposta ✓" if row["answer_ok"] else " resposta ✗"
    return mark
