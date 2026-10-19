import os
from pathlib import Path

import pytest

from neoson_core.infrastructure.config import (
    ConfigError,
    ServerSettings,
    load_settings,
)

SECRET = "valor-ficticio-de-teste-123"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Sem variáveis NEOSON_* do processo e fora da raiz (sem o neoson.env real)."""
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def write_env(directory: Path, content: str) -> Path:
    path = directory / "neoson.env"
    path.write_text(content, encoding="utf-8")
    return path


def test_defaults_without_file_or_environment() -> None:
    settings = load_settings(env_file=None)

    assert settings == ServerSettings(_env_file=None)
    assert settings.env == "development"
    assert settings.log_level == "INFO"
    assert settings.llm_provider == "openai"
    assert settings.llm_model is None
    assert settings.openai_api_key is None
    assert settings.db_path == Path("data/neoson.db")
    assert settings.max_agent_steps == 12
    assert settings.max_subagents == 2
    assert settings.memory_graph_enabled is False
    assert settings.metrics_export_enabled is True
    assert (settings.gateway_host, settings.gateway_port) == ("127.0.0.1", 8000)


def test_reads_values_from_env_file(tmp_path: Path) -> None:
    env_file = write_env(
        tmp_path,
        "NEOSON_ENV=production\n"
        "NEOSON_LOG_LEVEL=debug\n"
        "NEOSON_LLM_MODEL=modelo-x\n"
        "NEOSON_MAX_AGENT_STEPS=5\n"
        "NEOSON_MEMORY_GRAPH_ENABLED=true\n"
        "NEOSON_GATEWAY_PORT=9000\n"
        f"OPENAI_API_KEY={SECRET}\n",
    )

    settings = load_settings(env_file=env_file)

    assert settings.env == "production"
    assert settings.log_level == "DEBUG"
    assert settings.llm_model == "modelo-x"
    assert settings.max_agent_steps == 5
    assert settings.memory_graph_enabled is True
    assert settings.gateway_port == 9000
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == SECRET


def test_default_env_file_is_neoson_env_in_working_directory(tmp_path: Path) -> None:
    write_env(tmp_path, "NEOSON_MAX_SUBAGENTS=0\n")

    assert load_settings().max_subagents == 0


def test_process_environment_overrides_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = write_env(tmp_path, "NEOSON_MAX_AGENT_STEPS=5\n")
    monkeypatch.setenv("NEOSON_MAX_AGENT_STEPS", "7")

    assert load_settings(env_file=env_file).max_agent_steps == 7


def test_ignores_empty_and_unrelated_entries(tmp_path: Path) -> None:
    env_file = write_env(
        tmp_path,
        "NEOSON_LLM_MODEL=\nOPENAI_API_KEY=\nNEOSON_PI_HOST=neoson-pi.local\nOUTRA=1\n",
    )

    settings = load_settings(env_file=env_file)

    assert settings.llm_model is None
    assert settings.openai_api_key is None


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("NEOSON_ENV", "staging"),
        ("NEOSON_LOG_LEVEL", "verboso"),
        ("NEOSON_MAX_AGENT_STEPS", "-3"),
        ("NEOSON_MAX_SUBAGENTS", "-1"),
        ("NEOSON_GATEWAY_PORT", "70000"),
        ("NEOSON_METRICS_EXPORT_ENABLED", "talvez"),
    ],
)
def test_invalid_value_names_the_variable_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigError) as caught:
        load_settings(env_file=None)

    message = str(caught.value)
    assert variable in message
    assert value not in message
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_secret_is_hidden_in_repr_and_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)

    settings = load_settings(env_file=None)

    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert SECRET not in repr(settings.model_dump())


def test_secret_is_not_leaked_by_configuration_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("NEOSON_MAX_AGENT_STEPS", "muitos")

    with pytest.raises(ConfigError) as caught:
        load_settings(env_file=None)

    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)


def test_settings_are_immutable() -> None:
    settings = load_settings(env_file=None)

    with pytest.raises(ValueError):
        settings.max_agent_steps = 1  # type: ignore[misc]


def test_env_example_matches_code_defaults_and_lists_every_variable() -> None:
    example = Path(__file__).resolve().parents[1] / ".env.example"
    keys = {
        line.split("=", 1)[0]
        for line in example.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    variables = {
        str(field.validation_alias)
        if field.validation_alias
        else f"NEOSON_{name.upper()}"
        for name, field in ServerSettings.model_fields.items()
    }

    assert load_settings(env_file=example) == load_settings(env_file=None)
    assert variables <= keys
