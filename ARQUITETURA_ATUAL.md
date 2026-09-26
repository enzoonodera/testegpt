# ARQUITETURA ATUAL

> Auditoria estática do pacote `PAINEL_OFICIAL_YOUTUBE_TIKTOK(2).zip`.
> Data da auditoria: 2026-09-16.
> Documento iniciado pela auditoria estática e atualizado conforme mudanças incrementais aprovadas.
> Atualização de storage: 2026-09-16 — abstração central de caminhos e separação entre código instalado e dados mutáveis.

## 1. Resumo executivo

O projeto atual é um protótipo CLI para Windows, centrado em contas separadas de YouTube e TikTok. O ponto de entrada é `PAINEL_OFICIAL.bat`, que inicia `_sistema/painel_oficial.py`. O painel cria/seleciona contas e chama ferramentas especializadas em novos processos Python por meio da variável de ambiente `ACCOUNT_DIR`.

Existe uma camada de **modelos de domínio independentes** em `_sistema/domain/` e, a partir desta etapa, uma camada SQLite exclusivamente local em `_sistema/storage/`. O SQLite possui schema versionado, migrations numeradas, foreign keys, índices, WAL e CRUD testado, porém **ainda não está ligado aos engines legados**. Não existe hoje uma camada de aplicação única, Job Engine, Resource Manager, backend online, editor de vídeo, Smart Clip, importação por URL, updater ou frontend gráfico comercial. A fonte operacional dos engines continua sendo os JSONs/arquivos atuais até uma migration de adoção ser explicitamente autorizada.

A separação por scripts já fornece uma modularidade inicial:

- `_sistema/limpar_metadados_oficial.py`: renderização/transformação de vídeo em lote via FFmpeg;
- `_sistema/gerar_textos.py`: transcrição local + visão local + geração de textos;
- `_sistema/agendar_youtube.py`: upload/agendamento YouTube via navegador;
- `_sistema/agendar_tiktok.py`: upload/agendamento TikTok via navegador;
- `_sistema/login_conta.py`: criação/reuso do perfil Chrome por conta;
- `_sistema/painel_oficial.py`: menu, contas, dependências e orquestração simples;
- `_sistema/time_utils.py`: contrato central UTC/IANA, conversões e representação canônica de horários.

## 2. Árvore do pacote auditado

```text
PAINEL_OFICIAL.bat
LEIA_ME_PRIMEIRO.txt
requirements.txt
_sistema/
  app_paths.py
  time_utils.py
  domain/
    __init__.py
    models.py
    job_state_machine.py
  storage/
    __init__.py
    database.py
    migrations/
      __init__.py
      m001_initial.py
  painel_oficial.py
  login_conta.py
  limpar_metadados_oficial.py
  gerar_textos.py
  agendar_youtube.py
  agendar_tiktok.py
  defender_setup.ps1
```

O código instalado permanece na pasta do programa. Dados mutáveis não são mais criados ao lado do código. No Windows, o layout padrão é:

```text
%LOCALAPPDATA%\PainelOficial\
  database\
    painel.db              # SQLite local versionado (quando inicializado)
  accounts\
    youtube\<CONTA>\
    tiktok\<CONTA>\
  projects\
  cache\
  logs\
  temp\
  templates\
  backups\
    operational\          # snapshots SQLite + manifest
    removed_accounts\
  models\
  support\
```

`_sistema/app_paths.py` é a fonte central desses caminhos. `PAINEL_DATA_ROOT` existe como override controlado para testes/diagnóstico; o default de produção continua fora da pasta instalada.

## 2.1 Storage V1 e compatibilidade com o layout antigo

Na inicialização, `initialize_app_storage()` cria o layout mutável e executa uma migração idempotente do formato portátil anterior:

- `<pasta_instalada>/contas/` → `%LOCALAPPDATA%\PainelOficial\accounts/`;
- `<pasta_instalada>/_removidas/` → `%LOCALAPPDATA%\PainelOficial\backups\removed_accounts/`.

A migração trabalha **por unidade de conta**, nunca arquivo a arquivo. Se o diretório final não existe, no mesmo volume é preferido rename da conta inteira; entre volumes, a árvore inteira é copiada para `accounts/.migration_staging/<plataforma>/<conta>`, validada integralmente e só então publicada no caminho final. A origem só é retirada depois do commit validado. Se o destino já existe, a árvore completa é comparada: árvores integralmente idênticas são tratadas como duplicata; qualquer diferença preserva origem e destino sem mover nenhum arquivo daquela conta. O mesmo princípio é aplicado às unidades de `_removidas/`. Staging de crash nunca fica em `accounts/<plataforma>/<conta>` e é descartado antes de nova tentativa. Conflitos são registrados em `support/storage_migration_v1.json`.

Depois da migração, updates do código podem substituir a pasta instalada sem depender dela para preservar contas, perfis Chrome, vídeos, estados ou demais dados mutáveis.

## 2.2 SQLite local V1

`_sistema/storage/database.py` implementa a camada SQLite local independente dos engines. O arquivo padrão é `%LOCALAPPDATA%\PainelOficial\database\painel.db`, resolvido por `app_paths.py`. O banco não contém integração de rede.

