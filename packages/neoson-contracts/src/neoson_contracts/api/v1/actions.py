"""Consulta e decisão humana de ações propostas; nunca aceita novos argumentos."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool

ActionState = Literal[
    "pending",
    "approved",
    "denied",
    "expired",
    "executing",
    "completed",
    "unknown",
    "invalid",
]


class ActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: StrictBool


class ActionResume(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionResponse(BaseModel):
    id: UUID
    run_id: UUID
    session_id: UUID
    tool: str
    arguments: dict[str, object]
    fingerprint: str
    expires_at: datetime
    state: ActionState
