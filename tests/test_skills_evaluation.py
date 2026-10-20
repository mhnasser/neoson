import asyncio
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from neoson_core.engine import (
    AgentLoop,
    Budget,
    Engine,
    LLMError,
    LLMResponse,
    OutboxEvent,
    RunStatus,
    SkillRegistry,
    ToolCall,
    ToolCatalog,
    ToolContext,
    ToolResult,
    ToolSpec,
    Usage,
)
from neoson_core.engine.tools import RiskLevel
from neoson_core.infrastructure import outbox
from neoson_core.infrastructure.db import connect
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider, call_tool, reply
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.evaluation import SkillEvaluator
from neoson_core.skills.fixtures import FixtureInvalid, load_cases
from neoson_core.skills.loader import SkillPackage, load_skill, package_from_texts
from neoson_core.skills.registry import SqliteSkillRegistry
from neoson_core.telemetry.pricing import Price
from neoson_core.tools.calendar_fake import FakeCalendar
from neoson_core.tools.registry import ToolRegistry
from neoson_gateway.skills_main import main

LIBRARY = Path(__file__).resolve().parents[1] / "skill_library"
CASE = """id: agenda
input: Planeje o dia 2026-10-21. DADO_PRIVADO_DO_CASO
now: '2026-10-20T09:00:00-03:00'
tools:
  calendar_list:
    arguments: {day: '2026-10-21'}
    summary: '09:00 Planejamento (60 min)'
expect:
  state: completed
  required_tools: [calendar_list]
  forbidden_tools: [memory_save]
  contains: ['09:00', Planejamento]
  not_contains: [Inventado]
"""
INSTRUCTIONS = """# Agenda
## Quando usar
Para planejar o dia.
## Passos
Leia a agenda.
## Restrições
Não invente.
## Saída
Compromissos em ordem.
"""


def package(text: str = CASE) -> SkillPackage:
    return package_from_texts(
        Path("planejar"),
        {
            "metadata.yaml": "id: planejar\nversion: 1.0.0\n"
            "description: Planeja um dia com a agenda.\ntriggers: [planejar]\n"
            "allowed_tools: [calendar_list]\nrisk_level: low\nowner: neoson\n",
            "SKILL.md": INSTRUCTIONS,
            "tests/fixtures/agenda.yaml": text,
        },
    )


class NeverExecute:
    def __init__(self) -> None:
        self.catalog = ToolRegistry([FakeCalendar.fixed({})])

    def specs(self) -> tuple[ToolSpec, ...]:
        return self.catalog.specs()

    def check(self, call: ToolCall) -> RiskLevel:
        return self.catalog.check(call)

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        raise AssertionError("Não executar a implementação real da ferramenta.")


def script(
    text: str = "09:00 Planejamento.", day: str = "2026-10-21"
) -> list[LLMResponse | LLMError]:
    return [
        call_tool("skill_load", {"skill_id": "planejar", "version": "1.0.0"}, "load"),
        call_tool("calendar_list", {"day": day}, "calendar"),
        reply(text, Usage(20, 3, 5)),
    ]


def evaluator(
    db: Path, steps: list[LLMResponse | LLMError], *, priced: bool = False
) -> SkillEvaluator:
    def build(
        path: Path, skills: SkillRegistry, tools: ToolCatalog, now: datetime
    ) -> Engine:
        return AgentLoop(
            ScriptedProvider(steps),
            RulesRouter("balanced"),
            Budget(5, 5),
            skills=skills,
            tools=tools,
            clock=lambda: now,
            store=SqliteConversationStore(path),
        )

    return SkillEvaluator(
        db,
        build,
        lambda path: NeverExecute(),
        {"fake-model": Price(2, 4, 1)} if priced else None,
    )


def query(db: Path, sql: str) -> list[tuple[object, ...]]:
    connection = connect(db)
    try:
        return [tuple(row) for row in connection.execute(sql)]
    finally:
        connection.close()


