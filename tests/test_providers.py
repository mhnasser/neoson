import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

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
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.providers.openai_provider import OpenAIProvider

CALENDAR = ToolSpec(
    name="calendar_list",
    description="Lista os compromissos de um dia.",
    parameters={
        "type": "object",
        "properties": {"day": {"type": "string"}},
        "required": ["day"],
    },
)


def request(*messages: Message, tools: tuple[ToolSpec, ...] = ()) -> LLMRequest:
    return LLMRequest(
        model_profile="balanced",
        messages=messages or (Message(role=Role.USER, content="oi"),),
        tools=tools,
    )


# --- tipos da porta ---


def test_usage_adds_up_across_steps() -> None:
    total = Usage(10, 5, 2) + Usage(1, 1, 0)

    assert (total.input_tokens, total.output_tokens, total.cached_tokens) == (11, 6, 2)
    assert total.total_tokens == 17


def test_request_and_response_are_validated() -> None:
    with pytest.raises(ValueError):
        LLMRequest(model_profile="balanced", messages=())
    with pytest.raises(ValueError):
        LLMRequest(model_profile=" ", messages=(Message(role=Role.USER, content="a"),))
    with pytest.raises(ValueError):
        LLMResponse(
            message=Message(role=Role.USER, content="a"), usage=Usage(), model="m"
        )
    with pytest.raises(ValueError):
        ToolSpec(name="t", description="", parameters={})


def test_tool_spec_parameters_are_read_only() -> None:
    with pytest.raises(TypeError):
        CALENDAR.parameters["type"] = "array"  # type: ignore[index]


# --- provedor roteirizado ---


def test_scripted_provider_follows_the_script_and_records_requests() -> None:
    error = LLMError("limite", retryable=True)
    provider = ScriptedProvider([call_tool("calendar_list", {"day": "x"}), error])

    first = asyncio.run(provider.complete(request()))
    with pytest.raises(LLMError) as caught:
        asyncio.run(provider.complete(request()))
    with pytest.raises(AssertionError):
        asyncio.run(provider.complete(request()))

    assert first.message.tool_calls[0].name == "calendar_list"
    assert caught.value is error
    assert len(provider.requests) == 3
    assert reply("ok").message.content == "ok"


# --- adaptador OpenAI com transporte simulado ---

Handler = Callable[[httpx2.Request], httpx2.Response]


def openai_provider(handler: Handler, **kwargs: Any) -> OpenAIProvider:
    client = AsyncOpenAI(
        api_key="chave-de-teste",
        base_url="https://api.test/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    return OpenAIProvider(client, models={"balanced": "modelo-x"}, **kwargs)


def response_body(
    output: list[dict[str, Any]], status: str = "completed"
) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": "modelo-x-2026",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 30,
            "output_tokens": 7,
            "total_tokens": 37,
            "input_tokens_details": {"cached_tokens": 12},
            "output_tokens_details": {"reasoning_tokens": 3},
        },
    }


TEXT_OUTPUT: list[dict[str, Any]] = [
    {"type": "reasoning", "id": "rs_1", "summary": []},
    {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "Olá!", "annotations": []}],
    },
]


class Recorder:
    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body or response_body(TEXT_OUTPUT)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, http_request: httpx2.Request) -> httpx2.Response:
        self.sent.append(json.loads(http_request.content))
        return httpx2.Response(self.status, json=self.body)


def test_text_response_with_usage_and_private_storage() -> None:
    recorder = Recorder()

    response = asyncio.run(openai_provider(recorder).complete(request()))

    assert response.message == Message(role=Role.ASSISTANT, content="Olá!")
    assert response.usage == Usage(input_tokens=30, output_tokens=7, cached_tokens=12)
    assert response.model == "modelo-x-2026"
    sent = recorder.sent[0]
    assert sent["model"] == "modelo-x"
    assert sent["store"] is False
    assert sent["input"] == [{"role": "user", "content": "oi"}]
    assert "tools" not in sent


