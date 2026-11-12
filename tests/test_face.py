import struct
import subprocess
from collections.abc import Sequence
from dataclasses import astuple
from pathlib import Path

import pytest

from neoson_robot import face as face_module
from neoson_robot.face import (
    BLINK,
    CAMERA_COMMAND,
    CORNER_STEPS,
    DEFAULT_PALETTE,
    EXPRESSIONS,
    HOLD_TO_CLOSE,
    LAYERS,
    PROGRAM,
    REACTION,
    TAP,
    TRANSITION,
    Animator,
    CameraView,
    Eye,
    Face,
    FullscreenGuard,
    Gestures,
    blink_factor,
    eye_outline,
    eyelid_box,
    find_touchscreen,
    has_touch_button,
    needs_fullscreen,
    palette,
    parse_color,
    program_face,
    read_touches,
    rounded_rect,
    scene,
    touch_changes,
)

CYCLE = sum(duration for _, duration in PROGRAM)


def bounds(points: list[float]) -> tuple[float, float, float, float]:
    xs, ys = points[::2], points[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def gap(a: Face, b: Face) -> float:
    """Maior diferença entre parâmetros de dois rostos."""
    pairs = zip(
        astuple(a.left) + astuple(a.right),
        astuple(b.left) + astuple(b.right),
        strict=True,
    )
    return float(max(abs(x - y) for x, y in pairs))


def test_rounded_rect_keeps_point_count_and_bounds() -> None:
    for radius in (0.0, 20.0, 1000.0):  # 1000: vira pílula sem sair da caixa
        points = rounded_rect(10, 20, 110, 220, radius)
        assert len(points) == 2 * 4 * (CORNER_STEPS + 1)
        assert bounds(points) == pytest.approx((10, 20, 110, 220))


def test_glow_layers_grow_around_the_eye() -> None:
    eye = Eye(400, 240, 150, 180, 46)
    core = bounds(eye_outline(eye))
    outer = bounds(eye_outline(eye, LAYERS[0]))
    assert core == pytest.approx((325, 150, 475, 330))
    assert outer == pytest.approx((316, 141, 484, 339))


def test_eyelid_hides_below_eye_until_it_smiles() -> None:
    eye = Eye(400, 240, 176, 176, 88)
    lid_top = eyelid_box(eye)[1]
    glow_bottom = bounds(eye_outline(eye, LAYERS[0]))[3]
    assert lid_top >= glow_bottom - 1e-9

    happy = eyelid_box(Eye(400, 240, 176, 176, 88, smile=1.0))
    eye_top = 240 - 88
    assert eye_top < happy[1] < 240  # sobra só o arco de cima do olho
    assert (
        happy[0] < 400 - 88 and happy[2] > 400 + 88
    )  # cobre o olho inteiro na largura


def test_loop_alternates_neutral_and_surprised_without_happy() -> None:
    assert [name for name, _ in PROGRAM] == ["neutro", "surpreso"]
    assert program_face(3.0)[0] == "neutro"
    assert program_face(8.0 + 2.0)[0] == "surpreso"
    assert program_face(CYCLE + 3.0)[0] == "neutro"
    # Sem toque, o rosto nunca sorri: a expressão feliz é só da reação.
    steps = round(CYCLE / 0.05)
    assert max(program_face(i * 0.05)[1].left.smile for i in range(steps)) == 0.0


def test_no_jumps_between_expressions_or_when_the_loop_restarts() -> None:
    boundaries = [0.0, 8.0, CYCLE, CYCLE + 8.0]
    epsilon = 1e-4
    for boundary in boundaries:
        before = program_face(boundary - epsilon)[1]
        after = program_face(boundary + epsilon)[1]
        assert gap(before, after) < 0.5, boundary


def test_transition_eases_into_the_next_expression() -> None:
    calm, surprised = EXPRESSIONS["neutro"].left, EXPRESSIONS["surpreso"].left
    start = program_face(8.0)[1].left
    middle = program_face(8.0 + TRANSITION / 2)[1].left
    end = program_face(8.0 + TRANSITION)[1].left
    assert start.w == pytest.approx(calm.w)
    assert calm.w + 10 < middle.w < surprised.w - 10
    assert end.w == pytest.approx(surprised.w)


def test_blinks_close_the_eyes_briefly() -> None:
    assert blink_factor(1.0 + BLINK / 2, (1.0,)) == pytest.approx(0.08)
    assert blink_factor(0.5, (1.0,)) == 1.0
    open_eye = program_face(0.6)[1].left
    blinking = program_face(1.0 + BLINK / 2)[1].left
    assert blinking.h == pytest.approx(open_eye.h * 0.08, rel=0.01)


def test_neutral_face_glances_to_both_sides() -> None:
    base = EXPRESSIONS["neutro"]
    left = program_face(2.8)[1]
    right = program_face(5.5)[1]
    assert left.left.x == pytest.approx(base.left.x - 60)
    assert right.right.x == pytest.approx(base.right.x + 60)
    assert left.left.h > left.right.h  # o olho do lado do olhar cresce


def test_happy_face_hops_on_touch_and_surprised_face_pops() -> None:
    base = EXPRESSIONS["feliz"].left
    animator = Animator()
    animator.react(1.0)
    heights = [animator.frame(1.6 + i * 0.05).left.y for i in range(20)]
    assert min(heights) < base.y - 5
    assert max(heights) <= base.y + 1e-9

    surprised = EXPRESSIONS["surpreso"].left
    popped = program_face(8.0 + TRANSITION + 0.25)[1].left
    assert popped.w > surprised.w * 1.05


def test_tap_reaction_makes_any_expression_happy_for_a_while() -> None:
    animator = Animator()
    animator.react(2.0)
    assert animator.frame(2.0 + 1.0).left.smile == pytest.approx(1.0)
    assert animator.frame(2.0 + REACTION + 0.1).left.smile == 0.0
    assert animator.frame(2.0 + 1.0).left.smile == 0.0  # reação já terminou


def test_animator_rejects_phases_shorter_than_the_transition() -> None:
    with pytest.raises(ValueError):
        Animator((("neutro", TRANSITION),))
    with pytest.raises(ValueError):
        Animator(())


def test_scene_scales_and_centers_on_any_screen() -> None:
    calm = EXPRESSIONS["neutro"]
    items = scene(calm, 800, 480)
    assert len(items) == 2 * (len(LAYERS) + 1)
    assert bounds(items[2]) == pytest.approx(bounds(eye_outline(calm.left)))

    double = scene(calm, 1600, 960)
    assert double[2] == pytest.approx([2 * value for value in items[2]])

    wide = scene(calm, 1024, 480)  # sobra largura: centraliza
    assert wide[2][0] == pytest.approx(items[2][0] + 112)
    assert wide[2][1] == pytest.approx(items[2][1])


def test_palette_derives_glow_rim_and_core_from_one_color() -> None:
    assert parse_color(" #1F66FF ") == (31, 102, 255)
    outer, rim, core = palette("#1f66ff")
    assert rim == "#1f66ff"
    assert outer == "#091f4c"  # 30% da cor: brilho escuro
    assert core == "#3575ff"  # 10% em direção ao branco: miolo um pouco mais claro
    assert DEFAULT_PALETTE == palette("#0a50ff")
    for bad in ("1f66ff", "#1f66f", "#1f66fz"):
        with pytest.raises(ValueError):
            parse_color(bad)


def test_cli_runs_window_with_chosen_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_run(
        animator: Animator,
        fps: int,
        fullscreen: bool,
        seconds: float,
        colors: tuple[str, str, str],
        touch: Path | None = None,
        camera: CameraView | None = None,
    ) -> None:
        has_camera = camera is not None
        calls.append(
            (animator.program, fps, fullscreen, seconds, colors, touch, has_camera)
        )

    screen = tmp_path / "event7"
    monkeypatch.setattr(face_module, "run_window", fake_run)
    monkeypatch.setattr(face_module, "find_touchscreen", lambda: screen)
    assert face_module.main([]) == 0
    options = [
        "--expression",
        "feliz",
        "--fps",
        "20",
        "--windowed",
        "--color",
        "#FF8800",
    ]
    assert face_module.main(options) == 0
    monkeypatch.setattr(face_module, "find_touchscreen", lambda: None)
    assert face_module.main([]) == 0
    assert calls == [
        (PROGRAM, 30, True, 0.0, DEFAULT_PALETTE, screen, True),
        ((("feliz", 6.0),), 20, False, 0.0, palette("#ff8800"), None, False),
        (PROGRAM, 30, True, 0.0, DEFAULT_PALETTE, None, False),
    ]


def test_cli_without_display_and_bad_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_display(
        animator: Animator,
        fps: int,
        fullscreen: bool,
        seconds: float,
        colors: tuple[str, str, str],
        touch: Path | None = None,
        camera: CameraView | None = None,
    ) -> None:
        raise RuntimeError("sem tela gráfica disponível.")

    monkeypatch.setattr(face_module, "run_window", no_display)
    monkeypatch.setattr(face_module, "find_touchscreen", lambda: None)
    assert face_module.main(["--seconds", "1"]) == 1
    assert "sem tela gráfica" in capsys.readouterr().err

    bad_options = (
        ["--fps", "2"],
        ["--seconds", "-1"],
        ["--expression", "bravo"],
        ["--color", "azul"],
        ["--color", "#12345"],
        ["--color", "#gg0000"],
    )
    for bad in bad_options:
        with pytest.raises(SystemExit) as error:
            face_module.main(bad)
        assert error.value.code == 2


def test_fullscreen_guard_checks_a_few_times_then_stops() -> None:
    guard = FullscreenGuard((0.5, 1.0, 2.0))
    assert not guard.due(0.2)
    assert guard.due(0.6)
    assert not guard.due(0.7)  # cada instante vale uma vez só
    assert guard.due(5.0)  # atrasado: junta os instantes vencidos numa conferência
    assert not guard.due(100.0)  # lista esgotada: nunca mais


def test_needs_fullscreen_only_when_window_is_clearly_smaller() -> None:
    assert needs_fullscreen(376, 290, 800, 480)  # janela do boot que falhou
    assert not needs_fullscreen(800, 480, 800, 480)
    assert not needs_fullscreen(790, 470, 800, 480)  # tolerância de 5%
    assert needs_fullscreen(800, 400, 800, 480)


def tap(gestures: Gestures, t: float) -> str | None:
    gestures.press(t)
    return gestures.release(t + 0.1)


def test_single_tap_makes_the_face_happy() -> None:
    gestures = Gestures()
    assert tap(gestures, 1.0) == "feliz"
    assert tap(gestures, 3.0) == "feliz"  # depois da janela: outra sequência


def test_three_quick_taps_open_camera_and_two_close_it() -> None:
    gestures = Gestures()
    assert [tap(gestures, t) for t in (1.0, 1.3, 1.6)] == ["feliz", None, "camera"]
    assert gestures.camera
    assert tap(gestures, 5.0) is None  # um toque só não volta
    assert tap(gestures, 5.3) == "rosto"
    assert not gestures.camera
    assert tap(gestures, 8.0) == "feliz"


def test_slow_taps_and_long_presses_are_not_a_sequence() -> None:
    gestures = Gestures()
    assert [tap(gestures, t) for t in (1.0, 2.0, 3.0)] == ["feliz"] * 3
    gestures.press(10.0)
    assert gestures.release(10.0 + TAP + 0.05) is None  # não é toque curto
    gestures.press(20.0)
    assert not gestures.held(22.0)
    assert gestures.held(20.0 + HOLD_TO_CLOSE)


def test_camera_gestures_can_be_off_and_long_press_on_camera_keeps_it() -> None:
    off = Gestures(camera_enabled=False)
    assert [tap(off, t) for t in (1.0, 1.3, 1.6)] == ["feliz", None, None]
    assert not off.camera

    on = Gestures()
    for t in (1.0, 1.3, 1.6):
        tap(on, t)
    on.press(5.0)
    assert not on.held(5.0 + 10 * HOLD_TO_CLOSE)
    on.camera_closed()
    assert not on.camera


def input_event(kind: int, code: int, value: int) -> bytes:
    return struct.pack("llHHi", 0, 0, kind, code, value)


def touch_caps() -> str:
    bits = struct.calcsize("l") * 8
    return f"{1 << (0x14A % bits):x}" + " 0" * (0x14A // bits)


def test_touch_changes_reads_only_the_touch_button() -> None:
    data = (
        input_event(1, 0x14A, 1)  # BTN_TOUCH pressionado
        + input_event(3, 0x35, 120)  # posição (ignorada)
        + input_event(0, 0, 0)  # SYN
        + input_event(1, 0x14A, 0)  # BTN_TOUCH solto
        + input_event(1, 0x110, 1)  # outro botão
        + input_event(1, 0x14A, 2)  # repetição
    )
    assert touch_changes(data) == [True, False]
    assert touch_changes(data[:-5]) == [True, False]  # evento cortado no fim


def test_touch_button_capability_bits() -> None:
    assert has_touch_button(touch_caps())
    assert not has_touch_button(" ".join(["0"] * 11))
    assert not has_touch_button("ffff")  # poucas palavras: o bit nem existe
    if struct.calcsize("l") == 8:
        assert has_touch_button("400 0 0 0 0 0")  # a tela de 7" da Pi


def test_find_touchscreen_picks_the_device_with_touch_button(tmp_path: Path) -> None:
    base = tmp_path / "sys/class/input"
    for name, caps in (("event5", None), ("event6", "3 0"), ("event7", touch_caps())):
        folder = base / name / "device" / "capabilities"
        folder.mkdir(parents=True)
        if caps is not None:
            (folder / "key").write_text(caps + "\n")
    assert find_touchscreen(tmp_path) == tmp_path / "dev/input/event7"
    (base / "event7/device/capabilities/key").write_text("0\n")
    assert find_touchscreen(tmp_path) is None


def test_read_touches_streams_presses_until_the_end(tmp_path: Path) -> None:
    device = tmp_path / "event7"
    device.write_bytes(
        input_event(1, 0x14A, 1) + input_event(0, 0, 0) + input_event(1, 0x14A, 0)
    )
    seen: list[tuple[float, bool]] = []
    read_touches(device, lambda t, down: seen.append((t, down)), clock=lambda: 7.0)
    assert seen == [(7.0, True), (7.0, False)]
    read_touches(tmp_path / "nada", lambda t, down: seen.append((t, down)))
    assert len(seen) == 2


class FakeProcess:
    def __init__(self, stubborn: bool = False) -> None:
        self.code: int | None = None
        self.stubborn = stubborn
        self.calls: list[str] = []

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.calls.append("terminate")
        if not self.stubborn:
            self.code = -15

    def kill(self) -> None:
        self.calls.append("kill")
        self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        if self.code is None:
            raise subprocess.TimeoutExpired("rpicam-hello", timeout or 0.0)
        return self.code


def test_camera_view_opens_once_and_closes_the_process() -> None:
    spawned: list[tuple[tuple[str, ...], FakeProcess]] = []

    def spawn(args: Sequence[str]) -> FakeProcess:
        spawned.append((tuple(args), FakeProcess()))
        return spawned[-1][1]

    camera = CameraView(spawn=spawn)
    assert not camera.running()
    assert camera.open() and camera.open()
    assert [args for args, _ in spawned] == [CAMERA_COMMAND]
    assert camera.running()
    camera.close()
    assert spawned[0][1].calls == ["terminate", "wait"]
    assert not camera.running()
    camera.close()  # de novo: nada a fazer
    assert spawned[0][1].calls == ["terminate", "wait"]


def test_camera_view_kills_stubborn_process_and_handles_missing_camera() -> None:
    stubborn = FakeProcess(stubborn=True)
    camera = CameraView(spawn=lambda args: stubborn)
    camera.open()
    camera.close()
    assert stubborn.calls == ["terminate", "wait", "kill", "wait"]

    def missing(args: Sequence[str]) -> FakeProcess:
        raise FileNotFoundError("rpicam-hello")

    assert not CameraView(spawn=missing).open()

    ended = FakeProcess()
    camera = CameraView(spawn=lambda args: ended)
    camera.open()
    ended.code = 1  # o rpicam-hello saiu sozinho (ex.: câmera desconectada)
    assert not camera.running()
