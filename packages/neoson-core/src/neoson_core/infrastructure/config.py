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
    # text mantém o formato de antes; json escreve um objeto por linha.
    log_format: Literal["text", "json"] = "text"
    log_file: Path | None = None
    log_max_bytes: int = Field(default=5_000_000, ge=10_000, le=500_000_000)
    log_backups: int = Field(default=5, ge=1, le=50)
    # Falhas temporárias seguidas do modelo que abrem o circuito (0 desliga).
    llm_circuit_failures: NonNegativeInt = 5
    llm_circuit_reset_seconds: float = Field(default=30, gt=0, le=3600)

    llm_provider: Literal["openai", "echo"] = "openai"
    llm_model: str | None = None
    llm_reasoning_effort: str | None = None
    # Preços em USD por milhão de tokens, por modelo (JSON); sem preço, custo vazio.
    llm_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias="OPENAI_API_KEY"
    )

    db_path: Path = Path("data/neoson.db")
    router_lab_db_path: Path = Path("data/router-lab.db")
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
    # Análise delegada sem ferramentas; desligada por padrão. `max_subagents` limita
    # as tentativas por execução.
    delegation_enabled: bool = False
    delegation_timeout_seconds: float = Field(default=15, gt=0, le=120)
    delegation_max_tokens: int = Field(default=16000, ge=1, le=200000)
    delegation_max_output_tokens: int = Field(default=1000, ge=1, le=16000)
    approval_mode: Literal["required_for_sensitive", "always"] = (
        "required_for_sensitive"
    )
    timezone: str = "America/Sao_Paulo"

    memory_graph_enabled: bool = False
    graph_backend: Literal["sqlite", "neo4j"] = "sqlite"
    neo4j_uri: str | None = None
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr | None = None
    neo4j_database: str = "neo4j"
    neo4j_timeout_seconds: float = Field(default=10, gt=0, le=60)
    graph_extraction_timeout_seconds: PositiveFloat = 30
    graph_extraction_max_attempts: PositiveInt = 3
    graph_extraction_max_daily_jobs: PositiveInt = 20
    graph_extraction_max_daily_tokens: PositiveInt = 40000
    graph_extraction_max_input_chars: PositiveInt = 4000
    graph_extraction_max_output_tokens: PositiveInt = 1500
    graph_extraction_max_facts: int = Field(default=10, ge=1, le=50)
    graph_extraction_max_daily_cost_usd: PositiveFloat | None = None
    metrics_export_enabled: bool = True

    api_token: SecretStr | None = None

    personal_provider: Literal["disabled", "fake", "microsoft"] = "disabled"
    microsoft_client_id: str | None = None
    microsoft_tenant: str = "consumers"
    microsoft_token_path: Path = Path("data/credentials/microsoft.bin")
    personal_write_provider: Literal["disabled", "fake", "microsoft"] = "disabled"
    microsoft_write_token_path: Path = Path("data/credentials/microsoft-write.bin")
    personal_sandbox_db_path: Path = Path("data/personal-sandbox.db")

    # Leitura de páginas: desligada; fora dos domínios liberados exige aprovação.
    web_fetch_enabled: bool = False
    web_fetch_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    web_fetch_timeout_seconds: float = Field(default=10, gt=0, le=60)
    web_fetch_max_bytes: int = Field(default=1_000_000, ge=1024, le=5_000_000)
    web_fetch_max_chars: int = Field(default=6000, ge=200, le=20000)

    voice_provider: Literal["disabled", "fake", "openai"] = "disabled"
    voice_max_bytes: int = Field(default=1048576, ge=44, le=2097152)
    voice_max_seconds: float = Field(default=15, gt=0, le=30)
    voice_max_output_seconds: float = Field(default=30, gt=0, le=30)
    voice_timeout_seconds: float = Field(default=30, gt=0, le=120)
    voice_max_concurrency: int = Field(default=1, ge=1, le=4)
    # Só com NEOSON_VOICE_PROVIDER=openai; usa a mesma OPENAI_API_KEY.
    voice_stt_model: str = "gpt-4o-mini-transcribe"
    voice_tts_model: str = "gpt-4o-mini-tts"
    voice_tts_voice: str = "marin"
    voice_language: str = Field(default="pt", pattern=r"^[a-z]{2}$")
    voice_max_speech_chars: int = Field(default=400, ge=50, le=2000)

    telegram_bot_token: SecretStr | None = None
    telegram_allowed_users: Annotated[list[int], NoDecode] = Field(default_factory=list)

    worker_poll_seconds: PositiveFloat = 1.0
    worker_lease_seconds: PositiveFloat = 60.0
    worker_max_attempts: PositiveInt = 5

    curation_enabled: bool = False
    curation_timeout_seconds: PositiveFloat = 30
    curation_max_attempts: PositiveInt = 3
    curation_max_daily_jobs: PositiveInt = 5
    curation_max_daily_tokens: PositiveInt = 60000
    curation_max_input_chars: PositiveInt = 12000
    curation_max_output_tokens: PositiveInt = 3000
    curation_max_daily_cost_usd: PositiveFloat | None = None

    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=8000, ge=1, le=65535)

    @field_validator("web_fetch_allowed_domains", mode="before")
    @classmethod
    def _comma_separated_domains(cls, value: object) -> object:
        if isinstance(value, str):
            return [d.strip().lower() for d in value.split(",") if d.strip()]
        return value

    @field_validator("web_fetch_allowed_domains")
    @classmethod
    def _plain_domains(cls, value: list[str]) -> list[str]:
        for domain in value:
            labels = domain.rstrip(".").split(".")
            if len(labels) < 2 or not all(
                label and label.replace("-", "").isalnum() for label in labels
            ):
                raise ValueError("domínio inválido")
        return [d.rstrip(".") for d in value]

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
