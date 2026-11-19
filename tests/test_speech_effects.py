import io
import math
import wave
from array import array

import pytest

from neoson_core.engine.voice import VoiceLimits, pcm_wav, read_wav, speakable
from neoson_core.providers.speech_effects import (
    pcm_bytes,
    resample_pcm,
    robot_effect,
    samples,
)


def test_resampling_preserves_linear_signal_and_pcm16() -> None:
    raw = pcm_bytes(array("h", [0, 100, 200, 300, 400, 500]))
    assert list(samples(resample_pcm(raw, 24000))) == [0, 150, 300, 450]
    with pytest.raises(ValueError):
        resample_pcm(b"\x00", 22050)


def test_squirrel_robot_has_the_chosen_pitch_duration_and_peak() -> None:
    factor = 1.45
    raw = pcm_bytes(
        array(
            "h",
            (
                int(12000 * math.sin(2 * math.pi * 440 * i / 16000))
                for i in range(int(16000 * factor))
            ),
        )
    )
    # O backend alongou uma fala de 1 s sem mudar seu tom; o efeito restaura 1 s.
    output = robot_effect(pcm_wav(raw), factor)
    assert read_wav(output, VoiceLimits()).seconds == pytest.approx(1, abs=0.001)
    with wave.open(io.BytesIO(output)) as source:
        signal = samples(source.readframes(source.getnframes()))
    crossings = sum(
        left <= 0 < right for left, right in zip(signal, signal[1:], strict=False)
    )
    assert crossings == pytest.approx(440 * factor, abs=3)
    assert max(abs(value) for value in signal) == pytest.approx(0.9 * 32767, abs=1)
    assert robot_effect(output, 1)  # Timbre sem alterar duração também é suportado.


def test_silence_and_speech_limits_remain_valid() -> None:
    silence = pcm_wav(b"\x00\x00" * 3200)
    assert robot_effect(silence, 1) == silence
    assert len(speakable("palavramuitolongasemespacos", 5)) == 5
    with pytest.raises(ValueError):
        robot_effect(silence, float("nan"))
