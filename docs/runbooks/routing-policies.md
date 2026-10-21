# Revisão e rollback de políticas de roteamento

Políticas são snapshots de dados: palavras literais, intenções, complexidade por tamanho
e versão. Não admitem código, regex fornecidos, ferramentas, permissões ou execução de
subagentes. O modo continua `direct`; o perfil/modelo vem da configuração do servidor.
As regras embutidas `rules-v1` permanecem a referência inicial e o fallback.

## Preparar uma avaliação

Faça backup do banco de conversas e vault antes de atualizar uma instalação existente:

```bash
neoson-backup create
uv run --no-sync python -m neoson_gateway.router_policy status
```

O status informa versão/checksum, revisão do catálogo e hash do runtime. A primeira
abertura inicializa apenas a política embutida, com comportamento equivalente ao anterior.
Não cria nem aprova candidata. O banco de políticas usa o banco de conversas configurado;
`--db CAMINHO` antes do subcomando permite testar em outro banco.

Crie um plano local seguindo o [benchmark](router-lab.md), com um novo snapshot em
`promotion.snapshot`. Por exemplo:

```json
{
  "version": "rules-v2",
  "rules": [{"intent": "memory", "keywords": ["recordar", "recupere"]}],
  "long_request_chars": 600,
  "short_complexity": "low",
  "long_complexity": "medium",
  "default_intent": "conversation"
}
```

Esse objeto é apenas o snapshot; o plano completo precisa de todos os outros campos.
Regras extras têm precedência, depois são usadas as regras embutidas. Palavras são
normalizadas e escapadas, sem executar expressões arbitrárias. Uma versão aprovada é
imutável; conteúdo corrigido exige nova versão.

`promotion` também exige `base_revision` e `runtime_hash` do status atual, além de `limits`:

```json
{
  "min_cases": 8,
  "min_quality": 1.0,
  "min_routing_accuracy": 0.9,
  "max_cost_ratio": 1.0,
  "max_tokens_ratio": 1.1,
  "max_latency_ratio": 2.0,
  "max_age_seconds": 3600
}
```

Esses limites são escolhas explícitas do experimento, não autorização de aprovação.
O gate exige split `test`, pelo menos oito casos e todas as oito categorias do corpus,
100% das expectativas da candidata satisfeitas, zero falha de segurança/regressão e
acurácia dentro do limiar e não inferior ao baseline. Custo, entrada/saída e duração são
comparados aos tetos relativos; os orçamentos absolutos do plano também são conferidos.
As duas versões precisam ter consumo/custo completos, e o custo do baseline deve ser positivo.

O modo precisa ser `openai`; evidência simulada, antiga ou feita com outro contexto não
autoriza promoção. Perfil/modelo e esforço de raciocínio da candidata precisam corresponder
ao servidor atual. Todos os preços devem corresponder aos preços atuais do servidor,
inclusive os do baseline. O modelo efetivamente retornado deve coincidir exatamente com
o configurado no plano; alias que resolve para outra versão exige explicitar essa versão
e reavaliar. Custo desconhecido não possui opção de bypass.

O hash de runtime inclui modelo/perfil, raciocínio, preços e parâmetros operacionais,
sem chave, caminhos ou credenciais. O benchmark usa sandbox e orçamento de laboratório;
aprovação não amplia limites nem permissões do servidor. Testes com ferramentas simuladas
não aprovam integrações reais, e o corpus pequeno não prova equivalência geral de qualidade.

## Avaliar e revisar

```bash
uv run --no-sync python -m neoson_gateway.router_lab datasets/router/seed-v1.json plano-local.json --db data/router-lab.db --live
uv run --no-sync python -m neoson_gateway.router_policy promote AVALIACAO_UUID datasets/router/seed-v1.json plano-local.json --evidence-db data/router-lab.db --request-id REVISAO_UUID
```

