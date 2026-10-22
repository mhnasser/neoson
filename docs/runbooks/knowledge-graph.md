# Grafo de conhecimento: extração e revisão

O grafo guarda entidades e relações com a fonte de cada fato
([ADR-014](../decisions/ADR-014-Knowledge-Graph.md)). Nesta versão a fonte é a memória
curada: o que você pediu para o NeoSon lembrar. Um modelo propõe as relações, e nada é
usado antes da sua revisão ([ADR-015](../decisions/ADR-015-Graph-Extraction.md)).

## Ligar

O padrão é desligado. Antes de ligar numa instalação existente, faça
`neoson-backup create`: as migrações 16 e 17 rodam na abertura do banco.

```dotenv
NEOSON_MEMORY_GRAPH_ENABLED=true
NEOSON_GRAPH_EXTRACTION_MAX_DAILY_JOBS=20
NEOSON_GRAPH_EXTRACTION_MAX_DAILY_TOKENS=40000
# Opcional, exige NEOSON_LLM_PRICES para o modelo:
# NEOSON_GRAPH_EXTRACTION_MAX_DAILY_COST_USD=0.10
```

Reinicie o worker da outbox para ele enfileirar as memórias novas. Memórias salvas antes
de ligar não entram automaticamente. A extração usa a mesma chave e o mesmo modelo
(`NEOSON_LLM_MODEL`) do servidor. Rode o executor separado:

```bash
uv run --no-sync neoson-graph run          # contínuo
uv run --no-sync neoson-graph run --once   # um job e sai
```

Se o comando ainda não estiver instalado no ambiente, use
`uv run --no-sync python -m neoson_worker.graph_main run --once`. Use um único executor
por banco.

## Revisar

Os fatos extraídos ficam pendentes e não são usados em nenhuma resposta.

```bash
uv run --no-sync neoson-graph pending --limit 20
uv run --no-sync neoson-graph activate <id> [<id> ...]
uv run --no-sync neoson-graph retract <id> [<id> ...] --reason "incorreto"
```

A lista mostra nomes de pessoas e projetos: rode só no seu terminal e não copie a saída
para tickets, chats ou logs. Cada linha mostra o ID, o trio (sujeito, relação, objeto),
a confiança proposta pelo modelo e a fonte (`memory_fact:<id>`). "já confirmado" ou "já
rejeitado" indicam que o mesmo trio já foi decidido a partir de outra fonte. A confiança
é a opinião do modelo e não serve como prova; confira a memória de origem quando tiver
dúvida.

Um fato retirado não volta. Ativar não muda outros fatos: versões de fontes diferentes
continuam separadas.

## Usar nas respostas

Com o grafo ligado, a ferramenta `memory_search` acrescenta aos trechos de conversa os
fatos **ativos** das pessoas, projetos e temas citados na pergunta, com fonte, data e
confiança ([ADR-016](../decisions/ADR-016-Hybrid-Retrieval.md)). Pendentes, retirados e
fatos com validade vencida não aparecem. Reinicie o gateway e o bot depois de ligar: a
descrição da ferramenta muda e o hash de runtime também, o que invalida ações pendentes
criadas antes.

## Esquecer

Quando uma memória é esquecida (`memory_forget`), o worker cria um job de retirada. O
executor retira todos os fatos pendentes ou ativos daquela fonte, com o motivo "fonte
esquecida". Retiradas rodam antes das extrações e não chamam o modelo.

## Custos e falhas

Cada chamada reserva tokens antes de começar: os bytes da entrada, uma margem e a saída
máxima. Reservas, falhas e cancelamentos contam no limite do dia. Com teto de custo, a
extração fica parada até haver preço do modelo. Em `graph_attempts` ficam os tokens
medidos, o custo estimado, o código de erro e a contagem de itens descartados por
motivo, sem o texto da memória nem a mensagem do fornecedor.

| `last_error` em `graph_jobs` | Significado |
|---|---|
| `daily_job_limit`, `daily_token_budget`, `daily_cost_budget`, `price_missing` | Adiado; tenta de novo no próximo dia ou com preço configurado |
| `provider_error`, `timeout` | Falha temporária; espera crescente até o máximo de tentativas |
| `invalid_output` | Resposta fora do formato ou com pedido de ferramenta; sem nova chamada |
| `input_limit` | Memória longa demais para o limite de entrada |
| `source_forgotten` | A memória foi esquecida antes do fim; nada foi gravado |
| `attempt_limit` | Tentativas esgotadas |

