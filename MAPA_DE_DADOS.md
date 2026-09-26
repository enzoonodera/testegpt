# MAPA DE DADOS

> Estado atual do projeto em 2026-09-16. Atualizado após a introdução do storage V1 e da migração automática do layout portátil anterior.

## 1. Princípios observados hoje

- Persistência local por arquivos JSON/TXT/CSV e diretórios.
- Separação por conta/plataforma.
- Perfis completos de Chrome ficam dentro da pasta da conta.
- A maioria dos JSONs é salva com `.tmp` + `replace()`.
- Existe uma camada SQLite local versionada em `_sistema/storage/`, ainda não adotada pelos engines legados.
- Não existe backend online no projeto atual.
- Não foi encontrado envio de cookies, vídeos, transcrições ou logs a servidor próprio.

## 2. Mapa de diretórios de runtime

No Windows, dados mutáveis ficam por padrão em `%LOCALAPPDATA%\PainelOficial`. O código instalado não é fonte de verdade para dados do usuário.

```text
%LOCALAPPDATA%\PainelOficial\
  database\
    painel.db                # SQLite local versionado; criado pela camada storage quando inicializada
  accounts\
    youtube\
      <CONTA>\
        config_canal.json
        videos\
        dados\
          limpeza_estado.json
          textos_postagem.json
          textos_postagem.csv
          transcricoes\
          frames_ia_temp\
          estado_youtube.json
          titulos_youtube.txt
          descricoes_youtube.txt
          direitos_autorais_bloqueados.txt
        logs\
        perfil_youtube\
        bloqueados\
        youtube_nao_shorts.txt
    tiktok\
      <CONTA>\
        config_canal.json
        videos\
        dados\
          limpeza_estado.json
          textos_postagem.json
          textos_postagem.csv
          transcricoes\
          frames_ia_temp\
          estado_tiktok.json
        logs\
        perfil_tiktok\
  projects\                 # reservado para projetos não destrutivos
  cache\
  logs\                      # logs globais futuros
  temp\
  templates\
  backups\
    operational\            # snapshot SQLite + manifest; sem mídia original
    removed_accounts\
      <plataforma>\<conta>_YYYYMMDD_HHMMSS\
  models\
  support\
    storage_migration_v1.json
```

A variável `PAINEL_DATA_ROOT` pode substituir a raiz somente de forma explícita, principalmente para testes/diagnóstico. O layout interno das contas (`dados`, `videos`, perfis) foi preservado para manter compatibilidade com os JSONs e engines existentes.

### 2.1 Migração do layout portátil anterior

Fontes legadas reconhecidas ao lado do código instalado:

```text
contas/       -> accounts/
_removidas/   -> backups/removed_accounts/
```

A migração é idempotente e transacional por unidade `plataforma/conta`. Não existe merge parcial: destino ausente recebe a conta inteira por rename ou staging validado; destino existente só permite remoção da origem quando as árvores completas são equivalentes. Qualquer diferença preserva as duas árvores e registra conflito em `support/storage_migration_v1.json`. Staging fica fora do caminho de contas válidas, em `.migration_staging`, e restos de crash são descartados antes da próxima tentativa.

## 3. `config_canal.json`

### Produtor

`_sistema/painel_oficial.py`.

### Consumidores

Todos os engines da conta.

### Campos comuns observados

```json
{
  "plataforma": "youtube|tiktok",
  "nome_conta": "...",
  "nome_canal": "...",
  "pais_alvo": "...",
  "idioma_metadata": "pt-BR",
  "timezone_iana": "America/Sao_Paulo",
  "horarios": ["12:00", "17:00", "20:00"],
  "posts_por_dia": 3,
  "comecar_amanha_na_primeira_execucao": true,
  "pausa_entre_posts_segundos": 8,
  "timeout_upload_segundos": 600,
  "navegador_visivel": true,
  "ia_local": {
    "whisper_model_size": "small",
    "ollama_url": "http://localhost:11434",
    "ollama_modelo_texto": "llama3.2",
    "ollama_modelo_visao": "moondream"
  }
}
```

