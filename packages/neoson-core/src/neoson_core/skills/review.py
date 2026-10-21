"""Comparação isolada e revisão explícita; nunca publica código ou arquivos."""

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from neoson_core.engine.skills import SkillNotFound
from neoson_core.infrastructure.db import connect, now_iso, transaction
from neoson_core.skills.evaluation import Evaluation, SkillEvaluator
from neoson_core.skills.fixtures import load_cases
from neoson_core.skills.loader import SkillPackage
from neoson_core.skills.proposals import (
    Proposal,
    ProposalConflict,
    ProposalInvalid,
    ProposalStatus,
    SqliteProposalStore,
    _event,
    _get,
    _validate_candidate,
    _validate_sources,
)
from neoson_core.skills.registry import (
    SkillIntegrityError,
    SqliteSkillRegistry,
    _active,
    _package,
)

_REPORT = TypeAdapter(Evaluation)


@dataclass(frozen=True, slots=True)
class Comparison:
    id: UUID
    proposal_id: UUID
    base_revision: str
    context_hash: str
    base: Evaluation
    candidate: Evaluation

    @property
    def regressions(self) -> tuple[str, ...]:
        return tuple(
            b.id
            for b, c in zip(self.base.cases, self.candidate.cases, strict=True)
            if b.passed and not c.passed
        )

    @property
    def unknown_cost(self) -> bool:
        return self.base.cost_usd is None or self.candidate.cost_usd is None

    @property
    def resource_increase(self) -> bool:
        b, c = self.base, self.candidate
        return (
            c.usage.input_tokens > b.usage.input_tokens
            or c.usage.output_tokens > b.usage.output_tokens
            or c.duration_seconds > b.duration_seconds
            or (
                b.cost_usd is not None
                and c.cost_usd is not None
                and c.cost_usd > b.cost_usd
            )
        )

    @property
    def passed(self) -> bool:
        return (
            bool(self.candidate.cases)
            and not self.regressions
            and (self.candidate.passed_cases == len(self.candidate.cases))
        )


