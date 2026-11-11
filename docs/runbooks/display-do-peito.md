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

Ao iniciar, a tela mostra `NeoSon` / `monitor...` até a primeira medição. Ao receber
`SIGTERM` ou Ctrl+C, limpa a tela e apaga a luz de fundo. Saída 1 quando não há `/proc` ou
quando o I2C não abre; 2 para opção inválida.

Se uma escrita no I2C falhar com o programa já rodando (fio solto, por exemplo), ele registra
uma mensagem fixa e reinicia o LCD na volta seguinte, sem encerrar.

## Manter rodando

Teste rápido, até o próximo reinício da Pi, como serviço do usuário:

```bash
systemd-run --user --unit=neoson-chest --description="NeoSon: display do peito" \
  --property=Restart=on-failure --property=RestartSec=5 \
  /usr/bin/python3 -I "$HOME/neoson-chest/chest_display.py"
systemctl --user status neoson-chest
systemctl --user stop neoson-chest    # limpa a tela e apaga a luz
```

Para iniciar junto com a Pi, crie `~/.config/systemd/user/neoson-chest.service`:

```ini
[Unit]
Description=NeoSon: display do peito

[Service]
ExecStart=/usr/bin/python3 -I %h/neoson-chest/chest_display.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

e ative com `systemctl --user daemon-reload && systemctl --user enable --now neoson-chest`.
Serviços do usuário sobem com a sessão gráfica de login automático; sem ela, é preciso
`sudo loginctl enable-linger <usuário>`. Para desfazer: `systemctl --user disable --now
neoson-chest` e apagar o arquivo.

Logs: `journalctl --user -u neoson-chest`. Na Pi 5, o programa ocupou cerca de 15 MB de
memória e 0,2% de um núcleo, com a atualização a cada 2 segundos.

## Problemas comuns

| Sintoma | Causa provável |
|---|---|
| Luz acesa, sem caracteres | contraste: girar o trimpot azul do backpack |
| Primeira linha com blocos cheios | LCD alimentado mas não inicializado: conferir endereço e fios SDA/SCL |
| Caracteres fracos mesmo com o trimpot no máximo | backpack alimentado em 3,3 V |
| `sem acesso ao I2C 1 no endereço 0x27` | I2C desligado, usuário fora do grupo `i2c` ou endereço errado |