YouTube acrescenta `horarios_por_dia`, `dias_janela`, `max_uploads_por_execucao`, flags de audiência/Short/Content ID e reinício do Chrome. TikTok acrescenta `upload_url`, `dias_janela` e flags legadas de comentários/dueto/stitch.

Referências: `_sistema/painel_oficial.py:68-91`.

### Sensibilidade

Baixa a média. Pode revelar nome da conta, mercado, preferências e endpoint local, mas não contém credencial diretamente no schema atual.

### Riscos

- sem validação de schema;
- sem migration por versão;
- campos podem ficar obsoletos;
- contas legadas podem chegar sem `timezone_iana`; apenas mercados oficiais mapeados podem receber um default IANA determinístico. Mercado desconhecido/Personalizado sem timezone explícito permanece sem alteração e operações de agendamento falham claramente até a configuração ser fornecida. UTC nunca é inventado como fallback.

## 4. `dados/limpeza_estado.json`

### Produtor/consumidor

`_sistema/limpar_metadados_oficial.py`.

### Schema observado

Raiz:

```json
{
  "version": 1,
  "items": {
    "<fingerprint>": { },
    "...": { }
  },
  "updated_at": "YYYY-MM-DDTHH:MM:SS+00:00"
}
```

Item:

```json
{
  "fingerprint": "...",
  "source_name": "arquivo.mp4",
  "source_path": "C:/.../arquivo.mp4",
  "source_size": 123456,
  "output": "001.mp4",
  "status": "pending|done|failed",
  "reserved_at": "...",
  "last_seen_at": "...",
  "last_attempt_at": "...",
  "completed_at": "...",
  "last_error": "..."
}
```

Referências: `_sistema/limpar_metadados_oficial.py:263-279`, `591-630`, `676-713`.

### Função

- deduplicar originais por fingerprint;
- reservar numeração antes do encode;
- retomar falhas/crashes sem renumerar;
- evitar reprocessar item `done` cujo arquivo de saída ainda existe e tem tamanho >0.

### Sensibilidade

Média. Guarda caminhos absolutos de arquivos do usuário.

## 5. `dados/textos_postagem.json`

### Produtores

- `gerar_textos.py`;
- edição manual em `painel_oficial.py`;
- importação/conversão de legado em `painel_oficial.py`.

### Consumidores

- `agendar_youtube.py`;
- `agendar_tiktok.py`;
- editor textual do painel.

### Chave

Fingerprint do vídeo.

### Schema YouTube observado

```json
{
  "<fingerprint>": {
    "arquivo": "001.mp4",
    "plataforma": "youtube",
    "pais_alvo": "Brasil",
    "idioma": "pt-BR",
    "status": "done|failed",
    "pipeline_versao": "TXT_<hash>",
    "atualizado_em": "...",
    "titulo": "...",
    "descricao": "...",
    "hashtags": ["#Shorts", "#..."],
    "manual_edit": true,
    "ultimo_erro": "..."
  }
}
```

### Schema TikTok observado

Mesmo envelope, com `caption` no lugar de `titulo`/`descricao`.

Referências: `_sistema/gerar_textos.py:305-313`, `_sistema/painel_oficial.py:321-340`.

### Sensibilidade

Alta do ponto de vista de privacidade de conteúdo: contém conteúdo derivado de vídeo/transcrição, ainda que não contenha a transcrição completa.

## 6. `dados/textos_postagem.csv`

Espelho humano/exportável do JSON, com campos:

```text
arquivo,titulo,descricao,caption,hashtags,pais_alvo,idioma,status,atualizado_em,fingerprint
```

Referências: `_sistema/gerar_textos.py:238-250`, `_sistema/painel_oficial.py:168-179`.

