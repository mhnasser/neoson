"""Revalida evidência e limites; não confia nos agregados armazenados."""

import json
import math
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from uuid import UUID

from pydantic import TypeAdapter

from neoson_core.routing.benchmark import CaseMetrics, aggregate
from neoson_core.routing.dataset import Category, RoutingDataset
from neoson_core.routing.lab_plan import BenchmarkPlan, digest
from neoson_core.routing.policy_config import ModelBinding
from neoson_core.telemetry.pricing import Price, cost_usd, price_for


class PolicyRejected(ValueError):
    pass


def validate_evidence(
    path: Path,
    evaluation_id: UUID,
    dataset: RoutingDataset,
    plan: BenchmarkPlan,
    runtime_hash: str,
    binding: ModelBinding,
    prices: Mapping[str, Price],
) -> str:
    try:
        if (
            plan.candidate_profile != binding.profile
            or plan.profile(binding.profile).model != binding.model
            or plan.reasoning_effort != binding.reasoning_effort
        ):
            raise ValueError
        for profile in plan.profiles:
            rate = price_for(profile.model, prices)
            if (
                rate is None
                or profile.prices is None
                or (
                    rate.input,
                    rate.output,
                    rate.input if rate.cached_input is None else rate.cached_input,
                )
                != (
                    profile.prices.input,
                    profile.prices.output,
                    profile.prices.cached_input,
                )
            ):
                raise ValueError
        return _validate(path, evaluation_id, dataset, plan, runtime_hash)
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError, OverflowError):
        raise PolicyRejected(
            "Evidência insuficiente, inválida ou obsoleta para promoção."
        ) from None


