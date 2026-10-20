"""Porta das skills (§7, §11.1, ADR-007): procedimentos aprovados e versionados."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from neoson_core.engine._validation import require_text


class SkillRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class SkillRef:
    """O que entra no índice do prompt: o suficiente para o modelo escolher."""

    id: str
    version: str
    description: str
    triggers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.id, "SkillRef.id")
        require_text(self.version, "SkillRef.version")
        object.__setattr__(self, "triggers", tuple(self.triggers))


@dataclass(frozen=True, slots=True)
class Skill:
    """Uma versão aprovada: o procedimento e as permissões que ela declara."""

    ref: SkillRef
    instructions: str
    allowed_tools: frozenset[str]
    requires_approval: bool
    risk_level: SkillRisk
    checksum: str

    def __post_init__(self) -> None:
        require_text(self.instructions, "Skill.instructions")
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))


class SkillNotFound(LookupError):
    """Não há versão aprovada e ativa com esse id e versão."""


class SkillIntegrityError(ValueError):
    """A cópia no registro não corresponde à versão aprovada."""


class SkillRegistry(Protocol):
    async def index(self) -> Sequence[SkillRef]:
        """Skills ativas, em ordem de id."""
        ...

    async def load_approved(self, skill_id: str, version: str) -> Skill:
        """Cópia aprovada da versão; `SkillNotFound` se não estiver ativa."""
        ...