O schema base é aplicado pela migration congelada `001_initial_local_schema`; a migration `002_audit_append_only` adiciona a proteção persistente append-only de `audit_events`. Ambas são registradas na tabela `migrations` com versão, nome, checksum SHA-256 e instante de aplicação. `PRAGMA user_version` acompanha a versão aplicada (schema atual v2). `initialize()` negocia WAL, adquire `BEGIN IMMEDIATE` e **só depois do write lock** relê/valida o histórico antes de decidir quais migrations ainda precisam rodar; isso torna a inicialização idempotente entre múltiplas instâncias/processos. Divergência de checksum/nome, histórico não contínuo ou `user_version` incompatível falham explicitamente. Banco com schema mais novo que o binário continua sendo recusado.

As tabelas atuais são `sources`, `videos`, `projects`, `accounts`, `jobs`, `publications`, `schedules`, `templates`, `artifacts`, `errors`, `settings`, `audit_events` e `migrations`. Relações entre entidades usam foreign keys reais; `schedules.publication_id` possui índice UNIQUE parcial quando não nulo, impondo `Publication 1 -> 0..1 Schedule` sem impedir múltiplos Schedules ainda não associados (`publication_id IS NULL`). Campos naturalmente estruturados são serializados em JSON local dentro da linha. Writers usam transações explícitas, `busy_timeout` e WAL.

### 2.2.1 Backup operacional local

`_sistema/storage/backup.py` cria snapshots consistentes com a API de backup do SQLite, portanto não copia apenas `painel.db` ignorando um WAL ativo. Cada backup válido vive em `backups/operational/<timestamp>_<motivo>_<id>/` e contém somente `painel.db` + `manifest.json`. O manifest registra `backup_id`, instante UTC, arquivo/path lógico do banco, motivo (`PERIODIC`, `PRE_MIGRATION`, `PRE_UPDATE`, `PRE_RESTORE` ou `MANUAL`), método `SQLITE_BACKUP_API`, status de validação, schema, migrations, tamanho, SHA-256 e `integrity_check`. Metadados de gatilho usam allowlist por motivo; mappings arbitrários não são persistidos.

A criação ocorre em `.staging` e só publica o diretório final após snapshot, checksum, histórico de migrations e `PRAGMA integrity_check` válidos. **Nenhuma criação de backup executa retenção automaticamente**: `BackupRetentionPolicy`/`prune_backups()` existem apenas como helper explícito para política futura autorizada, e os defaults preservam tudo. `list_backups()` retorna somente backups integralmente válidos por padrão; discovery leve sem validação exige `validate=False`. `maybe_create_periodic()` considera apenas PERIODIC que passe manifest, hash, snapshot, migrations e `integrity_check`, portanto um backup recente corrompido não bloqueia nova proteção válida. Upgrades de schema existentes (`v1 -> v2+`) criam `PRE_MIGRATION` antes do DDL; o updater futuro deve chamar `backup_before_update()` antes de update importante.

Restore usa lock de SO entre processos e cria obrigatoriamente um `PRE_RESTORE` persistente do estado operacional atual antes de qualquer swap. Depois valida staging e persiste `database/restore_state.json` com fases `PREPARED`, `OLD_MOVED`, `NEW_PUBLISHED`, `VERIFIED`/`ROLLED_BACK`. O journal mantém separadamente `expected_source_schema_version` (schema do backup escolhido) e `expected_previous_schema_version` (schema do banco/safety anterior), além dos hashes correspondentes; restore/recovery nunca pressupõe que os dois schemas são iguais. Cada mudança crítica de fase é escrita atomicamente antes do passo seguinte. `LocalDatabase.initialize()` chama recovery **antes** de qualquer conexão que poderia criar um banco vazio. Crash após `OLD_MOVED` faz rollback determinístico para o estado anterior; crash após `NEW_PUBLISHED` valida o candidato publicado contra o schema do source e conclui ou faz rollback. Backup antigo ainda suportado pode ser restaurado e migrations posteriores continuam responsabilidade de `LocalDatabase.initialize()`. Artefato de restore sem journal suficiente gera `RestoreRecoveryRequiredError` e é preservado, nunca apagado por heurística. Vídeos originais, renders, perfis Chrome, caches, temp e modelos continuam explicitamente excluídos.

**Limite desta etapa:** nenhum engine lê ou escreve essa base ainda. Os JSONs legados continuam sendo a única fonte operacional dos fluxos existentes. A adoção do SQLite pelos engines exigirá uma migration separada, testada e explicitamente autorizada para evitar duas fontes de verdade.

### 2.2.2 Audit log operacional append-only

`_sistema/storage/audit.py` fornece `OperationalAuditLog`, uma camada local e independente de UI/connectors para registrar o ciclo operacional de `Job`. Criação e mudança de estado podem ser persistidas junto com os respectivos eventos na **mesma transaction SQLite**. Toda transição grava um evento canônico `JOB_STATE_CHANGED` com `from_state`/`to_state`; operações relevantes podem acrescentar eventos semânticos como `JOB_PAUSED`, `JOB_RESUMED`, `JOB_CANCELLED`, `JOB_RETRY`, `JOB_PROCESSING_COMPLETED`, `JOB_UPLOAD_STARTED`, `JOB_CONFIRMED`, `JOB_RECONCILIATION`, `JOB_ERROR` e `JOB_RECOVERY`.

