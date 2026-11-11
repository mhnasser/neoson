import signal
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from neoson_robot import chest_display
from neoson_robot.chest_display import (
    WIDTH,
    CpuTimes,
    Lcd1602,
    Memory,
    Snapshot,
    SystemReader,
    cpu_percent,
    encode,
    format_lines,
    parse_cpu_times,
    parse_meminfo,
    parse_temperature,
    run,
)

STAT = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 25 0 12 200 12 0 0 0 0 0\nintr 1\n"
STAT_LATER = "cpu  160 0 70 900 70 0 0 0 0 0\n"
MEMINFO = "MemTotal:        8255776 kB\nMemFree: 1 kB\nMemAvailable:    7820048 kB\n"
GIB = 1024 * 1024
ENABLE, BACKLIGHT, RS = 0x04, 0x08, 0x01


class FakeBus:
    def __init__(self, fail_at: set[int] | None = None) -> None:
        self.writes: list[int] = []
        self.fail_at = fail_at or set()
        self.calls = 0
        self.closed = False

    def write_byte(self, value: int) -> None:
        self.calls += 1
        if self.calls in self.fail_at:
            raise OSError("sem ACK")
        assert 0 <= value <= 0xFF
        self.writes.append(value)

    def close(self) -> None:
        self.closed = True


class Stop(threading.Event):
    """Encerra depois de `rounds` esperas, sem dormir."""

    def __init__(self, rounds: int) -> None:
        super().__init__()
        self.rounds, self.waits = rounds, 0

    def wait(self, timeout: float | None = None) -> bool:
        self.waits += 1
        return self.waits >= self.rounds


def decode(writes: list[int], init_pulses: int = 4) -> list[tuple[str, int]]:
    """Reconstrói comandos ("cmd") e caracteres ("chr") a partir dos pulsos em E."""
    nibbles: list[tuple[int, int]] = []
    for index, value in enumerate(writes):
        if value & ENABLE:
            assert writes[index + 1] == value & ~ENABLE  # descida de E
            nibbles.append((value & 0xF0, value & RS))
    nibbles = nibbles[init_pulses:]
    assert len(nibbles) % 2 == 0
    decoded: list[tuple[str, int]] = []
    for high, low in zip(nibbles[::2], nibbles[1::2], strict=True):
        assert high[1] == low[1]
        decoded.append(("chr" if high[1] else "cmd", high[0] | (low[0] >> 4)))
    return decoded


def snapshot(
    cpu: float, temperature: float | None, total: int, available: int
) -> Snapshot:
    return Snapshot(cpu, temperature, Memory(total, available))


def test_cpu_usage_from_two_samples_counts_iowait_as_idle() -> None:
    first, later = parse_cpu_times(STAT), parse_cpu_times(STAT_LATER)
    assert first == CpuTimes(busy=150, total=1000)
    assert later == CpuTimes(busy=230, total=1200)
    assert cpu_percent(first, later) == pytest.approx(40.0)
    assert cpu_percent(later, later) == 0.0
    assert cpu_percent(later, first) == 0.0


def test_memory_and_temperature_parsing() -> None:
    memory = parse_meminfo(MEMINFO)
    assert memory == Memory(total_kib=8255776, available_kib=7820048)
    assert memory.used_kib == 435728
    assert memory.percent == pytest.approx(5.278, abs=0.001)
    assert Memory(0, 0).percent == 0.0
    assert parse_temperature("39150\n") == pytest.approx(39.15)


@pytest.mark.parametrize(
    ("parser", "text"),
    [
        (parse_cpu_times, "intr 1\n"),
        (parse_cpu_times, "cpu 1 2\n"),
        (parse_meminfo, "MemTotal: 10 kB\n"),
    ],
)
def test_incomplete_proc_files_are_rejected(
    parser: Callable[[str], object], text: str
) -> None:
    with pytest.raises(ValueError):
        parser(text)


