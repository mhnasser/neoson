# ADR-005 — Contratos da API em `neoson-contracts`

- **Status:** accepted
- **Data:** 2026-10-19
- **Referências:** [ADR-001](ADR-001-monolito-modular.md), [ADR-004](ADR-004-Packaging.md);
  especificação técnica v1.0, §12 e §12.1

## Contexto

O gateway e a Raspberry trocam mensagens, respostas, aprovações e eventos de estado do robô. Os dois
não serão atualizados ao mesmo tempo. Se cada lado tiver a própria cópia dos schemas, uma mudança
(por exemplo, o gateway passar a exigir `session_id`) só aparece quando o robô tentar falar com o
servidor.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Schemas dentro do `neoson-core` | O robô teria de importar o motor, o que o [ADR-001](ADR-001-monolito-modular.md) proíbe |
| Copiar as classes no gateway e no robô | As cópias divergem, e o erro só aparece em execução |
| Escrever o OpenAPI primeiro e gerar código | Mais ferramentas antes de existir a API. Continua possível depois, porque o FastAPI gera o OpenAPI |

## Decisão

- Os schemas de transporte ficam no pacote `neoson-contracts`, que depende só de Pydantic. Ele não
  depende de FastAPI, SQLAlchemy, Neo4j nem do `neoson-core`.
- O gateway valida as requisições com FastAPI e esses modelos Pydantic, e o OpenAPI sai
  automaticamente. Um cliente futuro em outra linguagem (por exemplo, mobile) pode ser gerado a
  partir dele.
- Há dois tipos de contrato:

| Contrato | Para que serve |
|---|---|
| API HTTP (`api/v1`) | Mensagens, respostas, erros, sessões e aprovações |
| Eventos do robô (`events/v1`) | Estados de escuta, processamento, fala e erro |

```text
packages/neoson-contracts/
├── pyproject.toml
└── src/neoson_contracts/
    ├── __init__.py
    ├── api/v1/
    │   ├── messages.py      # MessageRequest e MessageResponse
    │   └── errors.py        # ErrorResponse
    └── events/v1/
        ├── robot.py
        └── states.py
```

### Versionamento

- Endpoints em `/api/v1/...` e um campo `protocol_version` em cada payload.
- Mudança compatível: só acrescentar campos opcionais. Mudança incompatível: novo módulo `v2` e
  novo endpoint, mantendo o `v1` enquanto houver cliente usando.
- Testes verificam que um cliente da versão anterior continua funcionando depois de uma mudança
  compatível do gateway.
- Os modelos ignoram campos desconhecidos (padrão do Pydantic). É isso que torna compatível
  acrescentar um campo opcional: o lado que ainda não o conhece simplesmente o descarta.
- Toda resposta fora de 2xx usa `ErrorResponse` (`code`, `message`, `details`), inclusive erros
  de validação (422), de rota (404, 405) e internos (500). Os erros nunca devolvem o conteúdo
  recebido.

### Transporte separado do domínio

Os modelos de transporte não são os tipos internos do motor. O `RunContext` do Agent Loop, por
exemplo, nunca é exposto na API. O gateway traduz os contratos para os tipos de
`neoson_core.engine`, e o `engine` não importa `neoson_contracts`.

### Exemplo

`neoson_contracts/api/v1/messages.py`:

```python
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class MessageRequest(BaseModel):
    protocol_version: Literal[1] = 1
    session_id: UUID | None = None
    text: str = Field(min_length=1, max_length=8000)


class MessageResponse(BaseModel):
    protocol_version: Literal[1] = 1
    run_id: UUID
    text: str
    status: Literal["completed", "needs_approval", "failed"]
```

Gateway:

```python
from fastapi import APIRouter
from neoson_contracts.api.v1.messages import MessageRequest, MessageResponse

router = APIRouter()


@router.post("/api/v1/messages", response_model=MessageResponse)
async def receive_message(request: MessageRequest) -> MessageResponse:
    ...  # traduz para os tipos do engine e encaminha ao motor
```

Robô:

```python
from neoson_contracts.api.v1.messages import MessageRequest

message = MessageRequest(text="NeoSon, qual é minha próxima reunião?")
payload = message.model_dump(mode="json")
```

## Implicações

- Uma mudança em `neoson-contracts` afeta o gateway e o robô no mesmo commit, e o CI testa os dois
  juntos.
- A Pi instala Pydantic.
- Cada contrato novo precisa de teste de compatibilidade com a versão anterior.

## Atualização — 2026-10-22: consultas

`neoson_contracts.api.v1.queries` acrescenta os modelos das consultas previstas na
especificação (§12): `RunResponse`, `SessionResponse` (paginação por cursor `before`,
só mensagens do usuário e do assistente), `ReadyResponse` e `MetricsSummary`. São
modelos novos na mesma versão 1, sem mudar os existentes. As consultas exigem o mesmo
bearer, exceto `health/ready`, que só devolve booleanos. Execução e sessão respondem com
`Cache-Control: no-store`.
