# Runbook — Backup e restauração

Procedimento para copiar e recuperar os dados do NeoSon: o banco SQLite (`NEOSON_DB_PATH`) e o
vault do Obsidian (`NEOSON_VAULT_PATH`). Referências: especificação §18 e §21, ADR-002.

## O que entra no backup

| Item | Como | Observação |
|---|---|---|
| Banco SQLite | API de backup do SQLite | Cópia consistente mesmo com gateway e worker rodando |
| Vault | cópia da pasta inteira | Arquivos `.tmp` de escrita em andamento ficam de fora |
| `manifest.json` | gerado no backup | Data, versão do esquema, contagens por tabela e SHA-256 de cada arquivo |

**Não entra:** o `neoson.env`, porque tem segredos (chave da OpenAI, token da API, token do bot).
Guarde uma cópia dele à parte, num cofre de senhas, e nunca no Git nem no vault.

## Criar um backup

```bash
uv run --package neoson-worker neoson-backup create
```

O backup vai para `NEOSON_BACKUP_PATH` (padrão `data/backups/`), numa pasta
`neoson-backup-AAAAMMDD-HHMMSS`. Use `--dest <pasta>` para outro lugar, por exemplo um disco
externo. O comando já verifica o backup que acabou de criar e mostra as contagens.

Frequência sugerida: diária, e sempre antes de atualizar o NeoSon. A política de retenção (quantos
backups manter) e a cópia para fora da máquina ainda não estão automatizadas.

## Verificar um backup

```bash
uv run --package neoson-worker neoson-backup verify data/backups/neoson-backup-AAAAMMDD-HHMMSS
```

Confere se o banco passa no `PRAGMA integrity_check` e se o banco e cada arquivo do vault têm o
hash registrado no manifesto. Saída 0 quando está íntegro; 1 com o motivo, caso contrário.

## Restaurar

1. **Pare o gateway, o worker e o `neoson-telegram`.** A restauração troca o arquivo do banco.
2. Confira o backup escolhido com `neoson-backup verify <pasta>`.
3. Restaure:

   ```bash
   uv run --package neoson-worker neoson-backup restore <pasta>
   ```

   Se já existirem banco ou vault no destino, o comando recusa. Para restaurar mesmo assim, use
   `--force`: o banco atual (com `-wal` e `-shm`) e o vault atual são **renomeados** para
   `*.antes-da-restauracao-AAAAMMDD-HHMMSS`, nunca apagados. Apague essas cópias só depois de
   confirmar que está tudo certo.
4. Recoloque o `neoson.env` guardado à parte, se a máquina for nova.
5. Suba os serviços e confira:
   - `GET /api/v1/health/live` responde `{"status": "ok"}`;
   - uma conversa antiga continua com `neoson-robot --session-id <id>`;
   - `neoson-worker --export-vault` regera os painéis sem erro.

## Testar a restauração sem mexer nos dados reais

Restaure num diretório temporário, apontando o banco e o vault para lá só nesse comando:

```bash
NEOSON_DB_PATH=/tmp/neoson-teste/neoson.db \
NEOSON_VAULT_PATH=/tmp/neoson-teste/vault \
uv run --package neoson-worker neoson-backup restore <pasta>
```

Os dados reais não são tocados. Faça esse teste de vez em quando: backup que nunca foi restaurado
não é garantia.
