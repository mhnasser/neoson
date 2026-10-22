"""Executor da extração e revisão local do grafo; mostra nomes só neste terminal."""

import argparse
import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path

from neoson_core.engine.graph import GraphStore, check_limit
from neoson_core.infrastructure.config import ConfigError, ServerSettings, load_settings
from neoson_core.memory.graph_backend import graph_store
from neoson_core.memory.graph_extraction import GraphExtractor, GraphLimits
from neoson_core.memory.graph_review import decide, pending_facts
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.telemetry.pricing import Price
from neoson_worker.graph_eval import evaluate, export_panel, load_dataset, save_report

DEFAULT_DATASET = Path("datasets/graph_retrieval/seed-v1.json")


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


def run_evaluation(args: argparse.Namespace) -> int:
    """Banco temporário semeado pelo dataset: os dados reais não participam."""
    try:
        settings = load_settings()
        if args.max_cases is not None and not 1 <= args.max_cases <= 100:
            raise ValueError("--max-cases deve estar entre 1 e 100.")
        dataset, checksum = load_dataset(args.dataset)
        provider = live_provider(settings) if args.live else None
    except (ConfigError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        report = asyncio.run(
            evaluate(
                dataset,
                checksum,
                Path(workdir),
                provider=provider,
                profile=settings.primary_model_profile,
                prices=prices_for(settings),
                max_cases=args.max_cases,
            )
        )
    path = save_report(report, args.output)
    export_panel(report, settings.vault_path)
    history, hybrid = report["arms"]["history"], report["arms"]["hybrid"]
    for key in history:
        print(f"{key}: só histórico {history[key]} | híbrido {hybrid[key]}")
    print(f"relatório: {path}")
    return 0


def live_provider(settings: ServerSettings) -> OpenAIProvider:
    if (
        settings.llm_provider != "openai"
        or not settings.openai_api_key
        or not settings.llm_model
    ):
        raise ConfigError("--live exige OpenAI, OPENAI_API_KEY e NEOSON_LLM_MODEL.")
    return OpenAIProvider.from_api_key(
        settings.openai_api_key.get_secret_value(),
        {settings.primary_model_profile: settings.llm_model},
        settings.run_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort,
        max_retries=0,
    )


def prices_for(settings: ServerSettings) -> dict[str, Price]:
    return {
        m: Price(p.input, p.output, p.cached_input)
        for m, p in settings.llm_prices.items()
    }


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
    evaluation = commands.add_parser("eval", help="compara a busca com e sem grafo")
    evaluation.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    evaluation.add_argument(
        "--live", action="store_true", help="responde com o modelo real (tem custo)"
    )
    evaluation.add_argument("--max-cases", type=int)
    evaluation.add_argument("--output", type=Path, default=Path("data/graph-eval"))
    args = parser.parse_args(argv)
    if args.command == "eval":
        return run_evaluation(args)
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


if __name__ == "__main__":
    raise SystemExit(main())
