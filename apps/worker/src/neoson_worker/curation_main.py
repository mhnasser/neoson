"""Executor separado; o worker da outbox nunca aguarda chamadas ao modelo."""

import argparse
import asyncio
from collections.abc import Sequence

from neoson_core.infrastructure.config import ConfigError, ServerSettings, load_settings
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.skills.curation import CurationLimits, Curator
from neoson_core.telemetry.pricing import Price


def limits_for(settings: ServerSettings) -> CurationLimits:
    return CurationLimits(
        max_attempts=settings.curation_max_attempts,
        timeout_seconds=settings.curation_timeout_seconds,
        max_daily_jobs=settings.curation_max_daily_jobs,
        max_daily_tokens=settings.curation_max_daily_tokens,
        max_input_chars=settings.curation_max_input_chars,
        max_output_tokens=settings.curation_max_output_tokens,
        max_daily_cost_usd=settings.curation_max_daily_cost_usd,
    )


def build_curator(settings: ServerSettings) -> Curator:
    if not settings.curation_enabled:
        raise ConfigError("Ative NEOSON_CURATION_ENABLED para executar a curadoria.")
    if (
        settings.llm_provider != "openai"
        or not settings.openai_api_key
        or not settings.llm_model
    ):
        raise ConfigError("Curadoria exige OpenAI, OPENAI_API_KEY e NEOSON_LLM_MODEL.")
    provider = OpenAIProvider.from_api_key(
        settings.openai_api_key.get_secret_value(),
        {settings.primary_model_profile: settings.llm_model},
        settings.curation_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort,
        max_retries=0,
    )
    prices = {
        m: Price(p.input, p.output, p.cached_input)
        for m, p in settings.llm_prices.items()
    }
    return Curator(
        settings.db_path,
        provider,
        profile=settings.primary_model_profile,
        limits=limits_for(settings),
        prices=prices,
        model=settings.llm_model,
    )


async def execute(curator: Curator, once: bool, poll_seconds: float) -> None:
    while True:
        await curator.run_once()
        if once:
            return
        await asyncio.sleep(poll_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neoson-curator")
    parser.add_argument("--once", action="store_true", help="tenta um job e sai")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        curator = build_curator(settings)
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    try:
        asyncio.run(execute(curator, args.once, settings.worker_poll_seconds))
    except KeyboardInterrupt:
        pass
    return 0
