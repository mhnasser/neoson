"""Composition root das avaliações; ferramentas reais só fornecem schemas e riscos."""

from datetime import UTC, datetime
from pathlib import Path

from neoson_core.engine import (
    AgentLoop,
    Budget,
    Engine,
    PolicyEngine,
    SkillRegistry,
    ToolCatalog,
)
from neoson_core.infrastructure.config import ServerSettings
from neoson_core.memory.curated import SqliteCuratedMemory
from neoson_core.memory.session_store import SqliteConversationStore
from neoson_core.routing.rules import RulesRouter
from neoson_core.skills.evaluation import Evaluation, SkillEvaluator
from neoson_core.telemetry.pricing import Price
from neoson_gateway.composition import build_provider, build_tools


def build_evaluator(settings: ServerSettings) -> SkillEvaluator:
    provider = build_provider(settings)

    def catalog(path: Path) -> ToolCatalog:
        isolated = settings.model_copy(update={"db_path": path})
        return build_tools(
            isolated, SqliteCuratedMemory(path), lambda: datetime.now(UTC)
        )

    def engine(
        path: Path, registry: SkillRegistry, tools: ToolCatalog, now: datetime
    ) -> Engine:
        return AgentLoop(
            provider,
            RulesRouter(settings.primary_model_profile),
            Budget(
                max_steps=settings.max_agent_steps,
                timeout_seconds=settings.run_timeout_seconds,
            ),
            tools=tools,
            policy=PolicyEngine(settings.approval_mode),
            clock=lambda: now,
            store=SqliteConversationStore(path),
            skills=registry,
        )

    prices = {
        model: Price(p.input, p.output, p.cached_input)
        for model, p in settings.llm_prices.items()
    }
    return SkillEvaluator(settings.db_path, engine, catalog, prices)


async def evaluate_skill(settings: ServerSettings, skill_id: str) -> Evaluation:
    return await build_evaluator(settings).evaluate(skill_id)
