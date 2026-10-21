"""Comparação isolada entre modelo único e regras; nunca ativa uma política."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID

from neoson_core.engine import (
    Engine,
    LLMProvider,
    RouteDecision,
    RouteRequest,
    RunResult,
    UserMessage,
)
from neoson_core.engine.route import Router
from neoson_core.engine.tools import Verdict
from neoson_core.routing.dataset import RoutingCase, RoutingDataset
from neoson_core.routing.lab_meter import Attempt, Ledger, Meter
from neoson_core.routing.lab_plan import BenchmarkPlan, Scenario
from neoson_core.routing.lab_sandbox import Sandbox
from neoson_core.routing.lab_store import EvaluationStore
from neoson_core.routing.policy_config import PolicySnapshot, SnapshotRouter
from neoson_core.routing.rules import RulesRouter

EngineFactory = Callable[[Path, LLMProvider, Router, Sandbox, BenchmarkPlan], Engine]
ProviderFactory = Callable[[RoutingCase, str], LLMProvider]


class ObservedRouter:
    def __init__(
        self, profile: str, baseline: bool, snapshot: PolicySnapshot | None = None
    ) -> None:
        self.profile, self.baseline = profile, baseline
        self.snapshot = snapshot
        self.decision: RouteDecision | None = None

    async def decide(self, request: RouteRequest) -> RouteDecision:
        self.decision = (
            RouteDecision(
                "conversation",
                "low",
                "direct",
                self.profile,
                1.0,
                "single-model-v1",
                "fixed:model",
            )
            if self.baseline
            else await (
                RulesRouter(self.profile)
                if self.snapshot is None
                else SnapshotRouter(self.snapshot, self.profile)
            ).decide(request)
        )
        return self.decision


@dataclass(frozen=True, slots=True)
class CaseMetrics:
    id: str
    category: str
    expected_intent: str
    predicted_intent: str | None
    intent_correct: bool
    complexity_correct: bool
    mode_correct: bool
    passed: bool
    safe: bool
    reasons: tuple[str, ...]
    state: str
    profile: str
    session_stable: bool
    session_profile_override: bool
    escalated: bool
    called_tools: tuple[str, ...]
    requested_tools: tuple[str, ...]
    invalid_requests: int
    injected_fault_observed: bool
    attempts: tuple[Attempt, ...]
    seconds: float


class RouterBenchmark:
    def __init__(
        self, store: EvaluationStore, engine: EngineFactory, provider: ProviderFactory
    ) -> None:
        self.store, self.engine, self.provider = store, engine, provider

    async def run(
        self, dataset: RoutingDataset, plan: BenchmarkPlan, request_id: UUID
    ) -> dict[str, object]:
        cases = dataset.select(plan.split)
        if plan.dataset_checksum != dataset.checksum:
            raise ValueError("Checksum do dataset difere do plano.")
        if {s.case_id for s in plan.scenarios} != {c.id for c in cases}:
            raise ValueError("O plano deve cobrir exatamente os casos do split.")
        names = {p.name for p in plan.profiles}
        if any(
            c.session_model_profile and c.session_model_profile not in names
            for c in cases
        ):
            raise ValueError("Perfil de sessão sem modelo no plano.")
        cached = self.store.claim(request_id, dataset, plan)
        if cached is not None:
            return cached
        ledger = Ledger()
        base: list[CaseMetrics] = []
        candidate: list[CaseMetrics] = []
        try:
            with TemporaryDirectory(prefix="neoson-router-lab-") as temp:
                for index, case in enumerate(cases):
                    scenario = next(s for s in plan.scenarios if s.case_id == case.id)
                    # Execuções pareadas e bancos distintos, inclusive entre versões.
                    for baseline, reports, profile in (
                        (True, base, plan.baseline_profile),
                        (False, candidate, plan.candidate_profile),
                    ):
                        reports.append(
                            await self._case(
                                Path(temp) / f"{index}-{baseline}.db",
                                case,
                                scenario,
                                plan,
                                baseline,
                                profile,
                                ledger,
                            )
                        )
            regressions = [
                a.id
                for a, b in zip(base, candidate, strict=True)
                if a.passed and not b.passed
            ]
            a, b = aggregate(base), aggregate(candidate)
            comparable = (
                plan.mode == "openai"
                and not regressions
                and all(
                    r.safe and r.session_stable and not r.reasons
                    for r in (*base, *candidate)
                )
                and all(
                    m["cost_usd"] is not None and m["usage_complete"] for m in (a, b)
                )
            )
            baseline_cost = a["cost_usd"]
            candidate_cost = b["cost_usd"]
            savings = None
            if comparable and isinstance(baseline_cost, float) and baseline_cost > 0:
                assert isinstance(candidate_cost, float)
                savings = round(
                    (baseline_cost - candidate_cost) / baseline_cost * 100, 4
                )
            report: dict[str, object] = {
                "id": str(request_id),
                "evaluated_at": datetime.now(UTC).isoformat(),
                "dataset_id": dataset.id,
                "dataset_version": dataset.version,
                "dataset_checksum": dataset.checksum,
                "plan_checksum": plan.checksum,
                "context": plan.public_context(),
                "split": plan.split,
                "mode": plan.mode,
                "baseline": {
                    "policy_version": "single-model-v1",
                    "metrics": a,
                    "cases": [asdict(r) for r in base],
                },
                "candidate": {
                    "policy_version": plan.candidate_version,
                    "metrics": b,
                    "cases": [asdict(r) for r in candidate],
                },
                "quality_regressions": regressions,
                "comparable_for_savings": comparable,
                "estimated_savings_pct": savings,
                "evaluation_overhead_usd": 0.0,
                "experiment_known_cost_usd": round(ledger.cost, 8),
                "experiment_cost_usd": None
                if ledger.unknown_cost
                else round(ledger.cost, 8),
                "activated": False,
            }
            self.store.finish(request_id, report)
            # Mesmo formato JSON no retorno inicial e nas repetições idempotentes.
            return json.loads(json.dumps(report))  # type: ignore[no-any-return]
        except BaseException:
            self.store.fail(request_id)
            raise

    async def _case(
        self,
        path: Path,
        case: RoutingCase,
        scenario: Scenario,
        plan: BenchmarkPlan,
        baseline: bool,
        default_profile: str,
        ledger: Ledger,
    ) -> CaseMetrics:
        profile = case.session_model_profile or default_profile
        router = ObservedRouter(
            profile,
            baseline,
            None if plan.promotion is None else plan.promotion.snapshot,
        )
        tools = Sandbox(scenario)
        meter = Meter(self.provider(case, profile), plan, scenario, ledger)
        started = perf_counter()
        result = None
        errors = []
        try:
            engine = self.engine(path, meter, router, tools, plan)
            result = await asyncio.wait_for(
                engine.run(UserMessage(text=case.text, channel="router_eval")),
                timeout=plan.timeout_seconds,
            )
        except Exception:
            errors.append("execution_error")
        elapsed = perf_counter() - started
        if meter.blocked:
            errors.append("budget")
        observed_fault = meter.injected or tools.fault_observed
        if scenario.fault != "none" and not observed_fault:
            errors.append("fault_not_observed")
        state = (
            "error"
            if result is None
            else (
                "waiting_approval"
                if result.status.value == "needs_approval"
                else result.status.value
            )
        )
        safe = not (set(tools.called) & set(case.outcome.forbidden_tools))
        errors.extend(_outcome(case, result, state, tools))
        safe = safe and not {"approval", "not_contains"}.intersection(errors)
        decision = router.decision
        stable = decision is not None and decision.primary_model_profile == profile
        if not stable:
            errors.append("session_profile")
        return CaseMetrics(
            case.id,
            case.category,
            case.route.intent,
            None if decision is None else decision.intent,
            decision is not None and decision.intent == case.route.intent,
            decision is not None and decision.complexity == case.route.complexity,
            decision is not None
            and decision.execution_mode == case.route.execution_mode,
            not errors,
            safe,
            tuple(errors),
            state,
            profile,
            stable,
            profile != default_profile,
            False,  # As duas políticas atuais mantêm o perfil fixo, sem escalonamento.
            tuple(tools.called),
            tuple(tools.requested),
            tools.invalid_requests,
            observed_fault,
            tuple(meter.attempts),
            elapsed,
        )


def _outcome(
    case: RoutingCase, result: RunResult | None, state: str, tools: Sandbox
) -> list[str]:
    reasons = []
    expected = case.outcome
    text = "" if result is None else result.text.casefold()
    if state != expected.state:
        reasons.append("state")
    if any(v.casefold() not in text for v in expected.contains):
        reasons.append("contains")
    if any(v.casefold() in text for v in expected.not_contains):
        reasons.append("not_contains")
    if set(expected.required_tools) - set(tools.called):
        reasons.append("required_tools")
    if set(expected.forbidden_tools) & set(tools.called):
        reasons.append("forbidden_tools")
    pending = Verdict.REQUIRE_APPROVAL in tools.policy.verdicts
    if (expected.approval == "required") != pending:
        reasons.append("approval")
    if tools.invalid_requests:
        reasons.append("invalid_tool")
    return reasons


def aggregate(cases: list[CaseMetrics]) -> dict[str, object]:
    attempts = [a for r in cases for a in r.attempts]
    known = all(a.input_tokens is not None for a in attempts)
    inputs = sum(a.input_tokens or 0 for a in attempts)
    outputs = sum(a.output_tokens or 0 for a in attempts)
    cached = sum(a.cached_tokens or 0 for a in attempts)
    cost = (
        round(sum(a.cost_usd or 0 for a in attempts), 8)
        if all(a.cost_usd is not None for a in attempts)
        else None
    )
    confusion: dict[str, dict[str, int]] = {}
    for r in cases:
        row = confusion.setdefault(r.expected_intent, {})
        predicted = r.predicted_intent or "unavailable"
        row[predicted] = row.get(predicted, 0) + 1
    count = len(cases)
    passed = sum(r.passed for r in cases)
    return {
        "cases": count,
        "passed": passed,
        "quality_rate": passed / count,
        "intent_accuracy": sum(r.intent_correct for r in cases) / count,
        "complexity_accuracy": sum(r.complexity_correct for r in cases) / count,
        "mode_accuracy": sum(r.mode_correct for r in cases) / count,
        "routing_accuracy": sum(
            r.intent_correct and r.complexity_correct and r.mode_correct for r in cases
        )
        / count,
        "confusion_matrix": confusion,
        "safety_failures": sum(not r.safe for r in cases),
        "input_tokens": inputs,
        "output_tokens": outputs,
        "cached_tokens": cached,
        "usage_complete": known,
        "attempts": len(attempts),
        "cache_hit_ratio": cached / inputs if known and inputs and cached else None,
        "cost_usd": cost,
        "cost_per_success": cost / passed if cost is not None and passed else None,
        "seconds": sum(r.seconds for r in cases),
        "escalation_rate": sum(r.escalated for r in cases) / count,
        "session_profile_override_rate": sum(r.session_profile_override for r in cases)
        / count,
        "errors": sum(bool(r.reasons) for r in cases),
    }
