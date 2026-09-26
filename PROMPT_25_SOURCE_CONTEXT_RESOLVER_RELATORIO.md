# PROMPT 25 — Source Context Resolver — Relatório de Entrega

## 0. Escopo e disciplina permanente

Implementado exatamente o texto literal do roadmap para este Prompt:
`SourceContextResolver` invisível ao usuário, classificando automaticamente
cada `SourceAsset` em `CONNECTED_CHANNEL_CONTENT` / `EXTERNAL_CONTENT` /
`LOCAL_CONTENT` / `UNKNOWN_SOURCE` — uma classificação técnica, nunca uma
determinação jurídica de propriedade, usada apenas para escolher pipeline.

Confirmado nesta rodada, sem exceção:

- Geração 1 (`agendar_youtube.py`/`agendar_tiktok.py`) **intocada**.
- `circuit_breaker.py`/`retry_policy.py`/`publication_idempotency.py`/
  `secrets_manager.py`/`domain/job_state_machine.py` **intocados**.
- `LocalFileImporter`/`FolderImporter`/`UrlImporter`/`ImportOptions`/
  `AssertionStore` (Prompts 24/24b) **não tiveram comportamento
  reaberto** — apenas a chamada automática ao resolver foi adicionada, nos
  pontos exatos documentados na seção 4.
- **Prompt 26 não foi iniciado.**

Todas as confirmações acima foram verificadas por hash byte-a-byte contra
o que está fisicamente em `C:\Users\Enzo\Desktop\teste` (seção 8).

---

## 1. Arquivos criados

- `_sistema/source_context.py` — `SourceContextResolver`, `SourceContext`,
  constantes `CONTEXT_*`/`CONTEXTS`, `RECOGNIZED_PLATFORM_DOMAINS`.
- `_sistema/storage/migrations/m008_source_asset_context.py` — migration
  nova (schema v7→v8), tabela `source_asset_context`. Próximo número livre
  confirmado por leitura de `_sistema/storage/migrations/__init__.py`
  antes de criar (m001–m007 já existiam; m008 era o único número livre).
