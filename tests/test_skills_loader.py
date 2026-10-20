import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from neoson_core.engine import SkillRisk
from neoson_core.infrastructure.config import load_settings
from neoson_core.memory.curated import SqliteCuratedMemory
from neoson_core.skills.loader import SkillInvalid, load_library, load_skill
from neoson_gateway.composition import TOOL_NAMES, build_tools
from neoson_gateway.skills_main import main

LIBRARY = Path(__file__).resolve().parents[1] / "skill_library"

METADATA = """\
id: resumir
version: 1.2.0
description: Resume um texto longo em poucos pontos.
triggers: [resumir, resuma isto]
allowed_tools: [calendar_list]
requires_approval: false
risk_level: low
owner: neoson
"""
INSTRUCTIONS = """\
# Resumir

## Quando usar
Quando pedirem um resumo.

## Passos
1. Leia o texto.

## Restrições
- Não invente.

## Saída
- Até 5 pontos.
"""


def make_skill(root: Path, name: str = "resumir") -> Path:
    folder = root / name
    (folder / "tests" / "fixtures").mkdir(parents=True)
    # Bytes, para não depender da quebra de linha padrão do sistema.
    (folder / "metadata.yaml").write_bytes(METADATA.encode())
    (folder / "SKILL.md").write_bytes(INSTRUCTIONS.encode())
    (folder / "tests" / "fixtures" / "caso.yaml").write_bytes(b"input: resuma\n")
    return folder


def replace(file: str, old: str, new: str) -> Callable[[Path], None]:
    def change(folder: Path) -> None:
        path = folder / file
        text = path.read_bytes().decode()
        assert old in text
        path.write_bytes(text.replace(old, new).encode())

    return change


def write(file: str, content: bytes) -> Callable[[Path], None]:
    def change(folder: Path) -> None:
        (folder / file).parent.mkdir(parents=True, exist_ok=True)
        (folder / file).write_bytes(content)

    return change


def remove(file: str) -> Callable[[Path], None]:
    return lambda folder: (folder / file).unlink()


def test_a_valid_skill_is_loaded_with_its_metadata(tmp_path: Path) -> None:
    package = load_skill(make_skill(tmp_path), TOOL_NAMES)

    skill = package.skill()
    assert (skill.ref.id, skill.ref.version) == ("resumir", "1.2.0")
    assert skill.ref.triggers == ("resumir", "resuma isto")
    assert skill.allowed_tools == {"calendar_list"}
    assert skill.risk_level is SkillRisk.LOW and not skill.requires_approval
    assert skill.instructions.startswith("# Resumir") and skill.checksum
    assert package.files == ("SKILL.md", "metadata.yaml", "tests/fixtures/caso.yaml")


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (remove("SKILL.md"), "falta SKILL.md"),
        (remove("metadata.yaml"), "falta metadata.yaml"),
        (replace("metadata.yaml", "id: resumir", "id: outro"), "nome da pasta"),
        (replace("metadata.yaml", "1.2.0", "1.2"), "version"),
        (
            replace("metadata.yaml", "[calendar_list]", "[shell]"),
            "desconhecidas: shell",
        ),
        (
            replace(
                "metadata.yaml", "[calendar_list]", "[calendar_list, calendar_list]"
            ),
            "repetidas",
        ),
        (replace("metadata.yaml", "risk_level: low", "risk_level: zero"), "risk_level"),
        (
            replace("metadata.yaml", "owner: neoson", "owner: neoson\nstatus: ok"),
            "status",
        ),
        (replace("metadata.yaml", "[resumir, resuma isto]", "[]"), "triggers"),
        (write("metadata.yaml", b"id: [aberto"), "YAML inválido"),
        (write("metadata.yaml", b"- uma lista"), "mapeamento"),
        (replace("SKILL.md", "## Restrições", "## Outra"), "'## Restrições'"),
        (write("SKILL.md", INSTRUCTIONS.encode() + b"x" * 8000), "máximo 8000"),
        (write("SKILL.md", "Restrições".encode("latin-1")), "UTF-8"),
        (write("scripts/extrair.py", b"print(1)"), "scripts de skill"),
        (write("scripts/notas.txt", b"solta"), "scripts/notas.txt: fora de"),
        (write("examples/extrair.py", b"print(1)"), "scripts de skill"),
        (write("notas.md", b"solta"), "notas.md: fora de"),
    ],
)
def test_each_invalid_skill_is_rejected_with_the_reason(
    tmp_path: Path, change: Callable[[Path], None], problem: str
) -> None:
    folder = make_skill(tmp_path)
    change(folder)

    with pytest.raises(SkillInvalid) as raised:
        load_skill(folder, TOOL_NAMES)

    assert any(problem in p for p in raised.value.problems), raised.value.problems


