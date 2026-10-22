# NeoSon

Assistente pessoal de IA com interface física, motor Python próprio, memória, skills e melhoria
contínua. A especificação técnica e os diagramas estão em [`docs/architecture/`](docs/architecture/);
as decisões de arquitetura, em [`docs/decisions/`](docs/decisions/).

> **Estado:** fundação pronta (workspace, configuração, CI) e primeira versão do motor. O cliente
> `neoson-robot` envia mensagens ao gateway em `POST /api/v1/messages`, e o Agent Loop responde
> pelo modelo configurado (OpenAI), com roteador por regras, limites de passos, tempo e tokens, e
> ferramentas autorizadas por uma Policy Engine, com integrações opcionais de leitura. A API
> exige token, a conversa fica gravada em SQLite (sobrevive a reinícios) e o worker consome os
> eventos da outbox com lease, retry e idempotência. Há busca no histórico, memória curada com
> fonte, painéis no Obsidian e skills aprovadas com versões e avaliação por fixtures.

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

`Dashboard/Skills-Learning.md` mostra o histórico por versão das skills: estado, execuções,
sucesso, tokens, custo conhecido e última avaliação. Cada skill ativa tem uma nota com
frontmatter em `Skills/Ativas/<id>.md`; rollback atualiza essa nota e retirada remove a nota
gerada, preservando o histórico no painel. Aprovações, rollback, retirada e avaliações
atualizam o painel pelo worker, assim como os resultados das execuções. A última avaliação
corresponde à mesma versão e checksum; seus tokens e custos ficam separados do uso normal.
O custo conhecido pode ser parcial: o painel também informa quantas execuções não têm custo.
Sem skills aprovadas, mostra o catálogo vazio. Nenhuma skill é aprovada pela exportação.

O mesmo painel reúne propostas por estado e as últimas comparações base/candidata,
incluindo regressões por caso. `Skills/Propostas/<uuid>.md` preserva cada proposta com
tokens, custo, duração e indicação de candidata ativa. Depois de rollback, a aprovação
continua no histórico, mas a nota indica que a candidata deixou de estar ativa. Conversas,
instruções geradas e motivos textuais dos testes não são exportados. Veja o
[fluxo de aprendizado e revisão](docs/runbooks/revisao-skills.md).

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

O Robot possui uma [demonstração de voz e estados](docs/runbooks/robot-voice.md) com
captura/playback/display fake, ativação explícita e progresso recebido do servidor.
Ela preserva apenas a sessão entre reinícios e não abre dispositivos reais.

O gateway também possui um [pipeline de voz](docs/runbooks/voice.md) com portas STT/TTS
substituíveis e entrada WAV autenticada/limitada. A rota `/api/v1/voice` vem desativada;
o modo fake, restrito a echo fora de produção, valida o fluxo com texto constante e
silêncio, sem comprovar reconhecimento ou fala reais. Decisão no [ADR-010](docs/decisions/ADR-010-Voice.md).

| Método e rota | Uso |
|---|---|
| `GET /api/v1/health/live` | Indica que o processo está ativo |
| `POST /api/v1/messages` | Recebe `{"text": "..."}` (e `session_id` opcional) e devolve a resposta e o `session_id`; exige `Authorization: Bearer <NEOSON_API_TOKEN>` |
| `GET /api/v1/actions` | Lista as ações ainda pendentes ou aprovadas sem execução, inclusive de pedidos por voz |
| `GET /api/v1/runs/{run_id}/actions` | Lista ações pendentes da execução para revisão autenticada |
| `GET /api/v1/actions/{id}` | Consulta a ação concreta e seu estado |
| `POST /api/v1/actions/{id}/decision` | Aprova ou nega com `{"approve": true/false}` |
| `POST /api/v1/actions/{id}/resume` | Executa somente a ação aprovada e ainda válida |

As rotas de ações exigem o mesmo bearer e estão disponíveis com o motor configurado
para OpenAI. Aprovações sobrevivem a reinícios e não autorizam outros argumentos ou
execuções. Falhas com efeito incerto bloqueiam repetição automática. Veja o
[procedimento de revisão e retomada](docs/runbooks/action-approvals.md).

A resposta vem do Agent Loop, pelo modelo configurado em `NEOSON_LLM_MODEL`. O modelo pode usar
ferramentas registradas; cada pedido é validado e passa pela Policy Engine, e ações sensíveis
suspendem a execução com `needs_approval` ([ADR-003](docs/decisions/ADR-003-Seguranca-Tools.md)).
Ferramentas disponíveis:

