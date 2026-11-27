# Serviços contínuos no Linux e corpus de voz

O instalador `infra/install.py` prepara unidades **do usuário** para Raspberry Pi OS
ou Ubuntu Server. Não usa sudo, não inicia processos, não habilita boot e não
substitui unidades existentes. Use a conta que vai executar o NeoSon.

| Serviço | Comando | Operação |
|---|---|---|
| `gateway` | `python -m neoson_gateway.main` | API contínua |
| `worker` | `python -m neoson_worker.main` | outbox contínua |
| `documents` | `python -m neoson_worker.docs_main process` | extração local de documentos, exige extra `documents` |
| `graph` | `python -m neoson_worker.graph_main run` | extração contínua, exige configuração do grafo |
| `robot` | `python -m neoson_robot.voice_main --device pipewire --activate` | uma captura por início manual |
| `robot-touch` | `python -m neoson_robot.touch_voice` | rosto contínuo, acorda com "Ei NeoSon" |

O serviço `robot` faz um pedido por ativação: sua unidade é `oneshot`, sem reinício
automático ou instalação no boot. Não habilite um laço de captura por reinício.
Rosto e display já têm unidades próprias; o instalador não as altera. Ele também não
instala Piper ou modelos de voz. Veja [voz do Robot](robot-voice.md).

## Preparar e revisar

Pré-requisitos: Git, uv no PATH, systemd com gerenciador do usuário e acesso à rede
para o primeiro clone/sync. O workspace exige Python 3.13 (o uv pode instalá-lo).
Capture áudio na sessão do usuário que tem acesso ao PipeWire; `pw-record` e
`pw-play` precisam estar instalados. Não use o Python do sistema para substituir
a versão do workspace.

A partir de um checkout, simule com **caminhos absolutos do Linux**:

```bash
python3 infra/install.py --checkout "$HOME/neoson" \
  --environment "$HOME/.config/neoson/server.env" \
  --services gateway worker graph --dry-run
```

`--dry-run` funciona também no Windows com caminhos como `/srv/neoson` e
`/srv/private/server.env`. Só imprime comandos e unidades; não clona, não escreve,
não sincroniza dependências nem chama systemctl.

Para clonar em um destino novo, acrescente `--repository URL_HTTPS_DO_REPOSITORIO`.
Não inclua tokens na URL. Para instalar a partir de um checkout existente, omita
essa opção. O instalador usa `uv sync --locked --no-dev --inexact --package ...`
com os pacotes dos serviços escolhidos; apenas `robot` instala o cliente e os
contratos, sem o motor. Execute a mesma chamada sem `--dry-run` para instalar.

