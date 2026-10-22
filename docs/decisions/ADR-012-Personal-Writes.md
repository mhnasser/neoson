# ADR-012 — Compromissos e rascunhos com aprovação concreta

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-003](ADR-003-Seguranca-Tools.md),
  [ADR-011](ADR-011-Personal-Integrations.md); especificação §5, §10 e §14.

## Contexto

Leitura de agenda/inbox não autoriza escrita. A autorização durável já apresenta
os argumentos de uma ação concreta e reserva sua execução antes do efeito. As
primeiras escritas precisam preservar essa separação, inclusive em simulação.

## Decisão

Introduzir portas `CalendarWriter` e `EmailDraftWriter`. Ferramentas `calendar_create`
e `email_draft` têm risco `sensitive_write` e exigem `ToolContext.action_id` para
executar. Grants em memória, instruções do modelo e permissões de skill não bastam:
somente a retomada da ação durável fornece esse identificador.

O evento tem título limitado e horários com offset, intervalo positivo de até
24 horas, sem convidados, recorrência, corpo ou anexos. O draft tem um destinatário
ASCII simples, assunto limitado e até 8.000 caracteres de texto simples. Não há
ferramenta para enviar, alterar ou excluir itens. O adaptador não aceita URLs,
headers, parâmetros Graph ou tipos MIME fornecidos pelo modelo.

`NEOSON_PERSONAL_WRITE_PROVIDER` é separado da leitura e vem `disabled`. O modo
`fake` escreve apenas em banco de sandbox separado, fora de produção. A chave
`action_id` e o fingerprint do payload impedem duplicação e reutilização com outros
argumentos, inclusive entre instâncias da sandbox.

O modo Microsoft exige `login-write` local, solicitando `Calendars.ReadWrite`,
`Mail.ReadWrite` e `offline_access` em cache separado. Nunca pede `Mail.Send`.
Esses escopos concedem ao token mais capacidade que as duas operações expostas:
o código mantém uma allowlist fixa de endpoints e payloads. A conta precisa consentir
explicitamente; a autorização de desenvolver o software não concede esse consentimento.

Um UUID de geração de login é persistido no cache e preservado na renovação. O
runtime inclui essa geração; o OAuth do adaptador exige o mesmo valor observado
na composição. Login substituído ou cache antigo sem geração exigem novo login e
reinício, invalidando aprovações anteriores. A rotação normal de access/refresh
tokens conserva as aprovações ainda válidas.

A revisão autenticada apresenta ferramenta, argumentos completos, prazo e descrição
fixa do destino: desativado, sandbox simulado ou conta do consentimento Microsoft.
`tool_description` é um campo opcional adicional na resposta HTTP; se o contexto
atual divergir, não reutiliza a descrição de outro destino. Não inclui endereço da
conta, token ou conteúdo privado em logs ou eventos.

Antes de criar no Graph, uma consulta limitada procura o marcador do `action_id`.
Um resultado existente com fingerprint correspondente é recuperado; divergência,
mais de um resultado, paginação ou draft já enviado exigem reconciliação. Eventos
também usam `transactionId` estável. O POST de calendário sempre tem convidados
vazios, pois [convidados geram convites](https://learn.microsoft.com/en-us/graph/api/user-post-events?view=graph-rest-1.0).
O POST de mensagem apenas [salva draft](https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0);
nunca é seguido por send. Marcadores usam
[extended properties](https://learn.microsoft.com/en-us/graph/api/singlevaluelegacyextendedproperty-get?view=graph-rest-1.0).

O prazo inclui OAuth, preflight e POST. Não há retry de rede. Sucesso fica
`completed`; erro ou cancelamento após reserva fica `unknown`/`executing` e não
libera nova execução. O mecanismo ajuda a recuperar um efeito pelo mesmo ID, mas
não promete exatamente uma vez em cópias divergentes do banco ou durante atraso
de indexação do fornecedor. Nunca repetir automaticamente após falha ambígua ou
restauração de backup sem reconciliar o destino.

## Alternativas consideradas

- Reutilizar token de leitura com ampliação implícita: mistura consentimentos;
  manter seleção, comando e cache separados.
- Aprovar nome de ferramenta ou aceitar “sim” do modelo: não vincula efeito e
  argumentos à identidade/contexto; preservar autorização concreta do ADR-003.
- Retry de POST em timeout: pode duplicar drafts; reservar antes e bloquear
  repetição quando o efeito é incerto.

## Consequências

Nenhuma migration operacional nova: a sandbox usa arquivo próprio e as aprovações
usam o schema existente. Payloads privados ficam apenas nos bancos locais e no
destino escolhido; eventos de auditoria continuam contendo IDs e estados.
Testes usam sandbox e HTTP mock, sem conta real ou mensagens a terceiros.

Operação no [runbook](../runbooks/personal-writes.md).
