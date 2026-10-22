# Pipeline de voz

O gateway recebe uma gravação ativada explicitamente e executa STT → motor → TTS pelas
portas configuradas. Este incremento oferece somente simulação determinística de
STT/TTS; não abre microfone nem acessa um fornecedor de fala. A rota vem desligada.

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

## Configuração e erros

| Variável | Padrão | Limite |
|---|---|---|
| `NEOSON_VOICE_PROVIDER` | `disabled` | `disabled` ou `fake` |
| `NEOSON_VOICE_MAX_BYTES` | 1048576 | Até 2097152, por entrada/saída |
| `NEOSON_VOICE_MAX_SECONDS` | 15 | Até 30, por áudio |
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
Não há retry automático nem deduplicação de POST neste incremento. Reconcilie pelo
histórico antes de repetir um pedido incerto. `needs_approval` continua exigindo a
[revisão autenticada da ação](action-approvals.md); fala reconhecida não concede aprovação.

## Privacidade e validação

Áudio não é persistido nem exportado e as respostas usam no-store. A transcrição é uma
mensagem do usuário no histórico privado, quando o motor tem armazenamento. Não registre
corpos, transcrições, bytes ou exceções de adaptadores nos logs. Não envie áudio privado
a um fornecedor antes de selecionar/configurar explicitamente esse adaptador.

`uv run --no-sync python -m pytest -q tests/test_voice.py` valida o formato/transporte,
ativação/autenticação, cancelamento/timeout/concorrência, falhas de síntese e isolamento
de conteúdo em SQLite/outbox/vault com portas fake. O teste não comprova microfone,
qualidade de STT/TTS, latência da Pi ou fala real. Nenhum reinício de polling do Telegram
é necessário para usar esse teste.
