"""Painel Memory Intelligence (§13.1): só contagens, sem nomes, textos ou fontes."""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from neoson_core.telemetry.vault import frontmatter

PANEL = Path("Dashboard") / "Memory-Intelligence.md"
STATUSES = ("pending", "active", "retracted")


@dataclass(frozen=True, slots=True)
class MemoryMetrics:
    curated: Mapping[str, int] = field(default_factory=dict)
    facts: Mapping[str, int] = field(default_factory=dict)
    facts_by_source: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    contradictions: int = 0
    corroborated: int = 0
    jobs: Mapping[str, int] = field(default_factory=dict)
    job_errors: Mapping[str, int] = field(default_factory=dict)
    documents: Mapping[str, int] = field(default_factory=dict)


def memory_metrics(connection: sqlite3.Connection) -> MemoryMetrics:
    def counts(sql: str) -> dict[str, int]:
        return {str(row[0]): int(row[1]) for row in connection.execute(sql)}

    by_source: dict[str, dict[str, int]] = {}
    for kind, status, total in connection.execute(
        "SELECT substr(source_id, 1, instr(source_id, ':') - 1), status, COUNT(*)"
        " FROM graph_facts GROUP BY 1, 2"
    ):
        by_source.setdefault(str(kind), {})[str(status)] = int(total)
    # Mesmo trio (sujeito, relação, objeto) vindo de fontes diferentes.
    contradictions, corroborated = connection.execute(
        "SELECT COALESCE(SUM(active > 0 AND retracted > 0), 0),"
        " COALESCE(SUM(active > 1), 0) FROM (SELECT"
        " SUM(status = 'active') AS active, SUM(status = 'retracted') AS retracted"
        " FROM graph_facts GROUP BY subject_id, relation, object_id)"
    ).fetchone()
    return MemoryMetrics(
        curated=counts("SELECT status, COUNT(*) FROM memory_facts GROUP BY status"),
        facts=counts("SELECT status, COUNT(*) FROM graph_facts GROUP BY status"),
        facts_by_source=by_source,
        contradictions=int(contradictions),
        corroborated=int(corroborated),
        jobs=counts(
            "SELECT kind || ':' || status, COUNT(*) FROM graph_jobs GROUP BY 1"
        ),
        job_errors=counts(
            "SELECT last_error, COUNT(*) FROM graph_jobs"
            " WHERE last_error IS NOT NULL AND status IN ('pending', 'failed')"
            " GROUP BY last_error"
        ),
        documents=counts("SELECT status, COUNT(*) FROM documents GROUP BY status"),
    )


def render_memory(metrics: MemoryMetrics) -> str:
    facts = metrics.facts
    head = frontmatter(
        {
            "tipo": "neoson-dashboard",
            "painel": "Memory Intelligence",
            "memorias_ativas": metrics.curated.get("active", 0),
            "fatos_pendentes": facts.get("pending", 0),
            "fatos_ativos": facts.get("active", 0),
            "fatos_retirados": facts.get("retracted", 0),
            "contradicoes": metrics.contradictions,
            "documentos_ativos": metrics.documents.get("active", 0),
        }
    )
    sources = "\n".join(
        f"| {kind} | "
        + " | ".join(str(values.get(status, 0)) for status in STATUSES)
        + " |"
        for kind, values in sorted(metrics.facts_by_source.items())
    )
    jobs = "\n".join(
        f"| {key.replace(':', ' / ')} | {total} |"
        for key, total in sorted(metrics.jobs.items())
    )
    errors = "\n".join(
        f"| {error} | {total} |" for error, total in sorted(metrics.job_errors.items())
    )
    return (
        f"{head}\n# NeoSon — Memory Intelligence\n\n"
        "Só contagens: nomes, textos e fontes ficam no banco local.\n\n"
        "| Indicador | Valor |\n|---|---|\n"
        f"| Memórias curadas ativas / esquecidas | {metrics.curated.get('active', 0)}"
        f" / {metrics.curated.get('forgotten', 0)} |\n"
        f"| Fatos pendentes de revisão | {facts.get('pending', 0)} |\n"
        f"| Fatos ativos / retirados | {facts.get('active', 0)}"
        f" / {facts.get('retracted', 0)} |\n"
        f"| Contradições (mesmo trio ativo e retirado) | {metrics.contradictions} |\n"
        f"| Trios confirmados por mais de uma fonte | {metrics.corroborated} |\n"
        f"| Documentos ativos / esquecidos | {metrics.documents.get('active', 0)}"
        f" / {metrics.documents.get('forgotten', 0)} |\n\n"
        "## Fatos por tipo de fonte\n\n"
        "| Fonte | Pendentes | Ativos | Retirados |\n|---|---|---|---|\n"
        f"{sources or '| — | 0 | 0 | 0 |'}\n\n"
        "## Jobs de extração\n\n| Tipo / estado | Jobs |\n|---|---|\n"
        f"{jobs or '| — | 0 |'}\n\n"
        "## Erros que pedem atenção\n\n| Erro | Jobs |\n|---|---|\n"
        f"{errors or '| — | 0 |'}\n\n"
        "Revise os pendentes com `neoson-graph pending`; veja o runbook do grafo.\n"
    )
