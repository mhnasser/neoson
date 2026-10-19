# Inventário do hardware

Dois equipamentos: o Raspberry Pi 5 (da seção "Sistema" até "Repetir o inventário") e o notebook
servidor (última seção).

Nomes de host e de usuário neste documento são fictícios. Os valores reais ficam em `neoson.env`
(`NEOSON_PI_HOST` e `NEOSON_PI_USER`), fora do Git.

O Raspberry Pi foi inventariado por SSH em 01/10/2026, por comandos somente de leitura. A única
alteração feita no sistema foi o volume da saída de som, indicado abaixo.

## Sistema

| Item | Observado |
|---|---|
| Modelo | Raspberry Pi 5 Model B Rev 1.1 (8 GB, informado pelo usuário) |
| Sistema | Raspberry Pi OS 64 bits; `/etc/os-release`: Debian GNU/Linux 13 (trixie), 13.7 |
| Imagem | `/etc/rpi-issue`: Raspberry Pi reference 2026-06-18, gerada por pi-gen, stage4 |
| Arquitetura | kernel `aarch64`, pacotes `arm64`, `getconf LONG_BIT` 64 |
| Python | 3.13.5, com `venv` e `ensurepip` disponíveis |
| git | 2.47.3 |
| Ambiente gráfico | `graphical.target`, LightDM e labwc instalados, `display-manager` ativo; sessão Wayland |
| Armazenamento | **SSD NVMe SK hynix BC901 256 GB** (238,5 GiB): é o disco de sistema. `/boot/firmware` (512 MB) e `/` ext4 (235 GB, 8,2 GB usados, 4%). Não há cartão SD |
| CPU | 4 núcleos (Cortex-A76); 35,6–47,7 °C em repouso; `vcgencmd get_throttled` → `0x0` (sem subtensão nem throttling) |
| Refrigeração | **cooler ativo** no conector de ventoinha (`pwm-fan`, 5 níveis); parado em repouso (liga por volta de 50 °C) |
| Relógio | fuso `America/Sao_Paulo`; NTP ativo e sincronizado. Sem bateria no RTC: até o NTP sincronizar, o relógio parte da última hora salva (aparece no journal como um salto) |
| Rede | Wi-Fi doméstico (`wlan0`), IPv4 por **DHCP**, sem reserva no roteador; mDNS (`avahi-daemon`) ativo: `neoson-pi.local` (`NEOSON_PI_HOST`) resolve no Windows (IPv4 e IPv6) |
| SSH | **só chave** desde 09/10/2026: `/etc/ssh/sshd_config.d/10-somente-chave.conf` com `PasswordAuthentication no` e `KbdInteractiveAuthentication no` (conferido com `sshd -T`) |
| sudo | exige senha (não há regra `NOPASSWD`), mas o Raspberry Pi OS traz `Defaults timestamp_type=global` (`/etc/sudoers.d/010_global-tty`): depois que a senha é digitada em **qualquer** sessão do usuário, **todas** as sessões, inclusive SSH automatizadas, ficam com sudo liberado por ~15 min |
| Journal | **persistente** desde 09/10/2026 (`/var/log/journal`, até 200 MB), pelo drop-in `/etc/systemd/journald.conf.d/50-persistente.conf`. O Raspberry Pi OS deixa o journal só na RAM por padrão (ver abaixo) |
| Memória | 7,9 GiB; 7,4 GiB disponíveis em repouso; swap de 2 GB em zram (comprimida na RAM, não no SSD) |
| Usuário `neoson` (`NEOSON_PI_USER`) | grupos `adm`, `sudo`, `audio`, `video`, `render`, `input`, `gpio`, `i2c`, `spi`, `dialout`, `plugdev`, `netdev` |
| Ferramentas | uv 0.12.24 em `~/.local/bin` (instalado em 09/10/2026, sem root). Ausentes: `docker`, `podman`, `pipx`, `gh`, `i2c-tools` |
| NeoSon | ainda não instalado. Papel previsto: NeoSon Robot, o cliente de voz e display; o núcleo roda no notebook (ver `docs/architecture/`) |