Risco: duplicação de fonte de verdade. O JSON é efetivamente canônico; o CSV é refeito a partir dele.

## 7. `dados/transcricoes/<fingerprint>.json`

### Produtor

`gerar_textos.py`.

### Schema

```json
{
  "text": "transcrição completa",
  "language": "en|pt|...",
  "saved_at": "YYYY-MM-DDTHH:MM:SS"
}
```

Referências: `_sistema/gerar_textos.py:169-177`.

### Sensibilidade

**Alta**. Pode conter fala pessoal, nomes, endereços, dados privados ou qualquer conteúdo do vídeo. Pelas regras permanentes, nunca deve ser enviado ao backend do produto.

### Invalidação atual

Somente por fingerprint do vídeo. Não contém versão/modelo/parâmetros do Whisper.

## 8. `dados/frames_ia_temp/`

### Conteúdo

JPGs extraídos do vídeo para Ollama visão.

### Ciclo de vida

Criados por vídeo quando não há fala e normalmente removidos no `finally` do loop. Referências: `_sistema/gerar_textos.py:67-87`, `287-323`.

### Sensibilidade

**Alta**, pois são frames reais do vídeo.

### Risco residual

Crash abrupto, kill do processo ou queda de energia antes do `finally` pode deixar a pasta temporária no disco.

## 9. `dados/estado_youtube.json`

### Inicialização

O painel cria atualmente:

```json
{"version": 8, "scheduled": []}
```

Referência: `_sistema/painel_oficial.py:102-105`.

O agendador usa fallback default `version: 7`, evidenciando ausência de migration/schema central. Referência: `_sistema/agendar_youtube.py:1436`.

### Registro agendado

```json
{
  "file": "001.mp4",
  "fingerprint": "...",
  "title": "...",
  "description": "...",
  "scheduled_local": "2026-09-17T12:00",
  "timezone_iana": "America/Sao_Paulo",
  "scheduled_utc": "2026-09-17T15:00+00:00",
  "time_origin": "MANUAL",
  "datetime": "2026-09-17T12:00",
  "duration": 42.1,
  "registered_at": "2026-09-16T21:00:00+00:00"
}
```

`datetime` permanece somente como alias legado de compatibilidade. A representação canônica é `scheduled_local + timezone_iana + scheduled_utc + time_origin`. `registered_at` é UTC. Históricos antigos sem offset continuam legíveis no timezone explícito da conta.

### Funções

- determina itens já concluídos por fingerprint;
- define o último horário ocupado;
- evita reutilizar números pelo tratador;
- base do reparo de rascunhos.

### Lacunas

Não armazena:

- ID remoto do vídeo;
- URL remota;
- idempotency key;
- status `SUBMITTING/UNKNOWN/CONFIRMED`;
- tentativas;
- timestamps antes/depois do clique;
- evidência/reconciliação;
- erro por tentativa.

## 10. `dados/estado_tiktok.json`

### Inicialização

```json
{"version": 2, "scheduled": []}
```

Referências: `_sistema/painel_oficial.py:106-107`, `_sistema/agendar_tiktok.py:673`.

### Registro

```json
{
  "file": "001.mp4",
  "fingerprint": "...",
  "caption": "...",
  "scheduled_local": "2026-09-17T10:00",
  "timezone_iana": "America/Sao_Paulo",
  "scheduled_utc": "2026-09-17T13:00+00:00",
  "time_origin": "MANUAL",
  "datetime": "2026-09-17T10:00",
  "registered_at": "2026-09-16T21:00:00+00:00"
}
```

A semântica temporal é a mesma do YouTube. O cálculo agora limita `build_slots()` à quantidade solicitada. Mesmas lacunas transacionais do YouTube permanecem, com agravante de confirmação remota fraca. Existe ainda um risco separado no Connector TikTok: um horário corretamente calculado pode ser aplicado incorretamente na interface; este prompt não altera essa lógica de DOM.

## 11. `dados/titulos_youtube.txt`

