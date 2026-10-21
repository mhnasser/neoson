import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from neoson_core.engine import LLMError, LLMProvider, LLMRequest, LLMResponse, Usage
from neoson_core.infrastructure.db import connect
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.benchmark import ProviderFactory, RouterBenchmark
from neoson_core.routing.dataset import RoutingCase, RoutingDataset
from neoson_core.routing.lab_meter import Ledger, Meter
from neoson_core.routing.lab_plan import BenchmarkPlan, Scenario, load_plan
from neoson_core.routing.lab_store import BenchmarkConflict, EvaluationStore
from neoson_gateway.router_lab import build_engine, main


def corpus(**changes: object) -> RoutingDataset:
    cases = []
    for split in ("train", "validation", "test"):
        case: dict[str, object] = {
            "id": f"{split}_case",
            "split": split,
            "category": "conversation",
            "text": f"Entrada fictícia distinta de {split}.",
            "route": {
                "intent": "conversation",
                "complexity": "low",
                "execution_mode": "direct",
            },
            "outcome": {
                "state": "completed",
                "contains": ["correto"],
                "approval": "none",
            },
        }
        if split == "validation":
            case.update(changes)
        cases.append(case)
    return RoutingDataset.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "id": "benchmark_fixture",
                "version": "1.0.0",
                "cases": cases,
            }
        )
    )


def plan_for(data: RoutingDataset, **changes: object) -> BenchmarkPlan:
    value: dict[str, object] = {
        "dataset_checksum": data.checksum,
        "split": "validation",
        "mode": "scripted",
        "profiles": [
            {
                "name": name,
                "model": "fake-model",
                "prices": {
                    "input": 1.0,
                    "output": 2.0,
                    "cached_input": 0.5,
                },
            }
            for name in ("advanced", "balanced")
        ],
        "baseline_profile": "advanced",
        "candidate_profile": "balanced",
        "scenarios": [{"case_id": c.id} for c in data.select("validation")],
        "now": "2030-01-01T12:00:00Z",
    }
    value.update(changes)
    return BenchmarkPlan.model_validate_json(json.dumps(value))


def execute(
    tmp_path: Path,
    data: RoutingDataset,
    plan: BenchmarkPlan,
    factory: ProviderFactory | None = None,
) -> dict[str, Any]:
    runner = RouterBenchmark(
        EvaluationStore(tmp_path / "lab.db"),
        build_engine,
        factory
        or (
            lambda case, profile: ScriptedProvider(
                [reply("correto", Usage(100, 20, 50))]
            )
        ),
    )
    return asyncio.run(runner.run(data, plan, uuid4()))


def test_real_loop_paired_metrics_isolation_and_idempotence(tmp_path: Path) -> None:
    data = corpus()
    plan = plan_for(data)
    paths = []
    providers = []

    def factory(case: RoutingCase, profile: str) -> LLMProvider:
        provider = ScriptedProvider([reply("correto", Usage(100, 20, 50))])
        providers.append(provider)
        return provider

    def engine(*args: Any) -> Any:
        paths.append(args[0])
        return build_engine(*args)

    store = EvaluationStore(tmp_path / "lab.db")
    runner = RouterBenchmark(store, engine, factory)
    request_id = uuid4()
    report: dict[str, Any] = asyncio.run(runner.run(data, plan, request_id))
    assert report == asyncio.run(runner.run(data, plan, request_id))
    assert len(providers) == 2 and len(paths) == 2 and paths[0] != paths[1]
    assert all(not p.exists() for p in paths)
    metrics = report["candidate"]["metrics"]
    assert metrics["input_tokens"] == 100 and metrics["cached_tokens"] == 50
    assert metrics["cache_hit_ratio"] == 0.5
    assert metrics["cost_usd"] == pytest.approx(0.000115)
    assert metrics["routing_accuracy"] == metrics["quality_rate"] == 1.0
    assert metrics["confusion_matrix"] == {"conversation": {"conversation": 1}}
    assert report["experiment_cost_usd"] == pytest.approx(0.00023)
    assert report["estimated_savings_pct"] is None  # mocks não são economia real
    assert not report["activated"]
    with closing(connect(store.path)) as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0


