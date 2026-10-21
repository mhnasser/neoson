# ADR-009 — Dataset e avaliação de políticas de roteamento

- **Status:** accepted
- **Data:** 2026-10-21
- **Referências:** [ADR-001](ADR-001-monolito-modular.md),
  [ADR-003](ADR-003-Seguranca-Tools.md), especificação §6 e §15.1–15.2, D06 e D16.

## Contexto

O `RulesRouter` atual usa `rules-v1`: reconhece calendário/e-mail, estima porte pelo
comprimento e mantém o perfil principal fixo. Não mede qualidade final e não implementa
recuperação ou delegação apenas por recomendar uma rota. Uma avaliação precisa expor
esses limites, sem rotular os exemplos a partir das respostas da implementação.

## Decisão

### Dataset público e contrato

O laboratório usa JSON versionado, carregado por `routing.dataset.load_dataset`.
Não participa da inicialização do gateway ou do roteador ativo. O primeiro corpus é
`datasets/router/seed-v1.json`, exclusivamente sintético: 24 casos, oito categorias em
cada split (`train`, `validation`, `test`). É uma amostra de engenharia, sem pretensão de
representar estatisticamente o uso real ou demonstrar superioridade de um modelo.

Cada caso separa entrada, rótulos de rota (intenção, complexidade e modo) e expectativas
observáveis de execução (estado, trechos exigidos/proibidos, ferramentas e aprovação).
`session_model_profile`, quando presente, representa uma sessão já estabelecida e exige
que o perfil principal continue estável. Perfis e ferramentas são nomes simbólicos;
não concedem acesso a serviços. Categorias são conversa, ferramentas, memória,
complexidade, ambiguidade, falha, permissão e adversarial.

Os contratos são estritos, imutáveis, com campos extras proibidos e limites de tamanho.
O loader limita a leitura a 2 MB e 256 casos, recusa chaves JSON repetidas, estados,
versões e rótulos inválidos, expectativas contraditórias e ausência de qualquer split.
IDs e entradas devem ser únicos em todo o corpus. A detecção de entrada duplicada usa
NFKC, casefold e espaços normalizados, inclusive quando o perfil da sessão difere.
Isso não detecta paráfrases nem vazamento semântico: a revisão humana dos splits continua
necessária. Nunca treinar/ajustar regras com o split de teste.

`schema_version` identifica o formato; `version` identifica a revisão semântica do corpus.
O SHA-256 inclui todos os campos validados e seus defaults, chaves ordenadas e casos por
ID, em JSON UTF-8 canônico. Formatação, escapes Unicode e ordem dos casos não mudam o hash;
conteúdo ou expectativas mudam. Qualquer alteração publicada exige nova versão. Mesmo
ID/versão com hashes diferentes nunca pode ser tratado como o mesmo dataset na persistência
do laboratório. Não há registro persistido de datasets neste incremento.

### Baseline e reprodução

O baseline será `single-model-v1`: um único perfil explicitamente escolhido para todos os
casos, sem classificador pago. O perfil deve ser resolvido para fornecedor/modelo e
parâmetros concretos antes do benchmark; `advanced` é apenas um nome simbólico, sem
pressupor modelo, preço ou credencial. A primeira candidata a medir é `rules-v1`.

Ambos usam o mesmo dataset/hash/split, critérios de qualidade, orçamento, relógio,
ferramentas simuladas, cenário de falhas e política de aprovação. Seeds, versões do
avaliador/política, modelos, parâmetros e preços ficam no contexto do experimento.
Instâncias de sessão e bancos são descartáveis, sem importar mensagens reais. Casos de
falha exigem injeção explícita no executor: a frase do corpus não prova que houve erro de
provedor, ferramenta ou memória. O executor e os cenários controlados serão implementados
separadamente; carregar o dataset não executa nenhuma ferramenta ou API.

### Métricas e custo/cache

- Acurácia de roteamento: acerto por intenção/complexidade/modo, acerto conjunto e matriz
  de confusão. A categoria do caso serve para estratificar resultados, não é a intenção.
- Qualidade final: critérios de resposta, estado e ferramentas por caso; reportar também
  violações de segurança e respeito a aprovação/negação. Classificação correta não implica
  tarefa concluída, e o modo recomendado não concede permissões.