Fallback legado de títulos rotativos. Criado com três textos padrão quando não existe. Referência: `_sistema/painel_oficial.py:104`.

O agendador ainda exige que o arquivo exista e seja não vazio, mesmo quando todos os itens possuam texto IA. Referências: `_sistema/agendar_youtube.py:71-75`, `1434`.

## 12. `dados/descricoes_youtube.txt`

Fallback legado separado pelo marcador `---SHORT---`. Referências: `_sistema/painel_oficial.py:105`, `_sistema/agendar_youtube.py:77-82`.

## 13. `dados/direitos_autorais_bloqueados.txt`

Log append-only simples para vídeos movidos após detecção de possível Content ID/direitos autorais. Referências: `_sistema/agendar_youtube.py:1619-1626`.

Não há estrutura de status, evidence snapshot ou reversão automática.

## 14. `bloqueados/direitos_autorais/`

Destino físico do vídeo local quando `CopyrightClaimError` ocorre. Referências: `_sistema/agendar_youtube.py:1606-1618`.

A movimentação remove o arquivo da fila normal. Se o descarte remoto falhar, pode restar um rascunho na plataforma e o MP4 ficar localmente bloqueado.

## 15. `youtube_nao_shorts.txt`

Criado quando há vídeos rejeitados pela validação de Short. Guarda `nome | motivo`. Referências: `_sistema/agendar_youtube.py:183-187`.

## 16. `logs/*.png` e `logs/*.html`

### Produtores

YouTube e TikTok.

### Conteúdo

Screenshot full-page + HTML completo em falhas ou diagnósticos.

### Sensibilidade

**Muito alta**. Pode conter informações da conta, conteúdo, estado da UI, identificadores e dados que não devem sair do dispositivo sem sanitização específica.

Referências: `_sistema/agendar_youtube.py:84-97`, `_sistema/agendar_tiktok.py:92-105`.

## 17. `logs/FFMPEG_ERROS.log`

Contém caminhos absolutos, nome de origem/destino, modo, código de saída e até 200 linhas recentes do FFmpeg. Referências: `_sistema/limpar_metadados_oficial.py:493-510`.

Sensibilidade: média/alta por revelar caminhos locais e nomes de conteúdo.

## 18. `logs/falharam.txt`

Lista de vídeos que falharam após retry. Referências: `_sistema/limpar_metadados_oficial.py:722-735`.

## 19. `logs/textos_erros.log`

Timestamp, arquivo e mensagem de exceção do pipeline de textos. Referências: `_sistema/gerar_textos.py:315-319`.

## 20. Perfis `perfil_youtube/` e `perfil_tiktok/`

Conteúdo gerenciado pelo Chrome/Chromium, potencialmente incluindo:

- cookies;
- storage local;
- tokens/sessões;
- preferências;
- cache;
- histórico interno do perfil.

O projeto não lê cookies diretamente; ele delega a sessão ao navegador. Porém `import_legacy()` copia o diretório completo. Referências: `_sistema/login_conta.py:45-60`, `_sistema/painel_oficial.py:186-194`.

Classificação: **SEGREDO LOCAL CRÍTICO**. Nunca enviar ao backend, telemetria ou suporte bruto.

## 21. `backups/removed_accounts/`

Ao remover uma conta pelo painel, ela não é apagada; é movida integralmente para `%LOCALAPPDATA%\PainelOficial\backups\removed_accounts/<plataforma>/<nome_timestamp>`. O diretório `_removidas/` ao lado do código é reconhecido apenas como fonte legada de migração.

Isso preserva recovery, mas também preserva perfis/cookies e demais dados sensíveis indefinidamente. Ainda não existe política de retenção/expurgo.

## 22. Metadata embutida nos MP4s tratados

O render atual grava valores gerados pelo sistema:

- criação/data;
- GPS;
- Apple/iPhone/iOS;
- creation_time em streams.

Referências: `_sistema/limpar_metadados_oficial.py:345-379`, `436-457`.

