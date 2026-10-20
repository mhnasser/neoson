"""Montagem do motor a partir da configuração (composition root do gateway)."""

from neoson_core.engine import AgentLoop, Budget, EchoEngine, Engine
from neoson_core.infrastructure.config import ConfigError, ServerSettings
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.routing.rules import RulesRouter


def build_engine(settings: ServerSettings) -> Engine:
    if settings.llm_provider == "echo":
        return EchoEngine()

    if settings.openai_api_key is None:
        raise ConfigError(
            "OPENAI_API_KEY é obrigatória com NEOSON_LLM_PROVIDER=openai "
            "(use NEOSON_LLM_PROVIDER=echo para desenvolver sem chave)."
        )
    if not settings.llm_model:
        raise ConfigError(
            "NEOSON_LLM_MODEL é obrigatório com NEOSON_LLM_PROVIDER=openai."
        )

    provider = OpenAIProvider.from_api_key(
        api_key=settings.openai_api_key.get_secret_value(),
        models={settings.primary_model_profile: settings.llm_model},
        timeout_seconds=settings.run_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort,
    )
    return AgentLoop(
        provider=provider,
        router=RulesRouter(settings.primary_model_profile),
        budget=Budget(
            max_steps=settings.max_agent_steps,
            timeout_seconds=settings.run_timeout_seconds,
        ),
    )
