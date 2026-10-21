import asyncio
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from neoson_core.engine import (
    AgentLoop,
    Budget,
    Engine,
    OutboxEvent,
    SkillRegistry,
    ToolCatalog,
    Usage,
)
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.config import load_settings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.evaluation import Evaluation, SkillEvaluator
from neoson_core.skills.loader import SkillPackage, package_from_texts
from neoson_core.skills.proposals import (
    ProposalConflict,
    ProposalSource,
    SqliteProposalStore,
)
from neoson_core.skills.registry import (
    SkillConflict,
    SkillIntegrityError,
    SqliteSkillRegistry,
)
from neoson_core.skills.review import Comparison, ProposalReview
from neoson_core.telemetry.pricing import Price
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.skills_main import main
from neoson_gateway.skills_review import context_hash

PRIVATE = "dado-privado-da-fixture"
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


def package(version: str = "1.0.0") -> SkillPackage:
    texts = {
        "metadata.yaml": f"id: resumir\nversion: {version}\n"
        "description: Resume em poucos pontos.\ntriggers: [resumir]\n"
        "allowed_tools: []\nrisk_level: low\nowner: neoson\n",
        "SKILL.md": INSTRUCTIONS
        + ("\nConfira a fonte.\n" if version != "1.0.0" else ""),
    }
    for case in ("aa", "bb"):
        texts[f"tests/fixtures/{case}.yaml"] = (
            f'id: {case}\ninput: {PRIVATE}\nnow: "2026-10-20T09:00:00-03:00"\n'
            f"expect:\n  contains: [ok-{case}]\n"
        )
    return package_from_texts(Path("resumir"), texts)


def evaluator(
    db: Path,
    outcomes: tuple[bool, ...] = (True, True, True, True),
    *,
    priced: bool = False,
) -> SkillEvaluator:
    calls = [0]

    def engine(
        path: Path, registry: SkillRegistry, tools: ToolCatalog, now: datetime
    ) -> Engine:
        with closing(connect(path)) as c:
            assert c.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 1
            version = c.execute("SELECT version FROM skills").fetchone()[0]
        index = calls[0]
        calls[0] += 1
        text = (
            f"ok-{('aa', 'bb')[index % 2]}"
            if outcomes[index]
            else "resposta incompleta"
        )
        return AgentLoop(
            ScriptedProvider(
                [
                    call_tool(
                        "skill_load", {"skill_id": "resumir", "version": version}
                    ),
                    reply(text, Usage(10 if version == "1.0.0" else 20, 5, 0)),
                ]
            ),
            RulesRouter("balanced"),
            Budget(5, 5),
            skills=registry,
            tools=tools,
            store=SqliteConversationStore(path),
            clock=lambda: now,
        )

    return SkillEvaluator(
        db,
        engine,
        lambda _: ToolRegistry([]),
        {"fake-model": Price(1, 1)} if priced else None,
    )


@dataclass
class Fixture:
    db: Path
    proposal_id: UUID
    base: SkillPackage

    def compare(
        self,
        outcomes: tuple[bool, ...] = (True, True, True, True),
        *,
        priced: bool = False,
        context: str = "test",
    ) -> Comparison:
        return asyncio.run(
            ProposalReview(self.db).compare(
                self.proposal_id, evaluator(self.db, outcomes, priced=priced), context
            )
        )

    def approve(self, result: Comparison, *, context: str = "test") -> None:
        ProposalReview(self.db).approve(
            self.proposal_id,
            result.id,
            context,
            accept_unknown_cost=True,
            accept_resource_increase=True,
        )


@pytest.fixture
def prepared(tmp_path: Path) -> Fixture:
    db = tmp_path / "neoson.db"
    base = package()
    SqliteSkillRegistry(db).approve(base)
    source_id = uuid4()
    with closing(connect(db)) as c:
        c.execute(
            "INSERT INTO skill_evaluations VALUES"
            " (?, 'resumir','1.0.0',?, ?,2,0,0,0,0,NULL,0,'[]')",
            (str(source_id), base.checksum, datetime.now(UTC).isoformat()),
        )
    p = SqliteProposalStore(db).create(
        package("1.1.0"),
        base_version="1.0.0",
        base_checksum=base.checksum,
        request_id=uuid4(),
        sources=(ProposalSource("evaluation", source_id),),
    )
    return Fixture(db, p.id, base)


