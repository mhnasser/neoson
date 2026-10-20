from uuid import UUID

import httpx2

from neoson_contracts.api.v1.errors import ErrorResponse
from neoson_contracts.api.v1.messages import MessageRequest, MessageResponse

MESSAGES_PATH = "/api/v1/messages"


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

    def __init__(self, http: httpx2.Client) -> None:
        self._http = http

    def send(self, text: str, session_id: UUID | None = None) -> MessageResponse:
        request = MessageRequest(text=text, session_id=session_id)
        try:
            response = self._http.post(
                MESSAGES_PATH, json=request.model_dump(mode="json")
            )
        except httpx2.HTTPError as error:
            raise GatewayError(
                f"Não foi possível falar com o gateway ({type(error).__name__})."
            ) from error

        if response.is_success:
            return MessageResponse.model_validate(response.json())
        raise _error_from(response)


def _error_from(response: httpx2.Response) -> GatewayError:
    try:
        error = ErrorResponse.model_validate(response.json())
    except ValueError:  # corpo não é JSON ou não segue o ErrorResponse
        return GatewayError(
            f"O gateway respondeu {response.status_code} fora do formato esperado.",
            status_code=response.status_code,
        )
    return GatewayError(
        error.message, status_code=response.status_code, code=error.code
    )
