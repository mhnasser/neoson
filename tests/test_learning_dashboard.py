"""Ciclo completo em dados isolados: observar, propor, comparar, aprovar e voltar."""

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from neoson_core.engine import (
    AgentLoop,
    Budget,
    Engine,
    LLMError,
    SkillRegistry,
    ToolCatalog,
    UserMessage,
)
from neoson_core.infrastructure.config import ServerSettings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.curation import Curator
from neoson_core.skills.evaluation import SkillEvaluator
from neoson_core.skills.loader import load_skill
from neoson_core.skills.proposals import SqliteProposalStore
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.skills.review import Comparison, ProposalReview
from neoson_core.telemetry.pricing import Price
from neoson_core.telemetry.proposal_metrics import EVENT_TYPES, proposal_metrics
from neoson_core.tools.registry import ToolRegistry
from neoson_worker.main import build_handlers, export_vault
from neoson_worker.worker import Worker

PRIVATE = "conteudo-privado-nao-exportar"
INSTRUCTIONS = """# Resumo
## Quando usar
Para resumir.
## Passos
Leia e resuma.
## Restrições
Não invente fatos.
## Saída
Um resumo.
"""


def metadata(path: Path) -> dict[str, object]:
    result: dict[str, object] = yaml.safe_load(
        path.read_text(encoding="utf-8").split("---\n", 2)[1]
    )
    return result


@dataclass
class Learning:
    settings: ServerSettings
    worker: Worker
    proposal_id: UUID
    source: Path
    checksum: str

    @property
    def note(self) -> Path:
        return self.settings.vault_path / "Skills/Propostas" / f"{self.proposal_id}.md"

    def pump(self) -> None:
        for _ in range(5):
            if self.worker.run_once() == 0:
                return
        raise AssertionError("Possível loop de eventos.")

    def compare(
        self,
        outcomes: tuple[bool, ...] = (True, True, True, True),
        *,
        priced: bool = False,
    ) -> Comparison:
        counter = [0]

        def build(
            path: Path, registry: SkillRegistry, tools: ToolCatalog, now: datetime
        ) -> Engine:
            with closing(connect(path)) as c:
                version = c.execute(
                    "SELECT version FROM skills WHERE status='active'"
                ).fetchone()[0]
            index = counter[0]
            counter[0] += 1
            text = f"ok-resumo_{1 + index % 2}" if outcomes[index] else PRIVATE
            return AgentLoop(
                ScriptedProvider(
                    [
                        call_tool(
                            "skill_load", {"skill_id": "resumir", "version": version}
                        ),
                        reply(text),
                    ]
                ),
                RulesRouter("balanced"),
                Budget(5, 5),
                skills=registry,
                tools=tools,
                store=SqliteConversationStore(path),
                clock=lambda: now,
            )

        evaluator = SkillEvaluator(
            self.settings.db_path,
            build,
            lambda _: ToolRegistry([]),
            {"fake-model": Price(1, 2)} if priced else None,
        )
        return asyncio.run(
            ProposalReview(self.settings.db_path).compare(
                self.proposal_id, evaluator, "learning-test"
            )
        )


@pytest.fixture
def prepared(tmp_path: Path) -> Learning:
    source = tmp_path / "biblioteca/resumir"
    (source / "tests/fixtures").mkdir(parents=True)
    (source / "metadata.yaml").write_text(
        "id: resumir\nversion: 1.0.0\ndescription: Resume em poucos pontos.\n"
        "triggers: [resumir]\nallowed_tools: []\nrisk_level: low\nowner: neoson\n",
        encoding="utf-8",
    )
    (source / "SKILL.md").write_text(INSTRUCTIONS, encoding="utf-8")
    for n in (1, 2):
        (source / f"tests/fixtures/{n}.yaml").write_text(
            f'id: resumo_{n}\ninput: {PRIVATE}\nnow: "2026-10-20T09:00:00-03:00"\n'
            f"expect:\n  contains: [ok-resumo_{n}]\n",
            encoding="utf-8",
        )
    base = load_skill(source)
    settings = ServerSettings(
        _env_file=None,
        db_path=tmp_path / "neoson.db",
        vault_path=tmp_path / "vault",
        curation_enabled=True,
    )
    registry = SqliteSkillRegistry(settings.db_path)
    registry.approve(base)
    agent = AgentLoop(
        ScriptedProvider(
            [
                call_tool("skill_load", {"skill_id": "resumir", "version": "1.0.0"}),
                LLMError("erro simulado", retryable=False),
            ]
        ),
        RulesRouter("balanced"),
        Budget(5, 5),
        skills=registry,
        store=SqliteConversationStore(settings.db_path),
    )
    asyncio.run(agent.run(UserMessage(PRIVATE)))
    worker = Worker(settings.db_path, build_handlers(settings), "learning-worker")
    worker.run_once()
    assert SqliteProposalStore(settings.db_path).list() == []
    curator = Curator(
        settings.db_path,
        ScriptedProvider(
            [
                reply(
                    json.dumps(
                        {
                            "instructions": base.instructions
                            + "\nConfira a fonte. "
                            + PRIVATE
                        }
                    )
                ),
            ]
        ),
    )
    assert asyncio.run(curator.run_once())
    proposal = SqliteProposalStore(settings.db_path).list()[0]
    assert proposal.candidate.metadata.version == "1.1.0"
    assert registry.approved_package("resumir").metadata.version == "1.0.0"
    learning = Learning(settings, worker, proposal.id, source, base.checksum)
    learning.pump()
    return learning


