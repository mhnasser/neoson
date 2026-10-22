# Benchmark de roteamento

O laboratório compara `single-model-v1` (perfil fixo) com `rules-v1` no mesmo split de
um dataset versionado. A execução é explícita e não ativa políticas. Cada caso/versão usa
banco e sessão descartáveis; somente métricas e proveniência ficam no banco do laboratório.
As ferramentas são simuladas e não acessam calendário, e-mail ou memória reais.

## Executar sem rede

Na raiz do workspace:

```bash
uv run --no-sync python -m neoson_gateway.router_lab datasets/router/seed-v1.json datasets/router/plan-validation-scripted.json --db data/router-lab.db --request-id 00000000-0000-4000-8000-000000000001
```

O plano de exemplo usa o split de validação, relógio fixo e provedor roteirizado de resposta
constante. Ele não lê os critérios esperados para construir respostas. Portanto, várias
tarefas falham: isso comprova o mecanismo e expõe limitações, sem demonstrar qualidade de
um modelo. Tokens do modo `scripted` são simulados e nunca autorizam alegação de economia.

Repetir o mesmo UUID com os mesmos dados devolve exatamente o relatório salvo, sem novas
chamadas. Para uma nova medição, use outro UUID. Mesmo UUID com outro contexto é conflito;
reserva interrompida ou em andamento não é retomada automaticamente, evitando cobrança
duplicada. Identificadores, versão e checksum do dataset também precisam permanecer
consistentes. Correções do corpus exigem nova versão.

## Plano e cenários

O plano JSON inclui checksum/split do dataset, perfis resolvidos para modelos/preços,
perfil do baseline e da candidata, relógio, cenários e orçamento. Deve haver um cenário
para cada caso do split, sem sobras. Perfis de sessões existentes também precisam estar
declarados; prevalecem sobre a seleção inicial das duas políticas, mantendo o modelo fixo.
Isso simula o vínculo de perfil, sem importar histórico real ou criar aquecimento artificial
de cache. Recuperação/delegação são rótulos de rota; recomendar `delegate` não executa
subagentes. A delegação opcional do motor depende de opt-in próprio
([runbook](delegation.md)).

Cada cenário declara separadamente:

- `fault`: `none`, `provider_error`, `tool_error` ou `memory_empty`. A falha precisa ser
  observada durante a execução; descrevê-la no texto não basta para passar o caso.
- `denied_tools`: ferramentas cujo uso foi negado. São bloqueadas pela política, mesmo
  se o modelo insistir. Escritas sensíveis restantes exigem aprovação e ficam suspensas;
  o runner não concede aprovações.
- `replies`: respostas fixas por ferramenta, com `tool`, `summary` e `error`. Não são
  geradas a partir dos labels. Nomes e argumentos de ferramentas são validados; neste
  sandbox os argumentos precisam ser um objeto vazio.

O provedor real recebe apenas entradas e resultados do cenário; não recebe expectativas
ou labels. Estes são usados somente na pontuação. Texto de resposta, estado, ferramentas
executadas e decisões da política são verificados separadamente. Uma frase afirmando que
uma ação foi feita não substitui a execução observada.

## Execução com API real

Crie um plano local a partir do exemplo. Use `mode: "openai"`, configure os modelos de
todos os perfis e os preços USD por milhão de tokens em `prices.input`, `prices.output` e
`prices.cached_input`. Declare `max_total_cost_usd` positivo, no máximo US$ 1 neste
incremento de testes curtos, e mantenha limites adequados de chamadas/tokens/tempo.
Não inclua a chave no plano: a CLI lê a credencial configurada em `neoson.env`.

```bash
uv run --no-sync python -m neoson_gateway.router_lab datasets/router/seed-v1.json plano-local.json --db data/router-lab.db --live
```

`--live` deve corresponder ao modo do plano. Não há seleção implícita de modelo ou preço.
O SDK usa zero retries internos; todas as tentativas do runner, inclusive retries, entram
no relatório. Falha de rede sem usage deixa consumo/custo desconhecidos. Com teto USD,
isso bloqueia novas chamadas, pois não é seguro tratar a tentativa como gratuita.

Antes de chamar, o runner verifica o limite de tentativas e reserva uma estimativa
conservadora de entrada por bytes/framing e saída máxima. A API recebe o limite de saída;
os contadores reais são conferidos depois. Uso acima do orçamento invalida o resultado e
impede novas chamadas. Os preços são estimativas fornecidas na configuração, não uma
garantia de faturamento do provedor. Não ajuste regras com o split de teste.

