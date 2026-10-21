import asyncio
import itertools
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from neoson_core.engine import (
    AgentLoop,
    Budget,
    LLMProvider,
    LLMResponse,
    Usage,
    UserMessage,
)
from neoson_core.infrastructure.config import ModelPrice, ServerSettings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.routing import benchmark, lab_meter
from neoson_core.routing.benchmark import RouterBenchmark
from neoson_core.routing.dataset import Category, RoutingCase, RoutingDataset
from neoson_core.routing.lab_plan import BenchmarkPlan, digest
from neoson_core.routing.lab_store import EvaluationStore
from neoson_core.routing.policy_config import PolicySnapshot
from neoson_core.routing.policy_gate import PolicyRejected
from neoson_core.routing.policy_registry import PolicyRegistry
from neoson_core.routing.rules import RulesRouter
from neoson_gateway.composition import build_engine
from neoson_gateway.router_lab import build_engine as lab_engine
from neoson_gateway.router_policy import main
from neoson_gateway.routing_runtime import binding_for, runtime_hash


@dataclass
class Ready:
    registry: PolicyRegistry
    evidence: Path
    dataset: RoutingDataset
    plan: BenchmarkPlan
    evaluation: UUID
    settings: ServerSettings

    def promote(self, request: UUID | None = None, **changes: Any) -> str:
        from neoson_core.telemetry.pricing import Price

        args: dict[str, Any] = {
            "request_id": request or uuid4(),
            "evaluation_id": self.evaluation,
            "evidence_path": self.evidence,
            "dataset": self.dataset,
            "plan": self.plan,
            "runtime_hash": runtime_hash(self.settings),
            "binding": binding_for(self.settings),
            "prices": {
                m: Price(p.input, p.output, p.cached_input)
                for m, p in self.settings.llm_prices.items()
            },
        }
        args.update(changes)
        return self.registry.promote(**args)

    def alter(self, change: Any, rehash: bool = False) -> None:
        with closing(connect(self.evidence)) as c:
            row = c.execute(
                "SELECT metrics_json FROM eval_runs WHERE id=?", (str(self.evaluation),)
            ).fetchone()
            report = json.loads(row[0])
            change(report)
            text = json.dumps(report)
            c.execute(
                "UPDATE eval_runs SET metrics_json=? WHERE id=?",
                (text, str(self.evaluation)),
            )
            if rehash:
                c.execute(
                    "UPDATE eval_runs SET evidence_checksum=? WHERE id=?",
                    (digest(text), str(self.evaluation)),
                )


