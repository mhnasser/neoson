"""Tool Registry: descreve, valida argumentos e executa ferramentas (ADR-003, D15)."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from neoson_core.engine import ToolCall, ToolResult, ToolResultStatus, ToolSpec
from neoson_core.engine.tools import RiskLevel, ToolContext, ToolRejected

logger = logging.getLogger(__name__)

TOOL_FAILED = "A ferramenta falhou ao executar este pedido."


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """O que a ferramenta produz; o registro acrescenta o `call_id`."""

    summary: str
    data: Mapping[str, object] = field(default_factory=dict)
    status: ToolResultStatus = ToolResultStatus.OK


class Tool[ArgsT: BaseModel](ABC):
    """Ferramenta tipada: o schema enviado ao modelo vem do modelo de argumentos."""

    name: ClassVar[str]
    description: ClassVar[str]
    risk: ClassVar[RiskLevel]
    arguments: type[ArgsT]

    @abstractmethod
    async def run(self, arguments: ArgsT, context: ToolContext) -> ToolOutput: ...

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.arguments.model_json_schema(),
        )


class ToolRegistry:
    """Implementa `ToolCatalog`. A decisão de executar é da Policy Engine."""

    def __init__(self, tools: Iterable[Tool[Any]]) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Ferramenta registrada duas vezes: {tool.name}.")
            self._tools[tool.name] = tool

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec() for tool in self._tools.values())

    def check(self, call: ToolCall) -> RiskLevel:
        tool = self._tool(call)
        self._arguments(tool, call)
        return tool.risk

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        tool = self._tool(call)
        arguments = self._arguments(tool, call)
        try:
            output = await tool.run(arguments, context)
        except Exception:
            logger.warning("ferramenta %s falhou no run %s", call.name, context.run_id)
            return ToolResult(
                call_id=call.id, status=ToolResultStatus.ERROR, summary=TOOL_FAILED
            )
        return ToolResult(
            call_id=call.id,
            status=output.status,
            summary=output.summary,
            data=output.data,
        )

    def _tool(self, call: ToolCall) -> Tool[Any]:
        tool = self._tools.get(call.name)
        if tool is None:
            raise ToolRejected(f"Ferramenta desconhecida: {call.name}.")
        return tool

    def _arguments(self, tool: Tool[Any], call: ToolCall) -> BaseModel:
        try:
            arguments: BaseModel = tool.arguments.model_validate(dict(call.arguments))
            return arguments
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in d['loc']) or 'argumentos'}: {d['msg']}"
                for d in error.errors(include_input=False, include_url=False)
            )
            raise ToolRejected(
                f"Argumentos inválidos para {call.name}: {problems}."
            ) from None
