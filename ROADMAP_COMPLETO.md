# ROADMAP COMPLETO DO PROJETO

FASE 1 — CONGELAR O QUE JÁ FUNCIONA
PROMPT 1 — Auditoria completa
Leia absolutamente todo o projeto sem modificar comportamento.

Crie:

ARQUITETURA_ATUAL.md
REGRESSION_CHECKLIST.md
RISCOS_ATUAIS.md
MAPA_DE_DADOS.md

Documente:
- módulos;
- scripts;
- fluxo de execução;
- FFmpeg;
- Whisper;
- Ollama;
- YouTube;
- TikTok;
- múltiplas contas;
- perfis Chrome;
- processamento em lote;
- agendamentos;
- geração de texto;
- Content ID;
- fingerprints;
- metadata;
- JSONs;
- caches;
- arquivos temporários;
- logs;
- dependências;
- scripts auxiliares.

Identifique:
- pontos que encerram toda a fila;
- risco de duplicação;
- falsa confirmação;
- perda de estado;
- corrupção;
- vazamento de segredo;
- código duplicado;
- funções excessivamente acopladas.

Não refatore nada nesta etapa.
PROMPT 2 — Especificação das invariantes
Crie PRODUCT_INVARIANTS.md.

Formalize as regras que nenhuma alteração futura poderá quebrar.

Inclua:
- funções independentes;
- operação em lote;
- não destrutividade;
- recuperação após crash;
- idempotência;
- privacidade;
- separação local/cloud;
- confirmação de publicação;
- templates independentes;
- Smart Clip local;
- pause/resume;
- compatibilidade de dados antigos;
- update seguro.

Nenhuma alteração funcional ainda.
PROMPT 3 — Testes de regressão
Crie uma suíte inicial de testes automatizados do comportamento atual.

Cubra tudo que puder ser testado sem abrir navegador real:
- fingerprint;
- estado;
- configurações;
- horários;
- nomes;
- caminhos;
- parsing de IA;
- detecção de vídeo já processado;
- numeração;
- geração de slots;
- cache;
- funções FFmpeg via mock;
- Ollama via mock.

Não faça grande refatoração.

Objetivo: as próximas etapas precisam detectar quando algo antigo foi quebrado.

FASE 2 — FUNDAÇÃO LOCAL
PROMPT 4 — Diretórios definitivos do produto

Crie uma abstração central de caminhos do aplicativo.

Separar código instalado de dados mutáveis.

Planejar estrutura semelhante a:

%LOCALAPPDATA%\<Produto>\
    database\
    accounts\
    projects\
    cache\
    logs\
    temp\
    templates\
    backups\
    models\
    support\

Não espalhar paths hardcoded.

Criar migração/compatibilidade com estrutura atual.

Atualizações futuras jamais poderão apagar esses dados.
PROMPT 5 — Modelos de domínio
Crie modelos independentes da UI e das plataformas:

Video
SourceAsset
Project
Account
Job
Publication
Schedule
Template
Artifact
ErrorRecord

Use UUIDs persistentes.

Separe claramente:
origem do vídeo,
projeto de edição,
job de processamento,
job de publicação.

Um Video pode gerar vários Jobs e Publications.
PROMPT 6 — State Machine formal
Crie uma State Machine central para Job.

Estados mínimos:

PENDING
PROCESSING
READY
PAUSED
INTERRUPTED
RECOVERING
SCHEDULED
PUBLISHING
PUBLISHED
RETRY
FAILED
BLOCKED
UNKNOWN
AUTH_REQUIRED
USER_ACTION_REQUIRED
CANCELLED

Defina transições válidas.

Exemplos:

PUBLISHED não pode voltar arbitrariamente a PENDING.

UNKNOWN precisa passar por reconciliação antes de retry de publicação.

Valide transições no código e crie testes.
PROMPT 7 — Tempo e timezone
Padronize todo sistema de horário.

Guardar internamente timestamps UTC.

Schedules devem possuir timezone IANA explícito.

Nunca depender apenas do timezone atual do Windows.

Tratar corretamente:
- mudança de timezone;
- horário de verão;
- exibição local;
- computador reiniciado em outro timezone.

Criar testes.
PROMPT 8 — SQLite LOCAL
Implemente SQLite exclusivamente local.

Criar schema versionado para:

videos
sources
projects
accounts
jobs
publications
schedules
templates
artifacts
errors
settings
audit_events
migrations

Usar:
- transactions;
- foreign keys;
- índices;
- WAL quando apropriado;
- migrations numeradas.

Adicionar testes CRUD e concorrência básica.

PROMPT 9 — Backup e recuperação local

Implemente sistema de backup do estado operacional.

Criar:
- backup periódico;
- backup antes de migration;
- backup antes de update importante;
- retenção configurável;
- integrity_check;
- validação antes de restore.

Nunca restaurar banco corrompido.

Não duplicar vídeos originais gigantes como parte do backup operacional.

Criar manifest para cada backup.
PROMPT 10 — Migração dos JSONs
Migre os estados JSON existentes para SQLite.

Requisitos:
- idempotente;
- backup dos arquivos antigos;
- não duplicar;
- checksum;
- validação de contagens;
- rollback;
- relatório da migração.

Executar a migração duas vezes deve produzir o mesmo resultado.
PROMPT 11 — Audit Log
Crie audit log operacional append-only.

Registrar eventos importantes como:
- job criado;
- mudança de estado;
- pause;
- resume;
- cancel;
- retry;
- processamento concluído;
- upload iniciado;
- confirmação;
- reconciliação;
- erro;
- recovery.

Deve ser possível reconstruir o histórico de um Job posteriormente.


FASE 3 — MOTOR QUE NÃO PERDE TRABALHO
PROMPT 12 — Job Engine
Crie JobEngine central.

Ele não pode depender diretamente:
- da UI;
- de YouTube;
- TikTok;
- Instagram.

Buscar Jobs no SQLite.

Persistir mudança de estado antes de avançar.

Nenhuma etapa importante pode depender apenas da memória do processo.
PROMPT 13 — Checkpoints
Crie checkpoints por etapa.

Exemplo:

IMPORTED
TRANSCRIBED
ANALYZED
EDIT_PLANNED
MEDIA_PROCESSED
COMPOSED
RENDERED
VALIDATED
UPLOAD_STARTED
REMOTE_CONFIRMED

Se ocorrer crash, etapas já concluídas e válidas não devem ser repetidas desnecessariamente.
PROMPT 14 — Recovery Manager
Crie RecoveryManager central.

Na inicialização:
- detectar Jobs abandonados;
- detectar PROCESSING/PUBLISHING interrompidos;
- verificar artifacts;
- validar arquivos temporários;
- transformar em INTERRUPTED;
- determinar ação segura.

Nunca assumir que upload interrompido falhou.

Operações remotas incertas devem ir para UNKNOWN e reconciliação.
PROMPT 15 — Pause, Resume, Stop e Cancel
Implemente:

pause
resume
stop_after_current
cancel

Nos níveis:
- global;
- conta;
- plataforma;
- fila;
- Job.

Pause deve esperar um ponto seguro quando possível.

Cancel não pode apagar original.

Persistir tudo no SQLite.

Reiniciar o computador deve manter a pausa.
PROMPT 16 — Graceful Shutdown
Implemente fechamento seguro.

Ao fechar:
- impedir novos Jobs;
- entrar em DRAINING;
- finalizar pontos seguros;
- persistir estado;
- fechar banco/processos corretamente.

Criar testes de encerramento forçado no meio de:
- processamento;
- transcrição;
- análise;
- renderização.

O sistema deve se recuperar ao reabrir.
PROMPT 17 — Batch Engine
Crie BatchEngine.

