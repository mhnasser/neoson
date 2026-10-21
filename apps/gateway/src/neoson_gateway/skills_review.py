"""CLI local de revisão; inspeção não gera nem aprova conteúdo."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from difflib import unified_diff
from uuid import UUID

from neoson_core.infrastructure.config import ServerSettings
from neoson_core.skills.evaluation import Evaluation
from neoson_core.skills.review import Comparison, ProposalReview
from neoson_gateway.composition import TOOL_NAMES
from neoson_gateway.skills_eval import build_evaluator


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    proposal = commands.add_parser(
        "proposal", help="inspeciona, avalia e revisa propostas"
    )
    actions = proposal.add_subparsers(dest="proposal_command", required=True)
    actions.add_parser("list", help="lista propostas sem conteúdo privado")
    for name in ("inspect", "eval", "reject", "approve"):
        action = actions.add_parser(name)
        action.add_argument("proposal_id", type=UUID)
        if name == "inspect":
            action.add_argument(
                "--diff", action="store_true", help="mostra diff local das instruções"
            )
        if name == "approve":
            action.add_argument("--comparison", type=UUID, required=True)
            action.add_argument("--accept-unknown-cost", action="store_true")
            action.add_argument("--accept-resource-increase", action="store_true")


def context_hash(settings: ServerSettings) -> str:
    # Sem caminhos, tokens ou credenciais. Alterar o avaliador exige nova revisão.
    values = {
        "evaluator": "fixtures-review-v1",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "reasoning": settings.llm_reasoning_effort,
        "profile": settings.primary_model_profile,
        "steps": settings.max_agent_steps,
        "timeout": settings.run_timeout_seconds,
        "approval": settings.approval_mode,
        "tools": TOOL_NAMES,
        "prices": {m: p.model_dump() for m, p in settings.llm_prices.items()},
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def execute(args: argparse.Namespace, settings: ServerSettings) -> int:
    review = ProposalReview(settings.db_path)
    if args.proposal_command == "list":
        for p in review.proposals.list():
            print(
                f"{p.id} {p.candidate.metadata.id} {p.base_version} → "
                f"{p.candidate.metadata.version} {p.status}"
            )
        return 0
    proposal = review.proposals.get(args.proposal_id)
    if args.proposal_command == "eval":
        result = asyncio.run(
            review.compare(
                proposal.id, build_evaluator(settings), context_hash(settings)
            )
        )
        print_comparison(result)
        return 0 if result.passed else 1
    if args.proposal_command == "approve":
        review.approve(
            proposal.id,
            args.comparison,
            context_hash(settings),
            accept_unknown_cost=args.accept_unknown_cost,
            accept_resource_increase=args.accept_resource_increase,
        )
        print(
            f"Aprovada {proposal.candidate.metadata.id} "
            f"{proposal.candidate.metadata.version}."
        )
        return 0
    if args.proposal_command == "reject":
        review.proposals.reject(proposal.id)
        print("Proposta rejeitada.")
        return 0
    print(f"Proposta {proposal.id}: {proposal.status}")
    print(f"Base {proposal.base_version}: {proposal.base_checksum}")
    print(
        f"Candidata {proposal.candidate.metadata.version}: "
        f"{proposal.candidate.checksum}"
    )
    print(f"Fontes: {len(proposal.sources)}")
    comparison = review.latest(proposal.id)
    if comparison is not None:
        print_comparison(comparison)
    if args.diff:
        base = review.base(proposal.id)
        for line in unified_diff(
            base.instructions.splitlines(),
            proposal.candidate.instructions.splitlines(),
            fromfile="base/SKILL.md",
            tofile="candidata/SKILL.md",
            lineterm="",
        ):
            # Escapar controles impede execução de sequências no terminal.
            print(json.dumps(line, ensure_ascii=False)[1:-1])
    return 0


def print_comparison(result: Comparison) -> None:
    print(f"Comparação {result.id}")
    for b, c in zip(result.base.cases, result.candidate.cases, strict=True):
        print(
            f"{b.id}: base {'PASS' if b.passed else 'FAIL'} → "
            f"candidata {'PASS' if c.passed else 'FAIL'}"
        )
    _metrics("Base", result.base)
    _metrics("Candidata", result.candidate)
    print("Regressões: " + (", ".join(result.regressions) or "nenhuma"))
    print(f"Aumento de recursos: {'sim' if result.resource_increase else 'não'}")
    print(f"Custo desconhecido: {'sim' if result.unknown_cost else 'não'}")


def _metrics(label: str, report: Evaluation) -> None:
    cost = "desconhecido" if report.cost_usd is None else f"USD {report.cost_usd:.8f}"
    u = report.usage
    print(
        f"{label}: {report.passed_cases}/{len(report.cases)}; "
        "tokens entrada/saída/cache "
        f"{u.input_tokens}/{u.output_tokens}/{u.cached_tokens}; custo {cost}; "
        f"duração {report.duration_seconds:.3f}s"
    )
