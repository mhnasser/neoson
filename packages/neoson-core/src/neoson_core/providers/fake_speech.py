"""Simulação explícita: não reconhece voz nem produz fala real."""

from neoson_core.engine.voice import AudioClip, pcm_wav


class FakeSpeech:
    async def transcribe(self, audio: AudioClip) -> str:
        return "Teste simulado de voz."

    async def synthesize(self, text: str) -> bytes:
        return pcm_wav(b"\x00" * 6400)
