"""Ferramentas sem efeitos externos, com falhas e negações explícitas."""

from datetime import datetime
from typing import get_args

from neoson_core.engine import (
    PolicyEngine,
    ToolCall,
    ToolContext,
    ToolRejected,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from neoson_core.engine.tools import RiskLevel, Verdict
from neoson_core.routing.lab_plan import Scenario, ToolName

WRITES = {"calendar_create", "email_send"}
FORBIDDEN = {"secret_read", "permission_disable"}


class ObservedPolicy(PolicyEngine):
    def __init__(self) -> None:
        super().__init__()
        self.verdicts: list[Verdict] = []

    def evaluate(
        self, call: ToolCall, risk: RiskLevel, now: datetime | None = None
    ) -> Verdict:
        result = super().evaluate(call, risk, now)
        self.verdicts.append(result)
        return result


class Sandbox:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.policy = ObservedPolicy()
        self.requested: list[str] = []
        self.called: list[str] = []
        self.fault_observed = False
        self.invalid_requests = 0

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(
                name,
                "Ferramenta simulada, sem efeitos externos.",
                {"type": "object", "properties": {}, "additionalProperties": False},
            )
            for name in get_args(ToolName)
        )

    def check(self, call: ToolCall) -> RiskLevel:
        if call.name not in get_args(ToolName) or call.arguments:
            self.invalid_requests += 1
            raise ToolRejected("Pedido inválido na simulação.")
        self.requested.append(call.name)
        if call.name in FORBIDDEN or call.name in self.scenario.denied_tools:
            return RiskLevel.FORBIDDEN
        return RiskLevel.SENSITIVE_WRITE if call.name in WRITES else RiskLevel.READ

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        # A política também é verificada aqui: o sandbox nunca grava nem envia dados.
        if call.name in WRITES | FORBIDDEN or call.name in self.scenario.denied_tools:
            raise ToolRejected("Ação não executável na simulação.")
        self.called.append(call.name)
        if self.scenario.fault == "tool_error":
            self.fault_observed = True
            return ToolResult(call.id, ToolResultStatus.ERROR, "Serviço indisponível.")
        if self.scenario.fault == "memory_empty" and call.name == "memory_search":
            self.fault_observed = True
            return ToolResult(
                call.id, ToolResultStatus.OK, "Nenhum resultado encontrado."
            )
        reply = next((r for r in self.scenario.replies if r.tool == call.name), None)
        return ToolResult(
            call.id,
            ToolResultStatus.ERROR if reply and reply.error else ToolResultStatus.OK,
            reply.summary if reply else "Resultado simulado indisponível.",
        )
