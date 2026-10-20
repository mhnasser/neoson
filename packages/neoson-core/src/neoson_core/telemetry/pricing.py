"""Custo estimado das execuções com preços por modelo (USD por milhão de tokens)."""

from collections.abc import Mapping
from dataclasses import dataclass

PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class Price:
    input: float
    output: float
    cached_input: float | None = None


def price_for(model: str | None, prices: Mapping[str, Price]) -> Price | None:
    """Preço do modelo exato ou do maior prefixo configurado (ex.: versão datada)."""
    if not model:
        return None
    if model in prices:
        return prices[model]
    matches = [name for name in prices if model.startswith(name)]
    return prices[max(matches, key=len)] if matches else None


def cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    prices: Mapping[str, Price],
) -> float | None:
    """`None` quando não há preço para o modelo: custo desconhecido não vira zero."""
    price = price_for(model, prices)
    if price is None:
        return None
    cached = min(cached_tokens, input_tokens)
    cached_rate = price.input if price.cached_input is None else price.cached_input
    total = (
        (input_tokens - cached) * price.input
        + cached * cached_rate
        + output_tokens * price.output
    )
    return round(total / PER_MILLION, 8)
