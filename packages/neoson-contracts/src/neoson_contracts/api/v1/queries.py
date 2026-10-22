"""Consultas da API v1: execução, sessão, prontidão e resumo de métricas (§12)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from neoson_contracts.api.v1.messages import MessageStatus


class RunUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)


class RunResponse(BaseModel):
    protocol_version: Literal[1] = 1
    run_id: UUID
    session_id: UUID
    status: MessageStatus
    text: str | None
    model: str | None
    started_at: datetime
    ended_at: datetime
    usage: RunUsage


class SessionMessage(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    run_id: UUID | None


class SessionResponse(BaseModel):
    """Mensagens mais recentes primeiro; `next_before` pede a página anterior."""

    protocol_version: Literal[1] = 1
    session_id: UUID
    channel: str
    created_at: datetime
    messages: list[SessionMessage]
    next_before: int | None


class ReadyResponse(BaseModel):
    protocol_version: Literal[1] = 1
    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]


class MetricsSummary(BaseModel):
    protocol_version: Literal[1] = 1
    day: date
    runs_total: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    waiting_approval: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cost_usd: float | None
    runs_without_cost: int = Field(ge=0)
    outbox_pending: int = Field(ge=0)
    outbox_dead_letter: int = Field(ge=0)
