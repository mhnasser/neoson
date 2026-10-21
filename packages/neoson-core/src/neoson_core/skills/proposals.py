"""Snapshots candidatos e proveniência; nenhuma ativação ou gravação na biblioteca."""

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from neoson_core.engine import OutboxEvent
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction
from neoson_core.skills.loader import SkillPackage, package_from_texts
from neoson_core.skills.registry import SkillIntegrityError, _active, _package

MAX_SOURCES = 32


class ProposalInvalid(ValueError):
    """Candidato ou fontes não respeitam o contrato procedural."""


class ProposalConflict(ValueError):
    """Base mudou, versão reservada ou chave idempotente reutilizada."""


class ProposalNotFound(LookupError):
    """Não há proposta com esse identificador."""


class ProposalStatus(StrEnum):
    PENDING = "pending"
    EVALUATED = "evaluated"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ProposalSource:
    kind: Literal["run", "evaluation"]
    id: UUID

    def __post_init__(self) -> None:
        if self.kind not in ("run", "evaluation") or not isinstance(self.id, UUID):
            raise ProposalInvalid(
                "Fonte deve identificar um run ou avaliação por UUID."
            )


@dataclass(frozen=True, slots=True)
class Proposal:
    id: UUID
    request_id: UUID
    base_version: str
    base_checksum: str
    candidate: SkillPackage
    sources: tuple[ProposalSource, ...]
    status: ProposalStatus
    created_at: datetime


class SqliteProposalStore:
    """Cria/rejeita propostas; estados de avaliação/aprovação reservados à revisão."""

    def __init__(self, path: Path) -> None:
        self._path = path
        with closing(connect(path)) as connection:
            migrate(connection)

    def create(
        self,
        candidate: SkillPackage,
        *,
        base_version: str,
        base_checksum: str,
        request_id: UUID,
        sources: Sequence[ProposalSource],
        _connection: sqlite3.Connection | None = None,
    ) -> Proposal:
        snapshot = package_from_texts(candidate.path, candidate.contents)
        if (
            snapshot.checksum != candidate.checksum
            or snapshot.metadata != candidate.metadata
        ):
            raise SkillIntegrityError("Candidato mudou após a validação.")
        if not isinstance(request_id, UUID):
            raise ProposalInvalid("request_id deve ser um UUID.")
        references = tuple(sorted(set(sources), key=lambda s: (s.kind, str(s.id))))
        if not references or len(references) > MAX_SOURCES:
            raise ProposalInvalid(f"Proposta exige entre 1 e {MAX_SOURCES} fontes.")
        skill_id = snapshot.metadata.id
        if _connection is not None and not _connection.in_transaction:
            raise ValueError("A conexão externa exige uma transação ativa.")
        with (
            (
                closing(connect(self._path))
                if _connection is None
                else nullcontext(_connection)
            ) as connection,
            transaction(connection) if _connection is None else nullcontext(connection),
        ):
            existing = connection.execute(
                "SELECT * FROM skill_proposals WHERE request_id = ?", (str(request_id),)
            ).fetchone()
            if existing is not None:
                proposal = _proposal(connection, existing)
                if (
                    proposal.candidate.metadata.id != skill_id
                    or proposal.candidate.checksum != snapshot.checksum
                    or proposal.base_version != base_version
                    or proposal.base_checksum != base_checksum
                    or proposal.sources != references
                ):
                    raise ProposalConflict(
                        "Chave idempotente já usada com outro conteúdo."
                    )
                return proposal
            base = _package(_active(connection, skill_id))
            if (base.metadata.version, base.checksum) != (base_version, base_checksum):
                raise ProposalConflict("A versão ativa mudou; gere nova proposta.")
            _validate_candidate(base, snapshot)
            _validate_sources(
                connection, skill_id, base_version, base_checksum, references
            )
            if connection.execute(
                "SELECT 1 FROM skills WHERE id = ? AND version = ?"
                " UNION ALL SELECT 1 FROM skill_proposals"
                " WHERE skill_id = ? AND candidate_version = ?",
                (
                    skill_id,
                    snapshot.metadata.version,
                    skill_id,
                    snapshot.metadata.version,
                ),
            ).fetchone():
                raise ProposalConflict(
                    "Versão candidata já reservada; use nova versão."
                )
            proposal_id = uuid4()
            connection.execute(
                "INSERT INTO skill_proposals"
                " (id,request_id,skill_id,base_version,base_checksum,candidate_version,"
                " candidate_checksum,files_json,status,created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    str(proposal_id),
                    str(request_id),
                    skill_id,
                    base_version,
                    base_checksum,
                    snapshot.metadata.version,
                    snapshot.checksum,
                    json.dumps(dict(snapshot.contents), ensure_ascii=False),
                    now_iso(),
                ),
            )
            for source in references:
                table = (
                    "skill_proposal_runs"
                    if source.kind == "run"
                    else "skill_proposal_evaluations"
                )
                connection.execute(
                    f"INSERT INTO {table} VALUES (?, ?)",
                    (str(proposal_id), str(source.id)),
                )
            _event(
                connection, "created", proposal_id, skill_id, snapshot.metadata.version
            )
            return _get(connection, proposal_id)

    def get(self, proposal_id: UUID) -> Proposal:
        with closing(connect(self._path)) as connection:
            return _get(connection, proposal_id)

    def list(self) -> list[Proposal]:
        with closing(connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT * FROM skill_proposals ORDER BY created_at, id"
            ).fetchall()
            return [_proposal(connection, row) for row in rows]

    def reject(self, proposal_id: UUID) -> Proposal:
        with closing(connect(self._path)) as connection, transaction(connection):
            proposal = _get(connection, proposal_id)
            if proposal.status == ProposalStatus.REJECTED:
                return proposal
            if proposal.status not in (
                ProposalStatus.PENDING,
                ProposalStatus.EVALUATED,
            ):
                raise ProposalConflict(
                    "Somente proposta pendente ou avaliada pode ser rejeitada."
                )
            connection.execute(
                "UPDATE skill_proposals SET status = 'rejected' WHERE id = ?",
                (str(proposal_id),),
            )
            _event(
                connection,
                "rejected",
                proposal.id,
                proposal.candidate.metadata.id,
                proposal.candidate.metadata.version,
            )
            return _get(connection, proposal_id)


