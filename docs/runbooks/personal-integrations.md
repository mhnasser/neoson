# Calendário e e-mail: leitura opcional

`calendar_list` consulta o calendário padrão no dia informado usando o fuso
`NEOSON_TIMEZONE`. `email_list` consulta metadados recentes do inbox. As ferramentas
nunca criam, alteram ou excluem itens, enviam e-mails ou marcam mensagens como lidas.
O acesso de rede é somente GET; os POSTs OAuth são exclusivos para autenticação.

## Selecionar fornecedor

O padrão `NEOSON_PERSONAL_PROVIDER=disabled` devolve erro explícito ao consultar;
não inventa agenda. Para demonstração local, configure `fake` fora de produção.
Resultados fake têm `simulated:true` e conteúdo fictício. O modo `echo` do motor
não chama ferramentas; para usar ferramentas, configure o motor OpenAI.

O adaptador `microsoft` requer registro próprio de aplicativo público no Microsoft
Entra, com device code/public client habilitado e tipo de conta adequado. Escolher
o adaptador não conecta automaticamente a conta usada por outros aplicativos.

No `neoson.env` local:

```dotenv
NEOSON_PERSONAL_PROVIDER=microsoft
NEOSON_MICROSOFT_CLIENT_ID=<id-do-aplicativo-publico>
NEOSON_MICROSOFT_TENANT=consumers
NEOSON_MICROSOFT_TOKEN_PATH=./data/credentials/microsoft.bin
```

`consumers` restringe contas pessoais; `organizations`, `common` ou um UUID de tenant
podem ser escolhidos explicitamente para outros tipos de conta. O client ID precisa
ser um UUID. Não configure segredo de cliente. Os únicos escopos solicitados são
`Calendars.ReadBasic`, `Mail.ReadBasic` e `offline_access`. Consentimento de escrita
ou leitura de corpo de e-mail não é solicitado nem aceito pelo cache.

Após instalar o workspace, execute em terminal privado:

```bash
uv run --package neoson-gateway neoson-personal login
# Alternativa sem reinstalar executáveis em uso:
uv run --no-sync python -m neoson_gateway.personal_main login
```

Abra manualmente `https://microsoft.com/devicelogin` e informe o código temporário
exibido naquele terminal. Confira a identidade e os escopos antes de consentir.
Não copie código/cache para chat, captura de tela ou Markdown. Ctrl+C cancela.
O login respeita pending/slow_down e expiração; negar ou expirar não sobrescreve o
cache anterior. Reinicie o gateway de forma controlada após configurar.

## Credenciais e privacidade

O destino padrão está em `data/`, fora do Git. Use um diretório local privado;
fora de `data/`, garanta também exclusão do controle de versão e sincronização.
DPAPI impede uso por outra identidade Windows; no POSIX o arquivo é0600 e o
diretório deve ser `0700`, com disco protegido. O cache é privado e não integra
backups SQLite/vault. Não copiar entre máquinas/usuários; refazer consentimento.
O arquivo `.lock` contém apenas um byte fixo e pode permanecer; o SO libera seu
lock quando o processo termina. Não apagar um lock para contornar processo ativo.

No caso de credenciais inválidas/expiradas, refaça login explicitamente. A renovação
usa somente os mesmos escopos e salva novos refresh tokens atomicamente. Um
processo concorrente pode receber erro temporário; tente a consulta posteriormente.
Revogação do consentimento é feita na conta Microsoft. Desativar o fornecedor e
reiniciar o serviço interrompe consultas, mas não revoga consentimento no servidor.

Somente assunto/título, horários, identificador e nome do remetente entram no
resultado. Corpo, anexos, token e erro HTTP bruto ficam fora. Esses metadados são
privados: o modelo recebe o resultado `tool` e ele pode permanecer no histórico
SQLite; não usar DEBUG de bibliotecas HTTP em operação com contas reais.

## Limites e diagnóstico

- `limit`: inteiro de 1 a 50, padrão10. Até3 páginas/20s por leitura; 256 KiB por resposta.
- `truncated:true`: há mais dados ou o teto de páginas/itens foi atingido. Não concluir
  que o restante da agenda/inbox está vazio. Uma nova consulta não continua o cursor.
- 401/403: erro seguro de credencial/permissão.429: limite do fornecedor, sem retry;
  aguardar antes de uma nova consulta explícita. Falhas HTTP/timeouts não incluem
  conteúdo privado e não repetem chamadas automaticamente.
- Redirecionamentos, paginação para outro host/path ou alteração dos filtros são
  recusados. Se o fornecedor alterar esse contrato, revisar o adaptador antes de liberar.
- Calendário usa intervalo entre meias-noites locais com offsets e respostas UTC;
  inclui ocorrências recorrentes devolvidas pelo calendarView, sem interpretar corpo.

O CI valida contratos com HTTP mock e fake. Testes não comprovam disponibilidade,
políticas do tenant, acesso à conta ou consentimento real. Nenhuma conexão real é
ativada implicitamente pela instalação.
