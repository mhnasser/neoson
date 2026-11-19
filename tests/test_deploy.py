import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from infra import install


@pytest.mark.parametrize("service", install.SERVICES)
def test_units_use_existing_modules_without_shell_or_automatic_capture(
    service: str,
) -> None:
    unit = install.unit_text(
        service, Path("/srv/Neo Son%$HOME"), Path("/srv/private.env")
    )
    assert "WorkingDirectory=/srv/Neo Son%%$HOME\n" in unit
    assert 'ExecStart=:"/srv/Neo Son%%$HOME/.venv/bin/python" -m ' in unit
    assert "UMask=0077" in unit
    if service == "robot":
        assert "Type=oneshot" in unit
        assert "Restart=" not in unit and "WantedBy=" not in unit
    else:
        assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit


def test_dry_run_clones_and_syncs_selected_packages_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("dry-run executou um comando")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert (
        install.main(
            [
                "--checkout",
                "/srv/neoson",
                "--environment",
                "/srv/private.env",
                "--services",
                "gateway",
                "worker",
                "telegram",
                "graph",
                "--repository",
                "https://example.com/neoson.git",
                "--dry-run",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "git clone -- https://example.com/neoson.git" in text
    assert "--package neoson-gateway --package neoson-worker" in text
    assert "neoson_worker.graph_main run" in text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("path", ["/srv/bad\npath", "/srv/bad\rpath", "/srv/bad\\path"])
def test_unit_rejects_directive_injection(path: str) -> None:
    with pytest.raises(ValueError):
        install.quoted(path)


def test_repository_credentials_are_never_printed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        install.main(
            [
                "--checkout",
                "/srv/neoson",
                "--environment",
                "/srv/private.env",
                "--repository",
                "https://user:private" + chr(64) + "example.com/repo",
                "--dry-run",
            ]
        )
        == 1
    )
    result = capsys.readouterr()
    assert "private" + chr(64) + "example" not in result.out + result.err


@pytest.mark.skipif(os.name != "posix", reason="instalação real somente no Linux")
def test_install_preserves_environment_and_refuses_existing_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> None:
        calls.append(command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    checkout = tmp_path / "checkout"
    (checkout / ".venv/bin").mkdir(parents=True)
    (checkout / "uv.lock").touch()
    (checkout / ".venv/bin/python").touch()
    environment = tmp_path / "private.env"
    args = ["--checkout", str(checkout), "--environment", str(environment), "--no-sync"]
    assert install.main(args) == 0
    assert environment.stat().st_mode & 0o777 == 0o600
    environment.write_text("EXISTING=value\n")
    assert install.main(args) == 1
    assert environment.read_text() == "EXISTING=value\n"
    assert calls == [["systemctl", "--user", "daemon-reload"]]


@pytest.mark.skipif(os.name != "posix", reason="instalação real somente no Linux")
def test_existing_environment_is_preserved_and_sync_is_not_implicit_for_existing_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> None:
        calls.append(command)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "uv.lock").touch()
    environment = tmp_path / "private.env"
    environment.write_text("EXISTING=value\n")
    environment.chmod(0o600)
    args = [
        "--checkout",
        str(checkout),
        "--environment",
        str(environment),
        "--services",
        "gateway",
        "worker",
    ]
    (checkout / ".venv").mkdir()
    assert install.main(args) == 1
    assert calls == []
    (checkout / ".venv").rmdir()
    assert install.main(args) == 0
    assert environment.read_text() == "EXISTING=value\n"
    assert calls[0] == [
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        "--inexact",
        "--package",
        "neoson-gateway",
        "--package",
        "neoson-worker",
    ]
    assert calls[1] == ["systemctl", "--user", "daemon-reload"]


@pytest.mark.skipif(os.name != "posix", reason="permissões POSIX")
@pytest.mark.parametrize("symlink", [False, True])
def test_install_refuses_exposed_or_symlink_environment_before_running_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink: bool,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Pré-condição inválida executou um comando")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "uv.lock").touch()
    environment = tmp_path / "private.env"
    if symlink:
        target = tmp_path / "target"
        target.write_text("EXISTING=value\n")
        target.chmod(0o600)
        environment.symlink_to(target)
    else:
        environment.write_text("EXISTING=value\n")
        environment.chmod(0o644)
    assert (
        install.main(["--checkout", str(checkout), "--environment", str(environment)])
        == 1
    )
