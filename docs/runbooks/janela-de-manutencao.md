# Janela de manutenção: atualizar o servidor

Roteiro para atualizar o servidor real com segurança: parar os serviços, fazer backup,
opcionalmente tirar o banco de uma pasta sincronizada com a nuvem, sincronizar o ambiente
e subir de novo. Leva cerca de uma hora na primeira vez. Os comandos abaixo são do
PowerShell no notebook; rode-os na pasta do projeto.

## 1. Conferir antes de parar

```powershell
uv run --no-sync python -m neoson_worker.maintenance_main check
```

O `check` só lê o banco. Ele informa a versão do esquema e as migrações pendentes, a
integridade, se o banco ou os documentos estão numa pasta sincronizada (OneDrive, Dropbox,
Google Drive, iCloud), se há sinal de processo usando o banco (arquivo `-wal`) e a idade do
último backup. Sai com código 1 se houver erro.

## 2. Parar os serviços

Veja quais processos do NeoSon estão rodando, conferindo a linha de comando antes de parar:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'neoson' } |
  Select-Object ProcessId, CommandLine
```

Pare o bot do Telegram, o gateway, o worker e o executor do grafo, se estiverem abertos
(`Stop-Process -Id <id>`). Não pare processos que você não reconhece. Rode o `check` de
novo: o aviso de `-wal` deve sumir.

## 3. Backup

```powershell
uv run --no-sync python -m neoson_worker.backup_main create
uv run --no-sync python -m neoson_worker.backup_main verify <pasta mostrada>
```

O backup inclui o banco, o vault e os originais dos documentos.

## 4. Tirar o banco da pasta sincronizada (recomendado)

```powershell
uv run --no-sync python -m neoson_worker.maintenance_main relocate --to D:\NeoSon-Data
```

A cópia do banco usa a API de backup do SQLite e é conferida (integridade e contagens por
tabela); a dos documentos é conferida arquivo por arquivo com SHA-256. Se algo não conferir,
só as cópias novas são removidas. O banco e os documentos originais ficam onde estavam. A
saída mostra as duas linhas para o `neoson.env`:

```dotenv
NEOSON_DB_PATH=D:\NeoSon-Data\neoson.db
NEOSON_DOCUMENTS_PATH=D:\NeoSon-Data\documents
```

Depois de subir e conferir que tudo funciona, você pode rodar de novo com
`--retire-original` numa pasta nova, ou renomear o arquivo antigo à mão, para ele não ser
usado por engano. O `--retire-original` renomeia só o banco (`.movido-<data>`); a pasta
antiga dos documentos fica como está.

O `relocate` recusa um destino que já tenha dados, um destino dentro da pasta dos
documentos e um banco com escrita em andamento. Esse último teste não enxerga um processo
parado entre duas escritas: pare os serviços antes, como no passo 2.

## 5. Sincronizar o ambiente

```powershell
uv sync --all-packages
```

Isso instala os comandos novos (`neoson-graph`, `neoson-docs`, `neoson-maintenance`). Só
rode com os serviços parados: no Windows, executáveis em uso impedem a troca.

## 6. Ligar recursos e subir

Ajuste o `neoson.env` com os recursos escolhidos (ver os runbooks de cada um). Suba o
gateway, o worker e o bot como de costume. As migrações pendentes rodam na primeira
abertura do banco. Confira:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
uv run --no-sync python -m neoson_worker.maintenance_main check
uv run --no-sync python -m neoson_worker.main --export-vault
```

`health/ready` deve responder `ready`, e o `check` deve mostrar o esquema atual. Mande uma
mensagem de teste pelo Telegram.

## Se algo der errado

Pare os serviços e restaure o backup do passo 3 com `backup_main restore <pasta> --force`;
os dados atuais são renomeados, nunca apagados. Se você mudou o banco de lugar, basta
voltar o `NEOSON_DB_PATH` e o `NEOSON_DOCUMENTS_PATH` do `neoson.env` para os caminhos
antigos, porque os originais continuam lá. Se tiver usado `--retire-original`, renomeie o
`neoson.db.movido-<data>` de volta para `neoson.db` antes.
