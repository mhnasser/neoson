"""Snapshots de regras são dados limitados, nunca Python ou regex fornecidos."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from neoson_core.engine import RouteDecision, RouteRequest
from neoson_core.engine.route import Complexity
from neoson_core.routing.dataset import Identifier, _Contract
from neoson_core.routing.rules import RulesRouter, _normalize

Checksum = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class KeywordRule(_Contract):
    intent: Identifier
    keywords: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = Field(
        min_length=1, max_length=16
    )

    @model_validator(mode="after")
    def _words(self) -> Self:
        if any(not k.strip() or not k.isprintable() for k in self.keywords):
            raise ValueError("Palavra de regra inválida.")
        return self


class PolicySnapshot(_Contract):
    version: Annotated[str, Field(pattern=r"^rules-v[1-9][0-9]*$")]
    rules: tuple[KeywordRule, ...] = Field(default=(), max_length=32)
    long_request_chars: int = Field(default=600, ge=32, le=8000)
    short_complexity: Complexity = "low"
    long_complexity: Complexity = "medium"
    default_intent: Identifier = "conversation"


BUILTIN = PolicySnapshot(version="rules-v1")


class SnapshotRouter:
    def __init__(self, snapshot: PolicySnapshot, profile: str) -> None:
        self.snapshot, self.profile = snapshot, profile

    async def decide(self, request: RouteRequest) -> RouteDecision:
        import re

        original = await RulesRouter(self.profile).decide(request)
        intent = original.intent
        matched = False
        text = _normalize(request.text)
        for rule in self.snapshot.rules:
            if any(
                re.search(r"(?<!\w)" + re.escape(_normalize(k)) + r"(?!\w)", text)
                for k in rule.keywords
            ):
                intent = rule.intent
                matched = True
                break
        if not matched and intent == "conversation":
            intent = self.snapshot.default_intent
        return RouteDecision(
            intent,
            self.snapshot.long_complexity
            if len(request.text) > self.snapshot.long_request_chars
            else self.snapshot.short_complexity,
            "direct",
            self.profile,
            original.confidence,
            self.snapshot.version,
            "snapshot:rules",
        )


class GateLimits(_Contract):
    min_cases: int = Field(ge=8, le=256)
    min_quality: Annotated[float, Field(ge=1, le=1)]
    min_routing_accuracy: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    max_cost_ratio: Annotated[float, Field(gt=0, le=2, allow_inf_nan=False)]
    max_tokens_ratio: Annotated[float, Field(gt=0, le=2, allow_inf_nan=False)]
    max_latency_ratio: Annotated[float, Field(gt=0, le=5, allow_inf_nan=False)]
    max_age_seconds: int = Field(ge=60, le=604800)


class PromotionContext(_Contract):
    snapshot: PolicySnapshot
    base_revision: Checksum
    runtime_hash: Checksum
    limits: GateLimits


class ModelBinding(_Contract):
    profile: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")]
    model: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,95}$")]
    reasoning_effort: Annotated[str, Field(pattern=r"^[a-z]{1,16}$")] | None = None