class ProposalReview:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.proposals = SqliteProposalStore(path)
        self.registry = SqliteSkillRegistry(path)

    async def compare(
        self,
        proposal_id: UUID,
        evaluator: SkillEvaluator,
        context_hash: str,
    ) -> Comparison:
        comparison_id = uuid4()
        with closing(connect(self.path)) as c, transaction(c):
            proposal = _get(c, proposal_id)
            _eligible(proposal)
            base = _current(c, proposal)
            revision = _revision(c, proposal.candidate.metadata.id)
            if base is not None:
                c.execute(
                    "UPDATE skill_proposals SET status='pending',review_token=?"
                    " WHERE id=?",
                    (str(comparison_id), str(proposal_id)),
                )
                _event(
                    c,
                    "review_started",
                    proposal.id,
                    base.metadata.id,
                    proposal.candidate.metadata.version,
                )
        if base is None:
            raise ProposalConflict("Base mudou; proposta obsoleta.")
        # Nenhuma conexão ou transação permanece aberta durante estas chamadas.
        baseline = await evaluator.evaluate_package(base)
        candidate = await evaluator.evaluate_package(proposal.candidate)
        result = Comparison(
            comparison_id, proposal.id, revision, context_hash, baseline, candidate
        )
        _validate_reports(result, base, proposal.candidate)
        with closing(connect(self.path)) as c, transaction(c):
            current = _get(c, proposal.id)
            _eligible(current)
            token = c.execute(
                "SELECT review_token FROM skill_proposals WHERE id=?",
                (str(proposal.id),),
            ).fetchone()[0]
            if token != str(comparison_id):
                raise ProposalConflict(
                    "Outra avaliação começou; resultado atrasado recusado."
                )
            active = _current(c, current, revision)
            if active is not None:
                if (
                    current.candidate.checksum != proposal.candidate.checksum
                    or current.sources != proposal.sources
                ):
                    raise ProposalConflict("A proposta mudou durante a avaliação.")
                values = (
                    str(result.id),
                    str(result.proposal_id),
                    now_iso(),
                    result.base_revision,
                    result.context_hash,
                    _REPORT.dump_json(result.base).decode(),
                    _REPORT.dump_json(result.candidate).decode(),
                )
                c.execute(
                    "INSERT INTO skill_proposal_comparisons VALUES (?,?,?,?,?,?,?,?)",
                    (*values, _digest(values)),
                )
                c.execute(
                    "UPDATE skill_proposals SET status='evaluated' WHERE id=?",
                    (str(proposal.id),),
                )
                _event(
                    c,
                    "evaluated",
                    proposal.id,
                    base.metadata.id,
                    proposal.candidate.metadata.version,
                )
        if active is None:
            raise ProposalConflict("Base mudou durante a avaliação; proposta obsoleta.")
        return result

    def latest(self, proposal_id: UUID) -> Comparison | None:
        with closing(connect(self.path)) as c:
            proposal = _get(c, proposal_id)
            row = c.execute(
                "SELECT * FROM skill_proposal_comparisons WHERE proposal_id=?"
                " ORDER BY rowid DESC LIMIT 1",
                (str(proposal_id),),
            ).fetchone()
            return None if row is None else _read(c, row, proposal)

    def base(self, proposal_id: UUID) -> SkillPackage:
        with closing(connect(self.path)) as c:
            proposal = _get(c, proposal_id)
            return _package(
                c.execute(
                    "SELECT * FROM skills WHERE id=? AND version=?",
                    (proposal.candidate.metadata.id, proposal.base_version),
                ).fetchone()
            )

    def approve(
        self,
        proposal_id: UUID,
        comparison_id: UUID,
        context_hash: str,
        *,
        accept_unknown_cost: bool = False,
        accept_resource_increase: bool = False,
    ) -> Proposal:
        with closing(connect(self.path)) as c, transaction(c):
            proposal = _get(c, proposal_id)
            if proposal.status == ProposalStatus.APPROVED:
                approval = c.execute(
                    "SELECT comparison_id FROM skill_proposal_approvals"
                    " WHERE proposal_id=?",
                    (str(proposal.id),),
                ).fetchone()
                active = _package(_active(c, proposal.candidate.metadata.id))
                if (
                    approval
                    and approval[0] == str(comparison_id)
                    and active.checksum == proposal.candidate.checksum
                ):
                    return proposal
                raise ProposalConflict(
                    "Proposta já revisada; não reativa versão após rollback."
                )
            if proposal.status != ProposalStatus.EVALUATED:
                raise ProposalConflict(
                    "Proposta precisa de avaliação válida antes da aprovação."
                )
            row = c.execute(
                "SELECT * FROM skill_proposal_comparisons WHERE proposal_id=?"
                " ORDER BY rowid DESC LIMIT 1",
                (str(proposal.id),),
            ).fetchone()
            if row is None or row["id"] != str(comparison_id):
                raise ProposalConflict(
                    "Selecione a comparação mais recente da proposta."
                )
            token = c.execute(
                "SELECT review_token FROM skill_proposals WHERE id=?",
                (str(proposal.id),),
            ).fetchone()[0]
            if token != str(comparison_id):
                raise ProposalConflict("Avaliação substituída ou ainda em andamento.")
            comparison = _read(c, row, proposal)
            if comparison.context_hash != context_hash:
                raise ProposalConflict(
                    "Configuração mudou; avalie novamente antes de aprovar."
                )
            base = _current(c, proposal, comparison.base_revision)
            if base is not None:
                if not comparison.passed:
                    raise ProposalConflict(
                        "Regressão ou caso reprovado impede aprovação."
                    )
                if comparison.unknown_cost and not accept_unknown_cost:
                    raise ProposalConflict(
                        "Custo desconhecido exige --accept-unknown-cost."
                    )
                if comparison.resource_increase and not accept_resource_increase:
                    raise ProposalConflict(
                        "Aumento de recursos exige --accept-resource-increase."
                    )
                self.registry.approve(
                    proposal.candidate, _connection=c, _proposal_id=proposal.id
                )
                c.execute(
                    "INSERT INTO skill_proposal_approvals VALUES (?,?,?,?,?)",
                    (
                        str(proposal.id),
                        str(comparison.id),
                        now_iso(),
                        int(accept_unknown_cost),
                        int(accept_resource_increase),
                    ),
                )
                c.execute(
                    "UPDATE skill_proposals SET status='approved' WHERE id=?",
                    (str(proposal.id),),
                )
                _event(
                    c,
                    "approved",
                    proposal.id,
                    proposal.candidate.metadata.id,
                    proposal.candidate.metadata.version,
                )
                proposal = _get(c, proposal.id)
        if base is None:
            raise ProposalConflict("Base mudou; proposta obsoleta.")
        return proposal


