# ADR-019 — Ingestão de documentos de texto para o vault e o grafo

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-014](ADR-014-Knowledge-Graph.md),
  [ADR-015](ADR-015-Graph-Extraction.md); especificação §9.1, §9.3 (D11) e §14.

## Contexto

O fluxo D11 da especificação leva um documento recebido até o vault e o grafo,
preservando o original com hash e metadados e tratando o texto extraído como dado não
confiável. Documentos podem trazer instruções escondidas e, no Obsidian, blocos que o
Dataview executa como JavaScript.

## Decisão

`neoson-docs ingest <arquivo>` é a única porta de entrada: local e explícita.

- **Formatos.** Só `.md`, `.markdown` e `.txt`, em UTF-8 (com ou sem BOM), sem bytes
  nulos e dentro dos limites de bytes e caracteres. Outros formatos e encodings são
  recusados com motivo fixo; PDF fica fora deste incremento.
- **Original.** Cópia exata em `NEOSON_DOCUMENTS_PATH/<sha256>.<ext>`, com permissão
  só do usuário, fora do vault. O SHA-256 do arquivo é o ID do documento e torna a
  ingestão idempotente.
- **Texto.** Quebras de linha normalizadas, caracteres de controle removidos e trechos
  de até `NEOSON_DOCUMENT_CHUNK_CHARS`, de preferência em parágrafos, guardados no
  SQLite (migração 18).
- **Nota.** `Documentos/<título>-<hash>.md` no vault, com fonte `document:<sha256>`,
  hash, nome original, data e contagens. O texto entra dentro de um bloco de código mais
  longo que qualquer sequência de crases do documento. Assim, blocos `dataviewjs`,
  links e embeds viram texto e não são executados nem resolvidos. Título e nome original
  são filtrados para caracteres simples.
- **Grafo.** Com o grafo ligado, cada trecho vira um job de extração com fonte
  `document:<sha256>:<n>`. O executor do ADR-015 é o mesmo: o trecho vai como dado JSON
  sem ferramentas, e os fatos entram como `pending` até a revisão.
- **Esquecer.** `neoson-docs forget <id>` torna obsoletas as extrações pendentes, cria
  retiradas para os fatos de cada trecho e apaga original, trechos e nota. O registro
  fica como `forgotten`, e o mesmo arquivo não pode ser reingerido.

## Alternativas consideradas

- Texto direto na nota, como Markdown: deixa o documento executar código no vault e
  criar links para notas privadas.
- Resumo por LLM na ingestão: custo e risco de alucinação em todo documento; a
  extração de fatos já passa por revisão.
- Guardar o original no SQLite: incha o banco e mistura bytes de terceiros com o
  histórico operacional.

## Consequências

O backup do NeoSon cobre o SQLite (registro, trechos, fatos) e o vault (notas), mas não
a pasta dos originais: ela precisa entrar na cópia manual. Documentos esquecidos não
voltam pelo mesmo arquivo. A busca `memory_search` não procura nos documentos; eles
chegam às respostas pelos fatos revisados. Operação no
[runbook](../runbooks/documentos.md).