A ordem de append é reconstruída pelo `rowid` SQLite exposto como `sequence`; `reconstruct_job_history()` valida a cadeia usando a `JobStateMachine` e devolve estado inicial, estado atual, caminho de estados e eventos. O histórico persiste entre reinicializações porque é lido diretamente de `audit_events`. A migration `002_audit_append_only` instala triggers persistentes que abortam `UPDATE`, `DELETE` e qualquer `INSERT` que tente reutilizar um `id` existente, inclusive `INSERT OR REPLACE`. Nas conexões criadas por `LocalDatabase`, o SQLite authorizer continua negando `UPDATE` e `DELETE` como defesa adicional; append com UUID novo continua permitido. Não há integração automática com engines legados nesta etapa e isto **não constitui Job Engine**.

## 2.3 Modelos de domínio V1

`_sistema/domain/models.py` define uma camada de domínio sem dependência de UI, Playwright, YouTube, TikTok ou SQLite. Os modelos atuais são:

- `SourceAsset`: origem/importação do arquivo, separada do vídeo lógico;
- `Video`: identidade lógica do vídeo;
- `Project`: estado não destrutivo de edição ligado ao vídeo;
- `Account`: conta lógica, com plataforma representada apenas como dado;
- `Job`: job de processamento local;
- `Publication`: ciclo/tentativa de publicação, separado do `Job`;
- `Schedule`: agendamento associado à publicação por `publication_id`, distinguindo `LOCAL_PENDING`, `REMOTE_SCHEDULED` e `UNKNOWN` e preservando `scheduled_local`, `timezone_iana`, `scheduled_utc` e `time_origin`;
- `Template`: definição de template independente do editor/plataforma;
- `Artifact`: saída derivada de processamento, distinta do `SourceAsset`;
- `ErrorRecord`: erro estruturado referenciável por UUID.

Todas as entidades recebem UUID persistível na criação. Relações usam UUIDs (`source_asset_id`, `video_id`, `project_id`, `account_id`, `job_id`, `artifact_id`, `publication_id`) em vez de referências de objeto. A relação `Publication`/`Schedule` possui uma única direção canônica: `Schedule.publication_id`; `Publication` não possui `schedule_id`, evitando duas fontes de verdade. `to_dict()` / `from_dict()` preservam a identidade e campos desconhecidos para facilitar evolução de schema, exceto campos explicitamente descontinuados por modelo. `Publication` tombstoneia o legado `schedule_id`, que é ignorado na leitura e não reaparece na serialização. `from_dict()` não inventa identidade ausente e rejeita payload de outro `model_type`.

Os JSONs atuais **não foram migrados para esses modelos** e os engines não foram alterados. A camada SQLite agora possui tabelas correspondentes aos modelos, mas permanece desacoplada do runtime legado; portanto não existe escrita dupla nem nova fonte de verdade operacional nesta etapa.

Relação principal planejada:

```text
SourceAsset -> Video -> Project
                  |-> Job -> Artifact
                  |-> Publication -> Schedule
                        |              (FK: Schedule.publication_id)
                        |-> Account
```

Um mesmo `Video` pode ser referenciado por vários `Job` e várias `Publication`, sem misturar a identidade do trabalho operacional com o registro do efeito remoto.

### 2.2.1 State Machine central de Job

`_sistema/domain/job_state_machine.py` define o vocabulário canônico e a tabela explícita de transições de `Job`, independente de UI, engines, plataformas e persistência. Os estados atuais são:

`PENDING`, `PROCESSING`, `READY`, `PAUSED`, `INTERRUPTED`, `RECOVERING`, `SCHEDULED`, `PUBLISHING`, `PUBLISHED`, `RETRY`, `FAILED`, `BLOCKED`, `UNKNOWN`, `AUTH_REQUIRED`, `USER_ACTION_REQUIRED` e `CANCELLED`.

`Job.transition_to()` valida a transição antes de alterar o estado. Alteração direta de `Job.status` para outro valor é bloqueada para evitar contornar a máquina. `PUBLISHED` e `CANCELLED` são terminais. `PUBLISHING` não pode transitar diretamente para `RETRY`: quando uma ação remota pode ter produzido efeito e o resultado é incerto, o caminho é `PUBLISHING -> UNKNOWN -> RECOVERING`; somente após reconciliação que torne seguro tentar novamente pode ocorrer `RECOVERING -> RETRY`. Para uma falha conhecida/comprovada sem efeito remoto, o domínio permite `PUBLISHING -> FAILED -> RETRY`. `UNKNOWN` possui somente a aresta `UNKNOWN -> RECOVERING`; portanto um resultado remoto incerto não pode ir diretamente para `RETRY` ou `PUBLISHING`.

Esta State Machine é apenas contrato de domínio nesta etapa. Ela ainda não é um Job Engine persistente, não executa workers, não persiste checkpoints e não altera os engines legados.

## 3. Entrada e fluxo principal

### 3.1 `PAINEL_OFICIAL.bat`

Responsabilidade:

1. muda o diretório atual para a pasta do pacote;
2. procura `py -3` ou `python`;
3. inicia `_sistema/painel_oficial.py`;
4. se o código de retorno for diferente de zero, mostra o código e pausa.

Referências: `PAINEL_OFICIAL.bat:1-31`.

### 3.2 `_sistema/painel_oficial.py`

É o orquestrador CLI. O menu atual expõe:

1. limpar metadata;
2. gerar/editar textos;
3. postar/agendar;
4. adicionar conta;
5. remover conta;
6. listar contas;
7. instalar/verificar dependências.

Referências: `_sistema/painel_oficial.py:436-462`.

