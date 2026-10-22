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
from neoson_core.engine.delegation import DelegationLimits
from neoson_core.engine.integrations import (
    CalendarReader,
    CalendarWriter,
    EmailDraftWriter,
    EmailReader,
    IntegrationError,
)
from neoson_core.engine.voice import VoiceLimits, VoicePipeline
from neoson_core.infrastructure.actions import SqliteActionStore
from neoson_core.infrastructure.config import ConfigError, ServerSettings
from neoson_core.memory.curated import SqliteCuratedMemory
from neoson_core.memory.graph_backend import graph_store
from neoson_core.memory.search import SqliteHistorySearch
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake_speech import FakeSpeech
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.providers.openai_speech import OpenAISpeech
from neoson_core.routing.policy_registry import PolicyRegistry
from neoson_core.routing.session_router import SessionRouter
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.tools.memory_facts import MemoryForget, MemorySave
from neoson_core.tools.memory_search import MemorySearch
from neoson_core.tools.microsoft_oauth import MicrosoftOAuth
from neoson_core.tools.microsoft_read import MicrosoftPersonalReader
from neoson_core.tools.microsoft_write import MicrosoftPersonalWriter
from neoson_core.tools.oauth_store import LocalTokenStore
from neoson_core.tools.personal_fake import DisabledPersonalReader, FakePersonalReader
from neoson_core.tools.personal_read import CalendarList, EmailList
from neoson_core.tools.personal_sandbox import (
    DisabledPersonalWriter,
    SqlitePersonalSandbox,
)
from neoson_core.tools.personal_write import CalendarCreate, EmailDraft
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.routing_runtime import (
    BoundProvider,
    binding_for,
    runtime_hash,
    write_binding,
)

# Ferramentas que o gateway registra; as skills só podem declarar estas.
TOOL_NAMES = (
    CalendarList.name,
    EmailList.name,
    CalendarCreate.name,
    EmailDraft.name,
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
        delegation_limits=delegation_limits(settings),
    )


