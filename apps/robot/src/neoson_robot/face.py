"""Rosto do robô na tela de 7": dois olhos animados, em tela cheia, sobre fundo preto.

As expressões neutro e surpreso se repetem em loop, com transições suaves, piscadas
e olhares. Durante a conversa o rosto segue o estado do robô (escutando, pensando,
falando, triste no erro, sonolento sem servidor) e um emoji da resposta muda a
expressão por alguns segundos. Esfregar a tela é carinho: os olhos viram arcos
felizes e balançam enquanto o dedo se move. Três toques rápidos (dedo parado)
mostram a câmera em tela cheia e dois voltam ao rosto; segurar 3 s com o dedo
parado, ou Esc, fecha. Dedo que se move não conta como toque nem como segurar,
então os gestos não se confundem. A lógica (formas, linha do tempo, cena em pixels
e gestos) não depende de tela e é testada sem Tk; só `run_window` desenha.
Usa só a biblioteca padrão, então também roda copiado sozinho para a Pi
(`python3 face.py`).
"""

import argparse
import math
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

DESIGN_W, DESIGN_H = 800.0, 480.0  # coordenadas de projeto, escaladas para a tela
TRANSITION = 0.5  # s para trocar de expressão
BLINK = 0.18  # s de uma piscada
REACTION = 2.5  # s de carinho depois do último movimento do dedo
MOOD = 4.0  # s que a expressão pedida por um emoji fica no rosto
TAP = 0.6  # toque mais curto que isso é "toque curto"
HOLD_TO_CLOSE = 3.0
# Caminho do dedo, em larguras de tela: STILL é o tremor de um dedo parado; cada
# PET_DISTANCE percorrida esfregando renova o carinho.
STILL = 0.03
PET_DISTANCE = 0.5
MULTI_TAP = 0.5  # s máximos entre dois toques da mesma sequência
CAMERA_TAPS = 3  # toques rápidos no rosto que abrem a câmera
FACE_TAPS = 2  # toques rápidos na câmera que voltam ao rosto
CAMERA_MAX = 300.0  # s: por segurança, a câmera volta sozinha ao rosto
CAMERA_COMMAND = ("rpicam-hello", "-t", "0", "--fullscreen", "--info-text", "")
ROOT = Path("/")
_EV_KEY, _BTN_TOUCH = 0x01, 0x14A  # linux/input-event-codes.h
_EV_ABS, _ABS_X, _ABS_Y = 0x03, 0x00, 0x01
_INPUT_EVENT = struct.Struct("llHHi")  # struct input_event (timeval, type, code, value)
_ABSINFO = struct.Struct("6i")  # struct input_absinfo (value, min, max, ...)
# Instantes (s) em que o rosto confere se está mesmo em tela cheia.
FULLSCREEN_CHECKS = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 45.0, 60.0)
DEFAULT_FPS = 30
CORNER_STEPS = 8

# Camadas de cada olho, de fora para dentro (quanto crescem além do olho): brilho
# externo, borda e miolo. As cores saem de uma cor base por `palette`.
LAYERS = (9.0, 4.0, 0.0)
DEFAULT_COLOR = "#0a50ff"  # azul saturado, no tom da luz de fundo do LCD do peito
BACKGROUND = "#000000"
Box = tuple[float, float, float, float]  # x0, y0, x1, y1
TOO_TALL = "O pedido não cabe nesta tela. Aprove ou negue pelo canal autenticado."


@dataclass(frozen=True)
class Eye:
    x: float  # centro
    y: float
    w: float
    h: float
    radius: float  # raio dos cantos; metade do menor lado = pílula ou círculo
    smile: float = 0.0  # 0..1: a pálpebra de baixo sobe e o olho vira um arco
    lid: float = 0.0  # 0..1: quanto da altura a pálpebra de cima cobre
    tilt: float = 0.0  # inclina a pálpebra de cima: > 0 baixa o lado de fora

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
    gap: float,
    y: float,
    w: float,
    h: float,
    radius: float,
    smile: float = 0.0,
    lid: float = 0.0,
    tilt: float = 0.0,
) -> Face:
    center = DESIGN_W / 2
    return Face(
        Eye(center - gap, y, w, h, radius, smile, lid, tilt),
        Eye(center + gap, y, w, h, radius, smile, lid, tilt),
    )


