import os
from pathlib import Path

import pytest

from neoson_core.engine import AgentLoop, EchoEngine
from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_gateway.composition import build_engine


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_echo_provider_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEOSON_LLM_PROVIDER", "echo")

    assert isinstance(build_engine(load_settings(env_file=None)), EchoEngine)


def test_openai_without_key_fails_with_a_clear_message() -> None:
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        build_engine(load_settings(env_file=None))


def test_openai_without_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "chave-ficticia")

    with pytest.raises(ConfigError, match="NEOSON_LLM_MODEL"):
        build_engine(load_settings(env_file=None))


def test_openai_with_key_and_model_builds_the_agent_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "chave-ficticia")
    monkeypatch.setenv("NEOSON_LLM_MODEL", "modelo-x")

    assert isinstance(build_engine(load_settings(env_file=None)), AgentLoop)


def test_unknown_provider_is_rejected_by_the_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEOSON_LLM_PROVIDER", "outro")

    with pytest.raises(ConfigError, match="NEOSON_LLM_PROVIDER"):
        load_settings(env_file=None)
