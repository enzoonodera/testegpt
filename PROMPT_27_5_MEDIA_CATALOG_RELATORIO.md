# PROMPT 27.5 — Media Catalog Backend — Relatório de Entrega

Data (UTC): 2026-09-23

## 1. Arquivos criados

- `_sistema/storage/migrations/m009_video_declarations.py` — nova migration numerada (versão 9), tabela append-only `video_declarations`, keyed por `video_id`, para marcações manuais do usuário (`USER_ASSERTION`/`USER_FLAG`/`USER_LABEL`).
- `_sistema/media_catalog.py` — `MediaCatalogService`/`MediaCatalogItem` e todo o contrato do Prompt (824 linhas).
- `tests/test_media_catalog.py` — suíte de testes dedicada (64 testes, 845 linhas).

## 2. Arquivos modificados

- `_sistema/storage/migrations/__init__.py` — registrada `MIGRATION_009`, `MIGRATIONS` agora tem 9 entradas, `LATEST_SCHEMA_VERSION` passa de 8 para 9.
- `tests/test_migrations_frozen.py` — hash de `m009_video_declarations.py` adicionado conscientemente a `FROZEN_MIGRATION_HASHES` (`708396f5f8ff1e29440327c04df55060c787b6aaa5d1d1ecffbdc70b7bc358a6`), com justificativa na docstring do módulo. Nenhum hash das 8 migrations anteriores foi alterado.
- `tests/test_sqlite_storage.py` — cascata 8→9: `EXPECTED_TABLES` (+`video_declarations`), `test_schema_inicial_cria_todas_as_tabelas_e_versao` (lista de `(version, name)` +`(9, "video_declarations")`, `== 8` → `== 9`), `test_upgrade_schema1_para_schema8_cria_pre_migration_e_e_idempotente` (todas as asserções de schema final `8` → `9`, tabela `video_declarations` adicionada à checagem de tabelas), `test_upgrade_schema2_para_schema9_preserva_m001_m002_byte_identicas` (renomeado de `..._schema8_...`; mesma cascata), `test_initialize_concorrente_mesmo_banco_e_idempotente_entre_instancias` (lista `[1..8]` → `[1..9]`).
- `tests/test_backup_restore.py` — cascata 8→9/9→10: `migration_versions == [1..8]` → `[1..9]`; a migration "sonda" simulada usada nos testes de backup pré-migration e de restore/crash passa de versão 9 (livre quando o real era 8) para versão 10 (livre agora que 9 é real); `_install_future_migration_v9` renomeada para `_install_future_migration_v10`; os três testes `test_*_schema8_previous_schema9_*` renomeados para `test_*_schema9_previous_schema10_*`, com todos os marcadores (`SOURCE_V8`→`SOURCE_V9`, `PREVIOUS_V9`→`PREVIOUS_V10`) e asserções numéricas (`schema_version == 8` → `== 9`, `== 9` → `== 10`, etc.) atualizados.
- `tests/test_edit_project.py` / `tests/test_video_promotion.py` — os dois testes `test_*_nao_cria_migration_nova` comparavam `LATEST_SCHEMA_VERSION` contra o literal `8` (valor real quando cada Prompt foi escrito); atualizados conscientemente para `9`, já que este Prompt adicionou legitimamente `m009`. Nenhuma outra asserção desses arquivos foi tocada.
- `empacotar_release.py` — `PROMPT_27_5_MEDIA_CATALOG_RELATORIO.md` adicionado a `ALLOWED_ROOT_FILES`.

Nenhuma migration m001–m008 foi alterada (confirmado por `tests/test_migrations_frozen.py`, que compara SHA-256 byte a byte e passa). Nenhum dos 10 módulos protegidos (`circuit_breaker.py`, `retry_policy.py`, `publication_idempotency.py`, `secrets_manager.py`, `domain/job_state_machine.py`, `source_import.py`, `source_context.py`, `media_probe.py`, `edit_project.py`, `video_promotion.py`) nem `domain/models.py` foram modificados neste Prompt.

## 3. Comportamento novo

`MediaCatalogService` (leitura/agregação, nunca escreve nas fontes de verdade operacional):

