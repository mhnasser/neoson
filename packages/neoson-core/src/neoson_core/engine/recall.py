"""Porta de busca em conversas anteriores (D10). A implementação fica em `memory`."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from neoson_core.engine.messages import Role


@dataclass(frozen=True, slots=True)
class HistoryHit:
    """Trecho de uma mensagem antiga, com a origem para citar."""

    session_id: UUID
    role: Role
    created_at: datetime
    snippet: str


class HistorySearch(Protocol):
    async def search(
        self, query: str, limit: int, exclude_session: UUID | None = None
    ) -> Sequence[HistoryHit]: ...
