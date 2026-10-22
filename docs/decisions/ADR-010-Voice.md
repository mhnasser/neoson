# ADR-010 — Portas de fala e transporte de áudio limitado

- **Status:** accepted
- **Data:** 2026-10-22
- **Referências:** especificação §12.1/D13; ADR-003, ADR-004, ADR-005 e ADR-006.

## Contexto e alternativas

A Raspberry precisa ativar captura de forma explícita, enviar áudio ao servidor e
receber feedback. Acoplar microfone, codec ou fornecedor ao motor impediria testes sem
hardware. Streaming contínuo acrescentaria captura, buffers e cancelamento antes de
existirem contratos de consentimento e limites. Um formato comprimido arbitrário também
exigiria um decodificador e limites próprios de expansão.

## Decisão

O primeiro transporte é `POST /api/v1/voice`, autenticado pelo bearer existente, com
WAV PCM16 mono de 16 kHz e `X-Neoson-Activation: explicit`. A rota é desativada por
padrão. Esse header declara a ativação no cliente; não comprova fisicamente um botão.
Captura/display pertencem ao Robot, em outro incremento. O serviço não abre microfone.

As portas `Transcriber`/`Synthesizer` e o pipeline ficam no motor e só dependem da
biblioteca padrão. Adaptadores ficam em `providers`; transporte e composição ficam no
gateway; resposta versionada em `neoson-contracts`. A Raspberry continua sem importar
o servidor. Não se escolhe fornecedor de STT/TTS neste incremento.

Antes de carregar o corpo, há admissão limitada por processo e rejeição sem fila quando
ocupado. O stream limita bytes mesmo sem Content-Length; formato/chunks, duração e
frames são conferidos. O WAV é reconstruído sem metadados auxiliares antes do STT.
Um prazo total inclui upload, STT, motor e TTS, com cancelamento propagado. Adaptadores
futuros também devem respeitar limites de rede/alocação/custo antes de produzir saídas.

Transcrição é entrada de usuário, com identidade atribuída pelo gateway, e não é
autoridade para aprovar ferramentas. Segue pelo mesmo motor, sessão e Policy Engine.
Não há retries automáticos ou concessão por reconhecer “sim”. O resultado do motor é
preservado se TTS falhar/exceder prazo: HTTP 200 com texto/IDs e `audio_status` explícito,
sem nova chamada ao motor. Se o prazo falhar durante o motor, o resultado pode ser
incerto; clientes não devem repetir automaticamente o POST.

Áudio fica na memória da requisição e não é salvo em arquivo, SQLite, evento ou vault.
O texto transcrito segue a política normal de histórico privado de conversas; não vai
para logs ou projeções de métricas. Respostas de áudio usam `Cache-Control: no-store`.

## Simulação e limites

`voice_provider=disabled` não monta a rota. `fake` retorna texto constante e silêncio,
com `simulated: true`; não é reconhecimento ou síntese real. A composição só permite
fake com `llm_provider=echo`, fora de produção, evitando ações reais decorrentes de
uma transcrição inventada. Testes podem injetar portas determinísticas no pipeline.

Limites padrão: 1 MiB, 15 segundos de áudio, 30 segundos de requisição e uma requisição
em andamento. Tetos configuráveis: 2 MiB/30 segundos de áudio/120 segundos totais/quatro
requisições por processo; texto STT/TTS limitado a 8000 caracteres. Não há alteração
de orçamento/permissões do motor nem migração de banco.

Procedimento em [voz](../runbooks/voice.md). Reconhecimento real, captura física e
feedback no display exigem validação própria e não são comprovados pelo modo fake.

## Feedback observável no Robot

O endpoint adicional `/api/v1/voice/stream` entrega eventos NDJSON versionados em
`events/v1/robot`, sem alterar a resposta JSON anterior. Progresso contém somente
estados, com buffer limitado. Callback por requisição informa using_tool apenas ao
executar ferramenta; não existe um observador global compartilhado entre sessões.
TTS é processamento; speaking é reservado à reprodução no cliente.

O Robot valida frames/áudio antes de reproduzir, exige ativação e indicador, encerra
dispositivos/stream em falhas e nunca faz retry automático. Continuidade entre reinícios
salva apenas sessão e vínculo ao gateway; não retoma captura/reprodução. Dispositivos
fake e CLI explícita permitem verificar o fluxo sem drivers. Procedimento em
[Robot de voz](../runbooks/robot-voice.md).

## Atualização — 2026-10-22: adaptador OpenAI

Primeiro adaptador real das portas, `providers/openai_speech.py`, escolhido só com
`NEOSON_VOICE_PROVIDER=openai`. Ele usa a chave já configurada para o modelo e transcreve
o WAV recebido com o idioma informado. A síntese pede PCM cru de 24 kHz e o converte
para WAV PCM16 mono de 16 kHz, o formato do pipeline e do Robot. A fala de cada resposta
é limitada a um número de caracteres, cortada no último fim de frase; o áudio
sintetizado tem limite próprio de duração (até 30 s), separado do limite de entrada.
Erros do fornecedor carregam só o tipo, e áudio e texto não vão para logs. A conversão
é linear e sem filtro: suficiente para fala, não para música. O áudio de cada pedido
passa pelo fornecedor; usar o modo é decisão de quem opera a instalação.
