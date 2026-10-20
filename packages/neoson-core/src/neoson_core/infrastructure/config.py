"""Configuração do servidor NeoSon.

Precedência: variáveis do processo > arquivo `neoson.env` > valores padrão. Só os
apps do servidor chamam `load_settings()`; os módulos recebem os valores prontos.
"""

from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_core import ErrorDetails
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_ENV_FILE = Path("neoson.env")


class ConfigError(Exception):
    """Configuração inválida. A mensagem cita as variáveis, nunca os valores."""


class ModelPrice(BaseModel):
    input: NonNegativeFloat
    output: NonNegativeFloat
    cached_input: NonNegativeFloat | None = None


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEOSON_",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    llm_provider: Literal["openai", "echo"] = "openai"
    llm_model: str | None = None
    llm_reasoning_effort: str | None = None
    # Preços em USD por milhão de tokens, por modelo (JSON); sem preço, custo vazio.
    llm_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias="OPENAI_API_KEY"
    )

    db_path: Path = Path("data/neoson.db")
    vault_path: Path = Path("data/NeoSon-Vault")
    backup_path: Path = Path("data/backups")
    skills_path: Path = Path("skill_library")

    primary_model_profile: str = "balanced"
    router_policy: str = "rules-v1"
    max_agent_steps: PositiveInt = 12
    history_max_messages: PositiveInt = 40
    memory_max_facts: PositiveInt = 30
    run_timeout_seconds: PositiveFloat = 120.0
    max_subagents: NonNegativeInt = 2
    approval_mode: Literal["required_for_sensitive", "always"] = (
        "required_for_sensitive"
    )
    timezone: str = "America/Sao_Paulo"

    memory_graph_enabled: bool = False
    metrics_export_enabled: bool = True

    api_token: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_allowed_users: Annotated[list[int], NoDecode] = Field(default_factory=list)

    worker_poll_seconds: PositiveFloat = 1.0
    worker_lease_seconds: PositiveFloat = 60.0
    worker_max_attempts: PositiveInt = 5

    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=8000, ge=1, le=65535)

    @field_validator("telegram_allowed_users", mode="before")
    @classmethod
    def _comma_separated_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("fuso horário IANA desconhecido") from None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level_upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> ServerSettings:
    """Carrega a configuração; `env_file=None` ignora o arquivo."""
    try:
        return ServerSettings(_env_file=env_file)
    except ValidationError as error:
        problems = "; ".join(
            _describe(detail)
            for detail in error.errors(include_input=False, include_url=False)
        )
        # `from None`: o ValidationError original traz os valores recebidos.
        raise ConfigError(f"Configuração inválida: {problems}") from None


def _describe(detail: ErrorDetails) -> str:
    name = str(detail["loc"][0]) if detail["loc"] else "?"
    variable = f"NEOSON_{name.upper()}" if name in ServerSettings.model_fields else name
    return f"{variable}: {detail['msg']}"
