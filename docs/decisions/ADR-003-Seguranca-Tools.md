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
- O fluxo HTTP para conceder aprovações fica para uma tarefa futura; a Policy Engine já aceita
  aprovações concedidas.
- Ferramentas de escrita com efeito externo também precisam de chave de idempotência no worker
  (§10.2), definida quando a primeira delas existir.
