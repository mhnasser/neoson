# Pipeline de voz

O gateway recebe uma gravação ativada explicitamente e executa STT → motor → TTS pelas
portas configuradas. Há dois adaptadores: simulação determinística (`fake`) e a API de
áudio da OpenAI (`openai`). O gateway não abre microfone; a captura é do Robot. A rota
vem desligada.

## Testar em simulação

Em configuração local de desenvolvimento, use `NEOSON_LLM_PROVIDER=echo` e
`NEOSON_VOICE_PROVIDER=fake`, com o token de API existente. Não copie credenciais para
documentação/histórico de comandos. Não use esse modo para validar transcrição/fala:
ele sempre reconhece “Teste simulado de voz.” e devolve silêncio PCM de 0,2 segundos.
A resposta declara `simulated: true`. Fake com OpenAI ou em produção impede a inicialização.

Envie uma requisição com seu cliente HTTP:

| Campo | Valor |
|---|---|
| Método/rota | `POST /api/v1/voice` |
| Autenticação | `Authorization: Bearer <token local>` |
| Ativação | `X-Neoson-Activation: explicit` |
| Formato | `Content-Type: audio/wav`, PCM16 mono, 16000 Hz |
| Corpo | Bytes do arquivo WAV, sem multipart/base64 na entrada |
| Sessão | Query `session_id` opcional, como UUID |

O cliente deve exigir ação de ativação e indicar captura; o header sozinho não detecta
se houve uma ação física. Não transmita continuamente. O cliente Robot de captura e
display será conectado separadamente, preservando a separação dos pacotes.

A resposta JSON herda `protocol_version`, `run_id`, `session_id`, `text` e `status` das
mensagens, com `audio_base64`, `audio_format: wav`, `sample_rate_hz: 16000`,
`audio_seconds`, `audio_status` e `simulated`. Decodifique `audio_base64` somente quando
não for null e o estado for `ready`. Falha de síntese mantém o resultado textual e IDs;
não repita a conversa para tentar obter áudio.

Pronúncia do nome: antes da síntese, `NeoSon` e `Neo Son` (sem diferenciar maiúsculas)
viram `Néusson`, que soa "Néuson"; com um "s" só, o sintetizador lê "Néuzon". Vale para
qualquer fornecedor de síntese, e o `text` da resposta continua `NeoSon`. Outras grafias
entram em `PRONUNCIATIONS`, em `engine/voice.py`.

## Fala real com a OpenAI

Escolha explícita, desligada por padrão. O áudio de cada pedido e o texto da resposta
vão para a OpenAI, com a mesma `OPENAI_API_KEY` do modelo, e cada pedido tem custo de
transcrição e síntese. Antes de usar com outras pessoas por perto, confira a política de
dados do fornecedor; a escolha do fornecedor de fala é sua.

```dotenv
NEOSON_VOICE_PROVIDER=openai
NEOSON_VOICE_STT_MODEL=gpt-4o-mini-transcribe
NEOSON_VOICE_TTS_MODEL=gpt-4o-mini-tts
NEOSON_VOICE_TTS_VOICE=marin
NEOSON_VOICE_LANGUAGE=pt
NEOSON_VOICE_MAX_SPEECH_CHARS=400
```

Funciona com o motor OpenAI ou `echo`. A resposta declara `simulated: false`, e a rota
recusa captura simulada (`X-Neoson-Simulated: true`), então a demonstração fake do
Robot não serve para esse modo. A síntese recebe PCM de 24 kHz e o converte para WAV
PCM16 mono de 16 kHz. Respostas longas são faladas só até
`NEOSON_VOICE_MAX_SPEECH_CHARS`, cortadas no último fim de frase; o texto completo
continua na resposta. O áudio sintetizado tem até `NEOSON_VOICE_MAX_OUTPUT_SECONDS`
(30 s, o máximo que o Robot reproduz). O SDK roda sem retentativas, e erros do
fornecedor viram `503` (transcrição) ou `synthesis_failed` (síntese) sem a mensagem
original.

