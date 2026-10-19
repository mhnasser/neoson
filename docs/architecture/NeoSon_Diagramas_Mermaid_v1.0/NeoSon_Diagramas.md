# NeoSon — Diagramas Mermaid v1.0

Todos os 18 diagramas reunidos para visualização. Abra o preview com `Ctrl+Shift+V`.

Os arquivos `.mmd` desta pasta continuam sendo a fonte: se alterar algum deles, atualize também o bloco correspondente aqui.

## Índice

- [D01 — Arquitetura lógica de componentes](#d01--arquitetura-lógica-de-componentes)
- [D02 — Distribuição física dos equipamentos](#d02--distribuição-física-dos-equipamentos)
- [D03 — Sequência de execução com ferramenta](#d03--sequência-de-execução-com-ferramenta)
- [D04 — Agent Loop e decisões](#d04--agent-loop-e-decisões)
- [D05 — Estados de execução](#d05--estados-de-execução)
- [D06 — Roteamento de modelos e cache](#d06--roteamento-de-modelos-e-cache)
- [D07 — Descoberta e execução de skills](#d07--descoberta-e-execução-de-skills)
- [D08 — Curadoria, testes e publicação de skills](#d08--curadoria-testes-e-publicação-de-skills)
- [D09 — Aprendizagem contínua assíncrona](#d09--aprendizagem-contínua-assíncrona)
- [D10 — Memória e recuperação de contexto](#d10--memória-e-recuperação-de-contexto)
- [D11 — Ingestão de documentos](#d11--ingestão-de-documentos)
- [D12 — Transactional outbox e worker](#d12--transactional-outbox-e-worker)
- [D13 — Pipeline de voz](#d13--pipeline-de-voz)
- [D14 — Métricas e dashboards no Obsidian](#d14--métricas-e-dashboards-no-obsidian)
- [D15 — Autorização de ferramentas](#d15--autorização-de-ferramentas)
- [D16 — Laboratório de avaliação do roteador](#d16--laboratório-de-avaliação-do-roteador)
- [D17 — CI/CD e publicação](#d17--cicd-e-publicação)
- [D18 — Primeiro incremento ponta a ponta](#d18--primeiro-incremento-ponta-a-ponta)

## D01 — Arquitetura lógica de componentes

Fonte: [D01.mmd](D01.mmd)

```mermaid
flowchart TB
    PI[NeoSon Robot - Raspberry Pi]
    TG[Telegram]
    WEB[Web e CLI]
    GW[NeoSon Gateway]
    ROUTER[NeoSon Router]
    CORE[NeoSon Core - Agent Loop]
    CTX[Context Builder]
    SK[Skill Registry e Loader]
    TOOL[Tool Registry e Policy Engine]
    SUB[Subagent Manager]
    LLM[LLM Adapter]
    DB[(SQLite - sessões, traces e outbox)]
    MEM[Memory Service]
    NEO[(Neo4j - grafo)]
    VAULT[NeoSon Vault - Obsidian]
    WORK[Learning Workers]
    DASH[Metrics Exporter]
    EXT[APIs externas e MCP]
    MODELS[Provedores de LLM]
    PI --> GW
    TG --> GW
    WEB --> GW
    GW --> ROUTER
    ROUTER --> CTX
    CTX --> CORE
    SK --> CTX
    MEM --> CTX
    CORE --> TOOL
    CORE --> SUB
    CORE --> LLM
    TOOL --> EXT
    LLM --> MODELS
    CORE --> DB
    DB --> WORK
    WORK --> MEM
    MEM --> NEO
    MEM --> VAULT
    DB --> DASH
    DASH --> VAULT
```

## D02 — Distribuição física dos equipamentos

Fonte: [D02.mmd](D02.mmd)

```mermaid
flowchart TB
    subgraph PI[Raspberry Pi 5 - 8 GB]
      MIC[Microfone e botões]
      SCREEN[Display touch e alto-falante]
      CLIENT[Robot Client e estados de interação]
      MIC --> CLIENT
      CLIENT --> SCREEN
    end
    subgraph NB[Notebook Lenovo - i7 13a geracao, 16 GB, SSD 512 GB]
      GATE[Gateway FastAPI]
      ENGINE[NeoSon Core e Router]
      STORAGE[(SQLite e arquivos)]
      WORKER[Workers e Scheduler]
      KNOW[Vault e Neo4j opcional]
      GATE --> ENGINE
      ENGINE --> STORAGE
      STORAGE --> WORKER
      WORKER --> KNOW
    end
    subgraph PC[PC pessoal - laboratorio]
      LAB[Fine-tuning, testes e experimentos]
    end
    subgraph INTERNET[Servicos externos]
      PROVIDERS[APIs LLM, STT e TTS opcionais]
      SERVICES[Telegram, Outlook, Google e Web]
    end
    CLIENT <--> GATE
    ENGINE <--> PROVIDERS
    GATE <--> SERVICES
    LAB -.-> ENGINE
```

## D03 — Sequência de execução com ferramenta

Fonte: [D03.mmd](D03.mmd)

```mermaid
sequenceDiagram
    participant U as Usuário
    participant G as Gateway
    participant R as Router
    participant C as Context Builder
    participant A as NeoSon Core
    participant L as LLM Adapter
    participant P as Policy Engine
    participant T as Tool Registry
    participant D as SQLite
    U->>G: Mensagem
    G->>D: Identificar e registrar sessão
    G->>R: Solicitação normalizada
    R-->>C: Perfil de execução sugerido
    C->>D: Histórico e memória relevante
    C-->>A: Contexto e skills candidatas
    A->>L: Mensagens + schemas das tools
    L-->>A: Solicitação de tool
    A->>P: Autorizar ação e argumentos
    P-->>A: Permitir ou solicitar confirmação
    A->>T: Executar ferramenta autorizada
    T-->>A: Resultado estruturado
    A->>L: Resultado + contexto atualizado
    L-->>A: Resposta final
    A->>D: Salvar resposta + evento na transação
    A-->>G: Resposta
    G-->>U: Mensagem final
```

## D04 — Agent Loop e decisões

Fonte: [D04.mmd](D04.mmd)

```mermaid
flowchart TD
    START[Receber RunContext]
    CHECK[Checar limites e prazo]
    PROMPT[Montar prompt incremental]
    LLM[Chamar LLM]
    DECIDE{Resposta do modelo}
    TOOL[Validar tool na Policy Engine]
    APPROVAL{Exige aprovação?}
    EXEC[Executar tool e registrar resultado]
    SUB[Executar subagente isolado]
    SKILL[Carregar skill relevante]
    END[Persistir resposta e evento final]
    STOP[Encerrar com erro controlado]
    START --> CHECK
    CHECK -->|Orçamento ok| PROMPT
    CHECK -->|Limite atingido| STOP
    PROMPT --> LLM
    LLM --> DECIDE
    DECIDE -->|Texto final| END
    DECIDE -->|Tool call| TOOL
    TOOL --> APPROVAL
    APPROVAL -->|Autorizado| EXEC
    APPROVAL -->|Negado| PROMPT
    EXEC --> CHECK
    DECIDE -->|Delegação| SUB
    SUB --> CHECK
    DECIDE -->|Skill sob demanda| SKILL
    SKILL --> PROMPT
```

## D05 — Estados de execução

Fonte: [D05.mmd](D05.mmd)

```mermaid
stateDiagram-v2
    [*] --> received
    received --> routing
    routing --> context_ready
    context_ready --> running
    running --> waiting_approval
    waiting_approval --> running: aprovado
    waiting_approval --> cancelled: negado ou expirado
    running --> completed
    running --> failed
    running --> cancelled
    completed --> [*]
    failed --> [*]
    cancelled --> [*]
```

## D06 — Roteamento de modelos e cache

Fonte: [D06.mmd](D06.mmd)

```mermaid
flowchart TD
    IN[Mensagem e contexto de sessão]
    RULE[Router: regras e classificador opcional]
    DEC{Sessão existente?}
    FIX[Manter modelo principal estável]
    SELECT[Escolher perfil inicial de modelo]
    ACT{O que a tarefa exige?}
    DIRECT[Resposta ou tool direta]
    DELEGATE[Subagente com contexto reduzido]
    MEMORY[Busca de memória ou skill]
    MEASURE[Medir qualidade, tokens, cache, custo e latência]
    IN --> RULE
    RULE --> DEC
    DEC -->|Sim| FIX
    DEC -->|Não| SELECT
    FIX --> ACT
    SELECT --> ACT
    ACT -->|Simples| DIRECT
    ACT -->|Complexa| DELEGATE
    ACT -->|Conhecimento anterior| MEMORY
    DIRECT --> MEASURE
    DELEGATE --> MEASURE
    MEMORY --> MEASURE
```

## D07 — Descoberta e execução de skills

Fonte: [D07.mmd](D07.mmd)

```mermaid
flowchart TD
    ASK[Solicitação]
    INDEX[Índice: nome, descrição e gatilhos]
    MATCH{Existe skill relevante?}
    LOAD[Carregar SKILL.md e versão aprovada]
    CONTEXT[Incluir procedimento no contexto]
    NORMAL[Continuar sem skill]
    LOOP[Agent Loop e ferramentas autorizadas]
    CHECK[Verificar resultado por critério objetivo]
    EVENT[Registrar skill_id, versão, sucesso e custo]
    ASK --> INDEX
    INDEX --> MATCH
    MATCH -->|Sim| LOAD
    MATCH -->|Não| NORMAL
    LOAD --> CONTEXT
    CONTEXT --> LOOP
    NORMAL --> LOOP
    LOOP --> CHECK
    CHECK --> EVENT
```

## D08 — Curadoria, testes e publicação de skills

Fonte: [D08.mmd](D08.mmd)

```mermaid
flowchart TD
    OBS[Execuções e feedback]
    CUR[Skill Curator]
    PROP[Candidate: nova skill ou alteração]
    CHECK[Lint, testes, permissões e sandbox]
    EVAL[Avaliações com casos de referência]
    GATE{Aprovada conforme risco?}
    STAGE[Staging e versionamento Git]
    PROD[Registro de skills aprovadas]
    ROLLBACK[Rollback para versão anterior]
    OBS --> CUR
    CUR --> PROP
    PROP --> CHECK
    CHECK -->|Passou| EVAL
    CHECK -->|Falhou| PROP
    EVAL --> GATE
    GATE -->|Sim| STAGE
    GATE -->|Não| PROP
    STAGE --> PROD
    PROD -->|Regressão| ROLLBACK
```

## D09 — Aprendizagem contínua assíncrona

Fonte: [D09.mmd](D09.mmd)

```mermaid
flowchart TD
    DONE[Run finalizado]
    OUTBOX[Evento durável no SQLite]
    WORK[Learning Worker]
    CLASS[Classificar tipo de aprendizado]
    FACT[Memory Consolidator]
    SKILL[Skill Curator]
    ROUTER[Router Evaluator]
    TESTS[Suite de avaliações]
    REVIEW[Validação e revisão]
    GRAPH[Neo4j e Obsidian]
    LIB[Skill Library aprovada]
    POLICY[Política de roteamento versionada]
    DONE --> OUTBOX
    OUTBOX --> WORK
    WORK --> CLASS
    CLASS --> FACT
    CLASS --> SKILL
    CLASS --> ROUTER
    FACT --> REVIEW
    SKILL --> TESTS
    ROUTER --> TESTS
    TESTS --> REVIEW
    REVIEW --> GRAPH
    REVIEW --> LIB
    REVIEW --> POLICY
```

## D10 — Memória e recuperação de contexto

Fonte: [D10.mmd](D10.mmd)

```mermaid
flowchart TD
    ASK[Pedido do usuário]
    RET[Memory Retriever]
    SESS[(SQLite - histórico completo)]
    PROFILE[Perfil e memórias curadas]
    GRAPH[(Neo4j - relações)]
    VAULT[Obsidian - documentos]
    CTX[Context Builder: apenas trechos relevantes]
    SOURCES[Referências à origem e data]
    ASK --> RET
    SESS --> RET
    PROFILE --> RET
    GRAPH --> RET
    VAULT --> RET
    RET --> SOURCES
    SOURCES --> CTX
```

## D11 — Ingestão de documentos

Fonte: [D11.mmd](D11.mmd)

```mermaid
flowchart TD
    FILE[PDF, artigo ou documento recebido]
    RAW[Salvar original com hash e metadados]
    EXTRACT[Extrair texto e detectar falhas]
    CLEAN[Normalizar e dividir conteúdo]
    SUM[Resumo com fonte e links]
    REVIEW[Revisão de qualidade e permissões]
    NOTE[Gerar nota Markdown no Obsidian]
    GRAPH[Extrair entidades e relações no Neo4j]
    INDEX[Atualizar índice de recuperação]
    FILE --> RAW
    RAW --> EXTRACT
    EXTRACT --> CLEAN
    CLEAN --> SUM
    SUM --> REVIEW
    REVIEW --> NOTE
    REVIEW --> GRAPH
    NOTE --> INDEX
    GRAPH --> INDEX
```

## D12 — Transactional outbox e worker

Fonte: [D12.mmd](D12.mmd)

```mermaid
sequenceDiagram
    participant A as NeoSon Core
    participant D as SQLite
    participant W as Worker
    participant M as Memory Service
    A->>D: BEGIN TRANSACTION
    A->>D: Salvar run e resposta
    A->>D: Inserir outbox_event
    A->>D: COMMIT
    A-->>A: Finalizar resposta ao usuário
    W->>D: Reservar evento pendente (lease)
    D-->>W: Evento e event_id
    W->>M: Processar com idempotency_key
    M-->>W: Resultado ou erro
    W->>D: Marcar completed / retry_at
```

## D13 — Pipeline de voz

Fonte: [D13.mmd](D13.mmd)

```mermaid
flowchart LR
    MIC[Microfone na Raspberry]
    VAD[Detecção de fala e botão]
    STT[STT no servidor ou API]
    GATE[Gateway]
    CORE[NeoSon Core]
    TTS[TTS no servidor ou API]
    AUDIO[Alto-falante]
    DISPLAY[Display: ouvindo, pensando, falando]
    MIC --> VAD
    VAD --> STT
    STT --> GATE
    GATE --> CORE
    CORE --> TTS
    TTS --> AUDIO
    CORE --> DISPLAY
```

## D14 — Métricas e dashboards no Obsidian

Fonte: [D14.mmd](D14.mmd)

```mermaid
flowchart TD
    RUNS[Runs, tools, tokens e custos]
    DB[(SQLite - fatos operacionais)]
    AGG[Metrics Aggregator]
    EXPORT[Markdown Exporter - escrita atômica]
    VAULT[NeoSon Vault]
    BASES[Bases e Dataview]
    CHART[Gráficos e relatórios]
    RUNS --> DB
    DB --> AGG
    AGG --> EXPORT
    EXPORT --> VAULT
    VAULT --> BASES
    BASES --> CHART
```

## D15 — Autorização de ferramentas

Fonte: [D15.mmd](D15.mmd)

```mermaid
flowchart TD
    CALL[LLM solicita tool]
    REG[Registry valida nome e schema]
    AUTH[Policy Engine consulta identidade e escopo]
    RISK{Nível de risco}
    EXEC[Executar com timeout e auditoria]
    ASK[Solicitar consentimento explícito]
    DENY[Negar e registrar motivo]
    CONF{Aprovado?}
    CALL --> REG
    REG --> AUTH
    AUTH --> RISK
    RISK -->|Leitura permitida| EXEC
    RISK -->|Escrita sensível| ASK
    RISK -->|Proibido| DENY
    ASK --> CONF
    CONF -->|Sim| EXEC
    CONF -->|Não| DENY
```

## D16 — Laboratório de avaliação do roteador

Fonte: [D16.mmd](D16.mmd)

```mermaid
flowchart TD
    DATA[Casos rotulados e versionados]
    BASE[Baseline - modelo único]
    RULE[Política por regras]
    ML[Classificador opcional - Laya]
    RUN[Testes reproduzíveis]
    SCORE[Qualidade, custo e latência]
    REPORT[Relatório no Obsidian]
    GATE{Supera critérios e segurança?}
    RELEASE[Publicar política versionada]
    HOLD[Revisar ou manter atual]
    DATA --> BASE
    DATA --> RULE
    DATA --> ML
    BASE --> RUN
    RULE --> RUN
    ML --> RUN
    RUN --> SCORE
    SCORE --> REPORT
    SCORE --> GATE
    GATE -->|Sim| RELEASE
    GATE -->|Não| HOLD
```

## D17 — CI/CD e publicação

Fonte: [D17.mmd](D17.mmd)

```mermaid
flowchart LR
    BRANCH[Feature branch]
    PR[Pull request]
    LINT[Ruff, mypy e testes]
    SECURITY[Scan de segredos e dependências]
    BUILD[Build e integração]
    APPROVE[Revisão e aprovação]
    DEPLOY[Deploy no notebook]
    SMOKE[Smoke tests e health checks]
    ROLLBACK[Rollback se falhar]
    BRANCH --> PR
    PR --> LINT
    LINT --> SECURITY
    SECURITY --> BUILD
    BUILD --> APPROVE
    APPROVE --> DEPLOY
    DEPLOY --> SMOKE
    SMOKE -->|Falha| ROLLBACK
```

## D18 — Primeiro incremento ponta a ponta

Fonte: [D18.mmd](D18.mmd)

```mermaid
flowchart LR
    CLI[CLI ou Telegram]
    GW[Gateway]
    ROUTE[Router por regras]
    LOOP[NeoSon Core]
    LLM[Modelo via API]
    TOOL[Tool mock de agenda]
    DB[(SQLite e outbox)]
    WORK[Worker de eventos]
    NOTE[Nota de métricas no Obsidian]
    CLI --> GW
    GW --> ROUTE
    ROUTE --> LOOP
    LOOP --> LLM
    LOOP --> TOOL
    LOOP --> DB
    DB --> WORK
    WORK --> NOTE
```