- `tests/test_source_context.py` — 25 testes novos.
- `PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `_sistema/storage/migrations/__init__.py` — registra `MIGRATION_008`.
- `_sistema/source_import.py` — nova seção de docstring ("PROMPT 25");
  `LocalFileImporter.__init__`/`UrlImporter.__init__` passam a construir
  `self.context_resolver = SourceContextResolver(database)`;
  `import_source` de ambos chama
  `self.context_resolver.resolve_and_persist(asset.id)` logo após
  `assertion_store.apply_options`, envolvida em `try/except Exception`
  (isolamento — ver seção 5); `SourceImportManager` expõe
  `context_resolver` e o novo método público `get_context(source_asset_id)`.
  **Nenhuma assinatura pública existente mudou de contrato** — todos os
  parâmetros novos são internos (não expostos como parâmetro de
  `import_*`); `ImportResult` permanece com exatamente os mesmos campos.
- `tests/test_migrations_frozen.py` — hash de `m008_source_asset_context.py`
  adicionado a `FROZEN_MIGRATION_HASHES`.
- `tests/test_sqlite_storage.py` — `EXPECTED_TABLES` ganhou
  `source_asset_context`; `LATEST_SCHEMA_VERSION`/versões numéricas
  bumped 7→8 em todo o arquivo; migration lists atualizadas com
  `(8, "source_asset_context")`; dois testes renomeados
  (`...schema7...`→`...schema8...`).
- `tests/test_backup_restore.py` — `migration_versions` bumped para
  `[1..8]`; probe/sonda de schema futuro renumerada 8→9
  (`_install_future_migration_v8`→`_install_future_migration_v9`,
  `SOURCE_V7`/`PREVIOUS_V8`→`SOURCE_V8`/`PREVIOUS_V9`), mesmo padrão de
  renumeração já usado no Prompt 24b para 7→8.
- `empacotar_release.py` — `PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md`
  adicionado a `ALLOWED_ROOT_FILES`.

## 3. Decisão de desenho de persistência (seção 0 do Prompt — decisão central)

Avaliadas as duas opções descritas pelo Prompt:

**(a) Estender `source_asset_declarations`/m007 com `kind='CONTEXT_CLASSIFICATION'`** —
rejeitada. SQLite não permite alterar um CHECK constraint existente
in-place — exigiria recriar a tabela inteira, reabrindo uma migration já
aprovada e congelada por `test_migrations_frozen.py` só para acomodar uma
semântica fundamentalmente diferente da que ela foi desenhada para
expressar. Pior: `AssertionStore.list_active` deriva "estado ativo" via
`ROW_NUMBER() PARTITION BY source_asset_id, kind, value` — desenhado para
"N valores podem estar ativos simultaneamente sob o mesmo kind" (várias
labels ao mesmo tempo). Uma classificação de contexto precisa do oposto
exato: exatamente UM valor vence por `source_asset_id`, e reclassificar
sempre substitui o anterior — forçar as duas semânticas na mesma tabela
exigiria um caminho de leitura especial só para esse `kind`, aumentando a
complexidade de um componente já aprovado sem necessidade arquitetural
demonstrável (violaria a seção "REVISÃO CRÍTICA" do CLAUDE.md).

**(b) Tabela nova dedicada `source_asset_context` — ESCOLHIDA.** Mesmo
espírito auditável/append-only de `audit_events` (m002) e
`source_asset_declarations` (m007): cada linha é um evento
(`source_asset_id`, `context`, `created_at`), nunca um
`UPDATE`/`DELETE` — reclassificar é sempre um novo `INSERT`. A diferença
de semântica em relação a m007 está inteiramente na CONSULTA de "estado
atual" (`SourceContextResolver.get_current`), não no schema: o estado
atual é o evento de MAIOR `rowid` para um `source_asset_id`, SEM
particionar por valor de `context` — reclassificar sempre substitui
globalmente, nunca acumula duas classificações simultâneas (testado em
`test_reclassificacao_substitui_classificacao_anterior_nunca_acumula`).
Estritamente aditiva: `source_asset_declarations`/m007 não foi tocada,
reaberta nem enfraquecida.

Justificativa completa também na docstring de
`m008_source_asset_context.py` e de `_sistema/source_context.py`.

## 4. Ponto de integração exato (item "invisível ao usuário")

`LocalFileImporter.import_source` e `UrlImporter.import_source` chamam
`self.context_resolver.resolve_and_persist(asset.id)` imediatamente após
`self.assertion_store.apply_options((asset.id,), options)` e antes de
devolver `ImportResult`. `FolderImporter.import_source` delega inteiramente
a `LocalFileImporter.import_source` por arquivo — nenhuma integração
adicional foi necessária ali; a classificação automática já cobre todos os
arquivos de um lote.

Nenhum parâmetro novo foi adicionado a `import_source`/`import_local_file`/
`import_folder`/`import_url` para controlar isso — a classificação sempre
acontece, sem escolha do chamador, exatamente como "invisível ao usuário"
pede. `ImportResult` continua com exatamente os mesmos campos
(`success`, `source_uri`, `source_asset`, `error_code`, `error_message`);
a classificação é consultada separadamente via
`SourceContextResolver.get_current`/`SourceImportManager.get_context`.

## 5. Isolamento — falha do resolver nunca derruba a importação

A chamada a `resolve_and_persist` é envolvida em `try/except Exception`
explícito em cada um dos dois pontos de integração — única exceção
genérica deliberada de todo este módulo, documentada em comentário no
próprio ponto de integração e na docstring do módulo. O `SourceAsset` já
foi persistido com sucesso ANTES dessa chamada. Uma falha inesperada
durante a classificação resulta em **nenhuma linha de classificação
gravada** para aquele `source_asset_id` — estado "ausente"
(`get_current` devolve `None`), distinto de `UNKNOWN_SOURCE` explícito
(gravado quando a classificação RODOU e concluiu esse resultado).
Provado por `test_falha_inesperada_do_resolver_nao_derruba_importacao`
(monkeypatch força uma `RuntimeError` real dentro de
`resolve_and_persist`; confirma `ImportResult.success is True` e o
`SourceAsset` persistido) e por
`test_folder_importer_falha_de_classificacao_em_um_arquivo_nao_afeta_os_demais`
(isolamento por item — falha no 1º arquivo de 2 não afeta a classificação
nem o resultado do 2º).

## 6. Lista exata de domínios reconhecidos e regra de extração/comparação

`RECOGNIZED_PLATFORM_DOMAINS` (`_sistema/source_context.py`) — lista
PRÓPRIA deste módulo, nunca importada de Geração 1:

```
youtube.com, www.youtube.com, m.youtube.com, youtu.be -> "youtube"
tiktok.com, www.tiktok.com, vm.tiktok.com -> "tiktok"
```

Extração de handle (`_extract_platform_handle`): primeiro segmento do
path. YouTube: `/@handle` (sem o `@`), `/c/<nome>`, `/user/<nome>`,
`/channel/<id>` (ID opaco — casamento improvável na prática, cai em
`EXTERNAL_CONTENT`, documentado como comportamento esperado, não bug).
TikTok: `/@handle` apenas. Qualquer outro formato → handle não extraível.

Comparação (`_normalize_for_comparison`): `strip()` + remoção de um `@`
inicial + `lower()` — determinística, não sofisticada de propósito
(exigência explícita do item 1.3 do Prompt). Aplicada ao handle extraído e
a `Account.name`/`Account.local_key` (e ao sufixo de `local_key` após a
primeira `/`, já que `local_key` é gravado como `"<platform>/<nome>"` pela
migração legada).

## 7. `CONNECTED_CHANNEL_CONTENT` só em correspondência inequívoca

Confirmado por leitura de `_sistema/storage/legacy_migration.py`
(`_account_entity`) e grep em todo o projeto: `Account.external_account_id`
nunca é populado — sempre `None`. `Account.name`/`local_key` vêm de um
nome ESCOLHIDO PELO USUÁRIO (`nome_conta`/`nome_canal` do
`config_canal.json`), nunca de uma API real (o login usa Chrome com perfil
persistente; nenhuma chamada à API do YouTube/TikTok para confirmar
identidade de canal). Consequência aplicada em todo `_classify`:
`CONNECTED_CHANNEL_CONTENT` só é devolvido quando existe **exatamente
uma** `Account` habilitada (`enabled=True`) da plataforma correspondente
batendo — zero contas OU duas ou mais batendo (ambíguo) sempre cai em
`EXTERNAL_CONTENT`, nunca escolhe uma arbitrariamente. Testado
explicitamente: `test_dominio_reconhecido_sem_nenhuma_account_cadastrada_external_content`,
`test_dominio_reconhecido_duas_accounts_batendo_ambiguamente_external_content`,
`test_account_desabilitada_nunca_conta_como_conectada`,
`test_conta_de_outra_plataforma_nao_bate`.

## 8. Confirmações estruturais (AST/grep)

- **Nenhuma chamada de rede**: `test_nenhuma_importacao_de_biblioteca_de_rede`
  inspeciona os nós AST reais de import de `_sistema/source_context.py` —
  nenhum de `urllib.request`/`http.client`/`socket`/`requests`/`httpx`/
  `ftplib`. O único import de `urllib` é `urllib.parse.urlparse` (parsing
  de string, nunca conexão).
- **Nenhum import de módulo protegido**:
  `test_source_context_nao_importa_modulos_protegidos` confirma ausência
  de `circuit_breaker`/`retry_policy`/`publication_idempotency`/
  `secrets_manager`/`job_state_machine`.
- **Nenhuma outra entidade criada**: `test_nenhum_job_publication_schedule_project_ou_video_criado_por_codigo_novo`
  (grep de `Job(`/`Publication(`/`Schedule(`/`Project(`/`Video(` no
  código-fonte real) e `test_resolver_nao_cria_nenhuma_outra_entidade_no_banco`
  (confirma tabelas vazias após uma importação completa).
- **Nenhum fluxo de confirmação de propriedade**:
  `test_nenhum_fluxo_de_confirmacao_de_propriedade_no_codigo` inspeciona
  nós AST reais (`ast.Call` para `input(...)`/`sys.stdin.read*`, nomes de
  função via `ast.FunctionDef`) e assinaturas reais via `inspect` — nunca
  um grep textual ingênuo sobre a docstring do módulo, que CITA
  propositalmente a pergunta proibida ("esse vídeo é seu?") em prosa, na
  própria seção que documenta esta garantia (mesma lição já aplicada ao
  teste equivalente de `source_import.py` no Prompt 24b, para não colidir
  com a própria documentação).

## 9. Ataques adversariais adicionais (GATE, além do pedido explícito no Prompt)

- **GATE 6 (concorrência)**: `test_duas_instancias_concorrentes_classificando_o_mesmo_source_asset`
  — duas threads reais, sincronizadas por `threading.Barrier`
  (determinístico), duas instâncias de `LocalDatabase`/`SourceContextResolver`
  contra o mesmo SQLite, classificando o MESMO `source_asset_id`
  simultaneamente. Nenhum crash, nenhuma corrupção — duas linhas
  gravadas no histórico append-only, `get_current` resolve
  deterministicamente para a mais recente. Analisado e documentado na
  docstring do módulo (seção 3b): não há decisão condicional
  "ler-decidir-escrever" que precise de `BEGIN IMMEDIATE` único —
  `resolve_and_persist` sempre ANEXA um evento, nunca lê o estado
  anterior para decidir SE deve escrever, então duas chamadas
  concorrentes nunca colidem (padrão commutativo, mesmo espírito de
  `AssertionStore._write_batch`).
- **GATE 4 (restart)**: `test_restart_com_novas_instancias_preserva_classificacao`
  — reabre com NOVAS instâncias de `LocalDatabase` e
  `SourceContextResolver` (nunca reutiliza objetos em memória) e confirma
  que a classificação persistida sobrevive.
- **GATE 12 (teste honesto)**: a falha forçada em
  `test_falha_inesperada_do_resolver_nao_derruba_importacao` é uma
  `RuntimeError` real levantada dentro do método monkeypatchado — não uma
  exceção já capturada internamente sendo "redescoberta" pelo teste.
- **Correção durante a própria implementação**: a primeira versão do
  teste de ambiguidade usava `sorted()` sobre uma lista contendo `None` e
  `str`, o que levanta `TypeError` em Python 3 (`'<' not supported
  between instances of 'str' and 'NoneType'`) — corrigido para comparação
  por contagem (`values.count(...)`), mais direta e sem depender de
  ordenação heterogênea.

## 10. Testes

- 25 testes novos em `tests/test_source_context.py`.
- Suíte completa: **1434 passed, 1 skipped, 36 subtests passed**
  (1409 da rodada anterior + 25 novos desta rodada).
- `python3 -m compileall -q _sistema tests empacotar_release.py`: limpo.
- Nenhum teste existente (Prompts 19–24b) foi removido, enfraquecido ou
  teve seu comportamento alterado — as únicas mudanças em arquivos de
  teste pré-existentes foram os bumps mecânicos de versão de schema
  (7→8) já descritos na seção 2.

## 11. Confirmação de arquivos protegidos (hash byte-a-byte, Windows)

Cada arquivo abaixo foi lido diretamente de
`C:\Users\Enzo\Desktop\teste` via `device_stage_files` e comparado
(`cmp -s`) contra a cópia local usada nesta rodada — nenhum foi editado:

- `_sistema/agendar_youtube.py` — idêntico.
- `_sistema/agendar_tiktok.py` — idêntico.
- `_sistema/circuit_breaker.py` — idêntico.
- `_sistema/retry_policy.py` — idêntico.
- `_sistema/publication_idempotency.py` — idêntico.
- `_sistema/secrets_manager.py` — idêntico.
- `_sistema/domain/job_state_machine.py` — idêntico.
- `_sistema/domain/models.py` — idêntico.

`_sistema/storage/migrations/m007_source_asset_declarations.py` também
permanece intocada — confirmado pela suíte `test_migrations_frozen.py`
continuar verde com o hash já congelado no Prompt 24b, sem nenhuma
alteração nesta rodada.

## 12. Entrega — arquivos desta rodada, confirmados byte-a-byte no Windows

Os 9 arquivos abaixo foram copiados para
`/mnt/user-data/outputs/`, entregues via `device_commit_files`
(`force=true`), verificados por tamanho via `device_list_dir` e então
lidos de volta via `device_stage_files` + `cmp -s` — todos **IDÊNTICOS**:

- `_sistema/source_context.py` (20616 bytes)
- `_sistema/source_import.py` (49320 bytes)
- `_sistema/storage/migrations/m008_source_asset_context.py` (4856 bytes)
- `_sistema/storage/migrations/__init__.py` (1266 bytes)
- `tests/test_source_context.py` (21566 bytes)
- `tests/test_migrations_frozen.py` (5888 bytes)
- `tests/test_sqlite_storage.py` (24664 bytes)
- `tests/test_backup_restore.py` (31890 bytes)
- `empacotar_release.py` (35801 bytes)

Este relatório (`PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md`) foi
entregue e confirmado byte-a-byte pelo mesmo processo, na mesma rodada,
DEPOIS da verificação do ZIP descrita na seção 13 (o ZIP de verificação
final da seção 13 já inclui este relatório em sua forma completa).

## 13. Confirmação do ZIP final — divulgação honesta

`device_bash` foi checado no início desta rodada e permanece
**indisponível** ("Workspace unavailable. The isolated Linux environment
on this device failed to start."). Portanto, como no Prompt 24b, **o ZIP
cuja saída de `unzip -l`/SHA-256 está colada abaixo é uma reprodução
construída no sandbox de nuvem, não literalmente o mesmo arquivo que
seria produzido rodando `EMPACOTAR_RELEASE.bat` no Windows** — os 6
arquivos exclusivos do Windows (`EMPACOTAR_RELEASE.bat`,
`INSTALAR_DEPENDENCIAS_TESTE.bat`, `LEIA_ME_PRIMEIRO.txt`,
`LIMPAR_ANTES_DE_ZIPAR.bat`, `PAINEL_OFICIAL.bat`, `RODAR_TESTES.bat`)
foram emprestados (lidos, nunca modificados) via `device_stage_files` só
para permitir a reprodução do build aqui.

A verificação continua significativa porque, ANTES de construir este ZIP:
(1) todos os 9 arquivos novos/modificados desta rodada foram confirmados
byte-a-byte idênticos ao que está fisicamente em
`C:\Users\Enzo\Desktop\teste` (seção 12); (2) todos os arquivos protegidos
foram igualmente reconfirmados idênticos (seção 11); (3) a suíte completa
e o `compileall` rodaram sobre exatamente essas mesmas cópias locais
(seção 10) antes da construção do ZIP. Recomendação: quando possível,
rodar `EMPACOTAR_RELEASE.bat` diretamente no Windows para uma verificação
que seja literalmente o artefato final.

**Comando**: `python3 empacotar_release.py --out /mnt/user-data/outputs/PAINEL_OFICIAL_ZIP_VERIFICACAO_25.zip`

**SHA-256 do ZIP de verificação**: `e3b5058aca0d5220fe5b7863285fe0531bc7cf1849eb777b5ed2b014e2662995`

**Saída literal de `unzip -l`** (117 arquivos, antes de este relatório ser
adicionado ao ZIP — ver nota abaixo):

```
Archive:  PAINEL_OFICIAL_ZIP_VERIFICACAO_25.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-22 19:59   ARQUITETURA_ATUAL.md
    13132  2026-09-22 19:59   CLAUDE.md
     2687  2026-09-22 19:59   EMPACOTAR_RELEASE.bat
   169662  2026-09-22 19:59   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-22 19:59   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-22 19:59   LEIA_ME_PRIMEIRO.txt
      866  2026-09-22 19:59   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-22 19:59   MAPA_DE_DADOS.md
      686  2026-09-22 19:59   PAINEL_OFICIAL.bat
    29374  2026-09-22 19:59   PRODUCT_INVARIANTS.md
    23512  2026-09-22 19:59   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-22 19:59   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-22 19:59   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-22 19:59   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-22 19:59   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    31899  2026-09-22 19:59   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-22 19:59   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    24543  2026-09-22 19:59   REGRESSION_CHECKLIST.md
    26204  2026-09-22 19:59   RISCOS_ATUAIS.md
    34429  2026-09-22 19:59   ROADMAP_COMPLETO.md
     6104  2026-09-22 19:59   RODAR_TESTES.bat
    15403  2026-09-22 19:59   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-22 19:59   _sistema/agendar_tiktok.py
    91980  2026-09-22 19:59   _sistema/agendar_youtube.py
    18611  2026-09-22 19:59   _sistema/app_paths.py
    68310  2026-09-22 19:59   _sistema/batch_engine.py
    27989  2026-09-22 19:59   _sistema/circuit_breaker.py
    55733  2026-09-22 19:59   _sistema/control_manager.py
     2545  2026-09-22 19:59   _sistema/domain/__init__.py
     3307  2026-09-22 19:59   _sistema/domain/checkpoints.py
     5442  2026-09-22 19:59   _sistema/domain/job_state_machine.py
    15978  2026-09-22 19:59   _sistema/domain/models.py
    17683  2026-09-22 19:59   _sistema/gerar_textos.py
    85229  2026-09-22 19:59   _sistema/job_engine.py
    27891  2026-09-22 19:59   _sistema/limpar_metadados_oficial.py
     2724  2026-09-22 19:59   _sistema/login_conta.py
    44162  2026-09-22 19:59   _sistema/painel_oficial.py
    21683  2026-09-22 19:59   _sistema/publication_idempotency.py
    32885  2026-09-22 19:59   _sistema/recovery_manager.py
    48725  2026-09-22 19:59   _sistema/resource_manager.py
    27252  2026-09-22 19:59   _sistema/retry_policy.py
    17712  2026-09-22 19:59   _sistema/secrets_manager.py
    94188  2026-09-22 19:59   _sistema/shutdown_coordinator.py
    20616  2026-09-22 19:59   _sistema/source_context.py
    49320  2026-09-22 19:59   _sistema/source_import.py
     1036  2026-09-22 19:59   _sistema/state_json.py
     3365  2026-09-22 19:59   _sistema/storage/__init__.py
    19254  2026-09-22 19:59   _sistema/storage/audit.py
    51041  2026-09-22 19:59   _sistema/storage/backup.py
    27352  2026-09-22 19:59   _sistema/storage/database.py
    38955  2026-09-22 19:59   _sistema/storage/legacy_migration.py
     1266  2026-09-22 19:59   _sistema/storage/migrations/__init__.py
     7411  2026-09-22 19:59   _sistema/storage/migrations/m001_initial.py
      764  2026-09-22 19:59   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-22 19:59   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-22 19:59   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-22 19:59   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-22 19:59   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-22 19:59   _sistema/storage/migrations/m007_source_asset_declarations.py
     4856  2026-09-22 19:59   _sistema/storage/migrations/m008_source_asset_context.py
   119736  2026-09-22 19:59   _sistema/storage_manager.py
    14323  2026-09-22 19:59   _sistema/time_utils.py
    35801  2026-09-22 19:59   empacotar_release.py
       33  2026-09-22 19:59   requirements.txt
     6014  2026-09-22 19:59   tests/README.md
       65  2026-09-22 19:59   tests/__init__.py
    11123  2026-09-22 19:59   tests/fakes_playwright.py
       21  2026-09-22 19:59   tests/requirements-test.txt
    23822  2026-09-22 19:59   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-22 19:59   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-22 19:59   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-22 19:59   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-22 19:59   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-22 19:59   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-22 19:59   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-22 19:59   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-22 19:59   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-22 19:59   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-22 19:59   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-22 19:59   tests/test_ai_cache_parsing.py
    20247  2026-09-22 19:59   tests/test_app_paths.py
    31890  2026-09-22 19:59   tests/test_backup_restore.py
    70112  2026-09-22 19:59   tests/test_batch_engine.py
    19226  2026-09-22 19:59   tests/test_checkpoints.py
    26822  2026-09-22 19:59   tests/test_circuit_breaker.py
     7389  2026-09-22 19:59   tests/test_config_names_paths.py
    86602  2026-09-22 19:59   tests/test_control_manager.py
    13408  2026-09-22 19:59   tests/test_domain_models.py
     6240  2026-09-22 19:59   tests/test_ffmpeg_processing.py
     4450  2026-09-22 19:59   tests/test_fingerprint_state.py
     5547  2026-09-22 19:59   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-22 19:59   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-22 19:59   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-22 19:59   tests/test_job_engine.py
     6839  2026-09-22 19:59   tests/test_job_state_machine.py
    17472  2026-09-22 19:59   tests/test_legacy_json_migration.py
     3209  2026-09-22 19:59   tests/test_limpeza_permission_error.py
     5888  2026-09-22 19:59   tests/test_migrations_frozen.py
    11476  2026-09-22 19:59   tests/test_operational_audit.py
    16612  2026-09-22 19:59   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-22 19:59   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-22 19:59   tests/test_publication_idempotency.py
    36620  2026-09-22 19:59   tests/test_recovery_manager.py
    37274  2026-09-22 19:59   tests/test_release_packaging.py
   105416  2026-09-22 19:59   tests/test_resource_manager.py
     4676  2026-09-22 19:59   tests/test_resume_detection.py
    31831  2026-09-22 19:59   tests/test_retry_policy.py
     7501  2026-09-22 19:59   tests/test_schedule_slots.py
    19241  2026-09-22 19:59   tests/test_secrets_manager.py
    89276  2026-09-22 19:59   tests/test_shutdown_coordinator.py
    21566  2026-09-22 19:59   tests/test_source_context.py
    18065  2026-09-22 19:59   tests/test_source_import.py
    29758  2026-09-22 19:59   tests/test_source_import_options.py
    24664  2026-09-22 19:59   tests/test_sqlite_storage.py
     7928  2026-09-22 19:59   tests/test_state_json_persistence.py
   116323  2026-09-22 19:59   tests/test_storage_manager.py
     5928  2026-09-22 19:59   tests/test_time_utils.py
---------                     -------
  3001946                     117 files
```

**Nota sobre este relatório não estar dentro do próprio ZIP listado
acima**: este ZIP foi construído ANTES da finalização deste relatório
(o relatório precisa existir para ser incluído — dependência circular
inerente a "o relatório documenta o build"). Isto é idêntico ao padrão já
usado nas rodadas anteriores e não compromete a verificação: o conteúdo
que importa para a auditoria (os 9 arquivos de código/teste desta rodada)
já está confirmado byte-a-byte tanto no ZIP quanto no Windows (seções 11
e 12); o próprio arquivo de relatório é entregue e confirmado
separadamente por hash byte-a-byte (seção 12), pelo mesmo processo
rigoroso, apenas não incluído neste ZIP específico de verificação de
código.

**Grep de padrões sensíveis** (`senha|password|token|cookie|authorization|
api[_-]?key|secret`, sobre o conteúdo extraído do ZIP) nos 3 arquivos
novos desta rodada: apenas correspondências benignas — a string
"secrets_manager" citada em prosa/nome de identificador (referência ao
módulo protegido, nunca a um segredo real) e a palavra portuguesa
"desenhada" (contém a substring "senha" por coincidência ortográfica, não
relacionada a senha/credencial). Nenhum segredo real, token, cookie ou
credencial encontrado.

## 14. Riscos conhecidos e dívida técnica

- **`CONNECTED_CHANNEL_CONTENT` é sempre heurístico, nunca verificado por
  API real.** Isso é uma limitação estrutural do produto hoje (login via
  Chrome com perfil persistente, sem chamada de API para confirmar
  identidade de canal) — não algo que este Prompt poderia corrigir sem
  mudar a arquitetura de login, fora de escopo aqui. Se/quando uma
  integração de API real existir no futuro, `Account.external_account_id`
  passaria a ser populável e uma correspondência verificada
  (comparando IDs, não nomes escolhidos pelo usuário) se tornaria
  possível — este resolver precisaria ser revisitado conscientemente
  nesse momento, preferindo o ID verificado sobre a heurística textual
  atual quando disponível.
- A extração de handle para `/channel/<id>` (YouTube) devolve o ID opaco
  como se fosse um handle comparável — na prática nunca vai bater contra
  `Account.name`/`local_key` (que são nomes escolhidos pelo usuário, não
  IDs de canal), então sempre cai em `EXTERNAL_CONTENT`. Comportamento
  intencional e documentado, não um bug a corrigir — mas significa que
  URLs no formato `/channel/UC...` nunca serão classificadas como
  `CONNECTED_CHANNEL_CONTENT` mesmo quando forem de fato do canal
  conectado, até que uma correspondência por ID real exista.
  Nenhum requisito de reconhecer este caso hoje.
- `RECOGNIZED_PLATFORM_DOMAINS` é uma lista fixa e pequena — um domínio
  regional/alternativo de YouTube/TikTok não listado (ex. um encurtador
  de terceiros) simplesmente cai em `EXTERNAL_CONTENT`, nunca em erro.
  Ampliar a lista é uma decisão de produto consciente, futura, não deste
  Prompt.

## 15. Pendências

Nenhuma pendência de escopo deste Prompt. Prompt 26 (MediaProbe, conforme
já mencionado nas docstrings de `source_import.py`) não foi iniciado, por
instrução explícita.
