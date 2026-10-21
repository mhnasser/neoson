"""Reserva idempotente antes da rede; relatórios somente com métricas públicas."""

import json
from contextlib import closing
from pathlib import Path
from uuid import UUID

from neoson_core.engine import OutboxEvent
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, migrate, now_iso, transaction
from neoson_core.routing.dataset import RoutingDataset
from neoson_core.routing.lab_plan import BenchmarkPlan, digest


class BenchmarkConflict(ValueError):
    pass


class EvaluationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(connect(path)) as c:
            migrate(c)

    def claim(
        self, request_id: UUID, dataset: RoutingDataset, plan: BenchmarkPlan
    ) -> dict[str, object] | None:
        context = plan.public_context()
        checksum = digest((dataset.checksum, plan.checksum, context))
        with closing(connect(self.path)) as c, transaction(c):
            row = c.execute(
                "SELECT checksum FROM eval_datasets WHERE id=? AND version=?",
                (dataset.id, dataset.version),
            ).fetchone()
            if row and row[0] != dataset.checksum:
                raise BenchmarkConflict("Mesma versão de dataset com outro checksum.")
            existing = c.execute(
                "SELECT * FROM eval_runs WHERE id=?", (str(request_id),)
            ).fetchone()
            if existing:
                if existing["context_hash"] != checksum:
                    raise BenchmarkConflict("UUID reutilizado com outro contexto.")
                if existing["status"] != "completed":
                    raise BenchmarkConflict(
                        "Benchmark já reservado; use novo UUID para nova tentativa."
                    )
                if digest(existing["metrics_json"]) != existing["evidence_checksum"]:
                    raise BenchmarkConflict("Relatório alterado ou inválido.")
                result: dict[str, object] = json.loads(existing["metrics_json"])
                return result
            c.execute(
                "INSERT OR IGNORE INTO eval_datasets VALUES (?,?,?)",
                (dataset.id, dataset.version, dataset.checksum),
            )
            c.execute(
                "INSERT INTO eval_runs (id,dataset_id,dataset_version,dataset_checksum,"
                "split,policy_version,context_hash,context_json,created_at,status)"
                " VALUES (?,?,?,?,?,?,?,?,?,'running')",
                (
                    str(request_id),
                    dataset.id,
                    dataset.version,
                    dataset.checksum,
                    plan.split,
                    plan.candidate_version,
                    checksum,
                    json.dumps(context, ensure_ascii=False),
                    now_iso(),
                ),
            )
            outbox.insert(
                c,
                OutboxEvent(
                    type="neoson.router.benchmark.started",
                    payload={"evaluation_id": str(request_id)},
                ),
            )
        return None

    def finish(self, request_id: UUID, report: dict[str, object]) -> None:
        text = json.dumps(report, ensure_ascii=False, allow_nan=False)
        with closing(connect(self.path)) as c, transaction(c):
            changed = c.execute(
                "UPDATE eval_runs SET status='completed',metrics_json=?,"
                "evidence_checksum=? WHERE id=? AND status='running'",
                (text, digest(text), str(request_id)),
            ).rowcount
            if changed != 1:
                raise BenchmarkConflict("Reserva do benchmark indisponível.")
            outbox.insert(
                c,
                OutboxEvent(
                    type="neoson.router.benchmark.completed",
                    payload={"evaluation_id": str(request_id)},
                ),
            )

    def fail(self, request_id: UUID) -> None:
        with closing(connect(self.path)) as c, transaction(c):
            changed = c.execute(
                "UPDATE eval_runs SET status='failed' WHERE id=? AND status='running'",
                (str(request_id),),
            ).rowcount
            if changed:
                outbox.insert(
                    c,
                    OutboxEvent(
                        type="neoson.router.benchmark.failed",
                        payload={"evaluation_id": str(request_id)},
                    ),
                )