## Avaliar com e sem grafo

`neoson-graph eval` compara a busca só no histórico com a recuperação híbrida, nos
mesmos casos do [dataset sintético](../../datasets/graph_retrieval/README.md). Um banco
temporário é semeado a partir do dataset; o banco real não participa.

```bash
uv run --no-sync python -m neoson_worker.graph_main eval
uv run --no-sync python -m neoson_worker.graph_main eval --live --max-cases 4
```

O relatório completo vai para `data/graph-eval/graph-eval-<data>.json` (`--output`
muda a pasta). O painel `Dashboard/Grafo-Avaliacao.md` do vault mostra só os agregados
e os IDs de caso.

| Métrica | Definição |
|---|---|
| Recall de fontes | Média, nos casos com fonte esperada, da fração de fontes esperadas que a busca trouxe |
| Casos com todas as fontes | Fração dos casos com fonte esperada em que todas vieram |
| Casos com fonte pendente/retirada | Fração dos casos que trouxeram uma fonte proibida; deve ser 0 |
| Fontes não esperadas por caso | Média de fontes trazidas além das esperadas (ruído) |
| Contexto médio | Tamanho médio do resultado entregue ao modelo, em caracteres |
| Respostas com as palavras esperadas | Só em `--live`: fração das respostas do modelo que contêm todas as palavras do caso |
| Tokens e custo | Só em `--live`: soma das execuções do braço; custo vazio sem preço configurado |

O modo padrão é determinístico: sem modelo, sem custo, e o mesmo dataset gera o mesmo
resultado. `--live` faz, por caso e por braço, uma execução do Agent Loop com o modelo
real (em geral duas chamadas); use `--max-cases` para limitar o gasto. Os dois braços
usam as mesmas regras, casos e limites; só o grafo muda.

Limites: o dataset é pequeno e sintético. Ele mede a mecânica da recuperação, não a
qualidade com os seus dados. Conferir palavras na resposta é um critério mínimo, não
uma avaliação humana. Ganho em recall vem com mais contexto e mais fontes não pedidas,
e a tabela mostra os dois lados.

## Neo4j (opcional)

O padrão guarda o grafo no próprio SQLite. Para usar um servidor Neo4j
([ADR-017](../decisions/ADR-017-Neo4j-Adapter.md)):

1. Instale e opere o servidor por conta própria, na rede confiável, com usuário e senha
   próprios. Confira a memória disponível no notebook antes.
2. Instale o driver no ambiente do servidor NeoSon, com o gateway, o bot e o worker
   parados (no Windows, executáveis em uso impedem a troca):

   ```bash
   uv sync --all-packages --extra neo4j
   ```

3. Configure em `neoson.env`:

   ```dotenv
   NEOSON_GRAPH_BACKEND=neo4j
   NEOSON_NEO4J_URI=bolt://neo4j-host.local:7687
   NEOSON_NEO4J_USER=neo4j
   NEOSON_NEO4J_PASSWORD=<senha>
   ```

4. Valide com um servidor **descartável** antes de usar o real: a suíte grava nós de
   teste.

   ```bash
   NEOSON_NEO4J_TESTS=1 NEOSON_NEO4J_URI=... NEOSON_NEO4J_PASSWORD=... \
     uv run --no-sync python -m pytest -q tests/test_graph_contract.py
   ```

Restrições e índices são criados na primeira consulta. Fatos já gravados no SQLite não
são copiados para o Neo4j: a troca começa um grafo vazio, e as memórias antigas não são
reextraídas automaticamente. Jobs, tentativas e revisão continuam no SQLite. Os avisos
de rede do driver podem citar o host nos logs locais.

## Backup

Com o backend SQLite, o grafo, os jobs e as tentativas ficam no mesmo banco e entram no
backup normal. O manifesto conta `graph_entities` e `graph_facts`. Com Neo4j, faça
também o backup do servidor (por exemplo `neo4j-admin database dump`); o backup do
NeoSon não o inclui. Restaurar um backup antigo também restaura estados de revisão
antigos.
