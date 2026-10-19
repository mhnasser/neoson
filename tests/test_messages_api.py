from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from neoson_contracts.api.v1.errors import ErrorResponse
from neoson_contracts.api.v1.messages import MessageResponse
from neoson_core.engine import RunResult, RunStatus, UserMessage
from neoson_gateway.app import create_app

URL = "/api/v1/messages"


class FakeEngine:
    def __init__(self, status: RunStatus = RunStatus.COMPLETED) -> None:
        self.status = status
        self.received: list[UserMessage] = []
        self.run_id = uuid4()

    async def run(self, message: UserMessage) -> RunResult:
        self.received.append(message)
        return RunResult(
            run_id=self.run_id,
            text=f"resposta para: {message.text}",
            status=self.status,
        )


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def client(engine: FakeEngine) -> TestClient:
    return TestClient(create_app(engine))


def assert_invalid(response: Any, location: str) -> ErrorResponse:
    assert response.status_code == 422
    error = ErrorResponse.model_validate(response.json())
    assert error.code == "invalid_request"
    assert location in [detail.location for detail in error.details]
    return error


def test_valid_message_is_translated_to_the_engine_and_back(
    client: TestClient, engine: FakeEngine
) -> None:
    session = uuid4()

    response = client.post(
        URL, json={"text": "Olá, NeoSon", "session_id": str(session)}
    )

    assert response.status_code == 200
    body = MessageResponse.model_validate(response.json())
    assert body == MessageResponse(
        run_id=engine.run_id, text="resposta para: Olá, NeoSon", status="completed"
    )
    assert response.json()["protocol_version"] == 1
    assert engine.received == [UserMessage(text="Olá, NeoSon", session_id=session)]


def test_session_id_is_optional(client: TestClient, engine: FakeEngine) -> None:
    response = client.post(URL, json={"protocol_version": 1, "text": "oi"})

    assert response.status_code == 200
    assert engine.received == [UserMessage(text="oi", session_id=None)]


def test_accepts_text_at_the_maximum_length(client: TestClient) -> None:
    assert client.post(URL, json={"text": "a" * 8000}).status_code == 200


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.COMPLETED, "completed"),
        (RunStatus.NEEDS_APPROVAL, "needs_approval"),
        (RunStatus.FAILED, "failed"),
    ],
)
def test_every_run_status_maps_to_the_contract(
    status: RunStatus, expected: str
) -> None:
    client = TestClient(create_app(FakeEngine(status)))

    response = client.post(URL, json={"text": "oi"})

    assert response.json()["status"] == expected


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        ({"text": ""}, "body.text"),
        ({"text": "a" * 8001}, "body.text"),
        ({}, "body.text"),
        ({"text": "oi", "protocol_version": 2}, "body.protocol_version"),
        ({"text": "oi", "session_id": "nao-e-uuid"}, "body.session_id"),
    ],
)
def test_invalid_request_returns_error_contract(
    client: TestClient, engine: FakeEngine, payload: dict[str, Any], location: str
) -> None:
    response = client.post(URL, json=payload)

    assert_invalid(response, location)
    assert engine.received == []


def test_invalid_json_returns_error_contract(client: TestClient) -> None:
    response = client.post(
        URL, content=b"{nao e json", headers={"content-type": "application/json"}
    )

    error = assert_invalid(response, "body.1")
    assert error.message == "Requisição inválida."


def test_error_does_not_echo_the_submitted_content(client: TestClient) -> None:
    secret_text = "conteudo-que-nao-deve-voltar-" + "x" * 8000

    response = client.post(URL, json={"text": secret_text})

    assert_invalid(response, "body.text")
    assert "conteudo-que-nao-deve-voltar" not in response.text


def test_openapi_exposes_the_contract_schemas(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    schemas = spec["components"]["schemas"]
    assert {"MessageRequest", "MessageResponse", "ErrorResponse"} <= set(schemas)
    operation = spec["paths"][URL]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/MessageRequest"
    }
    responses = operation["responses"]
    assert responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/MessageResponse"
    }
    assert responses["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }


def test_run_id_in_response_is_a_uuid(client: TestClient) -> None:
    body = client.post(URL, json={"text": "oi"}).json()

    assert UUID(body["run_id"])


class BrokenEngine:
    async def run(self, message: UserMessage) -> RunResult:
        raise RuntimeError("falha interna com detalhe que não deve vazar")


def test_body_that_is_not_utf8_returns_error_contract(client: TestClient) -> None:
    response = client.post(
        URL, content=b'{"text": "Ol\xe1"}', headers={"content-type": "application/json"}
    )

    assert response.status_code == 400
    error = ErrorResponse.model_validate(response.json())
    assert error.code == "bad_request"


def test_unknown_route_and_wrong_method_return_error_contract(
    client: TestClient,
) -> None:
    not_found = client.get("/api/v1/nao-existe")
    wrong_method = client.get(URL)

    assert not_found.status_code == 404
    assert ErrorResponse.model_validate(not_found.json()).code == "not_found"
    assert wrong_method.status_code == 405
    assert (
        ErrorResponse.model_validate(wrong_method.json()).code == "method_not_allowed"
    )
    assert "POST" in wrong_method.headers["allow"]


def test_engine_failure_returns_internal_error_without_details() -> None:
    client = TestClient(create_app(BrokenEngine()), raise_server_exceptions=False)

    response = client.post(URL, json={"text": "oi"})

    assert response.status_code == 500
    error = ErrorResponse.model_validate(response.json())
    assert (error.code, error.message) == ("internal_error", "Erro interno.")
    assert "não deve vazar" not in response.text