- `calendar_list` e `email_list`: leitura de agenda e metadados do inbox, desativada por padrão.
  Fornecedor fake explícito ou Microsoft Graph com OAuth mínimo; consulte o
  [runbook de integrações pessoais](docs/runbooks/personal-integrations.md).
- `calendar_create` e `email_draft`: compromisso sem convidados ou rascunho sem envio,
  com aprovação persistente por ação e destino separado, desativado por padrão.
  Veja o [runbook de escrita limitada](docs/runbooks/personal-writes.md).
- `delegate`: análise delegada a uma chamada sem ferramentas, só com a tarefa e o trecho
  escolhidos, com tentativas, prazo e tokens limitados por execução e custo contabilizado
  inclusive em falhas. Desativada por padrão; veja o [runbook](docs/runbooks/delegation.md)
  e o [ADR-013](docs/decisions/ADR-013-Delegation.md).
- `memory_search`: busca em conversas anteriores (índice FTS5 no SQLite, sem diferenciar acentos)
  e devolve trechos com data e autor, para o modelo citar a fonte. Com o grafo de conhecimento
  ligado, acrescenta fatos revisados com fonte e data; veja o
  [runbook do grafo](docs/runbooks/knowledge-graph.md).
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

### Backup e restauração

```bash
uv run --package neoson-worker neoson-backup create          # banco + vault + manifest.json
uv run --package neoson-worker neoson-backup verify <pasta>  # integridade e hashes
uv run --package neoson-worker neoson-backup restore <pasta> # pare gateway e worker antes
```

O banco é copiado pela API de backup do SQLite (consistente mesmo em uso). A restauração
verifica o backup antes e, com `--force`, guarda os dados atuais de lado em vez de apagá-los.
O `neoson.env` não entra no backup. Passo a passo em
[`docs/runbooks/backup-restauracao.md`](docs/runbooks/backup-restauracao.md).

### Skills

Procedimentos versionados em [`skill_library/`](skill_library/), um por pasta: `SKILL.md`
(quando usar, passos, restrições e saída) e `metadata.yaml` (versão, gatilhos, ferramentas
permitidas e risco). Só arquivos de texto; scripts de skill ainda não são aceitos.

```bash
uv run --package neoson-gateway neoson-skills validate            # toda a biblioteca
uv run --package neoson-gateway neoson-skills validate <pasta>    # uma skill
uv run --package neoson-gateway neoson-skills approve <pasta>     # aprova e ativa a versão
uv run --package neoson-gateway neoson-skills list                # versões e estados
uv run --package neoson-gateway neoson-skills rollback <id>       # aprovação anterior
uv run --package neoson-gateway neoson-skills retire <id>         # retira a versão ativa
uv run --package neoson-gateway neoson-skills eval <id>           # avalia a versão aprovada
```

Validar não aprova. A aprovação explícita guarda no SQLite a cópia integral da skill e seu
checksum, junto com um evento na outbox. Editar a pasta depois não altera a versão aprovada;
aprovar conteúdo diferente exige um número de versão novo. Só uma versão por skill fica ativa.
O rollback volta à aprovação anterior disponível (ordem de aprovação), e a retirada desativa
a versão sem apagar o histórico. O registro confere a integridade da cópia antes de carregá-la.

O modelo recebe um índice das skills ativas e pode carregar uma com a ação `skill_load`.
O procedimento aprovado entra como instrução apenas na execução atual. Depois do carregamento,
só as ferramentas declaradas pela skill ficam disponíveis; a política de segurança continua
valendo, e `requires_approval` exige aprovação também das leituras. Cada execução registra a
skill e sua versão na mesma transação da conversa e do evento de conclusão.

As fixtures YAML em `tests/fixtures/` definem a entrada, o relógio, respostas simuladas de
ferramentas e critérios objetivos. `eval` usa as fixtures do snapshot aprovado, o provedor
configurado e um banco temporário por caso. As ferramentas reais fornecem seus schemas e
riscos, mas sua implementação não é executada. Só as medições ficam no banco configurado:
casos aprovados/reprovados, motivos, tokens, modelo, latência e custo estimado. Sem preços
configurados, o custo fica desconhecido. Saída 0 quando todos os casos passam e 1 se algum
falhar. Avaliar não aprova nem altera a versão ativa.

O formato e a governança estão no [ADR-007](docs/decisions/ADR-007-Skills.md).

