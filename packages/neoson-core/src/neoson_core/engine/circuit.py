"""Circuit breaker do modelo (§18): para de insistir num provedor que está falhando.

Depois de `failures` falhas temporárias seguidas (erro `retryable` ou timeout), o
circuito abre e as chamadas são recusadas na hora, sem chegar ao fornecedor, até
`reset_seconds`. Então uma única chamada de teste passa (meia-abertura): sucesso
fecha, nova falha reabre. Erros definitivos (pedido inválido, por exemplo) não contam
e zeram a sequência, porque o fornecedor respondeu.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from enum import StrEnum

from neoson_core.engine.llm import LLMError, LLMProvider, LLMRequest, LLMResponse, Usage

logger = logging.getLogger(__name__)
OPEN_MESSAGE = "Provedor do modelo temporariamente indisponível."


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerProvider:
    def __init__(
        self,
        inner: LLMProvider,
        *,
        failures: int = 5,
        reset_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(failures) is not int or failures < 1:
            raise ValueError("failures deve ser um inteiro >= 1.")
        if not reset_seconds > 0:
            raise ValueError("reset_seconds deve ser > 0.")
        self._inner, self._limit, self._reset = inner, failures, reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._probing or self._clock() - self._opened_at >= self._reset:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    async def complete(self, request: LLMRequest) -> LLMResponse:
        # Sem lock: entre os `await` o loop não troca de tarefa, então cada bloco de
        # mudança de estado é atômico.
        state = self.state
        if state is CircuitState.OPEN or (
            state is CircuitState.HALF_OPEN and self._probing
        ):
            # Nada foi enviado: uso zero conhecido, a contabilidade fica completa.
            raise LLMError(OPEN_MESSAGE, retryable=True, usage=Usage())
        if state is CircuitState.HALF_OPEN:
            self._probing = True
        try:
            response = await self._inner.complete(request)
        except LLMError as error:
            self._failed(counts=error.retryable)
            raise
        except TimeoutError:
            self._failed(counts=True)
            raise
        except asyncio.CancelledError:
            self._probing = False
            raise
        if self._opened_at is not None:
            logger.info("circuito do modelo fechado")
        self._failures, self._opened_at, self._probing = 0, None, False
        return response

    def _failed(self, *, counts: bool) -> None:
        probing, self._probing = self._probing, False
        if not counts:
            # Erro definitivo: o fornecedor respondeu, a sequência recomeça.
            self._failures = 0
            if probing:
                self._opened_at = None
            return
        self._failures += 1
        if probing or self._failures >= self._limit:
            if self._opened_at is None or probing:
                logger.warning(
                    "circuito do modelo aberto após %d falha(s)", self._failures
                )
            self._opened_at = self._clock()