- `get_item(video_id)` → `MediaCatalogItem | None`.
- `list_items(limit, offset)` / `filter_items(CatalogFilter, limit, offset)` → paginação determinística via SQL puro (`ORDER BY created_at, id`).
- `count_by_filter(CatalogFilter | None)` → `COUNT(*)` SQL.
- `get_facets(CatalogFilter | None)` → uma única consulta `SUM(CASE WHEN ... THEN 1 ELSE 0 END)` por badge.
- `set_user_flag`/`remove_user_flag`, `add_user_label`/`remove_user_label`, `set_user_assertion`/`remove_user_assertion` — escrevem eventos `ADD`/`REMOVE` em `video_declarations`.
- `bulk_edit(video_ids, assertions=, flags=, labels=)` com `BulkFieldOp(mode=KEEP|SET|UNSET, values=...)` por campo, resultado `BulkEditResult(requested, updated, unchanged, failed)`.

`CatalogFilter(badges_all=(...), badges_none=(...))` valida cada badge contra `SYSTEM_BADGES` na construção — nome desconhecido nunca é aceito silenciosamente.

## 4. Badge a badge (os 17 do roadmap)

Deriváveis hoje (evidência real, fonte exata):

| Badge | Fonte exata |
|---|---|
| `IMPORTED` | `EXISTS Video.source_asset_id → SourceAsset.id` |
| `EDITED` | `EXISTS Project.video_id` com pelo menos 1 chave em `edit_state.categories` (via `json_each`/`json_extract`, nunca um Project vazio) |
| `SCHEDULED` | `EXISTS Publication.video_id → Publication.id = Schedule.publication_id` com `Schedule.delivery_state IN (LOCAL_PENDING, REMOTE_SCHEDULED)` |
| `PUBLISHED` | `EXISTS Job.video_id` com `Job.status = 'PUBLISHED'` (vocabulário fechado de `job_state_machine.py`; nunca `Publication.status`) |
| `ERROR` | `EXISTS Job.video_id` com `status='FAILED'`, e/ou `EXISTS ErrorRecord.entity_id = video_id` |

Estruturalmente impossíveis hoje (sem fonte de verdade — nunca inventados, nunca disparam mesmo com evidência adjacente, provado por teste dedicado `test_badges_impossiveis_nunca_aparecem_mesmo_com_evidencia_adjacente_rica` + `test_cada_badge_impossivel_individualmente_nunca_dispara` parametrizado nos 12):

- `VALIDATED` — nenhum módulo de validação de mídia persiste um resultado de validação hoje.
- `CAPTIONS` — nenhum `CaptionsEngine` existe.
- `METADATA_CLEAN` — nenhum `MetadataManager` (Gen2) existe (o `K` do roadmap — KEEP/CLEAN/PROFILE — ainda não tem um módulo Gen2 correspondente que persista o resultado da operação).
- `REFRAMED_9_16` — nenhum `AutoReframe` existe.
- `AUDIO_PROCESSED` — nenhum `AudioEngine` existe.
- `TEMPLATE_APPLIED` — nenhum módulo de Template/Render (Gen2) existe.
- `AI_TITLE` / `AI_DESCRIPTION` / `AI_HASHTAGS` — nenhum `ContentEngine` existe; `Publication.title`/`description` não-vazios são evidência de que um título/descrição *existe*, mas não de que veio de IA (ver `ContentStatus` na seção 5) — distinção deliberada, documentada no docstring de `ContentStatus`.
- `TRANSCRIBED` — nenhum módulo de transcrição persiste resultado.
- `RENDERED` — exigiria um `Artifact` de render final validado; um `Artifact` de qualquer `kind` sozinho **não** é evidência suficiente (`kind` é string livre sem vocabulário fechado confirmado hoje para "render final"); inventar essa regra seria inventar evidência, violando a seção 0.2 do próprio roadmap. Testado explicitamente com Artifacts de 6 `kind`s diferentes (`preview`, `thumbnail`, `render`, `export`, `final`, arbitrário) — nenhum dispara `RENDERED`.
- `READY` — depende de `RENDERED` pela definição do roadmap ("READY automático não pode nascer apenas de um checkbox manual"); como `RENDERED` é impossível hoje, `READY` também é, pela mesma cadeia — nenhuma regra alternativa mais fraca foi inventada.