`robot-touch` exige sessão gráfica e Tk, além do PipeWire. Antes de iniciá-lo,
pare o rosto anterior para evitar duas interfaces lendo os mesmos toques. Execute
na sessão gráfica `systemctl --user import-environment DISPLAY XAUTHORITY` (somente
as variáveis presentes), ou configure início pela sessão conforme o
[runbook do rosto](rosto-do-robo.md). Não habilite esse serviço headless antes de
validar a sessão. Ele precisa dos modelos da wake word em `~/neoson-wake` e só envia
o que for dito depois de "Ei NeoSon", com gateway disponível; veja
[wake word](robot-voice.md#rosto-com-wake-word-ei-neoson).

Se já existe `.venv`, o instalador exige `--no-sync`: ele reutiliza o ambiente e
evita modificar um runtime ativo. Confira primeiro se os módulos selecionados
estão instalados. Se faltarem dependências, pare os processos que usam esse venv,
faça backup e prepare o ambiente separadamente, ou use outro checkout/venv. Não
faça `uv sync` num ambiente em execução. Uma instalação parcial pode deixar o clone
ou o venv preparado; corrija a causa e retome sem `--repository` e com `--no-sync`
quando o ambiente estiver pronto.

## Ambiente e início explícito

O instalador cria o arquivo de ambiente apenas se ausente, com permissão `0600`,
e mantém conteúdo existente. Arquivo com permissões abertas ou link simbólico é
recusado. Preencha-o em um editor local; não coloque credenciais em argumentos,
histórico, fotos ou Git. O arquivo é lido pelo systemd, não por um shell: use
atribuições `CHAVE=valor`, sem `export`, comandos ou expansão de variáveis.

Para o servidor, use [`.env.example`](../../.env.example) como referência. Gateway,
worker e grafo devem compartilhar **o mesmo banco**, preferencialmente com
caminhos absolutos para banco/vault/backups. O diretório de trabalho é o checkout.
Configure token, fornecedor e demais serviços explicitamente. O gateway fica
em loopback por padrão; não abra portas na internet. Para o Robot, use um arquivo
separado com `NEOSON_GATEWAY_URL` e `NEOSON_API_TOKEN`, sem chaves de fornecedores.

Antes de reiniciar um servidor existente,
faça [backup](backup-restauracao.md) e confira as migrações. Instalar unidades
não migra dados nem decide onde fica o núcleo do sistema.

```bash
systemctl --user enable --now neoson-gateway neoson-worker
# Só após configurar cada componente e conferir instâncias existentes:
systemctl --user enable --now neoson-graph
systemctl --user status neoson-worker --no-pager
journalctl --user -u neoson-worker -n 30 --no-pager
```

Para iniciar os serviços do usuário no boot sem login, habilite linger na conta
correta, quando desejado: `loginctl enable-linger`. Esse passo pode exigir
autenticação administrativa; confira com `loginctl show-user "$USER" -p Linger`.
PipeWire pode depender da sessão gráfica: linger sozinho não comprova áudio.

A captura do Robot só começa ao executar explicitamente:

```bash
systemctl --user start neoson-robot
```

As unidades contínuas reiniciam em caso de falha, com intervalo de cinco segundos;
o limite padrão do systemd evita tentativas ilimitadas em falha rápida. Após
corrigir a causa: `systemctl --user reset-failed neoson-worker` e `start`.
Confira logs localmente; nunca publique saídas com dados privados.

## Remover ou substituir uma unidade

Pare e desabilite **somente o serviço escolhido**, depois remova seu arquivo:

```bash
systemctl --user disable --now neoson-worker.service
rm -- "$HOME/.config/systemd/user/neoson-worker.service"
systemctl --user daemon-reload
```

Para o Robot, use `stop` em vez de `disable`, pois sua unidade não é habilitável.
O arquivo privado de ambiente, banco, vault, gravações e checkout são preservados.
Para substituir uma unidade, guarde uma cópia local antes de removê-la e execute
novamente o instalador. Para reverter, restaure a unidade anterior, rode
`daemon-reload` e inicie somente os serviços que estavam ativos antes.

## Gravar as frases de teste

O comando inclui 30 frases fictícias (perguntas, números, nomes, datas e comandos).
Ele grava somente áudio local; as frases que descrevem ações não são executadas
nem enviadas ao gateway ou à nuvem. Use uma pasta **nova fora de qualquer Git**:

```bash
.venv/bin/python -m neoson_robot.record_batch \
  --output "$HOME/neoson-audio/sessao-01" --dry-run
.venv/bin/python -m neoson_robot.record_batch \
  --output "$HOME/neoson-audio/sessao-01" --seconds 7
```

Para cada frase: leia, pressione Enter e fale imediatamente. A captura dura o
tempo escolhido (padrão 5 s, máximo 15 s). Use Ctrl+C para encerrar; as amostras
concluídas são preservadas. Uma nova execução exige outra pasta e não sobrescreve
gravações. A duração é fixa, sem detecção automática de fim da fala.

Saída: `01.wav` a `30.wav` em PCM mono, 16 bits, 16 kHz e `manifest.jsonl` com
`audio`, `expected_text`, `sample_rate` e `channels`. No Linux, diretório final
`0700`, arquivos `0600`. Cada entrada do manifesto só é gravada após salvar o WAV.
Falha de disco pode deixar um WAV sem entrada; confira o manifesto antes de avaliar.
O áudio pode conter vozes reais: mantenha todo o corpus fora do Git.

Os testes usam processos PipeWire simulados. Instalação, boot e captura reais
precisam ser conferidos no equipamento; testes isolados não comprovam hardware.
