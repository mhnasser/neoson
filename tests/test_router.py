import asyncio

import pytest

from neoson_core.engine import RouteDecision, RouteRequest
from neoson_core.routing.rules import LONG_REQUEST_CHARS, POLICY_VERSION, RulesRouter


def decide(text: str) -> RouteDecision:
    return asyncio.run(RulesRouter("balanced").decide(RouteRequest(text=text)))


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Qual é minha próxima REUNIÃO?", "calendar"),
        ("o que tenho na agenda amanhã", "calendar"),
        ("Tenho e-mails novos?", "email"),
        ("Me conta uma curiosidade", "conversation"),
    ],
)
def test_intent_comes_from_keywords(text: str, intent: str) -> None:
    decision = decide(text)

    assert decision.intent == intent
    assert decision.execution_mode == "direct"
    assert decision.policy_version == POLICY_VERSION


def test_profile_stays_fixed_and_long_requests_are_medium() -> None:
    short = decide("oi")
    long = decide("x" * (LONG_REQUEST_CHARS + 1))

    assert short.primary_model_profile == long.primary_model_profile == "balanced"
    assert (short.complexity, long.complexity) == ("low", "medium")
    assert short.rationale_code == "default:conversation"
