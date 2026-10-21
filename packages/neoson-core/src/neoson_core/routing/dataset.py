"""Dataset público do laboratório; nunca carregado pelo roteador de produção."""

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from neoson_core.engine.route import Complexity, ExecutionMode

Split = Literal["train", "validation", "test"]
Category = Literal[
    "conversation",
    "tools",
    "memory",
    "complex",
    "ambiguity",
    "failure",
    "permission",
    "adversarial",
]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
Text = Annotated[str, Field(min_length=1, max_length=8000)]
MAX_BYTES = 2_000_000


class DatasetInvalid(ValueError):
    """Erro sanitizado, sem reproduzir caminhos ou conteúdo do dataset."""


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RouteLabels(_Contract):
    intent: Identifier
    complexity: Complexity
    execution_mode: ExecutionMode


class OutcomeLabels(_Contract):
    """Expectativas observáveis, separadas da classificação do roteador."""

    state: Literal["completed", "waiting_approval", "failed"]
    contains: tuple[Text, ...] = Field(default=(), max_length=32)
    not_contains: tuple[Text, ...] = Field(default=(), max_length=32)
    required_tools: tuple[Identifier, ...] = Field(default=(), max_length=32)
    forbidden_tools: tuple[Identifier, ...] = Field(default=(), max_length=32)
    approval: Literal["none", "required", "denied"]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for values in (
            self.contains,
            self.not_contains,
            self.required_tools,
            self.forbidden_tools,
        ):
            normalized = [_normalize(v) for v in values]
            if any(not v for v in normalized) or len(set(normalized)) != len(values):
                raise ValueError("Expectativas vazias ou repetidas.")
        if not (
            self.contains
            or self.not_contains
            or self.required_tools
            or self.forbidden_tools
        ):
            raise ValueError("Defina um critério observável além do estado.")
        if set(self.required_tools) & set(self.forbidden_tools):
            raise ValueError("Ferramenta simultaneamente exigida e proibida.")
        # Uma expressão exigida não pode conter outra explicitamente proibida.
        if any(
            _normalize(no) in _normalize(yes)
            for yes in self.contains
            for no in self.not_contains
        ):
            raise ValueError("Expectativas de resposta contraditórias.")
        if (self.state == "waiting_approval") != (self.approval == "required"):
            raise ValueError("Estado e expectativa de aprovação incompatíveis.")
        return self


class RoutingCase(_Contract):
    id: Identifier
    split: Split
    category: Category
    text: Text
    session_model_profile: Identifier | None = None
    route: RouteLabels
    outcome: OutcomeLabels

    @field_validator("text")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not _normalize(value):
            raise ValueError("Entrada vazia.")
        return value


class RoutingDataset(_Contract):
    schema_version: Literal[1]
    id: Identifier
    version: Annotated[
        str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    ]
    cases: tuple[RoutingCase, ...] = Field(min_length=3, max_length=256)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Versão de schema deve ser um inteiro.")
        return value

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        ids = [c.id for c in self.cases]
        texts = [_normalize(c.text) for c in self.cases]
        if len(set(ids)) != len(ids) or len(set(texts)) != len(texts):
            raise ValueError("ID ou entrada repetida, inclusive entre splits.")
        if {c.split for c in self.cases} != {"train", "validation", "test"}:
            raise ValueError("Todos os splits precisam de casos.")
        return self

    def select(self, split: Split) -> tuple[RoutingCase, ...]:
        if split not in ("train", "validation", "test"):
            raise ValueError("Split inválido.")
        return tuple(
            sorted((c for c in self.cases if c.split == split), key=lambda c: c.id)
        )

    @property
    def checksum(self) -> str:
        data = self.model_dump(mode="json")
        data["cases"] = sorted(data["cases"], key=lambda c: c["id"])
        canonical = json.dumps(
            data,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Chave JSON repetida.")
        result[key] = value
    return result


def load_dataset(path: Path) -> RoutingDataset:
    """Valida JSON limitado; ordenação e formatação não alteram o checksum."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Dataset excede o limite.")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
        # JSON mode aceita arrays como tuplas imutáveis, mantendo validação estrita.
        return RoutingDataset.model_validate_json(json.dumps(data, allow_nan=False))
    except (OSError, UnicodeError, ValueError, ValidationError, RecursionError):
        raise DatasetInvalid("Dataset de roteamento inválido ou ilegível.") from None
