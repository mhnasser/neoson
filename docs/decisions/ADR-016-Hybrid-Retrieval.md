# ADR-016 — Recuperação híbrida pela ferramenta `memory_search`

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-003](ADR-003-Seguranca-Tools.md),
  [ADR-007](ADR-007-Skills.md), [ADR-014](ADR-014-Knowledge-Graph.md),
  [ADR-015](ADR-015-Graph-Extraction.md); especificação §5.1, §9 (D10) e §14.

## Contexto

O Memory Retriever da especificação combina histórico, memória curada e grafo, e
entrega ao contexto só trechos relevantes, com referência à origem. O histórico já é
consultado pela ferramenta `memory_search` (FTS5). O grafo agora tem fatos revisados.

## Decisão

Com `NEOSON_MEMORY_GRAPH_ENABLED=true`, a própria `memory_search` passa a devolver,
além dos trechos de conversa, uma seção de fatos revisados:

- Termos da consulta com 3 ou mais caracteres, sem palavras frequentes, até 6, buscam
  até 5 entidades pelo nome.
- Entram só fatos `active` dessas entidades e que ainda estejam válidos. Pendentes e
  retirados nunca chegam ao modelo.
- Cada fato traz sujeito, relação, objeto, fonte (`memory_fact:<id>`), data observada e
  confiança, ordenado por confiança e data, com até 8 por chamada. A seção vem marcada
  como dados, não instruções.

Não há ferramenta nova. As skills continuam precisando declarar `memory_search` e o
catálogo validado (`TOOL_NAMES`) não muda. A política trata a busca como leitura, como
antes. Com o grafo ligado, a descrição da ferramenta menciona os fatos, e o
`runtime_hash` ganha uma chave. Com o grafo desligado, descrição, resultado e hash ficam
idênticos aos anteriores.

Os nomes das entidades vão ao modelo como qualquer trecho do histórico: ficam no
contexto da execução, não em eventos nem no vault.

## Alternativas consideradas

- Ferramenta separada `graph_search`: obrigaria o modelo a escolher entre duas buscas
  para a mesma pergunta e cada skill a declarar uma ferramenta nova.
- Injetar fatos no prompt de sistema em toda execução: aumenta o contexto sempre e
  desfaz a recuperação sob demanda do §5.1.

## Consequências

A busca por nome casa trechos (`LIKE`); nomes muito curtos ou genéricos podem trazer
fatos pouco relevantes, limitados a 8. A avaliação com e sem grafo mede esse efeito.
