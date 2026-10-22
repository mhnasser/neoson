import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from neoson_core.engine import Usage
from neoson_core.providers.fake import FAKE_MODEL, ScriptedProvider, reply
from neoson_core.telemetry.pricing import Price
from neoson_worker import graph_main
from neoson_worker.graph_eval import (
    PANEL,
    RetrievalDataset,
    evaluate,
    export_panel,
    load_dataset,
    render_panel,
    save_report,
    seed,
)

SEED = (
    Path(__file__).resolve().parents[1]
    / "datasets"
    / "graph_retrieval"
    / "seed-v1.json"
)
NOW = datetime(2026, 10, 22, 20, tzinfo=UTC)


def run(tmp_path: Path, **options: Any) -> dict[str, Any]:
    dataset, checksum = load_dataset(SEED)
    workdir = tmp_path / f"work-{len(list(tmp_path.iterdir()))}"
    workdir.mkdir()
    return asyncio.run(
        evaluate(dataset, checksum, workdir, clock=lambda: NOW, **options)
    )


def raw() -> dict[str, Any]:
    return dict(json.loads(SEED.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["cases"].append(dict(d["cases"][0])),
        lambda d: d["cases"][0]["expected_sources"].append("memory_fact:99"),
        lambda d: d["cases"][1]["expected_sources"].append("memory_fact:4"),
        lambda d: d["sessions"][0].update(started_at="2026-09-02T12:00:00"),
        lambda d: d["facts"][0].update(confidence=1.5),
        lambda d: d["facts"][0].update(relation="KNOWS"),
        lambda d: d.update(extra=True),
        lambda d: d["sessions"].append(dict(d["sessions"][0])),
    ],
)
def test_dataset_is_strictly_validated(change: Any) -> None:
    data = raw()
    change(data)
    with pytest.raises(ValidationError):
        RetrievalDataset.model_validate(data)


def test_seed_dataset_is_valid_synthetic_and_checksummed() -> None:
    dataset, checksum = load_dataset(SEED)
    assert len(checksum) == 64 and dataset.schema_version == 1
    assert len(dataset.cases) == 8
    assert {f.status.value for f in dataset.facts} == {"active", "pending", "retracted"}


def test_same_cases_compare_history_and_hybrid_reproducibly(tmp_path: Path) -> None:
    first, second = run(tmp_path), run(tmp_path)
    assert first == second  # mesmo dataset, mesmo relógio: mesmo relatório
    history, hybrid = first["arms"]["history"], first["arms"]["hybrid"]
    assert first["mode"] == "deterministic" and first["cases_total"] == 8
    assert history["source_recall"] < hybrid["source_recall"] == 1.0
    assert hybrid["all_sources_rate"] == 1.0
    assert history["forbidden_rate"] == hybrid["forbidden_rate"] == 0.0
    # O ganho tem preço: mais contexto e fontes não pedidas.
    assert hybrid["mean_context_chars"] > history["mean_context_chars"]
    assert hybrid["mean_extra_sources"] > history["mean_extra_sources"]
    for case in first["cases"]:
        before, after = case["history"]["recall"], case["hybrid"]["recall"]
        assert before is None or after >= before, case["id"]
        assert "memory_fact:4" not in case["hybrid"]["retrieved"]
        assert "memory_fact:5" not in case["hybrid"]["retrieved"]


def test_seed_never_reuses_an_existing_database(tmp_path: Path) -> None:
    dataset, _ = load_dataset(SEED)
    db = tmp_path / "graph-eval.db"
    db.write_bytes(b"")
    with pytest.raises(ValueError, match="banco novo"):
        asyncio.run(seed(dataset, db))


def test_live_mode_scores_answers_tokens_and_cost(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            reply("Usamos SQLite.", Usage(100, 10)),
            reply("SQLite no Atlas.", Usage(200, 10)),
            reply("Não sei.", Usage(100, 5)),
            reply("Ela trabalha no Atlas.", Usage(200, 5)),
        ]
    )
    report = run(
        tmp_path,
        provider=provider,
        max_cases=2,
        prices={FAKE_MODEL: Price(1.0, 2.0)},
    )
    history, hybrid = report["arms"]["history"], report["arms"]["hybrid"]
    assert report["mode"] == "live" and report["cases_total"] == 2
    assert (history["answer_accuracy"], hybrid["answer_accuracy"]) == (0.5, 1.0)
    assert (history["input_tokens"], hybrid["input_tokens"]) == (200, 400)
    assert hybrid["cost_usd"] == pytest.approx((400 + 2 * 15) / 1e6)
    # Cada pergunta roda sem armazenamento: não polui o histórico semeado.
    assert all(len(r.messages) == 2 for r in provider.requests)


def test_panel_has_only_aggregates_and_case_ids(tmp_path: Path) -> None:
    report = run(tmp_path)
    panel = render_panel(report)
    dataset, _ = load_dataset(SEED)
    assert "Recall de fontes" in panel and "| historico_banco |" in panel
    private = [m.content for s in dataset.sessions for m in s.messages]
    private += [f.subject.name for f in dataset.facts] + [
        c.question for c in dataset.cases
    ]
    assert not [text for text in private if text in panel]
    vault = tmp_path / "vault"
    assert export_panel(report, vault) and not export_panel(report, vault)
    assert (vault / PANEL).read_text(encoding="utf-8") == panel
    saved = save_report(report, tmp_path / "reports")
    assert json.loads(saved.read_text(encoding="utf-8")) == report


def test_cli_eval_writes_report_and_panel_without_touching_real_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "real.db"))
    out = tmp_path / "out"
    assert graph_main.main(["eval", "--dataset", str(SEED), "--output", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "source_recall" in printed and "relatório:" in printed
    assert len(list(out.glob("graph-eval-*.json"))) == 1
    assert (tmp_path / "vault" / PANEL).exists()
    assert not (tmp_path / "real.db").exists()
    with pytest.raises(SystemExit, match="--live"):
        graph_main.main(["eval", "--dataset", str(SEED), "--live"])
    with pytest.raises(SystemExit, match="max-cases"):
        graph_main.main(["eval", "--dataset", str(SEED), "--max-cases", "0"])


def test_module_entry_point_runs_the_cli(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NEOSON_")}
    env.update(NEOSON_VAULT_PATH=str(tmp_path / "vault"), PYTHONUTF8="1")
    out = tmp_path / "out"
    finished = subprocess.run(
        [sys.executable, "-m", "neoson_worker.graph_main", "eval"]
        + ["--dataset", str(SEED), "--output", str(out)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    assert "relatório:" in finished.stdout and list(out.glob("*.json"))
