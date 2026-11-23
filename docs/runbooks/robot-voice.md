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
e encerra. `--simulate` produz silêncio, playback sem som e display em memória; o
modo real está em "Dispositivos reais" abaixo. O terminal mostra somente estado/aviso. A captura fake envia
`X-Neoson-Simulated: true`; o servidor rejeita se não estiver simulado.

`--gateway-url` escolhe o servidor; `--state-file` escolhe o estado local, padrão
`data/robot-session.json`. Quando a resposta informa sessão, salva somente UUID/versão/
hash da URL, atomicamente. Echo pode não informar sessão. Reinício carrega o UUID daquele
gateway, começa em idle e não repete áudio ou operação. Arquivo corrompido/de outro
gateway impede inicialização; use um arquivo novo para começar outra sessão. Não há
tokens, texto ou áudio nesse arquivo.

## Estados

### Rosto com wake word "Ei NeoSon"

Na sessão gráfica da Pi, com Tk/PipeWire e o ambiente privado do Robot carregado:

```bash
.venv/bin/python -m neoson_robot.touch_voice
# Para conferir em uma janela, sem os gestos da câmera:
.venv/bin/python -m neoson_robot.touch_voice --windowed
```

Diga **"Ei NeoSon"**; quando o display do peito mostrar **Escutando...**, faça o pedido.
O pedido termina com cerca de 1 s de silêncio (no máximo `--seconds`, padrão 12, até
15 s). Sem fala em ~4 s depois da wake word, o robô volta ao repouso sem enviar nada.
O peito mostra `Escutando...`, `Pensando...`, `Consultando...` e `Falando...` e volta
para CPU/RAM no fim; avisos curtos (`Nao consegui`, `Aprove na tela`, `Sem servidor`)
somem sozinhos. O rosto não mostra texto, exceto o painel de aprovação. Esfregar a tela
é carinho; três toques rápidos abrem a câmera e dois voltam; segurar 3 s com o dedo
parado, ou Esc, fecha (gestos no [runbook do rosto](rosto-do-robo.md)). Não execute
outra instância do rosto ou do Robot junto.

Modelos da wake word (fora do Git), em `~/neoson-wake` ou `--wake-dir`:
`melspectrogram.onnx` e `embedding_model.onnx` (openWakeWord v0.5.1) e
`ei_neoson.onnx` (modelo da frase, treinado com vozes sintéticas). `--wake-threshold`
(padrão 0,9) troca acertos por menos disparos falsos: suba se acordar sozinho, desça
se não atender. Medido com vozes sintéticas que o modelo não viu no treino: a 0,9,
~93% de acerto e ~0,8 disparo falso por hora em 10,7 h de áudio comum; a 0,5, ~96% e
~3,6 por hora.

Privacidade: o microfone fica sempre aberto, mas o áudio antes da wake word é
analisado em trechos de 80 ms na própria Pi e descartado (fica no máximo 3 s em
memória). Só a fala depois da wake word vai para o gateway e, de lá, para a
transcrição configurada. Enquanto o robô fala, o microfone é ignorado, para ele não
se acordar com a própria voz. O fim da fala segue o ruído medido antes da wake word
(fala acima do dobro do ruído de fundo); em ambiente muito barulhento o pedido pode ir
até o limite de tempo.

A interface começa offline e só acorda após `health/ready` responder pronto.
Falhas de saúde usam espera de 1, 2, 4, 8, 16 e no máximo 30 segundos; conectada,
confere a cada 10 segundos. O teste de saúde não chama LLM. Wake word dita offline,
com outro turno em andamento ou com o painel de aprovação na tela é ignorada, sem
fila. Uma falha durante a fala nunca reenvia o pedido: é preciso chamar de novo. Um
erro pode ter ocorrido depois de um efeito; confira o canal autenticado antes de
repetir uma ação sensível. Se o microfone parar, o serviço sai com erro e o systemd
o reinicia.

Tk só desenha no thread principal; escuta/rede/reprodução rodam em segundo plano. O
estado vai para `$XDG_RUNTIME_DIR/neoson-robot-status` (texto fixo e validade, nunca
transcrição ou resposta), que o `neoson-chest` lê; um estado vale no máximo 2 min,
então o peito não fica preso se o Robot parar. Instalação: serviço `robot-touch` no
[runbook de serviços](servicos-continuos.md), somente após preparar a sessão gráfica.
Os testes usam microfone, detector e tela simulados; confirmar a wake word no equipamento.

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
é reproduzido. O status, o terminal e o arquivo de estado continuam sem argumentos e sem
o `run_id`.

### Aprovar na tela touch

Com o rosto (`neoson_robot.touch_voice`), logo depois desse turno a tela mostra um painel
por cima do rosto com o que o NeoSon quer fazer: ferramenta, descrição com o destino,
horário em que o pedido vence e cada argumento. Embaixo ficam **Negar** (esquerda) e
**Aprovar** (direita). O painel só mostra ações do pedido que acabou de ser falado
(`GET /api/v1/runs/{run_id}/actions`, filtrado no servidor); ações de outros pedidos
ficam para o canal autenticado.

