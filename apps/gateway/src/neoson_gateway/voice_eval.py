"""Comando `neoson-voice-eval`: mede a transcrição no corpus gravado e prepara amostras
cegas de síntese para o usuário dar notas.

Relatórios e painéis só com agregados e números de amostra: nada de áudio, texto
esperado ou transcrição. Os candidatos são montados como o gateway os montaria.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import random
import re
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from neoson_core.engine.voice import (
    AudioClip,
    InvalidAudio,
    Synthesizer,
    Transcriber,
    VoiceLimits,
    for_speech,
    read_wav,
)
from neoson_core.infrastructure.config import ConfigError, ServerSettings, load_settings
from neoson_core.telemetry.vault import frontmatter, write_atomic
from neoson_gateway.composition import build_synthesizer, build_transcriber

REPORTS = Path("data/voice-eval")
STT_PANEL = Path("Dashboard") / "Voz-Transcricao.md"
TTS_PANEL = Path("Dashboard") / "Voz-Sintese.md"
KEY_FILE = ".gabarito.json"
SCORES_FILE = "notas.csv"
TTS_CANDIDATES = ("piper", "openai")
# Respostas fictícias no tom do assistente: a síntese fala respostas, não perguntas.
TTS_PHRASES = (
    "Bom dia! Hoje o céu fica aberto, com máxima de vinte e oito graus.",
    "Anotei o compromisso fictício para amanhã às nove horas.",
    "Sete vezes oito dá cinquenta e seis.",
    "Não encontrei nada no histórico sobre a receita de bolo de cenoura.",
    "Oi, eu sou o NeoSon. Preciso da sua aprovação antes de criar o rascunho.",
)
UNITS = (
    "zero um dois três quatro cinco seis sete oito nove dez onze doze treze catorze "
    "quinze dezesseis dezessete dezoito dezenove"
).split()
TENS = "_ _ vinte trinta quarenta cinquenta sessenta setenta oitenta noventa".split()
HUNDREDS = (
    "_ cento duzentos trezentos quatrocentos quinhentos seiscentos setecentos "
    "oitocentos novecentos"
).split()


@dataclass(frozen=True, slots=True)
class Clip:
    id: str
    audio: AudioClip = field(repr=False)
    expected: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Row:
    ok: bool
    seconds: float
    audio_seconds: float
    cpu: float | None
    word_errors: int
    words: int
    char_errors: int
    chars: int


def spoken(number: int) -> str:
    """Inteiro por extenso, como o corpus escreve ("dois mil e vinte e sete")."""
    if number < 20:
        return UNITS[number]
    if number < 100:
        tens, unit = divmod(number, 10)
        return TENS[tens] + (f" e {UNITS[unit]}" if unit else "")
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        if not rest:
            return "cem" if hundreds == 1 else HUNDREDS[hundreds]
        return f"{HUNDREDS[hundreds]} e {spoken(rest)}"
    if number < 1_000_000:
        thousands, rest = divmod(number, 1000)
        head = "mil" if thousands == 1 else f"{spoken(thousands)} mil"
        if not rest:
            return head
        return head + (" e " if rest < 100 or rest % 100 == 0 else " ") + spoken(rest)
    return str(number)


def normalize(text: str) -> list[str]:
    """Minúsculas, sem pontuação e números por extenso; acentos contam."""
    text = unicodedata.normalize("NFC", text).casefold()
    text = re.sub(r"\d+", lambda digits: f" {spoken(int(digits[0]))} ", text)
    return re.sub(r"[^\w]|_", " ", text).split()


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, 1):
        current = [row]
        for column, heard in enumerate(hypothesis, 1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (expected != heard),
                )
            )
        previous = current
    return previous[-1]


def errors(expected: str, heard: str) -> tuple[int, int, int, int]:
    """Erros e tamanho da referência, em palavras e em caracteres."""
    words, guess = normalize(expected), normalize(heard)
    chars, guessed = " ".join(words), " ".join(guess)
    return (
        edit_distance(words, guess),
        len(words),
        edit_distance(chars, guessed),
        len(chars),
    )


def load_corpus(directory: Path) -> tuple[list[Clip], str]:
    """Clipes do `manifest.jsonl` do `record_batch` e o SHA-256 do manifesto."""
    manifest = (directory / "manifest.jsonl").read_bytes()
    clips = []
    for number, line in enumerate(manifest.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            entry = {}
        name, expected = entry.get("audio"), entry.get("expected_text")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[\w-]{1,60}\.wav", name, re.ASCII)
            or not isinstance(expected, str)
            or not expected.strip()
            or len(expected) > 500
        ):
            raise ValueError(f"manifest.jsonl, linha {number}: entrada inválida.")
        try:
            audio = read_wav((directory / name).read_bytes(), VoiceLimits())
        except InvalidAudio as error:
            raise ValueError(f"{name}: {error}") from None
        clips.append(Clip(name.removesuffix(".wav"), audio, expected))
    if not clips:
        raise ValueError("Corpus vazio.")
    return clips, hashlib.sha256(manifest).hexdigest()


async def evaluate_stt(
    clips: Sequence[Clip],
    checksum: str,
    candidates: Mapping[str, Callable[[], Transcriber]],
    *,
    clock: Callable[[], float] = perf_counter,
) -> dict[str, Any]:
    """Um candidato por vez: modelos grandes não cabem juntos na RAM da Pi."""
    cases: list[dict[str, Any]] = [
        {"id": clip.id, "seconds": round(clip.audio.seconds, 3)} for clip in clips
    ]
    summaries = {}
    for name, build in candidates.items():
        transcriber = build()
        rows = []
        try:
            # A 1ª chamada carrega o modelo: medida à parte, fora das médias.
            start = clock()
            try:
                await transcriber.transcribe(clips[0].audio)
            except Exception:
                pass
            first = clock() - start
            for clip, case in zip(clips, cases, strict=True):
                start = clock()
                try:
                    heard, ok = await transcriber.transcribe(clip.audio), True
                except Exception:
                    # Falha conta como transcrição vazia: o clipe inteiro errado.
                    heard, ok = "", False
                seconds = clock() - start
                cpu = getattr(transcriber, "cpu_seconds", None) if ok else None
                row = _Row(
                    ok, seconds, clip.audio.seconds, cpu, *errors(clip.expected, heard)
                )
                rows.append(row)
                case[name] = {
                    "ok": ok,
                    "wer": _ratio(row.word_errors, row.words),
                    "seconds": round(seconds, 3),
                }
        finally:
            await _close(transcriber)
        summaries[name] = _stt_summary(rows, first)
    return {
        "kind": "stt",
        "created_at": datetime.now(UTC).isoformat(),
        "machine": platform.machine() or "desconhecida",
        "cpus": os.cpu_count(),
        "corpus_checksum": checksum,
        "clips": len(clips),
        "audio_seconds": round(sum(clip.audio.seconds for clip in clips), 3),
        "candidates": summaries,
        "cases": cases,
    }


async def _close(adapter: object) -> None:
    """Encerra o processo vivo (Whisper, Piper); o efeito guarda o Piper em `base`."""
    close = getattr(getattr(adapter, "base", adapter), "close", None)
    if close is not None:
        await close()


def _stt_summary(rows: Sequence[_Row], first: float) -> dict[str, Any]:
    done = [row for row in rows if row.ok]
    latencies = [row.seconds for row in done]
    cpus = [row.cpu for row in done]
    return {
        "wer": _ratio(sum(r.word_errors for r in rows), sum(r.words for r in rows)),
        "cer": _ratio(sum(r.char_errors for r in rows), sum(r.chars for r in rows)),
        "mean_seconds": _mean(latencies),
        "max_seconds": round(max(latencies), 3) if latencies else None,
        "real_time_factor": _ratio(
            sum(latencies), sum(row.audio_seconds for row in done)
        ),
        "cpu_seconds": _mean([c for c in cpus if c is not None])
        if None not in cpus
        else None,
        "first_call_seconds": round(first, 3),
        "failures": len(rows) - len(done),
    }


def _ratio(part: float, whole: float) -> float | None:
    return round(part / whole, 4) if whole else None


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


async def make_samples(
    output: Path,
    candidates: Mapping[str, Callable[[], Synthesizer]],
    *,
    rng: random.Random | None = None,
) -> int:
    """Cada frase em cada candidato, com nomes sorteados; o gabarito fica à parte."""
    _new_folder_outside_git(output)
    audio: dict[tuple[str, int], bytes] = {}
    for name, build in candidates.items():
        synthesizer = build()
        try:
            for index, phrase in enumerate(TTS_PHRASES):
                audio[name, index] = await synthesizer.synthesize(for_speech(phrase))
        finally:
            await _close(synthesizer)
    order = list(audio)
    (rng or random.SystemRandom()).shuffle(order)
    output.mkdir(parents=True, mode=0o700)
    key = {}
    for number, (name, index) in enumerate(order, 1):
        label = f"amostra-{number:02d}"
        (output / f"{label}.wav").write_bytes(audio[name, index])
        key[label] = {"candidate": name, "phrase": index + 1}
    (output / KEY_FILE).write_text(json.dumps(key, indent=2), encoding="utf-8")
    (output / SCORES_FILE).write_text(
        "amostra,nota\n" + "".join(f"{label},\n" for label in key), encoding="utf-8"
    )
    return len(key)


def _new_folder_outside_git(output: Path) -> None:
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("Escolha uma pasta nova para as amostras.")
    if any((parent / ".git").exists() for parent in output.parents):
        raise ValueError("Escolha uma pasta fora de qualquer checkout Git.")


def score_samples(folder: Path) -> dict[str, Any]:
    """Junta as notas de 1 a 5 do `notas.csv` com o gabarito sorteado."""
    key = json.loads((folder / KEY_FILE).read_text(encoding="utf-8"))
    notes: dict[str, int] = {}
    # utf-8-sig e `;`: planilhas em português salvam CSV assim.
    lines = (folder / SCORES_FILE).read_text(encoding="utf-8-sig").splitlines()
    for line in lines[1:]:
        fields = [value.strip() for value in re.split(r"[,;\t]", line)]
        label, note = fields[0], fields[1] if len(fields) > 1 else ""
        if not label:
            continue
        if label not in key:
            raise ValueError(f"{SCORES_FILE}: amostra desconhecida {label!r}.")
        if note not in {"1", "2", "3", "4", "5"}:
            raise ValueError(f"{SCORES_FILE}: {label} precisa de nota de 1 a 5.")
        notes[label] = int(note)
    missing = sorted(set(key) - set(notes))
    if missing:
        raise ValueError(f"{SCORES_FILE}: sem nota em {', '.join(missing)}.")
    by_candidate: dict[str, list[int]] = {}
    for label, entry in key.items():
        by_candidate.setdefault(entry["candidate"], []).append(notes[label])
    return {
        "kind": "tts",
        "created_at": datetime.now(UTC).isoformat(),
        "samples": len(key),
        "phrases": len({entry["phrase"] for entry in key.values()}),
        "candidates": {
            name: {
                "mean": _mean(values),
                "min": min(values),
                "max": max(values),
                "count": len(values),
            }
            for name, values in sorted(by_candidate.items())
        },
    }


def render_panel(report: Mapping[str, Any]) -> str:
    """Painel só com agregados; nada de texto esperado, transcrito ou falado."""
    created = report["created_at"]
    if report["kind"] == "tts":
        head = frontmatter(
            {
                "tipo": "neoson-dashboard",
                "painel": "Voz-Sintese",
                "avaliado_em": created,
                "amostras": report["samples"],
            }
        )
        rows = "\n".join(
            f"| {name} | {c['mean']:.2f} | {c['min']} | {c['max']} | {c['count']} |"
            for name, c in report["candidates"].items()
        )
        return (
            f"{head}\n# Voz — síntese às cegas\n\n"
            f"{report['samples']} amostras ({report['phrases']} frases em cada "
            "candidato), ouvidas sem saber o fornecedor e notas de 1 a 5.\n\n"
            "| Candidato | Nota média | Mín. | Máx. | Amostras |\n"
            f"|---|---|---|---|---|\n{rows}\n"
        )
    head = frontmatter(
        {
            "tipo": "neoson-dashboard",
            "painel": "Voz-Transcricao",
            "avaliado_em": created,
            "corpus": report["corpus_checksum"][:12],
            "maquina": report["machine"],
            "clipes": report["clips"],
        }
    )
    rows = "\n".join(
        f"| {name} | {_percent(c['wer'])} | {_percent(c['cer'])} "
        f"| {_cell(c['mean_seconds'])} | {_cell(c['max_seconds'])} "
        f"| {_cell(c['real_time_factor'])} | {_cell(c['cpu_seconds'])} "
        f"| {_cell(c['first_call_seconds'])} | {c['failures']} |"
        for name, c in report["candidates"].items()
    )
    return (
        f"{head}\n# Voz — transcrição local e nuvem\n\n"
        f"Corpus `{report['corpus_checksum'][:12]}`: {report['clips']} clipes, "
        f"{report['audio_seconds']:.0f} s de áudio, máquina {report['machine']} "
        f"com {report['cpus']} CPUs. Mesmos clipes para todos; texto em minúsculas, "
        "sem pontuação e com números por extenso. Falha conta como clipe todo "
        "errado.\n\n"
        "| Candidato | WER | CER | Latência média (s) | Latência máx. (s) "
        "| Tempo real (RTF) | CPU por clipe (s) | 1ª chamada (s) | Falhas |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        f"{rows}\n\n"
        "RTF = tempo de transcrição ÷ duração do áudio (abaixo de 1 é mais rápido "
        "que a fala). A 1ª chamada inclui carregar o modelo e fica fora das médias.\n"
    )


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _cell(value: float | None) -> str:
    return "—" if value is None else f"{value:.3g}"


def save_report(report: Mapping[str, Any], directory: Path) -> Path:
    stamp = re.sub(r"[^0-9]", "", str(report["created_at"]))[:14]
    path = directory / f"voice-{report['kind']}-{stamp}.json"
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def stt_candidate(settings: ServerSettings, spec: str) -> Callable[[], Transcriber]:
    """`whisper[:modelo ou pasta]` ou `openai[:modelo]`, sem reserva."""
    provider, _, model = spec.partition(":")
    update: dict[str, Any] = {"voice_stt_fallback_openai": False}
    if provider == "whisper":
        update["voice_stt_provider"] = "whisper"
        if model:
            update["whisper_model"] = model
    elif provider == "openai":
        update["voice_stt_provider"] = "openai"
        if model:
            update["voice_stt_model"] = model
    else:
        raise ValueError(f"Candidato de transcrição desconhecido: {spec!r}.")
    chosen = settings.model_copy(update=update)
    build_transcriber(chosen)  # configuração inválida falha antes de transcrever
    return lambda: build_transcriber(chosen)


def tts_candidate(settings: ServerSettings, name: str) -> Callable[[], Synthesizer]:
    chosen = settings.model_copy(update={"voice_tts_provider": name})
    build_synthesizer(chosen)
    return lambda: build_synthesizer(chosen)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neoson-voice-eval", description="Avaliação de transcrição e síntese."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    stt = commands.add_parser("stt", help="mede a transcrição no corpus gravado")
    stt.add_argument("--corpus", type=Path, required=True, help="pasta da gravação")
    stt.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="whisper[:modelo ou pasta] ou openai[:modelo]; repita para comparar",
    )
    stt.add_argument("--timeout", type=float, help="prazo por clipe, até 120 s")
    stt.add_argument("--output", type=Path, default=REPORTS)
    samples = commands.add_parser("tts-samples", help="gera amostras cegas de síntese")
    samples.add_argument("--output", type=Path, required=True, help="pasta nova")
    samples.add_argument("--candidate", action="append", choices=TTS_CANDIDATES)
    score = commands.add_parser("tts-score", help="lê as notas das amostras cegas")
    score.add_argument("--samples", type=Path, required=True)
    score.add_argument("--output", type=Path, default=REPORTS)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        if args.command == "tts-samples":
            names = args.candidate or list(TTS_CANDIDATES)
            builders = {name: tts_candidate(settings, name) for name in names}
            count = asyncio.run(make_samples(args.output, builders))
            print(
                f"{count} amostras em {args.output}. Ouça cada uma, preencha "
                f"{SCORES_FILE} com notas de 1 a 5 e rode tts-score. "
                f"Não abra {KEY_FILE} antes."
            )
            return 0
        if args.command == "tts-score":
            report = score_samples(args.samples)
        else:
            if args.timeout is not None:
                if not 0 < args.timeout <= 120:
                    raise ValueError("--timeout deve estar entre 0 e 120 s.")
                settings = settings.model_copy(
                    update={"voice_timeout_seconds": args.timeout}
                )
            clips, checksum = load_corpus(args.corpus)
            stt_builders = {
                spec: stt_candidate(settings, spec) for spec in args.candidate
            }
            report = asyncio.run(evaluate_stt(clips, checksum, stt_builders))
    except (ConfigError, ValueError, OSError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    path = save_report(report, args.output)
    write_atomic(
        settings.vault_path / (TTS_PANEL if report["kind"] == "tts" else STT_PANEL),
        render_panel(report),
    )
    for name, summary in report["candidates"].items():
        print(f"{name}: {json.dumps(summary, ensure_ascii=False)}")
    print(f"relatório: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
