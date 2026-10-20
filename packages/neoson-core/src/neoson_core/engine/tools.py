"""Ferramentas e Policy Engine (ADR-003, D15). O modelo propõe; a política decide."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID, uuid4

from neoson_core.engine.llm import ToolSpec
from neoson_core.engine.messages import ToolCall, ToolResult


class RiskLevel(StrEnum):
    READ = "read"
    SENSITIVE_WRITE = "sensitive_write"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class ToolContext:
    run_id: UUID
    session_id: UUID


class ToolRejected(Exception):
    """Pedido de ferramenta inválido (nome desconhecido ou argumentos fora do schema).

    `summary` é seguro para devolver ao modelo.
    """

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


class ToolCatalog(Protocol):
    """Registro de ferramentas: descreve, valida e executa (a política fica fora)."""

    def specs(self) -> tuple[ToolSpec, ...]: ...

    def check(self, call: ToolCall) -> RiskLevel:
        """Nível de risco da ferramenta; `ToolRejected` se o pedido for inválido."""
        ...

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult: ...


class Verdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Approval:
    """Autoriza uma ação concreta (ferramenta + argumentos) até `expires_at`."""

    fingerprint: str
    expires_at: datetime
    approval_id: UUID = field(default_factory=uuid4)


ApprovalMode = Literal["required_for_sensitive", "always"]


class PolicyEngine:
    def __init__(self, mode: ApprovalMode = "required_for_sensitive") -> None:
        if mode not in ("required_for_sensitive", "always"):
            raise ValueError(f"Modo de aprovação desconhecido: {mode}.")
        self._mode = mode
        self._approvals: dict[str, Approval] = {}

    def evaluate(
        self, call: ToolCall, risk: RiskLevel, now: datetime | None = None
    ) -> Verdict:
        if risk is RiskLevel.FORBIDDEN:
            return Verdict.DENY
        needs_approval = self._mode == "always" or risk is RiskLevel.SENSITIVE_WRITE
        if not needs_approval:
            return Verdict.ALLOW
        if self._consume(fingerprint(call), now or datetime.now(UTC)):
            return Verdict.ALLOW
        return Verdict.REQUIRE_APPROVAL

    def grant(
        self, call: ToolCall, ttl: timedelta, now: datetime | None = None
    ) -> Approval:
        approval = Approval(
            fingerprint=fingerprint(call),
            expires_at=(now or datetime.now(UTC)) + ttl,
        )
        self._approvals[approval.fingerprint] = approval
        return approval

    def _consume(self, key: str, now: datetime) -> bool:
        approval = self._approvals.pop(key, None)
        return approval is not None and now < approval.expires_at


def fingerprint(call: ToolCall) -> str:
    """Identifica a ação: nome + argumentos canônicos (não depende do `call.id`)."""
    canonical = json.dumps(
        {"name": call.name, "arguments": dict(call.arguments)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
