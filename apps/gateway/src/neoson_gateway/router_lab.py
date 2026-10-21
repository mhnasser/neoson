"""CLI explícita do laboratório: python -m neoson_gateway.router_lab."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

from neoson_core.engine import (
    AgentLoop,
    Budget,
    Engine,
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    Usage,
)
from neoson_core.engine.route import Router
from neoson_core.infrastructure.config import load_settings
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.providers.fake import ScriptedProvider
from neoson_core.providers.openai_provider import OpenAIProvider
from neoson_core.routing.benchmark import ProviderFactory, RouterBenchmark
from neoson_core.routing.dataset import load_dataset
from neoson_core.routing.lab_plan import BenchmarkPlan, load_plan
from neoson_core.routing.lab_sandbox import Sandbox
from neoson_core.routing.lab_store import EvaluationStore


def build_engine(
    path: Path,
    provider: LLMProvider,
    router: Router,
    tools: Sandbox,
    plan: BenchmarkPlan,
) -> Engine:
    return AgentLoop(
        provider,
        router,
        Budget(plan.max_steps, plan.timeout_seconds, plan.max_case_tokens),
        tools=tools,
        policy=tools.policy,
        clock=lambda: plan.now,
        store=SqliteConversationStore(path),
    )


def build_provider(plan: BenchmarkPlan) -> ProviderFactory:
    if plan.mode == "scripted":
        # Resposta constante, sem ler expectativas para fabricar uma nota.
        return lambda case, profile: ScriptedProvider(
            [
                LLMResponse(
                    Message(Role.ASSISTANT, "Execução simulada."),
                    Usage(10, 5, 0),
                    plan.profile(profile).model,
                )
            ]
        )
    settings = load_settings()
    if settings.openai_api_key is None:
        raise ValueError("Credencial do provedor ausente.")
    provider = OpenAIProvider.from_api_key(
        settings.openai_api_key.get_secret_value(),
        {p.name: p.model for p in plan.profiles},
        plan.timeout_seconds,
        reasoning_effort=plan.reasoning_effort,
        max_retries=0,
    )
    return lambda case, profile: provider


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark isolado de roteamento.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/router-lab.db"))
    parser.add_argument("--request-id", type=UUID)
    parser.add_argument(
        "--live", action="store_true", help="habilita plano openai e API real"
    )
    args = parser.parse_args(argv)
    try:
        dataset, plan = load_dataset(args.dataset), load_plan(args.plan)
        if (plan.mode == "openai") != args.live:
            raise ValueError("Modo do plano e --live incompatíveis.")
        # Provider é construído apenas depois de validar o plano; sem chamada aqui.
        runner = RouterBenchmark(
            EvaluationStore(args.db), build_engine, build_provider(plan)
        )
        report = asyncio.run(runner.run(dataset, plan, args.request_id or uuid4()))
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    except Exception:
        print(
            "Benchmark não concluído: verifique dataset, plano, orçamento e UUID.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
