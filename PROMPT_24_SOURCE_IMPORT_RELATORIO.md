# PROMPT 24 — SOURCE IMPORT MANAGER — Relatório de Entrega

## 1. Arquivos criados

- `_sistema/source_import.py` — `SourceImportManager`, `LocalFileImporter`,
  `FolderImporter`, `UrlImporter`, `ImportResult`, `SourceImportError`,
  `compute_fingerprint`, `VIDEO_EXTS`, constantes `ERROR_*`.
- `tests/test_source_import.py` — 31 testes.
- `PROMPT_24_SOURCE_IMPORT_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `_sistema/app_paths.py` — novo campo `AppPaths.sources` (diretório
  mutável gerenciado, adicionado a `mutable_dirs()` e `build_app_paths()`
  com a mesma disciplina dos demais diretórios do produto).
- `empacotar_release.py` — este relatório adicionado a `ALLOWED_ROOT_FILES`.

Nenhum outro arquivo foi modificado.

## 3. Investigação da seção 0 (confirmada com o código real)

- **`SourceAsset`/`Video` já existiam**: confirmado lendo
  `_sistema/domain/models.py` (`SourceAsset`: `source_uri`, `local_path`,
  `original_name`, `fingerprint`, `media_type` default `"video"`,
  `size_bytes`; `Video.source_asset_id` referenciando `sources(id)`) e
  `m001_initial.py` (tabela `sources`, sem nenhuma restrição de
  unicidade). Nenhum campo foi alterado — este Prompt só constrói os
  importadores que os populam.
- **Geração 1 (`agendar_youtube.py`)**: confirmado `VIDEO_EXTS = {".mp4",
  ".mov", ".m4v", ".webm", ".avi", ".mkv"}` e `fingerprint(path)` =
  SHA-256 de tamanho + primeiro 1 MiB + último 1 MiB. Reproduzido de
  forma independente em `source_import.py` (nunca importado — zero
  cross-import) como referência de projeto já validada em produção; ver
  decisão de fingerprint abaixo.
- **`AppPaths` não tinha diretório dedicado a mídia importada**:
  confirmado lendo a lista completa de campos — `database`, `accounts`,
  `projects`, `cache`, `logs`, `temp`, `templates`, `backups`, `models`,
  `support`, `removed_accounts`, `migration_state`,
  `legacy_accounts`/`legacy_removed_accounts`. Nenhum servia como destino
  natural para conteúdo baixado por `UrlImporter`. Adicionado `sources`
  (novo campo, `mutable / "sources"`), pela mesma disciplina de
  `AppPaths`/migrations já usada no projeto (nenhum caminho hardcoded em
  outro lugar).

## 4. Comportamento novo

Três importadores independentes, cada um implementando `import_source(...)`
e devolvendo sempre `ImportResult` (o mesmo formato de resultado nos
três — `LocalFileImporter`/`UrlImporter` devolvem um único `ImportResult`,
`FolderImporter` devolve `list[ImportResult]`, um por arquivo
descoberto):

- **`LocalFileImporter`**: recebe o caminho de um arquivo já no disco do
  usuário, valida em nível de arquivo (existe, é arquivo regular,
  extensão permitida, tamanho > 0, legível), calcula o fingerprint,
  constrói e persiste um `SourceAsset` REFERENCIANDO o arquivo original
  (`local_path` = caminho original resolvido, nunca copiado).
- **`FolderImporter`**: descobre arquivos elegíveis em uma pasta (mesmo
  critério de extensão, não desce em subpastas) e delega cada um a um
  `LocalFileImporter` interno — um arquivo problemático nunca interrompe
  os demais (ver item 1.6 abaixo).
- **`UrlImporter`**: baixa o conteúdo de uma URL http(s) via GET simples
  e persiste o resultado em `AppPaths.sources` (obrigatório: não existe
  "arquivo original local" para uma URL).

`SourceImportManager` é a fachada: `import_local_file`, `import_folder`,
`import_url`, cada um delegando ao importador correspondente.

## 5. Decisões arquiteturais

### 5.1 Fingerprint: amostra parcial (tamanho + primeiro/último 1 MiB), não o arquivo inteiro

Reproduz exatamente a estratégia já usada e validada em produção por
Geração 1 (`agendar_youtube.py`, função `fingerprint()`), reimplementada
de forma independente (nunca importada). Justificativa do trade-off:
hashear um vídeo de vários GB por inteiro, a CADA importação — inclusive
`FolderImporter` processando centenas de arquivos de uma vez — custaria
I/O proporcional ao tamanho total da biblioteca do usuário toda vez que
uma pasta for reimportada/verificada. O fingerprint aqui é um sinal de
IDENTIDADE/DEDUP informal (base para dedup em rodadas futuras, não
implementado nesta), nunca uma garantia criptográfica de integridade —
não há verificação de assinatura nem detecção de adulteração byte-a-byte
dependendo deste valor. O trade-off aceito: dois arquivos diferentes com
o mesmo tamanho E os mesmos primeiro/último MiB colidiriam — teoricamente
possível, praticamente irrelevante para arquivos de vídeo reais, e já
aceito pela Geração 1 em produção. Testado: determinístico (mesmo arquivo
→ mesmo valor, em duas chamadas e em duas importações), e sensível a
diferenças reais de conteúdo (arquivos diferentes → valores diferentes).

### 5.2 Referenciar vs. copiar, por importador

- **`LocalFileImporter`/`FolderImporter`**: REFERENCIAM (não copiam).
  `local_path` aponta para o caminho original resolvido. Justificativa:
  vídeos podem ser arquivos enormes (GB) já armazenados pelo usuário —
  copiar duplicaria esse espaço em disco por padrão, sem que o usuário
  tenha pedido isso. O trade-off aceito: se o usuário mover ou apagar o
  arquivo original depois, a referência quebra — validar que o arquivo
  referenciado ainda existe e é válido no momento de USO (ex.: antes de
  processar um Job que o consome) é responsabilidade de uma camada
  futura, fora do escopo de um importador (cuja validação é só de
  nível-arquivo, no momento da importação).
- **`UrlImporter`**: copia OBRIGATORIAMENTE — não existe "arquivo
  original local" para uma URL. O conteúdo baixado é persistido em
  `AppPaths.sources/<uuid>_<nome-original>` (prefixo UUID para nunca
  colidir entre downloads com o mesmo nome de arquivo).

### 5.3 Nenhum `Job`/`Publication`/`Schedule`/`Project` criado; `Video` também NÃO criado

Os três importadores terminam exatamente em "devolver um `SourceAsset`
válido persistido" — nenhum cria `Job`, `Publication`, `Schedule` ou
`Project`. Confirmado por inspeção do código-fonte (teste
`test_source_import_nunca_referencia_job_publication_ou_schedule_no_codigo`,
que busca literalmente `"Job("`, `"Publication("`, `"Schedule("`,
`"Project("` no arquivo) e por teste de comportamento (importar um
arquivo local ou uma URL e confirmar que `database.list(Job)`,
`database.list(Publication)`, `database.list(Schedule)` e
`database.list(Video)` continuam vazios).

**`Video` deliberadamente também não é criado**, apesar de
`Video.source_asset_id` existir para isso e o Prompt permitir essa
decisão. Justificativa: a seção 0 do Prompt enquadra o trabalho deste
módulo como "popular `SourceAsset`"; criar também um `Video` por
importação levantaria perguntas de ciclo de vida fora de escopo desta
rodada — o que acontece ao reimportar o mesmo arquivo (novo `Video` a
cada vez? nunca mais de um por `SourceAsset`?), qual `status` inicial
faz sentido, se `Video.name` deve vir de `original_name` ou de outro
lugar. Manter o importador restrito a `SourceAsset` é o comportamento
mínimo e reversível: um Prompt futuro que definir essas regras de ciclo
de vida pode criar o `Video` de forma consciente, sem que este módulo
precise ser revisitado ou desfeito.

### 5.4 `UrlImporter` — limite rígido, garantia estrutural

`urllib.request.urlopen(url, timeout=...)` é chamado com a URL como
STRING pura — nunca um objeto `Request` (que permitiria anexar headers).
Nenhum `http.cookiejar`, nenhuma sessão, nenhum header `Authorization`/
`Cookie` em lugar nenhum do código. Confirmado por três testes AST
dedicados: (a) nenhuma chamada a `Request(...)` em lugar nenhum do
módulo; (b) nenhuma referência (import, atributo ou nome) a
`cookiejar`/`cookie`/`session`/`authorization`/`auth_header`; (c) nenhuma
chamada em todo o módulo passa `headers=` como argumento nomeado. Isso é
estrutural — não é "não foi testado", é "não existe caminho de código
para isso". Escopo do scheme é validado explicitamente (só `http`/
`https` são aceitos — uma URL `file://` é rejeitada com
`ERROR_URL_INVALIDA`, testado, evitando que `urlopen` seja usado
acidentalmente para ler um arquivo local arbitrário do disco).

