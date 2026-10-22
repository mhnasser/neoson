import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from neoson_core.engine import LLMError, LLMRequest, LLMResponse, Message, Role, Usage
from neoson_core.engine.circuit import (
    OPEN_MESSAGE,
    CircuitBreakerProvider,
    CircuitState,
)
from neoson_core.infrastructure.config import load_settings
from neoson_core.infrastructure.logs import QUIET, configure_logging, redact
from neoson_core.providers.fake import reply
from neoson_gateway.composition import build_engine

SECRET = "sk-" + "a" * 40
BOT = "123456789:" + "B" * 35
REQUEST = LLMRequest("balanced", (Message(Role.USER, "oi"),))


@pytest.mark.parametrize(
    "text",
    [
        f"chave {SECRET} no texto",
        "Authorization: Bearer abcdefghijklmnop",
        f"https://api.telegram.org/bot{BOT}/getUpdates",
        "url?token=valor-secreto&x=1",
        "senha=hunter22",
    ],
)
def test_known_secrets_are_masked(text: str) -> None:
    masked = redact(text)
    assert "[redigido]" in masked
    for secret in (SECRET, "abcdefghijklmnop", BOT, "valor-secreto", "hunter22"):
        assert secret not in masked


def test_json_lines_with_run_id_traceback_and_rotation(
    tmp_path: Path, capsys: Any
) -> None:
    log = tmp_path / "logs" / "neoson.log"
    configure_logging("INFO", "gateway", fmt="json", file=log, max_bytes=10_000)
    run_id = uuid4()
    logger = logging.getLogger("neoson.teste")
    logger.info("run %s concluído com %s", run_id, SECRET, extra={"run_id": run_id})
    try:
        raise RuntimeError(f"falha com {SECRET}")
    except RuntimeError:
        logger.exception("erro")
    for handler in logging.getLogger().handlers:
        handler.flush()
    lines = log.read_text(encoding="utf-8").splitlines()
    first, second = (json.loads(line) for line in lines)
    assert first["process"] == "gateway" and first["level"] == "INFO"
    assert first["run_id"] == str(run_id) and first["time"].endswith("+00:00")
    assert SECRET not in json.dumps(first) and "[redigido]" in first["message"]
    assert second["error"] == "RuntimeError" and SECRET not in second["traceback"]
    assert all(logging.getLogger(n).level >= logging.WARNING for n in QUIET)
    # Rotação: passa do limite e vira arquivo .1.
    for _ in range(200):
        logger.info("x" * 100)
    assert (log.parent / "neoson.log.1").exists()


def test_text_format_keeps_the_previous_layout(capsys: Any) -> None:
    configure_logging("WARNING", "worker")
    logging.getLogger("neoson.teste").warning("aviso com %s", SECRET)
    err = capsys.readouterr().err.strip()
    assert err.endswith("WARNING neoson.teste aviso com [redigido]")
    configure_logging("WARNING", "worker")  # reconfigurar não duplica handlers
    assert len(logging.getLogger().handlers) == 1


class Provider:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[no-any-return]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def call(breaker: CircuitBreakerProvider) -> Any:
    try:
        return asyncio.run(breaker.complete(REQUEST))
    except (LLMError, TimeoutError) as error:
        return error


def state(breaker: CircuitBreakerProvider) -> CircuitState:
    # Função à parte: a propriedade muda entre leituras, e o mypy não sabe disso.
    return breaker.state


def temporary() -> LLMError:
    return LLMError("instável", retryable=True)


def test_circuit_opens_rejects_then_half_opens_and_closes() -> None:
    clock = Clock()
    inner = Provider(temporary(), TimeoutError(), temporary(), reply("ok"))
    breaker = CircuitBreakerProvider(inner, failures=3, reset_seconds=30, clock=clock)
    for _ in range(3):
        call(breaker)
    assert state(breaker) is CircuitState.OPEN and inner.calls == 3
    refused = call(breaker)
    assert isinstance(refused, LLMError) and str(refused) == OPEN_MESSAGE
    assert refused.retryable and refused.usage == Usage() and inner.calls == 3
    clock.now = 30
    assert state(breaker) is CircuitState.HALF_OPEN
    assert call(breaker).message.content == "ok" and inner.calls == 4
    assert state(breaker) is CircuitState.CLOSED


def test_failed_probe_reopens_and_definitive_errors_do_not_count() -> None:
    clock = Clock()
    inner = Provider(
        temporary(),
        LLMError("pedido inválido", retryable=False),
        temporary(),
        temporary(),
        temporary(),
    )
    breaker = CircuitBreakerProvider(inner, failures=2, reset_seconds=10, clock=clock)
    call(breaker)
    call(breaker)  # erro definitivo zera a sequência
    assert state(breaker) is CircuitState.CLOSED
    call(breaker)
    call(breaker)
    assert state(breaker) is CircuitState.OPEN
    clock.now = 10
    call(breaker)  # teste falha: reabre na hora
    assert state(breaker) is CircuitState.OPEN and inner.calls == 5
    clock.now = 15
    assert isinstance(call(breaker), LLMError) and inner.calls == 5


def test_only_one_probe_passes_while_half_open() -> None:
    clock = Clock()

    class Slow:
        calls = 0

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.calls += 1
            await asyncio.sleep(0.05)
            return reply("ok")

    inner = Slow()
    breaker = CircuitBreakerProvider(inner, failures=1, reset_seconds=5, clock=clock)
    breaker._opened_at = 0.0  # já aberto
    clock.now = 5

    async def both() -> list[Any]:
        results = await asyncio.gather(
            breaker.complete(REQUEST), breaker.complete(REQUEST), return_exceptions=True
        )
        return list(results)

    first, second = asyncio.run(both())
    assert isinstance(first, LLMResponse) and isinstance(second, LLMError)
    assert inner.calls == 1 and state(breaker) is CircuitState.CLOSED


def test_engine_wraps_the_provider_unless_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.upper().startswith("NEOSON_") or name.upper() == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "chave-ficticia")
    monkeypatch.setenv("NEOSON_LLM_MODEL", "modelo-x")
    monkeypatch.setenv("NEOSON_DB_PATH", str(tmp_path / "neoson.db"))
    engine: Any = build_engine(load_settings(env_file=None))
    assert isinstance(engine._provider, CircuitBreakerProvider)
    monkeypatch.setenv("NEOSON_LLM_CIRCUIT_FAILURES", "0")
    engine = build_engine(load_settings(env_file=None))
    assert not isinstance(engine._provider, CircuitBreakerProvider)
