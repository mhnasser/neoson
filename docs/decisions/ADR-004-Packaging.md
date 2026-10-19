# ADR-004 — Empacotamento: uv workspace e `[project.scripts]`

- **Status:** accepted
- **Data:** 2026-10-19
- **Referências:** [ADR-001](ADR-001-monolito-modular.md); especificação técnica v1.0, §16 e §17

## Contexto

O [ADR-001](ADR-001-monolito-modular.md) divide o NeoSon em bibliotecas (`neoson-core`,
`neoson-contracts`) e apps (`neoson-gateway`, `neoson-worker`, `neoson-robot`) com dependências
diferentes. A Pi precisa instalar só o cliente. Cada app também precisa de um comando padronizado
para iniciar.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Pacote único com extras (`neoson[gateway]`, `neoson[robot]`) | O código do servidor continua indo para a Pi, e as fronteiras não ficam físicas |
| Um repositório por pacote | Versionar e sincronizar contratos entre repositórios custa caro para um mantenedor |
| Só `[project.scripts]`, sem workspace | Resolve como iniciar os apps, mas não separa as dependências |

Workspace e `[project.scripts]` não são concorrentes: o workspace gerencia vários projetos Python no
mesmo repositório; `[project.scripts]` define como iniciar um app instalado. Os dois são usados.

## Decisão

### Estrutura

```text
neoson/
├── pyproject.toml           # raiz do workspace e configuração das ferramentas
├── uv.lock                  # um lockfile para todo o workspace
├── packages/
│   ├── neoson-contracts/    # pyproject.toml + src/neoson_contracts/
│   └── neoson-core/         # pyproject.toml + src/neoson_core/
├── apps/
│   ├── gateway/             # pyproject.toml + src/neoson_gateway/
│   ├── worker/              # pyproject.toml + src/neoson_worker/
│   └── robot/               # pyproject.toml + src/neoson_robot/
├── skill_library/
├── tests/
├── docs/
│   ├── architecture/
│   └── decisions/
└── .github/workflows/ci.yml
```

### Regras

- A raiz não é instalada (`package = false`). Ela declara os membros do workspace, o grupo `dev`
  (pytest, mypy, ruff, import-linter) e a configuração de ruff, mypy, pytest e import-linter.
- Cada membro tem `pyproject.toml` próprio, só com as próprias dependências, backend `uv_build` e
  layout `src/`. Dependências entre membros usam `[tool.uv.sources] <pacote> = { workspace = true }`.
- Cada app expõe um comando em `[project.scripts]`, apontando para uma função `main()`.
- Python 3.13, fixado em `.python-version`, com `requires-python = ">=3.13,<3.14"` em todos os
  membros. A proposta inicial previa `>=3.12` e CI em 3.12; ficou a 3.13, que é a versão da Pi.
- A faixa do `uv_build` acompanha a versão do uv em uso.
- Membros novos só quando houver motivo. Não há `apps/dashboard`: o Metrics Exporter é consumidor
  de eventos e roda no worker (T-009 da §19.2 depende do worker).

### Exemplo

Raiz:

```toml
[project]
name = "neoson-workspace"
version = "0.1.0"
requires-python = ">=3.13,<3.14"
dependencies = []

[tool.uv]
package = false

[tool.uv.workspace]
members = ["packages/*", "apps/*"]

[dependency-groups]
dev = ["pytest", "mypy", "ruff", "import-linter"]
```

`apps/gateway/pyproject.toml`:

```toml
[project]
name = "neoson-gateway"
version = "0.1.0"
requires-python = ">=3.13,<3.14"
dependencies = ["neoson-core", "neoson-contracts", "fastapi", "uvicorn"]

[tool.uv.sources]
neoson-core = { workspace = true }
neoson-contracts = { workspace = true }

[project.scripts]
neoson-gateway = "neoson_gateway.main:main"

[build-system]
requires = ["uv_build>=0.12.21,<0.13"]
build-backend = "uv_build"
```

### Comandos

```bash
uv sync --all-packages                              # ambiente de desenvolvimento completo
uv run --package neoson-gateway neoson-gateway      # gateway
uv run --package neoson-worker neoson-worker        # worker
uv run --package neoson-robot neoson-robot          # cliente da Raspberry
```

### Ordem de criação

1. Raiz do workspace, `neoson-core`, `neoson-contracts` e `neoson-gateway`.
2. Comunicação HTTP simples, validada por Pydantic.
3. `neoson-robot`, sem dependências do servidor.
4. Ruff, mypy, pytest e import-linter cobrindo todos os membros.
5. `neoson-worker`, quando começar o processamento assíncrono de memória e skills.

## Implicações

- No desenvolvimento, `uv sync --all-packages` instala tudo junto, então um import proibido
  funcionaria na máquina e só quebraria na Pi. O [ADR-006](ADR-006-Import-Boundaries.md) cobre esse
  risco.
- A Pi instala só `neoson-robot` e `neoson-contracts` (por exemplo, `uv sync --package
  neoson-robot`). A forma de deploy fica para a F6.
- Um lockfile para tudo: atualizar uma dependência compartilhada afeta todos os membros de uma vez.
