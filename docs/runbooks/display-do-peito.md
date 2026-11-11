# Display do peito: CPU e RAM no LCD 1602

O `neoson-chest` mostra no LCD 1602 do peito do Robot o uso e a temperatura da CPU e o uso da
RAM da Raspberry Pi, atualizados a cada 2 segundos:

```
CPU  12%  41.3°C
RAM   6% 0.5G/8G
```

- **CPU**: uso de todos os núcleos desde a leitura anterior (`/proc/stat`; espera de disco
  conta como ociosa) e temperatura do SoC (`/sys/class/thermal/thermal_zone0`). Sem
  leitura de temperatura, aparece `--°C`.
- **RAM**: memória em uso, ou seja, total menos a disponível (`MemAvailable` de
  `/proc/meminfo`, que já desconta cache), em porcentagem e em GiB. Se a linha não couber
  nas 16 colunas, o total sai primeiro.

O programa é o módulo `neoson_robot.chest_display` e usa só a biblioteca padrão. Ele escreve
direto no `/dev/i2c-N`, então roda tanto pelo pacote quanto copiado sozinho para a Pi.

## Hardware

| Item | Valor |
|---|---|
| Display | LCD 1602 (HD44780) com backpack I2C PCF8574 |
| Barramento | I2C dos pinos GPIO (`/dev/i2c-1`): pino 3 = SDA, pino 5 = SCL |
| Endereço | `0x27` (padrão); alguns backpacks usam `0x3F` |
| Ligação do backpack | P0=RS, P1=RW, P2=E, P3=luz de fundo, P4..P7=D4..D7 |

Antes de ligar o módulo, leia o alerta de tensão em
[Inventário do hardware](../raspberry-pi-os.md#alerta-antes-de-ligar-o-lcd-1602): o backpack
alimentado em 5 V pode puxar SDA/SCL para 5 V.

Pré-requisitos na Pi:

- I2C dos GPIO ligado: `dtparam=i2c_arm=on` em `/boot/firmware/config.txt` (ou
  `sudo raspi-config` → Interface Options → I2C) e reinício.
- Usuário no grupo `i2c` (o padrão do Raspberry Pi OS).

Para descobrir o endereço sem instalar o `i2c-tools`:

```bash
python3 -c "
import smbus
bus = smbus.SMBus(1)
for a in (*range(0x20, 0x28), *range(0x38, 0x40)):
    try: bus.read_byte(a); print(hex(a))
    except OSError: pass"
```

## Executar

Com o NeoSon Robot instalado:

```bash
uv run --package neoson-robot neoson-chest
```

Sem instalar nada, copiando só o módulo para a Pi:

```bash
ssh neoson-pi.local 'mkdir -p ~/neoson-chest'
scp apps/robot/src/neoson_robot/chest_display.py neoson-pi.local:neoson-chest/
ssh neoson-pi.local 'python3 -I ~/neoson-chest/chest_display.py --console --once'
```

| Opção | Efeito |
|---|---|
| `--bus N` | usa `/dev/i2c-N` (padrão 1) |
| `--address 0x3f` | endereço do PCF8574 (padrão `0x27`) |
| `--interval S` | segundos entre leituras (padrão 2, mínimo 0,5) |
| `--console` | mostra as linhas no terminal, sem abrir o I2C |
| `--once` | mede uma vez, mostra e sai |
| `--wait S` | segundos esperando o display responder antes de desistir (padrão 0: uma tentativa; máximo 300) |

Antes de usar o LCD, o programa confere se ele está lá: abre o barramento e lê um byte do
PCF8574 no endereço. Barramento inexistente ou endereço sem resposta contam como **display
ausente**; com `--wait`, tenta de novo a cada segundo até o prazo.

Ao iniciar, a tela mostra `NeoSon` / `monitor...` até a primeira medição. Ao receber
`SIGTERM` ou Ctrl+C, limpa a tela e apaga a luz de fundo.

| Saída | Significado |
|---|---|
| 0 | encerrado normalmente |
| 1 | sem `/proc`, sem permissão no `/dev/i2c-N` ou falha ao escrever no LCD |
| 2 | opção inválida |
| 3 | display não encontrado |

Se uma escrita no I2C falhar com o programa já rodando (fio solto, por exemplo), ele registra
uma mensagem fixa e reinicia o LCD na volta seguinte, sem encerrar.

## Iniciar junto com a Pi

O serviço do usuário abaixo sobe no boot e só mostra as medições se o display for
encontrado. Crie `~/.config/systemd/user/neoson-chest.service`:

```ini
[Unit]
Description=NeoSon: display do peito (CPU e RAM no LCD 1602)
# No máximo 5 partidas em 2 min; depois desiste, em vez de ficar em laço.
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
# Espera até 20 s o LCD responder no I2C. Sem display, sai com 3: fica
# inativo, sem erro e sem reinício. Falhas de verdade reiniciam após 5 s.
ExecStart=/usr/bin/python3 -I %h/neoson-chest/chest_display.py --wait 20
SuccessExitStatus=3
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Ative e confira:

```bash
systemd-analyze --user verify ~/.config/systemd/user/neoson-chest.service
systemctl --user daemon-reload
systemctl --user enable --now neoson-chest
systemctl --user status neoson-chest
journalctl --user -u neoson-chest -b    # logs desde o último boot
```

Comportamento no boot:

- **Display presente**: o serviço fica `active (running)` e atualiza a tela até a Pi desligar.
  No desligamento, o systemd envia `SIGTERM` e a tela é limpa.
- **Display ausente** (desconectado, I2C desligado ou outro endereço): depois de 20 s, o
  journal registra `display não encontrado` e o serviço fica `inactive`, sem erro. Se o LCD
  for ligado depois, inicie com `systemctl --user start neoson-chest`.
- **Outra falha** (por exemplo, permissão): reinicia a cada 5 s, até 5 vezes em 2 minutos,
  e então fica `failed`.

Serviços do usuário sobem quando o gerenciador do usuário inicia, o que na Pi acontece com
o login automático da sessão gráfica, logo após o boot. Sem login automático, habilite com
`sudo loginctl enable-linger <usuário>`. Para desfazer: `systemctl --user disable --now
neoson-chest` e apagar o arquivo. Para atualizar o programa, copie o módulo de novo e rode
`systemctl --user restart neoson-chest`.

Para um teste rápido, sem instalar o serviço (some no próximo reinício):

```bash
systemd-run --user --unit=neoson-chest-teste -p Restart=on-failure \
  /usr/bin/python3 -I "$HOME/neoson-chest/chest_display.py"
systemctl --user stop neoson-chest-teste    # limpa a tela e apaga a luz
```

Pare o serviço permanente antes desse teste: dois processos escrevendo no mesmo LCD
embaralham a tela.

Na Pi 5, o programa ocupou cerca de 15 MB de memória e 0,2% de um núcleo, com a
atualização a cada 2 segundos.

## Problemas comuns

| Sintoma | Causa provável |
|---|---|
| Luz acesa, sem caracteres | contraste: girar o trimpot azul do backpack |
| Primeira linha com blocos cheios | LCD alimentado mas não inicializado: conferir endereço e fios SDA/SCL |
| Caracteres fracos mesmo com o trimpot no máximo | backpack alimentado em 3,3 V |
| `display não encontrado no I2C 1 no endereço 0x27` | LCD desconectado, I2C desligado no `config.txt` ou endereço errado (tentar `--address 0x3f`) |
| `sem acesso ao I2C 1 no endereço 0x27` | usuário fora do grupo `i2c` |
