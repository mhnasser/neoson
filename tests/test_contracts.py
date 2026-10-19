from uuid import uuid4

from neoson_contracts.api.v1.errors import ErrorResponse
from neoson_contracts.api.v1.messages import MessageRequest, MessageResponse


def test_request_serializes_with_protocol_version() -> None:
    payload = MessageRequest(text="oi").model_dump(mode="json")

    assert payload == {"protocol_version": 1, "session_id": None, "text": "oi"}


def test_client_tolerates_fields_added_by_a_newer_gateway() -> None:
    """Mudança compatível (ADR-005): um campo opcional novo não quebra o cliente."""
    run_id = uuid4()
    payload = {
        "protocol_version": 1,
        "run_id": str(run_id),
        "text": "oi",
        "status": "completed",
        "campo_novo": {"qualquer": "coisa"},
    }

    response = MessageResponse.model_validate(payload)

    assert response.run_id == run_id


def test_gateway_tolerates_fields_sent_by_a_newer_client() -> None:
    request = MessageRequest.model_validate({"text": "oi", "campo_novo": True})

    assert request.text == "oi"


def test_error_response_details_default_to_empty() -> None:
    error = ErrorResponse(code="invalid_request", message="Requisição inválida.")

    assert error.details == []