def delegation_limits(settings: ServerSettings) -> DelegationLimits | None:
    """Delegação só com opt-in explícito; skills não podem declará-la."""
    if not settings.delegation_enabled:
        return None
    if settings.max_subagents < 1:
        raise ConfigError(
            "NEOSON_MAX_SUBAGENTS deve ser >= 1 com NEOSON_DELEGATION_ENABLED=true."
        )
    if settings.delegation_max_output_tokens >= settings.delegation_max_tokens:
        raise ConfigError(
            "NEOSON_DELEGATION_MAX_OUTPUT_TOKENS deve ser menor que "
            "NEOSON_DELEGATION_MAX_TOKENS."
        )
    return DelegationLimits(
        max_attempts=settings.max_subagents,
        max_tokens=settings.delegation_max_tokens,
        max_output_tokens=settings.delegation_max_output_tokens,
        timeout_seconds=settings.delegation_timeout_seconds,
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


def build_voice(settings: ServerSettings, engine: Engine) -> VoicePipeline | None:
    if settings.voice_provider == "disabled":
        return None
    limits = VoiceLimits(
        max_bytes=settings.voice_max_bytes,
        max_seconds=settings.voice_max_seconds,
        timeout_seconds=settings.voice_timeout_seconds,
        max_concurrency=settings.voice_max_concurrency,
        max_output_seconds=settings.voice_max_output_seconds,
    )
    if settings.voice_provider == "fake":
        if settings.llm_provider != "echo" or settings.env == "production":
            raise ConfigError(
                "Voz fake exige NEOSON_LLM_PROVIDER=echo fora de produção."
            )
        fake = FakeSpeech()
        return VoicePipeline(engine, fake, fake, limits, simulated=True)
    if settings.openai_api_key is None:
        raise ConfigError(
            "OPENAI_API_KEY é obrigatória com NEOSON_VOICE_PROVIDER=openai."
        )
    # Fala real: escolha explícita; o áudio vai à OpenAI e não é guardado.
    speech = OpenAISpeech.from_api_key(
        settings.openai_api_key.get_secret_value(),
        timeout_seconds=settings.voice_timeout_seconds,
        stt_model=settings.voice_stt_model,
        tts_model=settings.voice_tts_model,
        voice=settings.voice_tts_voice,
        language=settings.voice_language,
        max_speech_chars=settings.voice_max_speech_chars,
    )
    return VoicePipeline(engine, speech, speech, limits)


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
    calendar, email = personal_readers(settings, clock)
    calendar_writer, draft_writer = personal_writers(settings)
    destination = {
        "disabled": "integração desativada",
        "fake": "sandbox local simulado, sem efeito na conta real",
        "microsoft": "conta Microsoft do consentimento local de escrita",
    }[settings.personal_write_provider]
    return ToolRegistry(
        [
            CalendarList(calendar),
            EmailList(email),
            CalendarCreate(calendar_writer, destination),
            EmailDraft(draft_writer, destination),
            MemorySearch(
                SqliteHistorySearch(settings.db_path),
                ZoneInfo(settings.timezone),
                # Só fatos revisados; desligado, a ferramenta fica como antes.
                graph_store(settings) if settings.memory_graph_enabled else None,
            ),
            MemorySave(memory),
            MemoryForget(memory),
        ]
    )


def microsoft_oauth(
    settings: ServerSettings,
    *,
    write: bool = False,
    bind: bool = False,
) -> MicrosoftOAuth:
    if not settings.microsoft_client_id:
        raise ConfigError("NEOSON_MICROSOFT_CLIENT_ID é obrigatório para Microsoft.")
    try:
        if (
            write
            and settings.microsoft_write_token_path.resolve()
            == settings.microsoft_token_path.resolve()
        ):
            raise ConfigError("Caches OAuth de leitura e escrita devem ser separados.")
        return MicrosoftOAuth(
            settings.microsoft_client_id,
            settings.microsoft_tenant,
            LocalTokenStore(
                settings.microsoft_write_token_path
                if write
                else settings.microsoft_token_path
            ),
            write=write,
            expected_binding=write_binding(settings) if bind else None,
        )
    except IntegrationError:
        raise ConfigError(
            "NEOSON_MICROSOFT_CLIENT_ID ou NEOSON_MICROSOFT_TENANT inválido."
        ) from None


def personal_readers(
    settings: ServerSettings,
    clock: Callable[[], datetime],
) -> tuple[CalendarReader, EmailReader]:
    if settings.personal_provider == "disabled":
        disabled = DisabledPersonalReader()
        return disabled, disabled
    if settings.personal_provider == "fake":
        if settings.env == "production":
            raise ConfigError("Integrações fake não são permitidas em produção.")
        fake = FakePersonalReader.demo(clock)
        return fake, fake
    reader = MicrosoftPersonalReader(
        microsoft_oauth(settings), ZoneInfo(settings.timezone)
    )
    return reader, reader


def personal_writers(
    settings: ServerSettings,
) -> tuple[CalendarWriter, EmailDraftWriter]:
    if settings.personal_write_provider == "disabled":
        disabled = DisabledPersonalWriter()
        return disabled, disabled
    if settings.personal_write_provider == "fake":
        if settings.env == "production":
            raise ConfigError("Sandbox fake não é permitido em produção.")
        if settings.personal_sandbox_db_path.resolve() == settings.db_path.resolve():
            raise ConfigError("Sandbox exige banco separado das conversas.")
        sandbox = SqlitePersonalSandbox(settings.personal_sandbox_db_path)
        return sandbox, sandbox
    writer = MicrosoftPersonalWriter(microsoft_oauth(settings, write=True, bind=True))
    return writer, writer


def api_token(settings: ServerSettings) -> str:
    if settings.api_token is None:
        raise ConfigError(
            "NEOSON_API_TOKEN é obrigatório: os clientes enviam "
            '"Authorization: Bearer <token>".'
        )
    return settings.api_token.get_secret_value()