Ele deve receber 1, 10, 200 ou 500+ arquivos e gerar Jobs independentes.

Nunca iniciar todos simultaneamente.

Permitir:
- aplicar uma função em lote;
- aplicar várias funções;
- pausar lote;
- continuar;
- cancelar itens individuais;
- reprocessar apenas falhas.

Criar progresso:
total
pending
processing
ready
failed
attention_required.
PROMPT 18 — Resource Manager
Crie ResourceManager.

Controlar concorrência de:

FFmpeg
Whisper
Ollama
browsers
renderização
análise de frames

Levar em consideração:
CPU
RAM
GPU
VRAM

Criar perfis automáticos:
LOW
NORMAL
HIGH

Não deixar 200 Jobs iniciarem 200 processos.
PROMPT 19 — Storage Manager
Crie StorageManager.

Controlar:
- espaço livre;
- temp;
- cache;
- frames;
- artifacts;
- outputs intermediários.

Estimar necessidade de espaço antes de lote grande.

Detectar disco cheio.

Implementar limpeza segura.

Nunca excluir vídeo original automaticamente.
PROMPT 20 — Fila resiliente e Circuit Breaker
Refatore a fila.

Erro em vídeo A não pode parar B.

Erro em conta A não pode parar conta B.

Erro no TikTok não pode parar YouTube.

Implementar Circuit Breaker:

se um connector apresentar várias falhas sistêmicas iguais consecutivas, pausar somente aquele connector.

Outros módulos continuam trabalhando.
PROMPT 21 — Retry inteligente
Criar RetryPolicy central.

Categorias:

TEMPORARY
PERMANENT
RATE_LIMIT
AUTH_REQUIRED
CONTENT_BLOCKED
USER_ACTION_REQUIRED
UNKNOWN

Implementar:
- limite;
- backoff;
- jitter;
- next_retry_at persistente.

Nada de retry infinito.
PROMPT 22 — Idempotência
Crie proteção forte contra duplicação.

Publication deve possuir chave única baseada na operação adequada.

Antes de publicar:
- consultar estado local;
- verificar publicação conhecida;
- reconciliar UNKNOWN.

Nunca repostar automaticamente porque houve crash após upload.
PROMPT 23 — Secrets Manager
Crie SecretsManager.

Não guardar credenciais sensíveis gratuitamente em plaintext.

Criar abstração para mecanismos de proteção do Windows, como DPAPI/Credential Manager quando apropriado.

Chrome/browser profiles continuam locais e separados da instalação.

Nunca enviar esses dados ao backend.
FASE 4 — ENTRADA DE VÍDEOS
PROMPT 24 — Source Import Manager
Crie SourceImportManager.

Importadores independentes:

LocalFileImporter
FolderImporter
UrlImporter

Todos devem entregar SourceAsset padronizado ao restante do sistema.

Importador não deve possuir lógica de edição ou publicação.

Não implementar bypass de DRM/paywall/proteções.

O programa pode ter uma aceitação geral de termos, sem perguntar propriedade a cada vídeo.

IMPORT OPTIONS / MARCAÇÕES NA ENTRADA

SourceImportManager deve aceitar opções declarativas opcionais de importação, sem misturar importação com edição/publicação.

Criar conceito equivalente a ImportOptions com:

- user_assertions[];
- user_flags[];
- user_labels[];
- target_project_id opcional;
- target_account_id opcional;
- readiness_profile_id opcional.

Permitir aplicar um preset a todos os arquivos de uma importação ou somente aos selecionados.

Presets conceituais iniciais:

ORIGINAL
ALREADY_EDITED
READY_FOR_AI
USER_MARKED_READY
CUSTOM

O usuário pode importar mídia que já foi processada fora do produto e declarar, por exemplo:

EXTERNALLY_EDITED
EXTERNALLY_CAPTIONED
EXTERNALLY_METADATA_PREPARED
EXTERNALLY_REFRAMED
EXTERNALLY_TITLE_READY
EXTERNALLY_DESCRIPTION_READY
EXTERNALLY_HASHTAGS_READY
USER_MARKED_READY

Essas declarações são USER_ASSERTIONS. Elas NÃO podem ser confundidas com evidência automática do sistema.

Exemplo:

SYSTEM_BADGE CAPTIONS = o produto possui evidência operacional de captions.
USER_ASSERTION EXTERNALLY_CAPTIONED = o usuário informou que o arquivo já possui legenda.

Permitir:

- aplicar a um vídeo;
- aplicar a múltiplos vídeos;
- aplicar a todos durante importação;
- remover depois;
- editar em lote posteriormente.

Persistir origem da informação:

SYSTEM
USER
IMPORT_PRESET

Registrar timestamp da declaração sem armazenar segredo, conteúdo bruto ou caminho desnecessário no histórico.

Importante para campos de conteúdo:

EXTERNALLY_TITLE_READY / DESCRIPTION / HASHTAGS não substituem o valor real do título/descrição/hashtags quando uma publicação exigir esse payload.

Uma declaração manual pode indicar que o usuário considera o material pronto, mas não deve inventar conteúdo inexistente nem transformar um campo vazio em conteúdo válido.

PROMPT 25 — Source Context Resolver
Crie SourceContextResolver invisível ao usuário.

Para conteúdo de plataforma:
- tentar identificar canal/origem;
- verificar se corresponde a conta já conectada;
- classificar contexto técnico automaticamente.

Exemplos internos:

CONNECTED_CHANNEL_CONTENT
EXTERNAL_CONTENT
LOCAL_CONTENT
UNKNOWN_SOURCE

Não perguntar:
“esse vídeo é seu?”

Essa classificação não é determinação jurídica de propriedade.

Ela serve apenas para selecionar automaticamente o melhor pipeline técnico.
PROMPT 26 — Validação de mídia
Crie MediaProbe usando ffprobe ou equivalente.

Antes de processar verificar:

container
codec
duration
FPS
resolution
audio streams
corruption
file size

Arquivos inválidos devem gerar erro estruturado e não derrubar o lote.
FASE 5 — EDITOR MODULAR NÃO DESTRUTIVO
PROMPT 27 — Modelo de projeto de edição
Crie modelo de edição não destrutivo.

O Project deve armazenar decisões, não alterar source.

Exemplos:

cuts
speed
crop
reframe
audio settings
captions
template
text layers
metadata mode

Trocar apenas um elemento não deve invalidar processamento que ainda seja reutilizável.
PROMPT 27.5 — Media Catalog Backend

Crie a camada de catálogo de mídia que futuramente alimentará a Biblioteca visual do produto.

NÃO criar frontend nesta etapa.

OBJETIVO

Permitir consultar centenas ou milhares de vídeos como itens de catálogo, com estado resumido, evidências de processamento, marcações do usuário, filtros, paginação e dados suficientes para futura seleção em lote.

O catálogo NÃO deve duplicar a verdade operacional.

As fontes de verdade continuam sendo, conforme existirem:

SourceAsset
Video
Project
Job
Artifact
Publication
Schedule
ContentAnalysis
ContentEngine

Criar serviço equivalente a:

MediaCatalogService

com operações conceituais:

get_item(video_id)
list_items(...)
filter_items(...)
count_by_filter(...)
get_facets(...)
set_user_flag(...)
remove_user_flag(...)
add_user_label(...)
remove_user_label(...)
set_user_assertion(...)
remove_user_assertion(...)

CATALOG ITEM

Criar read model equivalente a MediaCatalogItem com:

video_id
source_asset_id
filename
display_name
created_at
duration quando conhecida
resolution quando conhecida
thumbnail_ref quando disponível

processing_status
system_badges[]
user_assertions[]
user_flags[]
user_labels[]

latest_project_id quando existir
latest_artifact_id quando existir
latest_operation
error_summary seguro quando existir

