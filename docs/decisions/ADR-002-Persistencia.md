# ADR-002 — Persistência operacional em SQLite

- **Status:** accepted
- **Data:** 2026-10-20
- **Referências:** [ADR-001](ADR-001-monolito-modular.md); especificação técnica v1.0, §9, §10, §11
  e §18

## Contexto

O NeoSon precisa guardar sessões, mensagens e execuções, e publicar eventos duráveis para o worker
(§10). Roda num notebook com 16 GB, com um usuário e poucos processos (gateway e worker). A
especificação define o SQLite como fonte operacional, o Obsidian como interface humana e o Neo4j
como grafo opcional (§9).

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| PostgreSQL | Mais um serviço para operar e fazer backup, sem necessidade com um usuário |
| ORM (SQLAlchemy) e migrations com Alembic | Dependências e abstração maiores que o esquema; SQL direto basta e é mais fácil de auditar |
| Driver assíncrono (`aiosqlite`) | Mais uma dependência; operações curtas em uma thread (`asyncio.to_thread`) resolvem |
| Broker de mensagens (Redis, Kafka) | Excluído pela §1.2; a transactional outbox no próprio SQLite garante a atomicidade |

## Decisão

- **Banco:** um arquivo SQLite (`NEOSON_DB_PATH`), acessado com o `sqlite3` da biblioteca padrão.
  Toda conexão liga `foreign_keys`; o banco usa `journal_mode=WAL`, para que gateway e worker leiam
  e escrevam ao mesmo tempo.
- **Migrations:** arquivos SQL numerados em `neoson_core/infrastructure/migrations/`
  (`0001_initial.sql`, …), aplicados em ordem, cada um na sua transação, com a versão registrada em
  `schema_migrations`. Aplicar de novo não muda nada. Uma migration aplicada nunca é editada: mudança
  de esquema é sempre uma migration nova.
- **Transactional outbox:** a resposta, as mensagens e o evento (`outbox_events`) são gravados na
  mesma transação (§10). O worker consome os eventos depois; nada é enviado a outro sistema antes
  do commit.
- **Fronteiras:** a porta `ConversationStore` fica no `engine`, só com tipos da biblioteca padrão.
  A implementação SQLite fica em `neoson_core.memory`, usando a conexão e as migrations de
  `neoson_core.infrastructure.db`.
- **Async:** cada operação abre uma conexão curta e roda em `asyncio.to_thread`, sem bloquear o
  laço de eventos do gateway.
- **Backup:** pela API de backup do SQLite (`Connection.backup`), que gera uma cópia consistente
  mesmo com o banco em uso. Copiar o arquivo ativo não é suportado (§18).
- **Datas:** texto ISO 8601 em UTC. **IDs:** UUID em texto.

## Implicações

- Um único escritor por vez (limite do SQLite). Adequado para um usuário; se virar gargalo, a
  troca de banco fica atrás da porta `ConversationStore`.
- O esquema é SQL versionado no repositório, revisável em PR.
- O restore é abrir a cópia de backup; o teste de restauração faz parte da suíte.