- Custo total: todas as chamadas do cenário, classificação, retries e auxiliares, com
  tokens de entrada/saída/cache separados. Custo desconhecido permanece desconhecido,
  nunca zero. Custo da própria avaliação deve ser identificado separadamente e incluído
  no total do experimento, sem esconder despesas em estimativas de economia.
- Cache: usar tokens cacheados reportados pelo provedor sobre entrada elegível. Manter
  sessão/modelo estáveis; ausência de dado não é cache miss medido. Latência e escalonamento
  incluem todas as etapas e tentativas.
- Economia: `(custo_baseline - custo_candidata) / custo_baseline`, apenas com baseline
  positivo, cobertura completa e condições comparáveis. Mostrar qualidade junto ao custo;
  não extrapolar métricas de mocks para preços, cache ou qualidade em produção.

### Promoção explícita

A avaliação gera evidência, não ativa uma política. A promoção exigirá revisão explícita,
snapshot versionado e comparação íntegra no split de teste, com dataset, política e
contexto atuais. Limites numéricos de custo, tokens, latência e acurácia mínima serão
explícitos no plano do benchmark e farão parte do seu hash, sem defaults permissivos.
Condições obrigatórias: zero violação de segurança/aprovação, zero regressão por caso de
qualidade final, qualidade agregada e acurácia de rota dentro dos limites definidos e
recursos dentro dos tetos aceitos. Falha, dado ausente ou evidência incompatível impede
promoção; não é convertida em aprovação automática. Resultado sem custo conhecido não
autoriza alegação de economia nem um gate dependente de custo.

Modelo principal permanece estável nas sessões existentes. Troca de perfil, fallback e
impacto em cache precisam ser explícitos e observáveis. Aprovação e rollback preservarão
auditoria; regras novas continuam sujeitas a revisão normal de código, sem autoedição.
Laya ou outro classificador pode ser comparado depois, sem dependência estrutural do motor.

## Alternativas e consequências

Derivar rótulos das regras atuais esconderia erros. Conversas reais como corpus público
criariam risco de privacidade e não são utilizadas. Um classificador treinado agora
adicionaria custo e dependência antes de existir evidência comparável.

O incremento inicial forneceu contrato, corpus e validação. O executor subsequente
compara o baseline com regras em bancos descartáveis e persiste apenas contexto público
e métricas em `eval_runs`, com reserva idempotente antes da rede. Falhas injetadas são
declaradas no plano, independentemente dos labels; retries sem consumo informado não
viram custo zero. O modo simulado não autoriza estimativa de economia. Procedimento e
limites no [runbook](../runbooks/router-lab.md).

O registro de políticas usa snapshots estritos de dados, sem código ou permissões,
com revisão transacional, auditoria e rollback. O gate exige split de teste, cobertura
mínima de categorias, qualidade integral, modelos/preços/contexto atuais e limites de
recursos explícitos. Agregados são recalculados dos casos e custos das tentativas;
evidência desconhecida, incompatível, antiga ou alterada não possui bypass.

O contexto `router-lab-v2` inclui snapshot/limites/revisão e impede reutilizar resultados
anteriores para promoção. Revisão do catálogo inclui o histórico de decisões, de modo
que rollback não restaura validade de uma avaliação velha. A cópia aprovada é conferida
contra seu checksum e a auditoria de aprovação, não apenas contra JSON válido.

A composição do gateway usa vínculos persistidos de snapshot/modelo por sessão. Novas
aprovações e rollback afetam sessões novas; modelo conhecido permanece estável nas
existentes. Fallback de regras é explícito e não seleciona outro modelo. Configuração
nova incompatível com a aprovação usa a política embutida até reavaliação; vínculo de
modelo inválido bloqueia execução. Nenhuma ferramenta/permissão do motor é ampliada.
Procedimento em [Revisão de políticas](../runbooks/routing-policies.md).

O dashboard do laboratório continua separado. O registro não publica software nem
altera políticas por geração automática. O conjunto restrito continua com execução
direct; classificação de recuperação/delegação não implementa subagentes.
