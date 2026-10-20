"""Identidade, limites e estados de uma execução (§5.1, §5.2)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class Budget:
    """Limites de uma execução. `None` em tokens ou custo: sem limite próprio."""

    max_steps: int
    timeout_seconds: float
    max_tokens: int | None = None
    max_cost_usd: float | None = None

    def __post_init__(self) -> None:
        if not _is_int(self.max_steps) or self.max_steps < 1:
            raise ValueError("Budget.max_steps deve ser um inteiro >= 1.")
        if not (isfinite(self.timeout_seconds) and self.timeout_seconds > 0):
            raise ValueError("Budget.timeout_seconds deve ser > 0.")
        if self.max_tokens is not None and (
            not _is_int(self.max_tokens) or self.max_tokens < 1
        ):
            raise ValueError("Budget.max_tokens deve ser um inteiro >= 1.")
        if self.max_cost_usd is not None and not (
            isfinite(self.max_cost_usd) and self.max_cost_usd >= 0
        ):
            raise ValueError("Budget.max_cost_usd deve ser >= 0.")


@dataclass(frozen=True, slots=True)
class RunContext:
    """Uma execução por pedido, sempre dentro de uma sessão."""

    session_id: UUID
    budget: Budget
    run_id: UUID = field(default_factory=uuid4)
    trace_id: UUID = field(default_factory=uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.started_at.utcoffset() is None:
            raise ValueError("RunContext.started_at precisa ter fuso horário.")

    @property
    def deadline(self) -> datetime:
        return self.started_at + timedelta(seconds=self.budget.timeout_seconds)

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.deadline


class RunState(StrEnum):
    RECEIVED = "received"
    ROUTING = "routing"
    CONTEXT_READY = "context_ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


class InvalidTransition(ValueError):
    """Transição de estado não prevista."""


_TERMINAL = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})

# Caminho normal do D05. Além dele, qualquer estado não final pode ir para
# `failed` (erro) ou `cancelled` (cancelamento, prazo ou aprovação negada): o
# diagrama é simplificado e só mostra essas saídas a partir de `running`.
_NEXT: Mapping[RunState, frozenset[RunState]] = {
    RunState.RECEIVED: frozenset({RunState.ROUTING}),
    RunState.ROUTING: frozenset({RunState.CONTEXT_READY}),
    RunState.CONTEXT_READY: frozenset({RunState.RUNNING}),
    RunState.RUNNING: frozenset({RunState.WAITING_APPROVAL, RunState.COMPLETED}),
    RunState.WAITING_APPROVAL: frozenset({RunState.RUNNING}),
}


def allowed_transitions(state: RunState) -> frozenset[RunState]:
    if state.is_terminal:
        return frozenset()
    return _NEXT[state] | {RunState.FAILED, RunState.CANCELLED}


def transition(current: RunState, target: RunState) -> RunState:
    """Valida e devolve o novo estado; `InvalidTransition` se não for permitido."""
    if target not in allowed_transitions(current):
        raise InvalidTransition(f"Transição não permitida: {current} -> {target}.")
    return target


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
