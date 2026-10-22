# Leitura de páginas web (`web_fetch`)

Com a ferramenta ligada, o modelo pode ler o texto de uma página http(s). A decisão de
segurança está no [ADR-018](../decisions/ADR-018-Web-Fetch.md).

## Ligar

```dotenv
NEOSON_WEB_FETCH_ENABLED=true
# Domínios lidos sem aprovação (e seus subdomínios), separados por vírgula
NEOSON_WEB_FETCH_ALLOWED_DOMAINS=docs.python.org,pt.wikipedia.org
NEOSON_WEB_FETCH_TIMEOUT_SECONDS=10
NEOSON_WEB_FETCH_MAX_BYTES=1000000
NEOSON_WEB_FETCH_MAX_CHARS=6000
```

Reinicie o gateway e o bot. Ligar, desligar ou mudar a lista de domínios muda o hash de
runtime e invalida ações pendentes criadas antes.

## Aprovação

URLs fora da lista não são acessadas sem a sua aprovação. A execução fica em
`needs_approval`, e a revisão autenticada (`GET /api/v1/actions`, ver
[aprovações](action-approvals.md)) mostra a URL completa. Antes de aprovar, confira o
domínio e a query: uma página ou um e-mail pode ter pedido ao modelo que leve dados seus
numa URL. Não coloque na lista domínios onde qualquer pessoa publica conteúdo com
links (encurtadores, fóruns), porque eles servem de ponte para outras URLs.

## O que é bloqueado

- Endereços de rede interna ou reservada (loopback, `10.x`, `192.168.x`, `169.254.x`,
  IPv6 local), mesmo por nome de DNS ou por redirecionamento.
- URLs com usuário e senha, esquemas que não sejam http(s) e portas inválidas.
- Tipos que não sejam HTML, texto, Markdown ou JSON; corpo acima do limite (cortado).

Erros aparecem como "Página não lida: <motivo>", sem repetir o que o servidor
respondeu. Páginas que dependem de JavaScript devolvem pouco ou nenhum texto.

## Validação

`uv run --no-sync python -m pytest -q tests/test_web_fetch.py` cobre a fixação do IP, os
bloqueios, os redirecionamentos, os limites, a aprovação por domínio e a jornada no Agent
Loop, com HTTP e DNS simulados. Com `NEOSON_LIVE_TESTS=1`, um teste lê `example.com`
de verdade, com TLS, e confirma que `localhost` é recusado.
