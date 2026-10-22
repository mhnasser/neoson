import os
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from neoson_core.engine import (
    AgentLoop,
    Budget,
    EchoEngine,
    RiskLevel,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from neoson_core.infrastructure.config import load_settings
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_gateway.app import create_app
from neoson_gateway.composition import build_queries
from neoson_gateway.queries import QueryConfig

TOKEN = "token-de-teste"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Catalog:
    def specs(self) -> tuple[ToolSpec, ...]:
        return (ToolSpec("ver", "ver", {"type": "object"}),)

    def check(self, call: ToolCall) -> RiskLevel:
        return RiskLevel.READ

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        return ToolResult(call.id, ToolResultStatus.OK, "dado-da-ferramenta")


def client(tmp_path: Path, script: list[Any]) -> tuple[TestClient, Path]:
    db = tmp_path / "neoson.db"
    engine = AgentLoop(
        ScriptedProvider(script),
        RulesRouter("balanced"),
        Budget(4, 5),
        tools=Catalog(),
        store=SqliteConversationStore(db),
    )
    app = create_app(engine, TOKEN, queries=QueryConfig(db, UTC))
    return TestClient(app), db


def test_run_shows_status_text_and_usage(tmp_path: Path) -> None:
    api, _ = client(tmp_path, [call_tool("ver"), reply("Resposta final.")])
    sent = api.post("/api/v1/messages", json={"text": "Olá"}, headers=AUTH).json()
    response = api.get(f"/api/v1/runs/{sent['run_id']}", headers=AUTH)
    body = response.json()
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    assert (body["status"], body["text"]) == ("completed", "Resposta final.")
    assert body["session_id"] == sent["session_id"] and body["model"]
    assert body["usage"] == {
        "input_tokens": 20,
        "output_tokens": 10,
        "cached_tokens": 0,
    }
    assert api.get(f"/api/v1/runs/{uuid4()}", headers=AUTH).status_code == 404
    assert api.get(f"/api/v1/runs/{sent['run_id']}").status_code == 401


def test_session_history_is_paginated_newest_first_without_tool_messages(
    tmp_path: Path,
) -> None:
    script: list[Any] = [call_tool("ver"), reply("R1."), reply("R2."), reply("R3.")]
    api, _ = client(tmp_path, script)
    first = api.post("/api/v1/messages", json={"text": "P1"}, headers=AUTH).json()
    session = first["session_id"]
    for text in ("P2", "P3"):
        api.post(
            "/api/v1/messages", json={"text": text, "session_id": session}, headers=AUTH
        )
    page = api.get(f"/api/v1/sessions/{session}?limit=4", headers=AUTH).json()
    assert [m["content"] for m in page["messages"]] == ["R3.", "P3", "R2.", "P2"]
    assert page["channel"] == "http" and page["next_before"] is not None
    rest = api.get(
        f"/api/v1/sessions/{session}?limit=4&before={page['next_before']}",
        headers=AUTH,
    ).json()
    assert [m["content"] for m in rest["messages"]] == ["R1.", "P1"]
    assert rest["next_before"] is None
    assert "dado-da-ferramenta" not in str(page) + str(rest)
    assert api.get(f"/api/v1/sessions/{uuid4()}", headers=AUTH).status_code == 404
    for query in ("limit=0", "limit=101", "before=0"):
        assert (
            api.get(f"/api/v1/sessions/{session}?{query}", headers=AUTH).status_code
            == 422
        )


def test_readiness_checks_database_and_migrations_without_auth(tmp_path: Path) -> None:
    api, db = client(tmp_path, [reply("Oi.")])
    api.post("/api/v1/messages", json={"text": "Oi"}, headers=AUTH)
    ready = api.get("/api/v1/health/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    assert ready.json()["checks"] == {"database": True, "migrations": True}
    with closing(connect(db)) as c, c:
        c.execute(
            "DELETE FROM schema_migrations WHERE version ="
            " (SELECT MAX(version) FROM schema_migrations)"
        )
    behind = api.get("/api/v1/health/ready")
    assert behind.status_code == 503 and behind.json()["checks"]["migrations"] is False
    missing = tmp_path / "nao-existe.db"
    other = TestClient(
        create_app(EchoEngine(), TOKEN, queries=QueryConfig(missing, UTC))
    ).get("/api/v1/health/ready")
    assert other.status_code == 503 and not missing.exists()
    echo = TestClient(create_app(EchoEngine(), TOKEN)).get("/api/v1/health/ready")
    assert echo.status_code == 200 and echo.json() == {
        "protocol_version": 1,
        "status": "ready",
        "checks": {},
    }


def test_metrics_summary_counts_the_day(tmp_path: Path) -> None:
    api, _ = client(tmp_path, [reply("Oi."), reply("De novo.")])
    for text in ("a", "b"):
        api.post("/api/v1/messages", json={"text": text}, headers=AUTH)
    today = datetime.now(UTC).date().isoformat()
    body = api.get("/api/v1/metrics/summary", headers=AUTH).json()
    assert body["day"] == today and body["runs_total"] == 2
    assert body["completed"] == 2 and body["input_tokens"] == 20
    assert body["outbox_pending"] == 2 and body["outbox_dead_letter"] == 0
    past = api.get("/api/v1/metrics/summary?day=2020-01-01", headers=AUTH).json()
    assert past["runs_total"] == 0
    assert api.get("/api/v1/metrics/summary").status_code == 401


def test_queries_only_exist_with_the_persistent_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NEOSON_LLM_PROVIDER", "echo")
    assert build_queries(load_settings(env_file=None)) is None
    monkeypatch.setenv("NEOSON_LLM_PROVIDER", "openai")
    config = build_queries(load_settings(env_file=None))
    assert config is not None and config.db.name == "neoson.db"
