"""Agenda fake (`calendar_list`) do primeiro incremento (§19.1).

Será trocada pela integração real com o calendário, com autorização.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, ConfigDict, Field

from neoson_core.engine.tools import RiskLevel, ToolContext
from neoson_core.tools.registry import Tool, ToolOutput


class CalendarListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date = Field(description="Dia a consultar, no formato AAAA-MM-DD.")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    start: time
    title: str
    duration_minutes: int


class FakeCalendar(Tool[CalendarListArguments]):
    name = "calendar_list"
    description = "Lista os compromissos da agenda do usuário em um dia."
    risk = RiskLevel.READ
    arguments = CalendarListArguments

    def __init__(self, lookup: Callable[[date], Sequence[CalendarEvent]]) -> None:
        self._lookup = lookup

    @classmethod
    def fixed(cls, events: Mapping[date, Sequence[CalendarEvent]]) -> "FakeCalendar":
        return cls(lambda day: events.get(day, ()))

    @classmethod
    def demo(cls, clock: Callable[[], datetime]) -> "FakeCalendar":
        """Agenda de demonstração relativa ao dia atual do relógio dado."""
        by_offset = {
            0: (
                CalendarEvent(time(9, 0), "Reunião de planejamento do NeoSon", 60),
                CalendarEvent(time(14, 30), "Revisão de código", 45),
            ),
            1: (CalendarEvent(time(10, 0), "Consulta no dentista", 60),),
        }
        return cls(
            lambda day: by_offset.get((day - clock().date()) // timedelta(days=1), ())
        )

    async def run(
        self, arguments: CalendarListArguments, context: ToolContext
    ) -> ToolOutput:
        events = sorted(self._lookup(arguments.day), key=lambda e: e.start)
        day = arguments.day.isoformat()
        if not events:
            return ToolOutput(
                summary=f"Nenhum compromisso em {day}.", data={"day": day, "events": []}
            )
        items = "; ".join(
            f"{e.start:%H:%M} {e.title} ({e.duration_minutes} min)" for e in events
        )
        return ToolOutput(
            summary=f"Compromissos em {day}: {items}.",
            data={
                "day": day,
                "events": [
                    {
                        "start": e.start.isoformat(timespec="minutes"),
                        "title": e.title,
                        "duration_minutes": e.duration_minutes,
                    }
                    for e in events
                ],
            },
        )
