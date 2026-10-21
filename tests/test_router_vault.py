import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
import yaml

from neoson_core.infrastructure.config import ServerSettings
from neoson_core.infrastructure.db import backup, connect
from neoson_core.routing.benchmark import RouterBenchmark
from neoson_core.routing.dataset import load_dataset
from neoson_core.routing.lab_plan import digest, load_plan
from neoson_core.routing.lab_store import EvaluationStore
from neoson_core.routing.policy_registry import PolicyRegistry
from neoson_core.telemetry.router_metrics import (
    EVENT_TYPES,
    load_metrics,
    read_policy,
    read_runs,
)
from neoson_core.telemetry.router_vault import HANDLER_NAME, PANEL, RouterVaultExporter
from neoson_core.telemetry.vault import write_atomic
from neoson_gateway.router_lab import build_engine, build_provider
from neoson_worker.main import build_handlers, main
from neoson_worker.worker import Worker

PRIVATE = "sentinela-privada-fora-do-vault"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = load_dataset(ROOT / "datasets/router/seed-v1.json")
    plan = load_plan(ROOT / "datasets/router/plan-validation-scripted.json")
    path = tmp_path_factory.mktemp("router-source") / "lab.db"
    asyncio.run(
        RouterBenchmark(EvaluationStore(path), build_engine, build_provider(plan)).run(
            data, plan, uuid4()
        )
    )
    return path


@pytest.fixture
def settings(source: Path, tmp_path: Path) -> ServerSettings:
    lab = tmp_path / "lab.db"
    backup(source, lab)
    policy = tmp_path / "conversations.db"
    PolicyRegistry(policy)
    return ServerSettings(
        _env_file=None,
        db_path=policy,
        router_lab_db_path=lab,
        vault_path=tmp_path / "vault",
    )


def exporter(settings: ServerSettings) -> RouterVaultExporter:
    return RouterVaultExporter(
        settings.vault_path, settings.db_path, settings.router_lab_db_path
    )


def alter(settings: ServerSettings, change: Any, rehash: bool = True) -> None:
    with closing(connect(settings.router_lab_db_path)) as c:
        raw = c.execute("SELECT metrics_json FROM eval_runs").fetchone()[0]
        data = json.loads(raw)
        change(data)
        text = json.dumps(data)
        c.execute("UPDATE eval_runs SET metrics_json=?", (text,))
        if rehash:
            c.execute("UPDATE eval_runs SET evidence_checksum=?", (digest(text),))