Se a URL não servir o conteúdo diretamente com um GET simples (precisa de
login, JS, redireciona para uma página HTML em vez do arquivo, etc.), o
resultado é sempre uma falha estruturada (`ERROR_RESPOSTA_HTTP_INVALIDA`/
`ERROR_DOWNLOAD_FALHOU`/`ERROR_CONTEUDO_VAZIO`/
`ERROR_NOME_ARQUIVO_NAO_RECONHECIVEL`) — nunca um problema a contornar.
Nenhum extrator de site de terceiros, nenhum bypass de DRM/paywall foi
implementado.

### 5.5 Isolamento por item em `FolderImporter` (item 1.6)

Cada arquivo descoberto é processado independentemente via um
`LocalFileImporter` interno; um arquivo problemático (extensão inválida,
vazio, ilegível) produz um `ImportResult(success=False, ...)` sem
interromper os demais. `FolderImporter` também captura `OSError` por
item (falha de I/O inesperada ao processar um arquivo específico,
ex.: permissão negada) — mesmo princípio de isolamento já usado em toda a
Geração 2 (Prompt 20, `JobEngine.run_pending()`).

**Decisão consciente**: uma falha de banco de dados (`sqlite3.Error`) NÃO
é capturada por item dentro do loop — é sistêmica (o mesmo banco é usado
para todos os arquivos da pasta), então se falhar uma vez provavelmente
falharia para todos os demais também; mascará-la produziria um lote
inteiro de resultados "falhou" enganosos em vez de sinalizar claramente
um problema de infraestrutura que precisa de atenção imediata. A única
falha de nível-PASTA que propaga (em vez de virar um `ImportResult`) é a
pasta não existir/não ser um diretório — não há nada para iterar nesse
caso.