`IMPOSSIBLE_BADGES_TODAY`/`DERIVABLE_BADGES_TODAY` são constantes computadas automaticamente a partir do dicionário único `_BADGE_SQL_EXPRESSIONS` (nunca uma lista solta que poderia divergir) — um teste (`test_derivavel_e_impossivel_particionam_todos_os_badges_sem_sobreposicao`) confirma que as duas constantes particionam exatamente os 17 badges sem sobreposição.

## 5. Campos "desconhecidos hoje" — valores bem definidos, nunca inventados

- `duration`, `resolution`, `thumbnail_ref` — sempre `None`. Confirmado por leitura da própria docstring de `media_probe.py` (linhas 79-97): `MediaProbe` é deliberadamente *stateless* — nenhum resultado de probe é persistido em nenhuma tabela/coluna. Não há hoje nenhuma fonte de verdade da qual derivar esses campos.
- `ContentStatus.hashtags_available` — sempre `False`: `Publication` não tem nenhuma coluna de hashtags no schema atual.
- `ContentStatus.transcript_available`/`captions_available` — sempre `False`: nenhum módulo de transcrição/legendas existe.
- `ContentStatus.title_available`/`description_available` — usam evidência REAL (`Publication.title`/`description` não-vazios associados ao vídeo), mas deliberadamente **não** são confundidos com `AI_TITLE`/`AI_DESCRIPTION` (que exigiriam proveniência de IA via `ContentEngine`, inexistente) — distinção documentada explicitamente no docstring de `ContentStatus`.

## 6. Decisão de índices

Nenhum índice novo foi necessário nas tabelas já existentes (`jobs`, `artifacts`, `publications`, `schedules`, `sources`, `projects`, `errors`) — confirmado por leitura completa de `m001_initial.py` **antes** de desenhar `m009`. Em particular, a especulação do próprio Prompt de que "hoje não existe índice em `schedules.publication_id`" está **incorreta**: `idx_schedules_publication_id` já existe desde `m001` e já é **`UNIQUE`** (`WHERE publication_id IS NOT NULL`) — o caminho de join `Publication.video_id → Publication.id → Schedule.publication_id` usado para derivar `SCHEDULED` já era bem indexado. Nenhum índice redundante foi adicionado (princípio "não fazer overengineering sem benefício concreto" do CLAUDE.md).

`m009_video_declarations.py` adiciona apenas os três índices que a tabela NOVA exige: `idx_video_declarations_video_id` (`video_id`), `idx_video_declarations_lookup` (`video_id, kind, value` — cobre a derivação de estado atual por vídeo), `idx_video_declarations_kind_value` (`kind, value` — suporta busca por marcação sem partir de um vídeo específico).

## 7. Confirmação: nenhuma migration congelada foi alterada

`tests/test_migrations_frozen.py::test_migrations_congeladas_nao_foram_alteradas` compara SHA-256 byte a byte de `m001`–`m008` contra os hashes já congelados em rodadas anteriores — passa sem nenhuma mudança nesses hashes. `test_nenhuma_migration_nova_fora_da_lista_congelada` confirma que `m009_video_declarations.py` é a única migration nova e está conscientemente na lista.

## 8. Confirmação: nenhuma consulta de filtro/paginação/contagem carrega a tabela inteira em memória

Nenhum método usa `LocalDatabase.list()`. Exemplo real de SQL usado por `filter_items` (duas consultas deliberadas — ver seção 9 sobre a otimização de paginação):

```sql
-- passo 1: resolve SOMENTE os ids da página, sem nenhuma coluna rica
SELECT v.id AS video_id FROM videos v
WHERE (EXISTS (SELECT 1 FROM jobs j WHERE j.video_id = v.id AND j.status = 'PUBLISHED'))
  AND NOT (0)  -- badge impossível (ex. CAPTIONS) vira literal "0"
ORDER BY v.created_at, v.id LIMIT ? OFFSET ?;

-- passo 2: computa os campos ricos SOMENTE para os ids resolvidos
SELECT v.id AS video_id, ..., CASE WHEN EXISTS (...) THEN 1 ELSE 0 END AS badge_published, ...
FROM videos v LEFT JOIN sources src ON src.id = v.source_asset_id
WHERE v.id IN (?, ?, ..., ?)
ORDER BY v.created_at, v.id;
```

`get_facets` usa uma única consulta `SELECT SUM(CASE WHEN <expr> THEN 1 ELSE 0 END) AS facet_<badge>, ... FROM videos v WHERE <filtro>` — nunca itera itens em Python para contar.

