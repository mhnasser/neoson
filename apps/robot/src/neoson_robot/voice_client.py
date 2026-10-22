"""Cliente de eventos limitado; não importa o motor nem salva áudio."""

import base64
import binascii
import io
import wave
from collections.abc import Iterator
from time import monotonic
from uuid import UUID

import httpx2

from neoson_contracts.api.v1.voice import VoiceResponse
from neoson_contracts.events.v1.robot import (
    VOICE_EVENT,
    VoiceCompleted,
    VoiceEvent,
    VoiceFailed,
)
from neoson_robot.client import GatewayError, _error_from

MAX_AUDIO_BYTES = 2097152
MAX_FRAME_BYTES = 3 * 1024 * 1024


def check_audio(data: bytes) -> None:
    if not isinstance(data, bytes) or not 44 <= len(data) <= MAX_AUDIO_BYTES:
        raise GatewayError("Áudio fora dos limites.")
    if (
        data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or int.from_bytes(data[4:8], "little") + 8 != len(data)
    ):
        raise GatewayError("Áudio inválido.")
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            frames = source.getnframes()
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != 16000
                or source.getcomptype() != "NONE"
                or not 0 < frames <= 480000
                or len(source.readframes(frames)) != frames * 2
            ):
                raise GatewayError("Formato de áudio inválido.")
    except (wave.Error, EOFError, ValueError):
        raise GatewayError("Áudio inválido.") from None


def response_audio(result: VoiceResponse) -> bytes | None:
    if result.audio_status != "ready" or result.audio_base64 is None:
        return None
    if len(result.audio_base64) > 4 * ((MAX_AUDIO_BYTES + 2) // 3):
        raise GatewayError("Resposta de áudio acima do limite.")
    try:
        raw = base64.b64decode(result.audio_base64, validate=True)
    except (ValueError, binascii.Error):
        raise GatewayError("Resposta de áudio inválida.") from None
    check_audio(raw)
    return raw


class HttpVoiceGateway:
    def __init__(
        self, http: httpx2.Client, api_token: str | None, timeout_seconds: float = 60
    ) -> None:
        self.http, self.token, self.timeout_seconds = http, api_token, timeout_seconds

    def events(
        self, audio: bytes, session_id: UUID | None, simulated_capture: bool
    ) -> Iterator[VoiceEvent]:
        check_audio(audio)
        headers = {"Content-Type": "audio/wav", "X-Neoson-Activation": "explicit"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if simulated_capture:
            headers["X-Neoson-Simulated"] = "true"
        params = {"session_id": str(session_id)} if session_id is not None else {}
        deadline = monotonic() + self.timeout_seconds
        try:
            with self.http.stream(
                "POST",
                "/api/v1/voice/stream",
                content=audio,
                headers=headers,
                params=params,
            ) as response:
                if not response.is_success:
                    # Limita também o corpo de erro antes de tentar interpretá-lo.
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if monotonic() > deadline:
                            raise GatewayError("Prazo da resposta excedido.")
                        if len(content) + len(chunk) > 16384:
                            raise GatewayError("Resposta de erro acima do limite.")
                        content.extend(chunk)
                    safe = httpx2.Response(response.status_code, content=bytes(content))
                    raise _error_from(safe)
                if (
                    response.headers.get("content-type", "").split(";", 1)[0]
                    != "application/x-ndjson"
                ):
                    raise GatewayError("Formato de eventos inválido.")
                pending = bytearray()
                total = frames = 0
                terminal = False
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if monotonic() > deadline or total > 4 * 1024 * 1024:
                        raise GatewayError("Limite da resposta de voz excedido.")
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        frames += 1
                        if len(line) > MAX_FRAME_BYTES or frames > 512 or terminal:
                            raise GatewayError("Stream de voz inválido.")
                        try:
                            event = VOICE_EVENT.validate_json(line)
                        except ValueError:
                            raise GatewayError("Evento de voz inválido.") from None
                        terminal = isinstance(event, (VoiceCompleted, VoiceFailed))
                        yield event
                    if len(pending) > MAX_FRAME_BYTES:
                        raise GatewayError("Evento de voz acima do limite.")
                if pending or not terminal:
                    raise GatewayError("Stream de voz interrompido.")
        except httpx2.HTTPError:
            raise GatewayError(
                "Não foi possível falar com o gateway.", code="offline"
            ) from None
