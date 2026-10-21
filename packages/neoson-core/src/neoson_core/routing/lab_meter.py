"""Contabiliza chamadas e retries; falha sem usage permanece desconhecida."""

import asyncio
import json
from dataclasses import dataclass, replace
from time import perf_counter

from neoson_core.engine import LLMError, LLMProvider, LLMRequest, LLMResponse, Usage
from neoson_core.routing.lab_plan import BenchmarkPlan, ModelProfile, Scenario, digest
from neoson_core.telemetry.pricing import Price, cost_usd


@dataclass(frozen=True, slots=True)
class Attempt:
    profile: str
    model_checksum: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None
    seconds: float
    error: str | None


@dataclass
class Ledger:
    tokens: int = 0
    cost: float = 0.0
    unknown_cost: bool = False


class Meter:
    def __init__(
        self,
        provider: LLMProvider,
        plan: BenchmarkPlan,
        scenario: Scenario,
        ledger: Ledger,
    ) -> None:
        self.provider, self.plan, self.scenario, self.ledger = (
            provider,
            plan,
            scenario,
            ledger,
        )
        self.attempts: list[Attempt] = []
        self.tokens = 0
        self.blocked = False
        self.injected = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        profile = self.plan.profile(request.model_profile)
        for retry in range(self.plan.max_retries + 1):
            self._admit(request, profile)
            started = perf_counter()
            try:
                if self.scenario.fault == "provider_error":
                    self.injected = True
                    # Não é uma tentativa de rede: não há cobrança desconhecida aqui.
                    self.attempts.append(
                        Attempt(
                            profile.name,
                            None,
                            0,
                            0,
                            0,
                            0.0,
                            0.0,
                            "injected_provider_error",
                        )
                    )
                    raise LLMError("Falha injetada no provedor.", retryable=False)
                response = await asyncio.wait_for(
                    self.provider.complete(
                        replace(request, max_output_tokens=self.plan.max_output_tokens)
                    ),
                    timeout=self.plan.timeout_seconds,
                )
            except BaseException as error:
                if self.injected:
                    raise
                self.ledger.unknown_cost = True
                code = (
                    "provider_error" if isinstance(error, LLMError) else "interrupted"
                )
                self.attempts.append(
                    Attempt(
                        profile.name,
                        None,
                        None,
                        None,
                        None,
                        None,
                        perf_counter() - started,
                        code,
                    )
                )
                if (
                    isinstance(error, LLMError)
                    and error.retryable
                    and retry < self.plan.max_retries
                ):
                    continue
                if isinstance(error, Exception):
                    raise LLMError(
                        "Falha do provedor no benchmark.", retryable=False
                    ) from None
                raise
            usage = response.usage
            if not _valid_usage(usage):
                self.ledger.unknown_cost = True
                self.attempts.append(
                    Attempt(
                        profile.name,
                        None,
                        None,
                        None,
                        None,
                        None,
                        perf_counter() - started,
                        "invalid_usage",
                    )
                )
                raise LLMError("Consumo inválido do provedor.", retryable=False)
            rates = profile.prices
            prices = (
                {}
                if rates is None
                else {
                    profile.model: Price(rates.input, rates.output, rates.cached_input)
                }
            )
            cost = cost_usd(
                response.model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                prices,
            )
            self.tokens += usage.total_tokens
            self.ledger.tokens += usage.total_tokens
            if cost is None:
                self.ledger.unknown_cost = True
            else:
                self.ledger.cost += cost
            if (
                usage.output_tokens > self.plan.max_output_tokens
                or self.tokens > self.plan.max_case_tokens
                or self.ledger.tokens > self.plan.max_total_tokens
                or (
                    self.plan.max_total_cost_usd is not None
                    and self.ledger.cost > self.plan.max_total_cost_usd
                )
            ):
                self.blocked = True
            self.attempts.append(
                Attempt(
                    profile.name,
                    digest(response.model),
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_tokens,
                    cost,
                    perf_counter() - started,
                    None,
                )
            )
            return response
        raise AssertionError("Retry fora dos limites.")

    def _admit(self, request: LLMRequest, profile: ModelProfile) -> None:
        # Limite conservador em bytes, incluindo framing. A API recebe limite de saída.
        upper = (
            len(
                json.dumps(
                    [
                        [
                            (
                                m.role.value,
                                m.content,
                                [(c.name, dict(c.arguments)) for c in m.tool_calls],
                            )
                            for m in request.messages
                        ],
                        [
                            (t.name, t.description, dict(t.parameters))
                            for t in request.tools
                        ],
                    ],
                    ensure_ascii=False,
                ).encode()
            )
            + 1024
        )
        reserved = upper + self.plan.max_output_tokens
        rates = profile.prices
        usd = (
            None
            if rates is None
            else (
                upper * max(rates.input, rates.cached_input)
                + self.plan.max_output_tokens * rates.output
            )
            / 1_000_000
        )
        if (
            self.blocked
            or len(self.attempts) >= self.plan.max_calls_per_case
            or self.tokens + reserved > self.plan.max_case_tokens
            or self.ledger.tokens + reserved > self.plan.max_total_tokens
            or (
                self.plan.max_total_cost_usd is not None
                and (
                    self.ledger.unknown_cost
                    or usd is None
                    or self.ledger.cost + usd > self.plan.max_total_cost_usd
                )
            )
        ):
            self.blocked = True
            raise LLMError("Orçamento do benchmark esgotado.", retryable=False)


def _valid_usage(usage: Usage) -> bool:
    return (
        all(
            type(v) is int and v >= 0
            for v in (usage.input_tokens, usage.output_tokens, usage.cached_tokens)
        )
        and usage.cached_tokens <= usage.input_tokens
    )
