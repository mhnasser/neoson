"""Grafo de conhecimento (§9.2): entidades com ID estável e fatos com proveniência.

O grafo não substitui o histórico: cada fato aponta para a fonte em que foi
observado. Nomes de entidades podem ser pessoais e ficam só no armazenamento local.
"""

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

MAX_NAME_CHARS = 200
MAX_REASON_CHARS = 200
MAX_QUERY_LIMIT = 100
# `<tipo>:<id>`, por exemplo `memory_fact:12` ou `message:<uuid>`.
SOURCE_ID = re.compile(r"[a-z][a-z_]{0,31}:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class EntityKind(StrEnum):
    PERSON = "Person"
    PROJECT = "Project"
    DOCUMENT = "Document"
    CONCEPT = "Concept"
    DECISION = "Decision"
    TASK = "Task"
    EVENT = "Event"
    SKILL = "Skill"
    SOURCE = "Source"


class Relation(StrEnum):
    WORKS_ON = "WORKS_ON"
    MENTIONS = "MENTIONS"
    DECIDED = "DECIDED"
    DEPENDS_ON = "DEPENDS_ON"
    DERIVED_FROM = "DERIVED_FROM"
    RELATES_TO = "RELATES_TO"
    USES_SKILL = "USES_SKILL"


class FactStatus(StrEnum):
    """`pending` não entra na recuperação; `retracted` é final e não volta."""

    PENDING = "pending"
    ACTIVE = "active"
    RETRACTED = "retracted"


_TRANSITIONS = {
    FactStatus.PENDING: frozenset({FactStatus.ACTIVE, FactStatus.RETRACTED}),
    FactStatus.ACTIVE: frozenset({FactStatus.RETRACTED}),
    FactStatus.RETRACTED: frozenset(),
}


def allowed_status(current: FactStatus, target: FactStatus) -> bool:
    return target in _TRANSITIONS[current]


def name_key(name: str) -> str:
    """Chave de comparação: Unicode NFKC, espaços colapsados e sem caixa."""
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def stable_id(*parts: str) -> str:
    """Hash canônico das partes: o mesmo conteúdo gera sempre o mesmo ID."""
    text = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Entity:
    kind: EntityKind
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EntityKind):
            raise ValueError("Tipo de entidade desconhecido.")
        if not isinstance(self.name, str):
            raise ValueError("Nome de entidade inválido.")
        name = " ".join(unicodedata.normalize("NFKC", self.name).split())
        if not 1 <= len(name) <= MAX_NAME_CHARS:
            raise ValueError("Nome de entidade inválido.")
        object.__setattr__(self, "name", name)

    @property
    def id(self) -> str:
        return stable_id("entity", self.kind.value, name_key(self.name))


@dataclass(frozen=True, slots=True)
class Provenance:
    """Onde e quando o fato foi observado, com a confiança atribuída."""

    source_id: str
    observed_at: datetime
    confidence: float
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not SOURCE_ID.fullmatch(
            self.source_id
        ):
            raise ValueError("Fonte do fato inválida.")
        for moment in (self.observed_at, self.valid_from, self.valid_to):
            if moment is not None and (
                not isinstance(moment, datetime) or moment.utcoffset() is None
            ):
                raise ValueError("Datas do fato precisam ter fuso horário.")
        confidence = self.confidence
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("Confiança do fato deve estar entre 0 e 1.")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("Validade do fato termina antes de começar.")


@dataclass(frozen=True, slots=True)
class Fact:
    """Uma observação de relação. Mesma relação de outra fonte é outro fato."""

    subject: Entity
    relation: Relation
    object: Entity
    provenance: Provenance
    status: FactStatus = FactStatus.PENDING

    def __post_init__(self) -> None:
        if not isinstance(self.relation, Relation):
            raise ValueError("Relação desconhecida.")
        if not isinstance(self.status, FactStatus):
            raise ValueError("Estado de fato desconhecido.")
        if not isinstance(self.subject, Entity) or not isinstance(self.object, Entity):
            raise ValueError("Fato precisa de duas entidades.")
        if self.subject.id == self.object.id:
            raise ValueError("Fato não pode ligar uma entidade a ela mesma.")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("Fato sem proveniência.")

    @property
    def id(self) -> str:
        return stable_id(
            "fact",
            self.subject.id,
            self.relation.value,
            self.object.id,
            self.provenance.source_id,
        )


def check_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"Limite deve ser um inteiro entre 1 e {MAX_QUERY_LIMIT}.")


class GraphStore(Protocol):
    async def upsert(self, facts: Sequence[Fact]) -> int:
        """Grava fatos novos e devolve quantos entraram; IDs repetidos são ignorados,
        inclusive os retirados, que não voltam."""
        ...

    async def set_status(
        self, fact_id: str, status: FactStatus, reason: str | None = None
    ) -> bool:
        """`False` se o fato não existe ou a transição não é permitida. Repetir o
        estado atual não muda nada (nem o motivo) e devolve `True`."""
        ...

    async def facts_about(
        self,
        entity_id: str,
        *,
        limit: int,
        statuses: Collection[FactStatus] = (FactStatus.ACTIVE,),
    ) -> Sequence[Fact]:
        """Fatos em que a entidade é sujeito ou objeto, mais confiáveis primeiro."""
        ...

    async def find_entities(self, text: str, *, limit: int) -> Sequence[Entity]:
        """Entidades cujo nome contém o texto, sem diferenciar caixa."""
        ...