A orquestração não é um job scheduler. `run_engine()` cria um subprocesso síncrono e aguarda seu término. Enquanto um engine roda, o menu não executa outro trabalho. Referências: `_sistema/painel_oficial.py:153-158`.

## 4. Modelo de múltiplas contas

Cada conta possui diretório próprio e configuração própria.

Estrutura criada por `ensure_account()` dentro de `%LOCALAPPDATA%\PainelOficial\accounts`:

```text
accounts/<plataforma>/<nome>/
  config_canal.json
  videos/
  dados/
  logs/
  perfil_youtube/      # YouTube
  perfil_tiktok/       # TikTok
  bloqueados/          # YouTube
```

A raiz global é resolvida por `_sistema/app_paths.py`; `ensure_account()` preserva o layout interno legado da conta para compatibilidade.

O nome físico da conta é sanitizado por `slugify()`. Duplicidade é checada de forma case-insensitive pelo nome configurado e pelo slug. Referências: `_sistema/painel_oficial.py:50-53`, `240-258`.

### 4.1 Isolamento de execução

O painel define `ACCOUNT_DIR=<pasta da conta>` ao iniciar cada engine. `app_paths.account_dir_from_env()` e `app_paths.account_paths()` centralizam a resolução de caminhos dos engines. Para preservar compatibilidade com execução/importação standalone sem permitir mistura silenciosa, o fallback exige um namespace explícito e usa `accounts/_standalone/<engine>`; YouTube, TikTok, geração de textos e login usam namespaces distintos. O fluxo normal do painel continua usando `ACCOUNT_DIR` e não depende desses fallbacks.

### 4.2 Perfis Chrome

`login_conta.py` abre o Google Chrome normal com `--user-data-dir=<perfil da conta>` e aguarda o navegador fechar. Isso preserva a sessão local da conta. Referências: `_sistema/login_conta.py:33-68`.

O agendamento usa esses mesmos diretórios como `user_data_dir` do Playwright persistent context. Referências: `_sistema/agendar_youtube.py:1546-1555`, `_sistema/agendar_tiktok.py:737-750`.

Os perfis contêm dados de autenticação do navegador e devem ser classificados como **segredo local de alta sensibilidade**.

## 5. Importação de conta antiga

`import_legacy()` pode copiar:

- `videos/`;
- perfil completo do navegador;
- estados antigos de YouTube/TikTok;
- histórico de textos;
- estado de limpeza;
- parte das configurações de horários/lotes.

Referências: `_sistema/painel_oficial.py:181-238`.

Para TikTok, aceita também o nome legado `perfil_chrome`. Referência: `_sistema/painel_oficial.py:186-194`.

Não há migração formal por versão de schema. A lógica é baseada em existência/nome de arquivos e conversões pontuais.

## 6. Processamento de vídeo / FFmpeg

### 6.1 Entrada

O painel pede uma pasta de originais, encontra recursivamente extensões de vídeo e chama `limpar_metadados_oficial.py` apontando a saída para `conta/videos`. Referências: `_sistema/painel_oficial.py:292-310`.

### 6.2 FFprobe

Antes do encode, `obter_info_video()` chama `ffprobe` para detectar:

- largura;
- altura;
- presença de áudio;
- FPS;
- duração.

Referências: `_sistema/limpar_metadados_oficial.py:88-155`.

### 6.3 Pipeline atual de renderização

Apesar do nome “limpar metadata”, o engine atual faz muito mais do que limpeza:

- zoom progressivo 100% ↔ 104%;
- contraste/saturação/brilho/gamma aleatórios discretos;
- color balance discreto;
- vinheta;
- noise/grain;
- unsharp;
- velocidade aleatória entre 0,99x e 1,01x;
- áudio com `atempo=<velocidade>` e volume +1%;
- H.264/libx264, preset medium, CRF aleatório 21–25;
- AAC 128 kbps;
- `yuv420p`;
- remoção de metadata/chapter originais;
- adição de novos metadados de data/GPS/fabricante/modelo/software.

Referências: `_sistema/limpar_metadados_oficial.py:163-230`, `345-459`.

A saída é escrita diretamente como MP4 numerado em `videos/` e o original não é sobrescrito. Portanto o original físico é preservado, mas não existe ainda um modelo de projeto/EDL/estado de edição não destrutivo: a decisão é incorporada no arquivo renderizado.

### 6.4 Paralelismo atual

O painel escolhe de 1 a 3 vídeos simultâneos com base em CPU. O engine aceita 1–10 jobs e divide aproximadamente 75% das CPUs lógicas entre os FFmpegs. Referências: `_sistema/painel_oficial.py:304-307`, `_sistema/limpar_metadados_oficial.py:645-653`.

O paralelismo é local a esse script. Não existe Resource Manager compartilhado com Whisper, Ollama, navegador ou outros futuros jobs.

### 6.5 Resume do tratamento

Antes de codificar, o engine calcula fingerprints e reserva números em `limpeza_estado.json`. Depois de cada futuro concluído, grava `done` ou `failed`. Falhas passam por um retry serial. Referências: `_sistema/limpar_metadados_oficial.py:548-737`.

Esse é o mecanismo de checkpoint mais robusto do pacote atual.

## 7. Fingerprints

Há implementações repetidas do mesmo conceito em quatro arquivos.

Algoritmo atual:

1. SHA-256;
2. inclui o tamanho do arquivo;
3. inclui o primeiro 1 MiB;
4. se o arquivo tiver mais de 1 MiB, inclui o último 1 MiB.

