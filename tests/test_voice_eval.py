import asyncio
import hashlib
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_whisper_speech import FAKE_HELPER

from neoson_core.engine.voice import AudioClip, pcm_wav
from neoson_core.infrastructure.config import ServerSettings
from neoson_core.providers import whisper_speech
from neoson_core.providers.speech_effects import EffectSynthesizer
from neoson_gateway import voice_eval
from neoson_gateway.voice_eval import (
    KEY_FILE,
    SCORES_FILE,
    TTS_PHRASES,
    Clip,
    errors,
    evaluate_stt,
    load_corpus,
    make_samples,
    normalize,
    render_panel,
    score_samples,
    spoken,
)

# Palavras que só existem no "texto falado": não podem aparecer em relatório ou painel.
SECRET = ("Aurora", "Jardim Lunar", "hortelã")


@pytest.mark.parametrize(
    ("number", "words"),
    [
        (0, "zero"),
        (7, "sete"),
        (16, "dezesseis"),
        (21, "vinte e um"),
        (100, "cem"),
        (101, "cento e um"),
        (125, "cento e vinte e cinco"),
        (200, "duzentos"),
        (1000, "mil"),
        (1200, "mil e duzentos"),
        (1250, "mil duzentos e cinquenta"),
        (2027, "dois mil e vinte e sete"),
        (23000, "vinte e três mil"),
        (1_000_000, "1000000"),
    ],
)
def test_numbers_are_spelled_like_the_corpus(number: int, words: str) -> None:
    assert spoken(number) == words


def test_normalization_ignores_case_punctuation_and_digits_but_not_accents() -> None:
    assert normalize("Quanto é 125 dividido por 5?") == normalize(
        "quanto é cento e vinte e cinco dividido por cinco"
    )
    assert normalize("Olá, NeoSon!") == ["olá", "neoson"]
    assert normalize("guarda-chuva_x") == ["guarda", "chuva", "x"]
    assert normalize("exceção") != normalize("excecao")


@pytest.mark.parametrize(
    ("expected", "heard", "result"),
    [
        ("a b c", "a b c", (0, 3, 0, 5)),
        ("a b c", "a x c", (1, 3, 1, 5)),
        ("a b c", "a c", (1, 3, 2, 5)),
        ("a b c", "a b b c", (1, 3, 2, 5)),
        ("a b c", "", (3, 3, 5, 5)),
        ("casa", "caso", (1, 1, 1, 4)),
        ("Sete vezes oito.", "7 vezes 8", (0, 3, 0, 15)),
    ],
)
def test_error_rates_on_known_cases(
    expected: str, heard: str, result: tuple[int, int, int, int]
) -> None:
    assert errors(expected, heard) == result


def write_corpus(folder: Path, lines: list[str], frames: int = 1600) -> None:
    folder.mkdir()
    for index in range(1, len(lines) + 1):
        (folder / f"{index:02d}.wav").write_bytes(pcm_wav(b"\x01\x00" * frames))
    (folder / "manifest.jsonl").write_text(
        "".join(
            json.dumps({"audio": f"{i:02d}.wav", "expected_text": text}) + "\n"
            for i, text in enumerate(lines, 1)
        ),
        encoding="utf-8",
    )


def test_corpus_is_read_from_the_recorded_manifest(tmp_path: Path) -> None:
    corpus = tmp_path / "sessao"
    write_corpus(corpus, ["Meu personagem se chama Aurora.", "Jardim Lunar."])
    clips, checksum = load_corpus(corpus)
    assert [c.id for c in clips] == ["01", "02"]
    assert clips[0].audio.seconds == pytest.approx(0.1)
    assert (
        checksum == hashlib.sha256((corpus / "manifest.jsonl").read_bytes()).hexdigest()
    )
    assert "Aurora" not in repr(clips[0])


@pytest.mark.parametrize(
    "line",
    [
        '{"audio": "../01.wav", "expected_text": "x"}',
        '{"audio": "01.wav", "expected_text": ""}',
        '{"audio": "01.wav"}',
        '["01.wav"]',
        "não é json",
    ],
)
def test_invalid_manifest_entries_are_rejected(tmp_path: Path, line: str) -> None:
    corpus = tmp_path / "sessao"
    write_corpus(corpus, ["x"])
    (corpus / "manifest.jsonl").write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_corpus(corpus)