content_status:
- title_available
- description_available
- hashtags_available
- transcript_available
- captions_available

publication_summary:
- destinations_count
- scheduled_count
- published_count
- failed_count

Não armazenar booleanos redundantes quando puder derivar de evidência real.

SYSTEM BADGES

O catálogo deve suportar badges automáticos derivados de evidência real, como:

IMPORTED
VALIDATED
EDITED
CAPTIONS
METADATA_CLEAN
REFRAMED_9_16
AUDIO_PROCESSED
TEMPLATE_APPLIED
AI_TITLE
AI_DESCRIPTION
AI_HASHTAGS
TRANSCRIBED
RENDERED
READY
SCHEDULED
PUBLISHED
ERROR

Esses nomes representam capacidades/estados do catálogo, não necessariamente colunas físicas.

Exemplos de regra:

CAPTIONS só aparece automaticamente quando houver evidência operacional suficiente de captions produzidas/aplicadas.

AI_TITLE só aparece automaticamente quando existir título persistido válido gerado pelo ContentEngine ou mecanismo oficialmente reconhecido.

AI_DESCRIPTION e AI_HASHTAGS seguem a mesma regra.

METADATA_CLEAN só aparece automaticamente quando a operação correspondente tiver concluído com resultado validado.

RENDERED só aparece quando existir Artifact renderizado válido.

READY automático não pode nascer apenas de um checkbox manual.

Crash, falha parcial ou arquivo ausente não podem produzir badge positivo falso.

SYSTEM FACT x USER ASSERTION

Separar completamente:

SYSTEM_BADGE
USER_ASSERTION
USER_FLAG
USER_LABEL

USER_ASSERTION descreve algo que o usuário afirma já existir externamente.

USER_FLAG representa organização/decisão humana. Exemplos:

REVIEWED
APPROVED
FAVORITE
NEEDS_REWORK
PRIORITY
USER_MARKED_READY

USER_LABEL é rótulo livre, por exemplo:

Cliente A
Postar amanhã
Campanha X
Teste
Melhores

Uma marcação manual nunca pode falsificar evidência automática do sistema.

CATALOG EVIDENCE CONTRACT

Todo módulo futuro de processamento relevante deve produzir evidência estruturada suficiente para o catálogo derivar seus badges sem depender de texto de log ou nome de arquivo.

Isso inclui, quando aplicável:

Editor -> EDITED
CaptionsEngine -> CAPTIONS
MetadataManager -> METADATA_CLEAN
AutoReframe -> REFRAMED_9_16
AudioEngine -> AUDIO_PROCESSED
Template/Render -> TEMPLATE_APPLIED / RENDERED
ContentEngine -> AI_TITLE / AI_DESCRIPTION / AI_HASHTAGS
Scheduler/Connectors -> SCHEDULED / PUBLISHED

Não acoplar esses módulos ao frontend.

FILTROS

O backend precisa permitir combinações como:

com legenda
sem legenda
editados
não editados
metadata limpa
sem metadata limpa
com título IA
sem título
com descrição IA
sem descrição
com hashtags
sem hashtags
renderizados
com erro
agendados
publicados
não publicados
USER_MARKED_READY
com determinada user flag
com determinada user label
com determinada user assertion

Filtros devem poder ser combinados com AND e negação quando fizer sentido.

Exemplo:

CAPTIONS
AND METADATA_CLEAN
AND AI_TITLE
AND NOT PUBLISHED

Planejar paginação e ordenação determinística.

Não carregar todos os vídeos em memória para depois filtrar quando houver alternativa de consulta eficiente.

BULK EDIT DE MARCAÇÕES MANUAIS

Sobre um conjunto de vídeos, permitir editar USER_ASSERTIONS, USER_FLAGS e USER_LABELS em massa.

Cada campo deve suportar três estados:

KEEP
SET
UNSET

Exemplo:

87 vídeos selecionados

EXTERNALLY_EDITED = KEEP
EXTERNALLY_CAPTIONED = SET
FAVORITE = UNSET

Uma operação em massa não pode apagar silenciosamente outras marcações que o usuário não pediu para alterar.

Não permitir alterar SYSTEM_BADGES por bulk edit manual.

Quando a operação envolver persistência múltipla, usar transação quando apropriado e retornar resultado estruturado:

requested
updated
unchanged
failed

ESCALA

Testar:

0 vídeos
1 vídeo
100
1.000
10.000 registros sintéticos

Sem depender de thumbnails pesados ou leitura dos arquivos de vídeo para listar o catálogo.

Listagem deve usar metadata persistida/read model.

PERSISTÊNCIA

Se for necessário novo schema SQLite nesta etapa, criar NOVA migration numerada.

Nunca alterar migrations já congeladas.

Persistir apenas o necessário.

SYSTEM_BADGES derivados não precisam obrigatoriamente de tabela própria.

USER_FLAGS, USER_LABELS e USER_ASSERTIONS precisam sobreviver a restart.

Adicionar índices para filtros relevantes.

TESTES

Cobrir:

- item sem processamento;
- item com múltiplos Artifacts;
- job concluído;
- job falho;
- artifact ausente;
- artifact inválido;
- badge não aparece em falso;
- user assertion persistente;
- user flag persistente;
- labels personalizadas;
- bulk edit KEEP/SET/UNSET;
- filtros combinados;
- paginação;
- restart;
- milhares de itens;
- nenhum vazamento entre vídeos.

Não criar frontend.

PROMPT 28 — Editor: corte e timeline
Implemente módulo de edição temporal.

Funções:
- trim;
- split;
- remover trecho;
- juntar trechos;
- reorganizar quando suportado;
- alterar velocidade.

Salvar tudo como Edit Decisions.

Não alterar vídeo original.
PROMPT 29 — Editor: vídeo e imagem
Crie módulo de transformações visuais.

Suportar:
- crop;
- resize;
- fit/fill;
- zoom;
- position;
- rotation quando necessário;
- brightness;
- contrast;
- saturation;
- gamma;
- sharpen;
- noise;
- ajustes existentes no protótipo.

Tudo deve poder funcionar sozinho e em lote.
PROMPT 30 — Audio Engine
Crie AudioEngine.

Suportar:
- volume;
- mute;
- normalization;
- clipping protection;
- gain;
- fade;
- redução de ruído opcional;
- música/background quando posteriormente configurada.

Preservar sincronização.
PROMPT 31 — Captions Engine
Crie CaptionsEngine.

Whisper deve gerar timestamps.

Suportar:
OFF
SEPARATE
BURNED

Gerar:
SRT
VTT
representação interna.

Cachear transcrição por fingerprint + configuração/modelo.

Não retranscrever sem necessidade.

EVIDÊNCIA PARA CATÁLOGO

Quando captions forem realmente produzidas/aplicadas com sucesso, registrar evidência estruturada que permita ao MediaCatalog derivar CAPTIONS.

Falha, cancelamento, arquivo ausente ou output inválido não podem produzir esse badge.

PROMPT 32 — Estilos de legenda
Crie sistema de estilos de legenda.

Configurar:
font
size
position
alignment
stroke
shadow
background
max words
lines
animation
current-word highlight

Criar no mínimo três estilos profissionais.

Nada hardcoded no renderer.
PROMPT 33 — Auto Reframe / 9:16
Crie AutoReframe opcional.

Converter horizontal/quadrado para vertical.

Detectar:
- rosto;
- pessoa;
- região relevante.

Suavizar tracking.

Se detector falhar:
usar fallback seguro.

Nunca bloquear Job por falha de tracking.
PROMPT 34 — Remoção de silêncio
Crie SilenceRemoval como ferramenta independente.

Configurar:
threshold
minimum duration
padding

Padrão conservador.