def test_scoring_quality_is_independent_of_routing(tmp_path: Path) -> None:
    data = corpus(
        route={"intent": "memory", "complexity": "medium", "execution_mode": "retrieve"}
    )
    report = execute(tmp_path, data, plan_for(data))
    m = report["candidate"]["metrics"]
    assert m["quality_rate"] == 1 and m["routing_accuracy"] == 0
    assert m["confusion_matrix"] == {"memory": {"conversation": 1}}


def test_actual_tool_calls_and_fault_observation(tmp_path: Path) -> None:
    data = corpus(
        outcome={
            "state": "completed",
            "contains": ["correto"],
            "required_tools": ["memory_search"],
            "approval": "none",
        }
    )
    plan = plan_for(
        data, scenarios=[{"case_id": "validation_case", "fault": "memory_empty"}]
    )
    report = execute(
        tmp_path,
        data,
        plan,
        lambda c, p: ScriptedProvider(
            [
                call_tool("memory_search"),
                reply("correto"),
            ]
        ),
    )
    case = report["candidate"]["cases"][0]
    assert case["called_tools"] == ["memory_search"]
    assert case["injected_fault_observed"] and case["passed"]
    assert report["candidate"]["metrics"]["attempts"] == 2


@pytest.mark.parametrize("denied", [True, False])
def test_sensitive_tools_require_approval_or_deny(tmp_path: Path, denied: bool) -> None:
    data = corpus(
        outcome={
            "state": "completed" if denied else "waiting_approval",
            "contains": ["correto"] if denied else [],
            "forbidden_tools": ["email_send"],
            "approval": "denied" if denied else "required",
        }
    )
    plan = plan_for(
        data,
        scenarios=[
            {
                "case_id": "validation_case",
                "denied_tools": ["email_send"] if denied else [],
            }
        ],
    )
    report = execute(
        tmp_path,
        data,
        plan,
        lambda c, p: ScriptedProvider(
            [
                call_tool("email_send"),
                reply("correto"),
            ]
        ),
    )
    case = report["candidate"]["cases"][0]
    assert case["passed"] and case["safe"] and case["called_tools"] == []
    assert case["requested_tools"] == ["email_send"]


def test_session_profile_pinned_and_unknown_cost_not_zero(tmp_path: Path) -> None:
    data = corpus(session_model_profile="advanced")
    plan = plan_for(
        data,
        profiles=[{"name": n, "model": "fake-model"} for n in ("advanced", "balanced")],
    )
    report = execute(tmp_path, data, plan)
    case = report["candidate"]["cases"][0]
    assert case["profile"] == "advanced" and case["session_stable"]
    assert report["candidate"]["metrics"]["cost_usd"] is None
    assert report["experiment_cost_usd"] is None
    assert report["estimated_savings_pct"] is None


def test_retries_record_unknown_charges_without_failing_final_quality(
    tmp_path: Path,
) -> None:
    data = corpus()
    report = execute(
        tmp_path,
        data,
        plan_for(data, max_retries=1),
        lambda c, p: ScriptedProvider(
            [
                LLMError("sentinela-privada", retryable=True),
                reply("correto"),
            ]
        ),
    )
    m = report["candidate"]["metrics"]
    assert m["attempts"] == 2 and m["quality_rate"] == 1
    assert m["cost_usd"] is None and not m["usage_complete"]
    assert m["cache_hit_ratio"] is None
    assert "sentinela-privada" not in json.dumps(report)


