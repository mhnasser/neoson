from fastapi.testclient import TestClient

from neoson_gateway.app import create_app


def test_health_live_reports_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
