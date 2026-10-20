import asyncio
import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import yaml

from neoson_core.engine import (
    Message,
    OutboxEvent,
    Role,
    RunRecord,
    RunStatus,
    Usage,
)
from neoson_core.infrastructure.config import load_settings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.telemetry import metrics, usage
from neoson_core.telemetry.pricing import Price, cost_usd, price_for
from neoson_core.telemetry.vault import HANDLER_NAME as VAULT_HANDLER
from neoson_core.telemetry.vault import (
    VaultExporter,
    frontmatter,
    render_daily,
    write_atomic,
)
from neoson_worker.main import build_handlers
from neoson_worker.worker import Handler, Worker

ZONE = ZoneInfo("America/Sao_Paulo")
SECRET_TEXT = "conteudo-privado-da-conversa"
PRICES = {"modelo-x": Price(input=2.0, output=8.0, cached_input=0.5)}
DEFAULT_USAGE = Usage(1000, 500, 200)


def add_run(
    db: Path,
    started: datetime,
    status: RunStatus = RunStatus.COMPLETED,
    model: str = "modelo-x-2026",
    usage: Usage = DEFAULT_USAGE,
) -> None:
    store = SqliteConversationStore(db)
    session = asyncio.run(store.create_session("http"))
    run_id = uuid4()
    kinds = {
        RunStatus.COMPLETED: "neoson.run.completed",
        RunStatus.FAILED: "neoson.run.failed",
        RunStatus.NEEDS_APPROVAL: "neoson.run.waiting_approval",
    }
    asyncio.run(
        store.record_run(
            RunRecord(
                run_id=run_id,
                session_id=session,
                status=status,
                started_at=started,
                ended_at=started,
                messages=(
                    Message(role=Role.USER, content=SECRET_TEXT),
                    Message(role=Role.ASSISTANT, content=SECRET_TEXT),
                ),
                usage=usage,
                model=model,
                events=(
                    OutboxEvent(
                        type=kinds[status],
                        run_id=run_id,
                        session_id=session,
                        occurred_at=started,
                        payload={
                            "model": model,
                            "usage": {
                                "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens,
                                "cached_tokens": usage.cached_tokens,
                            },
                        },
                    ),
                ),
            )
        )
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "neoson.db"
    # 20/10 em São Paulo: das 03:00 UTC de 20/10 às 03:00 UTC de 21/10.
    add_run(path, datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    add_run(path, datetime(2026, 10, 21, 2, 30, tzinfo=UTC), RunStatus.FAILED)
    add_run(path, datetime(2026, 10, 20, 2, 0, tzinfo=UTC))  # ainda 19/10 local
    add_run(
        path,
        datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
        RunStatus.NEEDS_APPROVAL,
        model="sem-preco",
    )
    return path


def process(db: Path, vault: Path | None = None) -> None:
    """Roda o worker com os handlers de uso (com preços) e, se pedido, do vault."""
    handlers = [Handler(usage.HANDLER_NAME, usage.UsageRecorder(PRICES))]
    if vault is not None:
        handlers.append(Handler(VAULT_HANDLER, VaultExporter(vault, ZONE)))
    Worker(db, {kind: handlers for kind in usage.EVENT_TYPES}, "w1").run_once()


# --- preços ---


def test_price_lookup_by_exact_name_or_longest_prefix() -> None:
    prices = {"gpt": Price(1, 1), "gpt-6": Price(2, 2), "gpt-6-luna": Price(3, 3)}

    assert price_for("gpt-6-luna", prices) == Price(3, 3)
    assert price_for("gpt-6-luna-2026-10-01", prices) == Price(3, 3)
    assert price_for("gpt-6-sol", prices) == Price(2, 2)
    assert price_for("outro", prices) is None
    assert price_for(None, prices) is None


def test_cost_uses_the_cached_rate_and_unknown_price_is_none() -> None:
    # 800 entrada normal × 2 + 200 cache × 0,5 + 500 saída × 8 = 5700 / 1e6
    assert cost_usd("modelo-x", 1000, 500, 200, PRICES) == 0.0057
    assert cost_usd("modelo-x", 1000, 0, 0, {"modelo-x": Price(1, 1)}) == 0.001
    assert cost_usd("outro", 1000, 500, 0, PRICES) is None


def test_prices_come_from_json_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "NEOSON_LLM_PRICES",
        '{"modelo-x": {"input": 2, "output": 8, "cached_input": 0.5}}',
    )

    price = load_settings(env_file=None).llm_prices["modelo-x"]

    assert (price.input, price.output, price.cached_input) == (2, 8, 0.5)


# --- agregados ---


def test_daily_metrics_follow_the_user_time_zone_and_costs(db: Path) -> None:
    process(db)
    connection = connect(db)
    try:
        day = metrics.daily_metrics(connection, date(2026, 10, 20), ZONE)
        previous = metrics.daily_metrics(connection, date(2026, 10, 19), ZONE)
    finally:
        connection.close()

    assert (day.runs_total, day.completed, day.failed, day.waiting_approval) == (
        3,
        1,
        1,
        1,
    )
    assert day.success_pct == 33.3
    assert (day.input_tokens, day.output_tokens, day.cached_tokens) == (3000, 1500, 600)
    assert day.cache_pct == 20.0
    assert day.cost_usd == pytest.approx(0.0114)  # 2 execuções com preço
    assert day.runs_without_cost == 1
    assert day.models == {"modelo-x-2026": 2, "sem-preco": 1}
    assert previous.runs_total == 1


def test_empty_day_has_no_percentages(db: Path) -> None:
    connection = connect(db)
    try:
        empty = metrics.daily_metrics(connection, date(2026, 1, 1), ZONE)
    finally:
        connection.close()

    assert (empty.runs_total, empty.success_pct, empty.cache_pct, empty.cost_usd) == (
        0,
        None,
        None,
        None,
    )


def test_system_metrics_count_the_queue_and_worker_lag(db: Path) -> None:
    process(db)
    connection = connect(db)
    try:
        connection.execute(
            "UPDATE outbox_events SET status = 'pending', attempts = 2"
            " WHERE id = (SELECT id FROM outbox_events LIMIT 1)"
        )
        system = metrics.system_metrics(connection)
    finally:
        connection.close()

    assert system.outbox == {"completed": 3, "pending": 1}
    assert system.retrying == 1
    assert system.oldest_retry_at is not None
    assert system.avg_lag_seconds is not None
    assert system.last_run_at == datetime(2026, 10, 21, 2, 30, tzinfo=UTC)


# --- renderização e escrita ---


def test_daily_note_has_valid_yaml_frontmatter() -> None:
    note = render_daily(
        metrics.DailyMetrics(
            day=date(2026, 10, 20),
            runs_total=2,
            completed=2,
            models={"modelo: com dois pontos": 2},
        )
    )

    _, head, body = note.split("---\n", 2)
    data = yaml.safe_load(head)
    assert data["tipo"] == "neoson-metricas"
    assert data["data"] == "2026-10-20"
    assert data["sucesso_pct"] == 100.0
    assert data["custo_usd"] is None
    assert data["modelos"] == ["modelo: com dois pontos"]
    assert "# Métricas de 20/10/2026" in body


def test_frontmatter_quotes_strings_safely() -> None:
    head = frontmatter({"a": 'x: "y"\nz', "b": True, "c": None, "d": 1.5})

    assert yaml.safe_load(head.strip("-\n")) == {
        "a": 'x: "y"\nz',
        "b": True,
        "c": None,
        "d": 1.5,
    }


def test_atomic_write_skips_unchanged_content(tmp_path: Path) -> None:
    path = tmp_path / "Dashboard" / "Home.md"

    assert write_atomic(path, "um")
    mtime = path.stat().st_mtime_ns
    assert not write_atomic(path, "um")
    assert path.stat().st_mtime_ns == mtime
    assert write_atomic(path, "dois")
    assert path.read_text(encoding="utf-8") == "dois"
    assert [p.name for p in path.parent.iterdir()] == ["Home.md"]


def test_worker_exports_the_vault_without_conversation_content(
    db: Path, tmp_path: Path
) -> None:
    vault = tmp_path / "NeoSon-Vault"

    process(db, vault)

    files = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*.md"))
    assert files == [
        "Dashboard/Home.md",
        "Dashboard/LLM-Custos.md",
        "Dashboard/NeoSon-System.md",
        "Metricas/Diarias/2026-10-19.md",
        "Metricas/Diarias/2026-10-20.md",
    ]
    for path in vault.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert SECRET_TEXT not in text
        yaml.safe_load(text.split("---\n", 2)[1])
    daily = yaml.safe_load(
        (vault / "Metricas/Diarias/2026-10-20.md")
        .read_text(encoding="utf-8")
        .split("---\n", 2)[1]
    )
    assert daily["execucoes_total"] == 3
    costs = (vault / "Dashboard/LLM-Custos.md").read_text(encoding="utf-8")
    assert "| sem-preco | 1 |" in costs and "| — |" in costs


def test_exporting_again_without_new_data_changes_nothing(
    db: Path, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    process(db, vault)
    connection = connect(db)
    try:
        changed = VaultExporter(vault, ZONE).export(connection, date(2026, 10, 20))
    finally:
        connection.close()

    assert changed == []


def test_vault_export_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_METRICS_EXPORT_ENABLED", "false")

    handlers = build_handlers(load_settings(env_file=None))

    assert {h.name for hs in handlers.values() for h in hs} == {"telemetry.usage"}


def test_vault_handler_is_registered_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)

    handlers = build_handlers(load_settings(env_file=None))

    names = [h.name for h in handlers["neoson.run.completed"]]
    assert names == ["telemetry.usage", "telemetry.vault"]


def test_worker_can_regenerate_the_vault_from_the_database(
    db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from neoson_worker.main import main

    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_DB_PATH", str(db))
    monkeypatch.setenv("NEOSON_VAULT_PATH", str(tmp_path / "vault"))

    assert main(["--export-vault"]) == 0

    home = (tmp_path / "vault" / "Dashboard" / "Home.md").read_text(encoding="utf-8")
    assert "# NeoSon — Home" in home
    assert len(list((tmp_path / "vault" / "Metricas" / "Diarias").glob("*.md"))) == 7
