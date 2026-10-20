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
from neoson_core.engine.messages import (
    Message,
    Role,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from neoson_core.engine.route import Complexity, ExecutionMode, RouteDecision
from neoson_core.engine.run import (
    Budget,
    InvalidTransition,
    RunContext,
    RunState,
    allowed_transitions,
    transition,
)

__all__ = [
    "Budget",
    "Complexity",
    "EchoEngine",
    "Engine",
    "ExecutionMode",
    "InvalidTransition",
    "LLMError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "Role",
    "RouteDecision",
    "RunContext",
    "RunResult",
    "RunState",
    "RunStatus",
    "ToolCall",
    "ToolResult",
    "ToolResultStatus",
    "ToolSpec",
    "Usage",
    "UserMessage",
    "allowed_transitions",
    "transition",
]
