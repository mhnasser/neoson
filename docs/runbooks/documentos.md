# Documentos: ingerir, revisar e esquecer

`neoson-docs` traz arquivos de texto para o NeoSon
([ADR-019](../decisions/ADR-019-Document-Ingestion.md)). Enquanto o comando não estiver
instalado no ambiente, use `uv run --no-sync python -m neoson_worker.docs_main`.

## Ingerir

```bash
uv run --no-sync python -m neoson_worker.docs_main ingest ./notas/plano.md
```

Aceita `.md`, `.markdown` e `.txt` em UTF-8. A saída mostra o ID (SHA-256), se o
documento é novo, quantos trechos gerou e o título. Ingerir o mesmo arquivo de novo não
duplica nada.

O que acontece:

1. O original é copiado para `NEOSON_DOCUMENTS_PATH` (padrão `data/documents/`).
2. O texto normalizado é dividido em trechos e guardado no banco.
3. Uma nota é criada em `Documentos/` no vault, com a fonte e o texto em bloco de
   código. Blocos `dataviewjs`, links e embeds do documento aparecem só como texto.
4. Com `NEOSON_MEMORY_GRAPH_ENABLED=true`, cada trecho vira um job de extração. Rode o
   executor e revise os fatos como no [runbook do grafo](knowledge-graph.md); a fonte
   aparece como `document:<id>:<trecho>`.

Documentos de terceiros podem conter instruções para o modelo. Elas nunca são
executadas: a extração não tem ferramentas e todo fato fica pendente até a sua revisão.

## Listar e esquecer

```bash
uv run --no-sync python -m neoson_worker.docs_main list
uv run --no-sync python -m neoson_worker.docs_main forget <id>
```

`forget` apaga o original, os trechos e a nota, e agenda a retirada dos fatos desse
documento (o executor do grafo processa a retirada sem chamar o modelo). O registro
fica como `forgotten` e o mesmo arquivo não pode ser ingerido de novo.

## Limites e backup

| Variável | Padrão |
|---|---|
| `NEOSON_DOCUMENT_MAX_BYTES` | 2000000 |
| `NEOSON_DOCUMENT_MAX_CHARS` | 200000 |
| `NEOSON_DOCUMENT_CHUNK_CHARS` | 3000 (cada trecho também precisa caber no limite de entrada da extração) |

`neoson-backup` copia o banco e o vault, mas **não** a pasta dos originais. Inclua
`NEOSON_DOCUMENTS_PATH` na sua cópia de segurança. A migração 18 roda na abertura do
banco; faça backup antes de atualizar.
