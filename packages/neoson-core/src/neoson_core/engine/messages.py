"""Mensagens, pedidos de ferramenta e resultados trocados com a LLM (§5.1, §11.1)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from neoson_core.engine._validation import frozen_mapping, require_text


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Pedido da LLM para executar uma ferramenta. Quem autoriza é a Policy Engine."""

    id: str
    name: str
    arguments: Mapping[str, object] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        require_text(self.id, "ToolCall.id")
        require_text(self.name, "ToolCall.name")
        object.__setattr__(self, "arguments", frozen_mapping(self.arguments))


class ToolResultStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    REQUIRES_APPROVAL = "requires_approval"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Resultado de uma ferramenta: dados estruturados e um resumo seguro para a LLM."""

    call_id: str
    status: ToolResultStatus
    summary: str
    data: Mapping[str, object] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        require_text(self.call_id, "ToolResult.call_id")
        object.__setattr__(self, "status", ToolResultStatus(self.status))
        object.__setattr__(self, "data", frozen_mapping(self.data))


@dataclass(frozen=True, slots=True)
class Message:
    """Uma mensagem da conversa com a LLM.

    Só o assistente pede ferramentas (`tool_calls`), e só mensagens de ferramenta
    indicam a qual pedido respondem (`tool_call_id`).
    """

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        role = Role(self.role)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if self.tool_calls and role is not Role.ASSISTANT:
            raise ValueError("Só mensagens do assistente pedem ferramentas.")
        if role is Role.TOOL:
            require_text(self.tool_call_id, "Message.tool_call_id")
        elif self.tool_call_id is not None:
            raise ValueError("Só mensagens de ferramenta indicam tool_call_id.")
        if not self.content and not self.tool_calls:
            raise ValueError("Mensagem sem conteúdo nem pedido de ferramenta.")
