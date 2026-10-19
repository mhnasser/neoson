from uuid import uuid4

from neoson_core.engine.conversation import RunResult, RunStatus, UserMessage


class EchoEngine:
    """Implementação provisória do `Engine`: devolve o texto recebido.

    Mantém o gateway funcionando de ponta a ponta até o Agent Loop existir.
    """

    async def run(self, message: UserMessage) -> RunResult:
        return RunResult(run_id=uuid4(), text=message.text, status=RunStatus.COMPLETED)