### 5.6 Validação só de nível-arquivo (item 1.5)

Confirmado: existe, é arquivo regular, extensão permitida, tamanho > 0,
legível. Nenhum `ffprobe`/equivalente foi usado ou importado — validação
profunda de mídia (codec, duração, corrupção, FPS, resolução) é
explicitamente o Prompt 26 (MediaProbe), fora de escopo aqui. Um arquivo
que passa nesta validação superficial mas está corrompido por dentro
ainda é aceito nesta camada, como o Prompt pede explicitamente.

### 5.7 Nenhum fluxo de confirmação de propriedade

Nenhum importador pergunta ou registra "o usuário confirma que tem
direitos sobre isto" — confirmado por leitura do código (não há nenhum
prompt interativo, nenhuma flag de confirmação persistida). Consistente
com o texto do roadmap: uma eventual tela de termos geral, se existir, é
responsabilidade de outra camada, fora deste módulo.

## 6. Confirmações explícitas exigidas pelo Prompt

- **`UrlImporter` não tem nenhum caminho de código para
  autenticação/cookies/bypass**: confirmado pelos três testes AST da
  seção 5.4.
- **`circuit_breaker.py`/`retry_policy.py`/`publication_idempotency.py`/
  `secrets_manager.py`/`domain/job_state_machine.py`/Geração 1 não foram
  tocados**: confirmado por hash SHA-256 (idêntico aos registros
  anteriores) e por teste automatizado (AST,
  `test_source_import_nunca_importa_modulos_ortogonais`) que garante que
  `source_import.py` nunca importa nenhum desses módulos.
- **Nenhum `Job`/`Publication`/`Schedule` foi criado por qualquer
  importador**: confirmado na seção 5.3.

## 7. Testes automatizados

`tests/test_source_import.py`: **31 testes**, cobrindo:

1. Consistência de formato entre os três importadores (`ImportResult`
   em todos os casos).
2. `LocalFileImporter`: arquivo válido → `SourceAsset` completo e
   persistido; arquivo inexistente/vazio/extensão inválida → falha
   estruturada, nunca exceção crua.
3. `FolderImporter`: pasta com válidos + inválidos misturados → válidos
   importados, inválidos reportados individualmente, sem interromper os
   demais; pasta vazia → lista vazia; pasta inexistente → falha
   estruturada; subpastas ignoradas.
4. `UrlImporter`: contra servidor HTTP local efêmero (`http.server`,
   porta 0) — download correto com `fingerprint`/`size_bytes` corretos;
   404 → falha estruturada; conteúdo vazio → falha estruturada; esquema
   não-http(s) rejeitado; URL sem extensão reconhecível rejeitada; nenhum
   arquivo parcial sobra após falha.