Revise o relatório antes do segundo comando. Ele é a aprovação explícita. O gate
revalida dataset/plano/contexto, hash e idade da evidência, recalcula os agregados por caso
e os preços por tentativa; não confia em um total isolado do relatório. Snapshot, evento
de auditoria e versão ativa são gravados na mesma transação. Não há publicação de código.

Mudança no catálogo, inclusive trocar uma política e voltar por rollback, invalida a
revisão base de uma avaliação anterior. Refazer o benchmark com revisão/contexto atuais
é obrigatório. O avaliador de snapshots usa contexto `router-lab-v2`; resultados antigos
continuam históricos e não são convertidos em evidência nova.

Repetir o mesmo UUID de aprovação com os mesmos dados é idempotente enquanto a decisão
continuar ativa. Depois de rollback, repetir não reativa a política. Conflitos, evidência
inválida ou limites reprovados mantêm a versão ativa intacta. Não há promoção automática.

## Rollback

```bash
uv run --no-sync python -m neoson_gateway.router_policy status
uv run --no-sync python -m neoson_gateway.router_policy rollback --expected-revision REVISAO_ATUAL --request-id ROLLBACK_UUID
```

Restaura a versão aprovada anterior por ordem de aprovação, preservando snapshots e
auditoria. Revisão esperada impede decisão concorrente sobre estado antigo. Repetir o
mesmo UUID, enquanto o rollback continuar atual, é idempotente. Na política inicial não
há uma versão anterior para restaurar.

## Sessões e fallback

No primeiro turno, o motor entrega ao roteador a sessão já resolvida. Snapshot e modelo
são vinculados nesse momento; aprovação e rollback afetam sessões novas. Sessões
existentes continuam com sua versão e modelo, inclusive após reinício ou mudança da
configuração global. Para usar a nova política, inicie nova sessão. O vínculo mantém
também o esforço de raciocínio conhecido; não há troca automática de fornecedor/modelo
quando o provedor falha.

Sessões anteriores ao vínculo recuperam o último modelo persistido e usam regras
embutidas, com `fallback:legacy_session` no log/decisão. O histórico antigo não guardava
esforço de raciocínio: nesse primeiro vínculo legado vale a configuração atual desse
parâmetro. Nenhum texto de conversa é lido para recuperar o modelo.

Snapshot inválido produz `fallback:policy_invalid`, com regras embutidas e o mesmo modelo.
Se o contexto aprovado mudou, sessões novas usam `fallback:runtime_changed` até nova
avaliação/aprovação; o catálogo histórico não é reescrito. Vínculo de modelo inválido
bloqueia a chamada, pois substituir silenciosamente um modelo desconhecido não preservaria
a sessão. Logs usam códigos, sem prompts, chaves ou conteúdo dos snapshots.

Atualize/reinicie o processo do gateway para carregar esta implementação; o catálogo é
consultado a cada vínculo e não exige reinício a cada aprovação. Não inicie dois processos
de polling do Telegram ao mesmo tempo. Nenhuma candidata é aprovada pela instalação.

## Conferir o ciclo no vault

Em um ambiente isolado, use banco de conversas, laboratório e vault descartáveis.
Execute o benchmark, confira a comparação em `Dashboard/Router-Lab.md`, revise e
promova pela CLI, processe os eventos e confira a nova seleção do catálogo. Em seguida,
execute rollback com a revisão atual e processe os eventos novamente: a seleção volta
à anterior, mantendo a avaliação e ambas as decisões no histórico. Uma avaliação com
regressão deve ser recusada pelo gate e não deve produzir evento de promoção.
O [runbook do laboratório](router-lab.md#painel-no-obsidian) descreve os workers e a
reconstrução explícita. Use novas sessões para conferir os efeitos de cada decisão;
não copie o histórico de teste para o banco real. Testes automatizados cobrem esse ciclo
com provedor determinístico e preços fictícios, sem chamadas externas ou gastos reais.
