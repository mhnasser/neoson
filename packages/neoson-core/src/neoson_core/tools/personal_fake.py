"""Fake explícito e leitor desativado, sem rede ou arquivo de credenciais."""

from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from neoson_core.engine.integrations import (
    CalendarItem,
    IntegrationError,
    MailItem,
    ReadPage,
)


class DisabledPersonalReader:
    async def list_events(self, day: date, limit: int) -> ReadPage[CalendarItem]:
        raise IntegrationError("disabled")

    async def list_mail(self, limit: int) -> ReadPage[MailItem]:
        raise IntegrationError("disabled")


class FakePersonalReader:
    def __init__(
        self,
        events: tuple[CalendarItem, ...] = (),
        mail: tuple[MailItem, ...] = (),
    ) -> None:
        self.events, self.mail = events, mail

    @classmethod
    def demo(cls, clock: Callable[[], datetime]) -> "FakePersonalReader":
        now = clock()
        start = datetime.combine(now.date(), time(9), now.tzinfo)
        return cls(
            (
                CalendarItem(
                    "demo-event", "Agenda simulada", start, start + timedelta(hours=1)
                ),
            ),
            (MailItem("demo-mail", "Mensagem simulada", "Remetente fictício", now),),
        )

    async def list_events(self, day: date, limit: int) -> ReadPage[CalendarItem]:
        events = tuple(e for e in self.events if e.start.date() == day)
        return ReadPage(events[:limit], len(events) > limit, simulated=True)

    async def list_mail(self, limit: int) -> ReadPage[MailItem]:
        return ReadPage(self.mail[:limit], len(self.mail) > limit, simulated=True)
