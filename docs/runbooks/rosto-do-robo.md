# Rosto do robô na tela de 7"

O `neoson-face` ocupa a tela touch de 7" com o rosto do robô: dois olhos azuis, no tom da luz
de fundo do LCD do peito, sobre fundo preto, sem barra de tarefas nem cursor. Duas expressões
se repetem em loop, com transições suaves de meio segundo:

| Expressão | Duração | Movimento |
|---|---|---|
| Neutro | 8 s | olhos ovais; olha para a esquerda e para a direita e pisca |
| Surpreso | 4,5 s | olhos redondos e maiores, que dão um salto rápido e piscam |

A expressão **feliz** (olhos em arco, "^ ^", dando pequenos pulos) não entra no loop: aparece
só como reação ao toque.

Toque na tela:

- **toque curto**: o rosto fica feliz por 2,5 s e volta ao loop;
- **3 toques rápidos** (até 0,5 s entre eles): mostra a imagem ao vivo da câmera em tela
  cheia, por cima do rosto;
- **2 toques rápidos**, com a câmera aberta: fecha a câmera e volta ao rosto;
- **toque longo (3 s)** no rosto, ou **Esc**: fecha o rosto e devolve a área de trabalho
  (para, por exemplo, reiniciar a Pi pelo menu). O atalho "Rosto do NeoSon" no menu reabre
  o rosto. Com a câmera aberta, o toque longo não fecha nada.

### Câmera

A imagem da câmera é o `rpicam-hello -t 0 --fullscreen`, aberto e fechado pelo rosto. Como a
janela da câmera fica na frente, os toques são lidos direto do touchscreen (`/dev/input/eventN`,
o primeiro dispositivo com `BTN_TOUCH` em `capabilities/key`), sem tomá-lo da sessão gráfica: o
usuário precisa estar no grupo `input`. Sem touchscreen encontrado (ou com `--windowed`), os
toques vêm do Tk e os gestos da câmera ficam desligados, para nunca deixar a câmera aberta sem
jeito de voltar. Por segurança, a câmera também volta sozinha ao rosto depois de 5 minutos
(`CAMERA_MAX`) ou se o `rpicam-hello` parar (por exemplo, sem câmera conectada). Fechar o rosto
(Esc, toque longo ou `systemctl --user stop`) fecha a câmera junto.

O programa é o módulo `neoson_robot.face`, só com a biblioteca padrão (Tk para desenhar). A
lógica (formas, linha do tempo e cena em pixels) é testada sem tela; o desenho é escalado e
centralizado para qualquer resolução a partir de um projeto de 800x480.

## Executar

Na sessão gráfica da Pi (terminal da própria tela):

```bash
uv run --package neoson-robot neoson-face
```

Sem instalar nada, copiando só o módulo, e abrindo pela sessão SSH na tela da Pi:

```bash
ssh neoson-pi.local 'mkdir -p ~/neoson-face'
scp apps/robot/src/neoson_robot/face.py neoson-pi.local:neoson-face/
ssh neoson-pi.local 'systemd-run --user --unit=neoson-face -E DISPLAY=:0 \
  -E WAYLAND_DISPLAY=wayland-0 /usr/bin/python3 -I "$HOME/neoson-face/face.py"'
ssh neoson-pi.local 'systemctl --user stop neoson-face'   # fechar
```

A sessão gráfica da Pi é Wayland (labwc); o Tk abre pelo Xwayland em `DISPLAY=:0`, que o
labwc inicia sob demanda.

| Opção | Efeito |
|---|---|
| `--expression neutro\|feliz\|surpreso` | repete só essa expressão (a cada 6 s) |
| `--fps N` | quadros por segundo (padrão 30, de 5 a 60) |
| `--color #rrggbb` | cor base dos olhos (padrão `#0a50ff`, azul saturado no tom do LCD 1602) |
| `--seconds S` | fecha depois de S segundos (padrão 0: não fecha) |
| `--windowed` | janela de 800x480 em vez de tela cheia, para testar num computador |