def test_end_to_end_learning_and_rollback_without_automatic_activation(
    prepared: Learning,
) -> None:
    settings = prepared.settings
    assert metadata(prepared.note)["estado"] == "pending"
    assert metadata(prepared.note)["comparacao_id"] is None
    comparison = prepared.compare()
    prepared.pump()
    assert metadata(prepared.note)["estado"] == "evaluated"
    assert metadata(prepared.note)["regressoes"] == 0
    assert metadata(prepared.note)["candidata_ativa"] is False
    assert load_skill(prepared.source).checksum == prepared.checksum
    review = ProposalReview(settings.db_path)
    assert review.registry.approved_package("resumir").metadata.version == "1.0.0"
    review.approve(
        prepared.proposal_id,
        comparison.id,
        "learning-test",
        accept_unknown_cost=True,
        accept_resource_increase=True,
    )
    prepared.pump()
    assert metadata(prepared.note)["estado"] == "approved"
    assert metadata(prepared.note)["candidata_ativa"] is True
    review.registry.rollback("resumir")
    prepared.pump()
    assert metadata(prepared.note)["estado"] == "approved"  # decisão histórica
    assert metadata(prepared.note)["candidata_ativa"] is False
    assert (
        metadata(settings.vault_path / "Skills/Ativas/resumir.md")["versao"] == "1.0.0"
    )
    assert load_skill(prepared.source).checksum == prepared.checksum
    with closing(connect(settings.db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM curation_jobs").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE status!='completed'"
            ).fetchone()[0]
            == 0
        )
    for path in settings.vault_path.rglob("*.md"):
        assert PRIVATE not in path.read_text(encoding="utf-8")
        assert "Confira a fonte." not in path.read_text(encoding="utf-8")
        metadata(path)


def test_regressions_counts_cost_privacy_and_idempotence(prepared: Learning) -> None:
    result = prepared.compare((True, False, False, True))
    prepared.pump()
    data = metadata(prepared.note)
    assert data["base_aprovados"] == data["candidata_aprovados"] == 1
    assert data["regressoes"] == data["melhorias"] == 1
    assert data["base_custo_usd"] is None and data["candidata_custo_usd"] is None
    panel = prepared.settings.vault_path / "Dashboard/Skills-Learning.md"
    assert metadata(panel)["regressoes_ultimas_comparacoes"] == 1
    assert metadata(panel)["propostas_evaluated"] == 1
    assert str(result.id) in prepared.note.read_text(encoding="utf-8")
    assert PRIVATE not in prepared.note.read_text(encoding="utf-8")
    assert export_vault(prepared.settings) == 0
    before = {
        p: p.stat().st_mtime_ns for p in prepared.settings.vault_path.rglob("*.md")
    }
    assert export_vault(prepared.settings) == 0
    # Notas diárias novas podem ser criadas; notas existentes não são regravadas.
    assert before == {p: p.stat().st_mtime_ns for p in before}


