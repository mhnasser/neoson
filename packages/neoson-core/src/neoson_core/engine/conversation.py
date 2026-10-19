from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


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


class Engine(Protocol):
    """Porta de entrada do motor: uma mensagem do usuário gera uma execução."""

    async def run(self, message: UserMessage) -> RunResult: ...
