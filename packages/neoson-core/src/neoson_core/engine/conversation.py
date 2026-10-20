from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from neoson_core.engine.llm import Usage


@dataclass(frozen=True, slots=True)
class UserMessage:
    text: str
    session_id: UUID | None = None


class RunStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_APPROVAL = "needs_approval"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: UUID
    text: str
    status: RunStatus
    usage: Usage = field(default_factory=Usage)
    model: str | None = None


class Engine(Protocol):
    """Porta de entrada do motor: uma mensagem do usuário gera uma execução."""

    async def run(self, message: UserMessage) -> RunResult: ...
