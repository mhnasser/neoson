# Robot: estados e voz sem hardware

O cliente tem portas de captura, reprodução e display com adaptadores fake. Não abre
microfone, alto-falante ou LCD reais. O display recebe somente estados e avisos fixos.

## Demonstração

Configure o gateway de desenvolvimento com `NEOSON_LLM_PROVIDER=echo`,
`NEOSON_VOICE_PROVIDER=fake` e seu token local. No processo do Robot, configure
`NEOSON_API_TOKEN` e, se necessário, `NEOSON_GATEWAY_URL`. Ele não lê o `neoson.env`
do servidor. Não exponha tokens em argumentos ou histórico de comandos.

```bash
uv run --no-sync python -m neoson_robot.voice_main --simulate
uv run --no-sync python -m neoson_robot.voice_main --simulate --activate
```

Sem `--activate`, fica em idle e não captura nem envia. Com a flag, faz um único pedido
e encerra. `--simulate` é obrigatório: produz silêncio, playback sem som e display em
memória. O terminal mostra somente estado/aviso. A captura fake envia
`X-Neoson-Simulated: true`; o servidor rejeita se não estiver simulado.

`--gateway-url` escolhe o servidor; `--state-file` escolhe o estado local, padrão
`data/robot-session.json`. Quando a resposta informa sessão, salva somente UUID/versão/
hash da URL, atomicamente. Echo pode não informar sessão. Reinício carrega o UUID daquele
gateway, começa em idle e não repete áudio ou operação. Arquivo corrompido/de outro
gateway impede inicialização; use um arquivo novo para começar outra sessão. Não há
tokens, texto ou áudio nesse arquivo.

## Estados

| Estado | Origem |
|---|---|
| `idle` | Inicialização, conclusão ou cancelamento |
| `listening` | Indicador apresentado antes da captura |
| `transcribing` | Evento após validação/admissão do áudio no servidor |
| `thinking` | Pedido em processamento ou atividade do servidor |
| `using_tool` | Entrada observada na execução de ferramenta permitida |
| `speaking` | Reprodução de áudio validado no cliente |
| `error` | Falha de servidor, protocolo, áudio ou dispositivo |
| `offline` | Falha de transporte de rede |

Síntese em andamento não significa speaking. Falta de áudio gera aviso fixo
`audio_unavailable`; `needs_approval` gera `approval_required` e não concede autorização.
Resultados failed e falhas de limpeza são error. Display não recebe respostas,
argumentos, IDs de sessão ou exceções. Esses dispositivos continuam simulados.

## Stream e cancelamento

`POST /api/v1/voice/stream` usa os mesmos headers/WAV/limites da [rota JSON](voice.md).
Retorna `application/x-ndjson` com protocol_version1 e eventos progress (somente estado),
result (resposta de voz final) ou error (status_code/ErrorResponse seguro). Antes do
primeiro progresso, erros mantêm o HTTP correspondente; depois dos headers200, usam
evento terminal error. Buffer de progresso limitado pode descartar estados intermediários;
resultado/erro final é mantido. O cliente limita frames/bytes/prazo e exige terminal único.
Stream truncado, duplicado ou áudio inválido não provocam reprodução. Não há retry automático.

Uma interação por instância do controlador. Cancelamento fecha stream e para dispositivos;
o servidor cancela o trabalho associado e libera a admissão. Callback de cancelamento é
observado entre etapas/eventos; leitura bloqueante pode aguardar timeout de transporte
(35 segundos na CLI). Ctrl+C também cancela. Drivers reais devem limitar captura/
reprodução, observar cancelamento e implementar stop idempotente. Não estão incluídos.
Execução interrompida pode ter efeito incerto; não repetir automaticamente.

## Pedido que exige aprovação

Quando a fala leva a uma ação sensível (`calendar_create`, `email_draft`), o gateway
suspende a execução e o Robot mostra `approval_required`. O áudio do pedido de aprovação
é reproduzido, mas o Robot não decide nada e não mostra argumentos nem o `run_id`. A
decisão acontece no canal autenticado do [runbook de aprovações](action-approvals.md):

1. `GET /api/v1/actions` lista as ações abertas do proprietário, as mais recentes
   primeiro, com ferramenta, argumentos completos e destino.
2. `POST /api/v1/actions/{id}/decision` com `{"approve": true}` ou `false`.
3. `POST /api/v1/actions/{id}/resume` executa a chamada salva uma única vez.

A próxima fala continua a mesma sessão pelo arquivo de estado local. Fechar o stream
durante o processamento cancela a execução no servidor: ela é gravada como falha, com o
uso já medido, e nenhuma ação pendente é criada.

Para montar essa jornada em desenvolvimento, o gateway precisa do motor com OpenAI e
das ferramentas de escrita com `NEOSON_PERSONAL_WRITE_PROVIDER=fake`. A voz fake hoje
exige `NEOSON_LLM_PROVIDER=echo`, que não cria ações. Por isso a jornada completa é
validada pelo teste isolado abaixo, não pela composição do servidor.

## Validação

`uv run --no-sync python -m pytest -q tests/test_voice_approval_journey.py` executa a
jornada voz → análise delegada → aprovação → efeito no sandbox → nova fala na mesma
sessão, com Robot, cliente HTTP, gateway, motor, SQLite e sandbox reais, e fala,
dispositivos e modelo simulados. Também cobre negação sem efeito e cancelamento pelo
stream durante a análise delegada.

`uv run --no-sync python -m pytest -q tests/test_robot_voice.py` cobre ativação, estados,
privacidade, cancelamento, desconexão, parsing limitado, limpeza, estado atômico e
reinício, com cliente/gateway/loop reais e dispositivos/provedores fake. Transporte de
teste é no processo. Não comprova reconhecimento, fala, rede da Pi ou hardware reais.
Consulte o [inventário](../raspberry-pi-os.md); drivers, volume, GPIO e validação física
continuam separados. Não há alteração de hardware necessária para a simulação.

## O que ainda não foi validado fisicamente

Os testes acima não substituem estas verificações, que dependem de hardware, contas e
escolhas ainda não feitas:

| Item | Situação | O que falta |
|---|---|---|
| Captura e reprodução na Pi | Sem adaptador real; só os fakes existem | Implementar drivers com limite de duração e parada idempotente, e testar com o microfone e a saída USB do [inventário](../raspberry-pi-os.md). O ganho do microfone medido é baixo para reconhecimento |
| Display | Tela touch funciona no sistema; LCD 1602 não conectado e I2C do GPIO desligado | Escolher o display e ligar o LCD só depois de resolver a tensão do backpack (alerta no inventário) |
| STT e TTS reais | Nenhum fornecedor selecionado | Escolher fornecedor e custo; a seleção não é implícita |
| NeoSon Robot na Pi | Não instalado | Instalar o cliente e apontá-lo para o gateway na rede confiável |
| Conta Microsoft | Nenhum consentimento de escrita | Consentimento local `login-write` ([runbook de escrita](personal-writes.md)); efeitos reais só com aprovação de cada ação |

Até essas validações, a jornada da F6 está comprovada somente com adaptadores
simulados. Nenhuma mensagem foi enviada e nenhum efeito real foi executado.
