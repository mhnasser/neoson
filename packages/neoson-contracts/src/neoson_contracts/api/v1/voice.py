"""WAV binário na entrada; resposta JSON preserva resultado quando TTS falha."""

from typing import Literal

from neoson_contracts.api.v1.messages import MessageResponse


class VoiceResponse(MessageResponse):
    audio_base64: str | None = None
    audio_format: Literal["wav"] = "wav"
    sample_rate_hz: Literal[16000] = 16000
    audio_seconds: float | None = None
    audio_status: Literal[
        "ready", "synthesis_timeout", "synthesis_failed", "text_too_long"
    ] = "ready"
    simulated: bool = False