Isso não é um arquivo de dados separado; faz parte do artefato exportado.

## 23. Modelos de domínio V1 e dados ainda ausentes

A camada `_sistema/domain/models.py` agora formaliza, sem alterar a persistência operacional existente:

- `SourceAsset`;
- `Video`;
- `Project`;
- `Account`;
- `Job`;
- `Publication`;
- `Schedule`;
- `Template`;
- `Artifact`;
- `ErrorRecord`.

Cada entidade possui UUID persistível e `schema_version`. Relações são guardadas por UUID, não por referência de objeto. Para agendamento, a relação canônica é exclusivamente `Schedule.publication_id`; `Publication` não armazena `schedule_id`. `Schedule.delivery_state` aceita `LOCAL_PENDING`, `REMOTE_SCHEDULED` e `UNKNOWN`, onde `UNKNOWN` representa efeito remoto possível ainda sem confirmação/reconciliação. O contrato temporal de `Schedule` preserva `scheduled_local`, `timezone_iana`, `scheduled_utc` e `time_origin`; se houver `scheduled_local` ou `scheduled_utc`, `timezone_iana` é obrigatório e nunca é inventado como UTC. Um `Schedule` ainda sem horário pode existir sem timezone. `time_origin` aceita `MANUAL` e `RECOMMENDED`. O modo `RECOMMENDED` apenas prepara o domínio para recomendação futura e não implementa IA de horário. O modelo também preserva campos desconhecidos em round-trip para reduzir perda de dados durante evolução de schema, exceto campos explicitamente tombstoned/descontinuados pelo próprio modelo; em `Publication`, o legado `schedule_id` é descartado e não é reemitido.

`Job.status` agora possui State Machine central em `_sistema/domain/job_state_machine.py`, com os estados `PENDING`, `PROCESSING`, `READY`, `PAUSED`, `INTERRUPTED`, `RECOVERING`, `SCHEDULED`, `PUBLISHING`, `PUBLISHED`, `RETRY`, `FAILED`, `BLOCKED`, `UNKNOWN`, `AUTH_REQUIRED`, `USER_ACTION_REQUIRED` e `CANCELLED`. Transições são validadas por `Job.transition_to()`. `PUBLISHED`/`CANCELLED` são terminais; `PUBLISHING -> RETRY` é proibido; resultado remoto incerto deve seguir `PUBLISHING -> UNKNOWN -> RECOVERING`, e somente depois de reconciliação segura pode chegar a `RETRY`. Falha conhecida sem efeito remoto pode seguir `PUBLISHING -> FAILED -> RETRY`. `UNKNOWN` só pode transitar diretamente para `RECOVERING`.

**Importante:** esses modelos ainda não são a fonte de verdade dos engines. A migration explícita JSON -> SQLite já existe, preserva snapshots/checksums e pode materializar o estado legado no banco, mas os engines ainda continuam lendo/escrevendo os JSONs nesta etapa; não existe escrita dupla automática. A State Machine também não substitui o futuro Job Engine persistente.

Ainda faltam estruturas persistentes/operacionais para:

- `job_attempts`;
- `job_dependencies`;
- `resource_leases`;
- locks/heartbeats;
- persistência/execução operacional de `pause/resume/stop_after_current/cancel` no futuro Job Engine;
- reconciliação persistente de publicação com `UNKNOWN/USER_ACTION_REQUIRED`;
- `metadata_profiles`;
- feature flags locais cacheadas;
- migrations futuras além da `001` inicial;
- integração do audit trail ao runtime (a tabela/API local já existe);
- versão/invalidação de caches;
- storage confiável de IDs remotos.

## 24. Fonte de verdade atual por domínio