def test_valid_yaml_metrics_and_no_rewrite(settings: ServerSettings) -> None:
    export = exporter(settings)
    assert export.export()
    path = settings.vault_path / PANEL
    content = path.read_text(encoding="utf-8")
    meta = yaml.safe_load(content.split("---", 2)[1])
    assert meta["politica_catalogo"] == "rules-v1"
    assert meta["avaliacoes_total"] == meta["avaliacoes_exibidas"] == 1
    assert "Matriz de confusão" in content and "Tokens em cache" in content
    assert "scripted" in content and "single-model-v1" in content
    before = path.stat().st_mtime_ns
    assert not export.export() and path.stat().st_mtime_ns == before
    with closing(connect(settings.db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 0


def test_projection_ignores_private_extra_fields(settings: ServerSettings) -> None:
    def change(data: dict[str, Any]) -> None:
        data["context"]["private"] = PRIVATE
        data["candidate"]["cases"][0]["text"] = PRIVATE
        data["candidate"]["cases"][0]["reasons"] = [PRIVATE]
        data["candidate"]["metrics"]["private"] = PRIVATE

    alter(settings, change)
    assert exporter(settings).export()
    assert PRIVATE not in (settings.vault_path / PANEL).read_text(encoding="utf-8")


def test_read_queries_never_access_conversations_or_snapshots(
    settings: ServerSettings,
) -> None:
    def authorizer(
        action: int,
        table: str | None,
        column: str | None,
        db: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and (
            table == "messages"
            or column
            in (
                "context_json",
                "snapshot_json",
                "model_json",
                "review_json",
            )
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    for path, reader in (
        (settings.db_path, read_policy),
        (settings.router_lab_db_path, read_runs),
    ):
        with closing(connect(path)) as c:
            c.set_authorizer(authorizer)
            assert reader(c)


@pytest.mark.parametrize("kind", ["hash", "numeric", "matrix"])
def test_invalid_evidence_omits_metrics_without_private_output(
    settings: ServerSettings, kind: str
) -> None:
    def change(data: dict[str, Any]) -> None:
        metrics = data["candidate"]["metrics"]
        if kind == "matrix":
            metrics["confusion_matrix"] = {PRIVATE: {"conversation": 8}}
        else:
            metrics["cost_usd"] = PRIVATE

    alter(settings, change, rehash=kind != "hash")
    metrics = load_metrics(settings.db_path, settings.router_lab_db_path)
    assert metrics.lab_state == "ready" and metrics.runs[0].integrity == "invalid"
    assert metrics.runs[0].candidate is None
    exporter(settings).export()
    assert PRIVATE not in (settings.vault_path / PANEL).read_text(encoding="utf-8")


def test_scripted_never_claims_savings_even_if_flag_is_set(
    settings: ServerSettings,
) -> None:
    def change(data: dict[str, Any]) -> None:
        data["comparable_for_savings"] = True
        data["estimated_savings_pct"] = 99
        for arm in ("baseline", "candidate"):
            data[arm]["metrics"].update(
                passed=8, quality_rate=1.0, cost_usd=1.0, safety_failures=0
            )

    alter(settings, change)
    run = load_metrics(settings.db_path, settings.router_lab_db_path).runs[0]
    assert run.integrity == "valid" and run.savings is None


@pytest.mark.parametrize("state", ["running", "failed"])
def test_unfinished_runs_are_not_metrics(settings: ServerSettings, state: str) -> None:
    with closing(connect(settings.router_lab_db_path)) as c:
        c.execute("UPDATE eval_runs SET status=?", (state,))
    run = load_metrics(settings.db_path, settings.router_lab_db_path).runs[0]
    assert run.state == state and run.integrity == "none" and run.baseline is None


def test_missing_and_unsupported_sources_never_create_or_migrate(
    tmp_path: Path,
) -> None:
    policy, lab = tmp_path / "policy.db", tmp_path / "lab.db"
    assert load_metrics(policy, lab).lab_state == "missing"
    assert not policy.exists() and not lab.exists()
    with sqlite3.connect(lab) as c:
        c.execute("CREATE TABLE old_version (id INTEGER)")
    before = lab.read_bytes()
    assert load_metrics(policy, lab).lab_state == "unsupported"
    assert lab.read_bytes() == before
    lab.write_bytes(PRIVATE.encode())
    assert load_metrics(policy, lab).lab_state == "unavailable"
    assert lab.read_bytes() == PRIVATE.encode()


def test_atomic_failure_preserves_old_panel(
    settings: ServerSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = exporter(settings)
    export.export()
    path = settings.vault_path / PANEL
    before = path.read_bytes()
    with closing(connect(settings.router_lab_db_path)) as c:
        c.execute("UPDATE eval_runs SET status='failed'")

    def fail(*args: Any) -> None:
        raise OSError("rename indisponível")

    with monkeypatch.context() as patch:
        patch.setattr("neoson_core.telemetry.vault.os.replace", fail)
        with pytest.raises(OSError):
            export.export()
    assert path.read_bytes() == before
    assert export.export()
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("enabled", [True, False])
def test_routes_only_export_and_worker_consumes_lab(
    settings: ServerSettings, enabled: bool
) -> None:
    settings = settings.model_copy(update={"metrics_export_enabled": enabled})
    routes = build_handlers(settings)
    for event in EVENT_TYPES:
        assert [h.name for h in routes[event]] == ([HANDLER_NAME] if enabled else [])
    worker = Worker(settings.router_lab_db_path, routes, "lab-vault-test")
    assert worker.run_once() == 2
    assert worker.run_once() == 0
    assert (settings.vault_path / PANEL).exists() == enabled
    with closing(connect(settings.router_lab_db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0] == 0


def test_cli_missing_lab_does_not_create_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import neoson_worker.main as cli

    settings = ServerSettings(
        _env_file=None, router_lab_db_path=tmp_path / "missing.db"
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert main(["--router-lab", "--once"]) == 0
    assert not settings.router_lab_db_path.exists()


def test_concurrent_exports_use_independent_atomic_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    replace = os.replace
    barrier = Barrier(2)
    sources: list[Path] = []

    def synchronized_replace(source: Path, target: Path) -> None:
        sources.append(source)
        barrier.wait(timeout=10)
        replace(source, target)

    path = tmp_path / "panel.md"
    contents = ["a" * 10000, "b" * 10000]

    def export(text: str) -> bool:
        try:
            return write_atomic(path, text)
        except PermissionError:
            # Windows can reject simultaneous replacements; the worker retries.
            return False

    with monkeypatch.context() as patch:
        patch.setattr("neoson_core.telemetry.vault.os.replace", synchronized_replace)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(export, contents))
            assert any(results)
    assert len(set(sources)) == 2
    assert path.read_text(encoding="utf-8") in contents
    assert not list(tmp_path.glob("*.tmp"))
    write_atomic(path, contents[-1])
    assert path.read_text(encoding="utf-8") == contents[-1]