## Interpretar o relatório

O JSON mostra casos, versões das políticas e proveniência de dataset/plano/contexto.
Por versão há acurácia de intenção/complexidade/modo e conjunta, matriz de confusão,
qualidade final, segurança, tokens/cache, tentativas, custo e duração. Acurácia e sucesso
final são métricas distintas. Falhas de segurança incluem ferramenta proibida executada,
desrespeito à aprovação esperada e presença de trecho explicitamente proibido na resposta.
`quality_regressions` identifica regressões por caso, mesmo
se as taxas globais forem iguais. `escalation_rate` é zero nas duas políticas fixas atuais.
`session_profile_override_rate` conta sessões cujo perfil já estabelecido difere do
default do braço; isso não é escalonamento nem delegação a subagente.

Custo sem preço ou uso não informado aparece como `null`. Tokens conhecidos em tentativas
parciais continuam somados, com `usage_complete: false`. Custo por sucesso só existe com
custo completo e ao menos um caso aprovado. O contrato atual não distingue cache omitido
de zero tokens cacheados; por isso, sem cache positivo observado, a razão fica `null`.
Cache positivo é reportado sobre a entrada conhecida, sem simular hits.

`experiment_cost_usd` inclui os dois braços; `experiment_known_cost_usd` pode ser parcial.
A avaliação local não usa LLM juiz nem classificador pago, então seu overhead monetário
é zero. Duração inclui roteamento, chamadas/retries e ferramentas simuladas.

`estimated_savings_pct` só é calculado em modo real, com custos completos, baseline
positivo, todas as expectativas satisfeitas, nenhuma regressão e sessões estáveis.
Comparações incompletas ou simuladas mantêm o campo `null`. Mesmo uma comparação válida
neste corpus pequeno não comprova equivalência geral de qualidade humana ou ganho em
produção. Promoção/revisão/rollback de políticas ainda são uma etapa separada.

`eval_runs` guarda contexto público, métricas e hash de integridade. Não guarda prompts,
saídas, respostas simuladas ou mensagens de exceções. O modelo retornado pelo provedor é
registrado por hash, junto ao nome configurado no contexto; não é reproduzido como texto
livre. Casos e cenários privados ficam apenas nos arquivos locais e no banco descartável.
Use o banco separado do exemplo; não importe este histórico sintético no banco de conversas.

## Painel no Obsidian

`Dashboard/Router-Lab.md`, acessível pela Home, mostra as últimas 100 avaliações:
baseline/candidata, dataset/split, qualidade, custo/cache/duração e matrizes de confusão.
Inclui a seleção histórica do catálogo e as últimas 100 decisões de promoção/rollback.
Sessões existentes podem manter outra política; o painel não substitui a revisão da CLI.

Configure `NEOSON_ROUTER_LAB_DB_PATH` para o mesmo caminho passado a `--db` no benchmark
(padrão `data/router-lab.db`). O catálogo continua em `NEOSON_DB_PATH`. A projeção abre
as duas fontes em modo somente leitura, sem importar resultados entre elas. Cada fonte
tem sua própria leitura consistente; não há snapshot atômico entre bancos distintos.
Fonte ausente, incompatível ou indisponível é indicada, sem criar/migrar o laboratório.

```bash
neoson-worker --export-vault
neoson-worker --router-lab --once
neoson-worker --once
```

O primeiro comando reconstrói os painéis, inclusive para bancos antigos sem eventos.
Os outros consomem, respectivamente, eventos do laboratório e do banco de conversas.
Para atualização contínua, mantenha os dois workers sem `--once`; se ambos os caminhos
apontarem para o mesmo banco, basta um worker. `NEOSON_METRICS_EXPORT_ENABLED=false`
desliga os handlers de exportação; a reconstrução explícita continua disponível.
Eventos são gravados na mesma transação do benchmark ou decisão, e retries não duplicam
o efeito. O arquivo usa substituição atômica e só muda quando a projeção muda.

`valid` significa integridade/estrutura histórica, não elegibilidade atual para promoção.
`invalid` omite métricas inconsistentes e `none` indica execução sem relatório concluído.
Valores desconhecidos aparecem como `—`; resultados `scripted` nunca mostram economia.
Textos dos casos, conversas, snapshots e credenciais não são exportados.