## 9. Escala — tempos medidos (0/1/100/1.000/10.000 registros sintéticos, distribuição não uniforme)

Medido com `pytest -s` na máquina de desenvolvimento (sandbox Linux, SQLite local, sem otimização de hardware específica — números são indicativos, não um SLA):

| n | `count_by_filter` | `list_items` (offset = n-50) | `filter_items` (2 badges) | `get_facets` (17 badges) |
|---|---|---|---|---|
| 0/1/100 | < 2ms | < 16ms | < 60ms | < 80ms |
| 1.000 | 0.0009s | 0.0125s | 0.0421s | 0.0702s |
| 10.000 | 0.0016s | 0.0871s | 3.3485s | 6.7891s |

**Otimização aplicada durante este Prompt** (não estava na primeira versão): a primeira implementação de `filter_items`/`list_items` computava as ~17 colunas ricas (badges/joins/contagens) por linha percorrida **antes** de aplicar `OFFSET`, o que tornava uma página perto do fim de um catálogo de 10.000 itens custar **14.9s** (`SELECT` com `LIMIT 50 OFFSET 9950` ainda avalia as subqueries correlacionadas para as 9.950 linhas descartadas). A correção (seção 8): resolver primeiro os `video_id` da página com uma consulta barata (sem colunas ricas), depois computar os campos ricos só para esse punhado de ids — reduziu para **0.087s**, uma melhora de ~170×. `filter_items`/`get_facets` continuam da ordem de segundos em 10.000 registros porque, por natureza, precisam avaliar os badges deriváveis (subqueries `EXISTS` correlacionadas) para cada linha da tabela ao menos uma vez (filtragem/agregação genuínas, não um full-load em Python) — ver "dívida técnica" abaixo.

## 10. Confirmação: bulk edit nunca altera SYSTEM_BADGES nem apaga marcação não solicitada

`bulk_edit` só aceita `assertions`/`flags`/`labels` como parâmetros — não existe caminho de código para passar um `SYSTEM_BADGE` (confirmado por `test_bulk_edit_nunca_altera_system_badges`, que inspeciona a assinatura real do método). Um campo em `KEEP` (padrão) nunca gera nenhuma escrita — testado diretamente (`test_bulk_edit_keep_nao_escreve_nada_para_o_campo`) e adversarialmente (`test_bulk_edit_nao_apaga_silenciosamente_campo_nao_mencionado`: marca `FLAG`+`LABEL` manualmente, roda `bulk_edit` só em `assertions`, confirma que `FLAG`/`LABEL` permanecem intocados byte a byte). `SET`/`UNSET` são idempotentes (só escrevem se o estado mudaria) e resultam em `unchanged` quando já satisfeitos.

## 11. Confirmação: Geração 1 e módulos protegidos intocados

Nenhum dos 10 módulos protegidos nem `domain/models.py` foi modificado. `_sistema/media_catalog.py` não importa nenhum deles (`test_media_catalog_nao_importa_modulos_protegidos`, AST) e não chama `subprocess`/`ffmpeg`/`ffprobe` (`test_media_catalog_nao_chama_subprocess_ffmpeg_ffprobe`, AST — não grep ingênuo, já que a própria docstring do módulo cita "ffmpeg"/"ffprobe" ao explicar por que não são usados, o mesmo problema já enfrentado nos Prompts 24b/25/26/27/27b). `test_media_catalog_so_toca_tabelas_de_leitura_e_video_declarations` confirma que toda linha de `INSERT`/`UPDATE`/`DELETE` no módulo referencia exclusivamente `video_declarations`.

## 12. GATE 6 — concorrência real

`test_duas_threads_gravando_flags_diferentes_no_mesmo_video_nenhuma_perdida` (2 threads, `Barrier`) e `test_muitas_threads_bulk_edit_concorrente_mesmo_video_nao_perde_marcacoes` (8 threads, `Barrier`, cada uma fazendo `bulk_edit` com um `LABEL` distinto no mesmo vídeo) confirmam que escritas concorrentes em `video_declarations` nunca se perdem — `LocalDatabase.transaction()` (`BEGIN IMMEDIATE`) serializa as escritas.

## 13. Testes automatizados executados