Saída 0 ao fechar; 1 sem tela gráfica (sem `DISPLAY`); 2 para opção inválida.

Na Pi 5, a 30 quadros por segundo, o rosto usou cerca de 2% de um núcleo e 25 MB de memória.

## Iniciar junto com a Pi

O rosto precisa da sessão gráfica, então quem o inicia é a própria sessão, pelo início
automático do XDG (`lxsession-xdg-autostart`, chamado pelo labwc), através de um serviço do
usuário. Assim ele só abre quando a tela já existe, reinicia se falhar e pode ser controlado
com `systemctl --user`.

`~/.config/systemd/user/neoson-face.service`:

```ini
[Unit]
Description=NeoSon: rosto do robô na tela de 7 polegadas
# Iniciado pela sessão gráfica (~/.config/autostart/neoson-face.desktop).
# No máximo 5 partidas em 2 min; depois desiste, em vez de ficar em laço.
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Environment=DISPLAY=:0 WAYLAND_DISPLAY=wayland-0
ExecStart=/usr/bin/python3 -I %h/neoson-face/face.py
# Toque longo ou Esc encerram com 0: fica fechado. Falhas reiniciam após 5 s.
Restart=on-failure
RestartSec=5
```

`~/.config/autostart/neoson-face.desktop` (abre ao entrar na sessão):

```ini
[Desktop Entry]
Type=Application
Name=NeoSon: rosto
Comment=Abre o rosto do robô em tela cheia ao entrar na sessão gráfica
Exec=systemctl --user start neoson-face
NoDisplay=true
```

`~/.local/share/applications/neoson-face.desktop` (atalho "Rosto do NeoSon" no menu, para
reabrir pelo toque depois de fechar):

```ini
[Desktop Entry]
Type=Application
Name=Rosto do NeoSon
Comment=Mostra o rosto do robô em tela cheia (toque longo de 3 s fecha)
Exec=systemctl --user start neoson-face
Icon=face-smile
Categories=Utility;
```

Confira com `desktop-file-validate` e `systemd-analyze --user verify`, depois
`systemctl --user daemon-reload`. O serviço não tem `[Install]`: não é habilitado, quem o
inicia é a sessão. Para desligar o início automático, apague o arquivo de
`~/.config/autostart`; para fechar agora, `systemctl --user stop neoson-face`.

No boot, o rosto abre logo depois do labwc e é ele quem faz o Xwayland iniciar; o primeiro
pedido de tela cheia pode chegar antes de o gerenciador de janelas estar pronto e se perder
(a janela aparecia pequena, com barra de título). Por isso a janela já nasce do tamanho da
tela e, no primeiro minuto, o rosto confere algumas vezes (`FULLSCREEN_CHECKS`) se está mesmo
em tela cheia e refaz o pedido se não estiver. A lista de conferências é finita: com duas
telas ligadas, a área total nunca bate com uma janela em tela cheia, e o pedido não pode se
repetir para sempre.

## Conferir sem olhar a tela

Com o rosto aberto, uma captura da tela mostra o que está sendo exibido:

```bash
ssh neoson-pi.local 'WAYLAND_DISPLAY=wayland-0 grim /tmp/rosto.png'
scp neoson-pi.local:/tmp/rosto.png .
```

## Ajustes

As expressões ficam em `EXPRESSIONS` (posição, tamanho e arredondamento de cada olho, e
`smile` de 0 a 1 para o arco) e a ordem e a duração em `PROGRAM`. A espessura das camadas
de cada olho fica em `LAYERS`; as cores saem de uma cor base por `palette` (brilho externo a
30% da cor, borda na cor e miolo 10% mais perto do branco). Uma expressão nova precisa de uma
entrada em `EXPRESSIONS` e, se tiver movimento próprio, de um caso em `behave`.
