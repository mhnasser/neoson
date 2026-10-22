# ADR-017 — Adaptador Neo4j opcional para o grafo

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-002](ADR-002-Persistencia.md), [ADR-004](ADR-004-Packaging.md),
  [ADR-014](ADR-014-Knowledge-Graph.md); especificação §9, §18 e §20.

## Contexto

A especificação aponta o Neo4j como destino do grafo, mas recomenda adicioná-lo só
depois de medir, porque o notebook servidor tem 16 GB (§20). O ADR-014 começou pelo
SQLite com uma porta `GraphStore` pensada para outros adaptadores.

## Decisão

`memory/neo4j_graph.py` implementa a mesma porta no Neo4j:

- **Modelo.** Entidades são nós `NeosonEntity` e cada fato é um nó `NeosonFact`
  ligado por `SUBJECT` e `OBJECT`. Fato como nó permite restrição de unicidade por ID
  também na edição Community e guarda a proveniência no próprio fato. Restrições e
  índices são criados com `IF NOT EXISTS` antes da primeira consulta.
- **Consultas fixas.** Todas as consultas são constantes do módulo, com parâmetros;
  tipos, relações e nomes nunca entram no texto Cypher. O executor recusa qualquer
  consulta fora desse conjunto. A busca por nome usa `CONTAINS`, sem curingas.
- **Mesma semântica.** Upsert conta só os fatos criados na chamada; fato retirado não
  volta; transições de estado são as mesmas e a escrita confere o estado lido antes
  (outra escrita no meio faz a mudança não acontecer). Datas vão como texto ISO em
  UTC, como no SQLite.
- **Falhas.** Cada consulta tem prazo (`NEOSON_NEO4J_TIMEOUT_SECONDS`). Timeout e erros
  do driver viram `GraphBackendError` só com o tipo do erro, sem URI, usuário ou senha.
  Na `memory_search`, o grafo indisponível não derruba a busca no histórico: a
  resposta avisa que os fatos estão indisponíveis.
- **Dependência opcional.** O driver oficial (`neo4j>=6.4,<7`) é o extra
  `neoson-core[neo4j]`. Só é importado ao abrir o adaptador; a instalação padrão, o CI
  e a Pi não o instalam. Sem o driver, a configuração falha com instrução de instalação.
- **Seleção explícita.** `NEOSON_GRAPH_BACKEND=sqlite` continua padrão. Com `neo4j`,
  `NEOSON_NEO4J_URI` e `NEOSON_NEO4J_PASSWORD` são obrigatórios e ficam no
  `neoson.env`. Jobs, tentativas e revisão da extração continuam no SQLite; só os fatos
  e entidades vão para o Neo4j.

## Alternativas consideradas

- Relação nativa por tipo (`[:WORKS_ON]`): o tipo de relação não pode ser parâmetro
  no Cypher, então exigiria montar texto da consulta; e a unicidade por ID de relação
  não está disponível em todas as edições.
- Driver como dependência obrigatória: aumenta a instalação de quem usa só SQLite e da
  Pi, que não precisa do grafo.

## Consequências

O backup do SQLite não inclui o grafo quando o backend é Neo4j; é preciso o backup
do próprio servidor. A suíte de contrato roda contra o SQLite e contra um executor
simulado no CI. Contra um servidor real, roda só com opt-in e um servidor descartável.
Os avisos de rede do próprio driver podem citar o host nos logs locais. A instalação do
servidor depende do usuário (memória, edição e licença).