def test_isolated_comparison_explicit_approval_and_rollback(prepared: Fixture) -> None:
    result = prepared.compare()
    review = ProposalReview(prepared.db)
    assert review.latest(prepared.proposal_id) == result
    assert result.passed and result.unknown_cost and result.resource_increase
    assert (
        result.base.usage.input_tokens == 40
        and result.candidate.usage.input_tokens == 60
    )
    assert asyncio.run(review.registry.index()) == [prepared.base.skill().ref]
    prepared.approve(result)
    assert review.proposals.get(prepared.proposal_id).status == "approved"
    assert asyncio.run(review.registry.index())[0].version == "1.1.0"
    prepared.approve(result)  # idempotente, sem evento extra
    assert review.registry.rollback("resumir").ref.version == "1.0.0"
    with pytest.raises(ProposalConflict):
        prepared.approve(result)  # não reativar candidata após rollback
    with closing(connect(prepared.db)) as c:
        assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert (
            c.execute("SELECT COUNT(*) FROM skill_proposal_approvals").fetchone()[0]
            == 1
        )
        body = c.execute(
            "SELECT base_report_json,candidate_report_json"
            " FROM skill_proposal_comparisons"
        ).fetchone()
        assert PRIVATE not in str(tuple(body)) and INSTRUCTIONS not in str(tuple(body))


def test_equal_rate_still_detects_per_case_regression(prepared: Fixture) -> None:
    result = prepared.compare((True, False, False, True))
    assert result.base.passed_cases == result.candidate.passed_cases == 1
    assert result.regressions == ("aa",) and not result.passed
    with pytest.raises(ProposalConflict, match="Regressão"):
        prepared.approve(result)


def test_any_candidate_failure_blocks_approval(prepared: Fixture) -> None:
    result = prepared.compare((False, False, True, False))
    assert not result.regressions and not result.passed
    with pytest.raises(ProposalConflict):
        prepared.approve(result)


def test_requires_evidence_and_acknowledges_resources(prepared: Fixture) -> None:
    review = ProposalReview(prepared.db)
    with pytest.raises(ProposalConflict, match="avaliação"):
        review.approve(prepared.proposal_id, uuid4(), "test")
    result = prepared.compare()
    with pytest.raises(ProposalConflict, match="unknown-cost"):
        review.approve(prepared.proposal_id, result.id, "test")
    with pytest.raises(ProposalConflict, match="resource-increase"):
        review.approve(
            prepared.proposal_id, result.id, "test", accept_unknown_cost=True
        )
    with pytest.raises(SkillConflict, match="reservada"):
        review.registry.approve(package("1.1.0"))


@pytest.mark.parametrize(
    "changed", ["base", "rollback", "context", "evidence", "older"]
)
def test_stale_or_altered_evidence_is_refused(prepared: Fixture, changed: str) -> None:
    result = prepared.compare()
    context = "test"
    review = ProposalReview(prepared.db)
    if changed in ("base", "rollback"):
        review.registry.approve(package("1.2.0"))
        if changed == "rollback":
            review.registry.rollback("resumir")
    elif changed == "context":
        context = "new-config"
    elif changed == "evidence":
        with closing(connect(prepared.db)) as c:
            c.execute(
                "UPDATE skill_proposal_comparisons SET candidate_report_json='{}'"
            )
    else:
        prepared.compare()
    with pytest.raises((ProposalConflict, SkillIntegrityError)):
        prepared.approve(result, context=context)
    assert all(v.ref.version != "1.1.0" for v in review.registry.versions())
    if changed in ("base", "rollback"):
        assert review.proposals.get(prepared.proposal_id).status == "stale"


def test_cannot_weaken_fixture_even_if_checksum_recomputed(prepared: Fixture) -> None:
    altered = dict(package("1.1.0").contents)
    altered["tests/fixtures/aa.yaml"] = altered["tests/fixtures/aa.yaml"].replace(
        "ok-aa", "resposta incompleta"
    )
    snapshot = package_from_texts(Path("resumir"), altered)
    with closing(connect(prepared.db)) as c:
        c.execute(
            "UPDATE skill_proposals SET files_json=?,candidate_checksum=?",
            (json.dumps(altered), snapshot.checksum),
        )
    with pytest.raises(ProposalConflict):
        prepared.compare()
    assert (
        ProposalReview(prepared.db).proposals.get(prepared.proposal_id).status
        == "stale"
    )


