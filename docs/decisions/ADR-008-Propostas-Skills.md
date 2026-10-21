# ADR-008 — Propostas de melhoria de skills

- **Status:** accepted
- **Data:** 2026-10-20
- **Referências:** [ADR-007](ADR-007-Skills.md), [ADR-002](ADR-002-Persistencia.md),
  especificação técnica §7.2, §8 (D09) e §8.1.

## Contexto

Execuções e avaliações oferecem evidências para melhorar procedimentos. Essa curadoria
precisa ocorrer sem atrasar a conversa, ampliar permissões ou tornar conteúdo gerado
em instruções aprovadas. Proposta, job de geração e versão ativa são estados distintos.

## Decisão

### Contrato e isolamento

Uma proposta guarda um snapshot completo e imutável, a versão/checksum da base,
versão/checksum do candidato, data, chave idempotente UUID e entre uma e 32 referências
distintas a runs ou avaliações da mesma skill e versão base. Avaliações também devem
corresponder ao checksum da base. IDs inexistentes ou evidências de outra versão são recusados.
O texto das conversas não é duplicado na proveniência.

O primeiro incremento permite apenas alterar `SKILL.md` e aumentar a versão semântica.
Todos os demais metadados, ferramentas, permissões, exemplos e fixtures permanecem iguais;
nenhum arquivo pode ser adicionado ou removido. O loader existente valida formato,
seções, tamanho e checksum. Uma mudança apenas de versão não é uma melhoria candidata.
Esses limites também se aplicam a propostas manuais; scripts e ferramentas novas seguem
o fluxo normal de código e revisão.

`skill_proposals`, `skill_proposal_runs` e `skill_proposal_evaluations` são tabelas
separadas de `skills`. O índice do motor continua lendo somente versões ativas aprovadas.
Não há escrita na biblioteca em disco, ferramentas de autoedição nem aprovação implícita.
Conteúdo gerado pode repetir dados privados: snapshots ficam no banco local e não são
exportados integralmente ao vault, logs ou eventos.

### Persistência e repetição

A criação verifica a base ativa, valida candidato e fontes, grava snapshot, referências
e evento `neoson.skill.proposal.created` na mesma transação. A versão candidata não pode
existir no catálogo nem estar reservada por outra proposta. Uma versão rejeitada continua
reservada: uma revisão do conteúdo exige outra versão.

Repetir o mesmo `request_id` com candidato, base e referências idênticos retorna a proposta
existente, inclusive depois de rejeição ou mudança posterior da base. Reutilizar a chave
com outros dados é conflito. A base explicitamente informada impede que um resultado lento
seja atribuído à versão que ficou ativa enquanto a geração estava em andamento.
Leituras conferem identidade e checksum da cópia armazenada. O evento contém só IDs e versão.

### Estados e revisão

| Origem | Destino | Condição |
|---|---|---|
| Criação | `pending` | Contrato, base ativa e proveniência válidos |
| `pending` | `rejected` | Rejeição explícita, idempotente |
| `pending` | `evaluated` | Comparação base/candidato nos mesmos casos, evidência válida |
| `pending` ou `evaluated` | `stale` | Base, checksum ou evidência mudou antes da aprovação |
| `evaluated` | `approved` | Revisão explícita e revalidação transacional da base e resultados |
| `evaluated` | `rejected` | Decisão explícita na revisão |

A CLI `neoson-skills proposal` oferece listagem, inspeção, avaliação, rejeição e aprovação
explícita. Não existe setter genérico de estado. A aprovação usa o registro versionado,
preserva rollback e não publica código nem faz commit automaticamente.

### Avaliação comparativa e aprovação

Base e candidato executam separadamente os mesmos casos da base aprovada, em bancos
descartáveis e com ferramentas simuladas. O candidato não é ativado na origem para ser
avaliado. A verificação do contrato impede alterar fixtures, exemplos ou permissões,
inclusive se alguém recalcular o checksum do candidato.

`skill_proposal_comparisons` guarda os dois relatórios sem transcrições, a revisão do
catálogo e um hash de integridade da evidência. Comparam-se resultados por ID de caso,
tokens de entrada/saída/cache, custo conhecido e duração. Uma taxa global igual não
esconde regressão: um caso aprovado na base e reprovado no candidato é regressão.
A aprovação exige todos os casos do candidato aprovados e nenhuma regressão.

Iniciar outra avaliação volta o estado a `pending` e troca um token de revisão; a evidência
anterior não autoriza aprovação enquanto essa avaliação estiver em andamento. Só a
avaliação iniciada mais recentemente pode publicar resultado. Se falhar, é preciso
avaliar de novo; a evidência antiga não é restaurada automaticamente.

