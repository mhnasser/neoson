"""Display do peito: uso e temperatura da CPU e uso da RAM num LCD 1602 por I2C.

O LCD (controlador HD44780) fica atrás de um backpack PCF8574, no barramento I2C
dos pinos GPIO. Usa só a biblioteca padrão: lê `/proc/stat`, `/proc/meminfo` e a
zona térmica do sysfs e escreve no PCF8574 por `/dev/i2c-N`, de modo que o módulo
também roda sozinho com o Python do sistema (`python3 chest_display.py`).

Tela (16 colunas):

    CPU  12%  41.3°C
    RAM   6% 0.5G/8G
"""

import argparse
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol

WIDTH = 16
ROWS = 2
DEFAULT_BUS = 1
DEFAULT_ADDRESS = 0x27
DEFAULT_INTERVAL = 2.0
MIN_INTERVAL = 0.5
MAX_WAIT = 300.0
PROBE_INTERVAL = 1.0
EXIT_NO_DISPLAY = 3  # barramento inexistente ou nada respondendo no endereço
ROOT = Path("/")
THERMAL_ZONE = "sys/class/thermal/thermal_zone0/temp"
KIB_PER_GIB = 1024 * 1024

# Bits do PCF8574 no backpack comum: P0=RS, P1=RW, P2=E, P3=luz, P4..P7=D4..D7.
_RS = 0x01
_ENABLE = 0x04
_BACKLIGHT = 0x08
_I2C_SLAVE = 0x0703  # ioctl de linux/i2c-dev.h
_DEGREE = 0xDF  # símbolo de grau na ROM A00 do HD44780
_UNKNOWN = ord("?")
_ROW_ADDRESS = (0x00, 0x40)


class ByteBus(Protocol):
    """Barramento que entrega um byte por transação ao PCF8574."""

    def write_byte(self, value: int) -> None: ...
    def close(self) -> None: ...


class I2CDevice(ByteBus, Protocol):
    """Barramento que também lê, para confirmar que há um dispositivo no endereço."""

    def read_byte(self) -> int: ...


class LinuxI2C:
    """`/dev/i2c-N` aberto para um único endereço, sem bibliotecas externas."""

    _fd: int

    def __init__(self, bus: int, address: int) -> None:
        if sys.platform == "win32":
            raise OSError("I2C disponível só no Linux.")
        import fcntl

        self._fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self._fd, _I2C_SLAVE, address)
        except OSError:
            os.close(self._fd)
            raise

    def write_byte(self, value: int) -> None:
        os.write(self._fd, bytes((value,)))

    def read_byte(self) -> int:
        # Sem ACK no endereço, o kernel devolve erro (ENXIO/EREMOTEIO).
        data = os.read(self._fd, 1)
        if not data:
            raise OSError("leitura I2C vazia.")
        return data[0]

    def close(self) -> None:
        os.close(self._fd)


def connect(
    open_bus: Callable[[], I2CDevice],
    timeout: float,
    stop: threading.Event,
    clock: Callable[[], float] = time.monotonic,
) -> I2CDevice | None:
    """Abre o barramento e confirma que o endereço responde, por até `timeout` s.

    Barramento inexistente ou endereço sem resposta contam como display ausente
    (None, também quando `stop` interrompe a espera). Outros erros, como falta
    de permissão no `/dev/i2c-N`, sobem como OSError.
    """
    deadline = clock() + timeout
    while True:
        try:
            device = open_bus()
        except FileNotFoundError:
            pass
        else:
            try:
                device.read_byte()
                return device
            except OSError:
                device.close()
        if clock() >= deadline or stop.wait(PROBE_INTERVAL):
            return None


def encode(text: str) -> bytes:
    """Converte para a ROM do LCD, cortando ou completando com espaços até WIDTH."""
    codes = bytearray()
    for char in text[:WIDTH]:
        if char == "°":
            codes.append(_DEGREE)
        elif 0x20 <= ord(char) < 0x7E and char != "\\":
            codes.append(ord(char))  # ASCII imprimível; "\" é "¥" nessa ROM
        else:
            codes.append(_UNKNOWN)
    return bytes(codes.ljust(WIDTH, b" "))


