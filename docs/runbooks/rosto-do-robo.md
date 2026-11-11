# Rosto do robô na tela de 7"

O `neoson-face` ocupa a tela touch de 7" com o rosto do robô: dois olhos brancos com borda
ciano sobre fundo preto, sem barra de tarefas nem cursor. As expressões se repetem em loop,
com transições suaves de meio segundo:

| Expressão | Duração | Movimento |
|---|---|---|
| Neutro | 8 s | olhos ovais; olha para a esquerda e para a direita e pisca |
| Feliz | 5 s | olhos em arco ("^ ^"), dando pequenos pulos |
| Surpreso | 4,5 s | olhos redondos e maiores, que dão um salto rápido e piscam |

Toque na tela:

- **toque curto**: o rosto fica feliz por 2,5 s e volta ao loop;
- **toque longo (3 s)** ou **Esc**: fecha o rosto e devolve a área de trabalho.

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
| `--seconds S` | fecha depois de S segundos (padrão 0: não fecha) |
| `--windowed` | janela de 800x480 em vez de tela cheia, para testar num computador |

Saída 0 ao fechar; 1 sem tela gráfica (sem `DISPLAY`); 2 para opção inválida.

Na Pi 5, a 30 quadros por segundo, o rosto usou cerca de 2% de um núcleo e 25 MB de memória.

## Conferir sem olhar a tela

Com o rosto aberto, uma captura da tela mostra o que está sendo exibido:

```bash
ssh neoson-pi.local 'WAYLAND_DISPLAY=wayland-0 grim /tmp/rosto.png'
scp neoson-pi.local:/tmp/rosto.png .
```

## Ajustes

As expressões ficam em `EXPRESSIONS` (posição, tamanho e arredondamento de cada olho, e
`smile` de 0 a 1 para o arco) e a ordem e a duração em `PROGRAM`. As cores das camadas de
brilho estão em `GLOW`. Uma expressão nova precisa de uma entrada em `EXPRESSIONS` e, se tiver
movimento próprio, de um caso em `behave`.
