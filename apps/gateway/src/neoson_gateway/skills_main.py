"""Comando `neoson-skills`: valida as skills da biblioteca (ADR-007)."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_core.skills.loader import SkillInvalid, load_library, load_skill
from neoson_gateway.composition import TOOL_NAMES


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-skills", description="Skills do NeoSon (ADR-007)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser(
        "validate", help="valida uma skill ou toda a biblioteca"
    )
    validate.add_argument(
        "paths", nargs="*", type=Path, help="padrão: NEOSON_SKILLS_PATH"
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    return _validate(args.paths, settings.skills_path)


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