## Dispositivos

| Função | Dispositivo | Observações |
|---|---|---|
| Microfone | USB PnP Sound Device (USB `08bb:2902`; `lsusb`: Texas Instruments PCM2902; ALSA: C-Media), mono | `Mic Capture Volume` 16/16 (+23,81 dB, o máximo), `Auto Gain Control` ligado; fonte padrão no PipeWire com volume 1,00 |
| Saída de som | Jieli UACDemoV1.0 (USB `4c4a:4155`), estéreo; saída padrão | Volume 0,70 no `wpctl`, ajustado de 0,40 em 01/10/2026 com autorização do usuário |
| HDMI | `vc4-hdmi-0` e `vc4-hdmi-1` | Não usados para áudio |
| Tela touch 7" | `11-0038 generic ft5x06 (00)`, touchscreen no udev e no libinput | Usuário no grupo `input`; funcionamento confirmado pelo usuário no teclado virtual. O touch fica no barramento `i2c-11`, que é interno à interface DSI, e não nos pinos GPIO |
| Servidor de áudio | PipeWire 1.4.2 e WirePlumber | Acessíveis pela sessão SSH do usuário |
| LCD 1602 com interface I2C | **ainda não conectado nem detectado** | O I2C dos pinos GPIO está **desligado**: não existe `/dev/i2c-1`, e `config.txt` não tem `dtparam=i2c_arm=on`. Os barramentos presentes (`i2c-11`, `-13`, `-14`) são internos. Endereço provável 0x27 ou 0x3F (backpack PCF8574). Ver o alerta de tensão abaixo |
| Câmera 5 MP | **não detectada**: `rpicam-hello --list-cameras` → `No cameras available!` | `camera_auto_detect=1` está ligado. O Pi 5 usa conector de câmera de 22 pinos; câmeras de 5 MP (OV5647) vêm com cabo de 15 pinos e precisam de um cabo adaptador 15→22. Conferir cabo e encaixe |

Números de placa ALSA e de nó do PipeWire podem mudar ao reiniciar ou reconectar
dispositivos USB; resolver pelo nome ou pelo dispositivo padrão.

## Observações de áudio

- O `wpctl` mostra o volume em escala cúbica: 0,40 equivalia a um ganho linear de
  0,064 (cerca de −24 dB) e 0,70 equivale a 0,343 (cerca de −9,2 dB no mixer ALSA).
  Com 0,40, até um som próximo da escala máxima foi ouvido "bem baixo"; com 0,70,
  sinais de teste a 15% da escala foram ouvidos.
- Gravações de fala pelo PipeWire (mono, 16 kHz, 16 bits) ficaram entre −41,4 e
  −37,9 dBFS RMS, mesmo com o ganho do microfone no máximo: nível baixo para
  reconhecimento de fala sem ganho adicional.
- Nesta instalação, `pw-record` encerrado por SIGINT retorna código 1 mesmo depois
  de entregar o áudio.
- Para voltar ao volume original: `wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.40`.

## Alerta antes de ligar o LCD 1602

O backpack I2C (PCF8574) desses módulos costuma ser alimentado em 5 V e traz resistores de pull-up
para os 5 V nas linhas SDA e SCL. Os pinos GPIO da Pi aceitam **no máximo 3,3 V**: ligar direto pode
colocar mais de 3,3 V no GPIO 2/3. Antes de conectar, escolher uma destas saídas:

- alimentar o backpack em 3,3 V (o contraste do LCD pode ficar fraco);
- remover os pull-ups do backpack, deixando só os da Pi (1,8 kΩ para 3,3 V);
- usar um conversor de nível lógico bidirecional.

Habilitar o I2C (`sudo raspi-config` → Interface Options → I2C) e instalar o `i2c-tools` ficam para
a fase do display (F6 — Robot + integrações, na especificação técnica).