def test_explicit_provider_failure_never_calls_provider(tmp_path: Path) -> None:
    data = corpus()
    providers = []

    def factory(c: RoutingCase, p: str) -> LLMProvider:
        provider = ScriptedProvider([])
        providers.append(provider)
        return provider

    report = execute(
        tmp_path,
        data,
        plan_for(
            data,
            scenarios=[
                {
                    "case_id": "validation_case",
                    "fault": "provider_error",
                }
            ],
        ),
        factory,
    )
    assert all(not p.requests for p in providers)
    assert report["candidate"]["cases"][0]["injected_fault_observed"]
    assert report["experiment_cost_usd"] == 0.0


def test_missing_fault_and_regression_prevent_savings(tmp_path: Path) -> None:
    data = corpus()
    report = execute(
        tmp_path,
        data,
        plan_for(
            data,
            scenarios=[
                {
                    "case_id": "validation_case",
                    "fault": "tool_error",
                }
            ],
        ),
        lambda c, p: ScriptedProvider(
            [reply("correto" if p == "advanced" else "errado")]
        ),
    )
    assert "fault_not_observed" in report["baseline"]["cases"][0]["reasons"]
    assert report["estimated_savings_pct"] is None
    report = execute(
        tmp_path,
        data,
        plan_for(data),
        lambda c, p: ScriptedProvider(
            [reply("correto" if p == "advanced" else "errado")]
        ),
    )
    assert report["quality_regressions"] == ["validation_case"]


def test_scenarios_are_not_persisted_as_text(tmp_path: Path) -> None:
    data = corpus(text="Entrada sentinela-privada de exemplo.")
    plan = plan_for(
        data,
        scenarios=[
            {
                "case_id": "validation_case",
                "replies": [
                    {
                        "tool": "memory_search",
                        "summary": "sentinela-privada",
                    }
                ],
            }
        ],
    )
    report = execute(
        tmp_path,
        data,
        plan,
        lambda c, p: ScriptedProvider(
            [
                reply("correto sentinela-privada"),
            ]
        ),
    )
    assert "sentinela-privada" not in json.dumps(report)
    with closing(connect(tmp_path / "lab.db")) as c:
        stored = "".join(str(tuple(r)) for r in c.execute("SELECT * FROM eval_runs"))
        assert "sentinela-privada" not in stored


def test_reservation_conflicts_and_tampered_evidence(tmp_path: Path) -> None:
    data = corpus()
    plan = plan_for(data)
    store = EvaluationStore(tmp_path / "lab.db")
    request = uuid4()
    assert store.claim(request, data, plan) is None
    with pytest.raises(BenchmarkConflict, match="reservado"):
        store.claim(request, data, plan)
    store.fail(request)
    with pytest.raises(BenchmarkConflict):
        store.claim(request, data, plan)
    request = uuid4()
    runner = RouterBenchmark(
        store, build_engine, lambda c, p: ScriptedProvider([reply("correto")])
    )
    asyncio.run(runner.run(data, plan, request))
    with pytest.raises(BenchmarkConflict, match="contexto"):
        store.claim(request, data, plan_for(data, max_steps=3))
    with closing(connect(store.path)) as c:
        c.execute("UPDATE eval_runs SET metrics_json='{}' WHERE id=?", (str(request),))
    with pytest.raises(BenchmarkConflict, match="alterado"):
        store.claim(request, data, plan)
    changed = corpus(text="Outro texto único.")
    with pytest.raises(BenchmarkConflict, match="versão"):
        store.claim(uuid4(), changed, plan_for(changed))


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "openai"},
        {"max_total_cost_usd": 2},
        {"max_retries": 3},
        {"max_output_tokens": -1},
        {"timeout_seconds": float("nan")},
        {"baseline_profile": "unknown"},
        {"now": "2030-01-01T12:00:00"},
    ],
)
def test_invalid_plans_rejected(tmp_path: Path, changes: dict[str, object]) -> None:
    raw = plan_for(corpus()).model_dump(mode="json")
    raw.update(changes)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_plan(path)


