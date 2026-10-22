"""Entrada de áudio autenticada e limitada antes de carregar o corpo completo."""

import base64
from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from neoson_contracts.api.v1.messages import MessageStatus
from neoson_contracts.api.v1.voice import VoiceResponse
from neoson_core.engine import RunStatus
from neoson_core.engine.persistence import SessionNotFound
from neoson_core.engine.voice import InvalidAudio, SpeechUnavailable, VoicePipeline
from neoson_gateway.actions import HTTP_OWNER


def voice_router(
    pipeline: VoicePipeline, auth: Callable[..., Awaitable[None]]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["voice"], dependencies=[Depends(auth)])

    @router.post("/voice", response_model=VoiceResponse)
    async def voice(
        request: Request, response: Response, session_id: UUID | None = None
    ) -> VoiceResponse:
        if request.headers.get("x-neoson-activation") != "explicit":
            raise HTTPException(400, "Ativação explícita obrigatória.")
        if request.headers.get("content-type", "").lower() != "audio/wav":
            raise HTTPException(415, "Use Content-Type: audio/wav.")
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(415, "Áudio comprimido no transporte não é aceito.")
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise HTTPException(400, "Tamanho inválido.")
            if len(length) > 10 or int(length) > pipeline.limits.max_bytes:
                raise HTTPException(413, "Áudio acima do limite.")

        async def load() -> bytes:
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > pipeline.limits.max_bytes:
                    raise HTTPException(413, "Áudio acima do limite.")
                data.extend(chunk)
            return bytes(data)

        try:
            value = await pipeline.process(load, session_id, HTTP_OWNER)
        except InvalidAudio:
            raise HTTPException(422, "Áudio inválido ou fora dos limites.") from None
        except SessionNotFound:
            raise HTTPException(404, "Sessão não encontrada.") from None
        except SpeechUnavailable as error:
            # Código fixo, nunca a mensagem do adaptador ou o áudio/transcrição.
            raise HTTPException(503, error.code) from None
        response.headers["Cache-Control"] = "no-store"
        result = value.result
        statuses: dict[RunStatus, MessageStatus] = {
            RunStatus.COMPLETED: "completed",
            RunStatus.NEEDS_APPROVAL: "needs_approval",
            RunStatus.FAILED: "failed",
        }
        return VoiceResponse(
            run_id=result.run_id,
            session_id=result.session_id,
            text=result.text,
            status=statuses[result.status],
            audio_base64=None
            if value.audio is None
            else base64.b64encode(value.audio.wav).decode("ascii"),
            audio_seconds=None if value.audio is None else value.audio.seconds,
            audio_status=value.audio_status,
            simulated=pipeline.simulated,
        )

    return router
