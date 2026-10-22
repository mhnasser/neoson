"""Revisão local de fatos pendentes: só uma decisão explícita ativa ou retira."""

from collections.abc import Sequence
from dataclasses import dataclass

from neoson_core.engine.graph import MAX_QUERY_LIMIT, Fact, FactStatus, GraphStore


@dataclass(frozen=True, slots=True)
class PendingFact:
    """`confirmed`/`rejected`: o mesmo trio já foi ativado/retirado em outra fonte."""

    fact: Fact
    confirmed: bool
    rejected: bool


async def pending_facts(store: GraphStore, limit: int) -> list[PendingFact]:
    pending = await store.list_facts(statuses=(FactStatus.PENDING,), limit=limit)
    result = []
    for fact in pending:
        related = await store.facts_about(
            fact.subject.id,
            limit=MAX_QUERY_LIMIT,
            statuses=(FactStatus.ACTIVE, FactStatus.RETRACTED),
        )
        same = [r for r in related if _triple(r) == _triple(fact)]
        result.append(
            PendingFact(
                fact,
                confirmed=any(r.status is FactStatus.ACTIVE for r in same),
                rejected=any(r.status is FactStatus.RETRACTED for r in same),
            )
        )
    return result


async def decide(
    store: GraphStore,
    fact_ids: Sequence[str],
    activate: bool,
    reason: str | None = None,
) -> dict[str, bool]:
    """Ativa ou retira cada fato; `False` quando ausente ou transição inválida."""
    target = FactStatus.ACTIVE if activate else FactStatus.RETRACTED
    return {
        fact_id: await store.set_status(fact_id, target, None if activate else reason)
        for fact_id in fact_ids
    }


def _triple(fact: Fact) -> tuple[str, str, str]:
    return fact.subject.id, fact.relation.value, fact.object.id
