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


def test_query_responses_tolerate_new_fields_and_keep_protocol_version() -> None:
    from neoson_contracts.api.v1.queries import (
        MetricsSummary,
        ReadyResponse,
        RunResponse,
        SessionResponse,
    )

    run = RunResponse.model_validate(
        {
            "run_id": str(uuid4()),
            "session_id": str(uuid4()),
            "status": "completed",
            "text": None,
            "model": None,
            "started_at": "2026-10-22T12:00:00+00:00",
            "ended_at": "2026-10-22T12:00:01+00:00",
            "usage": {"input_tokens": 1, "output_tokens": 2, "cached_tokens": 0},
            "campo_novo": 1,
        }
    )
    assert run.protocol_version == 1 and run.usage.output_tokens == 2
    session = SessionResponse.model_validate(
        {
            "session_id": str(uuid4()),
            "channel": "http",
            "created_at": "2026-10-22T12:00:00+00:00",
            "messages": [],
            "next_before": None,
            "campo_novo": True,
        }
    )
    assert session.messages == []
    assert (
        ReadyResponse(status="ready", checks={}).model_dump()["protocol_version"] == 1
    )
    summary = MetricsSummary.model_validate(
        {
            "day": "2026-10-22",
            "runs_total": 0,
            "completed": 0,
            "failed": 0,
            "waiting_approval": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cost_usd": None,
            "runs_without_cost": 0,
            "outbox_pending": 0,
            "outbox_dead_letter": 0,
        }
    )
    assert summary.cost_usd is None
