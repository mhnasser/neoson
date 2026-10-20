"""Exportação para o vault do Obsidian (§13, D14): notas Markdown com YAML e agregados.

Escrita atômica (`.tmp` → rename) e idempotente: sem mudança nos dados, nenhum arquivo
é reescrito. Nada de prompts, mensagens ou segredos.
"""

import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, tzinfo
from pathlib import Path
from uuid import UUID

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

HANDLER_NAME = "telemetry.vault"
DAILY_DIR = Path("Metricas") / "Diarias"
DASHBOARD_DIR = Path("Dashboard")


def write_atomic(path: Path, text: str) -> bool:
    """Grava só se o conteúdo mudou; devolve se o arquivo foi alterado."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
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
        f"| Custo estimado | {_usd(metrics.cost_usd)} |\n\n"
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
        "[[Dashboard/NeoSon-System|NeoSon System]]\n"
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
        }
        return [path for path, text in files.items() if write_atomic(path, text)]


def _daily_link(day: date) -> str:
    return f"{DAILY_DIR.as_posix()}/{day.isoformat()}"


def _yaml_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return json.dumps([str(item) for item in value], ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value}%"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"US$ {value:.4f}"
