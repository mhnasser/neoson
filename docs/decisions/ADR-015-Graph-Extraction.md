# ADR-015 — Extração de fatos para o grafo com revisão explícita

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-008](ADR-008-Propostas-Skills.md),
  [ADR-014](ADR-014-Knowledge-Graph.md); especificação §8, §9.1–9.3, §10 e §14.

## Contexto

O grafo do ADR-014 precisa de fatos com origem. Um modelo pode extrair relações de
texto, mas pode errar, inventar ou obedecer a instruções escondidas no conteúdo. A
memória incorreta é um risco conhecido da especificação (§20): proveniência, estado
pendente, contradições e revisão.

## Decisão

A primeira fonte é a memória curada: fatos e preferências que o usuário pediu para
lembrar, já com sessão e data. Trechos do histórico de conversa ficam fora desta
decisão; entram depois só com seleção explícita.

- **Agendamento.** Com `NEOSON_MEMORY_GRAPH_ENABLED=true`, o worker trata
  `neoson.memory.updated`: memória salva gera um job `extract`; memória esquecida torna
  o `extract` pendente obsoleto e gera um job `retract`. Há um job por fonte e tipo, e o
  agendador nunca chama o modelo.
- **Execução.** `neoson-graph run` é um processo separado, como a curadoria. Retiradas
  vêm primeiro e não usam o modelo. A extração usa lease, número máximo de tentativas
  com espera crescente, prazo, e limites diários de jobs, tokens reservados e, opcional,
  custo. Reservas e tentativas canceladas continuam contando no orçamento. A chamada
  não tem ferramentas, e o texto da memória vai como dado JSON na mensagem do usuário,
  separado do prompt de sistema fixo.
- **Validação.** A resposta precisa ser `{"facts": [...]}` e cada item precisa ter
  exatamente sujeito, relação, objeto e confiança, com tipos e relações do ADR-014.
  Itens inválidos, repetidos, laços ou acima do limite são descartados. Cada descarte
  conta um código de motivo na tentativa, sem guardar o texto. Resposta fora do
  contrato, ou com pedido de ferramenta, falha sem nova chamada.
- **Proveniência e estado.** Cada fato recebe `source_id = memory_fact:<id>`, a data da
  memória como `observed_at` e a confiança informada. O fato sempre entra como
  `pending`, e o modelo não tem como pedir outro estado. Se a memória for esquecida
  durante a chamada, nada é gravado.
- **Revisão.** `neoson-graph pending` lista os pendentes no terminal local e marca se o
  mesmo trio (sujeito, relação, objeto) já foi confirmado ou rejeitado em outra fonte.
  `activate` e `retract --reason` decidem por ID. Fatos ativos nunca são sobrescritos:
  versões de outras fontes convivem como fatos separados até a revisão.
- **Esquecimento.** O job `retract` retira, pela porta `GraphStore`, todos os fatos
  pendentes ou ativos da fonte, com o motivo "fonte esquecida". Funciona com qualquer
  adaptador da porta.

O uso medido e o custo estimado de cada tentativa ficam em `graph_attempts` (migração
17). O uso informado em erros também entra; timeout fica sem medição. Jobs e tentativas
não copiam o texto da memória.

## Alternativas consideradas

- Ativar automaticamente acima de uma confiança: a confiança vem do próprio modelo e
  não é evidência. Fica para depois, com regra explícita e avaliação.
- Extrair dentro do worker da outbox: prenderia a fila durante chamadas de rede; o
  executor separado segue o padrão da curadoria.
- Apagar fatos ao esquecer a memória: perde auditoria e permitiria reintroduzi-los em
  silêncio; retirar preserva o histórico.

## Consequências

Um processo a mais quando o grafo está ligado, e uma revisão manual antes de qualquer
uso. Um executor por banco: dois executores simultâneos podem gravar uma extração
antes da retirada da mesma fonte. A retirada seguinte corrige isso. Operação no
[runbook](../runbooks/knowledge-graph.md).
