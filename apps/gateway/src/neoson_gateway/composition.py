"""Montagem do motor a partir da configuração (composition root do gateway)."""

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from neoson_core.engine import (
    AgentLoop,
    Budget,
    CuratedMemory,
    EchoEngine,
    Engine,
    LLMProvider,
    PolicyEngine,
)
from neoson_core.engine.actions import ActionService, action_context
from neoson_core.infrastructure.actions import SqliteActionStore
from neoson_core.infrastructure.config import ConfigError, ServerSettings
from neoson_core.memory.curated import SqliteCuratedMemory
from neoson_core.memory.search import SqliteHistorySearch
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.routing.policy_registry import PolicyRegistry
from neoson_core.routing.session_router import SessionRouter
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.tools.calendar_fake import FakeCalendar
from neoson_core.tools.memory_facts import MemoryForget, MemorySave
from neoson_core.tools.memory_search import MemorySearch
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.routing_runtime import BoundProvider, binding_for, runtime_hash

# Ferramentas que o gateway registra; as skills só podem declarar estas.
TOOL_NAMES = (
    FakeCalendar.name,
    MemorySearch.name,
    MemorySave.name,
    MemoryForget.name,
)


def build_engine(settings: ServerSettings) -> Engine:
    if settings.llm_provider == "echo":
        return EchoEngine()
    if settings.openai_api_key is None:
        raise ConfigError(
            "OPENAI_API_KEY é obrigatória com NEOSON_LLM_PROVIDER=openai."
        )
    if not settings.llm_model:
        raise ConfigError(
            "NEOSON_LLM_MODEL é obrigatório com NEOSON_LLM_PROVIDER=openai."
        )
    binding = binding_for(settings)
    registry = PolicyRegistry(settings.db_path)
    provider = BoundProvider(settings, registry)
    zone = ZoneInfo(settings.timezone)
    memory = SqliteCuratedMemory(settings.db_path)

    def clock() -> datetime:
        return datetime.now(zone)

    return AgentLoop(
        provider=provider,
        router=SessionRouter(registry, binding, runtime_hash(settings)),
        budget=Budget(
            max_steps=settings.max_agent_steps,
            timeout_seconds=settings.run_timeout_seconds,
        ),
        tools=build_tools(settings, memory, clock),
        policy=PolicyEngine(settings.approval_mode),
        clock=clock,
        store=SqliteConversationStore(settings.db_path),
        history_max_messages=settings.history_max_messages,
        memory=memory,
        memory_max_facts=settings.memory_max_facts,
        skills=SqliteSkillRegistry(settings.db_path),
        approval_runtime=runtime_hash(settings),
    )


def build_actions(settings: ServerSettings) -> ActionService | None:
    if settings.llm_provider == "echo":
        return None
    zone = ZoneInfo(settings.timezone)
    tools = build_tools(
        settings, SqliteCuratedMemory(settings.db_path), lambda: datetime.now(zone)
    )
    policy = PolicyEngine(settings.approval_mode)
    return ActionService(
        SqliteActionStore(settings.db_path),
        tools,
        policy,
        action_context(runtime_hash(settings), policy, tools),
        settings.run_timeout_seconds,
        SqliteSkillRegistry(settings.db_path),
    )


def build_provider(settings: ServerSettings) -> LLMProvider:
    if settings.llm_provider != "openai":
        raise ConfigError("A avaliação de skills exige NEOSON_LLM_PROVIDER=openai.")
    if settings.openai_api_key is None:
        raise ConfigError(
            "OPENAI_API_KEY é obrigatória com NEOSON_LLM_PROVIDER=openai "
            "(use NEOSON_LLM_PROVIDER=echo para desenvolver sem chave)."
        )
    if not settings.llm_model:
        raise ConfigError(
            "NEOSON_LLM_MODEL é obrigatório com NEOSON_LLM_PROVIDER=openai."
        )
    return OpenAIProvider.from_api_key(
        api_key=settings.openai_api_key.get_secret_value(),
        models={settings.primary_model_profile: settings.llm_model},
        timeout_seconds=settings.run_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort,
    )


def build_tools(
    settings: ServerSettings, memory: CuratedMemory, clock: Callable[[], datetime]
) -> ToolRegistry:
    return ToolRegistry(
        [
            FakeCalendar.demo(clock),
            MemorySearch(
                SqliteHistorySearch(settings.db_path), ZoneInfo(settings.timezone)
            ),
            MemorySave(memory),
            MemoryForget(memory),
        ]
    )


def api_token(settings: ServerSettings) -> str:
    if settings.api_token is None:
        raise ConfigError(
            "NEOSON_API_TOKEN é obrigatório: os clientes enviam "
            '"Authorization: Bearer <token>".'
        )
    return settings.api_token.get_secret_value()