def test_tool_call_response_becomes_a_tool_call() -> None:
    output = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_abc",
            "name": "calendar_list",
            "arguments": '{"day": "2026-10-21"}',
            "status": "completed",
        }
    ]
    recorder = Recorder(body=response_body(output))

    response = asyncio.run(
        openai_provider(recorder).complete(request(tools=(CALENDAR,)))
    )

    assert response.message.tool_calls == (
        ToolCall(id="call_abc", name="calendar_list", arguments={"day": "2026-10-21"}),
    )
    assert recorder.sent[0]["tools"] == [
        {
            "type": "function",
            "name": "calendar_list",
            "description": "Lista os compromissos de um dia.",
            "parameters": dict(CALENDAR.parameters),
            "strict": False,
        }
    ]


def test_history_with_tool_round_trip_is_translated() -> None:
    recorder = Recorder()
    call = ToolCall(id="call_abc", name="calendar_list", arguments={"day": "amanhã"})
    history = (
        Message(role=Role.SYSTEM, content="Você é o NeoSon."),
        Message(role=Role.USER, content="Agenda de amanhã?"),
        Message(role=Role.ASSISTANT, content="", tool_calls=(call,)),
        Message(role=Role.TOOL, content="2 eventos", tool_call_id="call_abc"),
    )

    asyncio.run(openai_provider(recorder).complete(request(*history)))

    assert recorder.sent[0]["input"] == [
        {"role": "system", "content": "Você é o NeoSon."},
        {"role": "user", "content": "Agenda de amanhã?"},
        {
            "type": "function_call",
            "call_id": "call_abc",
            "name": "calendar_list",
            "arguments": '{"day": "amanhã"}',
        },
        {"type": "function_call_output", "call_id": "call_abc", "output": "2 eventos"},
    ]


def test_reasoning_effort_is_sent_when_configured() -> None:
    recorder = Recorder()

    asyncio.run(openai_provider(recorder, reasoning_effort="low").complete(request()))

    assert recorder.sent[0]["reasoning"] == {"effort": "low"}


def test_unknown_profile_fails_without_calling_the_api() -> None:
    recorder = Recorder()
    unknown = LLMRequest(
        model_profile="premium", messages=(Message(role=Role.USER, content="oi"),)
    )

    with pytest.raises(LLMError) as caught:
        asyncio.run(openai_provider(recorder).complete(unknown))

    assert not caught.value.retryable
    assert recorder.sent == []


@pytest.mark.parametrize(
    ("status", "retryable"), [(429, True), (500, True), (401, False), (400, False)]
)
def test_api_errors_are_mapped(status: int, retryable: bool) -> None:
    recorder = Recorder(status=status, body={"error": {"message": "falha"}})

    with pytest.raises(LLMError) as caught:
        asyncio.run(openai_provider(recorder).complete(request()))

    assert caught.value.retryable is retryable
    assert "chave-de-teste" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_connection_errors_are_retryable() -> None:
    def refuse(http_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("recusada", request=http_request)

    with pytest.raises(LLMError) as caught:
        asyncio.run(openai_provider(refuse).complete(request()))

    assert caught.value.retryable


def test_invalid_tool_arguments_are_rejected() -> None:
    output = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "c",
            "name": "calendar_list",
            "arguments": "{nao e json",
        }
    ]

    with pytest.raises(LLMError) as caught:
        asyncio.run(
            openai_provider(Recorder(body=response_body(output))).complete(request())
        )

    assert caught.value.retryable


def test_empty_incomplete_response_is_a_retryable_error() -> None:
    body = response_body(
        [{"type": "reasoning", "id": "rs_1", "summary": []}], status="incomplete"
    )

    with pytest.raises(LLMError) as caught:
        asyncio.run(openai_provider(Recorder(body=body)).complete(request()))

    assert caught.value.retryable
