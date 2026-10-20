"""Decisão do roteador (§6). É uma recomendação: não concede permissões."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal, Protocol, get_args
from uuid import UUID

from neoson_core.engine._validation import require_text

Complexity = Literal["low", "medium", "high"]
ExecutionMode = Literal["direct", "retrieve", "delegate"]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    intent: str
    complexity: Complexity
    execution_mode: ExecutionMode
    primary_model_profile: str
    confidence: float
    policy_version: str
    rationale_code: str  # código explicável, sem chain-of-thought

    def __post_init__(self) -> None:
        for name in (
            "intent",
            "primary_model_profile",
            "policy_version",
            "rationale_code",
        ):
            require_text(getattr(self, name), f"RouteDecision.{name}")
        if self.complexity not in get_args(Complexity):
            raise ValueError(f"RouteDecision.complexity: use {get_args(Complexity)}.")
        if self.execution_mode not in get_args(ExecutionMode):
            raise ValueError(
                f"RouteDecision.execution_mode: use {get_args(ExecutionMode)}."
            )
        if not (isfinite(self.confidence) and 0.0 <= self.confidence <= 1.0):
            raise ValueError("RouteDecision.confidence deve estar entre 0 e 1.")


@dataclass(frozen=True, slots=True)
class RouteRequest:
    text: str
    session_id: UUID | None = None


class Router(Protocol):
    async def decide(self, request: RouteRequest) -> RouteDecision: ...
