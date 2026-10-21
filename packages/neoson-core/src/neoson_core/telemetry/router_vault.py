"""Dashboard de roteamento sem conversas, snapshots ou configuração privada."""

import sqlite3
from pathlib import Path

from neoson_core.engine import OutboxEvent
from neoson_core.telemetry.router_metrics import Arm, RouterMetrics, load_metrics
from neoson_core.telemetry.vault import frontmatter, write_atomic

HANDLER_NAME = "telemetry.router_vault"
PANEL = Path("Dashboard/Router-Lab.md")


def _value(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "sim" if value else "não"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def render_router(metrics: RouterMetrics) -> str:
    head = frontmatter(
        {
            "tipo": "neoson-dashboard",
            "painel": "Router Lab",
            "fonte_laboratorio": metrics.lab_state,
            "fonte_politicas": metrics.policy_state,
            "avaliacoes_total": metrics.total,
            "avaliacoes_exibidas": len(metrics.runs),
            "avaliacoes_invalidas_exibidas": sum(
                r.integrity == "invalid" for r in metrics.runs
            ),
            "politica_catalogo": metrics.active,
        }
    )
    lines = [
        head,
        "# Router Lab",
        "",
        f"Política selecionada no catálogo: {_value(metrics.active)}.",
        "",
        "Seleção histórica para sessões novas, sujeita à compatibilidade do runtime. "
        "Sessões existentes podem manter outra versão; o painel não representa uma "
        "aprovação nem garante a política efetiva de cada sessão.",
        "",
        "## Comparações",
        "",
        f"Fonte do laboratório: {metrics.lab_state}; últimas 100 avaliações "
        f"(total: {metrics.total}).",
        "",
    ]
    if not metrics.runs:
        lines.append(
            "Nenhuma avaliação disponível. Fonte ausente não é criada pela exportação."
        )
    else:
        lines.extend(
            [
                "| Avaliação | Dataset / split | Candidata | Estado | Modo | "
                "Evidência | Regressões | Economia (%) |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for run in metrics.runs:
            lines.append(
                f"| {run.id} | {run.dataset} {run.version} / {run.split} | "
                f"{run.policy} | {run.state} | {_value(run.mode)} | {run.integrity} | "
                f"{_value(run.regressions)} | {_value(run.savings)} |"
            )
        for run in metrics.runs:
            if run.baseline is None or run.candidate is None:
                continue
            lines.extend(
                [
                    "",
                    f"### Avaliação {run.id}",
                    "",
                    f"Criada em {run.created_at}. "
                    f"Baseline single-model-v1 → {run.policy}.",
                    "",
                    "| Indicador | Baseline | Candidata |",
                    "|---|---|---|",
                ]
            )
            for key, label in (
                ("cases", "Casos"),
                ("passed", "Casos aprovados"),
                ("quality_rate", "Qualidade final (0–1)"),
                ("routing_accuracy", "Acurácia conjunta (0–1)"),
                ("intent_accuracy", "Acurácia de intenção (0–1)"),
                ("complexity_accuracy", "Acurácia de complexidade (0–1)"),
                ("mode_accuracy", "Acurácia de modo (0–1)"),
                ("safety_failures", "Falhas de segurança"),
                ("input_tokens", "Tokens de entrada"),
                ("output_tokens", "Tokens de saída"),
                ("cached_tokens", "Tokens em cache"),
                ("cache_hit_ratio", "Cache (0–1)"),
                ("usage_complete", "Consumo completo"),
                ("attempts", "Tentativas"),
                ("cost_usd", "Custo conhecido completo (USD)"),
                ("seconds", "Duração total (s)"),
                ("escalation_rate", "Escalonamento (0–1)"),
            ):
                lines.append(
                    f"| {label} | {_value(run.baseline.metrics[key])} | "
                    f"{_value(run.candidate.metrics[key])} |"
                )
            lines.extend(_confusion("Baseline", run.baseline))
            lines.extend(_confusion("Candidata", run.candidate))
    lines.extend(
        [
            "",
            "## Histórico de decisões",
            "",
            f"Fonte das políticas: {metrics.policy_state}; últimas 100 decisões.",
            "",
            "| Ação | Versão | Data UTC |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        f"| {e.action} | {e.version} | {e.created_at} |" for e in metrics.events
    )
    if not metrics.events:
        lines.append("Nenhuma decisão registrada.")
    lines.extend(
        [
            "",
            "## Limites de interpretação",
            "",
            "— indica dado desconhecido ou indisponível, nunca custo zero presumido. "
            "Acurácia de rota e qualidade final são medidas distintas. "
            "Tokens/cache/custo "
            "do laboratório não entram nos agregados de uso das conversas.",
            "",
            "scripted usa métricas simuladas e não autoriza economia. openai indica o "
            "modo registrado, com ferramentas simuladas; economia só aparece com "
            "comparação completa, custo conhecido, baseline positivo, qualidade "
            "integral e sem regressões. "
            "O corpus pequeno não comprova ganho geral em produção.",
            "",
            "valid verifica integridade e estrutura da projeção histórica, "
            "não atualidade para promoção. invalid omite métricas inconsistentes; "
            "none indica execução ainda sem relatório concluído. A CLI revalida "
            "contexto, base e limites antes "
            "de qualquer aprovação.",
            "",
            "[[Dashboard/Home|Home]]",
            "",
        ]
    )
    return "\n".join(lines)


def _confusion(label: str, arm: Arm) -> list[str]:
    return [
        "",
        f"#### Matriz de confusão — {label}",
        "",
        "| Intenção esperada | Intenção prevista | Casos |",
        "|---|---|---|",
    ] + [
        f"| {expected} | {actual} | {count} |"
        for expected, actual, count in arm.confusion
    ]


class RouterVaultExporter:
    def __init__(self, vault: Path, policy_db: Path, lab_db: Path) -> None:
        self.vault, self.policy_db, self.lab_db = vault, policy_db, lab_db

    def export(self) -> bool:
        metrics = load_metrics(self.policy_db, self.lab_db)
        return write_atomic(self.vault / PANEL, render_router(metrics))

    def __call__(self, connection: sqlite3.Connection, event: OutboxEvent) -> None:
        self.export()
