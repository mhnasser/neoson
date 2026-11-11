"""Rosto do robô na tela de 7": dois olhos animados, em tela cheia, sobre fundo preto.

As expressões se repetem em loop (neutro, feliz, surpreso) com transições suaves,
piscadas e olhares. Um toque curto deixa o rosto feliz por alguns segundos; um toque
longo (3 s) ou Esc fecha. A lógica (formas, linha do tempo e cena em pixels) não
depende de tela e é testada sem Tk; só `run_window` desenha. Usa só a biblioteca
padrão, então também roda copiado sozinho para a Pi (`python3 face.py`).
"""

import argparse
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

DESIGN_W, DESIGN_H = 800.0, 480.0  # coordenadas de projeto, escaladas para a tela
TRANSITION = 0.5  # s para trocar de expressão
BLINK = 0.18  # s de uma piscada
REACTION = 2.5  # s de alegria depois de um toque
TAP = 0.6  # toque mais curto que isso é "toque curto"
HOLD_TO_CLOSE = 3.0
DEFAULT_FPS = 30
CORNER_STEPS = 8

# Camadas de cada olho, de fora para dentro: brilho, borda e miolo.
GLOW = ((9.0, "#0b3a49"), (4.0, "#5fd3f0"), (0.0, "#effdff"))
BACKGROUND = "#000000"


@dataclass(frozen=True)
class Eye:
    x: float  # centro
    y: float
    w: float
    h: float
    radius: float  # raio dos cantos; metade do menor lado = pílula ou círculo
    smile: float = 0.0  # 0..1: a pálpebra de baixo sobe e o olho vira um arco

    def moved(self, dx: float = 0.0, dy: float = 0.0) -> "Eye":
        return replace(self, x=self.x + dx, y=self.y + dy)

    def scaled(self, sx: float, sy: float | None = None) -> "Eye":
        sy = sx if sy is None else sy
        return replace(
            self, w=self.w * sx, h=self.h * sy, radius=self.radius * min(sx, sy)
        )


@dataclass(frozen=True)
class Face:
    left: Eye
    right: Eye

    def each(self, change: Callable[[Eye], Eye]) -> "Face":
        return Face(change(self.left), change(self.right))


def _pair(
    gap: float, y: float, w: float, h: float, radius: float, smile: float = 0.0
) -> Face:
    center = DESIGN_W / 2
    return Face(
        Eye(center - gap, y, w, h, radius, smile),
        Eye(center + gap, y, w, h, radius, smile),
    )


EXPRESSIONS: dict[str, Face] = {
    "neutro": _pair(150, 240, 150, 180, 60),
    "feliz": _pair(155, 252, 176, 176, 88, smile=1.0),
    "surpreso": _pair(160, 232, 196, 196, 98),
}
PROGRAM: tuple[tuple[str, float], ...] = (
    ("neutro", 8.0),
    ("feliz", 5.0),
    ("surpreso", 4.5),
)

# Olhar para os lados na expressão neutra: (instante, deslocamento em x).
_GLANCE = (
    (0.0, 0.0),
    (1.6, 0.0),
    (2.0, -60.0),
    (3.3, -60.0),
    (3.7, 0.0),
    (4.6, 0.0),
    (5.0, 60.0),
    (6.1, 60.0),
    (6.5, 0.0),
)
_BLINKS = {"neutro": (1.0, 4.2, 6.9, 7.25), "surpreso": (2.4,)}


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease(t: float) -> float:
    """Suaviza 0..1 (começa e termina devagar)."""
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def mix_eye(a: Eye, b: Eye, t: float) -> Eye:
    return Eye(
        lerp(a.x, b.x, t),
        lerp(a.y, b.y, t),
        lerp(a.w, b.w, t),
        lerp(a.h, b.h, t),
        lerp(a.radius, b.radius, t),
        lerp(a.smile, b.smile, t),
    )


def mix_face(a: Face, b: Face, t: float) -> Face:
    return Face(mix_eye(a.left, b.left, t), mix_eye(a.right, b.right, t))


def blink_factor(u: float, times: Sequence[float]) -> float:
    """Altura relativa do olho: 1 aberto, até 0,08 no meio de uma piscada."""
    for start in times:
        k = (u - start) / BLINK
        if 0.0 <= k <= 1.0:
            return 1.0 - 0.92 * math.sin(math.pi * k)
    return 1.0


