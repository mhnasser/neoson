import argparse
import logging
import os
import signal
import socket
import threading
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from neoson_core.infrastructure.config import (
    ConfigError,
    ServerSettings,
    load_settings,
)
from neoson_core.infrastructure.db import connect, migrate
from neoson_core.memory import curated, vault_export
from neoson_core.skills import curation
from neoson_core.telemetry import proposal_metrics, skill_metrics, usage, vault
from neoson_core.telemetry.pricing import Price
from neoson_worker.curation_main import limits_for
from neoson_worker.worker import Handler, RetryPolicy, Worker


def build_handlers(settings: ServerSettings | None = None) -> dict[str, list[Handler]]:
    """Uso e custo primeiro; depois o vault, que já lê o custo desta execução."""
    prices = {
        model: Price(p.input, p.output, p.cached_input)
        for model, p in (settings.llm_prices if settings else {}).items()
    }
    handlers = [Handler(usage.HANDLER_NAME, usage.UsageRecorder(prices))]
    memory_handlers = []
    skill_handlers = []
    if settings is not None and settings.metrics_export_enabled:
        zone = ZoneInfo(settings.timezone)
        exporter = vault.VaultExporter(settings.vault_path, zone)
        handlers.append(Handler(vault.HANDLER_NAME, exporter))
        skill_handlers.append(Handler(vault.HANDLER_NAME, exporter))
        memory_exporter = vault_export.MemoryVaultExporter(settings.vault_path, zone)
        memory_handlers.append(Handler(vault_export.HANDLER_NAME, memory_exporter))
    routes = {event_type: list(handlers) for event_type in usage.EVENT_TYPES}
    routes[curated.EVENT_TYPE] = memory_handlers
    routes.update(
        {event_type: list(skill_handlers) for event_type in skill_metrics.EVENT_TYPES}
    )
    routes.update(
        {
            event_type: list(skill_handlers)
            for event_type in proposal_metrics.EVENT_TYPES
        }
    )
    if settings is not None and settings.curation_enabled:
        scheduler = Handler(
            curation.HANDLER_NAME, curation.Scheduler(limits_for(settings))
        )
        for kind in curation.EVENT_TYPES:
            routes.setdefault(kind, []).append(scheduler)
    return routes


def export_vault(settings: ServerSettings, days: int = 7) -> int:
    zone = ZoneInfo(settings.timezone)
    exporter = vault.VaultExporter(settings.vault_path, zone)
    memory_exporter = vault_export.MemoryVaultExporter(settings.vault_path, zone)
    today = datetime.now(zone).date()
    with closing(connect(settings.db_path)) as connection:
        migrate(connection)
        changed = {
            path
            for offset in range(days)
            for path in exporter.export(connection, today - timedelta(days=offset))
        }
        changed.update(memory_exporter.export(connection))
    logging.getLogger("neoson_worker").info(
        "vault em %s: %d arquivo(s) atualizado(s)", settings.vault_path, len(changed)
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-worker", description="Consome os eventos da outbox do NeoSon."
    )
    parser.add_argument(
        "--once", action="store_true", help="processa o que estiver pronto e sai"
    )
    parser.add_argument(
        "--export-vault",
        action="store_true",
        help="regera no vault as notas dos últimos 7 dias e os dashboards, e sai",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.export_vault:
        return export_vault(settings)

    worker = Worker(
        db_path=settings.db_path,
        handlers=build_handlers(settings),
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
        lease_seconds=settings.worker_lease_seconds,
        retry=RetryPolicy(max_attempts=settings.worker_max_attempts),
    )
    if args.once:
        processed = worker.run_once()
        logging.getLogger("neoson_worker").info("%d evento(s) reservado(s)", processed)
        return 0

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    worker.run_forever(stop, settings.worker_poll_seconds)
    return 0