Referências:

- `_sistema/painel_oficial.py:160-166`;
- `_sistema/gerar_textos.py:38-44`;
- `_sistema/agendar_youtube.py:59-69`;
- `_sistema/agendar_tiktok.py:53-63`;
- `_sistema/limpar_metadados_oficial.py:249-260`.

É um fingerprint parcial, não o SHA-256 integral do conteúdo. Dois arquivos de mesmo tamanho, mesmo início e mesmo fim, mas meio diferente, colidem logicamente para o sistema.

## 8. Whisper

`gerar_textos.py` usa `faster-whisper` localmente. O padrão configurado é modelo `small`, CPU, `int8`. Referências: `_sistema/painel_oficial.py:75`, `_sistema/gerar_textos.py:258-283`.

Transcrição:

- `task='transcribe'`;
- detecção automática de idioma;
- VAD ligado;
- `beam_size=1`;
- sem condicionamento pelo texto anterior.

Referência: `_sistema/gerar_textos.py:161-167`.

Cada transcrição é cacheada em `dados/transcricoes/<fingerprint>.json`. Referências: `_sistema/gerar_textos.py:169-177`.

O cache atual não grava versão do Whisper, tamanho do modelo, parâmetros, idioma forçado ou versão de pipeline. Alterar esses parâmetros não invalida automaticamente a transcrição existente.

## 9. Ollama

O padrão é Ollama local em `http://localhost:11434`.

Modelos padrão:

- texto: `llama3.2`;
- visão: `moondream`.

Referências: `_sistema/painel_oficial.py:75`, `_sistema/gerar_textos.py:258-259`.

### 9.1 Inicialização

Se a API local não responder, o script tenta encontrar `ollama` e iniciar `ollama serve`. Referências: `_sistema/gerar_textos.py:100-119`.

### 9.2 Visão

Quando o Whisper não detecta fala, o sistema extrai até três frames em 25%, 50% e 75% e pede uma descrição visual curta ao modelo. Há fallback `/api/chat` → `/api/generate`. Referências: `_sistema/gerar_textos.py:67-87`, `131-159`, `291-304`.

### 9.3 Texto

A IA gera JSON estruturado diferente por plataforma:

- YouTube: título + descrição + 4 hashtags;
- TikTok: caption + 4 hashtags.

Referências: `_sistema/gerar_textos.py:201-236`.

A assinatura `pipeline_versao` considera plataforma, locale, país e modelos de texto/visão, mas não o modelo/configuração Whisper. Referências: `_sistema/gerar_textos.py:194-199`.

## 10. Geração e edição de textos

O histórico canônico é `dados/textos_postagem.json`, indexado por fingerprint. Após cada vídeo, o JSON e o CSV são gravados imediatamente. Referências: `_sistema/gerar_textos.py:238-250`, `285-324`.

Falha de um vídeo não encerra o lote de geração de textos; o item é marcado `failed` e o loop segue. Referências: `_sistema/gerar_textos.py:310-321`.

O painel permite editar manualmente título/descrição/caption/hashtags de um item `done`, marcando `manual_edit=True`. Referências: `_sistema/painel_oficial.py:313-340`.

## 11. YouTube

### 11.1 Tecnologia

Não usa YouTube Data API. Usa Playwright sobre um perfil persistente do Chrome e automação da interface do YouTube Studio. Referências: `_sistema/agendar_youtube.py:1430-1555`.

### 11.2 Validação de Short

`ffprobe` valida orientação e duração. Horizontal é rejeitado e, por padrão, duração >180 s é rejeitada. Há aviso especial acima de 60 s. Referências: `_sistema/agendar_youtube.py:99-189`.

### 11.3 Fluxo de upload

Resumo:

1. abre/garante modal de upload;
2. encontra `input[type=file][name=Filedata]`;
3. anexa o MP4;
4. preenche título e descrição;
5. marca “não é para crianças”;
6. avança pelas etapas;
7. checa direitos autorais/Content ID por texto visível;
8. escolhe Programar;
9. define data/hora;
10. clica botão final;
11. tenta confirmar status PROGRAMADO na UI;
12. só depois retorna ao loop principal, que grava o registro local.

Referências principais: `_sistema/agendar_youtube.py:302-424`, `608-679`, `1288-1375`, `1604-1664`.

### 11.4 Content ID / direitos autorais

Não há acesso a API de Content ID. O sistema lê textos da etapa “Verificações” e procura frases específicas em português/inglês. Se detectar problema, tenta descartar o upload, move o arquivo local para `bloqueados/direitos_autorais/` e continua a fila. Referências: `_sistema/agendar_youtube.py:1136-1285`, `1321-1335`, `1606-1629`.

Se a checagem for inconclusiva até o timeout, a função retorna `False` e segue com o agendamento. Referência: `_sistema/agendar_youtube.py:1223`.

### 11.5 Confirmação

`confirm_success()` exige toast/texto explícito ou uma linha na tela de Conteúdo contendo o título esperado e status Programado/Scheduled/Agendado. Rascunho é falha. Referências: `_sistema/agendar_youtube.py:994-1079`.

É mais forte que o TikTok atual, mas ainda existe uma janela entre confirmação remota e persistência local do registro em `estado_youtube.json`.

### 11.6 Reparo de rascunhos

Na abertura, o script pode remover do estado local registros cujo título aparece em uma linha marcada Rascunho/Draft. Referências: `_sistema/agendar_youtube.py:1376-1427`, `1560-1575`.