Teste ao vivo opcional, com a chave local e frases fictícias (ida e volta de fala e a
rota real do gateway com o motor eco):

```bash
NEOSON_LIVE_TESTS=1 uv run --no-sync python -m pytest -q tests/test_openai_speech.py -k live
```

## Configuração e erros

| Variável | Padrão | Limite |
|---|---|---|
| `NEOSON_VOICE_PROVIDER` | `disabled` | `disabled`, `fake` ou `openai` |
| `NEOSON_VOICE_MAX_BYTES` | 1048576 | Até 2097152, por entrada/saída |
| `NEOSON_VOICE_MAX_SECONDS` | 15 | Até 30, por áudio recebido |
| `NEOSON_VOICE_MAX_OUTPUT_SECONDS` | 30 | Até 30, por áudio sintetizado |
| `NEOSON_VOICE_TIMEOUT_SECONDS` | 30 | Até 120, prazo total |
| `NEOSON_VOICE_MAX_CONCURRENCY` | 1 | Até 4, por processo, sem fila |

O serviço confere o tamanho ao receber cada chunk e rejeita Content-Length inválido;
não depende da declaração do cliente para limitar o corpo. Formato, byte-rate,
alinhamento, duração e chunks são validados; metadados extras são removidos. O corpo
comprimido por Content-Encoding não é aceito. Texto STT/TTS tem limite de 8000 caracteres.

| HTTP/estado | Interpretação |
|---|---|
| 401 | Bearer ausente/inválido |
| 400 / 415 | Ativação/tamanho declarado inválidos ou formato/transporte incompatível |
| 413 / 422 | Áudio acima do teto ou WAV inválido/fora dos limites |
| 404 | Rota desativada ou sessão ausente |
| 503 | Serviço ocupado, STT indisponível/inválido ou timeout antes do resultado |
| 200 / `synthesis_failed` ou `synthesis_timeout` | Motor já retornou; texto/IDs preservados |
| 200 / `text_too_long` | Texto disponível, mas acima do limite de síntese |

Sem resultado do motor, uma falha/cancelamento pode ter ocorrido após algum efeito.
Não há retry automático. Reconcilie pelo histórico antes de repetir um pedido
incerto. A proteção opcional abaixo rejeita recebimentos repetidos, sem reutilizar
respostas. `needs_approval` continua exigindo a
[revisão autenticada da ação](action-approvals.md); fala reconhecida não concede aprovação.

## Proteção contra repetição

`NEOSON_VOICE_REPLAY_PROTECTION_ENABLED=true` exige, nas rotas JSON e stream:

- `X-Neoson-Request-Id`: UUID por pedido explícito;
- `X-Neoson-Request-Time`: horário Unix em segundos, próximo ao relógio do servidor.

O cliente atualizado já envia os dois. A janela configurável é
`NEOSON_VOICE_REQUEST_WINDOW_SECONDS` (padrão 120 s, entre 10 e 600 s), para passado
e futuro. Mantenha os relógios sincronizados. Header ausente/inválido ou horário
fora da janela retorna 400; ID já recebido retorna 409, antes de transcrever ou
executar o motor. A autenticação é verificada antes de consumir o ID.

A migration 19 adiciona recibos no mesmo SQLite: UUID e expiração, sem áudio ou
texto. O registro é atômico entre processos e permanece após reinício. Expira
quando o horário original já não pode ser aceito; um ID deve ser sempre novo em
um novo pedido. Há um teto de 10 mil recibos ativos; saturação retorna 503, sem
executar o pedido. Recibos expirados são limpos no próximo recebimento válido.
O ID é consumido antes de processar o corpo, inclusive se a captura ou o
processamento falhar depois. Nunca reenvie automaticamente um POST incerto.

Faça backup antes de atualizar o servidor para aplicar a migration. A opção vem
desligada, preservando clientes antigos. Atualize os clientes antes de ligá-la;
para reverter a exigência de headers, desligue a opção, preservando banco e recibos.

