"""Agent Loop (D04): o modelo propõe, o laço controla a repetição e os limites."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from neoson_core.engine.conversation import RunResult, RunStatus, UserMessage
from neoson_core.engine.llm import (
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Usage,
)
from neoson_core.engine.messages import Message, Role, ToolCall
from neoson_core.engine.route import Router, RouteRequest
from neoson_core.engine.run import Budget, RunContext, RunState, transition

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Você é o NeoSon, um assistente pessoal. Responda em português do Brasil, "
    "de forma clara e direta. Se não souber algo, diga que não sabe."
)
TOOL_UNAVAILABLE = "Ferramenta indisponível no momento."

StateObserver = Callable[[RunContext, RunState], None]


class _Stop(Exception):
    """Interrompe a execução com uma mensagem segura para o usuário."""

    def __init__(self, reason: str, user_text: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_text = user_text


class AgentLoop:
    def __init__(
        self,
        provider: LLMProvider,
        router: Router,
        budget: Budget,
        system_prompt: str = SYSTEM_PROMPT,
        on_state: StateObserver | None = None,
    ) -> None:
        self._provider = provider
        self._router = router
        self._budget = budget
        self._system_prompt = system_prompt
        self._on_state = on_state

    async def run(self, message: UserMessage) -> RunResult:
        context = RunContext(
            session_id=message.session_id or uuid4(), budget=self._budget
        )
        run = _Run(context, self._on_state)
        try:
            text, model = await self._execute(run, message)
        except _Stop as stop:
            logger.warning("run %s interrompido: %s", context.run_id, stop.reason)
            run.advance(RunState.FAILED)
            return RunResult(
                run_id=context.run_id,
                text=stop.user_text,
                status=RunStatus.FAILED,
                usage=run.usage,
                model=run.model,
            )
        run.advance(RunState.COMPLETED)
        return RunResult(
            run_id=context.run_id,
            text=text,
            status=RunStatus.COMPLETED,
            usage=run.usage,
            model=model,
        )

    async def _execute(self, run: "_Run", message: UserMessage) -> tuple[str, str]:
        run.advance(RunState.ROUTING)
        route = await self._router.decide(
            RouteRequest(text=message.text, session_id=message.session_id)
        )
        run.advance(RunState.CONTEXT_READY)
        messages = [
            Message(role=Role.SYSTEM, content=self._system_prompt),
            Message(role=Role.USER, content=message.text),
        ]
        run.advance(RunState.RUNNING)

        for _ in range(self._budget.max_steps):
            run.check_limits()
            response = await run.complete(
                self._provider,
                LLMRequest(
                    model_profile=route.primary_model_profile,
                    messages=tuple(messages),
                ),
            )
            messages.append(response.message)
            if not response.message.tool_calls:
                return response.message.content, response.model
            messages.extend(_unavailable(call) for call in response.message.tool_calls)

        raise _Stop(
            "limite de passos",
            "Não consegui concluir dentro do limite de passos desta conversa.",
        )


class _Run:
    """Estado mutável de uma execução: estado atual, uso acumulado e prazo."""

    def __init__(self, context: RunContext, on_state: StateObserver | None) -> None:
        self.context = context
        self.state = RunState.RECEIVED
        self.usage = Usage()
        self.model: str | None = None
        self._on_state = on_state
        self._notify()

    def advance(self, target: RunState) -> None:
        self.state = transition(self.state, target)
        self._notify()

    def check_limits(self) -> None:
        if self.context.expired():
            raise _Stop("prazo", "Não consegui concluir dentro do tempo limite.")
        max_tokens = self.context.budget.max_tokens
        if max_tokens is not None and self.usage.total_tokens >= max_tokens:
            raise _Stop("tokens", "Não consegui concluir dentro do limite de uso.")

    async def complete(self, provider: LLMProvider, request: LLMRequest) -> LLMResponse:
        remaining = (self.context.deadline - datetime.now(UTC)).total_seconds()
        try:
            response = await asyncio.wait_for(
                provider.complete(request), timeout=max(remaining, 0)
            )
        except TimeoutError:
            raise _Stop(
                "prazo", "Não consegui concluir dentro do tempo limite."
            ) from None
        except LLMError as error:
            text = (
                "Não consegui falar com o modelo agora. Tente de novo em instantes."
                if error.retryable
                else "Não consegui gerar uma resposta para este pedido."
            )
            raise _Stop(f"LLMError: {error}", text) from None
        self.usage += response.usage
        self.model = response.model
        return response

    def _notify(self) -> None:
        if self._on_state is not None:
            self._on_state(self.context, self.state)


def _unavailable(call: ToolCall) -> Message:
    return Message(role=Role.TOOL, content=TOOL_UNAVAILABLE, tool_call_id=call.id)