def track(u: float, keys: Sequence[tuple[float, float]]) -> float:
    """Valor entre quadros-chave (instante, valor), com transição suave."""
    if u <= keys[0][0]:
        return keys[0][1]
    for (t0, v0), (t1, v1) in zip(keys, keys[1:], strict=False):
        if u <= t1:
            return lerp(v0, v1, ease((u - t0) / (t1 - t0)))
    return keys[-1][1]


def behave(name: str, u: float, duration: float) -> Face:
    """Expressão `name` no instante `u` da sua fase, com seus movimentos."""
    face = EXPRESSIONS[name]
    if name == "neutro":
        dx = track(u, _GLANCE)
        # O olho do lado para onde ele olha fica um pouco maior.
        lean = dx / 60.0 * 0.04
        face = Face(
            face.left.moved(dx).scaled(1.0, 1.0 - lean),
            face.right.moved(dx).scaled(1.0, 1.0 + lean),
        )
    elif name == "feliz":
        envelope = min(1.0, max(0.0, (u - TRANSITION) / 0.3), (duration - u) / 0.4)
        hop = abs(math.sin(math.pi * 2.2 * u)) * 12.0 * max(0.0, envelope)
        face = face.each(lambda eye: eye.moved(dy=-hop))
    elif name == "surpreso" and TRANSITION <= u < TRANSITION + 0.5:
        # Já redondos, os olhos dão um salto rápido e voltam ao tamanho.
        pop = 1.0 + 0.12 * math.sin(math.pi * (u - TRANSITION) / 0.5)
        face = face.each(lambda eye: eye.scaled(pop))
    factor = blink_factor(u, _BLINKS.get(name, ()))
    if factor < 1.0:
        face = face.each(lambda eye: eye.scaled(1.0, factor))
    return face


def program_face(
    t: float, program: Sequence[tuple[str, float]] = PROGRAM
) -> tuple[str, Face]:
    """Expressão e rosto no instante `t` do programa em loop."""
    total = sum(duration for _, duration in program)
    t %= total
    start = 0.0
    for index, (name, duration) in enumerate(program):
        if t < start + duration or index == len(program) - 1:
            u = t - start
            face = behave(name, u, duration)
            if u < TRANSITION:
                previous = EXPRESSIONS[program[index - 1][0]]
                face = mix_face(previous, face, ease(u / TRANSITION))
            return name, face
        start += duration
    raise ValueError("programa vazio")


class Animator:
    """Linha do tempo do rosto, com a reação a toques por cima do programa."""

    def __init__(self, program: Sequence[tuple[str, float]] = PROGRAM) -> None:
        if not program or any(duration <= TRANSITION for _, duration in program):
            raise ValueError(f"cada fase precisa de mais de {TRANSITION} s")
        self.program = tuple(program)
        self._reaction: float | None = None

    def react(self, t: float) -> None:
        self._reaction = t

    def frame(self, t: float) -> Face:
        _, face = program_face(t, self.program)
        if self._reaction is None:
            return face
        r = t - self._reaction
        if r >= REACTION or r < 0:
            self._reaction = None
            return face
        weight = ease(min(1.0, r / 0.3, (REACTION - r) / 0.4))
        return mix_face(face, behave("feliz", r + TRANSITION, REACTION), weight)


def rounded_rect(
    x0: float, y0: float, x1: float, y1: float, radius: float
) -> list[float]:
    """Contorno de um retângulo de cantos redondos, sempre com o mesmo nº de pontos."""
    r = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    corners = (
        (x1 - r, y0 + r, -90.0),
        (x1 - r, y1 - r, 0.0),
        (x0 + r, y1 - r, 90.0),
        (x0 + r, y0 + r, 180.0),
    )
    points: list[float] = []
    for cx, cy, start in corners:
        for step in range(CORNER_STEPS + 1):
            angle = math.radians(start + 90.0 * step / CORNER_STEPS)
            points += [cx + r * math.cos(angle), cy + r * math.sin(angle)]
    return points


def eye_outline(eye: Eye, grow: float = 0.0) -> list[float]:
    half_w, half_h = eye.w / 2 + grow, eye.h / 2 + grow
    return rounded_rect(
        eye.x - half_w,
        eye.y - half_h,
        eye.x + half_w,
        eye.y + half_h,
        eye.radius + grow,
    )


