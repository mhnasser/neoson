from collections.abc import Callable
from typing import TypeVar
from uuid import UUID

import httpx2
from pydantic import TypeAdapter

from neoson_contracts.api.v1.actions import ActionDecision, ActionResponse
from neoson_contracts.api.v1.errors import ErrorResponse
from neoson_contracts.api.v1.messages import MessageRequest, MessageResponse

MESSAGES_PATH = "/api/v1/messages"
ACTIONS_PATH = "/api/v1/actions"
_ACTIONS = TypeAdapter(list[ActionResponse])
T = TypeVar("T")


class GatewayError(Exception):
    """O gateway não respondeu ou respondeu com erro."""

    def __init__(
        self, message: str, status_code: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class GatewayClient:
    """Envia mensagens ao gateway pela API v1 (contratos de `neoson-contracts`)."""

    def __init__(self, http: httpx2.Client, token: str | None = None) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def send(self, text: str, session_id: UUID | None = None) -> MessageResponse:
        request = MessageRequest(text=text, session_id=session_id)
        try:
            response = self._http.post(
                MESSAGES_PATH,
                json=request.model_dump(mode="json"),
                headers=self._headers,
            )
        except httpx2.HTTPError as error:
            raise GatewayError(
                f"Não foi possível falar com o gateway ({type(error).__name__})."
            ) from error

        if response.is_success:
            try:
                return MessageResponse.model_validate(response.json())
            except ValueError:  # corpo não é JSON ou não segue o MessageResponse
                raise _unexpected(response) from None
        raise _error_from(response)


class ApprovalClient:
    """Revisão de ações pela mesma API autenticada do canal HTTP."""

    def __init__(self, http: httpx2.Client, token: str | None = None) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def for_run(self, run_id: UUID) -> list[ActionResponse]:
        """Só as ações do pedido indicado; o servidor filtra por execução e dono."""
        path = f"/api/v1/runs/{run_id}/actions"
        return self._call("GET", path, _ACTIONS.validate_python)

    def decide(self, action_id: UUID, approve: bool) -> ActionResponse:
        body = ActionDecision(approve=approve).model_dump()
        path = f"{ACTIONS_PATH}/{action_id}/decision"
        return self._call("POST", path, ActionResponse.model_validate, body)

    def resume(self, action_id: UUID) -> ActionResponse:
        path = f"{ACTIONS_PATH}/{action_id}/resume"
        return self._call("POST", path, ActionResponse.model_validate)

    def _call(
        self, method: str, path: str, parse: Callable[[object], T], body: object = None
    ) -> T:
        try:
            response = self._http.request(
                method, path, json=body, headers=self._headers, timeout=15
            )
        except httpx2.HTTPError:
            raise GatewayError(
                "Não foi possível falar com o gateway.", code="offline"
            ) from None
        if not response.is_success:
            raise _error_from(response)
        try:
            return parse(response.json())
        except ValueError:  # corpo não é JSON ou não segue o ActionResponse
            raise _unexpected(response) from None


def _error_from(response: httpx2.Response) -> GatewayError:
    try:
        error = ErrorResponse.model_validate(response.json())
    except ValueError:  # corpo não é JSON ou não segue o ErrorResponse
        return _unexpected(response)
    return GatewayError(
        error.message, status_code=response.status_code, code=error.code
    )


def _unexpected(response: httpx2.Response) -> GatewayError:
    """Resposta fora do contrato. O corpo recebido não entra na mensagem."""
    return GatewayError(
        f"O gateway respondeu {response.status_code} fora do formato esperado.",
        status_code=response.status_code,
    )
