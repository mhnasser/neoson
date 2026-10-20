# NeoSon

Assistente pessoal de IA com interface física, motor Python próprio, memória, skills e melhoria
contínua. A especificação técnica e os diagramas estão em [`docs/architecture/`](docs/architecture/);
as decisões de arquitetura, em [`docs/decisions/`](docs/decisions/).

> **Estado:** fundação pronta (workspace, configuração, CI) e primeira versão do motor. O cliente
> `neoson-robot` envia mensagens ao gateway em `POST /api/v1/messages`, e o Agent Loop responde
> pelo modelo configurado (OpenAI), com roteador por regras, limites de passos, tempo e tokens, e
> ferramentas autorizadas por uma Policy Engine (por enquanto, uma agenda de demonstração).
> Persistência da conversa, autenticação e worker ainda estão em construção.

## Requisitos

- Python 3.13, fixado em `.python-version` (a mesma versão da Raspberry Pi).
- [uv](https://docs.astral.sh/uv/) 0.12 (testado com 0.12.21).

## Setup

```bash
uv sync --all-packages
```

O comando cria `.venv/` com Python 3.13 e instala todos os membros do workspace em modo editável,
junto com as ferramentas de desenvolvimento, nas versões fixadas em `uv.lock`. Se o Python 3.13 não
estiver instalado, o uv faz o download.

Em pastas sincronizadas (OneDrive, Dropbox), use `UV_LINK_MODE=copy`: o uv não consegue criar
hardlinks nelas.

## Executar

```bash
uv run --package neoson-gateway neoson-gateway   # padrão: http://127.0.0.1:8000 (NEOSON_GATEWAY_*)
uv run --package neoson-robot neoson-robot "Olá, NeoSon"   # envia uma mensagem e mostra a resposta
```

O `neoson-robot` é o cliente da Raspberry Pi. Ele depende só de `neoson-contracts` e de um
cliente HTTP, sem nada do servidor; para instalar apenas ele:

```bash
uv sync --package neoson-robot --no-dev
```

Endereço do gateway: `--gateway-url` > `NEOSON_GATEWAY_URL` > `http://127.0.0.1:8000`. O robô lê
só variáveis do processo (não lê o `neoson.env`). Saída 0 em sucesso; 1 se o gateway falhar, não
responder ou responder fora do contrato; 2 para mensagem, opção ou URL inválida.

## API

| Método e rota | Uso |
|---|---|
| `GET /api/v1/health/live` | Indica que o processo está ativo |
| `POST /api/v1/messages` | Recebe `{"text": "..."}` (e `session_id` opcional) e devolve a resposta do NeoSon |

A resposta vem do Agent Loop, pelo modelo configurado em `NEOSON_LLM_MODEL`. O modelo pode usar
ferramentas registradas; cada pedido é validado e passa pela Policy Engine, e ações sensíveis
suspendem a execução com `needs_approval` ([ADR-003](docs/decisions/ADR-003-Seguranca-Tools.md)).
Hoje há só `calendar_list`, com uma agenda fake de demonstração. Para desenvolver sem
chave da OpenAI, use `NEOSON_LLM_PROVIDER=echo`: o motor devolve o próprio texto recebido. Os
schemas ficam em
`neoson-contracts` ([ADR-005](docs/decisions/ADR-005-API-Contracts.md)). Toda resposta fora de 2xx
segue o formato `ErrorResponse`:

```json
{"protocol_version": 1, "code": "invalid_request", "message": "Requisição inválida.",
 "details": [{"location": "body.text", "message": "String should have at least 1 character"}]}
```

Documentação interativa (OpenAPI) em `/docs`.

## Configuração

```bash
cp .env.example neoson.env
```

Preencha os valores em `neoson.env`. O arquivo está no `.gitignore` e não deve ir para o Git.

O servidor lê a configuração com `neoson_core.infrastructure.config.load_settings()`:

- **Precedência:** variáveis do processo > `neoson.env` no diretório atual > valores padrão.
- **Validação:** tipos e limites são conferidos na inicialização. Um valor inválido encerra o
  gateway com uma mensagem que cita a variável, sem mostrar o valor recebido.
- **Segredos:** `OPENAI_API_KEY` fica oculta em `repr`, logs e mensagens de erro.
- Variáveis desconhecidas e valores em branco são ignorados (valem os padrões).

O [`.env.example`](.env.example) lista todas as variáveis, com os padrões e os valores aceitos.

## Testes e qualidade

```bash
uv run pytest                  # testes
uv run ruff check .            # lint
uv run ruff format --check .   # formatação
uv run mypy                    # tipagem (modo estrito, todos os membros e testes)
uv run lint-imports            # limites de importação entre pacotes (ADR-006)
```

Os testes marcados como `live` chamam a API real da OpenAI (custo de centavos) e ficam
desligados por padrão; o CI não os executa. Para rodá-los, com `OPENAI_API_KEY` e
`NEOSON_LLM_MODEL` no `neoson.env`:

```bash
NEOSON_LIVE_TESTS=1 uv run pytest -m live
```

O GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) roda as mesmas verificações
a cada pull request e a cada push na `main`, com Python 3.13 e as versões do `uv.lock`.

## Estrutura

```text
packages/neoson-contracts/   schemas de transporte compartilhados (ADR-005)
packages/neoson-core/        motor do NeoSon
apps/gateway/                gateway HTTP (FastAPI)
apps/robot/                  cliente da Raspberry Pi (só contratos e HTTP)
tests/                       testes automatizados
docs/                        arquitetura, diagramas, decisões e inventário de hardware
```

O workspace e as regras de dependência entre pacotes estão descritos no
[ADR-001](docs/decisions/ADR-001-monolito-modular.md) e no
[ADR-004](docs/decisions/ADR-004-Packaging.md).

## Licença

[MIT](LICENSE).