@pytest.fixture
def ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ready:
    # Relógios de medição controlados; o gate não depende de ruído de CPU no CI.
    for module in (benchmark, lab_meter):
        counter = itertools.count(step=0.01)
        monkeypatch.setattr(module, "perf_counter", lambda c=counter: next(c))
    settings = ServerSettings(
        _env_file=None,
        db_path=tmp_path / "runtime.db",
        vault_path=tmp_path / "vault",
        openai_api_key="test-only",
        llm_model="fake-balanced",
        llm_prices={
            f"fake-{name}": ModelPrice(input=rate, output=rate, cached_input=rate)
            for name, rate in (("advanced", 2.0), ("balanced", 1.0))
        },
    )
    registry = PolicyRegistry(settings.db_path)
    cases: list[dict[str, Any]] = []
    for split, categories in (
        ("train", ("conversation",)),
        ("validation", ("conversation",)),
        ("test", get_args(Category)),
    ):
        for category in categories:
            text = {
                "memory": "recordar",
                "complex": "analise",
                "permission": "envio",
            }.get(category, "tarefa")
            intent = {
                "memory": "memory",
                "complex": "analysis",
                "permission": "email",
            }.get(category, "conversation")
            outcome: dict[str, Any] = {
                "state": "completed",
                "contains": ["correto"],
                "approval": "none",
            }
            if category == "permission":
                outcome = {
                    "state": "waiting_approval",
                    "contains": ["aprovação"],
                    "forbidden_tools": ["email_send"],
                    "approval": "required",
                }
            if category in ("tools", "memory", "failure"):
                outcome["required_tools"] = ["memory_search"]
            if category == "adversarial":
                outcome.update(forbidden_tools=["email_send"], approval="denied")
            cases.append(
                {
                    "id": f"{split}_{category}",
                    "split": split,
                    "category": category,
                    "text": f"{text} exemplo {split} {category}",
                    "route": {
                        "intent": intent,
                        "complexity": "low",
                        "execution_mode": "direct",
                    },
                    "outcome": outcome,
                }
            )
    dataset = RoutingDataset.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "id": "policy_fixture",
                "version": "1.0.0",
                "cases": cases,
            }
        )
    )
    snapshot = {
        "version": "rules-v2",
        "rules": [
            {"intent": intent, "keywords": [key]}
            for intent, key in (
                ("memory", "recordar"),
                ("analysis", "analise"),
                ("email", "envio"),
            )
        ],
    }
    plan = BenchmarkPlan.model_validate_json(
        json.dumps(
            {
                "promotion": {
                    "snapshot": snapshot,
                    "base_revision": registry.status()["revision"],
                    "runtime_hash": runtime_hash(settings),
                    "limits": {
                        "min_cases": 8,
                        "min_quality": 1.0,
                        "min_routing_accuracy": 1.0,
                        "max_cost_ratio": 1.0,
                        "max_tokens_ratio": 1.0,
                        "max_latency_ratio": 5.0,
                        "max_age_seconds": 3600,
                    },
                },
                "dataset_checksum": dataset.checksum,
                "split": "test",
                "mode": "openai",
                "profiles": [
                    {
                        "name": name,
                        "model": f"fake-{name}",
                        "prices": {"input": rate, "output": rate, "cached_input": rate},
                    }
                    for name, rate in (("advanced", 2.0), ("balanced", 1.0))
                ],
                "baseline_profile": "advanced",
                "candidate_profile": "balanced",
                "scenarios": [
                    {
                        "case_id": c.id,
                        "fault": "tool_error" if c.category == "failure" else "none",
                        "denied_tools": ["email_send"]
                        if c.category == "adversarial"
                        else [],
                    }
                    for c in dataset.select("test")
                ],
                "now": "2030-01-01T12:00:00Z",
                "max_total_cost_usd": 0.5,
            }
        )
    )

    def provider(case: RoutingCase, profile: str) -> LLMProvider:
        script = []
        if case.category in ("tools", "memory", "failure"):
            script.append(call_tool("memory_search"))
        if case.category in ("permission", "adversarial"):
            script.append(call_tool("email_send"))
        script.append(reply("correto"))
        return ScriptedProvider(
            [
                LLMResponse(step.message, Usage(100, 10, 20), f"fake-{profile}")
                for step in script
            ]
        )

    evidence = tmp_path / "evidence.db"
    evaluation = uuid4()
    report: dict[str, Any] = asyncio.run(
        RouterBenchmark(EvaluationStore(evidence), lab_engine, provider).run(
            dataset, plan, evaluation
        )
    )
    assert report["candidate"]["metrics"]["passed"] == 8
    assert registry.status()["version"] == "rules-v1"
    return Ready(registry, evidence, dataset, plan, evaluation, settings)


def test_explicit_promotion_rollback_and_no_historical_reactivation(
    ready: Ready,
) -> None:
    request = uuid4()
    before = ready.registry.status()["revision"]
    assert ready.promote(request) == "rules-v2"
    assert ready.promote(request) == "rules-v2"
    assert ready.registry.status()["revision"] != before
    revision = ready.registry.status()["revision"]
    rollback = uuid4()
    assert ready.registry.rollback(rollback, revision) == "rules-v1"
    assert ready.registry.rollback(rollback, revision) == "rules-v1"
    with pytest.raises(PolicyRejected):
        ready.promote(request)
    with pytest.raises(PolicyRejected, match="catálogo"):
        ready.promote()
    with closing(connect(ready.registry.path)) as c:
        assert (
            c.execute("SELECT COUNT(*) FROM routing_policy_events").fetchone()[0] == 3
        )
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize(
    "kind",
    [
        "hash",
        "aggregate",
        "missing",
        "regression",
        "expired",
        "future",
        "usage",
        "model",
    ],
)
def test_invalid_evidence_never_changes_active(ready: Ready, kind: str) -> None:
    def alter(report: dict[str, Any]) -> None:
        if kind in ("hash", "aggregate"):
            report["candidate"]["metrics"]["routing_accuracy"] = 99
        elif kind == "missing":
            report["candidate"]["cases"].pop()
        elif kind == "regression":
            report["candidate"]["cases"][0]["passed"] = False
        elif kind in ("expired", "future"):
            report["evaluated_at"] = (
                "2000-01-01T00:00:00Z" if kind == "expired" else "2099-01-01T00:00:00Z"
            )
        elif kind == "usage":
            report["candidate"]["cases"][0]["attempts"][0]["cost_usd"] = None
        else:
            report["candidate"]["cases"][0]["attempts"][0]["model_checksum"] = "0" * 64

    ready.alter(alter, rehash=kind != "hash")
    with pytest.raises(PolicyRejected):
        ready.promote()
    assert ready.registry.status()["version"] == "rules-v1"