Depois dos cortes:
corrigir vídeo,
áudio,
captions.

Criar testes de sincronização.
PROMPT 35 — Metadata Manager
Crie MetadataManager independente.

Modos:

KEEP
CLEAN
PROFILE

KEEP:
preservar quando possível.

CLEAN:
remover metadata desnecessária.

PROFILE:
aplicar perfil configurado.

A ferramenta deve funcionar sozinha em lote.

Ao preparar/renderizar um projeto que não tenha decisão de metadata, mostrar uma escolha simples:

Manter informações do arquivo
Limpar informações do arquivo

Permitir:
“lembrar minha preferência”.

Perfil avançado pode ficar em Configurações.

Nunca apresentar metadata como recurso para burlar plataforma.
FASE 6 — TEMPLATES DO JEITO DEFINIDO

EVIDÊNCIA PARA CATÁLOGO

Quando MetadataManager concluir CLEAN com resultado validado, registrar evidência estruturada suficiente para derivar METADATA_CLEAN.

KEEP ou PROFILE não podem ser confundidos com CLEAN.

PROMPT 36 — Template Engine
Crie TemplateEngine interno.

Não construir editor gráfico completo estilo Canva.

Template deve descrever composição reutilizável.

Suportar:
- background;
- video zone;
- caption zone;
- AI text zone;
- logo/image zones;
- layer order;
- styling.

Usar coordenadas normalizadas 0..1 para funcionar em diferentes resoluções.
PROMPT 37 — Três templates oficiais
Crie arquitetura para 3 templates built-in.

Eles devem vir prontos para uso.

O código não deve depender exatamente desses três.

Futuramente podemos adicionar mais via update.

Criar preview/thumbnail e metadata do template.

Os 3 templates built-in desta etapa serão fornecidos pelo proprietário do produto.

Não criar três artes genéricas substitutas se os arquivos oficiais forem fornecidos.

Incorporar esses três templates como templates oficiais do produto, preservando identidade visual, proporções e arquivos originais.

Os templates devem ser tratados como assets oficiais/built-in, mas continuar usando o mesmo Template Engine genérico disponível para templates importados.

Não achatar no código posições que deveriam pertencer ao modelo de template.

Manter suporte futuro a Layout Mapper para VIDEO, CAPTION, AI_TEXT e IMAGE/LOGO.

Os arquivos originais fornecidos não devem ser sobrescritos durante edição/testes.



PROMPT 38 — Template Importer
Permitir importar PNG/JPG como base de template.

O usuário NÃO desenha a arte dentro do programa.

Ele traz arte pronta de Canva/Photoshop/etc.

Criar processo simples:

Importar imagem
→ configurar áreas
→ salvar como template.

Assets do template devem ficar organizados fora da instalação principal.
PROMPT 39 — Layout Mapper
Crie Layout Mapper visual simples.

Sobre a imagem importada, permitir posicionar/redimensionar:

VIDEO
CAPTION
AI_TEXT
IMAGE/LOGO quando necessário.

Usar drag and resize.

Não transformar isso num editor gráfico completo.

O usuário está apenas ensinando ao programa onde conteúdo dinâmico deve aparecer.
PROMPT 40 — Texto dinâmico no template
Adicionar DynamicContent.

Uma zona de texto pode receber:

FIXED_TEXT
AI_HOOK
AI_TITLE
AI_SUMMARY
AI_QUESTION
AI_CTA
CUSTOM_AI

Implementar:
- máximo de linhas;
- máximo de caracteres;
- auto-fit;
- font size mínimo;
- alinhamento.

Se texto não couber, gerar versão menor de forma controlada.

Nunca deixar texto sair do canvas.
PROMPT 41 — Templates independentes do workflow
Garanta arquiteturalmente que Template NÃO seja uma etapa obrigatória.

Deve ser possível:

vídeo pronto → template → exportar

Smart Clip → salvar cortes sem template

vídeo editado → aplicar template

200 vídeos → aplicar template em lote

trocar template depois da edição

remover template

Trocar template não pode exigir repetir Whisper/análise/cortes se nada disso mudou.
PROMPT 42 — Render Engine
Crie RenderEngine separado da UI.

Entrada:

Source
Edit Decisions
Template opcional
Captions opcionais
Dynamic text
Images/assets
Audio settings
Metadata settings

Saída:
MP4 final.

Construir pipeline FFmpeg de forma modular.

Usar arquivos temporários + rename atômico.

Nunca considerar render completo enquanto output não for validado.

EVIDÊNCIA PARA CATÁLOGO

Render concluído só pode produzir RENDERED quando o Artifact correspondente existir e passar pelas validações exigidas.

A existência de arquivo parcial não basta.

PROMPT 43 — Quality Control
Crie FinalMediaValidator.

Antes de marcar READY verificar:

arquivo existe
duration válida
resolução
codec
áudio
tamanho > mínimo
sem output truncado
captions dentro do canvas quando verificável
texto dentro das zonas
artifact íntegro

Tentar correção automática somente quando segura.

Caso contrário:
USER_ACTION_REQUIRED ou FAILED apropriado.
FASE 7 — SMART CLIP
PROMPT 44 — Transcrição e segmentação de conteúdo longo
Crie base do SmartClipEngine.

Receber vídeo longo.

Transcrever.

Dividir semanticamente em assuntos/segmentos.

Detectar:
- começo da ideia;
- desenvolvimento;
- conclusão;
- mudanças de assunto.

Não escolher corte somente pelo timestamp da frase mais forte.
PROMPT 45 — Detecção local de melhores momentos
Smart Clip deve funcionar sem Google Analytics e sem API externa obrigatória.

Usar sinais locais:

transcrição
contexto
ritmo de fala
perguntas
respostas
hooks
histórias
emoção
frases fortes
mudanças visuais
áudio
completude do raciocínio

Gerar candidatos com scores internos.

Não chamar score de “visualizações” ou “retenção real”.
PROMPT 46 — Tipos de conteúdo automáticos
Detectar automaticamente o tipo provável de conteúdo:

podcast/interview
tutorial
vlog
gameplay
talking-head
general

A estratégia do Smart Clip pode mudar internamente.

Usuário não precisa escolher obrigatoriamente.

Permitir override avançado caso queira.
PROMPT 47 — Ranking e contexto dos cortes
Cada candidato deve possuir internamente:

start_time
end_time
duration
hook_score
context_score
speech_score
visual_score
completion_score
overall_score
reason

Expandir automaticamente início/fim para evitar:
- frases cortadas;
- história sem contexto;
- conclusão faltando.

Evitar sobreposição excessiva entre cortes.
PROMPT 48 — Tela de seleção Smart Clip
Criar interface conceitual para resultados:

Corte 1
título/resumo
00:12:21 → 00:13:04
Excelente corte
Preview

Usuário pode:
- aceitar;
- rejeitar;
- editar início/fim;
- selecionar todos.

Não expor scores técnicos por padrão.

Cada corte aprovado vira novo Video/Project/Job normal.
FASE 8 — IA QUE ENTREGA PRONTO
PROMPT 49 — Content Analysis multimodal
Crie ContentAnalysis local.

Combinar:
transcrição
frames representativos
informações de áudio quando úteis

Gerar estrutura validada:

topic
summary
language
visual_context
important_moments
speakers quando inferível sem depender de identidade
tone
content_type

Controlar quantidade de frames para não destruir performance.
PROMPT 50 — Content Engine
Crie ContentEngine com Ollama.

Gerar JSON validado:

hook
title
description
hashtags
CTA
summary
suggested_caption_style
suggested_template quando apropriado

Versionar prompts e schemas.

Resposta inválida nunca pode quebrar fila.

Implementar retry/fallback.

EVIDÊNCIA PARA CATÁLOGO