## Privacidade e validação

### Piper e efeito da voz

O fornecedor de transcrição continua em `NEOSON_VOICE_PROVIDER`. A síntese usa
`NEOSON_VOICE_TTS_PROVIDER`: `same` mantém o comportamento anterior, `openai` usa
a API e `piper` executa o sintetizador local no equipamento do gateway.

Exemplo para transcrição OpenAI e voz local, no arquivo privado do servidor:

```dotenv
NEOSON_VOICE_PROVIDER=openai
NEOSON_VOICE_TTS_PROVIDER=piper
NEOSON_PIPER_MODEL=/srv/neoson-voices/pt_BR-faber-medium.onnx
NEOSON_PIPER_PYTHON=/opt/neoson-piper/bin/python
NEOSON_VOICE_EFFECT=squirrel_robot
NEOSON_VOICE_PITCH_FACTOR=1.45
```

Reutilize sua instalação do Piper e os modelos já baixados. O modelo precisa do
arquivo `.onnx.json` ao lado do `.onnx`; a taxa é lida de `audio.sample_rate`.
`NEOSON_PIPER_PYTHON` aponta para o Python que tem `piper-tts` instalado, inclusive
se estiver fora do venv do NeoSon. Vazio, usa `python3`. Não é preciso
instalar Piper/modelos no workspace ou copiá-los para o Git. No Windows, use
caminhos locais equivalentes no arquivo privado. Modelos precisam estar no
equipamento da síntese; o cliente Robot só recebe o WAV final.

`none` preserva a voz sem efeito (padrão). `robot` aplica timbre suave sem mudar
o tom; `squirrel_robot` aplica também o fator de tom (entre 1 e 2). A receita
é: length_scale = fator no Piper, ou speed = 1/fator na OpenAI, seguido da
reamostragem por 1/fator, preservando aproximadamente a duração natural. Depois,
mistura de 30% com seno de 35 Hz, filtro pente de 6 ms/feedback 0,45 e pico a
0,9. O wrapper de efeito aceita qualquer Synthesizer PCM16/16 kHz; para compensar
a duração de outro backend, ele também precisa produzir a fala alongada antes
do pitch. `fake` continua apenas uma simulação, sem qualidade vocal real.

O Piper roda num processo vivo: esse Python executa um script embutido que carrega
o modelo uma vez e atende uma frase por vez (décimos de segundo por frase, contra
~1 s para abrir o processo e carregar o modelo). A primeira fala depois de subir o
gateway paga essa carga. O processo recebe texto por stdin, nunca em
argumentos/arquivos; stdout traz o tamanho e o PCM, limitado por duração/taxa;
stderr não é exposto. Timeout, cancelamento, quadro inválido ou saída do processo
o encerram, e a próxima frase abre outro. Não há shell ou fallback automático para outra API. Entrada de
8–48 kHz é convertida linearmente para WAV PCM16 mono/16 kHz, sem dependência
de áudio adicional. Conversão/efeito são adequados para fala, não masterização.
O texto completo da resposta permanece no JSON/histórico; só a fala recebe
pronúncia e corte de `NEOSON_VOICE_MAX_SPEECH_CHARS`.

### Transcrição local com Whisper

`NEOSON_VOICE_STT_PROVIDER` escolhe a transcrição separada da rota:
`same` mantém o comportamento anterior (segue `NEOSON_VOICE_PROVIDER`), `openai`
usa a API e `whisper` roda o faster-whisper no equipamento do gateway. Com
transcrição e síntese locais, nenhuma chamada de fala vai à OpenAI e a chave não é
exigida; `NEOSON_VOICE_PROVIDER=openai` continua sendo só o que liga a rota real.

```dotenv
NEOSON_VOICE_PROVIDER=openai
NEOSON_VOICE_STT_PROVIDER=whisper
NEOSON_WHISPER_MODEL=small
NEOSON_WHISPER_PYTHON=/opt/neoson-whisper/bin/python
NEOSON_VOICE_STT_FALLBACK_OPENAI=false
```