def eyelid_box(eye: Eye) -> list[float]:
    """Pálpebra de baixo: oval preto do tamanho do brilho externo do olho.

    Em smile=0 fica logo abaixo do olho; em smile=1 sobe e recorta o olho redondo
    num arco de pontas finas, como "^".
    """
    outer = GLOW[0][0]
    half_w, half_h = eye.w / 2 + outer, eye.h / 2 + outer
    hidden = eye.y + 2 * half_h
    smiling = eye.y + 0.43 * eye.h
    cy = lerp(hidden, smiling, eye.smile)
    return [eye.x - half_w, cy - half_h, eye.x + half_w, cy + half_h]


def scene(face: Face, width: float, height: float) -> list[list[float]]:
    """Coordenadas em pixels de cada item, na ordem de desenho.

    Por olho: as camadas de GLOW (polígonos) e a pálpebra (oval). O projeto de
    800x480 é escalado para caber na tela e centralizado.
    """
    scale = min(width / DESIGN_W, height / DESIGN_H)
    ox, oy = (width - DESIGN_W * scale) / 2, (height - DESIGN_H * scale) / 2

    def place(points: list[float]) -> list[float]:
        return [
            (ox if index % 2 == 0 else oy) + value * scale
            for index, value in enumerate(points)
        ]

    items: list[list[float]] = []
    for eye in (face.left, face.right):
        items += [place(eye_outline(eye, grow)) for grow, _ in GLOW]
        items.append(place(eyelid_box(eye)))
    return items


def run_window(animator: Animator, fps: int, fullscreen: bool, seconds: float) -> None:
    """Desenha o rosto em Tk até Esc, toque longo ou `seconds` (0 = sem fim)."""
    import tkinter

    try:
        root = tkinter.Tk()
    except tkinter.TclError as error:
        raise RuntimeError("sem tela gráfica disponível.") from error
    root.title("NeoSon")
    root.configure(background=BACKGROUND, cursor="none")
    if fullscreen:
        root.attributes("-fullscreen", True)
    else:
        root.geometry(f"{DESIGN_W:.0f}x{DESIGN_H:.0f}")
    canvas = tkinter.Canvas(
        root, background=BACKGROUND, highlightthickness=0, cursor="none"
    )
    canvas.pack(fill="both", expand=True)
    flat = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    items: list[int] = []
    for _ in range(2):
        for _grow, color in GLOW:
            items.append(canvas.create_polygon(*flat, fill=color, outline=""))
        items.append(canvas.create_oval(0, 0, 0, 0, fill=BACKGROUND, outline=""))

    start = time.monotonic()
    delay = max(1, round(1000 / fps))
    pressed: list[float] = []

    def now() -> float:
        return time.monotonic() - start

    def tick() -> None:
        t = now()
        if (seconds and t >= seconds) or (pressed and t - pressed[0] >= HOLD_TO_CLOSE):
            root.destroy()
            return
        coords = scene(animator.frame(t), canvas.winfo_width(), canvas.winfo_height())
        for item, points in zip(items, coords, strict=True):
            canvas.coords(item, points)
        root.after(delay, tick)

    def press(_event: object) -> None:
        pressed[:] = [now()]

    def release(_event: object) -> None:
        if pressed and now() - pressed[0] < TAP:
            animator.react(now())
        pressed.clear()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", lambda _event: root.destroy())
    root.after(0, tick)
    root.mainloop()


def _fps(value: str) -> int:
    fps = int(value)
    if not 5 <= fps <= 60:
        raise argparse.ArgumentTypeError("fps entre 5 e 60")
    return fps


def _seconds(value: str) -> float:
    seconds = float(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("tempo não pode ser negativo")
    return seconds


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-face",
        description="Mostra o rosto animado do robô em tela cheia.",
    )
    parser.add_argument(
        "--expression",
        choices=sorted(EXPRESSIONS),
        help="repete só essa expressão (padrão: neutro, feliz e surpreso em loop)",
    )
    parser.add_argument("--fps", type=_fps, default=DEFAULT_FPS)
    parser.add_argument(
        "--seconds", type=_seconds, default=0.0, help="fecha depois de N s (0 = nunca)"
    )
    parser.add_argument(
        "--windowed", action="store_true", help="janela de 800x480 em vez de tela cheia"
    )
    args = parser.parse_args(argv)
    program = ((args.expression, 6.0),) if args.expression else PROGRAM
    try:
        run_window(Animator(program), args.fps, not args.windowed, args.seconds)
    except RuntimeError as error:
        print(f"neoson-face: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
