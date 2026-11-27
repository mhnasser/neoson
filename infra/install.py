"""Instala unidades de usuário no Linux; --dry-run não escreve nem executa comandos."""

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

# O Robot atual faz um pedido explícito; não é um daemon de captura contínua.
SERVICES = {
    "robot": (
        "neoson-robot",
        "neoson_robot.voice_main",
        "--device",
        "pipewire",
        "--activate",
    ),
    "gateway": ("neoson-gateway", "neoson_gateway.main"),
    "worker": ("neoson-worker", "neoson_worker.main"),
    "graph": ("neoson-worker", "neoson_worker.graph_main", "run"),
    "documents": ("neoson-worker", "neoson_worker.docs_main", "process"),
    "robot-touch": ("neoson-robot", "neoson_robot.touch_voice"),
}


def quoted(value: str) -> str:
    if any(ord(char) < 32 for char in value) or "\\" in value or value != value.strip():
        raise ValueError("Caminho contém caracteres não suportados pelo instalador.")
    return '"' + value.replace("%", "%%").replace('"', '\\"') + '"'


def plain(value: str) -> str:
    """Caminho para EnvironmentFile: o systemd não remove aspas e expande *?[."""
    quoted(value)  # mesmas recusas de controles, barra invertida e espaços nas pontas
    if any(char in value for char in '"*?['):
        raise ValueError("Caminho contém caracteres não suportados pelo instalador.")
    return value.replace("%", "%%")


def unit_text(service: str, checkout: Path, environment: Path) -> str:
    command = SERVICES[service]
    executable = (checkout / ".venv/bin/python").as_posix()
    # ':' desliga expansão de $ no ExecStart; %% mantém percentuais literais.
    start = ":" + quoted(executable) + " -m " + " ".join(command[1:])
    restart = (
        "Type=oneshot\n" if service == "robot" else "Restart=on-failure\nRestartSec=5\n"
    )
    return (
        "[Unit]\n"
        f"Description=NeoSon {service}\n"
        "\n[Service]\n"
        + restart
        + f"WorkingDirectory={checkout.as_posix().replace('%', '%%')}\n"
        + f"EnvironmentFile={plain(environment.as_posix())}\n"
        + "Environment=PYTHONUNBUFFERED=1\n"
        + "UMask=0077\nNoNewPrivileges=true\nTimeoutStopSec=30\n"
        + f"ExecStart={start}\n"
        + ("" if service == "robot" else "\n[Install]\nWantedBy=default.target\n")
    )


def install(
    checkout: Path,
    environment: Path,
    services: Sequence[str],
    *,
    repository: str | None,
    dry_run: bool,
    no_sync: bool,
) -> None:
    checkout, environment = checkout.expanduser(), environment.expanduser()
    if not dry_run:
        checkout, environment = checkout.absolute(), environment.absolute()
    if not all(path.as_posix().startswith("/") for path in (checkout, environment)):
        raise ValueError("Use caminhos Linux absolutos, inclusive na simulação.")
    units = Path.home() / ".config/systemd/user"
    rendered = {
        units / f"neoson-{name}.service": unit_text(name, checkout, environment)
        for name in dict.fromkeys(services)
    }
    commands: list[list[str]] = []
    if repository:
        url = urlsplit(repository)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Use uma URL HTTPS de repositório sem credenciais.")
        commands.append(["git", "clone", "--", repository, str(checkout)])
    if not no_sync:
        packages = sorted({SERVICES[name][0] for name in services})
        commands.append(
            [
                "uv",
                "sync",
                "--locked",
                "--no-dev",
                "--inexact",
                *[part for package in packages for part in ("--package", package)],
            ]
        )
    for command in commands:
        print(shlex.join(command))
    for path, content in rendered.items():
        print(f"\n{path.name}\n{content}")
    print("Ambiente privado: criar somente se ausente; preservar conteúdo existente.")
    print("systemctl --user daemon-reload")
    if dry_run:
        return
    if sys.platform != "linux":
        raise ValueError("Instalação real exige Linux; use --dry-run para simular.")
    if any(path.exists() or path.is_symlink() for path in rendered):
        raise ValueError(
            "Já existe uma unidade selecionada; nenhuma unidade foi substituída."
        )
    if repository and checkout.exists():
        raise ValueError(
            "O destino do clone já existe; omita --repository para reutilizá-lo."
        )
    if not repository and not (checkout / "uv.lock").is_file():
        raise ValueError("Checkout precisa conter uv.lock.")
    if not no_sync and (checkout / ".venv").exists():
        raise ValueError(
            "Ambiente já existente: prepare dependências separadamente e use --no-sync."
        )
    if no_sync and not (checkout / ".venv/bin/python").is_file():
        raise ValueError("--no-sync exige um ambiente Linux já preparado no checkout.")
    if environment.is_symlink() or (environment.exists() and not environment.is_file()):
        raise ValueError(
            "Arquivo de ambiente precisa ser um arquivo regular, sem link."
        )
    if environment.exists() and environment.stat().st_mode & 0o077:
        raise ValueError("Restrinja o ambiente a 0600 antes de instalar.")
    for command in commands:
        subprocess.run(
            command, cwd=None if command[0] == "git" else checkout, check=True
        )
    environment.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not environment.exists():
        with open(
            environment, "x", opener=lambda path, flags: os.open(path, flags, 0o600)
        ) as file:
            file.write("# Configure localmente; nunca envie este arquivo ao Git.\n")
    units.mkdir(parents=True, exist_ok=True)
    for path, content in rendered.items():
        with path.open("x", encoding="utf-8") as file:
            file.write(content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(
        "Unidades instaladas. Preencha o ambiente e inicie cada serviço explicitamente."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--services", nargs="+", choices=SERVICES, default=["robot"])
    parser.add_argument("--repository", help="clonar por HTTPS em um destino novo")
    parser.add_argument(
        "--no-sync", action="store_true", help="reutilizar venv já preparado"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        install(
            args.checkout,
            args.environment,
            args.services,
            repository=args.repository,
            dry_run=args.dry_run,
            no_sync=args.no_sync,
        )
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(
            "Falha na instalação. Confira pré-requisitos e caminhos; "
            "unidades existentes são preservadas.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
