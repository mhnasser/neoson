"""Aprovação/rollback transacionais e vínculos imutáveis de sessão."""

import json
import logging
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction
from neoson_core.routing.dataset import RoutingDataset
from neoson_core.routing.lab_plan import BenchmarkPlan, digest
from neoson_core.routing.policy_config import BUILTIN, ModelBinding, PolicySnapshot
from neoson_core.routing.policy_gate import PolicyRejected, validate_evidence
from neoson_core.telemetry.pricing import Price

logger = logging.getLogger(__name__)


def snapshot_checksum(snapshot: PolicySnapshot) -> str:
    return digest(snapshot.model_dump(mode="json"))


class PolicyRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(connect(path)) as c:
            migrate(c)
            with transaction(c):
                if c.execute("SELECT 1 FROM routing_policy_state").fetchone() is None:
                    self._insert(c, BUILTIN, 0)
                    event = str(uuid4())
                    c.execute(
                        "INSERT INTO routing_policy_events"
                        " (id,action,version,previous_revision,created_at)"
                        " VALUES (?,'bootstrap',?,'',?)",
                        (event, BUILTIN.version, now_iso()),
                    )
                    c.execute(
                        "INSERT INTO routing_policy_state VALUES (1,?,?)",
                        (BUILTIN.version, event),
                    )

    @staticmethod
    def _insert(c: sqlite3.Connection, snapshot: PolicySnapshot, order: int) -> None:
        c.execute(
            "INSERT INTO routing_policies VALUES (?,?,?,?)",
            (
                snapshot.version,
                snapshot_checksum(snapshot),
                snapshot.model_dump_json(),
                order,
            ),
        )

    @staticmethod
    def _revision(c: sqlite3.Connection) -> str:
        return digest(
            [
                tuple(r)
                for r in c.execute(
                    "SELECT id,action,version,previous_revision,evidence_id,"
                    "evidence_checksum,runtime_hash,review_json,created_at"
                    " FROM routing_policy_events ORDER BY seq"
                )
            ]
        )

    @staticmethod
    def _snapshot(c: sqlite3.Connection, version: str) -> PolicySnapshot:
        row = c.execute(
            "SELECT * FROM routing_policies WHERE version=?", (version,)
        ).fetchone()
        try:
            if row is None:
                raise ValueError
            result = PolicySnapshot.model_validate_json(row["snapshot_json"])
            if (
                result.version != version
                or snapshot_checksum(result) != row["checksum"]
            ):
                raise ValueError
            if version == BUILTIN.version and result != BUILTIN:
                raise ValueError
            if version != BUILTIN.version:
                audit = c.execute(
                    "SELECT review_json FROM routing_policy_events"
                    " WHERE version=? AND action='promote'",
                    (version,),
                ).fetchone()
                if (
                    audit is None
                    or json.loads(audit[0]).get("snapshot_checksum") != row["checksum"]
                ):
                    raise ValueError
            return result
        except ValueError:
            raise PolicyRejected("Snapshot de política inválido.") from None

    def status(self) -> dict[str, str]:
        with closing(connect(self.path)) as c:
            row = c.execute(
                "SELECT version,event_id FROM routing_policy_state WHERE singleton=1"
            ).fetchone()
            snapshot = self._snapshot(c, row["version"])
            return {
                "version": snapshot.version,
                "checksum": snapshot_checksum(snapshot),
                "revision": self._revision(c),
                "event_id": row["event_id"],
            }

    def promote(
        self,
        request_id: UUID,
        evaluation_id: UUID,
        evidence_path: Path,
        dataset: RoutingDataset,
        plan: BenchmarkPlan,
        runtime_hash: str,
        binding: ModelBinding,
        prices: Mapping[str, Price],
    ) -> str:
        promotion = plan.promotion
        if promotion is None:
            raise PolicyRejected("Plano sem revisão de promoção.")
        snapshot = promotion.snapshot
        with closing(connect(self.path)) as c, transaction(c):
            state = c.execute(
                "SELECT * FROM routing_policy_state WHERE singleton=1"
            ).fetchone()
            existing = c.execute(
                "SELECT * FROM routing_policy_events WHERE id=?", (str(request_id),)
            ).fetchone()
            if existing:
                review = json.loads(existing["review_json"])
                if (
                    existing["action"] == "promote"
                    and existing["evidence_id"] == str(evaluation_id)
                    and existing["version"] == snapshot.version
                    and review.get("plan_checksum") == plan.checksum
                    and existing["runtime_hash"] == runtime_hash
                    and state["event_id"] == str(request_id)
                ):
                    if self._snapshot(c, snapshot.version) != snapshot:
                        raise PolicyRejected("Snapshot histórico mudou.")
                    return snapshot.version
                raise PolicyRejected(
                    "UUID de revisão já utilizado; não reativa política histórica."
                )
            if promotion.base_revision != self._revision(c):
                raise PolicyRejected("Revisão do catálogo mudou; refaça a avaliação.")
            self._snapshot(c, state["version"])
            if c.execute(
                "SELECT 1 FROM routing_policies WHERE version=?", (snapshot.version,)
            ).fetchone():
                raise PolicyRejected("Versão já aprovada; use uma nova versão.")
            proof = validate_evidence(
                evidence_path,
                evaluation_id,
                dataset,
                plan,
                runtime_hash,
                binding,
                prices,
            )
            order = c.execute(
                "SELECT MAX(approval_order)+1 FROM routing_policies"
            ).fetchone()[0]
            self._insert(c, snapshot, order)
            review = {
                "dataset_id": dataset.id,
                "dataset_version": dataset.version,
                "dataset_checksum": dataset.checksum,
                "plan_checksum": plan.checksum,
                "snapshot_checksum": snapshot_checksum(snapshot),
                "limits": promotion.limits.model_dump(mode="json"),
            }
            c.execute(
                "INSERT INTO routing_policy_events"
                " (id,action,version,previous_revision,evidence_id,evidence_checksum,"
                "runtime_hash,review_json,created_at)"
                " VALUES (?,'promote',?,?,?,?,?,?,?)",
                (
                    str(request_id),
                    snapshot.version,
                    promotion.base_revision,
                    str(evaluation_id),
                    proof,
                    runtime_hash,
                    json.dumps(review),
                    now_iso(),
                ),
            )
            c.execute(
                "UPDATE routing_policy_state SET version=?,event_id=?"
                " WHERE singleton=1",
                (snapshot.version, str(request_id)),
            )
        return snapshot.version

    def rollback(self, request_id: UUID, expected_revision: str) -> str:
        with closing(connect(self.path)) as c, transaction(c):
            state = c.execute(
                "SELECT * FROM routing_policy_state WHERE singleton=1"
            ).fetchone()
            existing = c.execute(
                "SELECT * FROM routing_policy_events WHERE id=?", (str(request_id),)
            ).fetchone()
            if existing:
                if (
                    existing["action"] == "rollback"
                    and existing["previous_revision"] == expected_revision
                    and state["event_id"] == str(request_id)
                ):
                    self._snapshot(c, state["version"])
                    return state["version"]  # type: ignore[no-any-return]
                raise PolicyRejected("UUID de rollback já utilizado.")
            if self._revision(c) != expected_revision:
                raise PolicyRejected("Revisão mudou antes do rollback.")
            row = c.execute(
                "SELECT version FROM routing_policies WHERE approval_order <"
                " (SELECT approval_order FROM routing_policies WHERE version=?)"
                " ORDER BY approval_order DESC LIMIT 1",
                (state["version"],),
            ).fetchone()
            if row is None:
                raise PolicyRejected("Não há versão anterior para rollback.")
            previous = self._snapshot(c, row[0])
            c.execute(
                "INSERT INTO routing_policy_events"
                " (id,action,version,previous_revision,created_at)"
                " VALUES (?,'rollback',?,?,?)",
                (str(request_id), previous.version, expected_revision, now_iso()),
            )
            c.execute(
                "UPDATE routing_policy_state SET version=?,event_id=?"
                " WHERE singleton=1",
                (previous.version, str(request_id)),
            )
            return previous.version

    def bind(
        self, session_id: UUID, current: ModelBinding, runtime_hash: str
    ) -> tuple[PolicySnapshot, str, str | None]:
        fallback = None
        with closing(connect(self.path)) as c, transaction(c):
            row = c.execute(
                "SELECT * FROM routing_session_bindings WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
            if row:
                binding = self._binding(row)
                version = row["version"]
                alias = row["alias"]
            else:
                version = c.execute(
                    "SELECT version FROM routing_policy_state WHERE singleton=1"
                ).fetchone()[0]
                binding = current
                legacy = c.execute(
                    "SELECT model FROM runs WHERE session_id=? AND model IS NOT NULL"
                    " ORDER BY rowid DESC LIMIT 1",
                    (str(session_id),),
                ).fetchone()
                if legacy:
                    binding = ModelBinding(
                        profile=current.profile,
                        model=legacy[0],
                        reasoning_effort=current.reasoning_effort,
                    )
                    version, fallback = BUILTIN.version, "fallback:legacy_session"
                    logger.warning(
                        "routing fallback: legacy_session; modelo anterior preservado"
                    )
                if version != BUILTIN.version:
                    approved = c.execute(
                        "SELECT runtime_hash FROM routing_policy_events"
                        " WHERE version=? AND action='promote'",
                        (version,),
                    ).fetchone()
                    if approved is None or approved[0] != runtime_hash:
                        version, fallback = BUILTIN.version, "fallback:runtime_changed"
                        logger.warning("routing fallback: runtime_changed")
                alias = "bound_" + digest(binding.model_dump(mode="json"))
            try:
                snapshot = self._snapshot(c, version)
            except PolicyRejected:
                # Mantém o modelo conhecido; fallback não escolhe outro modelo.
                snapshot, fallback = BUILTIN, "fallback:policy_invalid"
                logger.warning("routing fallback: policy_invalid; modelo preservado")
            if row is None:
                c.execute(
                    "INSERT INTO routing_session_bindings VALUES (?,?,?,?,?)",
                    (
                        str(session_id),
                        snapshot.version,
                        alias,
                        binding.model_dump_json(),
                        digest(binding.model_dump(mode="json")),
                    ),
                )
        return snapshot, alias, fallback

    @staticmethod
    def _binding(row: sqlite3.Row) -> ModelBinding:
        try:
            binding = ModelBinding.model_validate_json(row["model_json"])
            checksum = digest(binding.model_dump(mode="json"))
            if row["checksum"] != checksum or row["alias"] != "bound_" + checksum:
                raise ValueError
            return binding
        except ValueError:
            raise PolicyRejected(
                "Vínculo de modelo inválido; execução bloqueada."
            ) from None

    def resolve(self, alias: str) -> ModelBinding:
        with closing(connect(self.path)) as c:
            row = c.execute(
                "SELECT * FROM routing_session_bindings WHERE alias=? LIMIT 1", (alias,)
            ).fetchone()
            if row is None:
                raise PolicyRejected("Perfil de sessão desconhecido.")
            return self._binding(row)
