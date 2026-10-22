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
