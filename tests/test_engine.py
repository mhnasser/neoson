import asyncio
from uuid import uuid4

from neoson_core.engine import EchoEngine, RunStatus, UserMessage


def test_echo_engine_returns_the_text_as_a_completed_run() -> None:
    message = UserMessage(text="oi", session_id=uuid4())

    result = asyncio.run(EchoEngine().run(message))

    assert result.text == "oi"
    assert result.status is RunStatus.COMPLETED


def test_each_run_gets_its_own_id() -> None:
    engine = EchoEngine()

    first = asyncio.run(engine.run(UserMessage(text="a")))
    second = asyncio.run(engine.run(UserMessage(text="b")))

    assert first.run_id != second.run_id
