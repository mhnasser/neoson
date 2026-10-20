# ADR-007 — Skills: formato, aprovação e execução

- **Status:** accepted
- **Data:** 2026-10-20
- **Referências:** [ADR-003](ADR-003-Seguranca-Tools.md); especificação técnica v1.0, §7 (D07,
  D08), §8.1, §11, §14 e §20

## Contexto

Uma skill é um procedimento reutilizável que entra no contexto do modelo (§7). Como passa a
orientar o modelo, ela é uma forma de instrução: uma skill alterada ou maliciosa pode mudar o
comportamento do NeoSon ou tentar usar ferramentas que não deveria (§20). Ela precisa de versão,
aprovação, rollback e medição de resultado, sem que o motor publique nada sozinho.

## Alternativas consideradas

| Alternativa | Por que não |
|---|---|
| `status: approved` dentro do `metadata.yaml` | A própria skill declararia a sua aprovação; um commit bastaria para publicar |
| Ler a skill do disco a cada execução | Editar um arquivo mudaria o comportamento sem aprovação e sem versão para voltar |
| Escolher a skill por palavras-chave e injetá-la no prompt | Falsos positivos e um prompt diferente a cada pedido, o que desfaz o cache (§6) |
| Skills como plugins Python | Executariam código com o acesso do processo; §7 exige permissões limitadas |

## Decisão

**Formato.** Cada skill é uma pasta `skill_library/<id>/` versionada no Git:

| Arquivo | Conteúdo |
|---|---|
| `SKILL.md` | procedimento; seções obrigatórias `Quando usar`, `Passos`, `Restrições` e `Saída`; recomendadas `Pré-condições`, `Ferramentas`, `Validação` e `Falhas`; até 8000 caracteres |
| `metadata.yaml` | `id` (igual ao nome da pasta), `version` (semântica), `description`, `triggers`, `allowed_tools`, `requires_approval`, `risk_level` (`low`, `medium`, `high`), `owner` |
| `examples/`, `tests/fixtures/` | opcionais: exemplos e casos de avaliação |

Só arquivos de texto (`.md`, `.yaml`, `.yml`, `.json`, `.txt`). Scripts de skill ficam para
quando houver sandbox; até lá o validador os recusa. O `metadata.yaml` é lido com
`yaml.safe_load`. As `allowed_tools` precisam existir entre as ferramentas registradas no gateway.

**Identidade.** Uma versão é o par `id` + `version` com um checksum SHA-256 de todos os arquivos
da pasta. As quebras de linha são normalizadas antes do cálculo, para que o checksum seja o mesmo
no Windows e no Linux.

**Aprovação.** O estado não fica no arquivo. Uma versão só fica ativa por um comando humano
(`neoson-skills approve`), que valida a pasta e grava no SQLite uma cópia do conteúdo aprovado com
o checksum, junto com o evento `neoson.skill.approved`. Reaprovar a mesma versão com outro
conteúdo é recusado: mudança exige número novo. O rollback reativa a versão aprovada anterior e
nada é apagado. Propostas automáticas (F4) entram como candidatas e também dependem de aprovação.

**Execução.** O prompt de sistema traz um índice das skills ativas (id, descrição e gatilhos), que
só muda quando o catálogo muda. O modelo pede a skill com a ação `skill_load`. O loop inclui a
cópia aprovada como instrução de sistema, porque é conteúdo aprovado pelo usuário, ao contrário
do resultado de uma ferramenta. A partir daí:

- só as `allowed_tools` da skill podem ser chamadas naquela execução;
- com `requires_approval: true`, toda ferramenta da execução exige aprovação;
- a Policy Engine continua valendo: uma skill só restringe, nunca amplia permissões;
- uma skill por execução.

**Medição.** Cada execução que carrega uma skill grava `skill_executions` (run, skill, versão,
resultado) na mesma transação do run. As avaliações com fixtures registram casos aprovados,
tokens, custo e latência por versão (§8.1).

As fixtures de referência são YAML em `tests/fixtures/`, incluídas no checksum e na cópia
aprovada. Cada arquivo declara `id`, `input`, `now` com fuso, `tools` (resumo, dados, estado
e argumentos exatos opcionais) e `expect` (estado, ferramentas obrigatórias/proibidas e
textos obrigatórios/proibidos). A avaliação usa o modelo configurado e os schemas/riscos
das ferramentas, com as respostas da fixture. Nenhuma implementação real de ferramenta
é executada; cada caso usa um banco temporário com apenas a skill avaliada.

`skill_evaluations` guarda versão/checksum, métricas e motivos objetivos, sem transcrições
ou conteúdo das respostas. A gravação gera `neoson.skill.evaluated` na mesma transação.
O custo só é conhecido se houver preço configurado para todos os modelos envolvidos.

**Painéis.** O worker exporta agregados por versão em `Dashboard/Skills-Learning.md` e uma
nota por skill ativa em `Skills/Ativas/`. As execuções usam os tokens do run e o custo de
`usage_records`; avaliações ficam separadas e a última é selecionada pela versão e checksum.
A taxa de sucesso divide execuções concluídas pelo total, incluindo falhas e esperas por
aprovação. Valores sem preço continuam desconhecidos; somas parciais indicam quantas
execuções estão sem custo. Rollback atualiza a nota ativa; retirada remove apenas a nota
gerada, mantendo as métricas históricas no painel. Nenhuma instrução, fixture ou conversa
é exportada. A escrita é atômica por arquivo e repetições sem mudança não regravam notas.

## Implicações

- Novo adaptador `neoson_core.skills`, independente dos outros adaptadores (ADR-006). A porta
  `SkillRegistry` fica no motor.
- `pyyaml` passa a ser dependência de execução do `neoson-core`.
- Alterar uma skill é uma mudança de código normal (PR e CI), seguida da aprovação local da
  versão nova.
- Scripts de skill e o sandbox para eles ficam para uma decisão futura.
