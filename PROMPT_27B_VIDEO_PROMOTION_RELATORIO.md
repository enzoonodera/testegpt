# PROMPT 27B — PROMOÇÃO DE SourceAsset PARA Video — Relatório de Entrega

Este Prompt não está no roadmap original — é corretivo, criado porque a auditoria do Prompt 27 encontrou uma lacuna real antes de o Prompt 27.5 (Media Catalog Backend) ser iniciado: nenhum código do pipeline novo (Prompts 24/24b/25/26/27) criava uma entidade `Video`, apenas `storage/legacy_migration.py` (migração de dados antigos).

## 1. Arquivos criados

- `_sistema/video_promotion.py` (14013 bytes) — `VideoPromotionService`, 3 classes de erro estruturado.
- `tests/test_video_promotion.py` (16604 bytes) — 30 testes automatizados.
- `PROMPT_27B_VIDEO_PROMOTION_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `empacotar_release.py` — adicionada a entrada `"PROMPT_27B_VIDEO_PROMOTION_RELATORIO.md"` em `ALLOWED_ROOT_FILES`, logo após a entrada do Prompt 27. Tamanho final: **35933 bytes**. Nenhuma outra linha alterada.

Nenhum outro arquivo do projeto foi modificado. **`domain/models.py` não foi alterado** (confirmado: 15978 bytes, idêntico ao anterior). **Nenhuma migration nova foi criada** — confirmado abaixo (seção 5).

## 3. Confirmação do contexto (seção 0 do Prompt)

- **0.1** — confirmado via busca no código-fonte, antes de escrever qualquer linha: `Video(` só aparecia em `domain/models.py` (definição) e `storage/legacy_migration.py` (migração). Reconfirmado por teste vivo (`test_apenas_video_promotion_e_legacy_migration_criam_video_no_projeto`), que varre todo `_sistema/` e falha se qualquer outro arquivo (além dos esperados) contiver `Video(`.
- **0.2** — `Video`/tabela `videos` já existem desde `m001_initial.py` (congelada, sem índice único em `source_asset_id`). Nenhuma migration nova foi criada. `status="ACTIVE"` (valor padrão já existente na dataclass) foi confirmado como correto para este momento do ciclo de vida — um vídeo recém-promovido está imediatamente disponível para edição, e não existe hoje nenhum status intermediário mais adequado.
- **0.3** — a decisão do Prompt 24 (importadores nunca criam `Video` automaticamente) foi reafirmada, não reaberta. `VideoPromotionService.promote()` é um passo explícito e sob demanda.
- **0.4** — confirmado lendo `m001_initial.py`: nenhum `UNIQUE`/índice único sobre `videos.source_asset_id`. O modelo permite, em princípio, múltiplos `Video`s por origem. A decisão de idempotência tomada por este módulo está na seção 4 abaixo.

## 4. Comportamento novo

`VideoPromotionService`:

- `promote(source_asset_id) -> Video` — busca o `SourceAsset`, cria e persiste um `Video` novo (`source_asset_id`, `name` derivado, `status="ACTIVE"`) dentro de uma transação atômica. Idempotente (ver decisão abaixo).
- `list_videos_for_source(source_asset_id) -> tuple[Video, ...]` — lista, em ordem determinística (`created_at`, `id`), todos os `Video`s já promovidos a partir de uma origem — inclusive vídeos criados por outro caminho (ex. `legacy_migration.py`).

Derivação de `name` (nenhum campo novo foi inventado em `SourceAsset`/`Video`): prioridade `original_name` → nome-base de `local_path` → `source_uri` inteiro → fallback genérico `"video-sem-nome-<8 primeiros chars do id>"`.

## 5. Migrations

**Nenhuma migration nova foi criada.** `LATEST_SCHEMA_VERSION` permanece em 8 (confirmado por teste: `test_video_promotion_nao_cria_migration_nova`). A tabela `videos` já existente desde `m001_initial.py` comporta integralmente a promoção definida neste Prompt.

## 6. Decisão da seção 1.3 (idempotência) e justificativa

**Decisão**: `promote()` é IDEMPOTENTE — no máximo 1 `Video` é criado por `SourceAsset` através deste serviço. Chamadas repetidas (inclusive concorrentes) devolvem sempre o mesmo `Video` já existente (o mais antigo, por `created_at`/`id` como desempate, caso múltiplos já existam por outro caminho), nunca criam um segundo.

**Justificativa**:
- O Prompt 27.5 (Media Catalog Backend, ainda não iniciado) vai depender de uma relação previsível entre origem e item de catálogo — um resultado estável e repetível para "promover uma origem" evita que um duplo-clique numa UI futura, ou uma reconciliação que rechama `promote()` por segurança, multipliquem vídeos silenciosamente.
- Nada no roadmap deste Prompt nem do 27.5 pede explicitamente múltiplos vídeos por origem — é um caso de uso hipotético, não confirmado. Elevar a multiplicação a comportamento padrão sem necessidade demonstrada seria overengineering (princípio P do CLAUDE.md).
- Se um caso de uso real de múltiplas edições da mesma origem aparecer no futuro, ele pode ser modelado de forma explícita (ex. um parâmetro `force_new=True` num Prompt futuro) — não como efeito colateral silencioso de uma chamada simples.

**Como foi testada sob concorrência real (não apenas sequencial)**: `test_duas_chamadas_concorrentes_promovendo_o_mesmo_source_asset_nunca_duplicam` usa `threading.Barrier(2)` para forçar duas threads reais (cada uma com sua própria instância de `LocalDatabase`/`VideoPromotionService`, contra o mesmo arquivo SQLite) a chamar `promote()` para o MESMO `source_asset_id` no mesmo instante. Um segundo teste adversarial (`test_muitas_chamadas_concorrentes_mesmo_source_asset_apenas_um_video`) estende isso para 8 threads simultâneas. Em ambos, ao final, existe exatamente 1 `Video` para aquela origem — nunca um teste probabilístico: a resolução é via `BEGIN IMMEDIATE` (mesma técnica de `EditProjectManager`/`ControlManager`), que adquire o write lock do SQLite imediatamente ao abrir a transação, forçando qualquer chamada concorrente a bloquear até a primeira commitar e então ler o `Video` já criado.

## 7. Confirmação: promoção continua explícita/sob demanda

Nenhum dos três importadores (`LocalFileImporter`/`FolderImporter`/`UrlImporter`) referencia `VideoPromotionService`/`video_promotion`/`promote(` — confirmado por teste AST (`test_promocao_nao_e_chamada_automaticamente_pelos_importadores`, inspecionando nós `ast.Call`/`ast.Import` reais em `source_import.py`, nunca grep textual — a própria docstring de `video_promotion.py` cita os importadores em prosa, o que colidiria com um grep ingênuo, mesma lição já aplicada nos Prompts 25/26/27).

## 8. Confirmação: `SourceAsset` nunca é modificado pela promoção

`test_promocao_nunca_modifica_o_source_asset_original` insere um `SourceAsset`, promove duas vezes (incluindo a chamada idempotente), e confirma que a linha em `sources` permanece byte-a-byte idêntica (`==`) antes/depois, e que `database.list(SourceAsset)` continua com exatamente 1 linha, sem qualquer alteração de campo.

## 9. Confirmação: Geração 1 e módulos protegidos intocados

Reconfirmado por comparação de tamanho em bytes idêntico ao round anterior (Prompt 27) antes de qualquer trabalho neste Prompt: `circuit_breaker.py` (27989), `retry_policy.py` (27252), `publication_idempotency.py` (21683), `secrets_manager.py` (17712), `domain/job_state_machine.py` (5442), `source_import.py` (49320), `source_context.py` (20616), `media_probe.py` (23350), `edit_project.py` (19528), `domain/models.py` (15978), `agendar_youtube.py` (91980), `agendar_tiktok.py` (97511), `limpar_metadados_oficial.py` (27891), `gerar_textos.py` (17683), `painel_oficial.py` (44162) — todos idênticos, confirmados também na saída de `device_list_dir` do diretório `_sistema/` na máquina Windows antes da entrega. Confirmado também por teste AST (`test_video_promotion_nao_importa_modulos_protegidos`, `test_video_promotion_nao_importa_nada_de_geracao_1`) que `video_promotion.py` não importa nenhum deles.

## 10. Testes automatizados executados

**30 testes novos em `tests/test_video_promotion.py`, todos passando:**

```
test_apenas_video_promotion_e_legacy_migration_criam_video_no_projeto
test_promover_source_asset_valido_cria_video_corretamente
test_promover_deriva_nome_de_local_path_quando_sem_original_name
test_promover_deriva_nome_de_source_uri_quando_sem_nome_nem_local_path
test_promover_usa_fallback_generico_quando_nada_disponivel
test_promocao_sobrevive_a_restart
test_promover_source_asset_inexistente_gera_erro_estruturado
test_promover_source_asset_id_invalido_gera_erro_estruturado[6 variações: "", "nao-e-um-uuid", "123", None, 42, []]
test_list_videos_for_source_id_invalido_gera_erro_estruturado
test_construcao_rejeita_database_invalido
test_promover_o_mesmo_source_asset_duas_vezes_sequencialmente_e_idempotente
test_promover_duas_vezes_nao_cria_segundo_video_no_banco
test_promover_origens_diferentes_cria_videos_distintos
test_list_videos_for_source_vazio_antes_de_promover
test_list_videos_for_source_um_video_apos_promover
test_list_videos_for_source_reflete_multiplos_videos_criados_fora_do_servico
test_duas_chamadas_concorrentes_promovendo_o_mesmo_source_asset_nunca_duplicam
test_muitas_chamadas_concorrentes_mesmo_source_asset_apenas_um_video
test_promocao_nunca_modifica_o_source_asset_original
test_video_promotion_nao_importa_modulos_protegidos
test_video_promotion_nao_importa_nada_de_geracao_1
test_nenhum_job_artifact_publication_schedule_project_criado
test_nenhuma_outra_entidade_criada_no_banco
test_video_promotion_nao_cria_migration_nova
test_promocao_nao_e_chamada_automaticamente_pelos_importadores

30 passed in 0.47s
```

Destaques:
- **Idempotência sequencial e concorrente**, ambas testadas diretamente (seção 6 acima).
- **Restart (GATE 4)**: `test_promocao_sobrevive_a_restart` reabre com NOVAS instâncias de `LocalDatabase`/`VideoPromotionService`.
- **Vídeos legados**: `test_list_videos_for_source_reflete_multiplos_videos_criados_fora_do_servico` cria 2 `Video`s manualmente (simulando `legacy_migration.py`) para a mesma origem e confirma que `list_videos_for_source` os lista corretamente, e que uma chamada subsequente a `promote()` devolve um dos dois já existentes, sem criar um terceiro.
- **Derivação de nome**, testada nas 4 combinações (com `original_name`, só `local_path`, só `source_uri`, e nenhum dos três).

**Suíte completa (Prompts 19–27B, sem remover nem enfraquecer nenhum teste anterior):**

```
1529 passed, 1 skipped, 36 subtests passed in 104.03s (0:01:44)
```

(1499 da rodada anterior + 30 novos = 1529; o único teste pulado é pré-existente de rounds anteriores, não relacionado a este Prompt.)

**`compileall`:**

```
python3 -m compileall -q _sistema tests empacotar_release.py
compileall OK
```

## 11. Como testar manualmente

```python
from pathlib import Path
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.domain import SourceAsset
from _sistema.video_promotion import VideoPromotionService

paths = build_app_paths(data_root=Path("C:/temp/painel_teste"))
ensure_app_directories(paths)
db = LocalDatabase(paths.database / "painel.db")
db.initialize()

asset = SourceAsset(source_uri="C:/videos/bruto.mp4", original_name="bruto.mp4")
db.insert(asset)

svc = VideoPromotionService(db)
video = svc.promote(asset.id)
print(video)                              # Video(..., source_asset_id=..., name='bruto.mp4', status='ACTIVE')
print(svc.promote(asset.id).id == video.id)  # True -- idempotente
print(svc.list_videos_for_source(asset.id))  # (video,)
```

## 12. Riscos conhecidos e dívida técnica

- **A idempotência é uma garantia apenas do caminho `VideoPromotionService`**: se algum código futuro criar `Video` diretamente via `database.insert(Video(...))` fora deste serviço (como já acontece hoje em `legacy_migration.py`, por razões próprias daquele fluxo de migração), múltiplos `Video`s para a mesma origem PODEM existir — o serviço nunca cria um segundo por conta própria, mas não impede outro código de fazê-lo. `list_videos_for_source` foi projetado justamente para deixar esse cenário visível a um consumidor futuro (Prompt 27.5), em vez de escondê-lo.
- **`Video.status="ACTIVE"` é o único status usado**: nenhum outro valor de `status` foi considerado ou testado neste Prompt (o campo já existia com esse default). Se o Editor (Prompt 28+) precisar de um ciclo de vida mais rico (ex. `ARCHIVED`, `DRAFT`), isso é trabalho futuro, fora de escopo aqui.
- **Reafirmação explícita pedida pelo Prompt**: o Prompt 27.5 (Media Catalog) poderá agora contar com `Video`s reais promovidos a partir de `SourceAsset`s importados nos Prompts 24/24b — mas a promoção continua manual/sob demanda. Ninguém dispara `promote()` automaticamente hoje; isso fica para uma camada de UI/fluxo futura, fora de escopo deste Prompt.
- Nenhum dos riscos acima compromete a garantia central testada (idempotência sob concorrência real) nem a integridade do `SourceAsset` original.

## 13. Pendências

Nenhuma pendência dentro do escopo deste Prompt corretivo. Prompt 27.5 e Prompt 28 **não** foram iniciados.

## 14. Verificação da entrega (ZIP)

`device_bash` permaneceu indisponível durante todo este round ("Workspace unavailable. The isolated Linux environment on this device failed to start."), verificado no início da fase de entrega. Por isso, o ZIP de verificação abaixo foi construído localmente no sandbox de nuvem, reproduzindo `empacotar_release.py` com os 6 arquivos raiz exclusivos do Windows (`EMPACOTAR_RELEASE.bat`, `INSTALAR_DEPENDENCIAS_TESTE.bat`, `LEIA_ME_PRIMEIRO.txt`, `LIMPAR_ANTES_DE_ZIPAR.bat`, `PAINEL_OFICIAL.bat`, `RODAR_TESTES.bat`) **emprestados temporariamente** (via `device_stage_files`, somente leitura) da máquina Windows real, copiados para a raiz do projeto no sandbox só para permitir a execução do empacotador, e **removidos do sandbox logo em seguida** (não fazem parte de nenhum artefato deste round além do ZIP de verificação abaixo).

Isso significa: este ZIP comprova que `empacotar_release.py` produz um pacote consistente com o conteúdo atualmente confirmado byte-a-byte na máquina Windows (`_sistema/video_promotion.py`, `tests/test_video_promotion.py`, `empacotar_release.py`, e todos os arquivos protegidos/anteriores), mas **não é** o mesmo processo de build que rodaria nativamente no Windows via `EMPACOTAR_RELEASE.bat` — divulgação honesta, mesmo padrão já usado nos relatórios dos Prompts 24b/25/26/27 enquanto `device_bash` estava indisponível.

**SHA-256 do ZIP gerado nesta rodada:**
```
881149faa8930afee795aa2cbe9ac244760f94a8c38dff1a66d78b669f3042ae
```
Arquivo: `PAINEL_OFICIAL_ZIP_VERIFICACAO_27B.zip` (0.88 MB, 126 arquivos).

**Saída literal de `unzip -l`:**
```
Archive:  PAINEL_OFICIAL_ZIP_VERIFICACAO_27B.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-22 22:55   ARQUITETURA_ATUAL.md
    13132  2026-09-22 22:55   CLAUDE.md
     2687  2026-09-22 22:55   EMPACOTAR_RELEASE.bat
   169662  2026-09-22 22:55   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-22 22:55   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-22 22:55   LEIA_ME_PRIMEIRO.txt
      866  2026-09-22 22:55   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-22 22:55   MAPA_DE_DADOS.md
      686  2026-09-22 22:55   PAINEL_OFICIAL.bat
    29374  2026-09-22 22:55   PRODUCT_INVARIANTS.md
    23512  2026-09-22 22:55   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-22 22:55   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-22 22:55   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-22 22:55   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-22 22:55   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    31899  2026-09-22 22:55   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-22 22:55   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    28360  2026-09-22 22:55   PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md
    29422  2026-09-22 22:55   PROMPT_26_MEDIA_PROBE_RELATORIO.md
    28168  2026-09-22 22:55   PROMPT_27_EDIT_PROJECT_RELATORIO.md
    24543  2026-09-22 22:55   REGRESSION_CHECKLIST.md
    26204  2026-09-22 22:55   RISCOS_ATUAIS.md
    34429  2026-09-22 22:55   ROADMAP_COMPLETO.md
     6104  2026-09-22 22:55   RODAR_TESTES.bat
    15403  2026-09-22 22:55   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-22 22:55   _sistema/agendar_tiktok.py
    91980  2026-09-22 22:55   _sistema/agendar_youtube.py
    18611  2026-09-22 22:55   _sistema/app_paths.py
    68310  2026-09-22 22:55   _sistema/batch_engine.py
    27989  2026-09-22 22:55   _sistema/circuit_breaker.py
    55733  2026-09-22 22:55   _sistema/control_manager.py
     2545  2026-09-22 22:55   _sistema/domain/__init__.py
     3307  2026-09-22 22:55   _sistema/domain/checkpoints.py
     5442  2026-09-22 22:55   _sistema/domain/job_state_machine.py
    15978  2026-09-22 22:55   _sistema/domain/models.py
    19528  2026-09-22 22:55   _sistema/edit_project.py
    17683  2026-09-22 22:55   _sistema/gerar_textos.py
    85229  2026-09-22 22:55   _sistema/job_engine.py
    27891  2026-09-22 22:55   _sistema/limpar_metadados_oficial.py
     2724  2026-09-22 22:55   _sistema/login_conta.py
    23350  2026-09-22 22:55   _sistema/media_probe.py
    44162  2026-09-22 22:55   _sistema/painel_oficial.py
    21683  2026-09-22 22:55   _sistema/publication_idempotency.py
    32885  2026-09-22 22:55   _sistema/recovery_manager.py
    48725  2026-09-22 22:55   _sistema/resource_manager.py
    27252  2026-09-22 22:55   _sistema/retry_policy.py
    17712  2026-09-22 22:55   _sistema/secrets_manager.py
    94188  2026-09-22 22:55   _sistema/shutdown_coordinator.py
    20616  2026-09-22 22:55   _sistema/source_context.py
    49320  2026-09-22 22:55   _sistema/source_import.py
     1036  2026-09-22 22:55   _sistema/state_json.py
     3365  2026-09-22 22:55   _sistema/storage/__init__.py
    19254  2026-09-22 22:55   _sistema/storage/audit.py
    51041  2026-09-22 22:55   _sistema/storage/backup.py
    27352  2026-09-22 22:55   _sistema/storage/database.py
    38955  2026-09-22 22:55   _sistema/storage/legacy_migration.py
     1266  2026-09-22 22:55   _sistema/storage/migrations/__init__.py
     7411  2026-09-22 22:55   _sistema/storage/migrations/m001_initial.py
      764  2026-09-22 22:55   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-22 22:55   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-22 22:55   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-22 22:55   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-22 22:55   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-22 22:55   _sistema/storage/migrations/m007_source_asset_declarations.py
     4856  2026-09-22 22:55   _sistema/storage/migrations/m008_source_asset_context.py
   119736  2026-09-22 22:55   _sistema/storage_manager.py
    14323  2026-09-22 22:55   _sistema/time_utils.py
    14013  2026-09-22 22:55   _sistema/video_promotion.py
    35933  2026-09-22 22:55   empacotar_release.py
       33  2026-09-22 22:55   requirements.txt
     6014  2026-09-22 22:55   tests/README.md
       65  2026-09-22 22:55   tests/__init__.py
    11123  2026-09-22 22:55   tests/fakes_playwright.py
       21  2026-09-22 22:55   tests/requirements-test.txt
    23822  2026-09-22 22:55   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-22 22:55   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-22 22:55   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-22 22:55   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-22 22:55   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-22 22:55   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-22 22:55   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-22 22:55   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-22 22:55   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-22 22:55   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-22 22:55   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-22 22:55   tests/test_ai_cache_parsing.py
    20247  2026-09-22 22:55   tests/test_app_paths.py
    31890  2026-09-22 22:55   tests/test_backup_restore.py
    70112  2026-09-22 22:55   tests/test_batch_engine.py
    19226  2026-09-22 22:55   tests/test_checkpoints.py
    26822  2026-09-22 22:55   tests/test_circuit_breaker.py
     7389  2026-09-22 22:55   tests/test_config_names_paths.py
    86602  2026-09-22 22:55   tests/test_control_manager.py
    13408  2026-09-22 22:55   tests/test_domain_models.py
    22810  2026-09-22 22:55   tests/test_edit_project.py
     6240  2026-09-22 22:55   tests/test_ffmpeg_processing.py
     4450  2026-09-22 22:55   tests/test_fingerprint_state.py
     5547  2026-09-22 22:55   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-22 22:55   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-22 22:55   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-22 22:55   tests/test_job_engine.py
     6839  2026-09-22 22:55   tests/test_job_state_machine.py
    17472  2026-09-22 22:55   tests/test_legacy_json_migration.py
     3209  2026-09-22 22:55   tests/test_limpeza_permission_error.py
    23595  2026-09-22 22:55   tests/test_media_probe.py
     5888  2026-09-22 22:55   tests/test_migrations_frozen.py
    11476  2026-09-22 22:55   tests/test_operational_audit.py
    16612  2026-09-22 22:55   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-22 22:55   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-22 22:55   tests/test_publication_idempotency.py
    36620  2026-09-22 22:55   tests/test_recovery_manager.py
    37274  2026-09-22 22:55   tests/test_release_packaging.py
   105416  2026-09-22 22:55   tests/test_resource_manager.py
     4676  2026-09-22 22:55   tests/test_resume_detection.py
    31831  2026-09-22 22:55   tests/test_retry_policy.py
     7501  2026-09-22 22:55   tests/test_schedule_slots.py
    19241  2026-09-22 22:55   tests/test_secrets_manager.py
    89276  2026-09-22 22:55   tests/test_shutdown_coordinator.py
    21566  2026-09-22 22:55   tests/test_source_context.py
    18065  2026-09-22 22:55   tests/test_source_import.py
    29758  2026-09-22 22:55   tests/test_source_import_options.py
    24664  2026-09-22 22:55   tests/test_sqlite_storage.py
     7928  2026-09-22 22:55   tests/test_state_json_persistence.py
   116323  2026-09-22 22:55   tests/test_storage_manager.py
     5928  2026-09-22 22:55   tests/test_time_utils.py
    16604  2026-09-22 22:55   tests/test_video_promotion.py
---------                     -------
  3207928                     126 files
```

**Nota**: como este relatório (`PROMPT_27B_VIDEO_PROMOTION_RELATORIO.md`) é escrito *depois* da construção do ZIP acima, ele próprio não está incluído nesse ZIP específico — mesmo padrão já usado nos relatórios dos Prompts 25/26/27. O relatório é entregue separadamente à máquina Windows, com sua própria verificação byte-a-byte.

**Varredura de padrões sensíveis** (`grep -rIniE "senha|password|token|cookie|authorization|api[_-]?key|secret"` sobre o conteúdo dos dois arquivos novos deste round): apenas duas ocorrências, ambas benignas — a docstring de `video_promotion.py` citando `secrets_manager` como um dos módulos que ele nunca importa, e o teste `test_video_promotion_nao_importa_modulos_protegidos` listando `"secrets_manager"` como termo proibido a verificar. Nenhum segredo real, credencial ou dado sensível encontrado — mesmo padrão já confirmado nos Prompts 24b/25/26/27.

Os 6 arquivos `.bat`/`.txt` emprestados foram removidos do sandbox de nuvem imediatamente após a construção do ZIP acima.
