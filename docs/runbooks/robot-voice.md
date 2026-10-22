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

## Validação

`uv run --no-sync python -m pytest -q tests/test_robot_voice.py` cobre ativação, estados,
privacidade, cancelamento, desconexão, parsing limitado, limpeza, estado atômico e
reinício, com cliente/gateway/loop reais e dispositivos/provedores fake. Transporte de
teste é no processo. Não comprova reconhecimento, fala, rede da Pi ou hardware reais.
Consulte o [inventário](../raspberry-pi-os.md); drivers, volume, GPIO e validação física
continuam separados. Não há alteração de hardware necessária para a simulação.