Como o Piper, o faster-whisper fica num Python próprio, fora do workspace (não
entra no `uv.lock` nem no CI):

```bash
python3 -m venv /opt/neoson-whisper
/opt/neoson-whisper/bin/pip install faster-whisper
```

`NEOSON_WHISPER_MODEL` aceita um nome (`small`, `medium`, `large-v3-turbo`...),
baixado do Hugging Face no primeiro uso e guardado no cache do usuário, ou uma
pasta com um modelo CTranslate2. Modelos ajustados para português publicados no
formato do `transformers` precisam ser convertidos antes, por exemplo:

```bash
/opt/neoson-whisper/bin/pip install "transformers[torch]"
/opt/neoson-whisper/bin/ct2-transformers-converter --model zuazo/whisper-small-pt \
  --output_dir /srv/neoson-voices/whisper-small-pt-ct2 --quantization int8
```

O modelo roda na CPU em int8, no idioma de `NEOSON_VOICE_LANGUAGE`, com detecção de
voz ligada (corta o silêncio do fim da gravação, onde o Whisper tende a inventar
frases). O processo é vivo: carrega o modelo uma vez, recebe o WAV pelo stdin e
devolve o texto pelo stdout, nunca em argumentos ou logs; stderr não é exposto. A
primeira transcrição depois de subir o gateway paga a carga do modelo. Prazo,
cancelamento, resposta inválida ou saída do processo o encerram (o que interrompe a
CPU de verdade) e a próxima transcrição abre outro.

Com `NEOSON_VOICE_STT_FALLBACK_OPENAI=true` (exige a chave), uma falha rápida do
Whisper (Python sem faster-whisper, modelo ausente, processo morto) cai na
transcrição da OpenAI; o log registra só o tipo do erro. Um Whisper travado até o
prazo do pedido não deixa tempo para a reserva e o pedido termina em
`transcription_timeout`. O registro de custo usa o fornecedor configurado
(`whisper`) e o preço de `NEOSON_VOICE_STT_PRICE_PER_MINUTE_USD` também para as
transcrições da reserva.

### Avaliar transcrição e síntese

