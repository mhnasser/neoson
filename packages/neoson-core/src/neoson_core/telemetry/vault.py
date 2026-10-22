"""Exportação para o vault do Obsidian (§13, D14): notas Markdown com YAML e agregados.

Escrita atômica (`.tmp` → rename) e idempotente: sem mudança nos dados, nenhum arquivo
é reescrito. Nada de prompts, mensagens ou segredos.
"""

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from uuid import UUID, uuid4

from neoson_core.engine import OutboxEvent
from neoson_core.telemetry.metrics import (
    DailyMetrics,
    ModelUsage,
    SystemMetrics,
    daily_metrics,
    model_usage,
    recent_days,
    system_metrics,
)
from neoson_core.telemetry.proposal_metrics import (
    STATES,
    ProposalMetrics,
    ReportMetrics,
    proposal_metrics,
)
from neoson_core.telemetry.skill_metrics import SkillMetrics, skill_metrics

HANDLER_NAME = "telemetry.vault"
DAILY_DIR = Path("Metricas") / "Diarias"
DASHBOARD_DIR = Path("Dashboard")
ACTIVE_SKILLS_DIR = Path("Skills") / "Ativas"
PROPOSALS_DIR = Path("Skills") / "Propostas"


def write_atomic(path: Path, text: str) -> bool:
    """Grava só se o conteúdo mudou; devolve se o arquivo foi alterado."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def frontmatter(values: Mapping[str, object]) -> str:
    """YAML simples e seguro: strings entre aspas duplas (JSON é YAML válido)."""
    lines = ["---"]
    for key, value in values.items():
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render_daily(metrics: DailyMetrics) -> str:
    head = frontmatter(
        {
            "tipo": "neoson-metricas",
            "data": metrics.day.isoformat(),
            "execucoes_total": metrics.runs_total,
            "concluidas": metrics.completed,
            "falhas": metrics.failed,
            "aguardando_aprovacao": metrics.waiting_approval,
            "sucesso_pct": metrics.success_pct,
            "tokens_entrada": metrics.input_tokens,
            "tokens_saida": metrics.output_tokens,
            "tokens_cache": metrics.cached_tokens,
            "cache_pct": metrics.cache_pct,
            "custo_usd": metrics.cost_usd,
            "execucoes_sem_custo": metrics.runs_without_cost,
            "execucoes_uso_incompleto": metrics.runs_usage_incomplete,
            "delegacoes": metrics.delegation_attempts,
            "delegacoes_falhas": metrics.delegation_failed,
            "delegacoes_recusadas": metrics.delegation_rejected,
            "tokens_delegacao": metrics.delegation_tokens,
            "modelos": list(metrics.models),
        }
    )
    models = "\n".join(
        f"| {name} | {count} |" for name, count in metrics.models.items()
    )
    return (
        f"{head}\n# Métricas de {metrics.day:%d/%m/%Y}\n\n"
        f"| Indicador | Valor |\n|---|---|\n"
        f"| Execuções | {metrics.runs_total} |\n"
        f"| Concluídas | {metrics.completed} |\n"
        f"| Falhas | {metrics.failed} |\n"
        f"| Aguardando aprovação | {metrics.waiting_approval} |\n"
        f"| Sucesso | {_pct(metrics.success_pct)} |\n"
        f"| Tokens (entrada / saída / cache) | {metrics.input_tokens} / "
        f"{metrics.output_tokens} / {metrics.cached_tokens} |\n"
        f"| Custo estimado | {_usd(metrics.cost_usd)} |\n"
        f"| Execuções com uso incompleto | {metrics.runs_usage_incomplete} |\n"
        "| Delegações (tentativas / falhas / recusadas) | "
        f"{metrics.delegation_attempts} / {metrics.delegation_failed} / "
        f"{metrics.delegation_rejected} |\n"
        f"| Tokens de delegação | {metrics.delegation_tokens} |\n\n"
        "## Execuções por modelo\n\n| Modelo | Execuções |\n|---|---|\n"
        f"{models or '| — | 0 |'}\n"
    )


def render_home(
    days: Sequence[DailyMetrics], system: SystemMetrics, zone: tzinfo
) -> str:
    warnings = []
    if system.dead_letters:
        warnings.append(f"- {system.dead_letters} evento(s) em dead letter.")
    if system.oldest_retry_at is not None:
        warnings.append(
            f"- {system.retrying} evento(s) em nova tentativa, o mais antigo de "
            f"{system.oldest_retry_at.astimezone(zone):%d/%m/%Y %H:%M}."
        )
    rows = "\n".join(
        f"| [[{_daily_link(d.day)}\\|{d.day:%d/%m}]] | {d.runs_total} | "
        f"{_pct(d.success_pct)} | {d.input_tokens + d.output_tokens} | "
        f"{_usd(d.cost_usd)} |"
        for d in days
    )
    last = (
        f"{system.last_run_at.astimezone(zone):%d/%m/%Y %H:%M}"
        if system.last_run_at
        else "nenhuma"
    )
    return (
        frontmatter({"tipo": "neoson-dashboard", "painel": "Home"})
        + "\n# NeoSon — Home\n\n"
        f"Última execução: {last}\n\n"
        "## Últimos 7 dias\n\n"
        "| Dia | Execuções | Sucesso | Tokens | Custo |\n|---|---|---|---|---|\n"
        f"{rows}\n\n## Avisos\n\n"
        + ("\n".join(warnings) if warnings else "Nenhum aviso.")
        + "\n\nPainéis: [[Dashboard/LLM-Custos|LLM & Custos]] · "
        "[[Dashboard/NeoSon-System|NeoSon System]] · "
        "[[Dashboard/Skills-Learning|Skills & Learning]] · "
        "[[Dashboard/Router-Lab|Router Lab]]\n"
    )


def render_costs(usage: Sequence[ModelUsage]) -> str:
    rows = "\n".join(
        f"| {u.model} | {u.runs} | {u.input_tokens} | {u.output_tokens} | "
        f"{u.cached_tokens} | {_usd(u.cost_usd)} |"
        for u in usage
    )
    return (
        frontmatter({"tipo": "neoson-dashboard", "painel": "LLM & Custos"})
        + "\n# LLM & Custos — últimos 30 dias\n\n"
        "| Modelo | Execuções | Entrada | Saída | Cache | Custo |\n"
        "|---|---|---|---|---|---|\n"
        + (rows or "| — | 0 | 0 | 0 | 0 | — |")
        + "\n\nCusto estimado com os preços de `NEOSON_LLM_PRICES`; "
        "sem preço configurado, aparece como —.\n"
    )


def render_system(system: SystemMetrics) -> str:
    rows = "\n".join(
        f"| {status} | {count} |" for status, count in system.outbox.items()
    )
    lag = f"{system.avg_lag_seconds} s" if system.avg_lag_seconds is not None else "—"
    return (
        frontmatter(
            {
                "tipo": "neoson-dashboard",
                "painel": "NeoSon System",
                "dead_letters": system.dead_letters,
                "atraso_medio_worker_s": system.avg_lag_seconds,
            }
        )
        + "\n# NeoSon System\n\n## Fila (outbox)\n\n| Estado | Eventos |\n|---|---|\n"
        + (rows or "| — | 0 |")
        + f"\n\nAtraso médio do worker (últimos 100 eventos): {lag}\n"
    )


def render_skills(
    skills: Sequence[SkillMetrics],
    proposals: Sequence[ProposalMetrics] = (),
) -> str:
    rows = []
    for skill in skills:
        name = (
            f"[[Skills/Ativas/{skill.id}\\|{skill.id}]]"
            if skill.status == "active"
            else skill.id
        )
        rows.append(
            f"| {name} | {skill.version} | {skill.status} | {skill.runs} | "
            f"{_pct(skill.success_pct)} | "
            f"{skill.input_tokens} / {skill.output_tokens} / {skill.cached_tokens} | "
            f"{_usd(skill.cost_usd)} | {skill.runs_without_cost} | "
            f"{_evaluation(skill)} |"
        )
    return (
        frontmatter(
            {
                "tipo": "neoson-dashboard",
                "painel": "Skills & Learning",
                "skills_ativas": sum(s.status == "active" for s in skills),
                "versoes_aprovadas": len(skills),
                "propostas_total": len(proposals),
                **{
                    f"propostas_{state}": sum(p.status == state for p in proposals)
                    for state in STATES
                },
                "regressoes_ultimas_comparacoes": sum(
                    p.comparison.regressions
                    for p in proposals
                    if p.comparison is not None
                ),
            }
        )
        + "\n# Skills & Learning\n\n"
        "Histórico integral por versão. Sucesso = concluídas / execuções, "
        "incluindo falhas e esperas por aprovação no denominador.\n\n"
        "| Skill | Versão | Estado | Execuções | Sucesso | "
        "Tokens (entrada / saída / cache) | Custo conhecido | Sem custo | "
        "Última avaliação (UTC; casos aprovados/total) |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        + ("\n".join(rows) if rows else "Nenhuma skill aprovada.")
        + "\n\nCusto conhecido pode ser parcial; Sem custo conta execuções sem preço "
        "ou ainda não processadas pelo worker. — indica dado indisponível. "
        "Avaliações com fixtures não entram nas execuções nem no custo de uso.\n"
        + render_learning(proposals)
    )


def render_learning(proposals: Sequence[ProposalMetrics]) -> str:
    rows = []
    for p in proposals:
        result = p.comparison
        before = "—" if result is None else f"{result.base.passed}/{result.base.cases}"
        after = (
            "—"
            if result is None
            else f"{result.candidate.passed}/{result.candidate.cases}"
        )
        regressions = "—" if result is None else str(result.regressions)
        rows.append(
            f"| [[Skills/Propostas/{p.id}\\|{p.id[:8]}]] | {p.skill_id} | "
            f"{p.base_version} → {p.candidate_version} | {p.status} | "
            f"{before} → {after} | {regressions} | {p.evidence_state} |"
        )
    return (
        "\n## Propostas e aprendizado\n\n"
        "| Proposta | Skill | Versões | Estado | Casos aprovados (base → candidata) | "
        "Regressões | Evidência |\n|---|---|---|---|---|---|---|\n"
        + ("\n".join(rows) if rows else "Nenhuma proposta registrada.")
        + "\n\nMétricas da última comparação concluída, separadas do uso normal. "
        "current = referência atual da revisão; "
        "previous = comparação anterior ou histórica; "
        "invalid = evidência indisponível por integridade; none = sem comparação. "
        "Aprovações são decisões históricas; "
        "a nota informa se a candidata ainda está ativa. "
        "O painel não autoriza aprovação: a CLI revalida a base e as evidências.\n"
    )


def render_proposal(p: ProposalMetrics) -> str:
    values: dict[str, object] = {
        "tipo": "neoson-proposta",
        "proposta_id": p.id,
        "skill_id": p.skill_id,
        "versao_base": p.base_version,
        "versao_candidata": p.candidate_version,
        "estado": p.status,
        "criada_em": p.created_at,
        "fontes_execucoes": p.source_runs,
        "fontes_avaliacoes": p.source_evaluations,
        "candidata_ativa": p.candidate_active,
        "evidencia": p.evidence_state,
    }
    rows = []
    result = p.comparison
    if result is not None:
        values.update(
            {
                "comparacao_id": result.id,
                "comparada_em": result.created_at,
                "comparacao_atual": result.current,
                "regressoes": result.regressions,
                "melhorias": result.improvements,
            }
        )
        for label, metrics in (("base", result.base), ("candidata", result.candidate)):
            values.update(_report_values(label, metrics))
            rows.append(
                f"| {label} | {metrics.passed}/{metrics.cases} | "
                f"{metrics.input_tokens} / {metrics.output_tokens} / "
                f"{metrics.cached_tokens} | "
                f"{_usd(metrics.cost_usd)} | {metrics.seconds:.3f}s |"
            )
    else:
        values.update(
            {
                "comparacao_id": None,
                "comparada_em": None,
                "comparacao_atual": False,
                "regressoes": None,
                "melhorias": None,
            }
        )
    return (
        frontmatter(values) + f"\n# Proposta {p.id[:8]} — {p.skill_id}\n\n"
        f"Versão {p.base_version} → {p.candidate_version}; estado: {p.status}.\n\n"
        f"Candidata ativa: {'sim' if p.candidate_active else 'não'}. "
        f"Evidência: {p.evidence_state}.\n\n"
        "| Versão | Casos aprovados | Tokens (entrada / saída / cache) | "
        "Custo | Duração |\n"
        "|---|---|---|---|---|\n"
        + ("\n".join(rows) if rows else "Sem comparação disponível.")
        + "\n\n— indica custo desconhecido. "
        "Nenhum procedimento ou conversa é exportado. "
        "Revisão e aprovação são feitas explicitamente pela CLI.\n\n"
        "[[Dashboard/Skills-Learning|Skills & Learning]]\n"
    )


def _report_values(label: str, r: ReportMetrics) -> dict[str, object]:
    return {
        f"{label}_casos": r.cases,
        f"{label}_aprovados": r.passed,
        f"{label}_tokens_entrada": r.input_tokens,
        f"{label}_tokens_saida": r.output_tokens,
        f"{label}_tokens_cache": r.cached_tokens,
        f"{label}_custo_usd": r.cost_usd,
        f"{label}_duracao_s": r.seconds,
    }


def render_skill(skill: SkillMetrics) -> str:
    return (
        frontmatter(
            {
                "tipo": "neoson-skill",
                "skill_id": skill.id,
                "versao": skill.version,
                "estado": skill.status,
                "execucoes_total": skill.runs,
                "concluidas": skill.completed,
                "falhas": skill.failed,
                "aguardando_aprovacao": skill.waiting_approval,
                "sucesso_pct": skill.success_pct,
                "tokens_entrada": skill.input_tokens,
                "tokens_saida": skill.output_tokens,
                "tokens_cache": skill.cached_tokens,
                "custo_usd": skill.cost_usd,
                "execucoes_sem_custo": skill.runs_without_cost,
                "ultima_avaliacao": skill.evaluated_at,
                "avaliacao_casos_total": skill.total_cases,
                "avaliacao_casos_aprovados": skill.passed_cases,
                "avaliacao_tokens_entrada": skill.evaluation_input_tokens,
                "avaliacao_tokens_saida": skill.evaluation_output_tokens,
                "avaliacao_tokens_cache": skill.evaluation_cached_tokens,
                "avaliacao_custo_usd": skill.evaluation_cost_usd,
                "avaliacao_duracao_s": skill.evaluation_seconds,
            }
        )
        + f"\n# {skill.id} — {skill.version}\n\n"
        f"Execuções: {skill.runs}; concluídas: {skill.completed}; "
        f"falhas: {skill.failed}; aguardando aprovação: {skill.waiting_approval}.\n\n"
        f"Sucesso: {_pct(skill.success_pct)}. "
        f"Custo conhecido: {_usd(skill.cost_usd)} "
        f"({skill.runs_without_cost} execução/execuções sem custo).\n\n"
        f"Última avaliação: {_evaluation(skill)}. "
        f"Custo da avaliação: {_usd(skill.evaluation_cost_usd)}.\n\n"
        "[[Dashboard/Skills-Learning|Skills & Learning]]\n"
    )


def _evaluation(skill: SkillMetrics) -> str:
    if skill.evaluated_at is None:
        return "—"
    when = datetime.fromisoformat(skill.evaluated_at).astimezone(UTC)
    return f"{when:%Y-%m-%d %H:%M:%S} UTC; {skill.passed_cases}/{skill.total_cases}"


class VaultExporter:
    """Handler do worker: atualiza a nota do dia do evento e os dashboards."""

    def __init__(self, vault: Path, zone: tzinfo) -> None:
        self._vault = vault
        self._zone = zone

    def __call__(self, connection: sqlite3.Connection, event: OutboxEvent) -> None:
        day = event.occurred_at.astimezone(self._zone).date()
        self.export(connection, day, completing=event.event_id)

    def export(
        self,
        connection: sqlite3.Connection,
        day: date,
        completing: UUID | None = None,
    ) -> list[Path]:
        """Exporta e devolve os arquivos alterados."""
        today = datetime.now(self._zone).date()
        system = system_metrics(connection, completing)
        skills = skill_metrics(connection)
        proposals = proposal_metrics(connection)
        # IDs vêm do registro validado, mas nunca confiar em um caminho vindo do DB.
        if any(re.fullmatch(r"[a-z][a-z0-9_]{1,63}", s.id) is None for s in skills):
            raise ValueError("ID de skill inválido para exportação.")
        files = {
            self._vault / DAILY_DIR / f"{day.isoformat()}.md": render_daily(
                daily_metrics(connection, day, self._zone)
            ),
            self._vault / DASHBOARD_DIR / "Home.md": render_home(
                recent_days(connection, today, self._zone), system, self._zone
            ),
            self._vault / DASHBOARD_DIR / "LLM-Custos.md": render_costs(
                model_usage(connection, today, self._zone)
            ),
            self._vault / DASHBOARD_DIR / "NeoSon-System.md": render_system(system),
            self._vault / DASHBOARD_DIR / "Skills-Learning.md": render_skills(
                skills, proposals
            ),
        }
        files.update(
            {
                self._vault / ACTIVE_SKILLS_DIR / f"{s.id}.md": render_skill(s)
                for s in skills
                if s.status == "active"
            }
        )
        files.update(
            {
                self._vault / PROPOSALS_DIR / f"{p.id}.md": render_proposal(p)
                for p in proposals
            }
        )
        changed = [path for path, text in files.items() if write_atomic(path, text)]
        active = {s.id for s in skills if s.status == "active"}
        for skill_id in sorted({s.id for s in skills} - active):
            path = self._vault / ACTIVE_SKILLS_DIR / f"{skill_id}.md"
            marker = f'---\ntipo: "neoson-skill"\nskill_id: "{skill_id}"\n'
            if path.exists() and path.read_text(encoding="utf-8").startswith(marker):
                path.unlink()
                changed.append(path)
        return changed


def _daily_link(day: date) -> str:
    return f"{DAILY_DIR.as_posix()}/{day.isoformat()}"


def _yaml_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        scalar = repr(value)
        if isinstance(value, float) and "e" in scalar:
            mantissa, exponent = scalar.split("e")
            if "." not in mantissa:
                scalar = f"{mantissa}.0e{exponent}"
        return scalar
    if isinstance(value, list):
        return json.dumps([str(item) for item in value], ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value}%"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"US$ {value:.4f}"
