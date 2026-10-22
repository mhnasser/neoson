"""Executor da extração e revisão local do grafo; mostra nomes só neste terminal."""

import argparse
import asyncio
from collections.abc import Sequence

from neoson_core.engine.graph import GraphStore, check_limit
from neoson_core.infrastructure.config import ConfigError, ServerSettings, load_settings
from neoson_core.memory.graph_backend import graph_store
from neoson_core.memory.graph_extraction import GraphExtractor, GraphLimits
from neoson_core.memory.graph_review import decide, pending_facts
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.telemetry.pricing import Price


def limits_for(settings: ServerSettings) -> GraphLimits:
    return GraphLimits(
        max_attempts=settings.graph_extraction_max_attempts,
        timeout_seconds=settings.graph_extraction_timeout_seconds,
        max_daily_jobs=settings.graph_extraction_max_daily_jobs,
        max_daily_tokens=settings.graph_extraction_max_daily_tokens,
        max_input_chars=settings.graph_extraction_max_input_chars,
        max_output_tokens=settings.graph_extraction_max_output_tokens,
        max_facts=settings.graph_extraction_max_facts,
        max_daily_cost_usd=settings.graph_extraction_max_daily_cost_usd,
    )


def build_extractor(settings: ServerSettings, store: GraphStore) -> GraphExtractor:
    if not settings.memory_graph_enabled:
        raise ConfigError("Ative NEOSON_MEMORY_GRAPH_ENABLED para extrair fatos.")
    if (
        settings.llm_provider != "openai"
        or not settings.openai_api_key
        or not settings.llm_model
    ):
        raise ConfigError("Extração exige OpenAI, OPENAI_API_KEY e NEOSON_LLM_MODEL.")
    provider = OpenAIProvider.from_api_key(
        settings.openai_api_key.get_secret_value(),
        {settings.primary_model_profile: settings.llm_model},
        settings.graph_extraction_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort,
        max_retries=0,
    )
    return GraphExtractor(
        settings.db_path,
        provider,
        store,
        profile=settings.primary_model_profile,
        limits=limits_for(settings),
        prices={
            m: Price(p.input, p.output, p.cached_input)
            for m, p in settings.llm_prices.items()
        },
        model=settings.llm_model,
    )


async def run(extractor: GraphExtractor, once: bool, poll_seconds: float) -> None:
    while True:
        worked = await extractor.run_once()
        if once:
            return
        if not worked:
            await asyncio.sleep(poll_seconds)


async def show_pending(store: GraphStore, limit: int) -> None:
    rows = await pending_facts(store, limit)
    if not rows:
        print("Nenhum fato pendente.")
    for row in rows:
        fact = row.fact
        notes = [
            note
            for flag, note in (
                (row.confirmed, "já confirmado"),
                (row.rejected, "já rejeitado"),
            )
            if flag
        ]
        print(
            f"{fact.id} | {fact.subject.kind.value} {fact.subject.name!r} "
            f"{fact.relation.value} {fact.object.kind.value} {fact.object.name!r} | "
            f"confiança {fact.provenance.confidence:.2f} | {fact.provenance.source_id}"
            + (f" | {', '.join(notes)}" if notes else "")
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neoson-graph")
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run", help="processa jobs de extração e retirada")
    runner.add_argument("--once", action="store_true", help="processa um job e sai")
    listing = commands.add_parser("pending", help="lista fatos aguardando revisão")
    listing.add_argument("--limit", type=int, default=20)
    activate = commands.add_parser("activate", help="ativa fatos revisados")
    activate.add_argument("ids", nargs="+")
    retract = commands.add_parser("retract", help="retira fatos com um motivo")
    retract.add_argument("ids", nargs="+")
    retract.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        store = graph_store(settings)
        if args.command == "run":
            extractor = build_extractor(settings, store)
        elif args.command == "pending":
            check_limit(args.limit)
    except (ConfigError, ValueError) as error:
        raise SystemExit(str(error)) from None
    try:
        if args.command == "run":
            asyncio.run(run(extractor, args.once, settings.worker_poll_seconds))
        elif args.command == "pending":
            asyncio.run(show_pending(store, args.limit))
        else:
            approve = args.command == "activate"
            reason = None if approve else args.reason
            try:
                results = asyncio.run(decide(store, args.ids, approve, reason))
            except ValueError as error:
                raise SystemExit(str(error)) from None
            for fact_id, changed in results.items():
                print(f"{fact_id}: {'ok' if changed else 'não alterado'}")
            return 0 if all(results.values()) else 1
    except KeyboardInterrupt:
        pass
    return 0
