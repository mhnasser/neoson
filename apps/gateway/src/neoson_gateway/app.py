import asyncio
import hmac
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException

from neoson_contracts.api.v1.errors import ErrorDetail, ErrorResponse
from neoson_contracts.api.v1.messages import (
    MessageRequest,
    MessageResponse,
    MessageStatus,
)
from neoson_contracts.api.v1.queries import ReadyResponse
from neoson_core.engine import Engine, RunStatus, SessionNotFound, UserMessage
from neoson_core.engine.actions import ActionConflict, ActionNotFound, ActionService
from neoson_core.engine.voice import VoicePipeline
from neoson_core.memory.queries import readiness
from neoson_gateway import __version__
from neoson_gateway.actions import HTTP_OWNER, actions_router
from neoson_gateway.queries import QueryConfig, queries_router
from neoson_gateway.voice import voice_router

health = APIRouter(prefix="/api/v1/health", tags=["health"])


@health.get("/live")
async def live() -> dict[str, str]:
    """Indica que o processo do gateway está ativo."""
    return {"status": "ok"}


_STATUS: dict[RunStatus, MessageStatus] = {
    RunStatus.COMPLETED: "completed",
    RunStatus.NEEDS_APPROVAL: "needs_approval",
    RunStatus.FAILED: "failed",
}


_bearer = HTTPBearer(auto_error=False, description="Token de API (NEOSON_API_TOKEN)")


def require_token(
    expected: str,
) -> Callable[[HTTPAuthorizationCredentials | None], Awaitable[None]]:
    """Dependência que exige `Authorization: Bearer <token>` (comparação constante)."""
    expected_bytes = expected.encode("utf-8")

    async def check(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        given = credentials.credentials.encode("utf-8") if credentials else b""
        if not hmac.compare_digest(given, expected_bytes):
            raise HTTPException(
                status_code=401,
                detail="Token de API ausente ou inválido.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return check


def messages_router(engine: Engine, api_token: str) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["messages"],
        dependencies=[Depends(require_token(api_token))],
    )

    @router.post(
        "/messages",
        response_model=MessageResponse,
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    async def receive_message(
        request: MessageRequest,
    ) -> MessageResponse | JSONResponse:
        """Recebe uma mensagem do usuário e devolve a resposta do NeoSon."""
        try:
            result = await engine.run(
                UserMessage(
                    text=request.text,
                    session_id=request.session_id,
                    principal=HTTP_OWNER,
                )
            )
        except SessionNotFound:
            return _error(
                404,
                ErrorResponse(
                    code="session_not_found", message="Sessão não encontrada."
                ),
            )
        return MessageResponse(
            run_id=result.run_id,
            text=result.text,
            status=_STATUS[result.status],
            session_id=result.session_id,
        )

    return router


def create_app(
    engine: Engine,
    api_token: str,
    actions: ActionService | None = None,
    voice: VoicePipeline | None = None,
    queries: QueryConfig | None = None,
) -> FastAPI:
    if not api_token:
        raise ValueError("O gateway exige um token de API.")
    app = FastAPI(title="NeoSon Gateway", version=__version__)
    app.include_router(health)
    app.include_router(messages_router(engine, api_token))
    if queries is not None:
        app.include_router(queries_router(queries, require_token(api_token)))

    @app.get(
        "/api/v1/health/ready",
        response_model=ReadyResponse,
        responses={503: {"model": ReadyResponse}},
        tags=["health"],
    )
    async def ready() -> JSONResponse:
        """Dependências mínimas: banco e migrações. Não chama o modelo."""
        checks = (
            await asyncio.to_thread(readiness, queries.db)
            if queries is not None
            else {}
        )
        ok = all(checks.values())
        body = ReadyResponse(status="ready" if ok else "not_ready", checks=checks)
        return JSONResponse(status_code=200 if ok else 503, content=body.model_dump())

    if voice is not None:
        app.include_router(voice_router(voice, require_token(api_token)))
    if actions is not None:
        app.include_router(actions_router(actions, require_token(api_token)))

    @app.exception_handler(ActionNotFound)
    async def action_not_found(request: Request, error: ActionNotFound) -> JSONResponse:
        return _error(
            404, ErrorResponse(code="action_not_found", message="Ação não encontrada.")
        )

    @app.exception_handler(ActionConflict)
    async def action_conflict(request: Request, error: ActionConflict) -> JSONResponse:
        return _error(
            409, ErrorResponse(code="action_conflict", message="Ação indisponível.")
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # Sem o campo `input` do FastAPI: o erro não devolve o conteúdo recebido.
        body = ErrorResponse(
            code="invalid_request",
            message="Requisição inválida.",
            details=[
                ErrorDetail(
                    location=".".join(str(part) for part in detail["loc"]),
                    message=detail["msg"],
                )
                for detail in error.errors()
            ],
        )
        return _error(422, body)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        body = ErrorResponse(code=_code(error.status_code), message=str(error.detail))
        return _error(error.status_code, body, error.headers)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        body = ErrorResponse(code="internal_error", message="Erro interno.")
        return _error(500, body)

    return app


def _code(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).name.lower()
    except ValueError:
        return "http_error"


def _error(
    status_code: int, body: ErrorResponse, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=body.model_dump(mode="json"), headers=headers
    )