EXPRESSIONS: dict[str, Face] = {
    "neutro": _pair(150, 240, 150, 180, 60),
    "feliz": _pair(155, 252, 176, 176, 88, smile=1.0),
    "surpreso": _pair(160, 232, 196, 196, 98),
    "carinho": _pair(150, 256, 170, 170, 85, smile=1.0),
    # Estados do robô (e alguns emojis da resposta).
    "escutando": _pair(150, 236, 164, 200, 72),
    # Olha para o alto, um olho meio fechado: "hmm".
    "pensando": Face(
        Eye(250, 220, 140, 140, 56, lid=0.35), Eye(550, 212, 150, 168, 60)
    ),
    "falando": _pair(150, 240, 150, 176, 60),
    "triste": _pair(150, 258, 150, 150, 58, lid=0.32, tilt=0.4),
    "sonolento": _pair(150, 256, 150, 170, 60, lid=0.55),
}
# Loop sem toque. Feliz e carinho ficam fora do loop; carinho é a reação a esfregar.
PROGRAM: tuple[tuple[str, float], ...] = (
    ("neutro", 8.0),
    ("surpreso", 4.5),
)
# Expressão de cada estado do robô; estado fora daqui (repouso) é o loop.
STATE_FACES = {
    "listening": "escutando",
    "transcribing": "pensando",
    "thinking": "pensando",
    "using_tool": "pensando",
    "speaking": "falando",
    "error": "triste",
    "offline": "sonolento",
}

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
# Estados duram o quanto for preciso: piscada a cada período (s), no instante dado.
_REPEAT = {
    "escutando": (3.5, 2.0),
    "pensando": (4.0, 3.0),
    "falando": (3.2, 1.5),
    "triste": (4.5, 2.5),
    "sonolento": (6.0, 4.0),
}


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def parse_color(value: str) -> tuple[int, int, int]:
    """`#rrggbb` em (r, g, b)."""
    text = value.strip()
    if len(text) != 7 or text[0] != "#":
        raise ValueError("cor no formato #rrggbb")
    red, green, blue = (int(text[i : i + 2], 16) for i in (1, 3, 5))
    return red, green, blue


def palette(color: str) -> tuple[str, str, str]:
    """Cores das camadas a partir da cor base: brilho escuro, borda e miolo."""
    base = parse_color(color)

    def tone(target: int, amount: float) -> str:
        channels = (round(lerp(c, target, amount)) for c in base)
        return "#" + "".join(f"{c:02x}" for c in channels)

    return tone(0, 0.7), tone(0, 0.0), tone(255, 0.10)


