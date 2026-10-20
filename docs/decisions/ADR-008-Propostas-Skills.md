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

O armazenamento inicial implementa somente criação, consulta e rejeição de pendentes;
os demais estados ficam reservados para a etapa de avaliação/revisão. Não existe setter
genérico de estado. A aprovação futura usa o registro versionado, preserva rollback e
não publica código nem faz commit automaticamente. Custo desconhecido não é custo zero;
regressões por caso e aumento de custo/latência precisam ficar visíveis na revisão.

### Trabalho assíncrono (próximo incremento)

O handler da outbox apenas agenda um job idempotente com a evidência de origem. Um executor
reivindica o job em transação curta, libera a conexão e chama o modelo com orçamento,
tempo limite e quantidade limitada de tentativas. Só depois abre outra transação curta
para validar a base e persistir a proposta. Nunca manter transação SQLite durante rede
ou aguardar a curadoria no caminho da resposta ao usuário.

Jobs têm lease, retry e estado próprios; seus eventos e execuções de fixtures não alimentam
recursivamente a curadoria. Observações são dados não confiáveis, e nenhuma instrução nelas
autoriza publicar uma skill. Geração, comparação, CLI de revisão e visualização de propostas
são incrementos seguintes, não funcionalidades já disponíveis neste armazenamento.

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
