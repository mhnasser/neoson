"""Avalia a cópia aprovada em bancos descartáveis e guarda somente as medições."""

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID, uuid4

from neoson_core.engine import (
    Engine,
    OutboxEvent,
    RunResult,
    SkillRegistry,
    ToolCatalog,
    Usage,
    UserMessage,
)
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect, transaction
from neoson_core.skills.fixtures import FixtureTools, SkillCase, load_cases
from neoson_core.skills.loader import SkillPackage
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.telemetry.pricing import Price, cost_usd

EngineFactory = Callable[[Path, SkillRegistry, ToolCatalog, datetime], Engine]
CatalogFactory = Callable[[Path], ToolCatalog]


@dataclass(frozen=True, slots=True)
class CaseResult:
    id: str
    passed: bool
    reasons: tuple[str, ...]
    outcome: str
    called_tools: tuple[str, ...]
    requested_tools: tuple[str, ...]
    usage: Usage
    model: str | None
    cost_usd: float | None
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class Evaluation:
    id: UUID
    skill_id: str
    skill_version: str
    checksum: str
    evaluated_at: datetime
    cases: tuple[CaseResult, ...]
    duration_seconds: float

    @property
    def passed_cases(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def usage(self) -> Usage:
        total = Usage()
        for case in self.cases:
            total += case.usage
        return total

    @property
    def cost_usd(self) -> float | None:
        costs = [case.cost_usd for case in self.cases]
        return (
            None
            if any(cost is None for cost in costs)
            else round(sum(c for c in costs if c is not None), 8)
        )


class SkillEvaluator:
    def __init__(
        self,
        path: Path,
        engine_factory: EngineFactory,
        catalog_factory: CatalogFactory,
        prices: Mapping[str, Price] | None = None,
    ) -> None:
        self._path = path
        self._engine = engine_factory
        self._catalog = catalog_factory
        self._prices = dict(prices or {})

    async def evaluate(self, skill_id: str) -> Evaluation:
        package = SqliteSkillRegistry(self._path).approved_package(skill_id)
        report = await self.evaluate_package(package)
        self._save(report)
        return report

    async def evaluate_package(self, package: SkillPackage) -> Evaluation:
        """Avalia snapshot em réplicas descartáveis, sem aprová-lo na origem."""
        cases = load_cases(package)
        results = []
        started = perf_counter()
        with TemporaryDirectory(prefix="neoson-skill-eval-") as temporary:
            for number, case in enumerate(cases):
                database = Path(temporary) / f"case-{number}.db"
                registry = SqliteSkillRegistry(database)
                # A cópia já foi aprovada na origem; a réplica só existe neste teste.
                registry.approve(package)
                tools = FixtureTools(self._catalog(database), case.tools)
                engine = self._engine(database, registry, tools, case.now)
                before = perf_counter()
                result = await engine.run(
                    UserMessage(
                        text=f"Use a skill {package.metadata.id} nesta tarefa:\n"
                        + case.input,
                        channel="skill_eval",
                    )
                )
                elapsed = perf_counter() - before
                requested, loaded = _observed(database, result.run_id)
                reasons = _reasons(case, result, tools, requested)
                if loaded != [(package.metadata.id, package.metadata.version)]:
                    reasons.append("A versão avaliada da skill não foi carregada.")
                results.append(
                    CaseResult(
                        id=case.id,
                        passed=not reasons,
                        reasons=tuple(reasons),
                        outcome=result.status.value,
                        called_tools=tuple(tools.called),
                        requested_tools=tuple(requested),
                        usage=result.usage,
                        model=result.model,
                        cost_usd=cost_usd(
                            result.model,
                            result.usage.input_tokens,
                            result.usage.output_tokens,
                            result.usage.cached_tokens,
                            self._prices,
                        ),
                        duration_seconds=elapsed,
                    )
                )
        return Evaluation(
            uuid4(),
            package.metadata.id,
            package.metadata.version,
            package.checksum,
            datetime.now(UTC),
            tuple(results),
            perf_counter() - started,
        )

    def _save(self, report: Evaluation) -> None:
        connection = connect(self._path)
        try:
            with transaction(connection):
                connection.execute(
                    "INSERT INTO skill_evaluations"
                    " (id, skill_id, skill_version, checksum, evaluated_at,"
                    " total_cases, passed_cases, input_tokens, output_tokens,"
                    " cached_tokens, cost_usd, duration_seconds, cases_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(report.id),
                        report.skill_id,
                        report.skill_version,
                        report.checksum,
                        report.evaluated_at.isoformat(),
                        len(report.cases),
                        report.passed_cases,
                        report.usage.input_tokens,
                        report.usage.output_tokens,
                        report.usage.cached_tokens,
                        report.cost_usd,
                        report.duration_seconds,
                        json.dumps(
                            [asdict(case) for case in report.cases], ensure_ascii=False
                        ),
                    ),
                )
                outbox.insert(
                    connection,
                    OutboxEvent(
                        type="neoson.skill.evaluated",
                        payload={
                            "evaluation_id": str(report.id),
                            "skill_id": report.skill_id,
                            "version": report.skill_version,
                            "passed_cases": report.passed_cases,
                            "total_cases": len(report.cases),
                        },
                    ),
                )
        finally:
            connection.close()


def _observed(path: Path, run_id: UUID) -> tuple[list[str], list[tuple[str, str]]]:
    connection = connect(path)
    try:
        requested = [
            call["name"]
            for row in connection.execute(
                "SELECT tool_calls FROM messages WHERE run_id = ? ORDER BY id",
                (str(run_id),),
            )
            for call in json.loads(row["tool_calls"] or "[]")
            if call["name"] != "skill_load"
        ]
        loaded = [
            (row[0], row[1])
            for row in connection.execute(
                "SELECT skill_id, skill_version FROM skill_executions WHERE run_id = ?",
                (str(run_id),),
            )
        ]
    finally:
        connection.close()
    return requested, loaded


def _reasons(
    case: SkillCase, result: RunResult, tools: FixtureTools, requested: list[str]
) -> list[str]:
    expected = case.expect
    reasons = list(tools.problems)
    if result.status != expected.state:
        reasons.append(
            f"Estado esperado {expected.state.value}; recebido {result.status.value}."
        )
    for tool in expected.required_tools:
        if tool not in tools.called:
            reasons.append(f"Ferramenta obrigatória não executada: {tool}.")
    for tool in expected.forbidden_tools:
        if tool in tools.called or tool in requested:
            reasons.append(f"Ferramenta proibida solicitada: {tool}.")
    text = result.text.casefold()
    for index, required in enumerate(expected.contains, 1):
        if required.casefold() not in text:
            reasons.append(f"Texto obrigatório ausente (critério {index}).")
    for index, forbidden in enumerate(expected.not_contains, 1):
        if forbidden.casefold() in text:
            reasons.append(f"Texto proibido presente (critério {index}).")
    return reasons
