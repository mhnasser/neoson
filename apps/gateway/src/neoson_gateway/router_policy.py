"""Revisão explícita: python -m neoson_gateway.router_policy."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

from neoson_core.infrastructure.config import load_settings
from neoson_core.routing.dataset import load_dataset
from neoson_core.routing.lab_plan import load_plan
from neoson_core.routing.policy_registry import PolicyRegistry
from neoson_core.telemetry.pricing import Price
from neoson_gateway.routing_runtime import binding_for, runtime_hash


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revisão e rollback de políticas.")
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    promote = commands.add_parser("promote")
    promote.add_argument("evaluation_id", type=UUID)
    promote.add_argument("dataset", type=Path)
    promote.add_argument("plan", type=Path)
    promote.add_argument("--evidence-db", type=Path, required=True)
    promote.add_argument("--request-id", type=UUID)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--expected-revision", required=True)
    rollback.add_argument("--request-id", type=UUID)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        registry = PolicyRegistry(args.db or settings.db_path)
        if args.command == "status":
            print(
                json.dumps(
                    {**registry.status(), "runtime_hash": runtime_hash(settings)},
                    ensure_ascii=False,
                )
            )
        elif args.command == "promote":
            if settings.approval_mode != "required_for_sensitive":
                raise ValueError(
                    "Modo de aprovação incompatível com o benchmark atual."
                )
            version = registry.promote(
                args.request_id or uuid4(),
                args.evaluation_id,
                args.evidence_db,
                load_dataset(args.dataset),
                load_plan(args.plan),
                runtime_hash(settings),
                binding_for(settings),
                {
                    model: Price(p.input, p.output, p.cached_input)
                    for model, p in settings.llm_prices.items()
                },
            )
            print(f"Política aprovada: {version}.")
        else:
            version = registry.rollback(
                args.request_id or uuid4(), args.expected_revision
            )
            print(f"Rollback concluído: {version}.")
    except Exception:
        print(
            "Revisão não concluída: verifique evidência, contexto, limites "
            "e revisão atual.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