Esse reparo é baseado em título, não em ID remoto do vídeo.

## 12. TikTok

### 12.1 Tecnologia

Não usa API oficial de publicação. Usa Playwright e perfil persistente. Referências: `_sistema/agendar_tiktok.py:663-773`.

### 12.2 Fluxo

Resumo:

1. abre TikTok Studio Upload;
2. anexa arquivo em `input[type=file]`;
3. aguarda editor;
4. preenche caption;
5. marca Programar;
6. escolhe hora/minuto/data;
7. aguarda botão final ficar habilitado;
8. clica;
9. faz uma leitura best-effort do body;
10. retorna ao loop, que grava `estado_tiktok.json`.

Referências: `_sistema/agendar_tiktok.py:114-154`, `156-271`, `345-550`, `561-621`, `752-770`.

### 12.3 CAPTCHA / challenge

Há detecção textual de challenge/CAPTCHA. Quando detectado, a automação pausa e pede ao usuário para resolver no navegador e apertar ENTER. Não há tentativa de bypass. Referências: `_sistema/agendar_tiktok.py:107-112`, `552-559`, `570-571`, `598-599`.

Hoje isso é uma pausa bloqueante no terminal; não existe estado formal `USER_ACTION_REQUIRED` persistido.

### 12.4 Confirmação

A confirmação atual é fraca. Após o clique final, se encontrar termos como `scheduled/agendado/programado`, imprime confirmação; caso contrário, ainda imprime `OK: clique de agendamento concluído` e retorna normalmente. Referências: `_sistema/agendar_tiktok.py:611-621`.

O loop principal grava o item como `scheduled` logo depois. Referências: `_sistema/agendar_tiktok.py:762-769`.

## 13. Agendamentos e contrato temporal

`_sistema/time_utils.py` centraliza o contrato de data/hora. Timestamps operacionais novos são gravados em UTC timezone-aware. Contas de presets oficiais podem receber um default IANA determinístico; contas/mercados não mapeados precisam de `timezone_iana` explícito e nunca recebem UTC por fallback. UTC só é aceito quando configurado explicitamente. Os cálculos de calendário usam `zoneinfo`, nunca o timezone atual do Windows. O pacote inclui `tzdata` para fornecer a base IANA também no Windows.

Cada registro novo de agendamento preserva quatro dimensões:

- `scheduled_local`: data/hora local pretendida, sem offset;
- `timezone_iana`: timezone IANA que dá significado ao horário local;
- `scheduled_utc`: instante UTC correspondente;
- `time_origin`: `MANUAL` ou `RECOMMENDED`.

O alias legado `datetime` continua sendo escrito temporariamente para compatibilidade, mas não é a fonte canônica de tempo. Históricos antigos com `datetime` naive continuam legíveis usando o timezone explícito da conta. Registros novos preservam o timezone próprio, portanto uma mudança posterior do timezone da conta ou do Windows não reinterpreta horários já calculados. Horários inexistentes por início de DST são rejeitados/pulados em vez de serem deslocados silenciosamente; horários ambíguos têm o instante UTC persistido para eliminar ambiguidade após a criação.

### YouTube

- horários por dia da semana;
- horizonte padrão de até 5 anos;
- primeiro uso começa amanhã, por padrão no timezone da conta;
- continua após o maior instante UTC já salvo;
- máximo por execução opcional (`0` = sem limite interno).

### TikTok

- horários simples, padrão `10:00`, `15:00`, `20:00`;
- janela padrão de 9 dias no timezone da conta;
- continua após o maior instante UTC já salvo;
- `build_slots()` agora devolve no máximo a quantidade solicitada.

**Risco separado e ainda aberto:** o Connector TikTok pode aplicar incorretamente na interface um horário que foi calculado corretamente. Esta etapa padroniza cálculo/persistência, mas não considera resolvido o preenchimento/seleção do horário no DOM do TikTok. No Prompt 59, o Connector deverá considerar que o timezone efetivamente usado pela UI/plataforma pode divergir do `timezone_iana` canônico do `Schedule`: converter o instante para o timezone da UI quando necessário, preencher data/hora, reler o valor aplicado, convertê-lo novamente para instante UTC e compará-lo ao `scheduled_utc` antes de permitir o clique final.

`MANUAL` é usado hoje para os horários configurados explicitamente na conta. `RECOMMENDED` já existe no domínio para uma recomendação futura, mas nenhuma IA/recomendador de horário foi implementado nesta etapa.

## 14. Estados de fila e recuperação

Hoje existem três mecanismos independentes:

1. `limpeza_estado.json`: item a item, com `pending/done/failed`;
2. `textos_postagem.json`: item a item, com `done/failed`;
3. `estado_youtube.json` / `estado_tiktok.json`: lista de itens considerados agendados.

Existe um **modelo** `Job` comum e modelos separados de `Publication`/`Schedule`, mas os fluxos atuais ainda não os utilizam nem os persistem. Em runtime continuam inexistentes tentativa (`attempt`) comum, lease, lock, heartbeat, `pause`, `stop_after_current`, `cancel`, estado `UNKNOWN`, `USER_ACTION_REQUIRED` persistido ou reconciliação genérica.

## 15. JSONs e atomicidade

A maior parte dos escritores usa o padrão:

1. escreve `<arquivo>.tmp`;
2. `replace()` para o nome final.