def test_missing_or_invalid_audio_is_reported(tmp_path: Path) -> None:
    corpus = tmp_path / "sessao"
    write_corpus(corpus, ["x", "y"])
    (corpus / "02.wav").write_bytes(b"RIFF")
    with pytest.raises(ValueError, match="02.wav"):
        load_corpus(corpus)
    (corpus / "02.wav").unlink()
    with pytest.raises(FileNotFoundError):
        load_corpus(corpus)
    (corpus / "manifest.jsonl").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="vazio"):
        load_corpus(corpus)


class Scripted:
    """Responde por clipe; `None` simula falha. Mede CPU como o Whisper."""

    def __init__(self, replies: dict[str, str | None], cpu: float | None) -> None:
        self.replies, self.cpu_seconds, self.closed = replies, cpu, False
        self.by_audio: dict[int, str] = {}

    async def transcribe(self, audio: AudioClip) -> str:
        reply = self.replies[self.by_audio[id(audio)]]
        if reply is None:
            raise RuntimeError("falhou")
        return reply

    async def close(self) -> None:
        self.closed = True


def ticking(step: float) -> Callable[[], float]:
    now = [0.0]

    def clock() -> float:
        now[0] += step
        return now[0]

    return clock


def clips() -> list[Clip]:
    texts = ["Meu personagem se chama Aurora.", "O projeto é Jardim Lunar com hortelã."]
    return [
        Clip(f"{i:02d}", AudioClip(pcm_wav(b"\0\0" * 16000), 1.0), text)
        for i, text in enumerate(texts, 1)
    ]


def test_stt_evaluation_aggregates_without_content() -> None:
    corpus = clips()
    perfect = Scripted({"01": corpus[0].expected, "02": corpus[1].expected}, 0.5)
    sloppy = Scripted({"01": "meu personagem se chama", "02": None}, None)
    for fake in (perfect, sloppy):
        fake.by_audio = {id(c.audio): c.id for c in corpus}

    report = asyncio.run(
        evaluate_stt(
            corpus,
            "c" * 64,
            {"perfeito": lambda: perfect, "ruim": lambda: sloppy},
            clock=ticking(0.25),
        )
    )

    good, bad = report["candidates"]["perfeito"], report["candidates"]["ruim"]
    assert good["wer"] == 0 and good["cer"] == 0 and good["failures"] == 0
    assert good["mean_seconds"] == 0.25 and good["real_time_factor"] == 0.25
    assert good["cpu_seconds"] == 0.5 and good["first_call_seconds"] == 0.25
    # 1 palavra faltando em 5 e o clipe 2 (7 palavras) falhou inteiro: 8 de 12.
    assert bad["wer"] == round(8 / 12, 4) and bad["failures"] == 1
    assert bad["cpu_seconds"] is None and bad["mean_seconds"] == 0.25
    assert perfect.closed and sloppy.closed
    assert report["clips"] == 2 and report["audio_seconds"] == 2
    assert report["cases"][1]["ruim"] == {"ok": False, "wer": 1.0, "seconds": 0.25}

    panel = render_panel(report)
    assert "| perfeito | 0.0% | 0.0% |" in panel and "| ruim | 66.7% |" in panel
    dumped = json.dumps(report, ensure_ascii=False) + panel
    for secret in SECRET:
        assert secret not in dumped and secret.lower() not in dumped


class Voice:
    def __init__(self, size: int) -> None:
        self.size, self.closed = size, False
        self.spoken: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.spoken.append(text)
        return pcm_wav(b"\0\0" * self.size)

    async def close(self) -> None:
        self.closed = True


