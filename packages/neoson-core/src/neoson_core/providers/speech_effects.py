"""PCM16 em memória: conversão linear e timbre esquilo/robô suave, sem dependências."""

import asyncio
import io
import math
import sys
import wave
from array import array

from neoson_core.engine.voice import Synthesizer, VoiceLimits, pcm_wav, read_wav


def samples(pcm: bytes) -> array[int]:
    if len(pcm) % 2:
        raise ValueError("PCM16 incompleto.")
    result = array("h")
    result.frombytes(pcm)
    if sys.byteorder == "big":
        result.byteswap()
    return result


def pcm_bytes(values: array[int]) -> bytes:
    if sys.byteorder == "big":
        values.byteswap()
    return values.tobytes()


def resample_pcm(pcm: bytes, source_rate: float, target_rate: float = 16000) -> bytes:
    if not all(
        math.isfinite(rate) and 8000 <= rate <= 96000
        for rate in (source_rate, target_rate)
    ):
        raise ValueError("Taxa de amostragem inválida.")
    source = samples(pcm)
    output = array("h")
    for index in range(int(len(source) * target_rate / source_rate)):
        position = index * source_rate / target_rate
        left = min(int(position), len(source) - 1)
        right = min(left + 1, len(source) - 1)
        output.append(
            round(source[left] + (source[right] - source[left]) * (position - left))
        )
    return pcm_bytes(output)


def robot_effect(wav: bytes, factor: float) -> bytes:
    if not math.isfinite(factor) or not 1 <= factor <= 2:
        raise ValueError("Fator de voz inválido.")
    # O sintetizador compensa a duração por length_scale/speed antes do pitch.
    clip = read_wav(wav, VoiceLimits(max_bytes=2097152), 30 * factor)
    with wave.open(io.BytesIO(clip.wav), "rb") as source:
        pcm = source.readframes(source.getnframes())
    pitched = samples(resample_pcm(pcm, 16000 * factor))
    output: list[float] = []
    delay = 96  # 6 ms em 16 kHz.
    for index, value in enumerate(pitched):
        mixed = value * (0.7 + 0.3 * math.sin(2 * math.pi * 35 * index / 16000))
        if index >= delay:
            mixed += 0.45 * output[index - delay]
        output.append(mixed)
    peak = max((abs(value) for value in output), default=0)
    scale = 0.9 * 32767 / peak if peak else 1
    return pcm_wav(pcm_bytes(array("h", (int(value * scale) for value in output))))


class EffectSynthesizer:
    def __init__(self, base: Synthesizer, factor: float = 1) -> None:
        self.base, self.factor = base, factor

    async def synthesize(self, text: str) -> bytes:
        wav = await self.base.synthesize(text)
        return await asyncio.to_thread(robot_effect, wav, self.factor)
