import os
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from neoson_gateway import main as gateway_main


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_main_starts_uvicorn_with_configured_host_port_and_log_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("NEOSON_LLM_PROVIDER", "echo")
    monkeypatch.setenv("NEOSON_GATEWAY_PORT", "9123")
    monkeypatch.setenv("NEOSON_LOG_LEVEL", "WARNING")

    gateway_main.main()

    assert calls == [{"host": "127.0.0.1", "port": 9123, "log_level": "warning"}]


def test_main_exits_with_clear_message_on_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv("NEOSON_GATEWAY_PORT", "porta")

    with pytest.raises(SystemExit) as caught:
        gateway_main.main()

    assert "NEOSON_GATEWAY_PORT" in str(caught.value.code)
    assert "porta" not in str(caught.value.code)
