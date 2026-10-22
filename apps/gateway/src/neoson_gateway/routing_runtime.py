"""Composition root para perfis vinculados; chaves nunca entram no contexto público."""

from neoson_core.engine import LLMProvider, LLMRequest, LLMResponse
from neoson_core.infrastructure.config import ServerSettings
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.routing.lab_plan import digest
from neoson_core.routing.policy_config import ModelBinding
from neoson_core.routing.policy_registry import PolicyRegistry


def binding_for(settings: ServerSettings) -> ModelBinding:
    if settings.llm_provider != "openai" or not settings.llm_model:
        raise ValueError("Roteamento exige modelo e provedor explícitos.")
    return ModelBinding(
        profile=settings.primary_model_profile,
        model=settings.llm_model,
        reasoning_effort=settings.llm_reasoning_effort,
    )


def runtime_hash(settings: ServerSettings) -> str:
    return digest(
        {
            "binding": binding_for(settings).model_dump(mode="json"),
            "prices": {m: p.model_dump() for m, p in settings.llm_prices.items()},
            "steps": settings.max_agent_steps,
            "timeout": settings.run_timeout_seconds,
            "approval": settings.approval_mode,
            "history": settings.history_max_messages,
            "timezone": settings.timezone,
            "tools_version": "gateway-tools-v2",
            "personal_provider": settings.personal_provider,
            "microsoft_client": settings.microsoft_client_id,
            "microsoft_tenant": settings.microsoft_tenant,
            "runtime_version": "session-routing-v1",
        }
    )


class BoundProvider:
    def __init__(self, settings: ServerSettings, registry: PolicyRegistry) -> None:
        if settings.openai_api_key is None:
            raise ValueError("Credencial do provedor ausente.")
        self.settings, self.registry = settings, registry
        self.providers: dict[str, LLMProvider] = {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        binding = self.registry.resolve(request.model_profile)
        provider = self.providers.get(request.model_profile)
        if provider is None:
            assert self.settings.openai_api_key is not None
            provider = OpenAIProvider.from_api_key(
                api_key=self.settings.openai_api_key.get_secret_value(),
                models={request.model_profile: binding.model},
                timeout_seconds=self.settings.run_timeout_seconds,
                reasoning_effort=binding.reasoning_effort,
            )
            self.providers[request.model_profile] = provider
        return await provider.complete(request)