`neoson-voice-eval` compara candidatos com as gravações do corpus de 30 frases
([gravar as frases de teste](servicos-continuos.md#gravar-as-frases-de-teste)).
Rode na máquina que vai transcrever (Pi ou notebook), com o arquivo de configuração
do servidor:

```bash
.venv/bin/python -m neoson_gateway.voice_eval stt \
  --corpus "$HOME/neoson-audio/sessao-01" \
  --candidate whisper:small --candidate whisper:medium \
  --candidate whisper:/srv/neoson-voices/whisper-small-pt-ct2 \
  --candidate openai:gpt-4o-mini-transcribe --timeout 120
```

Cada candidato (`whisper[:modelo ou pasta]` ou `openai[:modelo]`) roda sozinho,
montado como o gateway o montaria e sem reserva; o processo do Whisper é encerrado
antes do próximo, para modelos grandes não disputarem a RAM. Candidatos `openai`
enviam as gravações à API e têm custo. Métricas por candidato:

- **WER/CER**: erros de palavra e de caractere sobre o texto esperado, os dois em
  minúsculas, sem pontuação e com números por extenso ("125" = "cento e vinte e
  cinco"); acentos contam. Falha conta como clipe inteiro errado.
- **Latência** média e máxima, **RTF** (tempo de transcrição ÷ duração do áudio)
  e **CPU por clipe** (só Whisper, medida no processo dele).
- **1ª chamada**: inclui carregar o modelo e fica fora das médias.

Saída: `data/voice-eval/voice-stt-<data>.json` (números por clipe, sem texto) e o
painel `Dashboard/Voz-Transcricao.md` no vault, com a última execução, a máquina e
o checksum do manifesto. Nada de áudio, texto esperado ou transcrição.

Síntese às cegas, com notas do usuário:

```bash
.venv/bin/python -m neoson_gateway.voice_eval tts-samples \
  --output "$HOME/neoson-audio/amostras-01"
# Ouça amostra-01.wav a amostra-10.wav e dê nota de 1 a 5 em notas.csv.
.venv/bin/python -m neoson_gateway.voice_eval tts-score \
  --samples "$HOME/neoson-audio/amostras-01"
```

`tts-samples` sintetiza 5 respostas fictícias em cada candidato (`piper` e
`openai`; `--candidate` restringe), com o modelo, a voz, o efeito e a pronúncia
configurados, e sorteia os nomes. O gabarito fica em `.gabarito.json`: não o abra
antes de dar as notas, nem compare tamanho ou duração dos arquivos. A pasta precisa
ser nova e fora de qualquer Git. `tts-score` aceita `notas.csv` salvo por planilha
(vírgula ou ponto e vírgula), exige nota de 1 a 5 em todas as amostras e grava
`voice-tts-<data>.json` e `Dashboard/Voz-Sintese.md` com média, mínimo e máximo
por candidato.

`tests/test_whisper_speech.py` e `tests/test_voice_eval.py` usam um processo
simulado com o mesmo protocolo e portas falsas: não comprovam modelo real, qualidade
da transcrição ou tempo na Pi.

### Uso e custo da fala

A migration 20 acrescenta `speech_usage_records`. Faça backup antes de atualizar
uma instalação existente. Para cada execução persistida pelo motor, grava:
fornecedores STT/TTS, segundos de entrada transcrita, caracteres submetidos à
síntese, completude e estimativa de custo. Não grava áudio, resposta ou
transcrição nessa tabela ou no evento de métricas.

Preços opcionais, configurados para os modelos/fornecedores escolhidos:

- `NEOSON_VOICE_STT_PRICE_PER_MINUTE_USD`: USD por minuto de entrada;
- `NEOSON_VOICE_TTS_PRICE_PER_MILLION_CHARS_USD`: USD por milhão de caracteres.

Sem preço, o custo fica desconhecido, nunca zero presumido. Para Piper sem
cobrança de API, o operador pode configurar o preço TTS como `0`; isso não mede
energia/hardware. As estimativas usam essas unidades, não substituem a fatura
do fornecedor (inclusive modelos cobrados por tokens). Falha/timeout/cancelamento
da síntese deixa custo desconhecido, preservando os contadores submetidos.
Chamadas STT ou falhas antes de o motor retornar uma execução não podem ser
vinculadas por esse fluxo e não entram no painel; modo echo também não persiste
execuções. Esse painel não é um medidor completo da conta do fornecedor.

Registro e evento de exportação são atômicos e idempotentes por execução. O
worker atualiza `Dashboard/Voz-Custos.md`, com agregados por par de fornecedores,
subtotal conhecido, execuções sem custo e incompletas. Há um link na Home do
vault. Os custos de fala ficam separados dos custos de tokens de LLM.

Para reverter a seleção de voz, volte TTS para `same` e efeito para `none`, sem
apagar modelos, banco ou métricas. Não há migração reversa destrutiva.

Áudio não é persistido nem exportado e as respostas usam no-store. A transcrição é uma
mensagem do usuário no histórico privado, quando o motor tem armazenamento. Não registre
corpos, transcrições, bytes ou exceções de adaptadores nos logs. Não envie áudio privado
a um fornecedor antes de selecionar/configurar explicitamente esse adaptador.
`tests/test_openai_speech.py` cobre o adaptador OpenAI com HTTP simulado: campos
enviados, conversão de áudio, corte de fala e falhas sem texto privado.

`uv run --no-sync python -m pytest -q tests/test_voice.py` valida o formato/transporte,
ativação/autenticação, cancelamento/timeout/concorrência, falhas de síntese e isolamento
de conteúdo em SQLite/outbox/vault com portas fake. O teste não comprova microfone,
qualidade de STT/TTS, latência da Pi ou fala real. Nenhum reinício de polling do Telegram
é necessário para usar esse teste.
