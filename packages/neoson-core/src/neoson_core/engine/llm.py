"""Porta para modelos de linguagem (§11.1). O motor não conhece o fornecedor."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from neoson_core.engine._validation import frozen_mapping, require_text
from neoson_core.engine.messages import Message, Role


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Ferramenta oferecida ao modelo. `parameters` é o JSON Schema dos argumentos."""

    name: str
    description: str
    parameters: Mapping[str, object] = field(hash=False)

    def __post_init__(self) -> None:
        require_text(self.name, "ToolSpec.name")
        require_text(self.description, "ToolSpec.description")
        object.__setattr__(self, "parameters", frozen_mapping(self.parameters))


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """O perfil (por exemplo, `balanced`) é traduzido para um modelo pelo provedor."""

    model_profile: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        require_text(self.model_profile, "LLMRequest.model_profile")
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        if not self.messages:
            raise ValueError("LLMRequest precisa de pelo menos uma mensagem.")


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def is_valid(self) -> bool:
        """Contagens inteiras e não negativas, com o cache contido na entrada."""
        counts = (self.input_tokens, self.output_tokens, self.cached_tokens)
        return (
            all(type(n) is int and n >= 0 for n in counts)
            and self.cached_tokens <= self.input_tokens
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Resposta do modelo: texto final ou pedidos de ferramenta do assistente."""

    message: Message
    usage: Usage
    model: str

    def __post_init__(self) -> None:
        if self.message.role is not Role.ASSISTANT:
            raise ValueError("LLMResponse.message precisa ser do assistente.")


class LLMError(Exception):
    """Falha ao consultar o modelo. `retryable` indica erro temporário.

    `usage` é o consumo medido pelo fornecedor (`Usage()` quando nada foi enviado);
    `None` significa consumo desconhecido, que não pode ser tratado como zero.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        usage: Usage | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.usage = usage
        self.model = model


class LLMProvider(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...