Isso reduz risco de truncamento em um único processo. Referências: `_sistema/painel_oficial.py:44-48`, `_sistema/gerar_textos.py:29-33`, `_sistema/agendar_youtube.py:49-53`, `_sistema/agendar_tiktok.py:48-51`, `_sistema/limpar_metadados_oficial.py:242-246`.

Não há file lock nem coordenação entre duas instâncias simultâneas. Além disso, `load_json()` em vários módulos engole qualquer erro e retorna um default, o que pode transformar corrupção de estado em “estado vazio”.

## 16. Caches e temporários

### Persistentes

- `dados/transcricoes/<fingerprint>.json`: cache Whisper.
- `dados/textos_postagem.json`: saída textual/cache do pipeline de IA.

### Temporários

- `dados/frames_ia_temp/<fingerprint>_vision/`: frames JPG usados por visão.
- removidos em `finally` quando `work` foi associado.

Referências: `_sistema/gerar_textos.py:11-12`, `67-87`, `322-323`.

Há também `extract_one_frame()` com pasta temporária própria, mas a função não é chamada no código atual. Referências: `_sistema/gerar_textos.py:53-65`.

## 17. Logs e diagnósticos

### YouTube/TikTok

Em falhas de UI, salvam:

- screenshot PNG full-page;
- HTML completo da página.

Referências: `_sistema/agendar_youtube.py:84-97`, `_sistema/agendar_tiktok.py:92-105`.

Esses artefatos são locais hoje, mas podem conter informações pessoais da conta, títulos, estado da página e possivelmente identificadores presentes no DOM. Devem ser tratados como dados sensíveis e nunca enviados brutos a um backend futuro.

### FFmpeg

- `logs/FFMPEG_ERROS.log`: caminho do arquivo, destino, modo, exit code e últimas linhas do FFmpeg;
- `logs/falharam.txt`: lista de falhas.

Referências: `_sistema/limpar_metadados_oficial.py:493-510`, `722-735`.

### IA

- `logs/textos_erros.log`: timestamp, nome do vídeo e exceção.

Referência: `_sistema/gerar_textos.py:315-319`.

## 18. Metadata

O comportamento atual não implementa `KEEP / CLEAN / PROFILE` como três modos.

A ferramenta “limpar metadata” sempre renderiza novamente e, após `-map_metadata -1`, adiciona metadata criada pelo sistema, incluindo:

- `creation_time` aleatório entre 2024-01-01 e 2026-09-12;
- GPS aleatório em torno de São Paulo;
- fabricante Apple;
- modelo aleatório iPhone 13/14/15;
- software iOS.

Referências: `_sistema/limpar_metadados_oficial.py:345-379`, `420-457`.

Esse comportamento deve ser considerado **baseline legado a substituir somente em etapa autorizada**, pois conflita com as regras permanentes atuais do projeto.

## 19. Dependências

### Python (`requirements.txt`)

Sem pin de versão:

- `playwright`;
- `faster-whisper`;
- `tzdata` (base IANA para `zoneinfo`, especialmente no Windows).

### Externas

- Python 3.10+;
- FFmpeg;
- FFprobe;
- Google Chrome;
- Ollama;
- modelos `llama3.2` e `moondream`;
- `winget` opcional para instalação;
- Tkinter para o seletor de pasta quando disponível.

O instalador interno executa `pip install --upgrade` sem lockfile, instala Playwright Chromium e tenta instalar Chrome/FFmpeg/Ollama via winget. Referências: `_sistema/painel_oficial.py:390-433`.

## 20. `defender_setup.ps1`

O arquivo existe e, se executado, adiciona a pasta inteira do projeto às exclusões do Windows Defender, solicitando elevação UAC se necessário. Referências: `_sistema/defender_setup.ps1:1-39`.

Não foi encontrada nenhuma chamada a esse script em outros arquivos do pacote atual. Portanto ele é **código órfão/latente**, não comportamento ativo no fluxo observado. Ainda assim, sua existência conflita diretamente com a regra permanente de não criar exclusões automáticas de antivírus.

## 21. Código duplicado / acoplamento

Duplicações relevantes:

- `load_json`: 5 implementações;
- `save_json`: 4 implementações;
- `fingerprint`: 4 implementações equivalentes + 1 variante nomeada;
- `natural_key`: 3 implementações;
- `save_debug`: 2 implementações;
- `find_chrome`: 2 implementações;
- `build_slots`: implementações separadas por plataforma.

Funções aparentando estar mortas/não utilizadas:

- `numero_par_acima()`;
- `destino_para()`;
- `extract_one_frame()`;
- `fill_input_force()`;
- classe `DailyLimitAfterScheduleError`.

Não devem ser removidas nesta etapa; são apenas registradas para futura análise.

Acoplamentos principais:

- o nome/fingerprint do MP4 tratado conecta limpeza → textos → publicação;
- publicação depende de `textos_postagem.json` pronto para o próximo item;
- YouTube/TikTok conhecem diretamente estrutura de diretórios e schemas JSON;
- UI do painel conhece nomes dos scripts e parâmetros CLI;
- lógica de conta, persistência e infraestrutura está misturada no painel CLI.

## 22. Componentes pedidos nas regras permanentes mas ausentes hoje

Não foram encontrados no pacote auditado:

