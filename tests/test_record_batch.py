import json
import os
import signal
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from neoson_robot import record_batch
from neoson_robot.fake_devices import silent_wav
from neoson_robot.pipewire import PipeWireCapture


def test_dry_run_lists_thirty_phrases_without_capture_or_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("dry-run gravou áudio")

    monkeypatch.setattr(PipeWireCapture, "record", forbidden)
    output = tmp_path / "recordings"
    assert record_batch.main(["--output", str(output), "--dry-run"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 30
    assert not output.exists()


def test_batch_captures_with_simulated_pipewire_and_keeps_expected_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    commands: list[list[str]] = []
    audio = silent_wav()

    class Process:
        code: int | None = None

        def __init__(self, args: Sequence[str]) -> None:
            self.path = Path(args[-1])
            commands.append(list(args))

        def poll(self) -> int | None:
            return self.code

        def send_signal(self, sig: int) -> None:
            assert sig == signal.SIGINT
            self.path.write_bytes(audio)
            self.code = 1

        def terminate(self) -> None:
            self.code = -1

        def kill(self) -> None:
            self.code = -9

        def wait(self, timeout: float | None = None) -> int:
            assert self.code is not None
            return self.code

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("builtins.input", lambda prompt: "")
    capture = PipeWireCapture(
        seconds=0.1, spawn=Process, clock=lambda: now[0], sleep=sleep
    )
    output = tmp_path / "recordings"
    record_batch.record_batch(output, capture, dry_run=False)
    rows = [
        json.loads(line)
        for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == len(commands) == 30
    assert [row["expected_text"] for row in rows] == list(record_batch.PHRASES)
    for row in rows:
        assert (output / row["audio"]).read_bytes() == audio
        if os.name == "posix":
            assert (output / row["audio"]).stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        record_batch.record_batch(output, capture, dry_run=False)


def test_recording_refuses_git_checkout(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="Git"):
        record_batch.record_batch(
            tmp_path / "recordings", PipeWireCapture(), dry_run=True
        )


def test_invalid_duration_creates_nothing(tmp_path: Path) -> None:
    assert (
        record_batch.main(["--output", str(tmp_path / "new"), "--seconds", "16"]) == 1
    )
    assert not (tmp_path / "new").exists()


def test_cancellation_keeps_completed_samples_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = [0]

    def prompt(text: str) -> str:
        count[0] += 1
        if count[0] == 2:
            raise KeyboardInterrupt
        return ""

    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(PipeWireCapture, "record", lambda self, cancelled: silent_wav())
    output = tmp_path / "recordings"
    assert record_batch.main(["--output", str(output)]) == 130
    assert (
        len((output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    )
    assert (output / "01.wav").is_file()
    assert not (output / "02.wav").exists()
