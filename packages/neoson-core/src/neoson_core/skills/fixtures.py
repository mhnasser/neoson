"""Casos de referência versionados junto da skill; só dados, sem scripts."""

from datetime import datetime

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from neoson_core.engine import (
    RunStatus,
    ToolCall,
    ToolCatalog,
    ToolContext,
    ToolRejected,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from neoson_core.engine.tools import RiskLevel
from neoson_core.skills.loader import SkillPackage


class FixtureInvalid(ValueError):
    """Fixture inválida; a mensagem identifica o arquivo sem expor seu conteúdo."""


class ToolFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    arguments: dict[str, object] | None = None
    data: dict[str, object] = Field(default_factory=dict)
    status: ToolResultStatus = ToolResultStatus.OK


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: RunStatus = RunStatus.COMPLETED
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    not_contains: tuple[str, ...] = ()


class SkillCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    input: str = Field(min_length=1, max_length=8000)
    now: datetime
    tools: dict[str, ToolFixture] = Field(default_factory=dict)
    expect: Expectations

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("o relógio da fixture precisa de fuso horário")
        return value


def load_cases(package: SkillPackage) -> tuple[SkillCase, ...]:
    cases: list[SkillCase] = []
    for name, text in sorted(package.contents.items()):
        if not name.startswith("tests/fixtures/") or not name.endswith(
            (".yaml", ".yml")
        ):
            continue
        try:
            case = SkillCase.model_validate(yaml.safe_load(text))
        except (yaml.YAMLError, ValidationError):
            raise FixtureInvalid(f"Fixture inválida: {name}.") from None
        if any(previous.id == case.id for previous in cases):
            raise FixtureInvalid(f"ID de caso repetido: {case.id}.")
        if set(case.tools) - set(package.metadata.allowed_tools):
            raise FixtureInvalid(f"Fixture {name} declara ferramentas fora da skill.")
        cases.append(case)
    if not cases:
        raise FixtureInvalid("A versão aprovada não contém fixtures YAML de avaliação.")
    return tuple(cases)


class FixtureTools:
    """Valida pelos schemas reais, mas nunca executa a implementação da ferramenta."""

    def __init__(self, catalog: ToolCatalog, fixtures: dict[str, ToolFixture]) -> None:
        self._catalog = catalog
        self._fixtures = fixtures
        self.called: list[str] = []
        self.problems: list[str] = []
        known = {spec.name for spec in catalog.specs()}
        if set(fixtures) - known:
            raise FixtureInvalid("Fixture declara uma ferramenta não registrada.")

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            spec for spec in self._catalog.specs() if spec.name in self._fixtures
        )

    def check(self, call: ToolCall) -> RiskLevel:
        risk = self._catalog.check(call)
        if call.name not in self._fixtures:
            raise ToolRejected("Ferramenta sem resposta configurada na fixture.")
        return risk

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        fixture = self._fixtures[call.name]
        self.called.append(call.name)
        if fixture.arguments is not None and dict(call.arguments) != fixture.arguments:
            self.problems.append(f"Argumentos diferentes da fixture: {call.name}.")
            return ToolResult(
                call.id, ToolResultStatus.ERROR, "Argumentos inesperados."
            )
        return ToolResult(call.id, fixture.status, fixture.summary, fixture.data)
