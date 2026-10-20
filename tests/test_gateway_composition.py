import asyncio
import os
from pathlib import Path

import pytest

from neoson_core.engine import AgentLoop, EchoEngine, Role, RunStatus, UserMessage
from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.skills.loader import load_skill
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_gateway.composition import api_token, build_engine


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


def test_gateway_connects_approved_skills_to_the_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "chave-ficticia")
    monkeypatch.setenv("NEOSON_LLM_MODEL", "modelo-x")
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "neoson.db"))
    settings = load_settings(env_file=None)
    source = Path(__file__).resolve().parents[1] / "skill_library" / "planejar_dia"
    SqliteSkillRegistry(settings.db_path).approve(load_skill(source))
    provider = ScriptedProvider(
        [
            call_tool("skill_load", {"skill_id": "planejar_dia", "version": "1.0.0"}),
            reply("Plano pronto."),
        ]
    )
    monkeypatch.setattr(
        "neoson_gateway.composition.OpenAIProvider.from_api_key",
        lambda **kwargs: provider,
    )
    result = asyncio.run(build_engine(settings).run(UserMessage("Planeje meu dia.")))
    assert result.status is RunStatus.COMPLETED
    assert "planejar_dia" in provider.requests[0].messages[0].content
    assert provider.requests[1].messages[-1].role is Role.SYSTEM


def test_unknown_provider_is_rejected_by_the_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEOSON_LLM_PROVIDER", "outro")

    with pytest.raises(ConfigError, match="NEOSON_LLM_PROVIDER"):
        load_settings(env_file=None)


def test_api_token_is_required() -> None:
    with pytest.raises(ConfigError, match="NEOSON_API_TOKEN"):
        api_token(load_settings(env_file=None))


def test_api_token_comes_from_the_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEOSON_API_TOKEN", "segredo-do-gateway")

    assert api_token(load_settings(env_file=None)) == "segredo-do-gateway"


def test_openai_mode_creates_the_conversation_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "chave-ficticia")
    monkeypatch.setenv("NEOSON_LLM_MODEL", "modelo-x")
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "db" / "neoson.db"))

    build_engine(load_settings(env_file=None))

    assert (tmp_path / "db" / "neoson.db").exists()