def test_stale_context_prices_and_restricted_config_rejected(ready: Ready) -> None:
    with pytest.raises(PolicyRejected):
        ready.promote(runtime_hash="0" * 64)
    with pytest.raises(PolicyRejected):
        ready.promote(prices={})
    with pytest.raises(ValidationError):
        PolicySnapshot.model_validate_json(
            '{"version":"rules-v3","allowed_tools":["shell"]}'
        )
    assert ready.registry.status()["version"] == "rules-v1"


def test_resource_increase_recomputed_not_accepted_from_totals(ready: Ready) -> None:
    def alter(report: dict[str, Any]) -> None:
        from pydantic import TypeAdapter

        from neoson_core.routing.benchmark import CaseMetrics, aggregate
        from neoson_core.telemetry.pricing import Price, cost_usd

        cases = report["candidate"]["cases"]
        for case in cases:
            for attempt in case["attempts"]:
                attempt["input_tokens"] *= 2
                attempt["cost_usd"] = cost_usd(
                    "fake-balanced",
                    attempt["input_tokens"],
                    attempt["output_tokens"],
                    attempt["cached_tokens"],
                    {"fake-balanced": Price(1, 1, 1)},
                )
        parsed = list(
            TypeAdapter(tuple[CaseMetrics, ...]).validate_json(json.dumps(cases))
        )
        report["candidate"]["metrics"] = aggregate(parsed)

    ready.alter(alter, rehash=True)
    with pytest.raises(PolicyRejected):
        ready.promote()


def test_concurrent_promotions_and_atomic_event_failure(ready: Ready) -> None:
    def approve(_: int) -> str:
        try:
            return ready.promote()
        except PolicyRejected:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(approve, range(2))) == ["blocked", "rules-v2"]


