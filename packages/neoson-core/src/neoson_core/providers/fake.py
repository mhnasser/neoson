"""Provedor roteirizado e determinístico, para testar o motor sem rede."""

from collections.abc import Iterable, Mapping

from neoson_core.engine import (
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    Usage,
)

FAKE_MODEL = "fake-model"


def reply(text: str, usage: Usage | None = None) -> LLMResponse:
    """Resposta final de texto."""
    return LLMResponse(
        message=Message(role=Role.ASSISTANT, content=text),
        usage=usage or Usage(input_tokens=10, output_tokens=5),
        model=FAKE_MODEL,
    )


def call_tool(
    name: str,
    arguments: Mapping[str, object] | None = None,
    call_id: str = "call-1",
    usage: Usage | None = None,
) -> LLMResponse:
    """Resposta que pede uma ferramenta."""
    return LLMResponse(
        message=Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments or {}),),
        ),
        usage=usage or Usage(input_tokens=10, output_tokens=5),
        model=FAKE_MODEL,
    )


class ScriptedProvider:
    """Devolve as respostas (ou levanta os erros) na ordem dada e guarda os pedidos."""

    def __init__(self, script: Iterable[LLMResponse | LLMError]) -> None:
        self._script = list(script)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("ScriptedProvider: roteiro esgotado.")
        step = self._script.pop(0)
        if isinstance(step, LLMError):
            raise step
        return step