DEFAULT_PALETTE = palette(DEFAULT_COLOR)


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
        lerp(a.lid, b.lid, t),
        lerp(a.tilt, b.tilt, t),
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
    elif name == "carinho":
        # Olhos fechados de gosto, balançando devagar de um lado para o outro.
        sway = math.sin(2 * math.pi * 0.8 * u) * 14.0
        face = face.each(lambda eye: eye.moved(dx=sway))
    elif name == "surpreso" and TRANSITION <= u < TRANSITION + 0.5:
        # Já redondos, os olhos dão um salto rápido e voltam ao tamanho.
        pop = 1.0 + 0.12 * math.sin(math.pi * (u - TRANSITION) / 0.5)
        face = face.each(lambda eye: eye.scaled(pop))
    elif name == "escutando":
        # Atento: sem olhar para os lados, os olhos "respiram" devagar.
        breath = 1.0 + 0.03 * math.sin(2 * math.pi * 0.6 * u)
        face = face.each(lambda eye: eye.scaled(breath))
    elif name == "pensando":
        # Olhar para o alto, indo e voltando devagar para o lado.
        drift = 45.0 + 12.0 * math.sin(2 * math.pi * 0.25 * u)
        face = face.each(lambda eye: eye.moved(drift))
    elif name == "falando":
        # Pequenos saltos no ritmo da fala.
        bob = abs(math.sin(math.pi * 2.6 * u)) * 6.0
        face = face.each(lambda eye: eye.moved(dy=-bob))
    elif name == "sonolento":
        nod = math.sin(2 * math.pi * 0.15 * u) * 6.0  # cochilando
        face = face.each(lambda eye: eye.moved(dy=nod))
    if name in _REPEAT:
        period, at = _REPEAT[name]
        factor = blink_factor(u % period, (at,))
    else:
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
    """Linha do tempo do rosto, em camadas: o programa em loop; no lugar dele, a
    expressão do estado do robô enquanto ele vale; e por cima de tudo uma reação
    curta (carinho ao esfregar ou o emoji da frase falada).

    `follow` e `express` podem vir de outra thread: só guardam o pedido, que o
    próximo quadro aplica no tempo do rosto.
    """

    def __init__(self, program: Sequence[tuple[str, float]] = PROGRAM) -> None:
        if not program or any(duration <= TRANSITION for _, duration in program):
            raise ValueError(f"cada fase precisa de mais de {TRANSITION} s")
        self.program = tuple(program)
        self._reaction: float | None = None
        self._until = 0.0
        self._reacting = "carinho"
        self._asked: tuple[str | None, float] = (None, 0.0)
        self._applied: tuple[str | None, float] | None = None
        self._mood: str | None = None
        self._mode: str | None = None
        self._mode_until = self._since = 0.0
        self._from: Face | None = None
        self._base: Face | None = None

    def follow(self, state: str, seconds: float) -> None:
        """Expressão do estado do robô por até `seconds`; repouso volta ao loop."""
        self._asked = (STATE_FACES.get(state), seconds)

    def express(self, name: str) -> None:
        """Mostra a expressão `name` por MOOD s, por cima do estado."""
        if name in EXPRESSIONS:
            self._mood = name

    def react(self, t: float, name: str = "carinho", seconds: float = REACTION) -> None:
        """Começa a reação, ou a estende sem recomeçar se a mesma estiver na tela."""
        if (
            self._reaction is None
            or name != self._reacting
            or not self._reaction <= t < self._until
        ):
            self._reaction = t
        self._reacting, self._until = name, t + seconds

    def frame(self, t: float) -> Face:
        self._apply(t)
        if self._mode is None:
            face = program_face(t, self.program)[1]
        else:
            face = behave(self._mode, t - self._since, math.inf)
        if self._from is not None and t - self._since < TRANSITION:
            face = mix_face(self._from, face, ease((t - self._since) / TRANSITION))
        self._base = face
        if self._reaction is None:
            return face
        r, left = t - self._reaction, self._until - t
        if left <= 0 or r < 0:
            self._reaction = None
            return face
        weight = ease(min(1.0, r / 0.3, left / 0.4))
        over = behave(self._reacting, r + TRANSITION, self._until - self._reaction)
        return mix_face(face, over, weight)

    def _apply(self, t: float) -> None:
        asked = self._asked
        if asked is not self._applied:
            self._applied = asked
            mode, seconds = asked
            if mode != self._mode:  # mesmo estado: só renova a validade
                self._from, self._since = self._base, t
            self._mode, self._mode_until = mode, t + seconds
        elif self._mode is not None and t >= self._mode_until:
            # Estado venceu (ex.: aviso de erro): volta ao loop, sem salto.
            self._from, self._since, self._mode = self._base, t, None
        mood, self._mood = self._mood, None
        if mood is not None:
            self.react(t, mood, MOOD)


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
    outer = LAYERS[0]
    half_w, half_h = eye.w / 2 + outer, eye.h / 2 + outer
    hidden = eye.y + 2 * half_h
    smiling = eye.y + 0.43 * eye.h
    cy = lerp(hidden, smiling, eye.smile)
    return [eye.x - half_w, cy - half_h, eye.x + half_w, cy + half_h]