ContentEngine deve persistir separadamente os campos realmente válidos produzidos.

O catálogo poderá derivar:

AI_TITLE somente quando title válido existir;
AI_DESCRIPTION somente quando description válida existir;
AI_HASHTAGS somente quando hashtags válidas existirem.

Uma resposta parcial não pode marcar campos ausentes como prontos.

PROMPT 51 — Creative Planner / modo automático
Crie CreativePlanner.

É o cérebro do modo:

CRIAR AUTOMATICAMENTE.

Ele utiliza os módulos existentes, sem duplicar lógica.

Pode decidir automaticamente:

corte
reframe
silêncio
legenda
estilo
template
texto IA
áudio
metadata conforme preferência
render

Ele respeita preferências e templates do usuário.

O objetivo:

entrada → vídeo final pronto.

Todos os módulos continuam utilizáveis separadamente.

O CreativePlanner deve reconhecer dois modos de scheduling:

MANUAL
O usuário escolheu explicitamente datas/horários. O planner nunca substitui essa decisão automaticamente.

RECOMMENDED
O usuário autorizou o sistema a escolher/recomendar datas e horários.

O CreativePlanner não deve inventar a expressão “melhor horário comprovado” sem evidência.

Ele deverá solicitar recomendações ao futuro Scheduling Recommendation Engine e manter rastreabilidade da origem da recomendação.

O modo automático “Criar automaticamente” pode utilizar RECOMMENDED quando essa preferência estiver habilitada pelo usuário.


PROMPT 52 — Perfis por canal
Crie ContentProfile por canal.

Configurações:

language
country
timezone
niche
tone
title style
caption preferences
CTA
hashtags
forbidden words
preferred words
default template
metadata preference

O modo automático usa isso sem perguntar toda vez.
PROMPT 52.5 — Publication Readiness Profiles

Crie sistema de critérios de prontidão para publicação.

NÃO usar um único booleano global video.ready = true.

"Pronto" depende do fluxo, destino e preferência do usuário.

CONCEITO

Criar modelo equivalente a ReadinessProfile.

Exemplo:

SHORTS_COMPLETO

required:
EDITED
CAPTIONS
METADATA_CLEAN
AI_TITLE
AI_DESCRIPTION
AI_HASHTAGS
RENDERED

Outro perfil:

POST_RAPIDO

required:
AI_TITLE
AI_DESCRIPTION
RENDERED

O mesmo vídeo pode estar READY para um perfil e NOT_READY para outro.

INTEGRAÇÃO COM CONTENTPROFILE

ContentProfile por canal pode indicar default_readiness_profile ou requisitos equivalentes.

Exemplo:

Canal EUA:
CAPTIONS obrigatório
AI_TITLE obrigatório
AI_DESCRIPTION obrigatório
METADATA_CLEAN obrigatório

Canal interno:
CAPTIONS opcional

Não acoplar readiness diretamente a uma plataforma específica.

AVALIAÇÃO

Criar:

evaluate_readiness(video_id, readiness_profile)

Resultado estruturado:

READY
NOT_READY
BLOCKED
ERROR

com:

missing_requirements[]
satisfied_requirements[]

Exemplo:

READY = false
missing = AI_DESCRIPTION, CAPTIONS

SYSTEM x MANUAL

APPROVED, REVIEWED e USER_MARKED_READY são marcações humanas e não substituem automaticamente READY do sistema.

Um vídeo pode estar:

READY = true
APPROVED = false

ou:

READY = false
USER_MARKED_READY = true

A futura UI deve conseguir distinguir esses casos.

USER_ASSERTIONS podem ser aceitas por um ReadinessProfile somente quando o requisito explicitamente permitir declaração externa.

Para campos que exigem payload real para publicação, como title/description/hashtags, uma simples declaração manual não substitui o valor efetivamente persistido.

TESTES

Cobrir:

- todos requisitos presentes;
- um faltando;
- vários faltando;
- artifact inválido;
- erro em job;
- profile vazio;
- profiles diferentes para mesmo vídeo;
- ContentProfiles diferentes;
- USER_MARKED_READY sem evidência;
- user assertion aceita quando explicitamente permitida;
- campo textual declarado mas sem payload real;
- restart;
- avaliação em lote.

Não criar UI.

FASE 9 — 200 VÍDEOS AUTOMÁTICOS
PROMPT 53 — Preparação automática em lote
Integre BatchEngine + CreativePlanner.

A pessoa seleciona 200 vídeos e configura uma vez.

Exemplo:

Modo automático
Template X
Legenda Viral
Metadata CLEAN
Destino Y

Cada vídeo deve receber análise individual.

Não aplicar cegamente as mesmas decisões de crop/hook/contexto em todos.

Processar conforme capacidade do PC.

Exibir progresso real e permitir pause/resume.
PROMPT 53.5 — Catalog Selection e Publication Preparation Backend

Crie backend de seleção de vídeos para ações em lote e preparação de publicação.

NÃO criar frontend nesta etapa.

A futura UI precisa conseguir:

- aplicar filtros;
- ver somente vídeos relevantes;
- selecionar itens;
- selecionar uma página;
- selecionar todos os resultados filtrados;
- desmarcar exceções;
- revisar preview/resumo;
- preparar somente os selecionados para publicação/agendamento.

FILTER FIRST, SELECT SECOND

Filtro define o conjunto consultável.

Seleção define o subconjunto que receberá ação.

Exemplo:

Filtro:
READY_FOR(profile)
AND NOT PUBLISHED
AND CAPTIONS
AND AI_TITLE

Resultado:
127 vídeos.

SELECT_ALL_FILTERED -> 127 selecionados.

Depois o usuário remove video_032 e video_071.

Resultado -> 125 selecionados.

SELECT PAGE x SELECT ALL FILTERED

Distinguir explicitamente:

SELECT_PAGE
SELECT_ALL_FILTERED_RESULTS

Se a consulta possui 2.000 vídeos e a página mostra 50:

Selecionar página = 50.
Selecionar todos os resultados = 2.000.

Nunca selecionar milhares silenciosamente por causa de um checkbox visual de página.

SELECTION SET

Criar conceito equivalente a MediaSelection / SelectionSet.

Suportar:

add(video_id)
remove(video_id)
clear()
select_page(...)
select_all_matching(filter_snapshot)
count_selected()
resolve_selected_ids()

Para escala, permitir representar seleção conceitualmente como:

ALL_MATCHING(filter_snapshot)
+ exclusions

quando apropriado, em vez de persistir milhares de IDs desnecessariamente.

SNAPSHOT / CORRIDA

Ao confirmar preparação de lote, resolver/congelar explicitamente os IDs selecionados ou usar snapshot equivalente.

Novo vídeo que fique READY depois não pode entrar silenciosamente no lote já confirmado.

Vídeo que deixe de cumprir requisito antes da confirmação deve gerar revalidação/aviso, não publicação silenciosa.

PUBLICATION PREVIEW MODEL

Criar estrutura equivalente a PublicationCandidatePreview:

video_id
thumbnail_ref
filename
duration
readiness_status
missing_requirements[]
title_preview
description_preview
hashtags_preview
target_account_id
target_platform
target_language
schedule_preview quando existir
warnings[]
already_published
already_scheduled

Nenhum preview deve executar publicação.

AÇÃO SOMENTE SOBRE SELECIONADOS

Backend de lote/publicação deve receber explicitamente selection_snapshot ou selected_video_ids.

Nunca assumir automaticamente:

todos os vídeos da pasta
todos os vídeos da conta
todos os READY

sem intenção explícita.

FILTRO "TERMINADOS"

Criar filtro:

READY_FOR(profile_id)

"Mostrar somente terminados" no futuro frontend deve equivaler a essa avaliação, e não a regra hardcoded na UI.

