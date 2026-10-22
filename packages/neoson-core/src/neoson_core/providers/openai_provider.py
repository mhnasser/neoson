"""Adaptador da OpenAI (Responses API).

Usa `store=False`: o histórico fica no NeoSon, não no fornecedor (§2, §9).
"""

import json
from collections.abc import Mapping
from typing import Any, cast

import openai
from openai import AsyncOpenAI
from openai.types.responses import ResponseInputParam

from neoson_core.engine import (
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)

_RETRYABLE = (
    openai.APIConnectionError,  # inclui APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
)


class OpenAIProvider:
    def __init__(
        self,
        client: AsyncOpenAI,
        models: Mapping[str, str],
        reasoning_effort: str | None = None,
    ) -> None:
        """`models` traduz perfis do roteador (ex.: `balanced`) em modelos da OpenAI."""
        self._client = client
        self._models = dict(models)
        self._reasoning_effort = reasoning_effort

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        models: Mapping[str, str],
        timeout_seconds: float,
        reasoning_effort: str | None = None,
        max_retries: int = 2,
    ) -> "OpenAIProvider":
        """Cria o cliente do SDK aqui, para que só `providers` conheça o fornecedor."""
        client = AsyncOpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=max_retries
        )
        return cls(client, models, reasoning_effort)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = self._models.get(request.model_profile)
        if model is None:
            raise LLMError(
                f"Perfil de modelo sem modelo configurado: {request.model_profile}.",
                retryable=False,
                usage=Usage(),  # nada foi enviado ao fornecedor
            )
        options: dict[str, Any] = {}
        if request.tools:
            options["tools"] = [_tool(spec) for spec in request.tools]
        if request.max_output_tokens is not None:
            options["max_output_tokens"] = request.max_output_tokens
        if self._reasoning_effort is not None:
            options["reasoning"] = {"effort": self._reasoning_effort}

        try:
            response = await self._client.responses.create(
                model=model,
                input=cast(
                    ResponseInputParam,
                    [item for m in request.messages for item in _input_items(m)],
                ),
                store=False,
                **options,
            )
        except openai.APIError as error:
            raise LLMError(
                f"OpenAI: {type(error).__name__}.",
                retryable=isinstance(error, _RETRYABLE),
            ) from None

        usage = _usage(response.usage)
        try:
            message = _assistant_message(response.output, response.status)
        except LLMError as error:
            # A resposta chegou: o consumo medido entra na conta mesmo sem conteúdo.
            raise LLMError(
                str(error),
                retryable=error.retryable,
                usage=usage if response.usage is not None else None,
                model=response.model,
            ) from None
        return LLMResponse(message=message, usage=usage, model=response.model)


def _tool(spec: ToolSpec) -> dict[str, Any]:
    # `strict=False`: quem valida os argumentos é o Tool Registry, não o fornecedor.
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": dict(spec.parameters),
        "strict": False,
    }


def _input_items(message: Message) -> list[dict[str, Any]]:
    if message.role is Role.TOOL:
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": message.content,
            }
        ]
    items: list[dict[str, Any]] = []
    if message.content:
        items.append({"role": message.role.value, "content": message.content})
    for call in message.tool_calls:
        items.append(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
            }
        )
    return items


def _assistant_message(output: list[Any], status: str | None) -> Message:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for item in output:
        if item.type == "message":
            texts.extend(
                part.text for part in item.content if part.type == "output_text"
            )
        elif item.type == "function_call":
            calls.append(
                ToolCall(id=item.call_id, name=item.name, arguments=_arguments(item))
            )
    text = "".join(texts)
    if not text and not calls:
        raise LLMError(
            f"OpenAI: resposta sem texto nem pedido de ferramenta (status {status}).",
            retryable=status != "completed",
        )
    return Message(role=Role.ASSISTANT, content=text, tool_calls=tuple(calls))


def _arguments(item: Any) -> Mapping[str, object]:
    try:
        arguments = json.loads(item.arguments or "{}")
    except json.JSONDecodeError:
        raise LLMError(
            f"OpenAI: argumentos inválidos para a ferramenta {item.name}.",
            retryable=True,
        ) from None
    if not isinstance(arguments, dict):
        raise LLMError(
            f"OpenAI: argumentos da ferramenta {item.name} não são um objeto.",
            retryable=True,
        )
    return arguments


def _usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    details = getattr(usage, "input_tokens_details", None)
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=getattr(details, "cached_tokens", 0) or 0,
    )