- `tests/test_media_catalog.py`: **64 testes**, todos passando (cobre: item sem processamento; múltiplos artifacts; job concluído/falho; artifact ausente/de kind arbitrário; badge nunca falso positivo — geral e parametrizado por badge; user assertion/flag/label persistentes com restart; labels livres; remoção/reativação; marcação manual nunca falsifica badge de sistema; bulk edit KEEP/SET/UNSET + não-apagamento silencioso + isolamento de falha por vídeo; filtros combinados AND/NOT; badge desconhecido rejeitado; paginação determinística; restart; nenhum vazamento entre vídeos — marcações e badges; concorrência GATE 6; escala 0/1/100/1.000/10.000 com tempos medidos; garantias estruturais AST; append-only da nova tabela via trigger; `origin` restrito a `USER`).
- Suíte completa: **1593 passed, 1 skipped, 36 subtests passed** (era 1529 antes deste Prompt).
- `python3 -m compileall -q _sistema tests empacotar_release.py` → limpo.

## 14. Como testar manualmente

```python
from _sistema.storage.database import LocalDatabase
from _sistema.media_catalog import MediaCatalogService, CatalogFilter, BulkFieldOp, BULK_SET

db = LocalDatabase("caminho/para/painel.db")
db.initialize()
catalog = MediaCatalogService(db)

item = catalog.get_item("<video_id>")
pagina = catalog.list_items(limit=50, offset=0)
publicados = catalog.filter_items(CatalogFilter(badges_all=("PUBLISHED",)))
facets = catalog.get_facets()

catalog.set_user_flag("<video_id>", "FAVORITE")
resultado = catalog.bulk_edit(["<id1>", "<id2>"], flags=BulkFieldOp(mode=BULK_SET, values=("REVIEWED",)))
```

## 15. Riscos conhecidos e dívida técnica

- `filter_items`/`get_facets` continuam O(n) no lado do SQL (não em Python) em relação ao total de vídeos, porque badges deriváveis exigem `EXISTS` correlacionado por linha — em 10.000 registros isso já soma alguns segundos. Uma melhoria futura razoável seria materializar os badges deriváveis em colunas denormalizadas atualizadas incrementalmente (trigger ou write-through) em vez de recomputadas em toda consulta — decisão arquitetural que caberia a um Prompt futuro dedicado, não decidida unilateralmente aqui (overengineering não justificado hoje, já que o roadmap não pede um SLA específico).
- Paginação é por `OFFSET` (não por cursor/keyset). Após a otimização da seção 9, o custo de `OFFSET` profundo ficou baixo (a query de resolução de ids não carrega colunas ricas), mas ainda existe um custo residual de ordenação para descartar as linhas puladas. Paginação por cursor (`WHERE (created_at, id) > (?, ?)`) eliminaria esse resíduo e é uma melhoria futura natural quando o frontend (fora de escopo deste Prompt) precisar de scroll infinito em catálogos muito grandes.
- `Publication`/`Schedule` continuam nunca criados pelo pipeline novo (mesma lacuna já documentada e corrigida parcialmente para `Video` no Prompt 27b) — o catálogo funciona corretamente com 0 registros desses tipos por vídeo (testado explicitamente), mas isso significa que `SCHEDULED`/`publication_summary` ficam vazios para todo vídeo que não passou por `legacy_migration.py` ou por um fluxo futuro que ainda não existe. Não é uma lacuna deste Prompt corrigir (documentado como contexto conhecido, igual à seção 0.4 do Prompt original).
- `content_status.title_available`/`description_available` dependem de `Publication.title`/`description`, que hoje só existem via `legacy_migration.py` ou testes — nenhum fluxo novo os popula ainda.
- `error_summary` usa `ErrorRecord.code`/`message` diretamente; ambos já são, por contrato de todo o projeto (GATE 10), campos sanitizados na origem — este módulo não adiciona sanitização própria, apenas concatena os dois campos já seguros.

## 16. Pendências

Nenhuma pendência aberta deste Prompt. Não foi criado frontend, não foi iniciado o Prompt 28, nenhuma migration congelada foi alterada, nenhum badge "impossível hoje" dispara sem evidência real, e nenhuma consulta de filtro/paginação/contagem carrega a tabela inteira em memória — todas as condições explícitas de "não declarar concluído" do Prompt foram verificadas.

## 17. Entrega / verificação (ZIP)

Ver seção separada abaixo com a saída literal de `unzip -l`, SHA-256 do ZIP enviado, e status de `device_bash`.