| Domínio | Fonte de verdade atual | Observação |
|---|---|---|
| Configuração da conta | `config_canal.json` | sem schema/migration |
| Limpeza/render | `limpeza_estado.json` + MP4 em `videos/` | checkpoint por fingerprint |
| Transcrição | `transcricoes/<fp>.json` | cache sem versão do modelo |
| Texto IA | `textos_postagem.json` | CSV é derivado |
| YouTube concluído | `estado_youtube.json` | sem ID remoto/UNKNOWN |
| TikTok concluído | `estado_tiktok.json` | confirmação remota insuficiente |
| Login | perfil completo Chrome | segredo local |
| Diagnóstico UI | PNG/HTML em `logs/` | sensível |
| Conta removida | `backups/removed_accounts/` | retenção indefinida |

## 25. Observação crítica sobre `load_json`

Várias implementações fazem `except Exception: return default`. Isso significa que arquivo ausente e arquivo corrompido podem virar o mesmo resultado lógico. Em estados de publicação, `default` normalmente é uma lista `scheduled` vazia. Portanto corrupção pode ser interpretada como “nada foi publicado” e criar risco de duplicação.

Referências: `_sistema/agendar_youtube.py:41-47`, `_sistema/agendar_tiktok.py:40-46`, `_sistema/painel_oficial.py:40-42`, `_sistema/gerar_textos.py:24-27`, `_sistema/limpar_metadados_oficial.py:233-239`.


## 45. SQLite local V1 — schema disponível, adoção operacional pendente

O arquivo padrão é `database/painel.db`, fora da pasta instalada. O schema atual v2 possui as tabelas exigidas: `videos`, `sources`, `projects`, `accounts`, `jobs`, `publications`, `schedules`, `templates`, `artifacts`, `errors`, `settings`, `audit_events` e `migrations`.

A migration congelada `001_initial_local_schema` cria o schema base; `002_audit_append_only` adiciona triggers append-only para `audit_events`. Ambas são numeradas e registradas com checksum. A inicialização concorrente adquire `BEGIN IMMEDIATE` antes de reler/decidir o prefixo pendente; migrations aplicadas precisam formar prefixo contínuo das migrations conhecidas, com nome/checksum e `PRAGMA user_version` coerentes. O banco habilita foreign keys por conexão, WAL para o arquivo local, `busy_timeout`, índices para FKs/estados/horários e transações explícitas com rollback. `schedules.publication_id` é UNIQUE quando não nulo, garantindo no banco a cardinalidade `Publication 1 -> 0..1 Schedule`. `settings` armazena valores JSON locais.

### Audit trail de Job

`audit_events` é o histórico operacional append-only. Para eventos de Job, `entity_type='Job'` e `entity_id=<job_uuid>` formam a chave lógica de consulta. Eventos `JOB_CREATED` guardam `initial_state`; cada `JOB_STATE_CHANGED` guarda `from_state` e `to_state`, permitindo reconstruir o caminho completo do estado sem inferir a partir do registro atual em `jobs`. Eventos semânticos registram pause/resume/cancel/retry, processamento concluído, upload iniciado, confirmação, reconciliação, erro e recovery. A coluna implícita `rowid` é exposta como `sequence` pela API para preservar a ordem de append. `UPDATE`/`DELETE` e reutilização de `id` em `INSERT OR REPLACE` são bloqueados por triggers persistentes da migration 002; o authorizer de `LocalDatabase` permanece como defesa adicional. O audit trail não possui API de edição.

Os modelos de domínio podem fazer round-trip pelo SQLite preservando UUIDs, `extra` e os campos estruturados. Esta camada, porém, **não está conectada aos schedulers, painel, limpeza ou geração de textos**. A migration explícita dos JSONs legados para o SQLite já está disponível, mas sua execução não altera os engines nem remove os JSONs; eles continuam sendo a fonte operacional até a adoção futura do banco. Isso evita escrita dupla automática nesta etapa.


## 46. Backup operacional SQLite

Cada backup operacional fica em `backups/operational/<timestamp>_<motivo>_<id>/` e contém somente:

- `painel.db`: snapshot consistente produzido pela SQLite Backup API;
- `manifest.json`: `backup_id`, `created_at` UTC, motivo, `backup_method=SQLITE_BACKUP_API`, `validation_status=VALIDATED`, filename/path lógico do banco, schema, migrations, tamanho, SHA-256, `integrity_check`, escopo e metadata **allowlistada** do gatilho.

Motivos reconhecidos: `PERIODIC`, `PRE_MIGRATION`, `PRE_UPDATE`, `PRE_RESTORE` e `MANUAL`. `PRE_RESTORE` permanece após restore bem-sucedido e contém o estado imediatamente anterior ao swap. Não entram no backup operacional: `accounts/**/videos`, perfis de navegador, renders/mídia, `cache/`, `temp/` ou `models/`. O banco pode guardar **referências** a arquivos grandes, mas não duplica seus bytes. Staging incompleto fica em `backups/operational/.staging/` e não é listado/restaurável como backup válido.

Criação periódica/pre-migration/pre-update/pre-restore/manual **não remove backups anteriores**. `prune_backups()` é helper explicitamente invocado; o default preserva tudo. `list_backups()` valida integralmente por padrão; `validate=False` é discovery diagnóstico explícito. A decisão de `maybe_create_periodic()` usa somente backups PERIODIC que ainda sejam válidos, então diretório corrompido/manifest inválido/snapshot ausente permanece fisicamente preservado, mas não conta como proteção recente. Restore usa `database/restore_state.json` fora do próprio banco e arquivos únicos `painel.db.restore_tmp.<restore_id>` / `painel.db.restore_previous.<restore_id>`. O journal referencia backup fonte, safety backup, hashes esperados, `expected_source_schema_version`, `expected_previous_schema_version` e fase. `database/restore.lock` é apenas o arquivo hospedeiro de um lock de kernel; arquivo stale não significa lock ativo. Artefatos de restore sem journal válido são preservados e bloqueiam inicialização destrutiva.


## 47. Snapshot/mapeamento da migration JSON -> SQLite

A migration V1 de dados legados usa somente uma allowlist de estado operacional:

| JSON legado | Destino SQLite | Observação |
|---|---|---|
| `config_canal.json` | `accounts` + `settings/legacy_json/v1/...` | UUID determinístico por plataforma/conta; payload integral preservado |
| `dados/limpeza_estado.json` | `sources`, `videos`, `jobs`, `artifacts`, `errors` + raw setting | SourceAsset preserva a origem. `done` só vira `READY` + Artifact se a saída existir e tiver >0 bytes; `done` inconsistente vira `PENDING` + erro/warning; `pending/failed` não criam Artifact fantasma |
| `dados/textos_postagem.json` | raw setting versionado/checksum | preserva integralmente campos de texto sem inventar modelo ainda inexistente |
| `dados/estado_youtube.json` | `publications`, `schedules` + raw setting | importado conservadoramente como `UNKNOWN` até reconciliação futura |
| `dados/estado_tiktok.json` | `publications`, `schedules` + raw setting | mesma semântica conservadora; não resolve o bug de DOM do Connector |

Cada arquivo recebe envelope local com `relative_path`, `sha256`, `size_bytes`, `modified_at_utc` e payload. O snapshot antigo fica em `backups/legacy_json_migration/<source_manifest_sha256>/accounts/...`; o manifest não contém vídeos/perfis nem secrets adicionais. O relatório final fica em `support/legacy_json_to_sqlite_v1_report.json`.

A migration usa IDs UUIDv5 determinísticos, lock interprocesso e uma transaction única. Zero arquivos retorna `NO_DATA` sem marker/audit. Validação de contagens ocorre antes do commit. Qualquer exceção após começar a escrita faz rollback do SQLite; o snapshot de segurança permanece disponível. Reexecução com a mesma árvore é `NOOP`. Mudança da árvore depois de conclusão gera erro explícito em vez de atualização/merge silencioso.
