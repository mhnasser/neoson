"""Fala pela API de áudio da OpenAI: transcrição e síntese, só em memória.

Nada de áudio ou texto vai para logs. A síntese pede PCM cru (24 kHz, 16 bits,
mono) e converte para o WAV de 16 kHz que o pipeline e o Robot aceitam.
"""

import sys
from array import array
from typing import Any

import openai
from openai import AsyncOpenAI

from neoson_core.engine.voice import SAMPLE_RATE, AudioClip, pcm_wav

TTS_SAMPLE_RATE = 24000
# 30 s de PCM16 a 24 kHz: o Robot não reproduz mais que isso.
MAX_TTS_BYTES = 30 * TTS_SAMPLE_RATE * 2
MAX_TRANSCRIPT_CHARS = 8000
_ENDINGS = (". ", "! ", "? ")


class SpeechError(RuntimeError):
    """Falha do fornecedor de fala; a mensagem traz só o tipo do erro."""


class OpenAISpeech:
    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        stt_model: str,
        tts_model: str,
        voice: str,
        language: str = "pt",
        max_speech_chars: int = 400,
    ) -> None:
        if not 1 <= max_speech_chars <= 4000:
            raise ValueError("max_speech_chars deve estar entre 1 e 4000.")
        self._client = client
        self._stt, self._tts, self._voice = stt_model, tts_model, voice
        self._language, self._max_speech_chars = language, max_speech_chars

    @classmethod
    def from_api_key(
        cls, api_key: str, *, timeout_seconds: float, **options: Any
    ) -> "OpenAISpeech":
        """Sem retentativa no SDK: o pipeline já tem prazo e não repete áudio."""
        client = AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        return cls(client, **options)

    async def transcribe(self, audio: AudioClip) -> str:
        try:
            result = await self._client.audio.transcriptions.create(
                file=("fala.wav", audio.wav, "audio/wav"),
                model=self._stt,
                language=self._language,
            )
        except openai.APIError as error:
            raise SpeechError(f"OpenAI: {type(error).__name__}.") from None
        text = getattr(result, "text", None)
        if not isinstance(text, str) or len(text) > MAX_TRANSCRIPT_CHARS:
            raise SpeechError("OpenAI: transcrição inválida.")
        return text.strip()

    async def synthesize(self, text: str) -> bytes:
        try:
            response = await self._client.audio.speech.create(
                model=self._tts,
                voice=self._voice,
                input=speakable(text, self._max_speech_chars),
                response_format="pcm",
            )
            raw = response.content
        except openai.APIError as error:
            raise SpeechError(f"OpenAI: {type(error).__name__}.") from None
        if not isinstance(raw, bytes) or not raw or len(raw) % 2:
            raise SpeechError("OpenAI: áudio sintetizado inválido.")
        if len(raw) > MAX_TTS_BYTES:
            raise SpeechError("OpenAI: áudio sintetizado longo demais.")
        return pcm_wav(resample_24k_to_16k(raw))


def speakable(text: str, limit: int) -> str:
    """Texto que cabe na fala: corta no último fim de frase antes do limite."""
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    head = clean[:limit]
    cut = max(head.rfind(ending) for ending in _ENDINGS)
    if head[-1] in ".!?" and clean[limit] == " ":
        cut = limit - 1
    if cut >= limit // 3:
        return head[: cut + 1]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip(",;:") + "…"


def resample_24k_to_16k(pcm: bytes) -> bytes:
    """PCM16 mono de 24 kHz para 16 kHz (razão 3:2, interpolação linear)."""
    if SAMPLE_RATE * 3 != TTS_SAMPLE_RATE * 2:
        raise AssertionError("Taxas de amostragem inesperadas.")
    source = array("h")
    source.frombytes(pcm)
    if source.itemsize != 2:
        raise AssertionError("PCM16 exige amostras de 2 bytes.")
    big_endian = sys.byteorder == "big"
    if big_endian:  # o PCM e o WAV são little-endian
        source.byteswap()
    output = array("h")
    # A cada 3 amostras de entrada saem 2: a primeira coincide, a segunda fica
    # no meio entre as duas seguintes.
    for start in range(0, len(source) - 2, 3):
        output.append(source[start])
        output.append((source[start + 1] + source[start + 2]) // 2)
    rest = len(source) % 3
    if rest:
        output.append(source[len(source) - rest])
    if big_endian:
        output.byteswap()
    return output.tobytes()