## Acesso remoto e journal (desde 09/10/2026)

| Configuração | Situação |
|---|---|
| SSH só por chave (`10-somente-chave.conf`) | conferida com `sshd -T`: `passwordauthentication no` |
| Reserva do IP no roteador | **adiada** pelo usuário: a Pi fica desligada até o NeoSon entrar no ar. Enquanto isso, o IP pode mudar a cada vez que ela for ligada; o acesso SSH usa o nome mDNS (`NEOSON_PI_HOST`) |
| Journal persistente | `50-persistente.conf`; journal em `/var/log/journal` |

**Por que o journal precisa de um drop-in.** Criar `/var/log/journal` não basta: o Raspberry Pi OS
traz `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` com `Storage=volatile`, para
poupar cartões SD, e com ele a pasta é ignorada. O drop-in em `/etc` com número maior (50) é lido
depois e vence:

```ini
# /etc/systemd/journald.conf.d/50-persistente.conf
[Journal]
Storage=persistent
SystemMaxUse=200M
```

Conferir com `systemd-analyze cat-config systemd/journald.conf` (o último `Storage=` vale) e
`journalctl --disk-usage`. Para desfazer: apagar o arquivo e rodar
`sudo systemctl restart systemd-journald`. Aqui o disco é um SSD NVMe, então o desgaste que
motivou o padrão volátil não se aplica.

**Sobre o sudo para automação.** Hoje, o sudo das sessões automatizadas (deploy, agentes) só funciona
na janela de ~15 min depois que alguém digita a senha. É um bom meio-termo para mudanças
pontuais: quem precisa de sudo pede, e você abre a janela com `sudo -v`. Para o deploy automático
do NeoSon Robot, o plano é uma regra **estreita** no sudoers, só para os comandos necessários (por
exemplo, o `systemctl restart` do serviço do NeoSon). Um `NOPASSWD: ALL` daria ao runner de CI
poder de root sobre a Pi.

O efeito colateral do `timestamp_type=global` é que **qualquer** processo do usuário herda o sudo
nessa janela, não só o terminal onde a senha foi digitada. Para restringir ao terminal, troque para
`timestamp_type=tty`, num arquivo próprio em `/etc/sudoers.d/` editado com `visudo`.

## Repetir o inventário

```bash
cat /etc/os-release; cat /etc/rpi-issue
uname -m; dpkg --print-architecture; getconf LONG_BIT
python3 --version
systemctl get-default; systemctl is-active display-manager
lsusb; cat /proc/asound/cards
wpctl status; wpctl get-volume @DEFAULT_AUDIO_SINK@
lsblk -o NAME,SIZE,TYPE,TRAN,MODEL,MOUNTPOINTS; df -h /
free -h; vcgencmd measure_temp; vcgencmd get_throttled
ls -l /dev/i2c*; grep -nE "^(dtparam=i2c|camera_auto_detect)" /boot/firmware/config.txt
rpicam-hello --list-cameras
id
```

`uname -m` descreve a arquitetura do kernel; `dpkg --print-architecture`, a dos
pacotes. Uma sessão SSH pode não informar a sessão gráfica em `XDG_SESSION_TYPE`.

## Notebook servidor

**Servidor do NeoSon**: pela especificação técnica, roda o core, o gateway, o worker e o SQLite;
a Pi fica só como cliente de interação.

Transcrito em 16/10/2026 da tela "Informações do dispositivo" do Windows (Configurações → Sistema →
Sobre), enviada pelo usuário.

| Item | Observado |
|---|---|
| Processador | Intel Core i7-13620H, 13ª geração, 2,40 GHz |
| Memória | 16,0 GB instalados (15,7 GB utilizáveis) |
| Placa gráfica | Intel UHD Graphics (128 MB), a única listada nessa tela |
| Armazenamento | 596 GB, com 162 GB usados |
| Sistema | Windows de 64 bits, processador x64 |
| microSD | **previsto**: cartão de 128 GB |