def _validate_candidate(base: SkillPackage, candidate: SkillPackage) -> None:
    old, new = base.metadata, candidate.metadata
    if old.model_copy(update={"version": new.version}) != new:
        raise ProposalInvalid("Só a versão pode mudar nos metadados.")
    if tuple(map(int, new.version.split("."))) <= tuple(
        map(int, old.version.split("."))
    ):
        raise ProposalInvalid("A versão candidata deve ser maior que a base.")
    if set(base.contents) != set(candidate.contents):
        raise ProposalInvalid("Candidato não pode adicionar ou remover arquivos.")
    for name in base.contents:
        if (
            name not in ("metadata.yaml", "SKILL.md")
            and base.contents[name] != candidate.contents[name]
        ):
            raise ProposalInvalid("Fixtures e exemplos precisam permanecer idênticos.")
    if base.instructions == candidate.instructions:
        raise ProposalInvalid("O procedimento candidato não mudou.")


def _validate_sources(
    connection: sqlite3.Connection,
    skill_id: str,
    version: str,
    checksum: str,
    sources: Sequence[ProposalSource],
) -> None:
    for source in sources:
        if source.kind == "run":
            row = connection.execute(
                "SELECT 1 FROM skill_executions WHERE run_id = ?"
                " AND skill_id = ? AND skill_version = ?",
                (str(source.id), skill_id, version),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT 1 FROM skill_evaluations WHERE id = ?"
                " AND skill_id = ? AND skill_version = ? AND checksum = ?",
                (str(source.id), skill_id, version, checksum),
            ).fetchone()
        if row is None:
            raise ProposalInvalid("Fonte inexistente ou não corresponde à versão base.")


def _get(connection: sqlite3.Connection, proposal_id: UUID) -> Proposal:
    row = connection.execute(
        "SELECT * FROM skill_proposals WHERE id = ?", (str(proposal_id),)
    ).fetchone()
    if row is None:
        raise ProposalNotFound("Proposta não encontrada.")
    return _proposal(connection, row)


def _proposal(connection: sqlite3.Connection, row: sqlite3.Row) -> Proposal:
    # O mesmo verificador do catálogo confere identidade, versão e checksum.
    candidate_row = connection.execute(
        "SELECT skill_id AS id, candidate_version AS version,"
        " candidate_checksum AS checksum, files_json FROM skill_proposals WHERE id = ?",
        (row["id"],),
    ).fetchone()
    snapshot = _package(candidate_row)
    sources = [
        ProposalSource("evaluation", UUID(r[0]))
        for r in connection.execute(
            "SELECT evaluation_id FROM skill_proposal_evaluations"
            " WHERE proposal_id = ? ORDER BY evaluation_id",
            (row["id"],),
        )
    ] + [
        ProposalSource("run", UUID(r[0]))
        for r in connection.execute(
            "SELECT run_id FROM skill_proposal_runs"
            " WHERE proposal_id = ? ORDER BY run_id",
            (row["id"],),
        )
    ]
    return Proposal(
        UUID(row["id"]),
        UUID(row["request_id"]),
        row["base_version"],
        row["base_checksum"],
        snapshot,
        tuple(sources),
        ProposalStatus(row["status"]),
        datetime.fromisoformat(row["created_at"]),
    )


def _event(
    connection: sqlite3.Connection,
    action: str,
    proposal_id: UUID,
    skill_id: str,
    version: str,
) -> None:
    outbox.insert(
        connection,
        OutboxEvent(
            type=f"neoson.skill.proposal.{action}",
            payload={
                "proposal_id": str(proposal_id),
                "skill_id": skill_id,
                "version": version,
            },
        ),
    )