def test_blind_samples_hide_the_provider_and_scores_are_joined(
    tmp_path: Path,
) -> None:
    piper, openai = Voice(1600), Voice(3200)
    output = tmp_path / "amostras"
    count = asyncio.run(
        make_samples(
            output,
            # O efeito embrulha o Piper, como na composição: o processo ainda fecha.
            {"piper": lambda: EffectSynthesizer(piper, 1.0), "openai": lambda: openai},
            rng=random.Random(7),
        )
    )

    assert count == 2 * len(TTS_PHRASES) and piper.closed and openai.closed
    assert "Néusson" in piper.spoken[-1]  # mesma pronúncia da conversa
    files = sorted(p.name for p in output.iterdir())
    assert files == sorted(
        [KEY_FILE, SCORES_FILE] + [f"amostra-{n:02d}.wav" for n in range(1, 11)]
    )
    assert all("piper" not in f and "openai" not in f for f in files if f != KEY_FILE)
    key = json.loads((output / KEY_FILE).read_text(encoding="utf-8"))
    order = [key[f"amostra-{n:02d}"]["candidate"] for n in range(1, 11)]
    assert order != ["piper"] * 5 + ["openai"] * 5 and sorted(order).count("piper") == 5
    scores = (output / SCORES_FILE).read_text(encoding="utf-8").splitlines()
    assert scores[0] == "amostra,nota" and scores[1] == "amostra-01,"

    # Planilha em português: BOM e ponto e vírgula.
    notes = {label: 5 if v["candidate"] == "piper" else 3 for label, v in key.items()}
    notes["amostra-01"] = 4 if key["amostra-01"]["candidate"] == "piper" else 2
    (output / SCORES_FILE).write_text(
        "﻿amostra;nota\n" + "".join(f"{k};{v}\n" for k, v in notes.items()),
        encoding="utf-8",
    )
    report = score_samples(output)
    first_is_piper = key["amostra-01"]["candidate"] == "piper"
    assert report["candidates"]["piper"]["mean"] == (4.8 if first_is_piper else 5)
    assert report["candidates"]["openai"]["min"] == (3 if first_is_piper else 2)
    assert report["samples"] == 10 and report["phrases"] == 5
    panel = render_panel(report)
    assert "| openai |" in panel and "| piper |" in panel
    for phrase in TTS_PHRASES:
        assert phrase not in panel and phrase not in json.dumps(report)

    with pytest.raises(ValueError, match="pasta nova"):
        asyncio.run(make_samples(output, {"piper": lambda: piper}))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("amostra-01,6", "1 a 5"),
        ("amostra-01,", "1 a 5"),
        ("amostra-99,3", "desconhecida"),
        ("", "sem nota em amostra-01"),
    ],
)
def test_scores_must_be_complete_and_between_one_and_five(
    tmp_path: Path, change: str, message: str
) -> None:
    key = {f"amostra-0{n}": {"candidate": "piper", "phrase": n} for n in (1, 2)}
    (tmp_path / KEY_FILE).write_text(json.dumps(key))
    (tmp_path / SCORES_FILE).write_text(f"amostra,nota\n{change}\namostra-02,4\n")
    with pytest.raises(ValueError, match=message):
        score_samples(tmp_path)


def test_samples_never_go_inside_a_git_checkout(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="Git"):
        asyncio.run(make_samples(tmp_path / "amostras", {"piper": lambda: Voice(1)}))
    assert not (tmp_path / "amostras").exists()


def test_command_measures_local_whisper_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(whisper_speech, "HELPER", FAKE_HELPER)
    settings = ServerSettings(
        _env_file=None,
        llm_provider="echo",
        vault_path=tmp_path / "vault",
        whisper_python=Path(sys.executable),
    )
    monkeypatch.setattr(voice_eval, "load_settings", lambda: settings)
    corpus = tmp_path / "sessao"
    write_corpus(corpus, ["Meu personagem se chama Aurora.", "ouvi três pequeno pt"])
    reports = tmp_path / "relatorios"

    assert (
        voice_eval.main(
            [
                "stt",
                "--corpus",
                str(corpus),
                "--candidate",
                "whisper:pequeno",
                "--timeout",
                "10",
                "--output",
                str(reports),
            ]
        )
        == 0
    )

    (saved,) = reports.iterdir()
    report = json.loads(saved.read_text(encoding="utf-8"))
    summary = report["candidates"]["whisper:pequeno"]
    assert summary["failures"] == 0 and summary["cpu_seconds"] == 0.25
    # O helper simulado responde "ouvi N pequeno pt"; a chamada 1 é o aquecimento,
    # então o 2º clipe ouve "ouvi 3 pequeno pt" e acerta tudo.
    assert report["cases"][1]["whisper:pequeno"]["wer"] == 0
    panel = (tmp_path / "vault" / "Dashboard" / "Voz-Transcricao.md").read_text(
        encoding="utf-8"
    )
    assert "| whisper:pequeno |" in panel and "Aurora" not in panel
    assert "Aurora" not in capsys.readouterr().out

    assert voice_eval.main(["stt", "--corpus", str(corpus), "--candidate", "x"]) == 1
    assert "desconhecido" in capsys.readouterr().err
