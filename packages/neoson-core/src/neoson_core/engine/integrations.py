"""Portas de leitura pessoal; fornecedores e credenciais ficam nos adaptadores."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


class IntegrationError(Exception):
    """Código estável; não transportar corpo HTTP, conteúdo ou credenciais."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CalendarItem:
    id: str
    title: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class MailItem:
    id: str
    subject: str
    sender: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class ReadPage[T]:
    items: tuple[T, ...]
    truncated: bool = False
    simulated: bool = False


class CalendarReader(Protocol):
    async def list_events(self, day: date, limit: int) -> ReadPage[CalendarItem]: ...


class EmailReader(Protocol):
    async def list_mail(self, limit: int) -> ReadPage[MailItem]: ...
