# Canal de mensagens: WhatsApp planejado

O WhatsApp será usado localmente na Raspberry Pi, aberto em um navegador ou
em um aplicativo próprio. A escolha entre essas interfaces e a integração
automática com o motor ficam para uma entrega futura. A versão atual atende
por voz no Robot e pela API/CLI autenticada; a sessão do WhatsApp ainda não
foi configurada.

## Capacidades previstas

- Conversar pelo mesmo motor e manter sessão por canal/remetente autorizado.
- Apresentar ações concretas para aprovação, reutilizando prazo, uso único e
  revalidação do serviço de ações. Uma resposta textual genérica não aprova ações.
- Encaminhar documentos e fotos, após confirmação, à ingestão local do
  [ADR-021](../decisions/ADR-021-Multimodal-Ingestion.md), com os mesmos limites,
  sanitização e revisão de fatos.

## Decisões para a implementação futura

Escolher navegador ou aplicativo local e definir como a sessão será vinculada
na Pi, como identificar remetentes e como proteger os dados da sessão. A eventual
automação precisa ser especificada antes de instalar dependências. A interface
local é a decisão de 28/11/2026; ela não cria uma integração automática por si só.
Esses itens não bloqueiam voz, documentos ou Outlook.

O adaptador deverá normalizar mensagens para as interfaces existentes do motor,
isolar sessões e recusar remetentes não autorizados. Dados de contato e conteúdo
ficam fora dos logs. Reentregas precisam de deduplicação persistente; confirmações
e ações mantêm seus controles de autorização. Esses comportamentos são requisitos
futuros e ainda não foram implementados ou testados como canal WhatsApp.