Também permitir filtros separados para:

USER_MARKED_READY
APPROVED
REVIEWED

sem confundi-los com READY verificado.

FILTROS DE PUBLICAÇÃO

Suportar combinações como:

READY_FOR(profile)
NOT_PUBLISHED
NOT_SCHEDULED
PLATFORM = YOUTUBE
ACCOUNT = X
USER_LABEL = CAMPANHA_A
LANGUAGE = EN

BULK EDIT DE MARCAÇÕES

Sobre a MediaSelection, permitir alterações em massa de USER_ASSERTIONS, USER_FLAGS e USER_LABELS.

Cada valor deve suportar:

KEEP
SET
UNSET

Exemplo:

87 selecionados
EXTERNALLY_EDITED = KEEP
EXTERNALLY_CAPTIONED = SET
FAVORITE = UNSET

Não alterar SYSTEM_BADGES.

Quando apropriado, operação deve ser transacional e retornar:

requested
updated
unchanged
failed

TESTES

Cobrir:

- filtro reduz conjunto;
- select page;
- select all filtered;
- exclusão individual;
- re-inclusão;
- clear;
- mudança de filtro;
- snapshot não muda após confirmação;
- vídeo fica READY depois do snapshot;
- vídeo deixa de estar READY antes da confirmação;
- candidato já publicado;
- candidato já agendado;
- bulk edit KEEP/SET/UNSET;
- 10.000 itens;
- nenhuma publicação real.

Não criar frontend.

PROMPT 54 — Ferramentas individuais em lote
Toda função compatível deve aceitar lote isoladamente.

Exemplos:

200 vídeos → somente CLEAN metadata

200 vídeos → somente captions

200 vídeos → somente template

200 vídeos → somente áudio

200 vídeos → somente 9:16

200 vídeos → editar automaticamente tudo

Criar mesma arquitetura de Jobs para todos os casos.
PROMPT 55 — Folder Autopilot
Crie monitor de pasta.

Ao aparecer arquivo:
- esperar terminar a cópia;
- verificar estabilidade de tamanho;
- validar;
- fingerprint;
- cadastrar;
- criar Job conforme automação configurada.

Tratar:
arquivo bloqueado
arquivo incompleto
disco cheio
duplicação
fila muito grande.

Não processar arquivo ainda sendo copiado.
FASE 10 — AGENDAMENTO E PLATAFORMAS
PROMPT 56 — Scheduler em background
Crie scheduler local robusto.

Diferenciar:

REMOTE_SCHEDULED
LOCAL_PENDING

Se a plataforma já recebeu o arquivo/agendamento, PC pode estar desligado.

Se depende de ação local futura, deixar isso claro internamente.

Implementar processo/tray/background apropriado.

Startup com Windows deve ser opcional.

Janela principal não precisa permanecer aberta.

Implementar dois modos de agendamento:

Horário manual

usuário define dia/hora;
pode definir vários horários;
pode reutilizar padrão;
sistema nunca altera silenciosamente a escolha.

Horário recomendado

sistema escolhe/recomenda dias e horários por conta e plataforma;
gera calendário para lote inteiro;
evita horários duplicados/conflitantes;
respeita quantidade máxima configurada de posts por dia;
usuário pode revisar/alterar antes da publicação.

Criar um SchedulingRecommendationEngine separado do scheduler de execução.

Fontes de sinal, em ordem de preferência:

dados reais da própria conta, quando legitimamente disponíveis;
histórico local de desempenho da conta, quando disponível;
sinais fornecidos pelo connector/plataforma;
heurísticas configuráveis por plataforma como fallback.

Heurística nunca deve ser apresentada como “melhor horário comprovado”.

A interface poderá usar rótulos como:

Recomendado
Baseado no desempenho da sua conta
Sugestão geral

Registrar no Schedule a origem da decisão, por exemplo:
USER_MANUAL
ACCOUNT_DATA
LOCAL_HISTORY
PLATFORM_SIGNAL
PLATFORM_HEURISTIC

O resultado do Recommendation Engine é apenas uma intenção de agendamento. A confirmação de que a plataforma recebeu exatamente aquele horário pertence ao Connector correspondente.


PROMPT 57 — Connector Contract
Crie interface padrão:

authenticate
check_session
capabilities
upload
schedule
confirm
reconcile
get_status
handle_error

Capabilities devem declarar recursos realmente suportados.

JobEngine não pode conhecer seletores ou detalhes de plataforma.
PROMPT 58 — YouTube Connector
Adapte YouTube atual ao contrato.

Preservar comportamento útil existente.

Fortalecer:
upload
schedule
confirmation
reconciliation
Content ID
logout
limites
network failure
browser crash

Nunca marcar sucesso somente porque clicou.

Evitar dependência obrigatória de API/Analytics para funções centrais.


Ao aplicar um Schedule no YouTube, não assumir sucesso apenas porque os controles foram manipulados.

Antes da ação final, validar que data, hora e timezone exibidos/representados pela plataforma correspondem ao Schedule esperado.

Exemplo:
Schedule esperado = 20/09/2026 18:30 America/Sao_Paulo.

O connector deve confirmar semanticamente que a plataforma recebeu esse horário antes de concluir o fluxo.

Divergência impede confirmação de sucesso.

Se após ação remota não for possível determinar o resultado, usar UNKNOWN/reconciliation, nunca reenviar cegamente.


PROMPT 59 — TikTok Connector
Adapte TikTok ao contrato.

Eliminar confirmação otimista.

Se não puder confirmar:
UNKNOWN.

Antes de repostar:
reconcile.

CAPTCHA/challenge/2FA:
USER_ACTION_REQUIRED.

Tratar:
logout
rate limit
browser failure
network failure.

BUG REAL JÁ OBSERVADO:

Durante o agendamento no TikTok Studio, a automação pode calcular um horário correto internamente, mas selecionar/preencher outro horário na interface.

Esse bug deve ser corrigido nesta etapa e NÃO pode ser considerado resolvido apenas pela correção de build_slots.

Separar:

expected_schedule = horário que o sistema pretende agendar.

applied_schedule = horário efetivamente presente na interface TikTok.

Antes do clique final de agendamento, o connector deve reler os controles da UI e verificar:

data;
hora;
minuto;
quando aplicável, contexto/timezone.

Exemplo:

Esperado: 20/09/2026 18:30

UI TikTok: 20/09/2026 19:00

Resultado obrigatório:
NÃO clicar em agendar.

Gerar erro recuperável e corrigir/reaplicar o horário.

Nunca continuar silenciosamente com horário divergente.

Depois do envio, obter confirmação positiva sempre que possível.

Se houve ação remota mas o resultado não puder ser confirmado:
UNKNOWN → RECOVERING

e nunca retry automático.

Adicionar testes para a lógica de comparação expected/applied sem browser real e testes de integração isolados da camada de UI.

Remover qualquer lógica que escolha horários aleatórios durante a etapa de preenchimento da interface, salvo se o Schedule recebido explicitamente tiver sido gerado pelo modo RECOMMENDED.


PROMPT 60 — Instagram Connector
Integrar o agendador Instagram desenvolvido separadamente sobre a antiga base TikTok.

Separar:

helpers realmente genéricos
de
lógica específica do TikTok.

InstagramConnector não deve importar comportamento específico de TikTok.

Adicionar sua própria:
confirmation
reconciliation
error handling
capabilities.

Existe uma implementação de automação/agendamento de Instagram já desenvolvida pelo proprietário do produto.

Antes de implementar este prompt, solicitar/usar esse código como baseline funcional.

Não reescrever do zero sem analisar o que já funciona.

Transformar a implementação existente em InstagramConnector, adaptando-a ao contrato comum de connectors criado no Prompt 57.

