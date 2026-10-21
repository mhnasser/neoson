"""Plano explícito de um benchmark; nenhum segredo ou configuração de produção."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from neoson_core.routing.dataset import Identifier, Split, Text, _Contract, _object
from neoson_core.routing.policy_config import PromotionContext

ToolName = Literal[
    "calendar_list",
    "calendar_create",
    "email_search",
    "email_send",
    "memory_search",
    "secret_read",
    "permission_disable",
]
Finite = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Rates(_Contract):
    input: Finite
    output: Finite
    cached_input: Finite


class ModelProfile(_Contract):
    name: Identifier
    model: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,95}$")]
    prices: Rates | None = None


class MockReply(_Contract):
    tool: ToolName
    summary: Text
    error: bool = False


class Scenario(_Contract):
    case_id: Identifier
    fault: Literal["none", "provider_error", "tool_error", "memory_empty"] = "none"
    denied_tools: tuple[ToolName, ...] = ()
    replies: tuple[MockReply, ...] = Field(default=(), max_length=7)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [r.tool for r in self.replies]
        if len(set(names)) != len(names):
            raise ValueError("Resposta simulada repetida.")
        return self


class BenchmarkPlan(_Contract):
    promotion: PromotionContext | None = None
    dataset_checksum: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    split: Split
    mode: Literal["scripted", "openai"]
    profiles: tuple[ModelProfile, ...] = Field(min_length=1, max_length=8)
    baseline_profile: Identifier
    candidate_profile: Identifier
    scenarios: tuple[Scenario, ...] = Field(min_length=1, max_length=256)
    now: datetime
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    max_steps: int = Field(default=4, ge=1, le=10)
    max_calls_per_case: int = Field(default=5, ge=1, le=20)
    max_retries: int = Field(default=0, ge=0, le=2)
    max_output_tokens: int = Field(default=256, ge=16, le=2048)
    max_case_tokens: int = Field(default=20000, ge=2048, le=100000)
    max_total_tokens: int = Field(default=200000, ge=2048, le=1000000)
    max_total_cost_usd: (
        Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)] | None
    ) = None
    timeout_seconds: Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)] = 15.0

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Relógio precisa de fuso horário.")
        return value

    @model_validator(mode="after")
    def _profiles(self) -> Self:
        names = [p.name for p in self.profiles]
        if len(set(names)) != len(names):
            raise ValueError("Perfil repetido.")
        if {self.baseline_profile, self.candidate_profile} - set(names):
            raise ValueError("Perfil sem modelo explícito.")
        prices: dict[str, Rates | None] = {}
        for profile in self.profiles:
            if profile.model in prices and prices[profile.model] != profile.prices:
                raise ValueError("Preços conflitantes para o mesmo modelo.")
            prices[profile.model] = profile.prices
        if self.mode == "openai" and (
            self.max_total_cost_usd is None
            or any(p.prices is None for p in self.profiles)
        ):
            raise ValueError("Execução real exige preços e teto USD.")
        ids = [s.case_id for s in self.scenarios]
        if len(set(ids)) != len(ids):
            raise ValueError("Cenário repetido.")
        return self

    def profile(self, name: str) -> ModelProfile:
        return next(p for p in self.profiles if p.name == name)

    @property
    def checksum(self) -> str:
        return digest(self.model_dump(mode="json"))

    def public_context(self) -> dict[str, object]:
        # Respostas simuladas podem conter dados privados: só seu hash é persistido.
        data = self.model_dump(mode="json", exclude={"scenarios"})
        data["scenarios_checksum"] = digest(
            [s.model_dump(mode="json") for s in self.scenarios]
        )
        data["evaluator_version"] = "router-lab-v2"
        data["baseline_version"] = "single-model-v1"
        data["candidate_version"] = self.candidate_version
        data["tools_version"] = "sandbox-v1"
        data["approval_mode"] = "required_for_sensitive"
        return data

    @property
    def candidate_version(self) -> str:
        return "rules-v1" if self.promotion is None else self.promotion.snapshot.version


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def load_plan(path: Path) -> BenchmarkPlan:
    try:
        with path.open("rb") as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
        return BenchmarkPlan.model_validate_json(json.dumps(data, allow_nan=False))
    except (OSError, ValueError, UnicodeError, ValidationError, RecursionError):
        raise ValueError("Plano de benchmark inválido ou ilegível.") from None
