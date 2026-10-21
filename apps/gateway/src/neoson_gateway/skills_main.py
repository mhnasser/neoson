"""Comando `neoson-skills`: validação e aprovação explícita de versões (ADR-007)."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from neoson_core.engine import LLMError
from neoson_core.engine.skills import SkillNotFound
from neoson_core.infrastructure.config import ConfigError, ServerSettings, load_settings
from neoson_core.skills.fixtures import FixtureInvalid
from neoson_core.skills.loader import SkillInvalid, load_library, load_skill
from neoson_core.skills.proposals import (
    ProposalConflict,
    ProposalInvalid,
    ProposalNotFound,
)
from neoson_core.skills.registry import (
    SkillConflict,
    SkillIntegrityError,
    SqliteSkillRegistry,
)
from neoson_gateway.composition import TOOL_NAMES
from neoson_gateway.skills_eval import evaluate_skill
from neoson_gateway.skills_review import add_commands, execute


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-skills", description="Skills do NeoSon (ADR-007)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add_commands(commands)
    validate = commands.add_parser(
        "validate", help="valida uma skill ou toda a biblioteca"
    )
    validate.add_argument(
        "paths", nargs="*", type=Path, help="padrão: NEOSON_SKILLS_PATH"
    )
    approve = commands.add_parser("approve", help="aprova e ativa uma cópia da skill")
    approve.add_argument("path", type=Path)
    commands.add_parser("list", help="lista as versões aprovadas e seus estados")
    evaluate = commands.add_parser("eval", help="avalia as fixtures da versão aprovada")
    evaluate.add_argument("skill_id")
    for name, help_text in (
        ("rollback", "reativa a versão aprovada anterior"),
        ("retire", "retira a versão ativa, preservando seu histórico"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("skill_id")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    if args.command == "validate":
        return _validate(args.paths, settings.skills_path)
    try:
        if args.command == "proposal":
            return execute(args, settings)
        if args.command == "eval":
            return _evaluate(settings, args.skill_id)
        # Valida antes de abrir o banco: uma pasta inválida não altera o registro.
        package = (
            load_skill(args.path, TOOL_NAMES) if args.command == "approve" else None
        )
        registry = SqliteSkillRegistry(settings.db_path)
        if package is not None:
            version = registry.approve(package)
            print(
                f"Aprovada {version.ref.id} {version.ref.version}"
                f" ({version.checksum[:12]})"
            )
        elif args.command == "list":
            versions = registry.versions()
            if not versions:
                print("Nenhuma versão aprovada.")
            for version in versions:
                print(
                    f"{version.ref.id} {version.ref.version} {version.status}"
                    f" {version.checksum[:12]}"
                )
        elif args.command == "rollback":
            version = registry.rollback(args.skill_id)
            print(f"Ativa {version.ref.id} {version.ref.version}")
        elif args.command == "retire":
            registry.retire(args.skill_id)
            print(f"Retirada {args.skill_id}")
    except (
        ConfigError,
        FixtureInvalid,
        SkillInvalid,
        SkillConflict,
        SkillIntegrityError,
        SkillNotFound,
        ProposalConflict,
        ProposalInvalid,
        ProposalNotFound,
        LLMError,
    ) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


def _evaluate(settings: ServerSettings, skill_id: str) -> int:
    report = asyncio.run(evaluate_skill(settings, skill_id))
    for case in report.cases:
        print(f"{'PASS' if case.passed else 'FAIL'} {case.id}")
        for reason in case.reasons:
            print(f"  - {reason}")
    cost = "não informado" if report.cost_usd is None else f"USD {report.cost_usd:.8f}"
    print(
        f"Avaliação {report.id}: {report.passed_cases}/{len(report.cases)} aprovados; "
        f"{report.usage.total_tokens} tokens; {report.duration_seconds:.2f}s; "
        f"custo {cost}."
    )
    return 0 if report.passed_cases == len(report.cases) else 1


def _validate(paths: list[Path], library: Path) -> int:
    if paths:
        valid, errors = [], []
        for path in paths:
            try:
                valid.append(load_skill(path, TOOL_NAMES))
            except SkillInvalid as error:
                errors.append(error)
    else:
        valid, errors = load_library(library, TOOL_NAMES)
        if not valid and not errors:
            print(f"Nenhuma skill em {library}", file=sys.stderr)
            return 1

    seen: dict[str, Path] = {}
    for package in valid:
        meta = package.metadata
        if meta.id in seen:
            errors.append(
                SkillInvalid(
                    package.path, [f"id '{meta.id}' repetido em {seen[meta.id]}"]
                )
            )
            continue
        seen[meta.id] = package.path
        print(f"ok   {meta.id} {meta.version}  checksum {package.checksum[:12]}")
    for invalid in errors:
        print(f"ERRO {invalid.path}", file=sys.stderr)
        for problem in invalid.problems:
            print(f"     - {problem}", file=sys.stderr)
    return 1 if errors else 0
