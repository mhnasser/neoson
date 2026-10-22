# Revisar e retomar ações sensíveis

O modelo propõe uma ferramenta; apenas uma decisão autenticada do proprietário autoriza
a chamada concreta salva. O fluxo está disponível no gateway com provedor `openai`,
inclusive quando um provedor fake é injetado pelos testes. O modo `echo` não cria ações
e não monta estes endpoints. `calendar_create` e `email_draft` oferecem escritas
limitadas com destino desativado por padrão, sandbox ou Microsoft por escolha
explícita; veja o [runbook de escrita](personal-writes.md). Testes não usam contas reais.

## Atualizar a instalação

Faça `neoson-backup create` antes de iniciar o novo gateway. A migration 14 acrescenta
`pending_actions` sem mudar sessões, mensagens ou tabelas anteriores. Backup/restauração
do SQLite já incluem a tabela e seus argumentos privados; preserve os backups no
armazenamento local autorizado. Não copie esses dados para Git ou vault.
Restaurar um snapshot também restaura seus estados de aprovação. Antes de retomar
ações de um backup, reconcilie os efeitos posteriores ao snapshot no destino; uma
cópia antiga pode ainda mostrar como aprovada uma ação já executada. A integração real
precisa preservar a mesma chave de idempotência no fornecedor mesmo após restauração.
Reinicie o gateway para carregar as rotas. Não inicie outro polling do Telegram.

## Consultar e decidir

Todas as rotas abaixo exigem `Authorization: Bearer <NEOSON_API_TOKEN>`. Use um cliente
HTTP que leia o token da configuração local, sem copiá-lo para documentação ou histórico
de comandos. Não compartilhe esse token: a instalação possui um só proprietário HTTP.

1. Envie `POST /api/v1/messages` normalmente. Se a resposta tiver `needs_approval`, guarde
   o `run_id` e consulte `GET /api/v1/runs/{run_id}/actions`. Quando o pedido veio do
   Robot, que não mostra o `run_id`, use `GET /api/v1/actions`: lista até 20 ações ainda
   pendentes ou aprovadas sem execução, as mais recentes primeiro. Ações vencidas saem
   da lista.
2. Revise ferramenta, `tool_description` (inclui destino/simulação quando disponível),
   argumentos completos, fingerprint, sessão, execução e validade.
   `GET /api/v1/actions/{id}` recupera o mesmo registro. A resposta usa `Cache-Control:
   no-store`; não encaminhe seu corpo a logs ou ferramentas de telemetria.
3. Para aprovar, envie `POST /api/v1/actions/{id}/decision` com `{"approve": true}`.
   Para negar, use `{"approve": false}`. Novos argumentos, identidade e campos extras
   não são aceitos. Cada ação pendente exige sua própria decisão.
4. Após a decisão, envie `POST /api/v1/actions/{id}/resume` sem corpo para executar a
   chamada salva. A resposta informa o estado da ação; não é uma nova resposta do LLM.

Repetir a mesma decisão é idempotente; uma decisão oposta é conflito. A validade é de
15 minutos desde a suspensão e não é renovada ao aprovar. HTTP 404 cobre ação ausente
ou de outra identidade; 409 cobre estado incompatível, registro inconsistente ou mudança
de contexto/permissões. HTTP 422 indica requisição inválida. Uma ação expirada/negada
não pode ser liberada por outra decisão.

O proprietário vem da autenticação do servidor; o cliente não pode informar principal.
Esta instalação não oferece isolamento multiusuário nem consentimento pelo Telegram.
Canais sem principal confiável continuam suspendendo de forma segura, sem uma ação
retomável. Aprovar um caso novo não autoriza outros pedidos, outras sessões ou chamadas
posteriores do modelo, mesmo com argumentos iguais.

## Estados e falhas

| Estado | Significado |
|---|---|
| `pending` | Aguarda decisão humana |
| `approved` | Aprovada, ainda sem efeito, dentro do prazo |
| `denied` / `expired` / `invalid` | Bloqueada; sem nova tentativa |
| `executing` | Reserva confirmada antes da chamada; pode indicar interrupção |
| `completed` | Ferramenta confirmou sucesso; repetir resume não a executa |
| `unknown` | Falha/timeout com efeito possivelmente ocorrido; sem retry automático |

Se houver `unknown` ou `executing` após uma interrupção, primeiro confira o efeito no
destino. Este incremento não oferece reset/reativação do registro. Não altere o estado
por SQL para repetir a operação. Integrações externas futuras precisam reconciliar com
o fornecedor e usar o ID estável da ação como chave de idempotência. Nova aprovação não
desfaz um efeito que já possa ter ocorrido.

Revalidação de contexto inclui configuração operacional/modelo/preços/política e schema
das ferramentas. Risco e permissões/versão/checksum da skill são conferidos novamente.
Mudanças de implementação com o mesmo schema exigem atualizar a versão do contexto de
ferramentas. A espera pela decisão não consome o teto de execução; o tempo remanescente
salvo limita somente a chamada retomada. Não há novas etapas/modelos ou orçamento extra.

## Verificação isolada

`uv run --no-sync python -m pytest -q tests/test_actions.py` cobre persistência/reinício,
autenticação, identidade, argumentos alterados, expiração, negação, orçamento, mudanças
de skill/risco/schema, concorrência e falhas antes/depois do efeito. O calendário/e-mail
reais não são acessados; não há envio de mensagens nem chamadas pagas nessa validação.