def test_call_and_token_budgets_stop_before_more_calls(tmp_path: Path) -> None:
    data = corpus()
    report = execute(
        tmp_path,
        data,
        plan_for(data, max_calls_per_case=1),
        lambda c, p: ScriptedProvider([call_tool("memory_search"), reply("correto")]),
    )
    assert report["candidate"]["metrics"]["attempts"] == 1
    assert "budget" in report["candidate"]["cases"][0]["reasons"]
    report = execute(tmp_path, data, plan_for(data, max_total_tokens=2048))
    assert report["candidate"]["metrics"]["attempts"] == 0
    assert report["estimated_savings_pct"] is None


def test_timeout_and_invalid_usage_are_unknown_not_free(tmp_path: Path) -> None:
    class Slow:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            await asyncio.sleep(1)
            return reply("correto")

    data = corpus()
    report = execute(
        tmp_path, data, plan_for(data, timeout_seconds=0.02), lambda c, p: Slow()
    )
    assert report["experiment_cost_usd"] is None
    report = execute(
        tmp_path,
        data,
        plan_for(data),
        lambda c, p: ScriptedProvider(
            [
                reply("correto", Usage(-1, 5, 0)),
            ]
        ),
    )
    assert report["candidate"]["cases"][0]["attempts"][0]["error"] == "invalid_usage"


def test_live_budget_stops_after_unknown_charge() -> None:
    data = corpus()
    plan = plan_for(data, mode="openai", max_total_cost_usd=0.1, max_retries=1)
    provider = ScriptedProvider([LLMError("falha", retryable=True), reply("correto")])
    meter = Meter(provider, plan, Scenario(case_id="validation_case"), Ledger())
    request = LLMRequest("advanced", (reply("entrada").message,))
    with pytest.raises(LLMError, match="Orçamento"):
        asyncio.run(meter.complete(request))
    assert len(provider.requests) == 1 and meter.blocked


def test_cli_offline_is_idempotent_and_live_requires_flag(
    tmp_path: Path, capsys: Any
) -> None:
    data = corpus()
    dataset_path, plan_path = tmp_path / "dataset.json", tmp_path / "plan.json"
    dataset_path.write_text(data.model_dump_json(), encoding="utf-8")
    plan_path.write_text(plan_for(data).model_dump_json(), encoding="utf-8")
    args = [
        str(dataset_path),
        str(plan_path),
        "--db",
        str(tmp_path / "lab.db"),
        "--request-id",
        str(uuid4()),
    ]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    assert first == json.loads(capsys.readouterr().out)
    assert main([*args, "--live"]) == 1
    assert "não concluído" in capsys.readouterr().err


def test_savings_only_when_both_arms_pass_with_known_comparable_cost(
    tmp_path: Path,
) -> None:
    data = corpus()
    plan = plan_for(
        data,
        mode="openai",
        max_total_cost_usd=0.1,
        profiles=[
            {
                "name": name,
                "model": f"fake-{name}",
                "prices": {
                    "input": rate,
                    "output": rate,
                    "cached_input": rate,
                },
            }
            for name, rate in (("advanced", 2.0), ("balanced", 1.0))
        ],
    )

    # Provedor injetado no teste; nenhuma rede. Testa a aritmética do modo real.
    def provider(c: RoutingCase, p: str) -> LLMProvider:
        return ScriptedProvider(
            [LLMResponse(reply("correto").message, Usage(100, 20, 0), f"fake-{p}")]
        )

    report = execute(tmp_path, data, plan, provider)
    assert report["comparable_for_savings"]
    assert report["estimated_savings_pct"] == 50.0
    report = execute(
        tmp_path,
        data,
        plan,
        lambda c, p: ScriptedProvider(
            [
                LLMResponse(
                    reply("correto" if p == "advanced" else "errado").message,
                    Usage(100, 20, 0),
                    f"fake-{p}",
                ),
            ]
        ),
    )
    assert report["estimated_savings_pct"] is None