def test_screen_fits_sixteen_columns() -> None:
    pi = snapshot(12.4, 41.3, 8255776, 7820048)
    assert format_lines(pi) == ("CPU  12%  41.3°C", "RAM   5% 0.4G/8G")

    busy = snapshot(100.0, 85.0, 16 * GIB, 8 * GIB)
    assert format_lines(busy) == ("CPU 100%  85.0°C", "RAM  50% 8.0G")

    small = snapshot(3.0, None, 2 * GIB, GIB // 2)
    assert format_lines(small) == ("CPU   3%    --°C", "RAM  75% 1.5G/2G")

    for lines in map(format_lines, (pi, busy, small)):
        assert all(len(encode(line)) == WIDTH and len(line) <= WIDTH for line in lines)


def test_encode_uses_lcd_rom() -> None:
    assert encode("41°C") == b"41\xdfC" + b" " * 12
    assert encode("é\\~ok") == b"???ok" + b" " * 11
    assert encode("x" * 20) == b"x" * WIDTH


def test_lcd_initializes_in_four_bit_mode_and_writes_both_rows() -> None:
    bus = FakeBus()
    lcd = Lcd1602(bus, sleep=lambda _: None)
    lcd.initialize()
    lcd.show(("AB", "C°"))

    init = [value & 0xF0 for value in bus.writes if value & ENABLE][:4]
    assert init == [0x30, 0x30, 0x30, 0x20]
    assert all(value & BACKLIGHT for value in bus.writes)
    decoded = decode(bus.writes)
    setup = [("cmd", code) for code in (0x28, 0x0C, 0x06, 0x01, 0x80)]
    assert decoded[:5] == setup
    row1 = [code for kind, code in decoded[5:21]]
    assert decoded[21] == ("cmd", 0xC0)
    row2 = [code for kind, code in decoded[22:]]
    assert bytes(row1) == b"AB" + b" " * 14
    assert bytes(row2) == b"C\xdf" + b" " * 14


def test_backlight_off_is_kept_in_later_writes() -> None:
    bus = FakeBus()
    lcd = Lcd1602(bus, sleep=lambda _: None)
    lcd.backlight(False)
    lcd.clear()
    assert bus.writes[0] == 0x00
    assert not any(value & BACKLIGHT for value in bus.writes)


def test_run_shows_readings_and_turns_off_on_stop() -> None:
    bus = FakeBus()
    samples = iter(snapshot(cpu, 40.0 + cpu / 10, 8 * GIB, 6 * GIB) for cpu in (10, 20))
    run(Lcd1602(bus, sleep=lambda _: None), lambda: next(samples), Stop(rounds=3))

    text = bytes(code for kind, code in decode(bus.writes[:-1]) if kind == "chr")
    assert b"NeoSon" in text
    assert b"CPU  10%  41.0\xdfCRAM  25% 2.0G/8G" in text
    assert b"CPU  20%  42.0\xdfC" in text
    assert decode(bus.writes[:-1])[-1] == ("cmd", 0x01)  # limpa ao sair
    assert bus.writes[-1] == 0x00  # e apaga a luz


def test_run_reinitializes_after_i2c_failure() -> None:
    bus = FakeBus(fail_at={200})  # falha no meio da primeira leitura
    calls = []

    def sample() -> Snapshot:
        calls.append(1)
        return snapshot(50, 45.0, 8 * GIB, 4 * GIB)

    run(Lcd1602(bus, sleep=lambda _: None), sample, Stop(rounds=4))

    # Reinício = 0x30, 0x30, 0x30, 0x20 com RS=0 (texto vai sempre com RS=1).
    pulses = [value & (0xF0 | RS) for value in bus.writes if value & ENABLE]
    restart = [0x30, 0x30, 0x30, 0x20]
    starts = [i for i in range(len(pulses)) if pulses[i : i + 4] == restart]
    assert len(starts) == 2
    assert len(calls) == 2  # a leitura que falhou e a seguinte, já reiniciado
    assert bus.writes[-1] == 0x00


def test_reader_measures_from_files(tmp_path: Path) -> None:
    (tmp_path / "proc").mkdir()
    stat = tmp_path / "proc/stat"
    stat.write_text(STAT)
    (tmp_path / "proc/meminfo").write_text(MEMINFO)
    zone = tmp_path / chest_display.THERMAL_ZONE
    zone.parent.mkdir(parents=True)
    zone.write_text("52300\n")

    reader = SystemReader(tmp_path)
    stat.write_text(STAT_LATER)
    first = reader.sample()
    assert first.cpu_percent == pytest.approx(40.0)
    assert first.temperature_c == pytest.approx(52.3)
    assert first.memory.total_kib == 8255776

    zone.write_text("invalido")
    assert reader.sample().temperature_c is None
    zone.unlink()
    assert reader.sample().temperature_c is None


def test_console_once_prints_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/stat").write_text(STAT)
    (tmp_path / "proc/meminfo").write_text(MEMINFO)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(chest_display, "SystemReader", lambda: SystemReader(tmp_path))

    assert chest_display.main(["--console", "--once", "--interval", "0.5"]) == 0
    assert capsys.readouterr().out == "CPU   0%    --°C\nRAM   5% 0.4G/8G\n"


def test_cli_reports_missing_i2c_and_rejects_bad_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/stat").write_text(STAT)
    (tmp_path / "proc/meminfo").write_text(MEMINFO)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(chest_display, "SystemReader", lambda: SystemReader(tmp_path))

    def no_bus(bus: int, address: int) -> FakeBus:
        raise OSError("sem /dev/i2c")

    monkeypatch.setattr(chest_display, "LinuxI2C", no_bus)
    assert chest_display.main(["--bus", "3", "--address", "0x3f"]) == 1
    assert "I2C 3 no endereço 0x3f" in capsys.readouterr().err

    for bad in (["--address", "0x80"], ["--interval", "0.1"]):
        with pytest.raises(SystemExit) as error:
            chest_display.main(bad)
        assert error.value.code == 2


def test_cli_without_proc_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing() -> SystemReader:
        raise FileNotFoundError("proc")

    monkeypatch.setattr(chest_display, "SystemReader", missing)
    assert chest_display.main(["--console"]) == 1
    assert "/proc" in capsys.readouterr().err