def _validate(
    path: Path,
    evaluation_id: UUID,
    dataset: RoutingDataset,
    plan: BenchmarkPlan,
    runtime_hash: str,
) -> str:
    promotion = plan.promotion
    if promotion is None or promotion.runtime_hash != runtime_hash:
        raise ValueError
    limits = promotion.limits
    cases = dataset.select("test")
    if (
        plan.split != "test"
        or plan.mode != "openai"
        or plan.dataset_checksum != dataset.checksum
        or len(cases) < limits.min_cases
        or {c.category for c in cases} != set(get_args(Category))
    ):
        raise ValueError
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM eval_runs WHERE id=?", (str(evaluation_id),)
        ).fetchone()
        registered = connection.execute(
            "SELECT checksum FROM eval_datasets WHERE id=? AND version=?",
            (dataset.id, dataset.version),
        ).fetchone()
    finally:
        connection.close()
    context = plan.public_context()
    if (
        row is None
        or registered is None
        or registered[0] != dataset.checksum
        or row["status"] != "completed"
        or row["dataset_id"] != dataset.id
        or row["dataset_version"] != dataset.version
        or row["dataset_checksum"] != dataset.checksum
        or row["split"] != "test"
        or row["policy_version"] != plan.candidate_version
        or json.loads(row["context_json"]) != context
        or row["context_hash"] != digest((dataset.checksum, plan.checksum, context))
        or digest(row["metrics_json"]) != row["evidence_checksum"]
    ):
        raise ValueError
    report = json.loads(row["metrics_json"])
    if (
        report["id"] != str(evaluation_id)
        or report["dataset_checksum"] != dataset.checksum
        or report["plan_checksum"] != plan.checksum
        or report["context"] != context
        or report["mode"] != "openai"
        or report["split"] != "test"
        or report["candidate"]["policy_version"] != plan.candidate_version
        or report["baseline"]["policy_version"] != "single-model-v1"
    ):
        raise ValueError
    for when in (row["created_at"], report["evaluated_at"]):
        stamp = datetime.fromisoformat(when)
        if (
            stamp.utcoffset() is None
            or not 0
            <= (datetime.now(UTC) - stamp).total_seconds()
            <= limits.max_age_seconds
        ):
            raise ValueError
    arms = []
    for name in ("baseline", "candidate"):
        observed = list(
            TypeAdapter(tuple[CaseMetrics, ...]).validate_json(
                json.dumps(report[name]["cases"]), strict=True
            )
        )
        if [r.id for r in observed] != [c.id for c in cases]:
            raise ValueError
        for result, case in zip(observed, cases, strict=True):
            if (
                result.category != case.category
                or result.expected_intent != case.route.intent
            ):
                raise ValueError
            if (
                not math.isfinite(result.seconds)
                or result.seconds < 0
                or result.seconds > plan.timeout_seconds
            ):
                raise ValueError
            default = (
                plan.baseline_profile if name == "baseline" else plan.candidate_profile
            )
            if (
                result.profile != (case.session_model_profile or default)
                or not result.session_stable
            ):
                raise ValueError
            profile = plan.profile(result.profile)
            if (
                profile.prices is None
                or not result.attempts
                or len(result.attempts) > plan.max_calls_per_case
            ):
                raise ValueError
            for attempt in result.attempts:
                if not math.isfinite(attempt.seconds) or attempt.seconds < 0:
                    raise ValueError
                values = (
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.cached_tokens,
                )
                if any(type(v) is not int or v < 0 for v in values):
                    raise ValueError
                assert (
                    attempt.input_tokens is not None
                    and attempt.output_tokens is not None
                )
                assert attempt.cached_tokens is not None
                if (
                    attempt.cached_tokens > attempt.input_tokens
                    or attempt.output_tokens > plan.max_output_tokens
                ):
                    raise ValueError
                if attempt.error == "injected_provider_error":
                    scenario = next(s for s in plan.scenarios if s.case_id == case.id)
                    if (
                        scenario.fault != "provider_error"
                        or any(values)
                        or attempt.cost_usd != 0
                    ):
                        raise ValueError
                    continue
                if (
                    attempt.error is not None
                    or attempt.profile != result.profile
                    or attempt.model_checksum != digest(profile.model)
                ):
                    raise ValueError
                rates = profile.prices
                expected_cost = cost_usd(
                    profile.model,
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.cached_tokens,
                    {
                        profile.model: Price(
                            rates.input, rates.output, rates.cached_input
                        )
                    },
                )
                if attempt.cost_usd != expected_cost:
                    raise ValueError
            if (
                sum(
                    (a.input_tokens or 0) + (a.output_tokens or 0)
                    for a in result.attempts
                )
                > plan.max_case_tokens
            ):
                raise ValueError
            if (
                not result.safe
                or "budget" in result.reasons
                or "execution_error" in result.reasons
            ):
                raise ValueError
            if name == "candidate" and (
                not result.passed
                or result.reasons
                or result.state != case.outcome.state
                or set(case.outcome.required_tools) - set(result.called_tools)
                or set(case.outcome.forbidden_tools) & set(result.called_tools)
            ):
                raise ValueError
        metrics = aggregate(observed)
        if metrics != report[name]["metrics"]:
            raise ValueError
        arms.append((observed, metrics))
    (before, baseline), (after, candidate) = arms
    if any(a.passed and not b.passed for a, b in zip(before, after, strict=True)):
        raise ValueError
    if report["quality_regressions"]:
        raise ValueError
    # Estes campos são calculados acima; não aceitar tipos frouxos de JSON.
    base_cost = _number(baseline["cost_usd"])
    cand_cost = _number(candidate["cost_usd"])
    if base_cost <= 0 or candidate["quality_rate"] != limits.min_quality:
        raise ValueError
    if _number(candidate["routing_accuracy"]) < max(
        limits.min_routing_accuracy, _number(baseline["routing_accuracy"])
    ):
        raise ValueError
    for key, ratio in (
        ("cost_usd", limits.max_cost_ratio),
        ("seconds", limits.max_latency_ratio),
    ):
        if _number(candidate[key]) > _number(baseline[key]) * ratio:
            raise ValueError
    for key in ("input_tokens", "output_tokens"):
        if _number(candidate[key]) > _number(baseline[key]) * limits.max_tokens_ratio:
            raise ValueError
    if (
        base_cost + cand_cost > (plan.max_total_cost_usd or 0)
        or sum(
            _number(m[k]) for _, m in arms for k in ("input_tokens", "output_tokens")
        )
        > plan.max_total_tokens
    ):
        raise ValueError
    return row["evidence_checksum"]  # type: ignore[no-any-return]


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    if not math.isfinite(value) or value < 0:
        raise ValueError
    return float(value)
