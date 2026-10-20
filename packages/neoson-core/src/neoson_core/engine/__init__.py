"""Motor do NeoSon: tipos de domínio e portas. Só depende da biblioteca padrão."""

from neoson_core.engine.conversation import Engine, RunResult, RunStatus, UserMessage
from neoson_core.engine.echo import EchoEngine
from neoson_core.engine.llm import (
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ToolSpec,
    Usage,
)
from neoson_core.engine.loop import AgentLoop
from neoson_core.engine.messages import (
    Message,
    Role,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from neoson_core.engine.persistence import (
    ConversationStore,
    OutboxEvent,
    RunRecord,
    SessionNotFound,
)
from neoson_core.engine.recall import HistoryHit, HistorySearch
from neoson_core.engine.route import (
    Complexity,
    ExecutionMode,
    RouteDecision,
    Router,
    RouteRequest,
)
from neoson_core.engine.run import (
    Budget,
    InvalidTransition,
    RunContext,
    RunState,
    allowed_transitions,
    transition,
)
from neoson_core.engine.tools import (
    PolicyEngine,
    RiskLevel,
    ToolCatalog,
    ToolContext,
    ToolRejected,
    Verdict,
)

__all__ = [
    "AgentLoop",
    "Budget",
    "Complexity",
    "ConversationStore",
    "EchoEngine",
    "Engine",
    "ExecutionMode",
    "HistoryHit",
    "HistorySearch",
    "InvalidTransition",
    "LLMError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "OutboxEvent",
    "PolicyEngine",
    "RiskLevel",
    "Role",
    "RouteDecision",
    "RouteRequest",
    "Router",
    "RunContext",
    "RunRecord",
    "RunResult",
    "RunState",
    "RunStatus",
    "SessionNotFound",
    "ToolCall",
    "ToolCatalog",
    "ToolContext",
    "ToolRejected",
    "ToolResult",
    "ToolResultStatus",
    "ToolSpec",
    "Usage",
    "UserMessage",
    "Verdict",
    "allowed_transitions",
    "transition",
]
