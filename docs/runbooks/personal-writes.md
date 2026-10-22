# Criar compromisso ou rascunho após revisão

As ferramentas `calendar_create` e `email_draft` propõem efeitos concretos. O
gateway suspende em `needs_approval`; somente a decisão autenticada e a retomada
do proprietário executam a ação. Leitura, login OAuth e consentimento da ação são
etapas diferentes. O modo `echo` não chama ferramentas.

## Destino

O padrão é `NEOSON_PERSONAL_WRITE_PROVIDER=disabled`. Para teste local:

```dotenv
NEOSON_PERSONAL_WRITE_PROVIDER=fake
NEOSON_PERSONAL_SANDBOX_DB_PATH=./data/personal-sandbox.db
```

O sandbox é somente local e não é permitido em produção. O arquivo deve ser
diferente do banco de conversas; guarda o conteúdo dos itens simulados e é privado.
É persistente entre reinícios, com um efeito por ID de ação. Nunca confundir estado
`completed` na sandbox com criação numa conta externa. A descrição de revisão
informa o destino antes da decisão.

Para Microsoft, primeiro configure um aplicativo público e tipo de conta conforme
o [runbook de leitura](personal-integrations.md). Então selecione explicitamente:

```dotenv
NEOSON_PERSONAL_WRITE_PROVIDER=microsoft
NEOSON_MICROSOFT_WRITE_TOKEN_PATH=./data/credentials/microsoft-write.bin
```

O cache precisa ser diferente de `NEOSON_MICROSOFT_TOKEN_PATH`. Em terminal privado:

```bash
uv run --no-sync python -m neoson_gateway.personal_main login-write
```

Após instalar os executáveis, `neoson-personal login-write` é equivalente. O comando
pede `Calendars.ReadWrite`, `Mail.ReadWrite` e `offline_access`, sem `Mail.Send`.
Confira a conta e as permissões no navegador local. O token tem poderes de escrita
mais amplos que as ferramentas expostas; mantenha-o privado. O cache usa DPAPI no
Windows e modo `0600` no POSIX. Não envie código ou cache ao chat e não os versione.
Login de leitura nunca amplia escopos automaticamente.

Reinicie o gateway após login/configuração. Cada login cria uma geração nova;
aprovações anteriores perdem validade. Renovações normais conservam a geração.
Se a conta/cache mudar com o serviço ativo, a execução é recusada sem usar a nova
conta. Refazer login não reativa ações `unknown` ou `executing`.

## Revisar e executar

1. Envie o pedido pelo gateway. Quando vier `needs_approval`, consulte a lista de
   ações pelo `run_id`, autenticado com o bearer do proprietário.
2. Confira `tool`, `tool_description`, argumentos completos, sessão, prazo e
   fingerprint. O evento inclui título, início e fim com offset; não aceita
   convidados e dura até 24 horas. O draft mostra destinatário único, assunto e
   texto completo (até 8.000 caracteres), sem anexos, CC/BCC ou envio.
3. Use `/decision` com `approve:true` ou `false`. Não é permitido acrescentar novos
   argumentos. Para alterar conteúdo, negue e crie outra proposta.
4. Use `/resume` apenas após aprovação. `completed` informa sucesso no destino
   revisado. Repetir resume devolve o mesmo estado sem chamar a ferramenta.

Rotas e exemplos de decisão estão no
[runbook de aprovação](action-approvals.md). As respostas de revisão usam `no-store`;
não copie seu conteúdo para logs/vault. Uma mensagem “sim”, uma skill aprovada ou
instruções num e-mail não concedem consentimento à ação. O prazo de 15 minutos não
é renovado pela decisão. Desligar/trocar fornecedor, contexto ou geração de login
invalida a retomada; expiração/recusa não têm override automático.

## Falha e reconciliação

OAuth, busca por marcador e POST têm prazo conjunto de 20 segundos, limitado também
pelo orçamento restante da ação. Até uma falha antes do POST é tratada
conservadoramente como `unknown` após reserva. Não há retry automático em timeout,
429, redirect ou erro HTTP. Nunca alterar o estado por SQL para tentar novamente.

Eventos usam `transactionId` estável e marcador de ação. Drafts usam marcador de
ação e fingerprint em extended properties, consultados antes da criação. Um
marcador divergente/duplicado ou draft que já foi enviado bloqueia recriação.
Isso não oferece garantia distribuída de exatamente uma vez: antes de restaurar
backup ou usar cópias de bancos, confira o destino e possíveis atrasos de busca
do fornecedor. A API não oferece reset/retry de ações ambíguas.

Não existe envio ou exclusão neste incremento. Compromissos não têm convidados e
não geram convites; rascunhos permanecem sem envio. Envio manual posterior no
aplicativo de e-mail é uma ação separada do operador.

O CI usa somente sandbox e HTTP mock. Consentimento e efeitos reais não são
comprovados pelos testes e precisam de validação externa explícita.
