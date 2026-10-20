"""Comando `neoson-backup`: cria, verifica e restaura backups do banco e do vault."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from neoson_core.infrastructure import backup
from neoson_core.infrastructure.config import ConfigError, load_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-backup",
        description="Backup e restauração do SQLite e do vault do NeoSon.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="cria um backup novo")
    create.add_argument("--dest", type=Path, help="padrão: NEOSON_BACKUP_PATH")
    check = commands.add_parser("verify", help="confere um backup")
    check.add_argument("path", type=Path)
    restore = commands.add_parser(
        "restore", help="restaura um backup (pare o gateway e o worker antes)"
    )
    restore.add_argument("path", type=Path)
    restore.add_argument(
        "--force",
        action="store_true",
        help="se já houver dados, guarda-os de lado (renomeia) e restaura",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as error:
        raise SystemExit(str(error)) from None

    try:
        if args.command == "create":
            info = backup.create(
                settings.db_path, settings.vault_path, args.dest or settings.backup_path
            )
            print(f"Backup criado em {info.path}")
            _describe(info)
        elif args.command == "verify":
            _describe(backup.verify(args.path))
            print("Backup íntegro.")
        else:
            kept = backup.restore(
                args.path, settings.db_path, settings.vault_path, force=args.force
            )
            for path in kept:
                print(f"Guardado de lado: {path}")
            print(f"Restaurado de {args.path}")
    except backup.BackupError as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


def _describe(info: backup.BackupInfo) -> None:
    counts = ", ".join(f"{table}={count}" for table, count in info.counts.items())
    print(
        f"  criado em {info.created_at:%Y-%m-%d %H:%M} UTC"
        f" | esquema v{info.schema_version} | {counts}"
        f" | vault: {info.vault_files} arquivo(s)"
    )
