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

## Backup

O grafo, os jobs e as tentativas ficam no mesmo SQLite e entram no backup normal. O
manifesto conta `graph_entities` e `graph_facts`. Restaurar um backup antigo também
restaura estados de revisão antigos.
