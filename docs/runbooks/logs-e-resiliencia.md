# Logs e resiliência do modelo

## Logs

Gateway, bot do Telegram e worker usam a mesma configuração de logs.

```dotenv
NEOSON_LOG_LEVEL=INFO
# text (padrão, como antes) ou json (um objeto por linha)
NEOSON_LOG_FORMAT=json
# Opcional: também grava em arquivo, com rotação por tamanho
NEOSON_LOG_FILE=./data/logs/neoson.log
NEOSON_LOG_MAX_BYTES=5000000
NEOSON_LOG_BACKUPS=5
```

No formato `json`, cada linha traz `time` (UTC), `level`, `logger`, `process`
(`gateway`, `telegram` ou `worker`), `message` e, quando houver, `run_id`, `error` (tipo
da exceção) e `traceback`. Exemplo para filtrar uma execução:

```bash
grep '"run_id": "<id>"' data/logs/neoson.log
```

Os módulos registram IDs, estados e contagens, nunca conversas, transcrições ou
argumentos de ferramentas. Como segunda barreira, os dois formatos mascaram padrões
conhecidos de segredo (chaves `sk-`, `Bearer`, token de bot, `token=`/`senha=` em URLs).
As bibliotecas HTTP, o SDK da OpenAI e o driver Neo4j ficam em WARNING, porque registram
URLs. O arquivo de log é local e privado: não envie para tickets ou chats sem revisar.
Com mais de um processo, use um arquivo por processo (rotação simultânea no mesmo
arquivo não é segura).

## Circuit breaker do modelo

```dotenv
# Falhas temporárias seguidas que abrem o circuito (0 desliga)
NEOSON_LLM_CIRCUIT_FAILURES=5
NEOSON_LLM_CIRCUIT_RESET_SECONDS=30
```

Depois dessa quantidade de falhas temporárias seguidas do modelo (indisponibilidade,
limite de taxa, timeout), o circuito abre. Os pedidos seguintes falham na hora, sem
chamar o fornecedor e sem custo, com a resposta de "não consegui falar com o modelo
agora". Passado o tempo de espera, um único pedido de teste vai ao fornecedor: se der
certo, o circuito fecha; se falhar, reabre. Erros definitivos, como um pedido inválido,
não contam e zeram a sequência. O log registra a abertura (WARNING) e o fechamento
(INFO).

O circuito vale para o motor de conversas (gateway e bot), inclusive as análises
delegadas. Curadoria, extração do grafo e avaliação usam limites próprios de tentativa
e não passam por ele.