- editor de vídeo geral;
- cortar vídeo;
- Smart Clip;
- geração/queima de legenda;
- templates oficiais/importados;
- melhoria de áudio como ferramenta independente;
- reenquadramento 9:16 como ferramenta independente;
- importação por URL;
- automatização de pasta;
- SQLite (camada local V1 já criada; engines legados ainda não a usam diretamente);
- Job Engine central;
- Resource Manager central;
- projeto/EDL não destrutivo;
- pause/resume/stop_after_current/cancel persistentes;
- estado `UNKNOWN` operacional/persistido nos engines (o domínio já o formaliza na State Machine de `Job` e em `Schedule`);
- USER_ACTION_REQUIRED formal;
- backend de licenças/dispositivos/planos/feature flags/releases;
- update DEV/BETA/STABLE, assinatura/hash/rollback/migrations/rollout/kill switch;
- telemetria;
- frontend gráfico comercial;
- sistema de templates;
- Google Analytics;
- APIs pagas de IA/publicação.

## 23. Fluxo ponta a ponta atual

```text
PAINEL_OFICIAL.bat
  -> painel_oficial.py
      -> adicionar conta
          -> cria diretórios/config
          -> opcionalmente importa legado
          -> login_conta.py -> Chrome persistente

      -> limpar metadata
          -> limpar_metadados_oficial.py
              -> fingerprint originais
              -> reserva 001/002/...
              -> ffprobe
              -> ffmpeg render
              -> limpeza_estado.json
              -> conta/videos/*.mp4

      -> gerar textos
          -> gerar_textos.py
              -> fingerprint MP4
              -> Whisper -> transcricoes/*.json
              -> se sem fala: FFmpeg frames -> Ollama visão
              -> Ollama texto
              -> textos_postagem.json/.csv

      -> postar/agendar
          -> agendar_youtube.py OU agendar_tiktok.py
              -> perfil Chrome da conta
              -> Playwright
              -> UI da plataforma
              -> estado_<plataforma>.json
```

## 24. Conclusão da arquitetura atual

O projeto já possui quatro propriedades úteis para evolução incremental: separação por conta, ferramentas executáveis separadamente, escrita JSON via arquivo temporário e alguns checkpoints por item. Porém a arquitetura ainda é de scripts de automação com estado distribuído, não de software desktop transacional. A próxima evolução deve preservar os comportamentos comprovadamente úteis, mas introduzir abstrações de job/tentativa/reconciliação antes de ampliar volume e publicação automática.


## 47. Migration explícita dos estados JSON legados para SQLite

A camada `_sistema/storage/legacy_migration.py` implementa a adoção controlada dos JSONs operacionais conhecidos sem ligar os engines ao banco. A migration é executável explicitamente por `python -m _sistema.storage.legacy_migration`; ela **não** roda automaticamente no painel nesta etapa.

Fluxo:

```text
adquirir lock interprocesso da migration
→ descobrir allowlist de JSONs operacionais
→ snapshot byte-a-byte em backups/legacy_json_migration/<digest>/
→ SHA-256 por arquivo + manifest do snapshot
→ parse JSON estrito a partir do backup
→ BEGIN IMMEDIATE no SQLite
→ preservar payload integral em settings/legacy_json/v1/...
→ materializar entidades de domínio com IDs UUID determinísticos
→ validar contagens dentro da mesma transaction
→ gravar marcador/checksums da migration
→ COMMIT
→ relatório em support/legacy_json_to_sqlite_v1_report.json
```

A allowlist atual migra `config_canal.json`, `dados/limpeza_estado.json`, `dados/textos_postagem.json`, `dados/estado_youtube.json` e `dados/estado_tiktok.json`. Não existe glob genérico dentro de perfis Chrome. `dados/transcricoes/*.json` continua sendo cache/conteúdo local e não é incorporado a esta migration de **estado operacional**. Vídeos originais/renders não são copiados para o backup da migration.

A migração não altera nem apaga os JSONs originais. A operação inteira é serializada por lock de kernel entre processos; após adquirir o lock, o marker SQLite é relido antes da decisão `MIGRATED/NOOP`, portanto duas instâncias com a mesma entrada resultam em uma migração e um `NOOP`, nunca duas transactions concorrentes. Se não existir nenhum JSON allowlistado, retorna `NO_DATA` sem marker nem audit event, permitindo migração futura quando dados aparecerem. A mesma árvore de entrada produz um `source_manifest_sha256` determinístico; segunda execução valida o snapshot/estado importado e retorna `NOOP`, sem criar entidades, audit events ou backups duplicados. Se os JSONs mudarem depois de uma migration concluída, a ferramenta falha explicitamente em vez de tentar mesclar duas fontes de verdade silenciosamente.

`SourceAsset` representa a origem e nunca é degradado por nomes/paths derivados vindos de estado de publicação. Em `limpeza_estado`, um `Artifact` só é materializado quando `status=done` **e** o arquivo de saída existe, é regular e possui tamanho > 0. `done` sem saída válida é importado conservadoramente como Job `PENDING`, sem Artifact, com warning/ErrorRecord explícito; `pending` e `failed` não ganham Artifact apenas por possuírem um nome reservado em `output`.

Estados legados de publicação são importados como `Publication.status=UNKNOWN` e `Schedule.delivery_state=UNKNOWN`, porque os JSONs antigos não carregam evidência suficiente para promover o efeito remoto a confirmado. Se um registro de horário não tiver timezone canônico suficiente, a Publication é preservada como UNKNOWN e o Schedule não é inventado; o payload bruto permanece no SQLite e o relatório registra warning.
