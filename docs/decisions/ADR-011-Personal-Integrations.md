# ADR-011 — Leitura de calendário e e-mail

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-003](ADR-003-Seguranca-Tools.md),
  [ADR-006](ADR-006-Import-Boundaries.md); especificação §14.

## Contexto

O assistente precisa consultar compromissos e metadados do inbox sem receber
permissão para alterar a agenda ou enviar mensagens. O motor permanece independente
do fornecedor e não recebe tokens OAuth.

## Decisão

As portas `CalendarReader` e `EmailReader` ficam no engine. Ferramentas tipadas
`calendar_list` e `email_list` declaram risco `read` e passam pelo registry e policy.
O gateway escolhe `disabled` (padrão), `fake` ou `microsoft`. O fake é explicitamente
simulado e proibido em produção. O antigo fake de calendário continua disponível
para fixtures isoladas, mas a composição usa as novas portas.

Microsoft Graph v1.0 é o primeiro adaptador opcional, com permissões delegadas
`Calendars.ReadBasic` e `Mail.ReadBasic`, mais `offline_access` para renovação local.
Não se usam `.default`, application permissions, client secret nem escopos de escrita.
Metadados incluem títulos, horários e nome do remetente; corpo e anexos são excluídos.
Documentação do fornecedor confirma as permissões mínimas para
[calendarView](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0)
e [messages](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0).

O login por [device code](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)
é iniciado somente pelo comando local do operador. A ferramenta nunca inicia login.
O código de consentimento aparece apenas no terminal local; não passa por LLM,
mensagens de chat ou logs. O cache pertence ao client/tenant configurado, valida
escopos retornados e preserva a rotação de refresh tokens atomicamente.

O arquivo é protegido por DPAPI por usuário no Windows e por modo `0600`/dono atual
no POSIX. No POSIX não há criptografia adicional: a implantação exige diretório
privado e proteção de disco. Links/junctions são recusados. Lock exclusivo do SO
impede renovação concorrente entre processos; concorrente recebe erro temporário.

Cada leitura aceita até50 itens, percorre até3 páginas e tem prazo total de 20 s.
Cada resposta é limitada a 256 KiB; campos individuais também têm limites. URLs são
fixas; redirects e proxies do ambiente são desativados. `nextLink` só é seguido
no mesmo host/path, mantendo os parâmetros originais e permitindo apenas os
marcadores de paginação do fornecedor. 429 retorna erro seguro sem retry automático.
Resultados indicam truncamento, inclusive ao atingir o teto de páginas.

Conteúdo externo é JSON em mensagens `tool`, marcado como dado sem autoridade.
Não altera prompt de sistema, permissões ou aprovações. A proteção não garante que
um LLM ignore toda injeção: a policy continua sendo a autoridade para efeitos.

## Alternativas consideradas

- Conectar automaticamente contas disponíveis no ambiente: não comprova consentimento
  específico para o NeoSon; exigir configuração e login locais.
- Escopos amplos para facilitar recursos futuros: amplia acesso antes de existir um
  fluxo de revisão; separar leitura de futuras operações sensíveis.
- Cache em texto simples no Windows: inadequado para credenciais renováveis;
  usar proteção do sistema sem acrescentar SDK obrigatório ao cliente Robot.

## Consequências

A escolha do fornecedor integra o hash do runtime, invalidando contexto de aprovações
anteriores ao trocar configuração. Nenhuma conta real é requisito dos testes.
O CI usa HTTP mock e fake; consentimento e consultas reais exigem configuração externa.
O conteúdo devolvido ao modelo pode fazer parte do histórico SQLite da conversa,
que deve ser tratado como privado; o vault recebe somente agregados.

Operação e limites no [runbook](../runbooks/personal-integrations.md).

O [ADR-012](ADR-012-Personal-Writes.md) acrescenta um modo de escrita separado,
com seleção, login e cache próprios. O cache de leitura mantém a validação dos
escopos mínimos acima e recusa tokens com escopos de escrita.
