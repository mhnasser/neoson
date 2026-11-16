"""Comando `neoson-maintenance`: checagens e mudança de lugar dos dados."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from neoson_core.infrastructure.config import ConfigError, load_settings
from neoson_core.infrastructure.maintenance import MaintenanceError, check, relocate

MARK = {"ok": "ok", "warning": "atenção", "error": "ERRO"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-maintenance", description="Janela de manutenção do NeoSon."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="confere esquema, integridade, pastas e backup")
    move = commands.add_parser(
        "relocate", help="copia banco e documentos para outra pasta e confere"
    )
    move.add_argument("--to", type=Path, required=True)
    move.add_argument(
        "--retire-original",
        action="store_true",
        help="renomeia o banco antigo (.movido-…) depois da cópia conferida",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    if args.command == "check":
        findings = check(
            settings.db_path, settings.backup_path, settings.documents_path
        )
        for finding in findings:
            print(f"[{MARK[finding.level]}] {finding.message}")
        return 1 if any(f.level == "error" for f in findings) else 0
    try:
        moved = relocate(
            settings.db_path,
            args.to,
            settings.documents_path,
            retire_original=args.retire_original,
        )
    except MaintenanceError as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    print(f"Banco copiado e conferido em {moved.db_path}")
    if moved.documents_path is not None:
        print(f"Documentos copiados em {moved.documents_path}")
    if moved.retired is not None:
        print(f"Banco antigo renomeado para {moved.retired}")
    print("Ajuste o neoson.env antes de subir os serviços:")
    print(f"  NEOSON_DB_PATH={moved.db_path}")
    if moved.documents_path is not None:
        print(f"  NEOSON_DOCUMENTS_PATH={moved.documents_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