def _eligible(proposal: Proposal) -> None:
    if proposal.status not in (ProposalStatus.PENDING, ProposalStatus.EVALUATED):
        raise ProposalConflict("Proposta não está disponível para avaliação.")


def _revision(c: sqlite3.Connection, skill_id: str) -> str:
    events = c.execute(
        "SELECT id FROM outbox_events WHERE type IN"
        " ('neoson.skill.approved','neoson.skill.rolled_back','neoson.skill.retired')"
        " AND json_extract(payload,'$.skill_id')=? ORDER BY id",
        (skill_id,),
    ).fetchall()
    return hashlib.sha256("\n".join(row[0] for row in events).encode()).hexdigest()


def _current(
    c: sqlite3.Connection, proposal: Proposal, revision: str | None = None
) -> SkillPackage | None:
    try:
        base = _package(_active(c, proposal.candidate.metadata.id))
        if (base.metadata.version, base.checksum) != (
            proposal.base_version,
            proposal.base_checksum,
        ):
            raise ProposalConflict("Base mudou.")
        if revision is not None and _revision(c, base.metadata.id) != revision:
            raise ProposalConflict("Catálogo mudou desde a avaliação.")
        _validate_candidate(base, proposal.candidate)
        _validate_sources(
            c, base.metadata.id, base.metadata.version, base.checksum, proposal.sources
        )
        if not proposal.sources:
            raise ProposalInvalid("Proposta sem fontes.")
        return base
    except (SkillNotFound, ProposalConflict, ProposalInvalid):
        c.execute(
            "UPDATE skill_proposals SET status='stale' WHERE id=?", (str(proposal.id),)
        )
        _event(
            c,
            "stale",
            proposal.id,
            proposal.candidate.metadata.id,
            proposal.candidate.metadata.version,
        )
        return None


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _read(c: sqlite3.Connection, row: sqlite3.Row, proposal: Proposal) -> Comparison:
    values = tuple(
        row[key]
        for key in (
            "id",
            "proposal_id",
            "created_at",
            "base_revision",
            "context_hash",
            "base_report_json",
            "candidate_report_json",
        )
    )
    if _digest(values) != row["evidence_checksum"]:
        raise SkillIntegrityError("Evidência de comparação foi alterada.")
    try:
        result = Comparison(
            UUID(row["id"]),
            UUID(row["proposal_id"]),
            row["base_revision"],
            row["context_hash"],
            _REPORT.validate_json(row["base_report_json"]),
            _REPORT.validate_json(row["candidate_report_json"]),
        )
    except (ValueError, ValidationError):
        raise SkillIntegrityError("Evidência de comparação inválida.") from None
    base_row = c.execute(
        "SELECT * FROM skills WHERE id=? AND version=?",
        (proposal.candidate.metadata.id, proposal.base_version),
    ).fetchone()
    base = _package(base_row)
    _validate_candidate(base, proposal.candidate)
    _validate_reports(result, base, proposal.candidate)
    return result


def _validate_reports(
    result: Comparison, base: SkillPackage, candidate: SkillPackage
) -> None:
    expected = tuple(case.id for case in load_cases(base))
    for report, package in ((result.base, base), (result.candidate, candidate)):
        if (
            (report.skill_id, report.skill_version, report.checksum)
            != (
                package.metadata.id,
                package.metadata.version,
                package.checksum,
            )
            or tuple(case.id for case in report.cases) != expected
            or not expected
        ):
            raise SkillIntegrityError(
                "Avaliação não corresponde aos casos e snapshots revisados."
            )
        for case in report.cases:
            numbers = (
                case.usage.input_tokens,
                case.usage.output_tokens,
                case.usage.cached_tokens,
                case.duration_seconds,
            )
            if any(not math.isfinite(n) or n < 0 for n in numbers) or case.passed != (
                not case.reasons
            ):
                raise SkillIntegrityError("Métricas de avaliação inválidas.")
            if case.cost_usd is not None and (
                not math.isfinite(case.cost_usd) or case.cost_usd < 0
            ):
                raise SkillIntegrityError("Custo de avaliação inválido.")
        if not math.isfinite(report.duration_seconds) or report.duration_seconds < 0:
            raise SkillIntegrityError("Duração de avaliação inválida.")