def test_approval_event_failure_rolls_back_catalog_and_state(
    prepared: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = prepared.compare()
    original = outbox.insert

    def fail(c: sqlite3.Connection, event: OutboxEvent) -> None:
        if event.type == "neoson.skill.proposal.approved":
            raise RuntimeError("evento indisponível")
        original(c, event)

    monkeypatch.setattr(outbox, "insert", fail)
    with pytest.raises(RuntimeError):
        prepared.approve(result)
    review = ProposalReview(prepared.db)
    assert review.proposals.get(prepared.proposal_id).status == "evaluated"
    assert asyncio.run(review.registry.index()) == [prepared.base.skill().ref]


def test_reject_after_comparison_and_price_visibility(prepared: Fixture) -> None:
    result = prepared.compare(priced=True)
    assert not result.unknown_cost and result.base.cost_usd is not None
    review = ProposalReview(prepared.db)
    assert review.proposals.reject(prepared.proposal_id).status == "rejected"
    with pytest.raises(ProposalConflict):
        prepared.approve(result)


def test_base_changes_during_evaluation_without_holding_lock(prepared: Fixture) -> None:
    real = evaluator(prepared.db)
    updated = package("1.2.0")

    class Changing(SkillEvaluator):
        async def evaluate_package(self, package: SkillPackage) -> Evaluation:
            registry = SqliteSkillRegistry(prepared.db)
            if registry.approved_package("resumir").metadata.version == "1.0.0":
                registry.approve(updated)
            return await real.evaluate_package(package)

    runner = Changing(prepared.db, real._engine, real._catalog)
    review = ProposalReview(prepared.db)
    with pytest.raises(ProposalConflict, match="durante"):
        asyncio.run(review.compare(prepared.proposal_id, runner, "test"))
    assert review.latest(prepared.proposal_id) is None
    assert review.proposals.get(prepared.proposal_id).status == "stale"


def test_late_comparison_cannot_replace_newer_result(prepared: Fixture) -> None:
    real = evaluator(prepared.db)
    nested: list[Comparison] = []

    class Late(SkillEvaluator):
        async def evaluate_package(self, package: SkillPackage) -> Evaluation:
            if not nested:
                nested.append(
                    await ProposalReview(prepared.db).compare(
                        prepared.proposal_id,
                        evaluator(prepared.db),
                        "test",
                    )
                )
            return await real.evaluate_package(package)

    runner = Late(prepared.db, real._engine, real._catalog)
    review = ProposalReview(prepared.db)
    with pytest.raises(ProposalConflict, match="atrasado"):
        asyncio.run(review.compare(prepared.proposal_id, runner, "test"))
    assert review.latest(prepared.proposal_id) == nested[0]
    prepared.approve(nested[0])


def test_starting_comparison_invalidates_previous_approval(prepared: Fixture) -> None:
    old = prepared.compare()
    real = evaluator(prepared.db)

    class Checking(SkillEvaluator):
        async def evaluate_package(self, package: SkillPackage) -> Evaluation:
            with pytest.raises(ProposalConflict, match="avaliação"):
                prepared.approve(old)
            return await real.evaluate_package(package)

    runner = Checking(prepared.db, real._engine, real._catalog)
    result = asyncio.run(
        ProposalReview(prepared.db).compare(
            prepared.proposal_id,
            runner,
            "test",
        )
    )
    assert result.id != old.id
    prepared.approve(result)


def test_comparison_event_failure_keeps_evidence_unpublished(
    prepared: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = outbox.insert

    def fail(c: sqlite3.Connection, event: OutboxEvent) -> None:
        if event.type == "neoson.skill.proposal.evaluated":
            raise RuntimeError("evento indisponível")
        original(c, event)

    monkeypatch.setattr(outbox, "insert", fail)
    with pytest.raises(RuntimeError):
        prepared.compare()
    review = ProposalReview(prepared.db)
    assert review.latest(prepared.proposal_id) is None
    assert review.proposals.get(prepared.proposal_id).status == "pending"


def test_cli_review_does_not_need_llm_for_inspection_or_approval(
    prepared: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in list(os.environ):
        if key.upper().startswith("NEOSON_") or key.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_DB_PATH", str(prepared.db))
    monkeypatch.setattr(
        "neoson_gateway.skills_review.build_evaluator",
        lambda settings: evaluator(prepared.db),
    )
    assert main(["proposal", "list"]) == 0
    assert main(["proposal", "inspect", str(prepared.proposal_id)]) == 0
    assert PRIVATE not in capsys.readouterr().out
    assert main(["proposal", "eval", str(prepared.proposal_id)]) == 0
    result = ProposalReview(prepared.db).latest(prepared.proposal_id)
    assert result is not None
    assert result.context_hash == context_hash(load_settings(env_file=None))
    assert "desconhecido" in capsys.readouterr().out
    assert main(["proposal", "inspect", str(prepared.proposal_id), "--diff"]) == 0
    assert "Confira a fonte" in capsys.readouterr().out
    assert (
        main(
            [
                "proposal",
                "approve",
                str(prepared.proposal_id),
                "--comparison",
                str(result.id),
                "--accept-unknown-cost",
                "--accept-resource-increase",
            ]
        )
        == 0
    )
    assert main(["rollback", "resumir"]) == 0
