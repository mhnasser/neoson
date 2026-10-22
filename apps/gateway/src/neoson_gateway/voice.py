"""Áudio limitado, com resultado JSON ou eventos NDJSON sem conteúdo em progresso."""

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from neoson_contracts.api.v1.errors import ErrorResponse
from neoson_contracts.api.v1.messages import MessageStatus
from neoson_contracts.api.v1.voice import VoiceResponse
from neoson_contracts.events.v1.robot import (
    VOICE_EVENT,
    VoiceCompleted,
    VoiceFailed,
    VoiceProgress,
)
from neoson_core.engine import RunStatus
from neoson_core.engine.persistence import SessionNotFound
from neoson_core.engine.voice import (
    InvalidAudio,
    SpeechUnavailable,
    VoiceActivity,
    VoicePipeline,
    VoiceResult,
)
from neoson_gateway.actions import HTTP_OWNER


def _headers(request: Request, pipeline: VoicePipeline) -> None:
    if request.headers.get("x-neoson-activation") != "explicit":
        raise HTTPException(400, "Ativação explícita obrigatória.")
    if request.headers.get("x-neoson-simulated") == "true" and not pipeline.simulated:
        raise HTTPException(400, "Captura simulada exige servidor simulado.")
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


async def _process(
    pipeline: VoicePipeline,
    request: Request,
    session: UUID | None,
    activity: Callable[[VoiceActivity], None] | None = None,
) -> VoiceResult:
    async def load() -> bytes:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > pipeline.limits.max_bytes:
                raise HTTPException(413, "Áudio acima do limite.")
            data.extend(chunk)
        return bytes(data)

    try:
        return await pipeline.process(load, session, HTTP_OWNER, activity)
    except InvalidAudio:
        raise HTTPException(422, "Áudio inválido ou fora dos limites.") from None
    except SessionNotFound:
        raise HTTPException(404, "Sessão não encontrada.") from None
    except SpeechUnavailable as error:
        raise HTTPException(503, error.code) from None


def _response(value: VoiceResult, pipeline: VoicePipeline) -> VoiceResponse:
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


def voice_router(
    pipeline: VoicePipeline, auth: Callable[..., Awaitable[None]]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["voice"], dependencies=[Depends(auth)])

    @router.post("/voice", response_model=VoiceResponse)
    async def voice(
        request: Request, response: Response, session_id: UUID | None = None
    ) -> VoiceResponse:
        _headers(request, pipeline)
        value = await _process(pipeline, request, session_id)
        response.headers["Cache-Control"] = "no-store"
        return _response(value, pipeline)

    @router.post("/voice/stream")
    async def stream(
        request: Request, session_id: UUID | None = None
    ) -> StreamingResponse:
        _headers(request, pipeline)
        queue: asyncio.Queue[VoiceProgress | VoiceCompleted | HTTPException] = (
            asyncio.Queue(16)
        )

        def activity(state: VoiceActivity) -> None:
            if not queue.full():
                queue.put_nowait(VoiceProgress(state=state))

        async def run() -> None:
            try:
                value = await _process(pipeline, request, session_id, activity)
                await queue.put(VoiceCompleted(result=_response(value, pipeline)))
            except HTTPException as error:
                await queue.put(error)
            except Exception:
                await queue.put(HTTPException(500, "Falha no processamento de voz."))

        task = asyncio.create_task(run())
        try:
            # Valida/admite o corpo antes dos headers 200; erros iniciais mantêm HTTP.
            first = await queue.get()
            if isinstance(first, HTTPException):
                raise first
        except BaseException:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise

        async def body() -> AsyncIterator[bytes]:
            current: VoiceProgress | VoiceCompleted | HTTPException = first
            try:
                while True:
                    if isinstance(current, HTTPException):
                        failure = VoiceFailed(
                            status_code=current.status_code,
                            error=ErrorResponse(
                                code="voice_failed", message=str(current.detail)
                            ),
                        )
                        yield VOICE_EVENT.dump_json(failure) + b"\n"
                        break
                    yield VOICE_EVENT.dump_json(current) + b"\n"
                    if isinstance(current, VoiceCompleted):
                        break
                    current = await queue.get()
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        return StreamingResponse(
            body(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return router
