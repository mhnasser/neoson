from collections.abc import Mapping
from http import HTTPStatus

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from neoson_contracts.api.v1.errors import ErrorDetail, ErrorResponse
from neoson_contracts.api.v1.messages import (
    MessageRequest,
    MessageResponse,
    MessageStatus,
)
from neoson_core.engine import Engine, RunStatus, UserMessage
from neoson_gateway import __version__

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


def messages_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["messages"])

    @router.post(
        "/messages",
        response_model=MessageResponse,
        responses={422: {"model": ErrorResponse}},
    )
    async def receive_message(request: MessageRequest) -> MessageResponse:
        """Recebe uma mensagem do usuário e devolve a resposta do NeoSon."""
        result = await engine.run(
            UserMessage(text=request.text, session_id=request.session_id)
        )
        return MessageResponse(
            run_id=result.run_id, text=result.text, status=_STATUS[result.status]
        )

    return router


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="NeoSon Gateway", version=__version__)
    app.include_router(health)
    app.include_router(messages_router(engine))

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