def test_equal_global_quality_does_not_hide_per_case_regressions(
    tmp_path: Path,
) -> None:
    raw = corpus().model_dump(mode="json")
    extra = json.loads(json.dumps(raw["cases"][1]))
    extra.update(id="validation_other", text="Outro exemplo fictício independente.")
    raw["cases"].append(extra)
    data = RoutingDataset.model_validate_json(json.dumps(raw))

    def provider(c: RoutingCase, p: str) -> LLMProvider:
        good = (c.id == "validation_case") == (p == "advanced")
        return ScriptedProvider([reply("correto" if good else "errado")])

    report = execute(tmp_path, data, plan_for(data), provider)
    assert report["baseline"]["metrics"]["passed"] == 1
    assert report["candidate"]["metrics"]["passed"] == 1
    assert report["quality_regressions"] == ["validation_case"]
    assert report["estimated_savings_pct"] is None


def test_concurrent_claims_only_one_reservation(tmp_path: Path) -> None:
    data = corpus()
    plan = plan_for(data)
    store = EvaluationStore(tmp_path / "lab.db")
    request = uuid4()

    def claim(_: int) -> str:
        try:
            store.claim(request, data, plan)
            return "claimed"
        except BenchmarkConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(claim, range(2))) == ["claimed", "conflict"]


def test_crash_reservation_failed_and_private_exception_not_saved(
    tmp_path: Path,
) -> None:
    data = corpus()
    store = EvaluationStore(tmp_path / "lab.db")

    def broken(c: RoutingCase, p: str) -> LLMProvider:
        raise RuntimeError("sentinela-privada")

    runner = RouterBenchmark(store, build_engine, broken)
    request = uuid4()
    with pytest.raises(RuntimeError):
        asyncio.run(runner.run(data, plan_for(data), request))
    with closing(connect(store.path)) as c:
        row = c.execute("SELECT * FROM eval_runs").fetchone()
        assert row["status"] == "failed" and row["metrics_json"] is None
        assert "sentinela" not in str(tuple(row))
    with pytest.raises(BenchmarkConflict):
        asyncio.run(runner.run(data, plan_for(data), request))


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset_checksum": "0" * 64},
        {"scenarios": [{"case_id": "unknown_case"}]},
    ],
)
def test_incompatible_plan_does_not_call_provider_or_reserve(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    data = corpus()

    def unused(c: RoutingCase, p: str) -> LLMProvider:
        raise AssertionError("Não deve chamar provider.")

    store = EvaluationStore(tmp_path / "lab.db")
    runner = RouterBenchmark(store, build_engine, unused)
    with pytest.raises(ValueError):
        asyncio.run(runner.run(data, plan_for(data, **changes), uuid4()))
    with closing(connect(store.path)) as c:
        assert c.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0] == 0


def test_reported_usage_exceeding_budget_cannot_claim_success(tmp_path: Path) -> None:
    data = corpus()
    report = execute(
        tmp_path,
        data,
        plan_for(data),
        lambda c, p: ScriptedProvider(
            [
                reply("correto", Usage(10, 3000, 0)),
            ]
        ),
    )
    assert "budget" in report["candidate"]["cases"][0]["reasons"]
    assert report["estimated_savings_pct"] is None


@pytest.mark.parametrize("kind", ["approval", "forbidden_output"])
def test_safety_counts_approval_mismatch_and_forbidden_output(
    tmp_path: Path, kind: str
) -> None:
    expected: dict[str, object] = {
        "state": "completed",
        "contains": ["correto"],
        "approval": "none",
    }
    if kind == "approval":
        expected.update(approval="denied", forbidden_tools=["email_send"])
        script = [call_tool("email_send"), reply("correto")]
    else:
        expected["not_contains"] = ["sentinela-privada"]
        script = [reply("correto sentinela-privada")]
    data = corpus(outcome=expected)
    report = execute(
        tmp_path, data, plan_for(data), lambda c, p: ScriptedProvider(script)
    )
    assert report["candidate"]["metrics"]["safety_failures"] == 1
    assert not report["candidate"]["cases"][0]["safe"]
    assert "sentinela-privada" not in json.dumps(report)
