from dataclasses import astuple

import pytest

from neoson_robot import face as face_module
from neoson_robot.face import (
    BLINK,
    CORNER_STEPS,
    DEFAULT_PALETTE,
    EXPRESSIONS,
    LAYERS,
    PROGRAM,
    REACTION,
    TRANSITION,
    Animator,
    Eye,
    Face,
    FullscreenGuard,
    blink_factor,
    eye_outline,
    eyelid_box,
    needs_fullscreen,
    palette,
    parse_color,
    program_face,
    rounded_rect,
    scene,
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


def test_cli_runs_window_with_chosen_program(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_run(
        animator: Animator,
        fps: int,
        fullscreen: bool,
        seconds: float,
        colors: tuple[str, str, str],
    ) -> None:
        calls.append((animator.program, fps, fullscreen, seconds, colors))

    monkeypatch.setattr(face_module, "run_window", fake_run)
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
    assert calls == [
        (PROGRAM, 30, True, 0.0, DEFAULT_PALETTE),
        ((("feliz", 6.0),), 20, False, 0.0, palette("#ff8800")),
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
    ) -> None:
        raise RuntimeError("sem tela gráfica disponível.")

    monkeypatch.setattr(face_module, "run_window", no_display)
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
