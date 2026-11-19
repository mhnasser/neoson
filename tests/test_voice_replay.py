from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from neoson_core.engine import EchoEngine
from neoson_core.engine.voice import VoicePipeline
from neoson_core.infrastructure.db import connect
from neoson_core.providers.fake_speech import FakeSpeech
from neoson_gateway.app import create_app
from neoson_gateway.voice_replay import VoiceReplayGuard
from neoson_robot.fake_devices import silent_wav
from neoson_robot.voice_client import HttpVoiceGateway


@pytest.mark.parametrize("offset", [-121, 121, float("nan"), float("inf")])
def test_replay_rejects_old_future_or_non_finite_time(
    tmp_path: Path, offset: float
) -> None:
    guard = VoiceReplayGuard(tmp_path / "test.db", clock=lambda: 1000)
    with pytest.raises(HTTPException) as caught:
        guard.claim(str(uuid4()), str(1000 + offset))
    assert caught.value.status_code == 400


def test_duplicate_is_atomic_and_survives_a_restart(tmp_path: Path) -> None:
    db, identifier = tmp_path / "test.db", str(uuid4())
    guard = VoiceReplayGuard(db, clock=lambda: 1000)

    def claim(_: int) -> int:
        try:
            guard.claim(identifier, "1000")
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(claim, range(8)))
    assert sorted(statuses) == [200] + [409] * 7
    with pytest.raises(HTTPException) as caught:
        VoiceReplayGuard(db, clock=lambda: 1001).claim(identifier, "1000")
    assert caught.value.status_code == 409


@pytest.mark.parametrize("route", ["/api/v1/voice", "/api/v1/voice/stream"])
def test_routes_require_headers_reject_duplicates_and_do_not_consume_on_failed_auth(
    tmp_path: Path,
    route: str,
) -> None:
    engine, speech = EchoEngine(), FakeSpeech()
    guard = VoiceReplayGuard(tmp_path / "test.db", clock=lambda: 1000)
    client = TestClient(
        create_app(
            engine,
            "test-only",
            voice=VoicePipeline(engine, speech, speech, simulated=True),
            voice_replay=guard,
        )
    )
    headers = {
        "Authorization": "Bearer test-only",
        "Content-Type": "audio/wav",
        "X-Neoson-Activation": "explicit",
    }
    assert client.post(route, content=silent_wav(), headers=headers).status_code == 400
    headers.update(
        {"X-Neoson-Request-Id": str(uuid4()), "X-Neoson-Request-Time": "1000"}
    )
    assert (
        client.post(
            route,
            content=silent_wav(),
            headers={**headers, "Authorization": "Bearer invalid"},
        ).status_code
        == 401
    )
    assert client.post(route, content=silent_wav(), headers=headers).status_code == 200
    assert client.post(route, content=silent_wav(), headers=headers).status_code == 409


def test_client_sends_a_new_id_and_time_for_each_explicit_request(
    tmp_path: Path,
) -> None:
    engine, speech = EchoEngine(), FakeSpeech()
    server = TestClient(
        create_app(
            engine,
            "test-only",
            voice=VoicePipeline(engine, speech, speech, simulated=True),
            voice_replay=VoiceReplayGuard(tmp_path / "test.db"),
        )
    )
    seen: list[str] = []

    def bridge(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["x-neoson-request-id"])
        reply = server.post(
            str(request.url.path),
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx2.Response(
            reply.status_code, content=reply.content, headers=dict(reply.headers)
        )

    with httpx2.Client(
        base_url="http://gateway.test", transport=httpx2.MockTransport(bridge)
    ) as http:
        gateway = HttpVoiceGateway(http, "test-only")
        assert list(gateway.events(silent_wav(), None, True))
        assert list(gateway.events(silent_wav(), None, True))
    assert len(seen) == len(set(seen)) == 2


def test_expired_receipts_are_cleaned_but_original_timestamp_is_rejected(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    guard = VoiceReplayGuard(tmp_path / "test.db", clock=lambda: now[0])
    identifier = str(uuid4())
    guard.claim(identifier, "1000")
    now[0] = 1121
    with pytest.raises(HTTPException):
        guard.claim(identifier, "1000")
    guard.claim(str(uuid4()), "1121")
    with closing(connect(guard.db)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM voice_requests").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    "identifier, timestamp",
    [(None, None), ("invalid", "1000"), (str(uuid4()), "invalid")],
)
def test_invalid_headers_do_not_create_receipts(
    tmp_path: Path,
    identifier: str | None,
    timestamp: str | None,
) -> None:
    guard = VoiceReplayGuard(tmp_path / "test.db", clock=lambda: 1000)
    with pytest.raises(HTTPException) as caught:
        guard.claim(identifier, timestamp)
    assert caught.value.status_code == 400


def test_receipt_limit_fails_closed_and_expiry_releases_capacity(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    guard = VoiceReplayGuard(tmp_path / "test.db", clock=lambda: now[0])
    with closing(connect(guard.db)) as connection:
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO voice_requests VALUES (?, ?)",
            ((str(uuid4()), 1120) for _ in range(10000)),
        )
        connection.execute("COMMIT")
    with pytest.raises(HTTPException) as caught:
        guard.claim(str(uuid4()), "1000")
    assert caught.value.status_code == 503
    now[0] = 1121
    guard.claim(str(uuid4()), "1121")
