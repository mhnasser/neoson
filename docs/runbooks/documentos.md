# Documentos: ingerir, revisar e esquecer

`neoson-docs` traz arquivos de texto, PDFs e fotos para o NeoSon
([ADR-019](../decisions/ADR-019-Document-Ingestion.md),
[ADR-021](../decisions/ADR-021-Multimodal-Ingestion.md)). Enquanto o comando não estiver
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

## PDFs: fila e extração local

Instale o extra opcional no ambiente parado de processamento:

```bash
uv sync --locked --all-packages --extra documents
uv run --no-sync python -m neoson_worker.docs_main ingest ./notas/plano.pdf
uv run --no-sync python -m neoson_worker.docs_main process --once
```

O registro confere assinatura, tamanho e hash e devolve `pending`. `process --once`
processa no máximo um documento; sem `--once`, acompanha a fila. O instalador oferece
o serviço opcional `documents`. Nenhum processo é iniciado pela ingestão.

O parser executa em um filho com diretório temporário e ambiente sem credenciais,
sem abrir banco, vault ou conexões de rede. No Linux, memória limitada a 512 MiB e
CPU limitada ao prazo configurado; no Windows, vale o timeout do processo.
Uma extração por vez, lease por reserva e até três tentativas em caso de timeout.
Falhas definitivas ficam registradas sem publicar trechos parciais. `list` mostra
o estado da extração; ingerir novamente não cria outro job.

Cada trecho guarda sua página, também exibida pela `memory_search`. Páginas vazias
ou com menos de 20 caracteres seguem para OCR quando há imagem embutida. Sem imagem
extraível, ficam marcadas com `no_embedded_image`: ainda não há renderizador para
páginas vetoriais. PDFs cifrados são recusados; tabelas e colunas podem sair fora de
ordem. O conteúdo fica como texto inerte no vault.

## Fotos e OCR local

Aceita JPG, PNG e HEIC estáticos. Confere assinatura e extensão, corrige orientação
EXIF e cria PNG RGB novo, sem EXIF/XMP/GPS, antes de persistir a foto. O ID continua
sendo o hash recebido e o banco guarda também o hash da cópia sanitizada. O arquivo
fornecido pelo usuário não é modificado; no armazenamento do NeoSon só fica a cópia
sanitizada. Imagens animadas são recusadas.

No alvo Linux, instale `tesseract-ocr` e `tesseract-ocr-por` pelo gerenciador de
pacotes do sistema. Confira `tesseract --list-langs` antes de iniciar o serviço.
O extra `documents` instala Pillow, pillow-heif e pypdf; o executável Tesseract fica
fora do workspace. Use a mesma fila e os mesmos comandos:

```bash
uv run --no-sync python -m neoson_worker.docs_main ingest ./notas/foto.jpg
uv run --no-sync python -m neoson_worker.docs_main process --once
```

Cada chamada do OCR tem prazo de 10 s por imagem e retorna texto e confiança de
0 a 100. A nota mostra páginas por rota, confiança média/mínima e quantidade de
páginas abaixo de 60. Qualidade baixa fica sinalizada para revisão; não envia
arquivos para a nuvem. O plano B multimodal continua previsto no ADR, sem provedor
ou ativação nesta entrega. Letra à mão, fórmulas e reconstrução de tabelas não são
alvos; a ordem de imagens múltiplas de uma página pode precisar de revisão.

Depois de corrigir uma dependência ou configuração, reagende explicitamente uma
extração falha com `neoson-docs retry <id>`. Documentos esquecidos e extrações já
concluídas não podem ser reagendados. A próxima tentativa usa os bytes armazenados,
validados pelo hash. Só `timeout` recebe novas tentativas automáticas.

Pillow usa MIT-CMU, pillow-heif usa BSD e libheif usa LGPL; confira as licenças dos
codecs no ambiente antes de redistribuir binários. O repositório não inclui wheels,
codecs nem executáveis; HEIC é usado para leitura, com saída PNG.
Fontes: [Pillow](https://pillow.readthedocs.io/en/stable/),
[pillow-heif](https://pillow-heif.readthedocs.io/en/latest/),
[Tesseract](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html).

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
| `NEOSON_DOCUMENT_PDF_MAX_BYTES` | 20000000 |
| `NEOSON_DOCUMENT_MAX_PAGES` | 200 |
| `NEOSON_DOCUMENT_EXTRACT_TIMEOUT_SECONDS` | 60 |
| `NEOSON_DOCUMENT_IMAGE_MAX_BYTES` | 15000000 (inclui a cópia PNG sanitizada) |
| `NEOSON_DOCUMENT_MAX_PIXELS` | 40000000 |
| `NEOSON_DOCUMENT_OCR_TIMEOUT_SECONDS` | 10 |
| `NEOSON_DOCUMENT_OCR_MIN_CONFIDENCE` | 60 |
| `NEOSON_DOCUMENT_TESSERACT_COMMAND` | `["tesseract"]` (lista JSON de executável/argumentos fixos) |

`neoson-backup` copia o banco, o vault e a pasta dos originais (`NEOSON_DOCUMENTS_PATH`).
As migrações rodam na abertura do banco. As versões 22/23 ampliam os documentos e mantêm
os IDs, trechos, fatos e índice de busca existentes. Pare os escritores e faça
backup verificado antes de atualizar a instalação real. Para reverter, use o
backup anterior com o código anterior; não abra o banco novo com código antigo.
