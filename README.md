# NeoSon

Assistente pessoal de IA com interface física, motor Python próprio, memória, skills e melhoria
contínua. A especificação técnica e os diagramas estão em [`docs/architecture/`](docs/architecture/);
as decisões de arquitetura, em [`docs/decisions/`](docs/decisions/).

> **Estado:** fundação pronta (workspace, configuração, CI) e primeira versão do motor. O cliente
> `neoson-robot` envia mensagens ao gateway em `POST /api/v1/messages`, e o Agent Loop responde
> pelo modelo configurado (OpenAI), com roteador por regras, limites de passos, tempo e tokens, e
> ferramentas autorizadas por uma Policy Engine (por enquanto, uma agenda de demonstração). A API
> exige token, a conversa fica gravada em SQLite (sobrevive a reinícios) e o worker consome os
> eventos da outbox com lease, retry e idempotência. Memória, Obsidian e skills vêm nas próximas
> fases.

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
uv run --package neoson-worker neoson-worker    # consome a outbox (--once: processa e sai)
uv run --package neoson-gateway neoson-telegram # canal Telegram (long polling)
uv run --package neoson-robot neoson-robot "Olá, NeoSon"   # envia uma mensagem e mostra a resposta
```

O worker processa os eventos que o gateway grava junto com cada resposta (transactional outbox,
[ADR-002](docs/decisions/ADR-002-Persistencia.md)). A entrega é pelo menos uma vez: se o worker
cair, outro assume o evento quando o lease vence, e `worker_effects` garante que o efeito não se
repete. Falhas são refeitas com backoff exponencial e, esgotadas as tentativas, vão para
`dead_letter`. Os efeitos de hoje: registrar uso e custo de cada execução em `usage_records` e
atualizar o vault do Obsidian.

### Vault do Obsidian

O worker mantém em `NEOSON_VAULT_PATH` (padrão `data/NeoSon-Vault`) a nota diária
`Metricas/Diarias/AAAA-MM-DD.md` (YAML frontmatter com execuções, sucesso, tokens, cache e custo) e
os painéis `Dashboard/Home.md`, `Dashboard/LLM-Custos.md` e `Dashboard/NeoSon-System.md` (fila,
dead letters e atraso do worker). Só agregados: nenhum texto de conversa, prompt ou segredo vai
para o vault. A escrita é atômica, e um arquivo só é regravado quando os dados mudam. Para
regerar tudo a partir do banco, use `neoson-worker --export-vault`; para desligar a exportação,
`NEOSON_METRICS_EXPORT_ENABLED=false`. O custo usa os preços de `NEOSON_LLM_PRICES` (USD por
milhão de tokens, por modelo); sem preço, aparece como —.

O `neoson-robot` é o cliente da Raspberry Pi. Ele depende só de `neoson-contracts` e de um
cliente HTTP, sem nada do servidor; para instalar apenas ele:

```bash
uv sync --package neoson-robot --no-dev
```

O `neoson-telegram` conversa pelo Telegram com o mesmo motor e o mesmo banco do gateway. Ele só
responde em chat privado a usuários listados em `NEOSON_TELEGRAM_ALLOWED_USERS` (IDs numéricos)
e precisa de `NEOSON_TELEGRAM_BOT_TOKEN` (criado no @BotFather). Cada chat tem a sua sessão;
`/nova` começa outra conversa.

O gateway exige `NEOSON_API_TOKEN`, e o robô envia o mesmo token (`Authorization: Bearer`). Para
continuar uma conversa, use `--show-session` e depois `--session-id <id>`.

Endereço do gateway: `--gateway-url` > `NEOSON_GATEWAY_URL` > `http://127.0.0.1:8000`. O robô lê
só variáveis do processo (não lê o `neoson.env`). Saída 0 em sucesso; 1 se o gateway falhar, não
responder ou responder fora do contrato; 2 para mensagem, opção ou URL inválida.

## API

| Método e rota | Uso |
|---|---|
| `GET /api/v1/health/live` | Indica que o processo está ativo |
| `POST /api/v1/messages` | Recebe `{"text": "..."}` (e `session_id` opcional) e devolve a resposta e o `session_id`; exige `Authorization: Bearer <NEOSON_API_TOKEN>` |

A resposta vem do Agent Loop, pelo modelo configurado em `NEOSON_LLM_MODEL`. O modelo pode usar
ferramentas registradas; cada pedido é validado e passa pela Policy Engine, e ações sensíveis
suspendem a execução com `needs_approval` ([ADR-003](docs/decisions/ADR-003-Seguranca-Tools.md)).
Ferramentas disponíveis:

- `calendar_list`: agenda fake de demonstração.
- `memory_search`: busca em conversas anteriores (índice FTS5 no SQLite, sem diferenciar acentos)
  e devolve trechos com data e autor, para o modelo citar a fonte.
- `memory_save` e `memory_forget`: memória curada. Guardam fatos e preferências que o usuário pediu
  para lembrar, com a origem (sessão e data). Os fatos ativos entram no prompt de sistema, marcados
  como dados, e aparecem no vault em `Memoria/Usuario/` e `Memoria/NeoSon/` para revisão. Para
  corrigir, peça ao NeoSon que esqueça o fato pelo número.

Para desenvolver sem
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
- **Segredos:** `OPENAI_API_KEY` e `NEOSON_API_TOKEN` ficam ocultos em `repr`, logs e mensagens
  de erro.
- **Dados:** a conversa fica em `NEOSON_DB_PATH` (padrão `data/neoson.db`, fora do Git). O
  esquema é criado e migrado na inicialização ([ADR-002](docs/decisions/ADR-002-Persistencia.md)).
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
apps/worker/                 worker da outbox
tests/                       testes automatizados
docs/                        arquitetura, diagramas, decisões e inventário de hardware
```

O workspace e as regras de dependência entre pacotes estão descritos no
[ADR-001](docs/decisions/ADR-001-monolito-modular.md) e no
[ADR-004](docs/decisions/ADR-004-Packaging.md).

## Licença

[MIT](LICENSE).
