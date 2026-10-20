"""Skills da execução atual: índice estável, carregamento e limites de permissão."""

import json
from collections.abc import Sequence

from neoson_core.engine.llm import ToolSpec
from neoson_core.engine.messages import Message, Role, ToolCall
from neoson_core.engine.skills import Skill, SkillNotFound, SkillRef, SkillRegistry

SKILL_LOAD = "skill_load"
SKILL_UNAVAILABLE = "Skill ou versão indisponível nesta execução."
SKILL_TOOL_DENIED = "Ferramenta não permitida pela skill ativa."

_LOAD_SPEC = ToolSpec(
    name=SKILL_LOAD,
    description="Carrega uma skill aprovada do índice para a execução atual.",
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "string"},
            "version": {"type": "string"},
        },
        "required": ["skill_id", "version"],
        "additionalProperties": False,
    },
)


class SkillContext:
    """Estado por run; nunca compartilhado entre pedidos ou sessões."""

    def __init__(self, registry: SkillRegistry | None) -> None:
        self._registry = registry
        self._refs: tuple[SkillRef, ...] = ()
        self.active: Skill | None = None

    async def prepare(self) -> None:
        if self._registry is not None:
            self._refs = tuple(
                sorted(await self._registry.index(), key=lambda ref: ref.id)
            )

    def system_index(self) -> str:
        if not self._refs:
            return ""
        index = [
            {
                "id": ref.id,
                "version": ref.version,
                "description": ref.description,
                "triggers": ref.triggers,
            }
            for ref in self._refs
        ]
        return (
            "Skills aprovadas disponíveis nesta execução:\n"
            + json.dumps(index, ensure_ascii=False, sort_keys=True)
            + "\nSe o pedido corresponder a uma skill, carregue-a com skill_load antes "
            "de usar ferramentas. Uma skill por execução. Skills de execuções "
            "anteriores precisam ser recarregadas. A política de segurança continua "
            "valendo."
        )

    def specs(self, tools: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]:
        if self.active is not None:
            return tuple(
                spec for spec in tools if spec.name in self.active.allowed_tools
            )
        return (*tools, _LOAD_SPEC) if self._refs else tuple(tools)

    async def load(self, call: ToolCall) -> tuple[bool, list[Message]]:
        """Confirma a ação como resultado; o procedimento é instrução de sistema."""
        arguments = call.arguments
        if set(arguments) != {"skill_id", "version"} or not all(
            isinstance(value, str) and value.strip() for value in arguments.values()
        ):
            return False, [_ack(call, "Argumentos inválidos para skill_load.")]
        skill_id, version = str(arguments["skill_id"]), str(arguments["version"])
        if self.active is not None:
            if (self.active.ref.id, self.active.ref.version) == (skill_id, version):
                return True, [_ack(call, "Skill já carregada nesta execução.")]
            return False, [_ack(call, "Só uma skill pode ser carregada por execução.")]
        if self._registry is None or (skill_id, version) not in {
            (ref.id, ref.version) for ref in self._refs
        }:
            return False, [_ack(call, SKILL_UNAVAILABLE)]
        try:
            self.active = await self._registry.load_approved(skill_id, version)
        except SkillNotFound:
            return False, [_ack(call, SKILL_UNAVAILABLE)]
        return True, [
            _ack(call, f"Skill carregada nesta execução: {skill_id} {version}."),
            Message(
                role=Role.SYSTEM,
                content=f"Procedimento aprovado: {skill_id} {version}\n\n"
                + self.active.instructions,
            ),
        ]


def _ack(call: ToolCall, text: str) -> Message:
    return Message(role=Role.TOOL, tool_call_id=call.id, content=text)