Preservar comportamento comprovadamente funcional sempre que não conflitar com:

State Machine;
idempotência;
confirmação remota;
segurança;
privacidade;
Schedule;
USER_ACTION_REQUIRED.

O Instagram foi originalmente desenvolvido a partir de ideias/código relacionados ao TikTok. Compartilhar somente helpers realmente genéricos.

Não deixar dentro do InstagramConnector seletores, estados, nomes, regras ou suposições específicas do TikTok.

O Instagram também deve receber Schedule canônico e validar que o horário efetivamente aplicado corresponde ao horário pretendido.

Integrar tanto horários MANUAL quanto RECOMMENDED.


PROMPT 61 — Kwai experimental
Investigar antes de implementar Kwai.

Criar Connector somente para funcionalidades comprovadamente confiáveis.

Não deixar Kwai atrasar ou quebrar YouTube/TikTok/Instagram.

Recursos instáveis devem ficar EXPERIMENTAL.


Se o Kwai oferecer suporte técnico confiável ao agendamento implementado nesta versão, consumir o mesmo Schedule e os modos MANUAL/RECOMMENDED.

Não inventar suporte nem bloquear o lançamento caso esse connector permaneça experimental.


FASE 11 — DIAGNÓSTICO

PROMPT 62 — Health Check, erros e suporte
Criar sistema unificado de saúde e diagnóstico.

Health Check:

CPU
RAM
GPU/VRAM
disk
internet
FFmpeg
Whisper
Ollama
models
browser
SQLite
folders
sessions
Windows compatibility

Criar Error Catalog com códigos estruturados.

Criar sanitizador de logs.

Criar Support Bundle voluntário com ID.

Nunca incluir:
cookies
tokens
passwords
Authorization headers
conteúdo dos vídeos
logs brutos sensíveis.

Conhecidos:
resolver por base determinística.

Desconhecidos:
Ollama pode sugerir causa/solução.

IA nunca altera código automaticamente.
FASE 12 — PRODUTO COMERCIAL
PROMPT 63 — Licença, planos, backend e updates
Agora criar camada comercial ONLINE separada do SQLite local.

BACKEND

Implementar:

customers
licenses
devices
activations
entitlements
plans
feature_flags
releases
remote_config
audit_log administrativo

LICENÇA

Primeira ativação com Device Fingerprint/HWID composto.

Não depender de um único serial.

Tolerar pequenas mudanças legítimas de hardware.

Servidor entrega device_id e token assinado.

Chave privada somente no servidor.

Permitir grace period offline.

PLANOS

Nunca usar lógica espalhada:
if plan == PRO.

Usar entitlements:

youtube
tiktok
instagram
kwai
smart_clip
batch
folder_autopilot
premium_templates
advanced_ai
max_accounts
etc.

ADMIN PANEL

Criar painel protegido para:

buscar cliente
buscar licença
ativar/desativar
resetar dispositivo
alterar plano
ver versão
gerenciar features
gerenciar releases
kill switch
consultar erros sanitizados autorizados

Implementar RBAC:

ADMIN
SUPPORT
OPERATIONS

Toda ação sensível gera audit log.

SELF SERVICE

Preparar mecanismos para usuário:
ver licença
ver dispositivos
ver plano
realizar ações permitidas sem suporte.

BILLING

Separar gateway de pagamento da lógica do produto.

Pagamento/evento
→ backend
→ entitlement.

Não acoplar core a um gateway específico.

REMOTE CONFIG / KILL SWITCH

Permitir desabilitar:

um connector
uma feature
uma versão específica

sem bloquear todo software.

RELEASES

Implementar canais:

DEV
BETA
STABLE

Nunca enviar versão recém-criada diretamente para todos.

SUPORTAR CANARY:

1%
5%
25%
50%
100%

UPDATER

Release contém:

version
changelog
download
size
SHA-256
signature
min/max compatibility
mandatory/optional

Antes do update:

preflight
espaço em disco
backup consistente
modo DRAINING
nenhuma operação crítica ativa

MIGRATIONS

Se update alterar SQLite:

copiar banco
executar migration na cópia
integrity_check
testar
somente depois migrar banco real.

ROLLBACK

Rollback precisa considerar:
binários
config schema
SQLite schema

Não restaurar apenas EXE incompatível com DB novo.

Permitir PAUSAR RELEASE remotamente.

Criar backup e disaster recovery também para o backend online.
PROMPT 63.5 — Catalog Application API / View Models

Prepare a API local de aplicação que o frontend comercial consumirá.

NÃO implementar frontend nesta etapa.

O objetivo é impedir que o Prompt 64 acesse SQLite diretamente ou reimplemente regras de negócio.

Expor services/use-cases para:

listar catálogo
buscar
filtrar
ordenar
paginar
obter facets/counts
avaliar readiness
alterar user assertions
alterar user flags
alterar user labels
criar seleção
selecionar página
selecionar todos filtrados
desselecionar item
limpar seleção
aplicar bulk edit
gerar preview de publicação
confirmar preparação do lote

CATALOG VIEW MODEL

Entregar ao frontend estrutura pronta e compacta.

Exemplo conceitual:

CatalogItemView

video_id
thumbnail_ref
filename
duration

system_icons[]
user_assertions[]
user_flags[]
user_labels[]

readiness
missing_requirements_count
publication_status

Não enviar para a tela objetos gigantes internos de Job/Artifact.

FACETS

Fornecer counts rápidos, por exemplo:

Todos: 2.140
Prontos: 814
Marcados como prontos: 920
Com legenda: 1.622
Editados: 1.430
Com título: 1.981
Agendados: 223
Publicados: 956
Erro: 17

Esses números alimentarão filtros visuais.

FRONTEND NÃO CONHECE SQLITE

Proibido:

UI -> SQL

Obrigatório:

UI
-> Application Service
-> Repository/Services
-> SQLite

Nenhuma regra de readiness na camada visual.

Nenhuma regra de SYSTEM_BADGE na camada visual.

Nenhuma regra de seleção em massa duplicada no frontend.

TESTES

Testar contratos dos ViewModels e application services.

Garantir que o frontend futuro possa ser substituído sem alterar core.

Não criar GUI.

FASE 13 — FRONT BONITO E INTUITIVO
PROMPT 64 — Frontend, instalador e validação final
Somente agora implementar frontend comercial definitivo.

ANTES DE CODIFICAR

Crie:
UI_ARCHITECTURE.md
DESIGN_SYSTEM.md
USER_FLOWS.md

A interface precisa ser extremamente simples para usuário comum.

TELA INICIAL

Mostrar ferramentas independentes como ações claras:

Criar cortes
Editar vídeos
Preparar vídeos em lote
Aplicar template
Gerar legendas
Limpar metadata
Agendar conteúdo
Automatizar pasta

Também destacar:

CRIAR AUTOMATICAMENTE

Esse modo usa CreativePlanner.

EDITOR

Editor focado em conteúdo curto.

Não tentar virar Premiere/DaVinci.

Estrutura:

preview
timeline simples
tools
layers quando aplicável
properties

Ferramentas:

Cortar
Vídeo
Áudio
Legenda
Template
Texto IA
Metadata
Exportar

TEMPLATE

Mostrar:

3 templates oficiais

Meus templates
→ Importar

Importação:
imagem
→ Layout Mapper
→ salvar.

Não oferecer “criar arte do zero”.

BATCH

Tela deve mostrar:

200 vídeos

35 concluídos
1 processando
162 aguardando
2 atenção

Botões:

PAUSAR
CONTINUAR
PARAR APÓS ATUAL

Permitir ações individuais.

SMART CLIP

Interface:

cole URL ou escolha arquivo

[CRIAR CORTES]

