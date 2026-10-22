"""Captura e reprodução reais pelo PipeWire (`pw-record`/`pw-play`), sem driver próprio.

Cada operação usa um WAV temporário privado, apagado no fim. O processo é encerrado
no prazo ou no cancelamento, e `stop()` pode ser chamado várias vezes. Mensagens de
erro são fixas: nunca repetem saída dos comandos nem caminhos.
"""

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from neoson_robot.client import GatewayError
from neoson_robot.voice_client import check_audio
from neoson_robot.voice_controller import Cancelled

POLL_SECONDS = 0.05
STOP_SECONDS = 2.0
MAX_CAPTURE_SECONDS = 15.0


class Process(Protocol):
    def poll(self) -> int | None: ...
    def send_signal(self, sig: int) -> None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Spawn = Callable[[Sequence[str]], Process]


def spawn(args: Sequence[str]) -> Process:
    return subprocess.Popen(
        list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class DeviceError(RuntimeError):
    """Falha de captura ou reprodução, com mensagem fixa."""


class _Device:
    def __init__(
        self,
        command: str,
        spawn: Spawn | None,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        workdir: Path | None,
    ) -> None:
        self._command, self._spawn = command, spawn
        self._clock, self._sleep, self._workdir = clock, sleep, workdir
        self._process: Process | None = None

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            _end(process, signal.SIGTERM)

    def _start(self, args: Sequence[str]) -> Process:
        # Resolvido na chamada: `spawn` pode ser trocado no módulo (testes).
        runner = self._spawn if self._spawn is not None else spawn
        try:
            self._process = runner([self._command, *args])
        except OSError:
            raise DeviceError(f"{self._command} indisponível.") from None
        return self._process

    def _temporary(self) -> Path:
        # mkstemp cria o arquivo só para o usuário atual (0600 no Linux).
        handle, name = tempfile.mkstemp(suffix=".wav", dir=self._workdir)
        os.close(handle)
        return Path(name)


class PipeWireCapture(_Device):
    simulated = False

    def __init__(
        self,
        seconds: float = 5,
        *,
        command: str = "pw-record",
        spawn: Spawn | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        workdir: Path | None = None,
    ) -> None:
        if not 0 < seconds <= MAX_CAPTURE_SECONDS:
            raise ValueError("Duração da captura deve ficar entre 0 e 15 s.")
        super().__init__(command, spawn, clock, sleep, workdir)
        self.seconds = seconds

    def record(self, cancelled: Callable[[], bool]) -> bytes:
        if cancelled():
            raise Cancelled
        path = self._temporary()
        try:
            process = self._start(
                ["--rate", "16000", "--channels", "1", "--format", "s16", str(path)]
            )
            deadline = self._clock() + self.seconds
            while self._clock() < deadline:
                if cancelled():
                    raise Cancelled
                if process.poll() is not None:
                    raise DeviceError("A captura terminou antes do tempo.")
                self._sleep(POLL_SECONDS)
            # SIGINT faz o pw-record fechar o WAV; ele sai com código 1 mesmo assim.
            self._process = None
            _end(process, signal.SIGINT)
            data = path.read_bytes()
        finally:
            self.stop()
            path.unlink(missing_ok=True)
        try:
            check_audio(data)
        except GatewayError:
            raise DeviceError("Áudio capturado inválido.") from None
        return data


class PipeWirePlayback(_Device):
    def __init__(
        self,
        *,
        command: str = "pw-play",
        spawn: Spawn | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        workdir: Path | None = None,
    ) -> None:
        super().__init__(command, spawn, clock, sleep, workdir)

    def play(self, audio: bytes, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise Cancelled
        try:
            check_audio(audio)
        except GatewayError:
            raise DeviceError("Áudio para reprodução inválido.") from None
        seconds = (len(audio) - 44) / (16000 * 2)
        path = self._temporary()
        try:
            path.write_bytes(audio)
            process = self._start([str(path)])
            deadline = self._clock() + seconds + 5
            while (code := process.poll()) is None:
                if cancelled():
                    raise Cancelled
                if self._clock() >= deadline:
                    raise DeviceError("A reprodução passou do tempo esperado.")
                self._sleep(POLL_SECONDS)
            self._process = None
            if code != 0:
                raise DeviceError("A reprodução falhou.")
        finally:
            self.stop()
            path.unlink(missing_ok=True)


def _end(process: Process, sig: int) -> None:
    """Pede o fim com `sig`; força se o processo não sair no prazo."""
    if process.poll() is not None:
        return
    try:
        process.send_signal(sig)
        process.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    except ProcessLookupError:
        pass
