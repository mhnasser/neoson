# NeoSon

Assistente pessoal de IA com interface física, motor Python próprio, memória, skills e melhoria
contínua. A especificação técnica e os diagramas estão em [`docs/architecture/`](docs/architecture/).

> **Estado:** fundação do repositório (pacote, testes e ferramentas de qualidade). As
> funcionalidades do assistente ainda não foram implementadas.

## Requisitos

- Python 3.13, fixado em `.python-version` (a mesma versão da Raspberry Pi).
- [uv](https://docs.astral.sh/uv/) (testado com 0.12.21).

## Setup

```bash
uv sync
```

O comando cria `.venv/` com Python 3.13 e instala o pacote `neoson` em modo editável, junto com as
ferramentas de desenvolvimento nas versões fixadas em `uv.lock`. Se o Python 3.13 não estiver
instalado, o uv faz o download.

## Configuração

```bash
cp .env.example neoson.env
```

Preencha os valores em `neoson.env`. O arquivo está no `.gitignore` e não deve ir para o Git. Por
enquanto nenhum código lê essas variáveis; elas documentam a configuração prevista.

## Testes e qualidade

```bash
uv run pytest                  # testes
uv run ruff check .            # lint
uv run ruff format --check .   # formatação
uv run mypy src                # tipagem (modo estrito)
```

## Estrutura

```text
src/neoson/   pacote principal
tests/        testes automatizados
docs/         arquitetura, diagramas, decisões e inventário de hardware
```
