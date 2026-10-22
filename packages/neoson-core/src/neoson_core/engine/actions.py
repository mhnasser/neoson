"""Ações concretas e porta de autorização durável, independente de transporte."""

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from neoson_core.engine.messages import ToolCall, ToolResultStatus
from neoson_core.engine.skills import Skill, SkillRegistry
from neoson_core.engine.tools import (
    PolicyEngine,
    RiskLevel,
    ToolCatalog,
    ToolContext,
    ToolRejected,
    Verdict,
)


def canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: UUID
    run_id: UUID
    session_id: UUID
    owner: str = field(repr=False)
    call: ToolCall = field(repr=False)
    context: str
    expires_at: datetime
    timeout_seconds: float
    risk: RiskLevel
    skill: tuple[str, str, str] | None = None

    def serialize(self) -> str:
        return canonical(
            {
                "id": str(self.id),
                "run_id": str(self.run_id),
                "session_id": str(self.session_id),
                "owner": self.owner,
                "call": {
                    "id": self.call.id,
                    "name": self.call.name,
                    "arguments": dict(self.call.arguments),
                },
                "context": self.context,
                "expires_at": self.expires_at.isoformat(),
                "timeout_seconds": self.timeout_seconds,
                "skill": self.skill,
                "risk": self.risk.value,
            }
        )


@dataclass(frozen=True, slots=True)
class ActionView:
    action: PendingAction = field(repr=False)
    state: str
    decision: str | None = None


class ActionNotFound(LookupError):
    pass


class ActionConflict(ValueError):
    pass


class ActionStore(Protocol):
    def get(self, action_id: UUID, owner: str) -> ActionView: ...
    def for_run(self, run_id: UUID, owner: str) -> tuple[ActionView, ...]: ...
    def open_for(self, owner: str, limit: int = 20) -> tuple[ActionView, ...]: ...
    def decide(self, action_id: UUID, owner: str, approve: bool) -> ActionView: ...
    def claim(self, action: PendingAction) -> bool: ...
    def finish(self, action: PendingAction, state: str) -> None: ...


def action_context(runtime: str, policy: PolicyEngine, tools: ToolCatalog) -> str:
    return checksum(
        canonical(
            {
                "version": "concrete-actions-v1",
                "runtime": runtime,
                "policy": policy.mode,
                "tools": [
                    {"name": s.name, "schema": dict(s.parameters)}
                    for s in sorted(tools.specs(), key=lambda s: s.name)
                ],
            }
        )
    )


def skill_binding(skill: Skill | None) -> tuple[str, str, str] | None:
    if skill is None:
        return None
    seal = checksum(
        canonical(
            {
                "checksum": skill.checksum,
                "tools": sorted(skill.allowed_tools),
                "approval": skill.requires_approval,
            }
        )
    )
    return skill.ref.id, skill.ref.version, seal


class ActionService:
    def __init__(
        self,
        store: ActionStore,
        tools: ToolCatalog,
        policy: PolicyEngine,
        context: str,
        timeout_seconds: float,
        skills: SkillRegistry | None = None,
    ) -> None:
        self.store, self.tools, self.policy = store, tools, policy
        self.context, self.timeout_seconds, self.skills = (
            context,
            timeout_seconds,
            skills,
        )

    async def resume(self, action_id: UUID, owner: str) -> ActionView:
        view = self.store.get(action_id, owner)
        if view.state == "completed":
            return view
        if view.state != "approved":
            raise ActionConflict("Ação não está aprovada para execução.")
        action = view.action
        try:
            if action.context != self.context or action.expires_at <= datetime.now(UTC):
                raise ValueError
            risk = self.tools.check(action.call)
            if risk is RiskLevel.FORBIDDEN or risk is not action.risk:
                raise ValueError
            if self.policy.evaluate(action.call, risk) is Verdict.DENY:
                raise ValueError
            if action.skill is not None:
                if self.skills is None:
                    raise ValueError
                skill = await self.skills.load_approved(*action.skill[:2])
                if skill_binding(skill) != action.skill:
                    raise ValueError
                if action.call.name not in skill.allowed_tools:
                    raise ValueError
            # O registro é a concessão de uso único, nunca uma grant global em memória.
            if (
                not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0
                or action.timeout_seconds <= 0
            ):
                raise ValueError
        except (ValueError, LookupError, ToolRejected):
            self.store.finish(action, "invalid")
            raise ActionConflict(
                "Ação incompatível com as permissões atuais."
            ) from None
        if not self.store.claim(action):
            raise ActionConflict("Ação já reservada ou indisponível.")
        try:
            result = await asyncio.wait_for(
                self.tools.execute(
                    action.call,
                    ToolContext(action.run_id, action.session_id, action.id),
                ),
                timeout=min(self.timeout_seconds, action.timeout_seconds),
            )
            state = "completed" if result.status is ToolResultStatus.OK else "unknown"
        except asyncio.CancelledError:
            self.store.finish(action, "unknown")
            raise
        except Exception:
            state = "unknown"
        # Falha ao confirmar deixa executing, sem liberar outra tentativa.
        self.store.finish(action, state)
        return self.store.get(action_id, owner)
