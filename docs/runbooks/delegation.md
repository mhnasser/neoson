# Análise delegada (subagente sem ferramentas)

Com a delegação ligada, o modelo pode chamar `delegate` com uma tarefa curta e um
trecho de contexto. O NeoSon faz uma chamada separada ao mesmo perfil de modelo, sem
ferramentas, histórico, memória ou skills, e devolve a resposta ao pai como dado. A
decisão está no [ADR-013](../decisions/ADR-013-Delegation.md).

## Ligar

O padrão é desligado. Para ligar, configure em `neoson.env`:

```dotenv
NEOSON_DELEGATION_ENABLED=true
# Tentativas por execução, contando falhas (>= 1)
NEOSON_MAX_SUBAGENTS=2
NEOSON_DELEGATION_TIMEOUT_SECONDS=15
# Soma de todas as delegações de uma execução
NEOSON_DELEGATION_MAX_TOKENS=16000
# Teto de saída de cada delegação, menor que o total
NEOSON_DELEGATION_MAX_OUTPUT_TOKENS=1000
```

Reinicie o gateway e o bot do Telegram, que leem a configuração na partida. Antes de
reiniciar, confira o processo ativo para não deixar dois pollings do Telegram. Ligar,
desligar ou mudar limites com a delegação ligada altera o hash de runtime. Com isso,
ações pendentes e vínculos de sessão criados antes da mudança deixam de valer e
precisam de novo pedido. Com a delegação desligada, os limites não entram no hash.

Com `NEOSON_APPROVAL_MODE=always`, a delegação é sempre negada: ela não tem efeito
externo para aprovar e não gera ação pendente. Quando uma skill está ativa, a
delegação também fica indisponível, porque skills não podem declarar `delegate`.

## Custos

Cada delegação é uma chamada extra ao modelo. O uso informado pelo fornecedor, mesmo
em falhas, é somado ao da execução. Quando parte do uso não é conhecida (timeout,
cancelamento, erro sem medição ou resposta de outro modelo), o registro de uso fica
com `usage_complete = 0` e custo `NULL`. Isso não significa custo zero. Após um uso
desconhecido, a execução não faz novas delegações.

A estimativa prévia de entrada conta bytes UTF-8 e fica acima do uso real. Por isso,
um contexto grande pode ser recusado com orçamento pequeno. Nesse caso, aumente
`NEOSON_DELEGATION_MAX_TOKENS` ou peça contexto menor.

## Métricas

O worker grava, sem texto de conversa:

- `usage_records.usage_complete`: 1 quando todo o uso da execução foi medido.
- `delegation_records`: tentativas, concluídas, falhas, recusadas e tokens das
  delegações de cada execução.

A nota diária do vault (`Metricas/Diarias`) mostra execuções com uso incompleto,
delegações (tentativas / falhas / recusadas) e tokens de delegação. Para consultar
direto no banco:

```sql
SELECT r.started_at, d.attempts, d.failed, d.rejected,
       d.input_tokens + d.output_tokens AS tokens, u.usage_complete, u.cost_usd
FROM delegation_records d
JOIN runs r ON r.id = d.run_id
LEFT JOIN usage_records u ON u.run_id = d.run_id
ORDER BY r.started_at DESC LIMIT 20;
```

## Atualizar uma instalação

A migração 15 roda automaticamente na abertura do banco. Faça backup do SQLite e do
vault antes, como nas demais atualizações. Execuções antigas ficam com
`usage_complete = 1`, pois eventos anteriores não traziam o campo.

## Diagnóstico

- `delegate` não aparece para o modelo: confira `NEOSON_DELEGATION_ENABLED`, se o
  gateway foi reiniciado e se não há skill ativa.
- Erro de configuração na partida: `NEOSON_MAX_SUBAGENTS` precisa ser >= 1, e o teto
  de saída precisa ser menor que o total de tokens.
- Resposta "Não consegui concluir a análise delegada com segurança": houve timeout, uso
  desconhecido, uso acima do saldo ou modelo divergente. A execução foi encerrada e o
  uso conhecido ficou registrado.
- Resultado de delegação com `attempts`, `tokens`, `deadline` ou `depth`: a tentativa
  foi recusada antes de chamar o modelo, sem custo.
- Resultado com `provider`, `tools` ou `output`: a chamada aconteceu e o uso medido
  entrou na conta. O filho falhou, pediu ferramenta ou passou do tamanho de resposta.

O teste ao vivo opcional usa a chave local e duas chamadas curtas:

```bash
NEOSON_LIVE_TESTS=1 uv run --no-sync python -m pytest -q tests/test_openai_live.py -k delegation
```
