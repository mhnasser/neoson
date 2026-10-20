"""Porta de armazenamento da conversa e eventos duráveis (§10, ADR-002)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from neoson_core.engine._validation import frozen_mapping, require_text
from neoson_core.engine.conversation import RunStatus
from neoson_core.engine.llm import Usage
from neoson_core.engine.messages import Message
from neoson_core.engine.skills import SkillRef


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """Evento durável (§10.1), gravado na mesma transação da mudança que o gerou."""

    type: str
    payload: Mapping[str, object] = field(default_factory=dict, hash=False)
    run_id: UUID | None = None
    session_id: UUID | None = None
    correlation_id: UUID | None = None
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_text(self.type, "OutboxEvent.type")
        object.__setattr__(self, "payload", frozen_mapping(self.payload))
        if self.occurred_at.utcoffset() is None:
            raise ValueError("OutboxEvent.occurred_at precisa ter fuso horário.")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Resultado de uma execução e o que ela acrescentou à conversa."""

    run_id: UUID
    session_id: UUID
    status: RunStatus
    started_at: datetime
    ended_at: datetime
    messages: tuple[Message, ...]
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    route_version: str | None = None
    events: tuple[OutboxEvent, ...] = ()
    skills: tuple[SkillRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "skills", tuple(self.skills))


class SessionNotFound(LookupError):
    """A sessão informada não existe."""


class ConversationStore(Protocol):
    async def create_session(self, channel: str) -> UUID: ...

    async def session_exists(self, session_id: UUID) -> bool: ...

    async def load_history(
        self, session_id: UUID, max_messages: int
    ) -> tuple[Message, ...]:
        """Últimas mensagens da sessão, começando sempre numa mensagem do usuário."""
        ...

    async def record_run(self, record: RunRecord) -> None:
        """Grava run, mensagens e eventos numa só transação: tudo ou nada."""
        ...
