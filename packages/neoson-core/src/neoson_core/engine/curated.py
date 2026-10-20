"""Porta da memória curada (§9.1, D10): fatos com origem, usados no contexto."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

Namespace = Literal["user", "neoson"]


@dataclass(frozen=True, slots=True)
class MemoryFact:
    id: int
    namespace: Namespace
    content: str
    created_at: datetime
    source_session_id: UUID | None = None
    source_run_id: UUID | None = None


class CuratedMemory(Protocol):
    async def save(
        self,
        namespace: Namespace,
        content: str,
        session_id: UUID | None,
        run_id: UUID | None,
    ) -> MemoryFact: ...

    async def forget(self, fact_id: int) -> bool:
        """`False` se o fato não existe ou já foi esquecido."""
        ...

    async def active(self, limit: int) -> Sequence[MemoryFact]:
        """Fatos ativos, mais recentes primeiro."""
        ...
