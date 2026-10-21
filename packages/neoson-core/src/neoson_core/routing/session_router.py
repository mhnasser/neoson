"""Sessões mantêm snapshot e modelo; fallback de regras é explícito na decisão."""

from dataclasses import replace

from neoson_core.engine import RouteDecision, RouteRequest
from neoson_core.routing.policy_config import ModelBinding, SnapshotRouter
from neoson_core.routing.policy_registry import PolicyRegistry


class SessionRouter:
    def __init__(
        self, registry: PolicyRegistry, binding: ModelBinding, runtime_hash: str
    ) -> None:
        self.registry, self.binding = registry, binding
        self.runtime_hash = runtime_hash

    async def decide(self, request: RouteRequest) -> RouteDecision:
        if request.session_id is None:
            raise ValueError("Roteamento persistente exige sessão resolvida.")
        snapshot, alias, fallback = self.registry.bind(
            request.session_id, self.binding, self.runtime_hash
        )
        result = await SnapshotRouter(snapshot, alias).decide(request)
        return result if fallback is None else replace(result, rationale_code=fallback)
