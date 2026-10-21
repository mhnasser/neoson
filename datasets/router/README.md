# Dataset inicial do Router Lab

`seed-v1.json` contém 24 exemplos sintéticos em português: oito categorias em cada um
dos splits `train`, `validation` e `test`. Nenhum exemplo veio de conversas reais.
Os rótulos descrevem o comportamento desejado, inclusive onde `rules-v1` ainda não atende.

Carregamento local, sem API ou acesso ao banco:

```python
from pathlib import Path
from neoson_core.routing.dataset import load_dataset

dataset = load_dataset(Path("datasets/router/seed-v1.json"))
print(dataset.id, dataset.version, dataset.checksum)
cases = dataset.select("validation")
```

Cada caso contém `id`, `split`, `category`, `text`, `session_model_profile`, `route` e
`outcome`. `route` rotula intenção, complexidade e modo de execução. `outcome` define
estado esperado, trechos exigidos/proibidos, ferramentas exigidas/proibidas e condição
de aprovação (`none`, `required`, `denied`). Espera por aprovação usa `waiting_approval`;
`denied` significa que uma ação não foi autorizada, não que precisa falhar a conversa.

Os trechos são critérios mínimos para o futuro avaliador, não respostas completas nem
prova suficiente de qualidade humana. Ferramentas são símbolos para implementações
simuladas: o corpus não habilita integrações. Falhas descritas no texto precisam ser
injetadas explicitamente pelo futuro runner; aprovação, estado e ferramentas precisam
ser verificados na trilha de execução, não inferidos de uma frase da resposta.

O loader recusa campos extras, expectativas vazias/contraditórias, formatos inválidos,
IDs/entradas repetidos e splits ausentes. Detecta duplicatas após normalização de Unicode,
caixa e espaços; paráfrases exigem revisão manual. Não copie casos entre splits nem ajuste
regras com os exemplos de teste. Limites: 2 MB, 256 casos e 8.000 caracteres por entrada.

O checksum usa o conteúdo validado, com defaults e ordenação canônica; formatação e ordem
dos casos não o alteram. Alterações de dados, rótulos ou critérios exigem nova `version`;
mudanças incompatíveis de formato exigem nova `schema_version`. O corpus pequeno não
sustenta conclusões estatísticas ou alegações de economia em produção.

Baseline, métricas e condições de promoção estão no
[ADR-009](../../docs/decisions/ADR-009-Router-Lab.md). O plano de exemplo
`plan-validation-scripted.json` executa a validação sem rede e sem derivar respostas dos
labels. Instruções e limites no [runbook](../../docs/runbooks/router-lab.md).
A política ativa permanece inalterada; promoção ainda não está implementada.