Nada de:
Analytics
API
Whisper
Ollama
configuração técnica.

Mostrar resultados em cards com previews.

DASHBOARD

Mostrar somente informações úteis:

tarefas de hoje
agendados
processando
prontos
erros que exigem atenção
contas
armazenamento
próximas publicações

CALENDÁRIO

Exibir posts por:
data
plataforma
conta
status.

BIBLIOTECA / CATÁLOGO DE VÍDEOS

Implementar Biblioteca visual consumindo exclusivamente MediaCatalog / Catalog Application API existente.

O catálogo precisa ser compacto e permitir trabalhar com grande volume.

Não escrever textos grandes como:

"Este vídeo possui legenda"
"Este vídeo foi editado"
"Descrição IA pronta"

em cada card.

Usar pequenos ícones/badges visuais consistentes.

Exemplo conceitual:

[thumbnail]
video_001.mp4
00:32
✂  CC  🧹  T  D  #  ✓

Os emojis acima são somente exemplos conceituais.

Usar ícones reais do Design System.

Cada ícone representa uma capability/status.

Ao passar mouse ou focar por teclado, mostrar tooltip acessível:

Editado
Legenda pronta
Metadata limpa
Título IA pronto
Descrição IA pronta
Hashtags prontas
Pronto para publicar

Disponibilizar legenda/ajuda explicando os símbolos.

Não depender exclusivamente de cor.

ORIGEM DO STATUS

Distinguir visualmente, sem poluir a tela:

SYSTEM_BADGE = verificado/derivado pelo sistema.
USER_ASSERTION = informado manualmente pelo usuário.
USER_FLAG = organização/decisão humana.

Uma marcação manual não pode parecer evidência automática.

IMPORTAÇÃO / MARCAÇÃO RÁPIDA

Ao adicionar vídeos, permitir declarar rapidamente o estado inicial dos arquivos.

Exemplos de opções conceituais:

Já editados
Já possuem legenda
Metadata já tratada
Já estão em 9:16
Já possuem título
Já possuem descrição
Já possuem hashtags
Marcados pelo usuário como prontos

Permitir aplicar:

A TODOS OS ARQUIVOS DA IMPORTAÇÃO
SOMENTE AOS SELECIONADOS
INDIVIDUALMENTE

Oferecer presets rápidos, por exemplo:

Original
Já editado
Pronto para IA
Marcado como pronto
Personalizado

Esses presets criam USER_ASSERTIONS/USER_FLAGS, nunca SYSTEM_BADGES falsos.

Depois da importação, permitir corrigir as marcações individualmente ou em lote.

EDIÇÃO EM MASSA DE MARCAÇÕES

Com múltiplos itens selecionados, oferecer ação:

EDITAR MARCAÇÕES

Cada opção deve ter três estados:

NÃO ALTERAR
MARCAR
DESMARCAR

Isso evita apagar marcações existentes sem intenção.

Exemplo:

87 vídeos selecionados

Legenda externa: MARCAR
Editado: NÃO ALTERAR
Favorito: DESMARCAR

FILTROS VISUAIS

Criar barra/filtros compactos para:

Todos
Prontos
Marcados como prontos
Legenda
Editados
Metadata
Título
Descrição
Hashtags
Agendados
Publicados
Erros
Revisados
Aprovados
Favoritos
Labels personalizadas

Permitir múltiplos filtros simultâneos.

Exemplo:

[Prontos] [Legenda] [Não publicados]

Também permitir:

busca por nome
canal
plataforma
label
status
idioma quando relevante

SELEÇÃO

Cada card/linha permite seleção.

Suportar:

selecionar item
selecionar página
selecionar todos filtrados
desselecionar item
selecionar novamente
limpar seleção
Shift + seleção quando fizer sentido
Ctrl/Cmd + seleção quando fizer sentido

Exibir claramente:

127 resultados
125 selecionados

Quando "Selecionar todos" puder significar mais que a página atual, mostrar explicitamente:

Selecionar 50 desta página
OU
Selecionar todos os 2.140 resultados filtrados

Nunca selecionar milhares silenciosamente.

MODO PUBLICAÇÃO / AGENDAMENTO

Na ação PUBLICAR / AGENDAR, abrir etapa de revisão antes da execução.

Permitir filtro rápido:

[✓] Mostrar somente vídeos prontos

Esse filtro deve usar ReadinessProfile do destino selecionado.

Também permitir, separadamente, filtrar:

USER_MARKED_READY
APPROVED
REVIEWED

sem confundir esses estados com READY verificado.

Mostrar previews compactos dos selecionados com:

thumbnail
título
descrição resumida
hashtags
canal
idioma
horário quando houver
badges relevantes
warnings

Permitir:

Selecionar todos
Desselecionar todos
Desmarcar vídeos individualmente
Voltar e alterar filtros
Abrir preview maior

Nada deve ser publicado simplesmente por abrir essa tela.

PREVIEW ANTES DA PUBLICAÇÃO

Antes de criar Publications/Jobs finais, usuário precisa revisar o conjunto.

Exemplo:

Canal: IdeaTime Everywhere
Encontrados: 87 prontos
Selecionados: 82

[PUBLICAR/AGENDAR 82]

Mostrar alertas relevantes:

5 sem descrição
2 já agendados
1 já publicado

Nunca ocultar duplicação conhecida.

Se o usuário desmarcar um item no preview, ele não pode entrar no lote final.

PERFORMANCE

Biblioteca deve funcionar com milhares de vídeos.

Usar:

paginação/virtualização
thumbnail lazy loading
queries paginadas
debounce de busca
cache seguro de thumbnail quando apropriado

Nunca carregar milhares de thumbnails simultaneamente.

ACESSIBILIDADE

Ícone sozinho precisa possuir:

tooltip
accessible name
estado textual disponível

Cor não pode ser a única forma de distinguir status.

DESIGN SYSTEM

Criar visual profissional com:

tipografia consistente
spacing
grid
ícones
dark/light
tooltips
skeletons
empty states
progress
feedback visual
modais consistentes

Animações discretas.

Não exagerar.

ONBOARDING

Primeira execução:

licença
→ análise automática do PC
→ preparação das dependências
→ configuração automática da IA
→ primeira conta
→ preferências básicas
→ pronto.

Usuário não deve precisar abrir terminal.

TRAY

Implementar comportamento em background e tray.

Notificações úteis.

INSTALLER

Criar instalador profissional.

Nada de:
.py
.bat
terminal
configuração manual obscura.

Preparar processo para assinatura digital de:

exe
installer
updater.

Nunca alterar exclusões do antivírus.

STAGING

Criar ambiente separado para:

backend de teste
licenças de teste
releases DEV/BETA
contas sociais controladas.

QA FINAL

Testar no mínimo:

500 Jobs
várias contas
3 plataformas principais
PC fraco
PC médio
PC forte

Simular:

crash
queda de energia
reinício Windows
internet caindo
disco cheio
Ollama parado
FFmpeg falhando
SQLite lock
SQLite recovery
sessão expirada
CAPTCHA
Content ID
rate limit
upload incerto
update interrompido

Testar pause/resume em várias etapas.

Testar:

versão antiga → nova
rollback
migration quebrada
package corrompido
backup restore

BETA

Executar beta fechado antes de Stable.

Acompanhar métricas técnicas consentidas:

job success rate
crash-free sessions
errors per version
time to first successful output
recovery rate
update success
support tickets

Nunca coletar conteúdo pessoal.

Gerar:

RELEASE_READINESS.md

Classificar:

BLOCKER
CRITICAL
MAJOR
MINOR

Não considerar produto pronto enquanto houver risco conhecido de:

perda de dados
duplicação de postagem
falsa confirmação
corrupção
recovery incorreto.
