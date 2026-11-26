"""Piper local num processo vivo; texto só pelo stdin, nunca em argumentos ou logs.

O modelo carrega uma vez e cada frase sai em décimos de segundo, em vez de abrir um
processo e recarregar o modelo a cada síntese.
"""

import asyncio
import json
import math
import struct
from contextlib import suppress
from pathlib import Path

from neoson_core.engine.voice import pcm_wav, speakable
from neoson_core.providers.speech_effects import resample_pcm

# Roda no Python que tem `piper-tts`: uma linha JSON por frase no stdin; no stdout,
# 4 bytes com o tamanho e depois o PCM16 na taxa do modelo.
HELPER = """
import json, struct, sys
from piper import PiperVoice, SynthesisConfig
voice = PiperVoice.load(sys.argv[1])
config = SynthesisConfig(length_scale=float(sys.argv[2]))
out = sys.stdout.buffer
for line in sys.stdin.buffer:
    text = json.loads(line)["text"]
    chunks = voice.synthesize(text, syn_config=config)
    pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
    out.write(struct.pack("<I", len(pcm)) + pcm)
    out.flush()
"""


class PiperError(RuntimeError):
    pass


class PiperSpeech:
    def __init__(
        self,
        model: Path,
        *,
        python: str = "python3",
        timeout_seconds: float = 30,
        length_scale: float = 1,
        max_speech_chars: int = 400,
        max_output_seconds: float = 30,
    ) -> None:
        try:
            if not model.is_file() or not isinstance(python, str) or not python:
                raise ValueError
            with Path(str(model) + ".json").open("rb") as file:
                raw = file.read(1048577)
            if len(raw) > 1048576:
                raise ValueError
            rate = json.loads(raw)["audio"]["sample_rate"]
            if type(rate) is not int or not 8000 <= rate <= 48000:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise PiperError("Piper: modelo ou configuração inválidos.") from None
        if not (
            math.isfinite(length_scale)
            and 1 <= length_scale <= 2
            and math.isfinite(timeout_seconds)
            and 0 < timeout_seconds <= 120
            and math.isfinite(max_output_seconds)
            and 0 < max_output_seconds <= 30
            and 1 <= max_speech_chars <= 8000
        ):
            raise PiperError("Piper: limites inválidos.")
        self.model, self.python, self.rate = model, python, rate
        self.timeout, self.scale, self.max_chars = (
            timeout_seconds,
            length_scale,
            max_speech_chars,
        )
        self.max_bytes = int(rate * max_output_seconds * length_scale) * 2
        # O gateway aquece na subida (`warm_speech`). shortcut: depois de falha ou
        # cancelamento, o pedido seguinte recarrega o modelo (~1 s); recriar o processo
        # em segundo plano se isso pesar.
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def synthesize(self, text: str) -> bytes:
        async with self._lock:
            try:
                raw = await asyncio.wait_for(
                    self._request(speakable(text, self.max_chars)), self.timeout
                )
            except BaseException as error:
                # Pedido pela metade deixa o stdout no meio de um quadro: recomeçar.
                await self.close()
                # TimeoutError herda de OSError: o prazo sobe como prazo.
                if not isinstance(error, TimeoutError) and isinstance(
                    error, (OSError, ValueError, EOFError)
                ):
                    raise PiperError(
                        "Piper: processo indisponível ou áudio inválido."
                    ) from None
                raise
        return pcm_wav(await asyncio.to_thread(resample_pcm, raw, self.rate))

    async def _request(self, text: str) -> bytes:
        if self._process is None or self._process.returncode is not None:
            self._process = await asyncio.create_subprocess_exec(
                self.python,
                "-c",
                HELPER,
                str(self.model),
                str(self.scale),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write((json.dumps({"text": text}) + "\n").encode("utf-8"))
        await process.stdin.drain()
        (size,) = struct.unpack("<I", await process.stdout.readexactly(4))
        if not 0 < size <= self.max_bytes or size % 2:
            raise PiperError("Piper: áudio vazio, inválido ou acima do limite.")
        return await process.stdout.readexactly(size)

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(TimeoutError):
                await asyncio.wait_for(process.communicate(), 2)
