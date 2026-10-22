from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from neoson_core.engine.llm import Usage

EngineActivity = Literal["thinking", "using_tool"]
ActivityObserver = Callable[[EngineActivity], None]


@dataclass(frozen=True, slots=True)
class UserMessage:
    text: str
    session_id: UUID | None = None
    channel: str = "http"
    principal: str | None = None
    on_activity: ActivityObserver | None = field(
        default=None, repr=False, compare=False
    )


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
    session_id: UUID | None = None


class Engine(Protocol):
    """Porta de entrada do motor: uma mensagem do usuário gera uma execução."""

    async def run(self, message: UserMessage) -> RunResult: ...