5. Garantias estruturais de `UrlImporter` (nunca `Request`/headers/
   cookies/sessão) — três testes AST.
6. Fingerprint determinístico (mesmo arquivo, duas chamadas e duas
   importações → mesmo valor; conteúdo diferente → valor diferente).
7. Isolamento de módulos ortogonais e ausência de
   `Job`/`Publication`/`Schedule`/`Video` — AST + comportamento real.
8. Restart com novas instâncias (`LocalDatabase`/`SourceImportManager`).
9. `AppPaths.sources` existe e está em `mutable_dirs()`.

Suíte completa: **1369 passed, 1 skipped, 36 subtests passed** (1338
anteriores + 31 novos), 0 falhas.

`python3 -m compileall -q _sistema tests`: **OK**.

## 8. Confirmação do ZIP final (saída literal de `unzip -l`)

`device_bash` foi checado antes de gerar o ZIP nesta rodada e continuou
indisponível ("Workspace unavailable. The isolated Linux environment on
this device failed to start."), exatamente como na correção do Prompt 23.
O ZIP foi então reproduzido fielmente no ambiente de auditoria (cloud
sandbox), com o MESMO `empacotar_release.py` e as MESMAS árvores
`_sistema`/`tests` já entregues e verificadas byte-a-byte no Windows: os
6 arquivos de raiz Windows-only (`EMPACOTAR_RELEASE.bat`,
`INSTALAR_DEPENDENCIAS_TESTE.bat`, `LEIA_ME_PRIMEIRO.txt`,
`LIMPAR_ANTES_DE_ZIPAR.bat`, `PAINEL_OFICIAL.bat`, `RODAR_TESTES.bat`)
foram lidos (somente leitura) via `device_stage_files` do Windows,
copiados temporariamente para o sandbox só para permitir que
`empacotar_release.py` complete a validação de `REQUIRED_ROOT_FILES`, e
removidos do sandbox logo em seguida (nunca modificados, nunca
persistidos ali).

Antes de gerar o ZIP: suíte completa executada no sandbox
(**1369 passed, 1 skipped, 36 subtests passed**) e `compileall` OK — ambos
idênticos aos números já reportados na seção 7, confirmando que o sandbox
está no mesmo estado dos arquivos entregues.

Comando executado: `python3 empacotar_release.py --out
/mnt/user-data/outputs/PAINEL_OFICIAL_ZIP_VERIFICACAO.zip`

Resultado do empacotador (build concluído com sucesso; as linhas
"dentro de arvore permitida, mas fora do manifesto de
extensoes/arquivos" são apenas a listagem informativa de `__pycache__`
sendo corretamente EXCLUÍDO do ZIP, não um erro):

```
[2/3] ZIP gerado atomicamente, com identidade revalidada por arquivo, e ja inspecionado.

[3/3] Calculando SHA-256 do ZIP final...
      SHA-256: 7def05915849399ecc7cce9eb94066ead4d05fda04903a38275b9e26e0818334
      Tamanho: 0.77 MB

======================================================================
EMPACOTAMENTO CONCLUIDO COM SUCESSO
Arquivo: PAINEL_OFICIAL_ZIP_VERIFICACAO.zip
SHA-256: 7def05915849399ecc7cce9eb94066ead4d05fda04903a38275b9e26e0818334
======================================================================
```

Saída LITERAL e COMPLETA de `unzip -l` sobre o ZIP recém-gerado (111
arquivos):

```
Archive:  /mnt/user-data/outputs/PAINEL_OFICIAL_ZIP_VERIFICACAO.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-22 18:49   ARQUITETURA_ATUAL.md
    13132  2026-09-22 18:49   CLAUDE.md
     2687  2026-09-22 18:49   EMPACOTAR_RELEASE.bat
   169662  2026-09-22 18:49   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-22 18:49   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-22 18:49   LEIA_ME_PRIMEIRO.txt
      866  2026-09-22 18:49   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-22 18:49   MAPA_DE_DADOS.md
      686  2026-09-22 18:49   PAINEL_OFICIAL.bat
    29374  2026-09-22 18:49   PRODUCT_INVARIANTS.md
    23512  2026-09-22 18:49   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-22 18:49   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-22 18:49   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-22 18:49   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-22 18:49   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    15364  2026-09-22 18:49   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    24543  2026-09-22 18:49   REGRESSION_CHECKLIST.md
    26204  2026-09-22 18:49   RISCOS_ATUAIS.md
    34429  2026-09-22 18:49   ROADMAP_COMPLETO.md
     6104  2026-09-22 18:49   RODAR_TESTES.bat
    15403  2026-09-22 18:49   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-22 18:49   _sistema/agendar_tiktok.py
    91980  2026-09-22 18:49   _sistema/agendar_youtube.py
    18611  2026-09-22 18:49   _sistema/app_paths.py
    68310  2026-09-22 18:49   _sistema/batch_engine.py
    27989  2026-09-22 18:49   _sistema/circuit_breaker.py
    55733  2026-09-22 18:49   _sistema/control_manager.py
     2545  2026-09-22 18:49   _sistema/domain/__init__.py
     3307  2026-09-22 18:49   _sistema/domain/checkpoints.py
     5442  2026-09-22 18:49   _sistema/domain/job_state_machine.py
    15978  2026-09-22 18:49   _sistema/domain/models.py
    17683  2026-09-22 18:49   _sistema/gerar_textos.py
    85229  2026-09-22 18:49   _sistema/job_engine.py
    27891  2026-09-22 18:49   _sistema/limpar_metadados_oficial.py
     2724  2026-09-22 18:49   _sistema/login_conta.py
    44162  2026-09-22 18:49   _sistema/painel_oficial.py
    21683  2026-09-22 18:49   _sistema/publication_idempotency.py
    32885  2026-09-22 18:49   _sistema/recovery_manager.py
    48725  2026-09-22 18:49   _sistema/resource_manager.py
    27252  2026-09-22 18:49   _sistema/retry_policy.py
    17712  2026-09-22 18:49   _sistema/secrets_manager.py
    94188  2026-09-22 18:49   _sistema/shutdown_coordinator.py
    18618  2026-09-22 18:49   _sistema/source_import.py
     1036  2026-09-22 18:49   _sistema/state_json.py
     3365  2026-09-22 18:49   _sistema/storage/__init__.py
    19254  2026-09-22 18:49   _sistema/storage/audit.py
    51041  2026-09-22 18:49   _sistema/storage/backup.py
    27352  2026-09-22 18:49   _sistema/storage/database.py
    38955  2026-09-22 18:49   _sistema/storage/legacy_migration.py
     1071  2026-09-22 18:49   _sistema/storage/migrations/__init__.py
     7411  2026-09-22 18:49   _sistema/storage/migrations/m001_initial.py
      764  2026-09-22 18:49   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-22 18:49   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-22 18:49   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-22 18:49   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-22 18:49   _sistema/storage/migrations/m006_publication_idempotency.py
   119736  2026-09-22 18:49   _sistema/storage_manager.py
    14323  2026-09-22 18:49   _sistema/time_utils.py
    35701  2026-09-22 18:49   empacotar_release.py
       33  2026-09-22 18:49   requirements.txt
     6014  2026-09-22 18:49   tests/README.md
       65  2026-09-22 18:49   tests/__init__.py
    11123  2026-09-22 18:49   tests/fakes_playwright.py
       21  2026-09-22 18:49   tests/requirements-test.txt
    23822  2026-09-22 18:49   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-22 18:49   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-22 18:49   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-22 18:49   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-22 18:49   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-22 18:49   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-22 18:49   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-22 18:49   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-22 18:49   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-22 18:49   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-22 18:49   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-22 18:49   tests/test_ai_cache_parsing.py
    20247  2026-09-22 18:49   tests/test_app_paths.py
    31890  2026-09-22 18:49   tests/test_backup_restore.py
    70112  2026-09-22 18:49   tests/test_batch_engine.py
    19226  2026-09-22 18:49   tests/test_checkpoints.py
    26822  2026-09-22 18:49   tests/test_circuit_breaker.py
     7389  2026-09-22 18:49   tests/test_config_names_paths.py
    86602  2026-09-22 18:49   tests/test_control_manager.py
    13408  2026-09-22 18:49   tests/test_domain_models.py
     6240  2026-09-22 18:49   tests/test_ffmpeg_processing.py
     4450  2026-09-22 18:49   tests/test_fingerprint_state.py
     5547  2026-09-22 18:49   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-22 18:49   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-22 18:49   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-22 18:49   tests/test_job_engine.py
     6839  2026-09-22 18:49   tests/test_job_state_machine.py
    17472  2026-09-22 18:49   tests/test_legacy_json_migration.py
     3209  2026-09-22 18:49   tests/test_limpeza_permission_error.py
     4485  2026-09-22 18:49   tests/test_migrations_frozen.py
    11476  2026-09-22 18:49   tests/test_operational_audit.py
    16612  2026-09-22 18:49   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-22 18:49   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-22 18:49   tests/test_publication_idempotency.py
    36620  2026-09-22 18:49   tests/test_recovery_manager.py
    37274  2026-09-22 18:49   tests/test_release_packaging.py
   105416  2026-09-22 18:49   tests/test_resource_manager.py
     4676  2026-09-22 18:49   tests/test_resume_detection.py
    31831  2026-09-22 18:49   tests/test_retry_policy.py
     7501  2026-09-22 18:49   tests/test_schedule_slots.py
    19241  2026-09-22 18:49   tests/test_secrets_manager.py
    89276  2026-09-22 18:49   tests/test_shutdown_coordinator.py
    18065  2026-09-22 18:49   tests/test_source_import.py
    24189  2026-09-22 18:49   tests/test_sqlite_storage.py
     7928  2026-09-22 18:49   tests/test_state_json_persistence.py
   116323  2026-09-22 18:49   tests/test_storage_manager.py
     5928  2026-09-22 18:49   tests/test_time_utils.py
---------                     -------
  2845669                     111 files
```

Confirmação via `grep` (arquivos deste Prompt presentes no ZIP, com
tamanhos idênticos aos entregues e verificados no Windows):

```
    15364  2026-09-22 18:49   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    18611  2026-09-22 18:49   _sistema/app_paths.py
    18618  2026-09-22 18:49   _sistema/source_import.py
    35701  2026-09-22 18:49   empacotar_release.py
    20247  2026-09-22 18:49   tests/test_app_paths.py
    18065  2026-09-22 18:49   tests/test_source_import.py
```

Confirmação adicional via `grep -i "secret\|token\|cookie\|credential\|\.env\b"`
sobre a listagem completa: apenas os três arquivos já aprovados e
exemplificados na correção do Prompt 23 aparecem —

```
    13751  2026-09-22 18:49   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    17712  2026-09-22 18:49   _sistema/secrets_manager.py
    19241  2026-09-22 18:49   tests/test_secrets_manager.py
```

— confirmando que nenhum arquivo novo deste Prompt colidiu com
`SENSITIVE_FILENAME_PATTERNS` e que a exceção nominal adicionada na
correção do Prompt 23 continua restrita exatamente aos três arquivos
originais (nenhuma expansão indevida).

## 9. Riscos conhecidos e dívida técnica

- **Dedup não implementado**: o fingerprint é determinístico e
  suficiente para dedup, mas nenhum importador consulta `fingerprint`s
  existentes antes de criar um novo `SourceAsset` — reimportar o mesmo
  arquivo hoje cria uma segunda linha em `sources`. Isso é consistente
  com o Prompt ("base para dedup em rodadas futuras, mesmo que este
  Prompt não implemente dedup automático") — não é um bug, é escopo
  explicitamente adiado.
- **Referências locais podem quebrar**: `LocalFileImporter`/
  `FolderImporter` não copiam o arquivo — se o usuário mover/apagar o
  arquivo original depois de importar, `local_path` aponta para um
  caminho que não existe mais. Nenhuma verificação de "o arquivo
  referenciado ainda existe" acontece automaticamente hoje; fica para
  quando uma camada futura for de fato USAR o `SourceAsset` (ex.: um Job
  de edição/processamento).
- **`UrlImporter` não tem limite de tamanho de download**: o conteúdo é
  gravado em disco em streaming (não carregado inteiro em memória), mas
  não há um teto de bytes/tempo além do timeout de conexão — uma URL que
  serve um arquivo extremamente grande consumiria disco proporcionalmente.
  Não implementado por não ter sido pedido explicitamente; um limite
  configurável seria uma extensão razoável de um Prompt futuro.
- **`Video` não é criado**: ver seção 5.3 — decisão consciente, mas
  significa que, sozinho, este Prompt não deixa o vídeo importado "pronto
  para uso" por uma camada de edição/Job — isso é trabalho de um Prompt
  futuro que definir o ciclo de vida `SourceAsset → Video`.

## 10. Pendências

Nenhuma pendência dentro do escopo autorizado deste Prompt. Não iniciado
Prompt 25, conforme instrução.