def test_success_records_metrics_without_conversation_data(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    original = package()
    SqliteSkillRegistry(db).approve(original)
    report = asyncio.run(evaluator(db, script(), priced=True).evaluate("planejar"))
    assert report.passed_cases == 1 and report.cases[0].reasons == ()
    assert report.usage == Usage(40, 13, 5)
    assert report.cost_usd == pytest.approx(0.000127)
    assert report.duration_seconds >= report.cases[0].duration_seconds >= 0
    assert report.checksum == original.checksum
    assert query(db, "SELECT COUNT(*) FROM sessions") == [(0,)]
    assert query(db, "SELECT COUNT(*) FROM runs") == [(0,)]
    assert query(db, "SELECT COUNT(*) FROM messages") == [(0,)]
    assert query(db, "SELECT COUNT(*) FROM skill_executions") == [(0,)]
    ((total, passed, cases),) = query(
        db, "SELECT total_cases, passed_cases, cases_json FROM skill_evaluations"
    )
    assert (total, passed) == (1, 1)
    assert "DADO_PRIVADO_DO_CASO" not in str(cases) and INSTRUCTIONS not in str(cases)
    assert json.loads(str(cases))[0]["called_tools"] == ["calendar_list"]
    assert query(
        db, "SELECT COUNT(*) FROM outbox_events WHERE type = 'neoson.skill.evaluated'"
    ) == [(1,)]


def test_failure_reports_objective_reasons_and_unknown_cost(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    SqliteSkillRegistry(db).approve(package())
    report = asyncio.run(evaluator(db, script("Inventado.")).evaluate("planejar"))
    assert report.passed_cases == 0 and report.cost_usd is None
    assert "Texto obrigatório ausente (critério 1)." in report.cases[0].reasons
    assert "Texto proibido presente (critério 1)." in report.cases[0].reasons
    assert query(db, "SELECT passed_cases, cost_usd FROM skill_evaluations") == [
        (0, None)
    ]


def test_different_tool_arguments_fail_even_if_answer_matches(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    SqliteSkillRegistry(db).approve(package())
    report = asyncio.run(evaluator(db, script(day="2026-10-22")).evaluate("planejar"))
    assert report.passed_cases == 0
    assert "Argumentos diferentes da fixture: calendar_list." in report.cases[0].reasons


def test_answer_without_loading_skill_or_calling_tool_fails(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    SqliteSkillRegistry(db).approve(package())
    report = asyncio.run(
        evaluator(db, [reply("09:00 Planejamento.")]).evaluate("planejar")
    )
    assert report.passed_cases == 0
    assert "A versão avaliada da skill não foi carregada." in report.cases[0].reasons
    assert (
        "Ferramenta obrigatória não executada: calendar_list."
        in report.cases[0].reasons
    )


def test_model_failure_is_a_measured_failed_case(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    SqliteSkillRegistry(db).approve(package())
    report = asyncio.run(
        evaluator(db, [LLMError("falha", retryable=True)]).evaluate("planejar")
    )
    assert report.passed_cases == 0 and report.cases[0].outcome == RunStatus.FAILED
    assert any("Estado esperado" in reason for reason in report.cases[0].reasons)


@pytest.mark.parametrize(
    "text",
    [
        "id: [quebrado",
        "[]",
        CASE.replace("09:00:00-03:00", "09:00:00"),
        CASE.replace("calendar_list:", "shell:"),
    ],
)
def test_invalid_fixtures_are_rejected_without_disclosing_input(text: str) -> None:
    with pytest.raises(FixtureInvalid) as caught:
        load_cases(package(text))
    assert "DADO_PRIVADO_DO_CASO" not in str(caught.value)


def test_duplicate_case_id_and_missing_fixtures_are_rejected() -> None:
    original = package()
    duplicate = package_from_texts(
        original.path, dict(original.contents) | {"tests/fixtures/outro.yaml": CASE}
    )
    with pytest.raises(FixtureInvalid, match="repetido"):
        load_cases(duplicate)
    without = package_from_texts(
        original.path,
        {
            name: text
            for name, text in original.contents.items()
            if not name.startswith("tests/")
        },
    )
    with pytest.raises(FixtureInvalid, match="não contém"):
        load_cases(without)


def test_fixtures_are_loaded_from_approved_copy(tmp_path: Path) -> None:
    db = tmp_path / "neoson.db"
    SqliteSkillRegistry(db).approve(package())
    # Não existe a pasta original no disco; as fixtures vêm do snapshot no banco.
    assert asyncio.run(evaluator(db, script()).evaluate("planejar")).passed_cases == 1


def test_catalog_update_and_report_event_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "neoson.db"
    SqliteSkillRegistry(db).approve(package())
    insert = outbox.insert

    def fail_report(connection: sqlite3.Connection, event: OutboxEvent) -> None:
        if event.type == "neoson.skill.evaluated":
            raise RuntimeError("evento indisponível")
        insert(connection, event)

    monkeypatch.setattr(outbox, "insert", fail_report)
    with pytest.raises(RuntimeError):
        asyncio.run(evaluator(db, script()).evaluate("planejar"))
    assert query(db, "SELECT COUNT(*) FROM skill_evaluations") == [(0,)]


def test_repository_skills_have_reference_cases() -> None:
    for name in ("planejar_dia", "retomar_assunto"):
        cases = load_cases(load_skill(LIBRARY / name))
        assert len(cases) == 2


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "neoson.db"
    monkeypatch.setenv("NEOSON_DB_PATH", str(db))
    monkeypatch.setenv("OPENAI_API_KEY", "chave-ficticia")
    monkeypatch.setenv("NEOSON_LLM_MODEL", "modelo-ficticio")
    return db


@pytest.mark.parametrize(
    "text,expected", [("09:00 Planejamento.", 0), ("Inventado.", 1)]
)
def test_cli_runs_evaluation_and_reports_outcome(
    cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
    expected: int,
) -> None:
    SqliteSkillRegistry(cli_env).approve(package())
    monkeypatch.setattr(
        "neoson_gateway.skills_eval.build_provider",
        lambda settings: ScriptedProvider(script(text)),
    )
    assert main(["eval", "planejar"]) == expected
    output = capsys.readouterr().out
    assert "Avaliação" in output and "custo não informado" in output
    assert ("PASS agenda" if expected == 0 else "FAIL agenda") in output
    assert "DADO_PRIVADO_DO_CASO" not in output


def test_cli_does_not_approve_a_missing_skill(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["eval", "ausente"]) == 1
    assert "não está ativa" in capsys.readouterr().err
    assert query(cli_env, "SELECT COUNT(*) FROM skills") == [(0,)]