def test_all_problems_are_reported_together(tmp_path: Path) -> None:
    folder = make_skill(tmp_path)
    replace("metadata.yaml", "1.2.0", "um")(folder)
    replace("SKILL.md", "## Saída", "## Fim")(folder)

    with pytest.raises(SkillInvalid) as raised:
        load_skill(folder, TOOL_NAMES)

    assert len(raised.value.problems) == 2


def test_tool_names_are_only_checked_when_known_tools_are_given(tmp_path: Path) -> None:
    folder = make_skill(tmp_path)
    replace("metadata.yaml", "[calendar_list]", "[shell]")(folder)

    assert load_skill(folder).metadata.allowed_tools == ("shell",)


def test_checksum_covers_every_file_but_not_line_endings(tmp_path: Path) -> None:
    folder = make_skill(tmp_path)
    original = load_skill(folder).checksum

    crlf = folder / "SKILL.md"
    crlf.write_bytes(crlf.read_bytes().replace(b"\n", b"\r\n"))
    assert load_skill(folder).checksum == original

    write("tests/fixtures/caso.yaml", b"input: resuma isto\n")(folder)
    assert load_skill(folder).checksum != original


def test_the_library_separates_valid_skills_from_errors(tmp_path: Path) -> None:
    make_skill(tmp_path, "resumir")
    broken = make_skill(tmp_path, "quebrada")

    valid, errors = load_library(tmp_path, TOOL_NAMES)

    assert [p.metadata.id for p in valid] == ["resumir"]
    assert [e.path for e in errors] == [broken]
    assert load_library(tmp_path / "nao-existe") == ([], [])


def test_the_skills_in_the_repository_are_valid() -> None:
    valid, errors = load_library(LIBRARY, TOOL_NAMES)

    assert not errors, [str(e) for e in errors]
    assert {p.metadata.id for p in valid} >= {"planejar_dia", "retomar_assunto"}


def test_skills_may_only_declare_the_tools_the_gateway_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "neoson.db"))
    settings = load_settings(env_file=None)
    tools = build_tools(
        settings, SqliteCuratedMemory(settings.db_path), lambda: datetime.now(UTC)
    )

    assert {spec.name for spec in tools.specs()} == set(TOOL_NAMES)


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    library = tmp_path / "biblioteca"
    library.mkdir()
    monkeypatch.setenv("NEOSON_SKILLS_PATH", str(library))
    return library


def test_command_line_validates_the_library(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for skill in ("planejar_dia", "retomar_assunto"):
        shutil.copytree(LIBRARY / skill, cli_env / skill)

    assert main(["validate"]) == 0
    out = capsys.readouterr().out
    assert "ok   planejar_dia 1.0.0" in out and "ok   retomar_assunto 1.0.0" in out


def test_command_line_reports_problems_and_fails(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = make_skill(cli_env)
    replace("metadata.yaml", "[calendar_list]", "[shell]")(folder)

    assert main(["validate", str(folder)]) == 1
    assert "ferramentas desconhecidas: shell" in capsys.readouterr().err


def test_command_line_rejects_a_repeated_id(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = make_skill(cli_env / "a")
    second = make_skill(cli_env / "b")

    assert main(["validate", str(first), str(second)]) == 1
    assert "repetido" in capsys.readouterr().err


def test_command_line_fails_on_an_empty_library(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate"]) == 1
    assert "Nenhuma skill" in capsys.readouterr().err