- **Aprovar** decide e executa a chamada salva uma única vez; **Negar** não executa nada.
  A tela mostra o resultado por alguns segundos ("Pronto, feito.", "Negado. Nada foi
  feito." ou o motivo da recusa) e, se o mesmo pedido tiver outra ação pendente, mostra a
  próxima.
- Toques no primeiro segundo do painel não decidem nada, para evitar aprovação acidental.
  Durante o envio, novos toques são ignorados.
- Enquanto o painel aparece, nenhum toque vira gesto: não grava nova fala, não abre a
  câmera e não fecha o rosto pelo toque longo.
- A tela só aprova o que mostra inteiro. Os botões só aparecem e só respondem quando o
  texto, como o Tk o desenhou, termina acima deles; senão a tela avisa que o pedido não
  cabe e ele fica para o canal autenticado. Caracteres de controle, de formatação e
  separadores Unicode aparecem escapados (`\u2028`, `\u202e`...), e só a quebra de
  linha comum quebra a linha.
- Pedidos vencidos somem. Ações já aprovadas e ainda não executadas ficam para o canal
  autenticado.

O painel usa a mesma API e o mesmo token (`NEOSON_API_TOKEN`) que o Robot já usa para a
voz, então não há permissão nova no servidor. Quem tocar na tela pode aprovar, e quem
estiver perto vê os argumentos: deixe o Robot onde só você tem acesso. Os toques com
posição chegam pela sessão gráfica (Tk); a validação na tela real ainda está pendente.

### Canal autenticado

Fora da tela, a decisão acontece pelo canal autenticado do
[runbook de aprovações](action-approvals.md):

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
stream durante a análise delegada. O caso da tela touch percorre a mesma jornada até o
painel: fala, painel com os argumentos, Aprovar com um único efeito, toque repetido sem
segundo efeito e recusa com token errado.

`uv run --no-sync python -m pytest -q tests/test_robot_voice.py` cobre ativação, estados,
privacidade, cancelamento, desconexão, parsing limitado, limpeza, estado atômico e
reinício, com cliente/gateway/loop reais e dispositivos/provedores fake. Transporte de
teste é no processo. Não comprova reconhecimento, fala, rede da Pi ou hardware reais.
Consulte o [inventário](../raspberry-pi-os.md); drivers, volume, GPIO e validação física
continuam separados. Não há alteração de hardware necessária para a simulação.

## Dispositivos reais (PipeWire)

Na Pi, o Robot pode usar o microfone e a saída de som padrão do PipeWire, pelos comandos
`pw-record` e `pw-play` que já vêm com o sistema (sem dependência Python nova):

```bash
uv run --no-sync python -m neoson_robot.voice_main --device pipewire
uv run --no-sync python -m neoson_robot.voice_main --device pipewire --activate --seconds 5
```

`--simulate` e `--device pipewire` são exclusivos, e um dos dois é obrigatório. Sem
`--activate`, nada é capturado. A captura grava por `--seconds` (até 15 s; o servidor
aceita até `NEOSON_VOICE_MAX_SECONDS`) em PCM16 mono de 16 kHz, num WAV temporário
privado que é apagado depois do envio. A captura termina com SIGINT; se o processo não
sair, é forçado. A reprodução usa outro arquivo temporário e para no cancelamento ou se
passar do tempo esperado. Mensagens de erro são fixas e o terminal mostra só estado e
aviso. Como a captura é real, use o gateway com voz real (`NEOSON_VOICE_PROVIDER=openai`). A
rota real recusa captura simulada; a rota simulada aceita captura real, mas só responde
silêncio, sem reconhecer a fala.

O dispositivo usado é o padrão do PipeWire; confira com `wpctl status` se é o microfone
USB do inventário. Ainda não há detecção de fala nem botão físico: a ativação é o
`--activate` da linha de comando.

## O que ainda não foi validado fisicamente

Os testes acima não substituem estas verificações, que dependem de hardware, contas e
escolhas ainda não feitas:

| Item | Situação | O que falta |
|---|---|---|
| Captura e reprodução na Pi | Adaptadores PipeWire prontos e testados com processos simulados | Rodar na Pi com o microfone e a saída USB do [inventário](../raspberry-pi-os.md) e ouvir o resultado. O ganho do microfone medido é baixo para reconhecimento |
| Display | Tela touch funciona no sistema; LCD 1602 não conectado e I2C do GPIO desligado | Escolher o display e ligar o LCD só depois de resolver a tensão do backpack (alerta no inventário) |
| STT e TTS reais | Adaptador OpenAI opcional, validado ao vivo sem microfone ([voz](voice.md)) | Decidir se usa esse fornecedor (custo e privacidade) ou outro |
| NeoSon Robot na Pi | Não instalado | Instalar o cliente e apontá-lo para o gateway na rede confiável |
| Conta Microsoft | Nenhum consentimento de escrita | Consentimento local `login-write` ([runbook de escrita](personal-writes.md)); efeitos reais só com aprovação de cada ação |

Até essas validações, a jornada da F6 está comprovada somente com adaptadores
simulados. Nenhuma mensagem foi enviada e nenhum efeito real foi executado.