def test_event_failure_rolls_back_entire_promotion(ready: Ready) -> None:
    import sqlite3

    with closing(connect(ready.registry.path)) as c:
        c.execute(
            "CREATE TRIGGER reject_review BEFORE INSERT ON routing_policy_events"
            " WHEN NEW.action='promote' BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        ready.promote()
    assert ready.registry.status()["version"] == "rules-v1"
    with closing(connect(ready.registry.path)) as c:
        assert c.execute("SELECT COUNT(*) FROM routing_policies").fetchone()[0] == 1


def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    observed: list[str] = []

    def create(**kwargs: Any) -> LLMProvider:
        model = next(iter(kwargs["models"].values()))
        observed.append(model)
        return ScriptedProvider(
            [
                LLMResponse(reply("correto").message, Usage(10, 5), model)
                for _ in range(8)
            ]
        )

    monkeypatch.setattr(OpenAIProvider, "from_api_key", create)
    return observed


def test_composition_pins_first_turn_across_promotion_and_rollback(
    ready: Ready, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_runtime(monkeypatch)
    engine = build_engine(ready.settings)
    first = asyncio.run(engine.run(UserMessage(text="recordar exemplo")))
    assert first.session_id is not None
    ready.promote()
    second = asyncio.run(
        engine.run(UserMessage(text="recordar de novo", session_id=first.session_id))
    )
    newer = asyncio.run(engine.run(UserMessage(text="recordar novo")))
    ready.registry.rollback(uuid4(), ready.registry.status()["revision"])
    after = asyncio.run(engine.run(UserMessage(text="recordar após rollback")))
    assert second.session_id == first.session_id
    with closing(connect(ready.registry.path)) as c:
        bindings = {
            r["session_id"]: r["version"]
            for r in c.execute("SELECT * FROM routing_session_bindings")
        }
        assert bindings[str(first.session_id)] == "rules-v1"
        assert bindings[str(newer.session_id)] == "rules-v2"
        assert bindings[str(after.session_id)] == "rules-v1"


def test_model_stays_after_configuration_change(
    ready: Ready, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = fake_runtime(monkeypatch)
    first = asyncio.run(build_engine(ready.settings).run(UserMessage(text="primeiro")))
    changed = ready.settings.model_copy(update={"llm_model": "fake-new"})
    engine = build_engine(changed)
    asyncio.run(engine.run(UserMessage(text="segundo", session_id=first.session_id)))
    asyncio.run(engine.run(UserMessage(text="nova sessão")))
    assert observed == ["fake-balanced", "fake-balanced", "fake-new"]


def test_legacy_model_recovered_without_silent_switch(
    ready: Ready, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    legacy = AgentLoop(
        ScriptedProvider([LLMResponse(reply("antigo").message, Usage(), "fake-old")]),
        RulesRouter("balanced"),
        Budget(2, 5),
        store=SqliteConversationStore(ready.registry.path),
    )
    first = asyncio.run(legacy.run(UserMessage(text="legado")))
    observed = fake_runtime(monkeypatch)
    asyncio.run(
        build_engine(ready.settings).run(
            UserMessage(text="retomar", session_id=first.session_id)
        )
    )
    assert observed == ["fake-old"]
    assert "legacy_session" in caplog.text


def test_policy_corruption_falls_back_preserving_model(
    ready: Ready, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    observed = fake_runtime(monkeypatch)
    ready.promote()
    first = asyncio.run(build_engine(ready.settings).run(UserMessage(text="recordar")))
    with closing(connect(ready.registry.path)) as c:
        c.execute(
            "UPDATE routing_policies SET snapshot_json='{}' WHERE version='rules-v2'"
        )
    result = asyncio.run(
        build_engine(ready.settings).run(
            UserMessage(text="recordar", session_id=first.session_id)
        )
    )
    assert result.model == "fake-balanced" and observed == [
        "fake-balanced",
        "fake-balanced",
    ]
    assert "policy_invalid" in caplog.text


def test_cli_status_review_and_rollback(
    ready: Ready, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import neoson_gateway.router_policy as cli

    monkeypatch.setattr(cli, "load_settings", lambda: ready.settings)
    assert main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["runtime_hash"] == runtime_hash(ready.settings)
    data_path, plan_path = tmp_path / "dataset.json", tmp_path / "plan.json"
    data_path.write_text(ready.dataset.model_dump_json(), encoding="utf-8")
    plan_path.write_text(ready.plan.model_dump_json(), encoding="utf-8")
    assert (
        main(
            [
                "promote",
                str(ready.evaluation),
                str(data_path),
                str(plan_path),
                "--evidence-db",
                str(ready.evidence),
            ]
        )
        == 0
    )

    assert "rules-v2" in capsys.readouterr().out
    assert (
        main(["rollback", "--expected-revision", ready.registry.status()["revision"]])
        == 0
    )


@pytest.mark.parametrize(
    "changes", [{"mode": "scripted"}, {"split": "validation"}, {"promotion": None}]
)
def test_non_production_or_incomplete_plan_cannot_promote(
    ready: Ready, changes: dict[str, Any]
) -> None:
    with pytest.raises(PolicyRejected):
        ready.promote(plan=ready.plan.model_copy(update=changes))
    assert ready.registry.status()["version"] == "rules-v1"


def test_new_context_uses_builtin_until_reevaluation(
    ready: Ready, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    fake_runtime(monkeypatch)
    ready.promote()
    changed = ready.settings.model_copy(update={"llm_model": "fake-new"})
    result = asyncio.run(build_engine(changed).run(UserMessage(text="recordar")))
    with closing(connect(ready.registry.path)) as c:
        row = c.execute(
            "SELECT version FROM routing_session_bindings WHERE session_id=?",
            (str(result.session_id),),
        ).fetchone()
        assert row[0] == "rules-v1"
    assert "runtime_changed" in caplog.text


def test_corrupted_model_binding_blocks_call_without_replacement(
    ready: Ready, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = fake_runtime(monkeypatch)
    first = asyncio.run(build_engine(ready.settings).run(UserMessage(text="início")))
    with closing(connect(ready.registry.path)) as c:
        c.execute("UPDATE routing_session_bindings SET model_json='{}'")
    with pytest.raises(PolicyRejected, match="Vínculo"):
        asyncio.run(
            build_engine(ready.settings).run(
                UserMessage(text="retomar", session_id=first.session_id)
            )
        )
    assert observed == ["fake-balanced"]


def test_valid_json_with_recomputed_hash_still_needs_approval_audit(
    ready: Ready, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    from neoson_core.routing.policy_registry import snapshot_checksum

    fake_runtime(monkeypatch)
    ready.promote()
    changed = PolicySnapshot(version="rules-v2", default_intent="unapproved")
    with closing(connect(ready.registry.path)) as c:
        c.execute(
            "UPDATE routing_policies SET snapshot_json=?,checksum=?"
            " WHERE version='rules-v2'",
            (changed.model_dump_json(), snapshot_checksum(changed)),
        )
    result = asyncio.run(build_engine(ready.settings).run(UserMessage(text="texto")))
    assert result.model == "fake-balanced" and "policy_invalid" in caplog.text
