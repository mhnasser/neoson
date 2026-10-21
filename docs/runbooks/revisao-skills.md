# Revisão de propostas de skills

As propostas ficam no banco local, separadas das versões aprovadas. Avaliar não ativa a
proposta. Os comandos abaixo usam o ambiente configurado; substitua `PROPOSTA_UUID` e
`COMPARACAO_UUID` pelos identificadores mostrados pela CLI.

## Inspecionar

```bash
neoson-skills proposal list
neoson-skills proposal inspect PROPOSTA_UUID
neoson-skills proposal inspect PROPOSTA_UUID --diff
```

A inspeção comum mostra estado, versões, checksums e última comparação, sem instruções
ou transcrições. `--diff` mostra a diferença das instruções com a base original;
controles de terminal são escapados. Revise esse conteúdo localmente antes de aprovar.
Propostas geradas podem conter dados privados; não publique sua saída integral.

## Avaliar

```bash
neoson-skills proposal eval PROPOSTA_UUID
```

Usa o modelo configurado e pode consumir a API duas vezes por caso: uma execução da base
e outra do candidato. Cada caso usa um banco descartável, sem executar as implementações
reais das ferramentas. As fixtures são as mesmas da base aprovada.

O relatório mostra PASS/FAIL por caso, tokens, custo e duração de cada versão, regressões
e aumentos de recursos. Custo sem preço configurado aparece como desconhecido. O retorno
é 0 quando todos os casos do candidato passam sem regressão; 1 quando o resultado é
reprovado ou ocorre erro. Mesmo com retorno 0, nenhuma aprovação é feita.

## Aprovar ou rejeitar

```bash
neoson-skills proposal approve PROPOSTA_UUID --comparison COMPARACAO_UUID
neoson-skills proposal reject PROPOSTA_UUID
```

Use o UUID da comparação atual exibido no relatório. Se houver custo desconhecido,
é preciso reconhecer isso com `--accept-unknown-cost`; se houver aumento de tokens de
entrada/saída, custo conhecido ou duração, com `--accept-resource-increase`. Por exemplo:

```bash
neoson-skills proposal approve PROPOSTA_UUID --comparison COMPARACAO_UUID --accept-unknown-cost --accept-resource-increase
```

As opções não substituem os testes e não contornam os bloqueios abaixo. A aprovação é
gravada com as confirmações, ativa a versão no registro e conserva a base anterior.
Não altera arquivos da biblioteca nem publica commits. Propostas pendentes ou avaliadas
podem ser rejeitadas; repetir a rejeição é idempotente.

| Bloqueio | Próxima ação |
|---|---|
| Sem comparação ou reavaliação em andamento | Concluir uma avaliação válida |
| Regressão ou caso reprovado | Rejeitar e gerar outra versão candidata corrigida |
| Configuração/modelo/preços alterados | Reavaliar com a configuração atual |
| Evidência alterada | Investigar a integridade e obter nova avaliação |
| Base, checksum ou revisão do catálogo alterada | Criar nova proposta a partir da base atual |
| Comparação antiga substituída | Inspecionar e selecionar a comparação atual |

Durante reavaliação, o resultado anterior não autoriza aprovação. Uma avaliação atrasada
não substitui a mais recente. O comando comum `neoson-skills approve <pasta>` recusa
versões reservadas por propostas.

## Rollback

```bash
neoson-skills rollback SKILL_ID
```

Reativa a versão aprovada anterior, sem apagar histórico. Depois disso, repetir a aprovação
da proposta não reativa a candidata. Uma troca de base seguida de rollback também exige
nova evidência; não torna válida uma comparação anterior à troca.