O armazenamento de propostas de melhoria fica separado do catálogo aprovado e preserva a
versão de origem e suas evidências ([ADR-008](docs/decisions/ADR-008-Propostas-Skills.md)).
O executor opcional `neoson-curator` gera candidatos a partir de falhas de skills ou
avaliações reprovadas. A CLI `neoson-skills proposal` permite inspecionar, comparar,
rejeitar e aprovar explicitamente uma proposta. A aprovação exige evidência válida da
comparação atual, sem regressões e com todos os casos aprovados; ela preserva rollback.
Custos desconhecidos e aumentos de recursos exigem reconhecimento explícito. Veja o
[runbook de revisão](docs/runbooks/revisao-skills.md) para os comandos e bloqueios.

### Curadoria de skills

Desativada por padrão (`NEOSON_CURATION_ENABLED=false`). Para experimentar, configure essa
variável como `true` no ambiente do worker e do executor e execute:

```bash
uv run --package neoson-worker neoson-worker --once   # agenda sinais elegíveis da outbox
uv run --package neoson-worker neoson-curator --once  # tenta um job, sem ativar a proposta
```

Sem `--once`, os dois processos permanecem ativos. A conversa não espera a curadoria;
o worker só agenda e o executor chama o modelo fora das transações do SQLite. Não são
enviadas transcrições de conversas: o modelo recebe o procedimento e um sinal de falha
ou um resumo limitado da avaliação, tratados como dados não confiáveis. Não há ferramentas
disponíveis nessa chamada, e candidatos só podem mudar o procedimento e a versão.

Os padrões são cinco jobs por dia UTC, três tentativas por job, 30 segundos por chamada,
12 mil caracteres de entrada, três mil tokens de saída e 60 mil tokens reservados por dia.
As reservas usam bytes UTF-8 da entrada, margem de protocolo e limite de saída; tentativas
interrompidas também consomem reserva. Apenas um job por skill fica pendente/em execução;
sinais adicionais são agrupados, e sinais além do teto diário não criam jobs.

`NEOSON_CURATION_MAX_DAILY_COST_USD` permite um teto estimado adicional. Ele exige preços
configurados em `NEOSON_LLM_PRICES`; sem preço, a execução fica pendente com `price_missing`.
Custos/reservas desconhecidos anteriores também impedem ultrapassar esse teto. Sem teto em
USD, os limites de volume, tokens e tempo continuam valendo, e custo desconhecido fica vazio.
Reservas persistem após reinício. Lease expirado pode ser retomado; resposta atrasada não
publica resultados. Erros são registrados por código, sem respostas ou mensagens privadas
do fornecedor. Esta opção permanece desativada até ser configurada explicitamente.

### Router Lab

O painel `Dashboard/Router-Lab.md` no Obsidian apresenta comparações, qualidade,
custo/cache, matrizes de confusão e histórico de promoção/rollback, sem textos privados.
Configure `NEOSON_ROUTER_LAB_DB_PATH` para o banco do benchmark; veja a
[atualização por eventos e reconstrução](docs/runbooks/router-lab.md#painel-no-obsidian).

O laboratório começa com um [dataset público sintético](datasets/router/README.md):
24 casos rotulados, separados em treino, validação e teste. O loader valida contratos,
recusa duplicatas entre splits e produz um hash reproduzível. Rótulos de roteamento e
expectativas de qualidade/segurança ficam separados. Baseline, métricas e critérios de
promoção estão no [ADR-009](docs/decisions/ADR-009-Router-Lab.md). A CLI
`python -m neoson_gateway.router_lab` compara regras com um perfil fixo, usando bancos
descartáveis e ferramentas simuladas. O relatório separa qualidade, acurácia e custo;
tentativas sem consumo conhecido não viram custo zero. Veja o
[runbook](docs/runbooks/router-lab.md) para executar sem rede ou com API e orçamento
explícitos. Benchmarks não alteram a política ativa.

Políticas podem ser aprovadas explicitamente pela CLI `python -m neoson_gateway.router_policy`,
com evidência de teste válida, contexto atual e limites de qualidade/segurança/recursos.
Snapshots e auditoria preservam rollback. Sessões mantêm o snapshot e o modelo vinculados
no primeiro turno; novas aprovações valem para novas sessões. Veja o
[runbook de revisão](docs/runbooks/routing-policies.md).

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
skill_library/               skills versionadas (SKILL.md + metadata.yaml)
tests/                       testes automatizados
docs/                        arquitetura, diagramas, decisões e inventário de hardware
```

O workspace e as regras de dependência entre pacotes estão descritos no
[ADR-001](docs/decisions/ADR-001-monolito-modular.md) e no
[ADR-004](docs/decisions/ADR-004-Packaging.md).

## Licença

[MIT](LICENSE).