class Lcd1602:
    """HD44780 em modo de 4 bits pelo PCF8574."""

    def __init__(
        self, bus: ByteBus, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._bus, self._sleep = bus, sleep
        self._light = _BACKLIGHT

    def initialize(self) -> None:
        self._light = _BACKLIGHT
        self._sleep(0.05)
        for _ in range(3):  # volta ao modo de 8 bits, qualquer que seja o estado
            self._pulse(0x30)
            self._sleep(0.005)
        self._pulse(0x20)  # passa para 4 bits
        self._command(0x28)  # 2 linhas, caracteres 5x8
        self._command(0x0C)  # display ligado, sem cursor
        self._command(0x06)  # escrita da esquerda para a direita
        self.clear()

    def clear(self) -> None:
        self._command(0x01)
        self._sleep(0.002)

    def show(self, lines: Sequence[str]) -> None:
        """Reescreve as linhas inteiras, sem limpar a tela (evita piscar)."""
        for row, text in enumerate(lines[:ROWS]):
            self._command(0x80 | _ROW_ADDRESS[row])
            for code in encode(text):
                self._send(code, _RS)

    def backlight(self, on: bool) -> None:
        self._light = _BACKLIGHT if on else 0
        self._bus.write_byte(self._light)

    def _command(self, value: int) -> None:
        self._send(value, 0)

    def _send(self, value: int, mode: int) -> None:
        self._pulse((value & 0xF0) | mode)
        self._pulse(((value << 4) & 0xF0) | mode)

    def _pulse(self, data: int) -> None:
        # O LCD lê o nibble na descida de E; cada transação I2C dura mais que
        # os tempos mínimos do HD44780, então só os comandos longos esperam.
        data |= self._light
        self._bus.write_byte(data | _ENABLE)
        self._bus.write_byte(data)


@dataclass(frozen=True)
class CpuTimes:
    busy: int
    total: int


@dataclass(frozen=True)
class Memory:
    total_kib: int
    available_kib: int

    @property
    def used_kib(self) -> int:
        return max(0, self.total_kib - self.available_kib)

    @property
    def percent(self) -> float:
        return 100.0 * self.used_kib / self.total_kib if self.total_kib else 0.0


@dataclass(frozen=True)
class Snapshot:
    cpu_percent: float
    temperature_c: float | None
    memory: Memory


def parse_cpu_times(text: str) -> CpuTimes:
    """Linha agregada `cpu` de /proc/stat; iowait conta como ocioso."""
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            values = [int(value) for value in fields[1:9]]  # user..steal
            if len(values) < 4:
                break
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            return CpuTimes(busy=total - idle, total=total)
    raise ValueError("/proc/stat sem a linha cpu.")


def cpu_percent(previous: CpuTimes, current: CpuTimes) -> float:
    elapsed = current.total - previous.total
    if elapsed <= 0:
        return 0.0
    busy = current.busy - previous.busy
    return min(100.0, max(0.0, 100.0 * busy / elapsed))


def parse_meminfo(text: str) -> Memory:
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if name in ("MemTotal", "MemAvailable") and fields:
            values[name] = int(fields[0])
    if "MemTotal" not in values or "MemAvailable" not in values:
        raise ValueError("/proc/meminfo sem MemTotal ou MemAvailable.")
    return Memory(total_kib=values["MemTotal"], available_kib=values["MemAvailable"])


def parse_temperature(text: str) -> float:
    """Zona térmica do sysfs, em milésimos de grau Celsius."""
    return int(text.strip()) / 1000


class SystemReader:
    """Lê CPU, temperatura e RAM a partir de `root` (`/` na Pi; pasta em testes)."""

    def __init__(self, root: Path = ROOT, thermal_zone: str = THERMAL_ZONE) -> None:
        self._root, self._thermal = root, thermal_zone
        self._previous = self._cpu_times()

    def sample(self) -> Snapshot:
        """Uso da CPU desde a amostra anterior (ou desde a criação do leitor)."""
        current = self._cpu_times()
        usage = cpu_percent(self._previous, current)
        self._previous = current
        meminfo = (self._root / "proc/meminfo").read_text(encoding="ascii")
        return Snapshot(usage, self._temperature(), parse_meminfo(meminfo))

    def _cpu_times(self) -> CpuTimes:
        return parse_cpu_times((self._root / "proc/stat").read_text(encoding="ascii"))

    def _temperature(self) -> float | None:
        try:
            return parse_temperature((self._root / self._thermal).read_text("ascii"))
        except (OSError, ValueError):
            return None


def format_lines(snapshot: Snapshot) -> tuple[str, str]:
    if snapshot.temperature_c is None:
        temperature = f"{'--':>5}°C"
    else:
        temperature = f"{snapshot.temperature_c:5.1f}°C"
    cpu = f"CPU {round(snapshot.cpu_percent):3d}% {temperature}"
    memory = snapshot.memory
    used, total = memory.used_kib / KIB_PER_GIB, memory.total_kib / KIB_PER_GIB
    prefix = f"RAM {round(memory.percent):3d}%"
    options = (
        f"{prefix} {used:.1f}G/{total:.0f}G",
        f"{prefix} {used:.1f}G",
        prefix,
    )
    ram = next(option for option in options if len(option) <= WIDTH)
    return cpu, ram


def _log(message: str) -> None:
    print(f"neoson-chest: {message}", file=sys.stderr, flush=True)


def run(
    lcd: Lcd1602,
    sample: Callable[[], Snapshot],
    stop: threading.Event,
    interval: float = DEFAULT_INTERVAL,
) -> None:
    """Atualiza o LCD até `stop`; falhas de I2C reiniciam o LCD na volta seguinte."""
    ready = False
    try:
        while True:
            try:
                if not ready:
                    lcd.initialize()
                    lcd.show(("NeoSon", "monitor..."))
                    ready = True
                    if stop.wait(interval):
                        break
                lcd.show(format_lines(sample()))
            except OSError:
                if ready:
                    _log("falha no I2C; o LCD será reiniciado.")
                ready = False
            if stop.wait(interval):
                break
    finally:
        if ready:
            try:
                lcd.clear()
                lcd.backlight(False)
            except OSError:
                pass


def _address(value: str) -> int:
    address = int(value, 0)
    if not 0x03 <= address <= 0x77:
        raise argparse.ArgumentTypeError("endereço I2C fora de 0x03..0x77")
    return address


def _interval(value: str) -> float:
    seconds = float(value)
    if seconds < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(f"intervalo mínimo de {MIN_INTERVAL} s")
    return seconds


def _wait(value: str) -> float:
    seconds = float(value)
    if not 0 <= seconds <= MAX_WAIT:
        raise argparse.ArgumentTypeError(f"espera entre 0 e {MAX_WAIT:.0f} s")
    return seconds


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-chest",
        description="Mostra CPU (uso e temperatura) e RAM no LCD 1602 do peito.",
    )
    parser.add_argument("--bus", type=int, default=DEFAULT_BUS, help="/dev/i2c-N")
    parser.add_argument(
        "--address", type=_address, default=DEFAULT_ADDRESS, help="ex.: 0x27"
    )
    parser.add_argument(
        "--interval", type=_interval, default=DEFAULT_INTERVAL, help="segundos"
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="imprime as linhas no terminal, sem usar o LCD",
    )
    parser.add_argument(
        "--once", action="store_true", help="mede uma vez, mostra e sai"
    )
    parser.add_argument(
        "--wait",
        type=_wait,
        default=0.0,
        help="segundos esperando o display responder antes de desistir (saída 3)",
    )
    args = parser.parse_args(argv)

    try:
        reader = SystemReader()
    except (OSError, ValueError):
        _log("não foi possível ler /proc (só funciona no Linux).")
        return 1
    stop = threading.Event()

    def _stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _stop)

    if args.console:
        while not stop.wait(args.interval):
            print(*format_lines(reader.sample()), sep="\n", flush=True)
            if args.once:
                break
            print(flush=True)
        return 0

    where = f"I2C {args.bus} no endereço {args.address:#04x}"
    try:
        bus = connect(lambda: LinuxI2C(args.bus, args.address), args.wait, stop)
    except OSError:
        _log(f"sem acesso ao {where}.")
        return 1
    if bus is None:
        if stop.is_set():
            return 0
        _log(f"display não encontrado no {where}.")
        return EXIT_NO_DISPLAY
    lcd = Lcd1602(bus)
    try:
        if args.once:
            lcd.initialize()
            stop.wait(args.interval)
            lcd.show(format_lines(reader.sample()))
            return 0
        run(lcd, reader.sample, stop, args.interval)
        return 0
    except OSError:
        _log("falha ao escrever no LCD.")
        return 1
    finally:
        bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
