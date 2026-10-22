# Dataset de recuperação com e sem grafo

`seed-v1.json` contém casos sintéticos em português para comparar a busca só no
histórico de conversas com a recuperação híbrida (histórico e fatos revisados do
grafo). Nomes, projetos e conversas são fictícios; nada veio de dados reais.

O arquivo tem três partes:

- `sessions`: conversas antigas, com data, gravadas no histórico.
- `facts`: fatos do grafo com fonte (`memory_fact:<n>`), data, confiança e estado. Há
  fatos `active`, `pending` e `retracted`: só os ativos podem aparecer na busca.
- `cases`: a consulta de palavras-chave enviada à `memory_search`, a pergunta em
  linguagem natural (modo ao vivo), as fontes esperadas (`session:<chave>` ou
  `memory_fact:<n>`), as fontes proibidas e as palavras que a resposta precisa conter.

A mistura de casos é intencional: alguns só se resolvem pelo histórico, outros só
pelos fatos, outros precisam dos dois, e um não tem resposta. Assim a avaliação mostra
ganho e também regressão ou ruído.

O loader recusa campos extras, chaves e IDs repetidos, fontes desconhecidas, fonte ao
mesmo tempo esperada e proibida, datas sem fuso e valores fora do schema. O relatório
registra o SHA-256 do arquivo: mudar o dataset muda o checksum, e resultados de
versões diferentes não devem ser comparados.

Para rodar, veja a seção de avaliação do
[runbook do grafo](../../docs/runbooks/knowledge-graph.md).
