"""Somente compromisso sem convidados e rascunho; execução exige ação durável."""

from dataclasses import replace
from datetime import timedelta
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from neoson_core.engine.integrations import (
    CalendarWriter,
    EmailDraftWriter,
    IntegrationError,
    PersonalEffect,
)
from neoson_core.engine.llm import ToolSpec
from neoson_core.engine.tools import RiskLevel, ToolContext
from neoson_core.tools.personal_read import failed
from neoson_core.tools.registry import Tool, ToolOutput


class EventArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Título não pode ficar vazio.")
        return value

    @field_validator("start", "end", mode="before")
    @classmethod
    def iso_time(cls, value: object) -> object:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError("Informe horário ISO8601 com offset explícito.")
        return value

    @model_validator(mode="after")
    def interval(self) -> Self:
        if not timedelta(0) < self.end - self.start <= timedelta(hours=24):
            raise ValueError("O intervalo deve ser positivo e de até24horas.")
        return self


class DraftArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=8000)

    @field_validator("subject", "body")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("O texto não pode ficar vazio.")
        return value

    @field_validator("recipient")
    @classmethod
    def single_address(cls, value: str) -> str:
        # Uma caixa ASCII simples; sem listas, display-name ou injeção de header.
        if (
            not value.isascii()
            or value.count("@") != 1
            or any(c.isspace() or c in '<>,;:"()[]\\' for c in value)
        ):
            raise ValueError("Informe um único destinatário válido.")
        local, domain = value.split("@")
        if not local or not domain or "." not in domain or domain.startswith("."):
            raise ValueError("Informe um único destinatário válido.")
        return value


def completed(effect: PersonalEffect, description: str) -> ToolOutput:
    prefix = "Simulação: " if effect.simulated else ""
    return ToolOutput(
        prefix + description,
        data={"id": effect.id, "simulated": effect.simulated},
    )


class CalendarCreate(Tool[EventArguments]):
    name = "calendar_create"
    description = (
        "Propõe compromisso de até24h sem convidados; exige revisão e aprovação."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    arguments = EventArguments

    def __init__(self, writer: CalendarWriter, target: str = "adaptador local") -> None:
        self.writer = writer
        self.target = target

    def spec(self) -> ToolSpec:
        return replace(
            super().spec(), description=self.description + " Destino: " + self.target
        )

    async def run(self, arguments: EventArguments, context: ToolContext) -> ToolOutput:
        if context.action_id is None:
            return failed(IntegrationError("approval"))
        try:
            effect = await self.writer.create_event(
                arguments.title,
                arguments.start,
                arguments.end,
                context.action_id,
            )
        except IntegrationError as error:
            return failed(error)
        return completed(effect, "Compromisso criado sem convidados.")


class EmailDraft(Tool[DraftArguments]):
    name = "email_draft"
    description = (
        "Propõe rascunho em texto simples para um destinatário, sem envio; "
        "exige aprovação."
    )
    risk = RiskLevel.SENSITIVE_WRITE
    arguments = DraftArguments

    def __init__(
        self, writer: EmailDraftWriter, target: str = "adaptador local"
    ) -> None:
        self.writer = writer
        self.target = target

    def spec(self) -> ToolSpec:
        return replace(
            super().spec(), description=self.description + " Destino: " + self.target
        )

    async def run(self, arguments: DraftArguments, context: ToolContext) -> ToolOutput:
        if context.action_id is None:
            return failed(IntegrationError("approval"))
        try:
            effect = await self.writer.create_draft(
                arguments.recipient,
                arguments.subject,
                arguments.body,
                context.action_id,
            )
        except IntegrationError as error:
            return failed(error)
        return completed(effect, "Rascunho criado. Nenhum e-mail foi enviado.")
