"""Roteador por regras (`rules-v1`): primeira política, explicável e sem custo."""

import re
import unicodedata

from neoson_core.engine import RouteDecision, RouteRequest

POLICY_VERSION = "rules-v1"
LONG_REQUEST_CHARS = 600

_INTENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("calendar", re.compile(r"\b(agenda|compromisso|reuniao|evento|calendario)s?\b")),
    ("email", re.compile(r"\b(e-?mail|caixa de entrada)s?\b")),
)


class RulesRouter:
    """Mantém o modelo principal fixo (preserva cache) e decide só intenção e porte."""

    def __init__(self, primary_model_profile: str) -> None:
        self._profile = primary_model_profile

    async def decide(self, request: RouteRequest) -> RouteDecision:
        text = _normalize(request.text)
        for intent, pattern in _INTENTS:
            if pattern.search(text):
                return self._decision(intent, request, 0.8, f"keyword:{intent}")
        return self._decision("conversation", request, 0.6, "default:conversation")

    def _decision(
        self, intent: str, request: RouteRequest, confidence: float, code: str
    ) -> RouteDecision:
        long_request = len(request.text) > LONG_REQUEST_CHARS
        return RouteDecision(
            intent=intent,
            complexity="medium" if long_request else "low",
            execution_mode="direct",
            primary_model_profile=self._profile,
            confidence=confidence,
            policy_version=POLICY_VERSION,
            rationale_code=code,
        )


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))