def test_rejection_and_review_in_progress_preserve_previous_metrics(
    prepared: Learning,
) -> None:
    prepared.compare()
    prepared.pump()
    with closing(connect(prepared.settings.db_path)) as c:
        c.execute("UPDATE skill_proposals SET status='pending',review_token=NULL")
    export_vault(prepared.settings, days=1)
    assert metadata(prepared.note)["evidencia"] == "previous"
    SqliteProposalStore(prepared.settings.db_path).reject(prepared.proposal_id)
    prepared.pump()
    assert metadata(prepared.note)["estado"] == "rejected"
    assert metadata(prepared.note)["comparacao_atual"] is False


def test_corrupted_evidence_is_not_exported(prepared: Learning) -> None:
    prepared.compare()
    prepared.pump()
    with closing(connect(prepared.settings.db_path)) as c:
        c.execute(
            "UPDATE skill_proposal_comparisons SET candidate_report_json=?", (PRIVATE,)
        )
    export_vault(prepared.settings, days=1)
    assert metadata(prepared.note)["evidencia"] == "invalid"
    assert metadata(prepared.note)["regressoes"] is None
    assert PRIVATE not in prepared.note.read_text(encoding="utf-8")


def test_projection_never_reads_snapshot_or_transcript(prepared: Learning) -> None:
    prepared.compare()

    def authorize(
        action: int,
        table: str | None,
        column: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and (
            table == "messages" or column == "files_json"
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with closing(connect(prepared.settings.db_path)) as c:
        c.set_authorizer(authorize)
        values = proposal_metrics(c)
    assert len(values) == 1 and values[0].comparison is not None


def test_non_numeric_private_field_with_recomputed_hash_is_redacted(
    prepared: Learning,
) -> None:
    prepared.compare()
    with closing(connect(prepared.settings.db_path)) as c:
        row = c.execute("SELECT * FROM skill_proposal_comparisons").fetchone()
        candidate = json.loads(row["candidate_report_json"])
        candidate["cases"][0]["usage"]["input_tokens"] = PRIVATE
        candidate_text = json.dumps(candidate)
        values = (
            row["id"],
            row["proposal_id"],
            row["created_at"],
            row["base_revision"],
            row["context_hash"],
            row["base_report_json"],
            candidate_text,
        )
        digest = hashlib.sha256(
            json.dumps(values, ensure_ascii=False).encode()
        ).hexdigest()
        c.execute(
            "UPDATE skill_proposal_comparisons SET candidate_report_json=?,"
            "evidence_checksum=?",
            (candidate_text, digest),
        )
    export_vault(prepared.settings, days=1)
    assert metadata(prepared.note)["evidencia"] == "invalid"
    assert PRIVATE not in prepared.note.read_text(encoding="utf-8")


def test_known_cost_and_source_counts_are_aggregated(prepared: Learning) -> None:
    result = prepared.compare(priced=True)
    prepared.pump()
    data = metadata(prepared.note)
    assert data["fontes_execucoes"] == 1 and data["fontes_avaliacoes"] == 0
    assert data["base_custo_usd"] == result.base.cost_usd
    assert data["candidata_custo_usd"] == result.candidate.cost_usd
    assert result.base.cost_usd is not None and result.base.cost_usd > 0


def test_atomic_proposal_note_retry(
    prepared: Learning, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = prepared.note.read_bytes()
    prepared.compare()
    replace = os.replace

    def fail(source: Path, target: Path) -> None:
        if target == prepared.note:
            raise OSError("rename indisponível")
        replace(source, target)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        export_vault(prepared.settings, days=1)
    assert prepared.note.read_bytes() == original
    monkeypatch.setattr(os, "replace", replace)
    export_vault(prepared.settings, days=1)
    assert metadata(prepared.note)["estado"] == "evaluated"
    assert not prepared.note.with_name("." + prepared.note.name + ".tmp").exists()


@pytest.mark.parametrize("enabled", [True, False])
def test_proposal_events_only_export_metrics(enabled: bool) -> None:
    settings = ServerSettings(_env_file=None, metrics_export_enabled=enabled)
    handlers = build_handlers(settings)
    for event in EVENT_TYPES:
        assert [h.name for h in handlers[event]] == (
            ["telemetry.vault"] if enabled else []
        )


def test_invalid_proposal_path_is_rejected(prepared: Learning) -> None:
    with closing(connect(prepared.settings.db_path)) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("UPDATE skill_proposals SET id='../../escape'")
    with pytest.raises(ValueError, match="Metadados de proposta"):
        export_vault(prepared.settings, days=1)
    assert not (prepared.settings.vault_path.parent / "escape.md").exists()
