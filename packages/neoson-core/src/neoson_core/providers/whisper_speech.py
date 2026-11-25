"""Transcrição local com faster-whisper num processo vivo, em outro Python.

Como o Piper: o modelo carrega uma vez, o áudio vai só pelo stdin e o texto volta pelo
stdout, nunca em argumentos ou logs. O prazo mata o processo, o que interrompe a CPU de
verdade (uma thread continuaria transcrevendo) e devolve a memória do modelo.
"""

import asyncio
import json
import math
import re
import struct
from contextlib import suppress

from neoson_core.engine.voice import AudioClip

MAX_TRANSCRIPT_CHARS = 8000
# Roda no Python que tem `faster-whisper`: no stdin, 4 bytes com o tamanho e o WAV;
# no stdout, uma linha JSON com o texto e os segundos de CPU gastos na transcrição.
HELPER = """
import io, json, struct, sys, time, wave
import numpy
from faster_whisper import WhisperModel
out, sys.stdout = sys.stdout.buffer, sys.stderr  # print solto não corrompe o protocolo
model = WhisperModel(sys.argv[1], device="cpu", compute_type="int8")
source = sys.stdin.buffer
while header := source.read(4):
    (size,) = struct.unpack("<I", header)
    with wave.open(io.BytesIO(source.read(size))) as clip:
        pcm = clip.readframes(clip.getnframes())
    audio = numpy.frombuffer(pcm, numpy.int16).astype(numpy.float32) / 32768
    start = time.process_time()
    segments, _ = model.transcribe(audio, language=sys.argv[2], vad_filter=True)
    text = "".join(segment.text for segment in segments).strip()
    reply = {"text": text, "cpu": time.process_time() - start}
    out.write(json.dumps(reply, ensure_ascii=False).encode() + b"\\n")
    out.flush()
"""


class WhisperError(RuntimeError):
    pass


class WhisperSpeech:
    def __init__(
        self,
        model: str,
        *,
        python: str = "python3",
        language: str = "pt",
        timeout_seconds: float = 30,
    ) -> None:
        if (
            not isinstance(model, str)
            or not model.strip()
            or len(model) > 500
            or not isinstance(python, str)
            or not python
            or not re.fullmatch(r"[a-z]{2}", language)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120
        ):
            raise WhisperError("Whisper: modelo ou limites inválidos.")
        self.model, self.python, self.language = model, python, language
        self.timeout = timeout_seconds
        # CPU da última transcrição (para a avaliação); a de OpenAI não é medida.
        self.cpu_seconds: float | None = None
        # shortcut: sem pré-aquecimento, a 1ª transcrição paga a carga do modelo
        # (segundos na Pi); aquecer na subida do gateway se isso incomodar.
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def transcribe(self, audio: AudioClip) -> str:
        async with self._lock:
            try:
                text, cpu = await asyncio.wait_for(
                    self._request(audio.wav), self.timeout
                )
            except BaseException as error:
                # Pedido pela metade deixa o stdout no meio de uma resposta: recomeçar.
                await self.close()
                # TimeoutError herda de OSError: o prazo sobe como prazo.
                if not isinstance(error, TimeoutError) and isinstance(
                    error, (OSError, ValueError, EOFError)
                ):
                    raise WhisperError(
                        "Whisper: processo indisponível ou resposta inválida."
                    ) from None
                raise
            self.cpu_seconds = cpu
            return text

    async def _request(self, wav: bytes) -> tuple[str, float]:
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                self.python,
                "-c",
                HELPER,
                self.model,
                self.language,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(struct.pack("<I", len(wav)) + wav)
        await process.stdin.drain()
        # Linha vazia (processo morreu) ou JSON quebrado sobem como ValueError.
        reply = json.loads(await process.stdout.readline())
        if not isinstance(reply, dict):
            raise ValueError
        text, cpu = reply.get("text"), reply.get("cpu")
        if (
            not isinstance(text, str)
            or len(text) > MAX_TRANSCRIPT_CHARS
            or isinstance(cpu, bool)
            or not isinstance(cpu, (int, float))
            or not math.isfinite(cpu)
            or cpu < 0
        ):
            raise ValueError
        return text, float(cpu)

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(TimeoutError):
                await asyncio.wait_for(process.communicate(), 2)
