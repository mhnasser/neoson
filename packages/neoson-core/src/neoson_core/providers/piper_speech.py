"""Piper local via stdin/stdout; texto não entra em argumentos, arquivos ou logs."""

import asyncio
import json
import math
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from neoson_core.engine.voice import pcm_wav, speakable
from neoson_core.providers.speech_effects import resample_pcm


class PiperError(RuntimeError):
    pass


class PiperSpeech:
    def __init__(
        self,
        model: Path,
        *,
        command: Sequence[str] = ("piper",),
        timeout_seconds: float = 30,
        length_scale: float = 1,
        max_speech_chars: int = 400,
        max_output_seconds: float = 30,
    ) -> None:
        try:
            if (
                not model.is_file()
                or isinstance(command, str)
                or not command
                or not all(command)
            ):
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
        self.model, self.command, self.rate = model, tuple(command), rate
        self.timeout, self.scale, self.max_chars = (
            timeout_seconds,
            length_scale,
            max_speech_chars,
        )
        self.max_bytes = int(rate * max_output_seconds * length_scale) * 2

    async def synthesize(self, text: str) -> bytes:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                "--model",
                str(self.model),
                "--output-raw",
                "--length-scale",
                str(self.scale),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

            async def collect() -> bytes:
                assert (
                    process is not None
                    and process.stdin is not None
                    and process.stdout is not None
                )
                process.stdin.write(
                    (speakable(text, self.max_chars) + "\n").encode("utf-8")
                )
                await process.stdin.drain()
                process.stdin.close()
                raw = bytearray()
                while chunk := await process.stdout.read(8192):
                    if len(raw) + len(chunk) > self.max_bytes:
                        raise PiperError("Piper: áudio acima do limite.")
                    raw.extend(chunk)
                if await process.wait() != 0 or not raw or len(raw) % 2:
                    raise PiperError("Piper: síntese falhou.")
                return bytes(raw)

            raw = await asyncio.wait_for(collect(), self.timeout)
            return pcm_wav(await asyncio.to_thread(resample_pcm, raw, self.rate))
        except TimeoutError:
            raise
        except (OSError, ValueError):
            raise PiperError(
                "Piper: processo indisponível ou áudio inválido."
            ) from None
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.communicate(), 2)
