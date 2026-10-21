# ADR-003 — Segurança na execução de ferramentas

- **Status:** accepted
- **Data:** 2026-10-20
- **Referências:** [ADR-001](ADR-001-monolito-modular.md); especificação técnica v1.0, §2, §5.1,
  §14 (D15) e §19.1

## Contexto

O modelo de linguagem propõe ações; ele não pode ser a autoridade que decide executá-las (§2,
§5.1). Ferramentas vão ler agenda e e-mail e, depois, escrever, enviar e excluir. Conteúdo vindo de
páginas, documentos e e-mails pode tentar instruir o modelo (prompt injection, §14).

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| Executar tudo o que o modelo pedir | Viola §2 ("LLM propõe, Policy Engine valida") |
| Validação de argumentos pelo fornecedor (`strict`) | Depende do fornecedor e não cobre risco nem aprovação |
| Policy dentro de cada ferramenta | Espalha a regra; uma ferramenta nova poderia esquecer a checagem |

## Decisão

Toda chamada de ferramenta passa, nesta ordem, por (D15):

1. **Registro:** o Tool Registry (`neoson_core.tools`) confere se a ferramenta existe e valida os
   argumentos contra o schema dela. O schema enviado ao modelo é gerado do mesmo modelo de
   argumentos, então não há duas definições divergentes. Pedido inválido volta ao modelo como
   erro, sem executar nada.
2. **Policy Engine** (`neoson_core.engine`, separada do registro): decide pelo nível de risco
   declarado pela ferramenta.

   | Risco | Decisão padrão (`required_for_sensitive`) |
   |---|---|
   | `read` | permitir |
   | `sensitive_write` (enviar, excluir, alterar agenda, escrever fora da área autorizada) | pedir aprovação |
   | `forbidden` | negar sempre |

   Com `NEOSON_APPROVAL_MODE=always`, toda ferramenta exige aprovação.
3. **Aprovação:** vale para uma ação concreta: o nome da ferramenta e o hash dos argumentos
   canônicos. Expira, é usada uma vez e não serve para outros argumentos (§14).
4. **Execução:** com o prazo restante da execução; exceção da ferramenta vira resultado de erro,
   sem detalhes internos.

Quando alguma chamada de um passo exige aprovação, nenhuma chamada desse passo é executada: a
execução fica em `waiting_approval` e o canal recebe `needs_approval`.

Os resultados das ferramentas voltam ao modelo como mensagens `tool`, tratadas como dados, nunca
como instruções do sistema. O resumo enviado ao modelo é texto curto e seguro; os dados
estruturados ficam no resultado.

## Implicações

- Cada ferramenta nova declara nome, descrição, modelo de argumentos e nível de risco; sem nível
  de risco, ela não entra no registro.
- Escrever na memória curada do próprio NeoSon (`memory_save`, `memory_forget`) é escrita dentro
  da área autorizada: entra como `read`. Os fatos ficam visíveis no vault para revisão, podem ser
  esquecidos, e a ferramenta só deve salvar o que o usuário pediu explicitamente.
- O gateway oferece consulta, decisão e retomada autenticadas de ações persistidas. A API
  de mensagens v1 continua devolvendo `needs_approval`; o `run_id` permite descobrir as ações.
- Concessões em memória da Policy Engine continuam úteis em testes isolados. O gateway não
  oferece uma grant global por nome/argumentos: sua autoridade está no registro concreto,
  vinculado à identidade, execução, sessão, contexto e prazo.

## Autorização durável e retomada

Pendências e execução original são gravadas na mesma transação SQLite. Cada pendência
contém uma chamada exata, identidade, validade de 15 minutos, tempo restante e vínculo
à skill aprovada, quando houver. Um checksum confere a estrutura imutável; não constitui
assinatura contra quem controla o banco. Argumentos são privados e ficam somente no banco
e na resposta autenticada de revisão, nunca em eventos, logs ou vault.

A implantação pessoal usa um único proprietário HTTP: todos os portadores do bearer
pertencem à mesma identidade. O cliente não escolhe o proprietário por JSON/header/query.
Rotação do token não muda esse proprietário. Isso não é autenticação multiusuário; separar
usuários exige uma nova composição de identidade e isolamento de sessões. Canais sem
identidade confiável não criam concessões retomáveis. A interface de consentimento do
Telegram fica separada desta API; nenhum texto do modelo ou mensagem “sim” concede permissão.

A decisão humana e a retomada são operações separadas. Antes de executar, o serviço
confere expiração, contexto atual, schema/risco, disponibilidade da ferramenta e a versão,
integridade e permissões atuais da skill. Mudanças invalidam a ação. O tempo de execução
fica limitado ao menor teto entre o remanescente salvo e o servidor atual; quando há
várias ações pendentes no passo, o remanescente é dividido entre elas. Não há nova chamada
ao modelo nem reconstituição de argumentos. Ferramentas não pendentes daquele passo não
são executadas na retomada. O resultado desta operação é o estado da ação; a execução
original continua no histórico como suspensa.

Uma transação confirma `executing` antes do efeito. Concorrentes não podem adquirir a
mesma concessão. Retry de ação `completed` só devolve o estado salvo. Erro/timeout vira
`unknown`; queda após a reserva ou falha ao confirmar pode deixar `executing`. Esses
estados exigem reconciliação e nunca liberam repetição automática. Não se promete efeito
externo exatamente uma vez: a integração futura também precisa usar `ToolContext.action_id`
como chave de idempotência e consultar o fornecedor antes de repetir algo incerto (§10.2).

Procedimento e limites no [runbook de ações](../runbooks/action-approvals.md).
