"""Atualiza o painel Memory Intelligence depois de mudanças feitas pelas CLIs."""

from contextlib import closing
from zoneinfo import ZoneInfo

from neoson_core.infrastructure.config import ServerSettings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.vault_export import MemoryVaultExporter


def refresh_memory_panel(settings: ServerSettings) -> None:
    if not settings.metrics_export_enabled:
        return
    exporter = MemoryVaultExporter(settings.vault_path, ZoneInfo(settings.timezone))
    with closing(connect(settings.db_path)) as connection:
        exporter.export(connection)
