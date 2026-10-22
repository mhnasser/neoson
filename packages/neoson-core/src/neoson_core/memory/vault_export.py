"""Handler do worker: mostra a memória curada no vault (`Memoria/`) para revisão.

Diferente das métricas, aqui vai o texto dos fatos: é a interface humana da memória
(§9, §13.2). O vault é local e fica fora do Git.
"""

import sqlite3
from datetime import tzinfo
from pathlib import Path

from neoson_core.engine import MemoryFact, OutboxEvent
from neoson_core.memory.curated import fact_from_row
from neoson_core.telemetry.memory_metrics import PANEL as MEMORY_PANEL
from neoson_core.telemetry.memory_metrics import memory_metrics, render_memory
from neoson_core.telemetry.vault import frontmatter, write_atomic

HANDLER_NAME = "memory.vault"
FILES = {
    "user": Path("Memoria") / "Usuario" / "Fatos.md",
    "neoson": Path("Memoria") / "NeoSon" / "Fatos.md",
}
TITLES = {"user": "Memória — Usuário", "neoson": "Memória — NeoSon"}


class MemoryVaultExporter:
    def __init__(self, vault: Path, zone: tzinfo) -> None:
        self._vault = vault
        self._zone = zone

    def __call__(self, connection: sqlite3.Connection, event: OutboxEvent) -> None:
        self.export(connection)

    def export(self, connection: sqlite3.Connection) -> list[Path]:
        changed = []
        for namespace, relative in FILES.items():
            rows = connection.execute(
                "SELECT * FROM memory_facts WHERE namespace = ? AND status = 'active'"
                " ORDER BY created_at, id",
                (namespace,),
            ).fetchall()
            forgotten = connection.execute(
                "SELECT COUNT(*) FROM memory_facts"
                " WHERE namespace = ? AND status = 'forgotten'",
                (namespace,),
            ).fetchone()[0]
            facts = [fact_from_row(row) for row in rows]
            lines = [self._line(fact) for fact in facts]
            text = (
                frontmatter(
                    {
                        "tipo": "neoson-memoria",
                        "namespace": namespace,
                        "fatos_ativos": len(facts),
                        "fatos_esquecidos": forgotten,
                    }
                )
                + f"\n# {TITLES[namespace]}\n\n"
                + ("\n".join(lines) if lines else "Nenhum fato registrado.")
                + "\n\nPara corrigir, peça ao NeoSon que esqueça o fato pelo número.\n"
            )
            if write_atomic(self._vault / relative, text):
                changed.append(self._vault / relative)
        panel = render_memory(memory_metrics(connection))
        if write_atomic(self._vault / MEMORY_PANEL, panel):
            changed.append(self._vault / MEMORY_PANEL)
        return changed

    def _line(self, fact: MemoryFact) -> str:
        since = f"{fact.created_at.astimezone(self._zone):%d/%m/%Y %H:%M}"
        source = (
            f" · sessão {str(fact.source_session_id)[:8]}"
            if fact.source_session_id
            else ""
        )
        return f"- **#{fact.id}** {fact.content}  \n  _desde {since}{source}_"
