# ADR-014 — Grafo de conhecimento com proveniência, primeiro em SQLite

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-001](ADR-001-monolito-modular.md),
  [ADR-002](ADR-002-Persistencia.md), [ADR-006](ADR-006-Import-Boundaries.md);
  especificação §9, §9.1, §9.2 e §20.

## Contexto

A especificação prevê um grafo de entidades e relações com proveniência, sem substituir
o histórico bruto do SQLite. Toda relação factual deve guardar origem, momento da
observação, confiança e, quando houver, validade. IDs estáveis devem impedir
duplicação. O notebook servidor tem 16 GB e a especificação recomenda adicionar o
Neo4j só depois de medir (§20).

## Decisão

O motor define o domínio e a porta `GraphStore` em `engine/graph.py`, só com a
biblioteca padrão:

- **Entidades** dos tipos do §9.2 (`Person`, `Project`, `Document`, `Concept`,
  `Decision`, `Task`, `Event`, `Skill`, `Source`). O nome é normalizado (Unicode NFKC e
  espaços colapsados). O ID é o SHA-256 canônico de tipo e nome sem caixa, então grafias
  com outra caixa ou espaçamento caem na mesma entidade. O primeiro nome observado vira o
  de exibição.
- **Relações** do §9.2 (`WORKS_ON`, `MENTIONS`, `DECIDED`, `DEPENDS_ON`, `DERIVED_FROM`,
  `RELATES_TO`, `USES_SKILL`). Tipos fora da lista são recusados.
- **Fatos** ligam duas entidades diferentes por uma relação e exigem `Provenance`:
  `source_id` no formato `<tipo>:<id>` (por exemplo `memory_fact:12`), `observed_at`
  com fuso, `confidence` entre 0 e 1 e validade opcional coerente. O ID do fato inclui
  a fonte: a mesma relação vista em duas fontes são dois fatos, cada um com a sua origem.
- **Estados.** `pending` (padrão) não entra em recuperação; `active` entra; `retracted`
  é final. Retirar preserva o fato e a proveniência, com um motivo curto. Reenviar um
  fato retirado não o reativa.
- **Consultas** limitadas a 100 itens: fatos de uma entidade (como sujeito ou objeto),
  ordenados por confiança e data, e busca de entidades por trecho do nome.

O adaptador de referência é `memory/graph.py`, em SQLite (migração 16). Gravações usam
`ON CONFLICT DO NOTHING`, que só cobre conflito de chave. Os `CHECK` do banco continuam
recusando valores inválidos. Datas vão em UTC para a ordenação textual seguir o tempo.
O backup já copia o banco inteiro, e o manifesto passa a contar as tabelas do grafo.

Nomes de entidades podem ser pessoais. Ficam só no banco local: não entram em eventos da
outbox, logs ou vault. Agregados futuros no vault terão só contagens.

O grafo continua desligado (`NEOSON_MEMORY_GRAPH_ENABLED=false`). Nesta decisão nada no
produto lê ou escreve nele. A extração de fatos, a recuperação híbrida e o adaptador
Neo4j vêm em incrementos separados, sobre esta mesma porta.

## Alternativas consideradas

- Neo4j desde o início: exige um servidor Java no notebook antes de haver dados ou
  medição de ganho. Fica como adaptador opcional que passa na mesma suíte de contrato.
- Um fato por relação, somando fontes: perde a origem por aresta exigida pelo §9.2 e
  dificulta retirar só o que veio de uma fonte apagada.
- Apagar fatos retirados: perde a trilha de auditoria e permitiria reintroduzir em
  silêncio um fato já rejeitado.

## Consequências

Uma migração nova (16), sem alterar tabelas existentes. A busca por nome é linear em
`LIKE`, adequada ao volume pessoal esperado. Se o volume crescer, a busca pode trocar
de índice sem mudar a porta.
