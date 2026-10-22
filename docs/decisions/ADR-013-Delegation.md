# ADR-013 — Delegação interna com contexto e orçamento isolados

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-003](ADR-003-Seguranca-Tools.md),
  [ADR-006](ADR-006-Import-Boundaries.md), [ADR-009](ADR-009-Router-Lab.md);
  especificação §5.1, §6, §14 e §15.

## Contexto

A especificação prevê subagentes com contexto menor e orçamento próprio, sem ganho de
privilégio por delegação e sem trocar o modelo principal da sessão. Uma delegação mal
limitada multiplica custo, repete chamadas, herda ferramentas ou vaza o histórico.
Falhas de rede e timeouts podem ter sido cobrados sem que o uso chegue ao NeoSon.

## Decisão

`DelegationManager` (em `engine/delegation.py`) executa uma **análise delegada**:
uma chamada ao modelo, sem ferramentas, que recebe apenas `task` (até 1.000
caracteres) e `context` (até 8.000), escolhidos pelo modelo do pai. O filho não recebe
histórico, memória curada, índice de skills, instruções de skill nem credenciais. Um
prompt de sistema fixo diz que o contexto é dado sem autoridade. A resposta volta ao
pai como resultado de ferramenta, marcado como dado, com até 4.000 caracteres.

Limites, todos por execução do pai:

- **Profundidade 1.** O filho recebe `tools=()`; qualquer pedido de ferramenta na
  resposta (inclusive `delegate`, aprovação ou publicação) é recusado. Não há
  delegação recursiva.
- **Tentativas.** `NEOSON_MAX_SUBAGENTS` conta tentativas, bem-sucedidas ou não. O
  gerenciador nunca repete uma chamada; o pai pode pedir outra enquanto houver vaga.
  As retentativas de transporte do SDK do fornecedor continuam valendo.
- **Prazo.** `NEOSON_DELEGATION_TIMEOUT_SECONDS`, limitado pelo prazo restante do pai.
  Timeout cancela a chamada do filho.
- **Tokens.** `NEOSON_DELEGATION_MAX_TOKENS` soma todas as delegações da execução,
  também limitado por `Budget.max_tokens` do pai quando houver. Antes de chamar, a
  entrada é estimada pelo número de bytes UTF-8 mais uma margem; como um token tem
  pelo menos um byte, a estimativa não fica abaixo do real. O teto de saída
  (`NEOSON_DELEGATION_MAX_OUTPUT_TOKENS`) é reduzido para caber no saldo. Uso
  informado acima do saldo é contabilizado e encerra a execução.
- **Perfil e modelo.** O filho usa o mesmo `model_profile` do pai. Resposta de outro
  modelo é contabilizada e encerra a execução.

Permissões nunca aumentam. A Policy Engine avalia `delegate` como leitura. No modo
`always`, em que leitura exige aprovação, a delegação é negada sem criar ação
retomável: ela não tem efeito externo para aprovar. Com uma skill ativa, só valem as
ferramentas declaradas pela skill, e nesta versão skills não podem declarar
`delegate` (o loader recusa o nome), então a delegação fica indisponível. O filho não
aprova ações nem publica código.

A delegação fica desligada por padrão (`NEOSON_DELEGATION_ENABLED=false`). Ligada, o
`runtime_hash` passa a incluir os limites, o que invalida aprovações e vínculos de
sessão criados com outra configuração. Desligada, o hash não muda.

## Contabilidade

Toda chamada entra na conta, inclusive as que falham:

- Uso informado, inclusive em `LLMError.usage`, é somado ao pai e ao ledger da
  delegação, que separa o uso por modelo.
- `LLMError.usage=None` significa uso desconhecido. `Usage()` significa que nada foi
  enviado, como perfil sem modelo. O adaptador OpenAI anexa o uso medido quando a
  resposta chega mas não traz conteúdo útil.
- Timeout, cancelamento durante uma chamada, uso inválido, tokens sem modelo ou mais
  de um modelo na mesma execução marcam `usage_complete=false`. Depois de um uso
  desconhecido, o gerenciador não faz novas chamadas na mesma execução.
- Cancelamento externo do pai grava a execução como `failed` (estado `cancelled`), com
  o uso já medido, e depois repassa o `CancelledError`. Um segundo cancelamento durante
  essa gravação pode perdê-la. A gravação não roda protegida em segundo plano.

O evento de fim de execução leva `usage_complete` e contadores da delegação
(tentativas, concluídas, falhas, recusadas e tokens), nunca texto. O worker grava
`usage_records.usage_complete` e `delegation_records` (migração 15). Custo estimado
com uso incompleto fica `NULL`, não um valor abaixo do real. A nota diária do vault
mostra execuções com uso incompleto, delegações e tokens delegados.

## Alternativas consideradas

- Subagente com cópia do catálogo de ferramentas: amplia o raio de efeito e exigiria
  aprovações delegadas; adiado até haver caso de uso com permissões menores.
- Retry automático do filho: duplica custo em falhas ambíguas; a decisão volta ao pai,
  dentro do limite de tentativas.
- Custo zero para timeout: subestima gasto; usar `NULL` e sinalizar incompleto.
- Modelo auxiliar mais barato para o filho: possível no futuro com binding explícito
  e precificação por modelo; hoje quebraria o custo de modelo único por execução.
- Rota `delegate` do Router Lab acionar subagentes: a rota continua sendo rótulo de
  avaliação; a delegação depende do opt-in e do pedido explícito do modelo.

## Consequências

O pedido do filho custa pelo menos uma chamada extra por delegação. A estimativa em
bytes pode recusar contextos grandes com orçamento pequeno; não promete custo exato
quando a chamada termina por timeout. Os testes usam provedores determinísticos. O
teste ao vivo opcional compara o uso real com a estimativa. Operação no
[runbook](../runbooks/delegation.md).