A aprovação exige o UUID da comparação atual. Na mesma transação, revalida estado,
snapshot, fixtures, proveniência, evidência, configuração e base ativa. A revisão do
catálogo é o hash dos IDs dos eventos de aprovação/rollback/retirada da skill: trocar
a base e voltar por rollback também invalida a avaliação antiga. O contexto contém
apenas versão do avaliador, modelo/perfil, orçamento, política de aprovação, ferramentas
registradas e preços; não contém caminhos ou credenciais. Alterações no contrato do
avaliador exigem atualizar sua versão de contexto, além de novas avaliações.

Custo desconhecido permanece desconhecido e exige `--accept-unknown-cost`. Aumentos de
entrada, saída, custo conhecido ou duração exigem `--accept-resource-increase`. Essas
opções reconhecem os dados mostrados; não permitem contornar regressão, caso reprovado,
evidência inválida ou base alterada. As confirmações ficam em `skill_proposal_approvals`.

Versões reservadas por propostas não podem usar o comando comum `approve <pasta>` para
pular a revisão. Registro da versão ativa, estado da proposta, auditoria e eventos são
atômicos. Repetir a mesma aprovação é idempotente se a candidata ainda estiver ativa;
depois de rollback, repetir o comando não reativa a candidata. A biblioteca no Git
continua inalterada. Procedimento operacional em
[Revisão de skills](../runbooks/revisao-skills.md).

### Trabalho assíncrono

O handler da outbox apenas agenda um job idempotente com a evidência de origem. Um executor
reivindica o job em transação curta, libera a conexão e chama o modelo com orçamento,
tempo limite e quantidade limitada de tentativas. Só depois abre outra transação curta
para validar a base e persistir a proposta. Nunca manter transação SQLite durante rede
ou aguardar a curadoria no caminho da resposta ao usuário.

Jobs têm lease, retry e estado próprios; seus eventos e execuções de fixtures não alimentam
recursivamente a curadoria. Observações são dados não confiáveis, e nenhuma instrução nelas
autoriza publicar uma skill. Geração, comparação, CLI de revisão e visualização de propostas
são etapas separadas. O agendamento e a geração estão disponíveis no executor opcional
`neoson-curator`; comparação e revisão são comandos explícitos. A visualização das
propostas no vault é um incremento seguinte.

`curation_jobs` guarda a referência à fonte e à base, tentativas, retry, lease e claim UUID.
`curation_attempts` reserva orçamento antes de cada chamada e guarda só métricas/códigos.
Só falhas de runs com skill aprovada (fora de canais de avaliação/curadoria) e avaliações
reprovadas da mesma versão/checksum são elegíveis. Há um job ativo por skill e teto diário;
sinais excedentes não são acumulados indefinidamente. O contexto não inclui transcrições.

O executor solicita somente JSON com `instructions`; versão e demais arquivos são montados
localmente e passam pelo contrato de propostas. A proposta e a conclusão do job são gravadas
na mesma transação, após conferir claim e validade do lease. A base ativa é revalidada nessa
transação. Cancelamento deixa lease/reserva persistidos para recuperação, sem publicar
resultado atrasado. SDK não faz retries ocultos nesse executor.

Limites configuráveis cobrem caracteres de entrada, tokens de saída, tempo, tentativas,
jobs e reserva diária de tokens. Teto USD opcional exige preços conhecidos; reservas
monetárias persistem e valores desconhecidos não viram zero. São custos estimados com os
preços configurados. Toda falha de provedor usa código genérico sem expor sua mensagem.

## Alternativas consideradas

- Inserir candidatos inativos em `skills`: confunde proposta com versão aprovada e enfraquece
  a fronteira de confiança do catálogo.
- Alterar a biblioteca durante a geração: mudaria arquivos versionados antes da revisão.
- Aceitar fixtures ou permissões novas automaticamente: permitiria reduzir a exigência dos
  testes ou ampliar autoridade para aparentar uma melhora.
- Executar a chamada LLM dentro do handler transacional: bloquearia escritores do SQLite
  durante rede, além de misturar lease da outbox com trabalho demorado.

## Implicações

- Migration aditiva; nenhum run, versão ou dado existente é reescrito.
- Snapshots e fontes são locais e podem conter conteúdo privado; backup protege o banco.
- Chaves estrangeiras preservam fontes e base. Eliminar essas evidências exige uma política
  explícita futura; o fluxo atual não as apaga.
- Propostas conservadoras podem ser recusadas mesmo sendo úteis; ampliar o contrato exige
  outra decisão e validação, preservando a aprovação humana para alterações sensíveis.