def upper_lid(eye: Eye) -> list[float]:
    """Pálpebra de cima: polígono preto do topo do brilho até uma linha no olho.

    `lid` diz quanto da altura fica coberto e `tilt` inclina a linha (> 0 baixa o
    lado de fora, como triste). Em lid=0 e tilt=0 fica toda acima do olho.
    """
    outer = LAYERS[0]
    x0, x1 = eye.x - eye.w / 2 - outer, eye.x + eye.w / 2 + outer
    top = eye.y - eye.h / 2 - outer
    base = top + eye.lid * (eye.h + 2 * outer)
    drop = eye.tilt * 0.5 * eye.h
    # O lado de fora do olho esquerdo é o da esquerda.
    if eye.x < DESIGN_W / 2:
        left, right = base + drop, base - drop
    else:
        left, right = base - drop, base + drop
    return [x0, top - 1, x1, top - 1, x1, right, x0, left]


def scene(face: Face, width: float, height: float) -> list[list[float]]:
    """Coordenadas em pixels de cada item, na ordem de desenho.

    Por olho: as camadas de LAYERS (polígonos), a pálpebra de baixo (oval) e a de
    cima (polígono). O projeto de 800x480 é escalado para caber na tela e
    centralizado.
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
        items += [place(eye_outline(eye, grow)) for grow in LAYERS]
        items.append(place(eyelid_box(eye)))
        items.append(place(upper_lid(eye)))
    return items


class FullscreenGuard:
    """Diz quando conferir se a tela cheia pegou.

    No boot, o rosto pode abrir antes de o gerenciador de janelas estar pronto, e o
    primeiro pedido de tela cheia se perde. Por isso o rosto confere algumas vezes no
    primeiro minuto (lista finita: com duas telas, a área total nunca bate com uma
    janela em tela cheia, e o pedido não pode ficar se repetindo para sempre).
    """

    def __init__(self, checks: Sequence[float] = FULLSCREEN_CHECKS) -> None:
        self._pending = sorted(checks)

    def due(self, t: float) -> bool:
        if not self._pending or t < self._pending[0]:
            return False
        while self._pending and t >= self._pending[0]:
            self._pending.pop(0)
        return True


class Gestures:
    """Transforma pressões, movimentos e solturas da tela em ações.

    No rosto: esfregar = "carinho" (a cada PET_DISTANCE de caminho do dedo);
    CAMERA_TAPS toques rápidos = "camera"; segurar parado (HOLD_TO_CLOSE) = fechar,
    conferido por `held`. Na câmera: FACE_TAPS toques rápidos = "rosto". Toque é curto
    e com o dedo parado (até STILL); toques são rápidos quando a soltura de um fica a
    até MULTI_TAP da soltura do anterior. Dedo que se move nunca é toque nem segurar.
    """

    def __init__(self, camera_enabled: bool = True) -> None:
        self.camera_enabled = camera_enabled
        self.camera = False
        self._pressed_at: float | None = None
        self._taps: list[float] = []
        self._last: tuple[float, float] | None = None
        self._path = 0.0
        self._pets = 0

    def press(self, t: float) -> None:
        self._pressed_at = t
        self._last, self._path, self._pets = None, 0.0, 0

    def move(self, t: float, x: float, y: float) -> str | None:
        """Posição do dedo em larguras de tela; devolve "carinho" ao esfregar."""
        if self._pressed_at is None:
            return None
        if self._last is not None:
            self._path += math.hypot(x - self._last[0], y - self._last[1])
        self._last = (x, y)
        if self.camera or self._path < (self._pets + 1) * PET_DISTANCE:
            return None
        self._pets += 1
        return "carinho"

    def release(self, t: float) -> str | None:
        start, self._pressed_at = self._pressed_at, None
        if self._path > STILL:  # esfregou: não entra numa sequência de toques
            self._taps = []
            return None
        if start is None or t - start >= TAP:
            return None
        if self._taps and t - self._taps[-1] > MULTI_TAP:
            self._taps = []
        self._taps.append(t)
        count = len(self._taps)
        if self.camera:
            if count >= FACE_TAPS:
                self.camera_closed()
                return "rosto"
            return None
        if self.camera_enabled and count >= CAMERA_TAPS:
            self._taps = []
            self.camera = True
            return "camera"
        return None

    def held(self, t: float) -> bool:
        """Dedo parado por HOLD_TO_CLOSE no rosto (na câmera, não fecha nada)."""
        pressed = self._pressed_at
        return (
            not self.camera
            and pressed is not None
            and self._path <= STILL
            and t - pressed >= HOLD_TO_CLOSE
        )

    def camera_closed(self) -> None:
        self.camera = False
        self._taps = []

    def reset(self) -> None:
        """Esquece toques em curso (ex.: enquanto um painel de aprovação aparece)."""
        self._pressed_at = None
        self._taps = []
        self._last, self._path, self._pets = None, 0.0, 0


def prompt_buttons(width: float, height: float) -> tuple[Box, Box]:
    """Negar à esquerda e Aprovar à direita, no rodapé, grandes para o dedo."""
    margin, w, h = width * 0.05, width * 0.38, max(64.0, height * 0.18)
    y0, y1 = height - margin - h, height - margin
    return (margin, y0, margin + w, y1), (width - margin - w, y0, width - margin, y1)


def choice_at(x: float, y: float, width: float, height: float) -> bool | None:
    """True = Aprovar, False = Negar, None = fora dos botões."""
    deny, approve = prompt_buttons(width, height)
    for (x0, y0, x1, y1), choice in ((approve, True), (deny, False)):
        if x0 <= x <= x1 and y0 <= y <= y1:
            return choice
    return None


def touch_changes(data: bytes) -> list[bool | tuple[int, int]]:
    """Eventos crus do evdev: pressão (True), soltura (False) e (eixo, valor) da
    posição em ABS_X/ABS_Y."""
    usable = len(data) - len(data) % _INPUT_EVENT.size
    changes: list[bool | tuple[int, int]] = []
    for _sec, _usec, kind, code, value in _INPUT_EVENT.iter_unpack(data[:usable]):
        if kind == _EV_KEY and code == _BTN_TOUCH and value in (0, 1):
            changes.append(value == 1)
        elif kind == _EV_ABS and code in (_ABS_X, _ABS_Y):
            changes.append((code, value))
    return changes


def axis_span(fd: int) -> float:
    """Largura do eixo X do touchscreen (EVIOCGABS); a do projeto se não ler."""
    if sys.platform == "win32":
        return DESIGN_W
    import fcntl

    request = (2 << 30) | (_ABSINFO.size << 16) | (ord("E") << 8) | (0x40 + _ABS_X)
    try:
        _value, low, high, *_ = _ABSINFO.unpack(
            fcntl.ioctl(fd, request, bytes(_ABSINFO.size))
        )
    except OSError:
        return DESIGN_W
    return float(high - low + 1) if high > low else DESIGN_W


def has_touch_button(capabilities: str) -> bool:
    """O `capabilities/key` do sysfs (hex, palavra mais alta primeiro) tem BTN_TOUCH."""
    bits = struct.calcsize("l") * 8
    words = capabilities.split()
    index = len(words) - 1 - _BTN_TOUCH // bits
    if index < 0:
        return False
    return bool(int(words[index], 16) >> (_BTN_TOUCH % bits) & 1)


def find_touchscreen(root: Path = ROOT) -> Path | None:
    """Primeiro `/dev/input/eventN` com botão de toque, ou None."""
    for event in sorted((root / "sys/class/input").glob("event*")):
        try:
            capabilities = (event / "device/capabilities/key").read_text("ascii")
        except (OSError, ValueError):
            continue
        if has_touch_button(capabilities):
            return root / "dev/input" / event.name
    return None


TouchEvent = bool | tuple[float, float]  # pressionou/soltou, ou posição (x, y)


def read_touches(
    path: Path,
    sink: Callable[[float, TouchEvent], None],
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Lê o dispositivo até o fim ou um erro, entregando (instante, evento).

    O evento é True/False (pressionou/soltou) ou a posição do dedo em larguras de
    tela. Não toma o dispositivo para si: a sessão gráfica continua recebendo os
    toques.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    span = axis_span(fd)
    position: dict[int, int] = {}
    pending = b""
    try:
        while True:
            try:
                chunk = os.read(fd, _INPUT_EVENT.size * 16)
            except OSError:
                return
            if not chunk:
                return
            pending += chunk
            usable = len(pending) - len(pending) % _INPUT_EVENT.size
            for change in touch_changes(pending[:usable]):
                if isinstance(change, bool):
                    sink(clock(), change)
                    continue
                position[change[0]] = change[1]
                if len(position) == 2:
                    x, y = position[_ABS_X] / span, position[_ABS_Y] / span
                    sink(clock(), (x, y))
            pending = pending[usable:]
    finally:
        os.close(fd)


class Process(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def _spawn(args: Sequence[str]) -> Process:
    return subprocess.Popen(
        list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class CameraView:
    """Imagem ao vivo da câmera em tela cheia, por cima do rosto (`rpicam-hello`)."""

    def __init__(
        self,
        command: Sequence[str] = CAMERA_COMMAND,
        spawn: Callable[[Sequence[str]], Process] = _spawn,
    ) -> None:
        self._command, self._spawn = tuple(command), spawn
        self._process: Process | None = None

    def open(self) -> bool:
        if self.running():
            return True
        try:
            self._process = self._spawn(self._command)
        except OSError:
            self._process = None
            return False
        return True

    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def needs_fullscreen(width: int, height: int, screen_w: int, screen_h: int) -> bool:
    """A janela está visivelmente menor que a tela (tolerância de 5%)."""
    return width < screen_w * 0.95 or height < screen_h * 0.95


def run_window(
    animator: Animator,
    fps: int,
    fullscreen: bool,
    seconds: float,
    colors: Sequence[str] = DEFAULT_PALETTE,
    touch: Path | None = None,
    camera: CameraView | None = None,
    on_action: Callable[[str, float], None] | None = None,
    status: Callable[[float], str] | None = None,
    prompt: Callable[[], tuple[str, bool] | None] | None = None,
    on_choice: Callable[[bool], None] | None = None,
) -> None:
    """Desenha o rosto em Tk até Esc, toque longo ou `seconds` (0 = sem fim).

    Com `touch` (o `/dev/input/eventN` da tela), os toques são lidos direto do
    dispositivo, inclusive quando a câmera está na frente do rosto; sem ele, vêm
    do Tk, e os gestos da câmera ficam desligados. Quando `prompt` devolve
    (texto, botões), um painel cobre o rosto; com botões, o toque do Tk (que traz a
    posição) chama `on_choice`, e nenhum toque vira gesto enquanto o painel aparece.
    """
    import tkinter

    try:
        root = tkinter.Tk()
    except tkinter.TclError as error:
        raise RuntimeError("sem tela gráfica disponível.") from error
    root.title("NeoSon")
    root.configure(background=BACKGROUND, cursor="none")
    if fullscreen:
        # Já do tamanho da tela: se o pedido de tela cheia se perder, a janela
        # ainda abre grande até o FullscreenGuard refazer o pedido.
        width, height = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{width}x{height}+0+0")
        root.attributes("-fullscreen", True)
    else:
        root.geometry(f"{DESIGN_W:.0f}x{DESIGN_H:.0f}")
    canvas = tkinter.Canvas(
        root, background=BACKGROUND, highlightthickness=0, cursor="none"
    )
    canvas.pack(fill="both", expand=True)
    label = (
        canvas.create_text(0, 0, fill="white", font=("sans", 22)) if status else None
    )
    flat = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    items: list[int] = []
    for _ in range(2):
        for color in colors:
            items.append(canvas.create_polygon(*flat, fill=color, outline=""))
        items.append(canvas.create_oval(0, 0, 0, 0, fill=BACKGROUND, outline=""))
        items.append(canvas.create_polygon(*flat, fill=BACKGROUND, outline=""))
    panel: list[int] = []
    if prompt is not None:
        # Criados por último: ficam por cima do rosto e do status.
        panel.append(
            canvas.create_rectangle(0, 0, 0, 0, fill=BACKGROUND, state="hidden")
        )
        panel.append(
            canvas.create_text(
                0, 0, fill="white", font=("sans", 16), anchor="n", state="hidden"
            )
        )
        for color, text in (("#8b1e1e", "Negar"), ("#1e6b34", "Aprovar")):
            panel.append(
                canvas.create_rectangle(0, 0, 0, 0, fill=color, state="hidden")
            )
            panel.append(
                canvas.create_text(
                    0,
                    0,
                    text=text,
                    fill="white",
                    font=("sans", 26, "bold"),
                    state="hidden",
                )
            )

    start = time.monotonic()
    delay = max(1, round(1000 / fps))
    guard = FullscreenGuard() if fullscreen else None
    gestures = Gestures(camera_enabled=camera is not None and touch is not None)
    touches: queue.SimpleQueue[tuple[float, TouchEvent]] = queue.SimpleQueue()
    camera_since: list[float] = []
    if touch is not None:

        def deliver(at: float, event: TouchEvent) -> None:
            touches.put((at - start, event))

        reader = threading.Thread(target=read_touches, args=(touch, deliver))
        reader.daemon = True
        reader.start()

    def now() -> float:
        return time.monotonic() - start

    def close() -> None:
        if camera is not None:
            camera.close()
        root.destroy()

    def handle(action: str | None, t: float) -> None:
        if action is not None and on_action is not None:
            on_action(action, t)
        if action == "carinho":
            animator.react(t)
        elif action == "camera":
            if camera is not None and camera.open():
                camera_since[:] = [t]
            else:
                gestures.camera_closed()
        elif action == "rosto" and camera is not None:
            camera.close()

    def follow_camera(t: float) -> None:
        """Volta ao rosto se a câmera parou sozinha ou passou do tempo máximo."""
        if not gestures.camera:
            return
        expired = bool(camera_since) and t - camera_since[0] >= CAMERA_MAX
        if camera is None or expired or not camera.running():
            if camera is not None:
                camera.close()
            gestures.camera_closed()

    def keep_fullscreen() -> None:
        size = (root.winfo_width(), root.winfo_height())
        screen = (root.winfo_screenwidth(), root.winfo_screenheight())
        if needs_fullscreen(*size, *screen):
            # Desligar e ligar de novo: o Tk só reenvia o pedido se o estado mudar.
            root.attributes("-fullscreen", False)
            root.attributes("-fullscreen", True)
            root.lift()

    # Botões só valem se o texto desenhado terminou acima deles no último quadro.
    buttons_visible = [False]

    def draw_panel(shown: tuple[str, bool] | None) -> None:
        w, h = canvas.winfo_width(), canvas.winfo_height()
        back, text, deny, deny_label, approve, approve_label = panel
        boxes = prompt_buttons(w, h)
        on = "hidden" if shown is None else "normal"
        canvas.coords(back, 0, 0, w, h)
        canvas.itemconfigure(back, state=on)
        canvas.coords(text, w / 2, 24)
        canvas.itemconfigure(
            text, state=on, text=shown[0] if shown else "", width=max(1, w - 48)
        )
        fits = False
        if shown is not None and shown[1]:
            drawn = canvas.bbox(text)
            fits = drawn is not None and drawn[3] <= boxes[0][1] - 8
            if not fits:
                canvas.itemconfigure(text, text=TOO_TALL)
        buttons_visible[0] = fits
        buttons = "normal" if fits else "hidden"
        for (box, label), (x0, y0, x1, y1) in zip(
            ((deny, deny_label), (approve, approve_label)),
            boxes,
            strict=True,
        ):
            canvas.coords(box, x0, y0, x1, y1)
            canvas.coords(label, (x0 + x1) / 2, (y0 + y1) / 2)
            canvas.itemconfigure(box, state=buttons)
            canvas.itemconfigure(label, state=buttons)

    def tick() -> None:
        t = now()
        shown = prompt() if prompt is not None else None
        if shown is not None:
            gestures.reset()
        while not touches.empty():
            at, event = touches.get_nowait()
            if shown is not None:
                continue  # o painel usa o toque com posição, vindo do Tk
            if event is True:
                gestures.press(at)
            elif event is False:
                handle(gestures.release(at), at)
            else:
                handle(gestures.move(at, *event), at)
        follow_camera(t)
        if (seconds and t >= seconds) or gestures.held(t):
            close()
            return
        if guard is not None and guard.due(t):
            keep_fullscreen()
        coords = scene(animator.frame(t), canvas.winfo_width(), canvas.winfo_height())
        for item, points in zip(items, coords, strict=True):
            canvas.coords(item, points)
        if label is not None and status is not None:
            canvas.coords(label, canvas.winfo_width() / 2, canvas.winfo_height() - 30)
            canvas.itemconfigure(label, text=status(t))
        if panel:
            draw_panel(shown)
        root.after(delay, tick)

    def press(_event: object) -> None:
        if touch is None and (prompt is None or prompt() is None):
            gestures.press(now())

    def release(event: Any) -> None:
        shown = prompt() if prompt is not None else None
        if shown is not None:
            if shown[1] and buttons_visible[0] and on_choice is not None:
                choice = choice_at(
                    event.x, event.y, canvas.winfo_width(), canvas.winfo_height()
                )
                if choice is not None:
                    on_choice(choice)
            return
        if touch is None:
            handle(gestures.release(now()), now())

    def motion(event: Any) -> None:
        # Sem touchscreen lido direto (ex.: em janela), o mouse também faz carinho.
        if touch is None and (prompt is None or prompt() is None):
            span = max(1, canvas.winfo_width())
            handle(gestures.move(now(), event.x / span, event.y / span), now())

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<ButtonRelease-1>", release)
    canvas.bind("<B1-Motion>", motion)
    root.bind("<Escape>", lambda _event: close())
    # SIGTERM (ex.: systemctl stop) também fecha a câmera antes de sair.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: root.after(0, close))
    root.after(0, tick)
    root.mainloop()


def _fps(value: str) -> int:
    fps = int(value)
    if not 5 <= fps <= 60:
        raise argparse.ArgumentTypeError("fps entre 5 e 60")
    return fps


def _color(value: str) -> str:
    try:
        parse_color(value)
    except ValueError:
        raise argparse.ArgumentTypeError("cor no formato #rrggbb") from None
    return value.strip().lower()


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
        help="repete só essa expressão (padrão: neutro e surpreso em loop)",
    )
    parser.add_argument("--fps", type=_fps, default=DEFAULT_FPS)
    parser.add_argument(
        "--color",
        type=_color,
        default=DEFAULT_COLOR,
        help=f"cor base dos olhos, #rrggbb (padrão {DEFAULT_COLOR})",
    )
    parser.add_argument(
        "--seconds", type=_seconds, default=0.0, help="fecha depois de N s (0 = nunca)"
    )
    parser.add_argument(
        "--windowed", action="store_true", help="janela de 800x480 em vez de tela cheia"
    )
    args = parser.parse_args(argv)
    program = ((args.expression, 6.0),) if args.expression else PROGRAM
    # Os gestos da câmera precisam do touchscreen lido direto (a janela da câmera
    # fica na frente do rosto); em janela, para testes, ficam desligados.
    touch = None if args.windowed else find_touchscreen()
    try:
        run_window(
            Animator(program),
            args.fps,
            not args.windowed,
            args.seconds,
            palette(args.color),
            touch=touch,
            camera=CameraView() if touch is not None else None,
        )
    except RuntimeError as error:
        print(f"neoson-face: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
