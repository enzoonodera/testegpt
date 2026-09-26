# PROMPT 27 — MODELO DE PROJETO DE EDIÇÃO (não destrutivo) — Relatório de Entrega

## 1. Arquivos criados

- `_sistema/edit_project.py` (19528 bytes) — `EditProjectManager`, `CategoryState`, 9 constantes de categoria, 4 classes de erro estruturado.
- `tests/test_edit_project.py` (22810 bytes) — 34 testes automatizados.
- `PROMPT_27_EDIT_PROJECT_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `empacotar_release.py` — adicionada a entrada `"PROMPT_27_EDIT_PROJECT_RELATORIO.md"` em `ALLOWED_ROOT_FILES`, logo após a entrada do Prompt 26. Tamanho final: **35886 bytes**. Nenhuma outra linha alterada.

Nenhum outro arquivo do projeto foi modificado. **Nenhuma migration nova foi criada** — confirmado abaixo (seção 6).

## 3. Confirmação do contexto (seção 0 do Prompt)

Investigação confirmada antes de escrever qualquer código:

- `Project`/tabela `projects` já existem desde `m001_initial.py` (congelada). `domain/models.py` já define `Project(video_id, name, revision=1, edit_state={})`, e `storage/database.py` já registra `EntitySpec(Project, "projects", frozenset({"edit_state"}))` — a coluna `edit_state_json` já comporta qualquer JSON.
- `Project.revision` existia mas era campo morto — confirmado por busca no código-fonte anterior a este Prompt: nenhum incremento em lugar nenhum, apenas a validação `>= 1` no `__post_init__`.
- `edit_state` era um dict completamente livre, sem estrutura — os únicos dois usos existentes eram smoke tests genéricos em `tests/test_domain_models.py`/`tests/test_sqlite_storage.py`.

## 4. Comportamento novo

`EditProjectManager` estrutura `Project.edit_state` como:

```json
{
  "categories": {
    "cuts": {
      "data": {"in": 0, "out": 20},
      "fingerprint": "d5e437e5eccb53d117589ff9020ae244fc53728eb8ceb06c8c501a963063d7e1",
      "revision": 2,
      "updated_at": "2026-09-23T01:28:25+00:00"
    },
    "crop": {
      "data": {"x": 0, "y": 0, "w": 1080, "h": 1920},
      "fingerprint": "e193cd8b4d645327625975f49c63be71d514be0890de7ad6046a402a1944a787",
      "revision": 1,
      "updated_at": "2026-09-23T01:28:25+00:00"
    }
  }
}
```

(exemplo real, gerado durante o desenvolvimento — `fingerprint` é o SHA-256 hex do JSON canônico de `data` daquela categoria, nunca do `edit_state` inteiro.)

API pública de `EditProjectManager`:

- `set_category(project_id, category, data) -> CategoryState` — define/atualiza uma categoria inteira. Recalcula só o fingerprint/revisão daquela categoria.
- `remove_category(project_id, category) -> bool` — remove uma categoria; `True` se removeu, `False` se já não existia (idempotente, nunca lança para esse caso).
- `get_category(project_id, category) -> CategoryState | None` — lê uma categoria específica; `None` quando ausente (estado normal, não erro).
- `list_categories(project_id) -> tuple[str, ...]` — lista, em ordem alfabética, todas as categorias presentes.
- `aggregate_fingerprint(project_id) -> str` — fingerprint agregado do projeto inteiro, derivado deterministicamente dos fingerprints individuais.

As nove constantes literais do roadmap (`CUTS`, `SPEED`, `CROP`, `REFRAME`, `AUDIO_SETTINGS`, `CAPTIONS`, `TEMPLATE`, `TEXT_LAYERS`, `METADATA_MODE`) são apenas strings de conveniência — qualquer outra string não vazia funciona como nome de categoria (testado explicitamente com uma categoria fora dessa lista).

## 5. Decisões arquiteturais

### 5.1 Mecanismo de concorrência (seção 0.4)

**Risco real identificado pelo Prompt**: como `edit_state` é uma única coluna JSON por linha, um padrão ingênuo de "ler o Project inteiro fora de uma transaction → decidir em memória → escrever de volta em outra transaction" tem uma janela de *lost update* — duas chamadas concorrentes alterando categorias DIFERENTES, mas lendo o mesmo estado inicial, fariam a escrita mais tardia apagar silenciosamente a mudança da outra.

**Resolução escolhida**: toda escrita (`set_category`/`remove_category`) executa leitura + decisão + escrita dentro da MESMA transaction `BEGIN IMMEDIATE` (`LocalDatabase.transaction()`), a mesma técnica já usada em `ControlManager._cancel_or_defer_job`. `BEGIN IMMEDIATE` adquire o write lock do SQLite imediatamente ao abrir a transaction: uma segunda chamada concorrente bloqueia em sua PRÓPRIA abertura de transaction até a primeira commitar, e só então lê o estado — já atualizado pela primeira. Isso elimina a janela de corrida por construção, não por sorte de timing.

**Como foi testado de forma genuinamente adversarial**: `test_duas_threads_atualizando_categorias_diferentes_do_mesmo_project_nenhuma_perdida` usa `threading.Barrier(2)` para forçar duas threads reais (cada uma com sua própria instância de `LocalDatabase`/`EditProjectManager`, contra o mesmo arquivo SQLite) a tentar `set_category` em categorias diferentes do MESMO `Project` no mesmo instante — nunca um teste probabilístico. Um `read-modify-write` ingênuo (ler fora de transaction) FALHARIA esse teste de forma real, não hipotética, porque o Barrier garante a sobreposição exata da janela de corrida. Um segundo teste (`test_muitas_threads_categorias_distintas_nenhuma_perdida`) estende isso adversarialmente para 8 threads simultâneas, cada uma com sua própria categoria — todas as 8 sobrevivem.

### 5.2 Papel de `Project.revision` vs. revisão por categoria (seção 1.5)

`Project.revision` passa a ser um contador GROSSO — "algo mudou no projeto como um todo" — incrementado a cada mutação bem-sucedida de qualquer categoria (`set_category` que realmente alterou dados, ou `remove_category` que removeu algo). Serve como sinal de controle de concorrência otimista no nível do `Project` inteiro (ex.: uma UI futura detectando "este projeto mudou desde que você abriu a tela"), mas é estritamente SEPARADO do mecanismo fino de reaproveitamento: a decisão "este processamento anterior ainda é válido?" depende exclusivamente de `fingerprint`/`revision` POR CATEGORIA, nunca de `Project.revision` (um bump nele não diz QUAL categoria mudou). Testado explicitamente em `test_project_revision_avanca_a_cada_mutacao_real_mas_nao_em_no_op`.

### 5.3 Decisão sobre reenvio idempotente (mesmos dados, mesma categoria)

Quando `set_category` recebe dados byte-a-byte idênticos (mesmo fingerprint) aos já gravados para aquela categoria, a chamada é tratada como NO-OP determinístico: nem a revisão da categoria nem `Project.revision` avançam, e nenhuma escrita é feita no banco. Justificativa: evita inflar revisões por reenvios idempotentes do mesmo estado (ex.: uma UI que salva periodicamente mesmo sem mudança real), mantendo a revisão como um sinal fiel de mudança real — testado em `test_mesmos_dados_mesma_categoria_fingerprint_deterministico_revisao_nao_avanca` e `test_mesmos_dados_em_ordem_de_chaves_diferente_ainda_e_no_op` (confirmando que a ordem de construção do dict Python não afeta o fingerprint, por ser calculado sobre o JSON canônico com chaves ordenadas).

### 5.4 Erros estruturados via classes tipadas (não via dataclass `Result`)

Diferente de `media_probe.py`/`source_import.py` (que devolvem um dataclass `Result` com `error_code` para permitir isolamento por item em lote), `EditProjectManager` levanta exceções tipadas (`ProjectNaoEncontradoError`, `CategoriaInvalidaError`, `DadosNaoSerializaveisError`), cada uma com um atributo `.code` estável — mesmo padrão já usado por `ControlManagerError`/subclasses para operações de mutação de entidade única (não em lote). Justificativa da divergência: este módulo opera sobre UM projeto por chamada (não itera uma lista de arquivos onde uma falha isolada não deve derrubar as demais); uma exceção tipada com `.code` é "erro estruturado" (nunca uma exceção crua de `json.dumps`/`TypeError` genérica) e é o padrão já estabelecido no código para esse tipo de operação (ver `ControlManager`).

## 6. Migrations

**Nenhuma migration nova foi criada.** `LATEST_SCHEMA_VERSION` permanece em 8 (confirmado por teste: `test_edit_project_nao_cria_migration_nova`). A coluna `edit_state_json` já existente desde `m001_initial.py` comporta integralmente a estrutura JSON definida neste Prompt — não há necessidade de mudança de schema, exatamente como a seção 0.1 do Prompt antecipava.

## 7. Confirmação: nenhuma categoria hardcoded como lista fechada

As nove constantes (`CUTS`/`SPEED`/etc.) são apenas strings de conveniência exportadas por `__all__` — a validação (`_validate_category_name`) só rejeita string vazia/tipo não-string, nunca compara contra uma lista fixa/enum. Confirmado por teste funcional (`test_categoria_nao_prevista_tambem_e_aceita`, usando o nome `"watermark_customizada_futura"`, nunca citado no roadmap) — não apenas por inspeção de código.

## 8. Confirmações estruturais (testadas via AST + funcional)

- **Nenhum `Job`/`Artifact`/`Publication`/`Schedule`/`Video` criado** — `test_nenhum_job_artifact_publication_schedule_video_criado` (AST) + `test_nenhuma_outra_entidade_criada_no_banco` (funcional).
- **`SourceAsset` nunca é tocado** — `test_edit_project_nunca_referencia_source_asset_no_codigo` (AST) + `test_edit_project_so_toca_tabela_projects_nunca_sources` (funcional: insere um `SourceAsset`, opera livremente sobre um `Project`, confirma que a linha de `sources` permanece byte-a-byte idêntica).
- **Nenhuma chamada a `subprocess`/`ffmpeg`/`ffprobe`** — `test_edit_project_nunca_chama_subprocess_ffmpeg_ffprobe` (AST — inspeciona nós `ast.Call`/`ast.Import` reais, nunca grep textual, pelo motivo já documentado nos Prompts 24b/25/26: a própria docstring deste módulo referencia esses termos em prosa).
- **Nenhum import de Geração 1 nem dos módulos protegidos** (`circuit_breaker`, `retry_policy`, `publication_idempotency`, `secrets_manager`, `job_state_machine`, e adicionalmente `source_import`/`source_context`/`media_probe`, já que este módulo não depende deles) — `test_edit_project_nao_importa_modulos_protegidos` + `test_edit_project_nao_importa_nada_de_geracao_1`.
- **Geração 1 e todos os módulos protegidos (incluindo `source_import.py`, `source_context.py`, `media_probe.py`) permanecem intocados** — reconfirmado por comparação de tamanho em bytes idêntico ao round anterior (Prompt 26) ANTES de qualquer trabalho neste Prompt: `circuit_breaker.py` (27989), `retry_policy.py` (27252), `publication_idempotency.py` (21683), `secrets_manager.py` (17712), `domain/job_state_machine.py` (5442), `source_import.py` (49320), `source_context.py` (20616), `media_probe.py` (23350), `agendar_youtube.py` (91980), `agendar_tiktok.py` (97511), `limpar_metadados_oficial.py` (27891), `gerar_textos.py` (17683), `painel_oficial.py` (44162) — todos idênticos, confirmados também na saída de `device_list_dir` do diretório `_sistema/` na máquina Windows antes da entrega.

## 9. Testes automatizados executados

**34 testes novos em `tests/test_edit_project.py`, todos passando:**

```
test_nove_constantes_literais_do_roadmap
test_categoria_nao_prevista_tambem_e_aceita
test_definir_categoria_nova_calcula_fingerprint_e_revisao
test_restart_com_novas_instancias_preserva_categoria
test_atualizar_uma_categoria_nao_muda_fingerprint_revisao_de_outras
test_remover_categoria_some_da_listagem_outras_inalteradas
test_remover_categoria_inexistente_e_idempotente_nao_lanca
test_mesmos_dados_mesma_categoria_fingerprint_deterministico_revisao_nao_avanca
test_mesmos_dados_em_ordem_de_chaves_diferente_ainda_e_no_op
test_dados_nao_serializaveis_gera_erro_estruturado
test_dados_com_set_python_nao_serializavel_gera_erro_estruturado
test_nome_categoria_invalido_gera_erro_estruturado[6 variações: "", "   ", None, 123, [], {}]
test_project_inexistente_gera_erro_estruturado_em_todas_as_operacoes
test_construcao_rejeita_database_invalido
test_category_state_e_dataclass_imutavel
test_fingerprint_agregado_muda_quando_qualquer_categoria_muda
test_fingerprint_agregado_independente_da_ordem_de_insercao
test_fingerprint_agregado_projeto_sem_categorias_e_deterministico
test_project_revision_avanca_a_cada_mutacao_real_mas_nao_em_no_op
test_duas_threads_atualizando_categorias_diferentes_do_mesmo_project_nenhuma_perdida
test_muitas_threads_categorias_distintas_nenhuma_perdida
test_edit_project_nao_importa_modulos_protegidos
test_edit_project_nao_importa_nada_de_geracao_1
test_edit_project_nunca_chama_subprocess_ffmpeg_ffprobe
test_nenhum_job_artifact_publication_schedule_video_criado
test_edit_project_nunca_referencia_source_asset_no_codigo
test_edit_project_so_toca_tabela_projects_nunca_sources
test_nenhuma_outra_entidade_criada_no_banco
test_edit_project_nao_cria_migration_nova

34 passed in 0.50s
```

Destaques:
- **Garantia central provada diretamente** (`test_atualizar_uma_categoria_nao_muda_fingerprint_revisao_de_outras`): grava 3 categorias, atualiza só uma, e confirma bit-a-bit (`fingerprint`, `revision`, `updated_at`, `data`) que as outras duas permanecem idênticas ao estado anterior.
- **Concorrência real, não probabilística**: dois testes com `threading.Barrier`, um com 2 threads (o caso mínimo pedido pelo Prompt) e um adversarial com 8 threads simultâneas, cada instância usando seu próprio `LocalDatabase` contra o mesmo arquivo SQLite.
- **Restart (GATE 4)**: `test_restart_com_novas_instancias_preserva_categoria` reabre com NOVAS instâncias de `LocalDatabase`/`EditProjectManager`, nunca reaproveitando objetos em memória.
- **Fingerprint agregado**: determinístico independente da ordem de inserção das categorias (`test_fingerprint_agregado_independente_da_ordem_de_insercao` grava as mesmas 3 categorias em ordens diferentes em dois projetos distintos e confirma que o agregado final é idêntico), muda quando qualquer categoria muda, e é bem definido mesmo para um projeto sem nenhuma categoria.

**Suíte completa (Prompts 19–27, sem remover nem enfraquecer nenhum teste anterior):**

```
1499 passed, 1 skipped, 36 subtests passed in 102.53s (0:01:42)
```

(1465 da rodada anterior + 34 novos = 1499; o único teste pulado é pré-existente de rounds anteriores, não relacionado a este Prompt.)

**`compileall`:**

```
python3 -m compileall -q _sistema tests empacotar_release.py
compileall OK
```

## 10. Como testar manualmente

```python
from pathlib import Path
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.domain import Project
from _sistema.edit_project import EditProjectManager, CUTS, CROP

paths = build_app_paths(data_root=Path("C:/temp/painel_teste"))
ensure_app_directories(paths)
db = LocalDatabase(paths.database / "painel.db")
db.initialize()

project = Project(name="Meu projeto")
db.insert(project)

mgr = EditProjectManager(db)
mgr.set_category(project.id, CUTS, {"in": 0, "out": 10})
mgr.set_category(project.id, CROP, {"x": 0, "y": 0, "w": 1080, "h": 1920})

print(mgr.list_categories(project.id))          # ('crop', 'cuts')
print(mgr.get_category(project.id, CUTS))        # CategoryState(...)
print(mgr.aggregate_fingerprint(project.id))     # hash agregado determinístico
```

## 11. Riscos conhecidos e dívida técnica

- **Concorrência resolvida apenas para escritas via `EditProjectManager`**: se algum código FUTURO escrever `Project.edit_state` diretamente via `database.save(project)` fora deste módulo (ignorando a transaction `BEGIN IMMEDIATE`/o read-modify-write atômico), a garantia de não-perda de categoria deixa de valer. Não há, hoje, nenhum mecanismo que impeça outro módulo de escrever `edit_state` por fora — a garantia depende de todo escritor futuro passar por `EditProjectManager`. Isso é aceitável para o escopo atual (nenhum outro módulo escreve `Project.edit_state` hoje), mas deve ser revisitado se o Editor (Prompt 28+) introduzir um caminho de escrita alternativo.
- **Sem limite de tamanho por categoria/projeto**: `data` de uma categoria pode crescer sem limite (ex.: uma lista muito longa de cortes) — toda a coluna `edit_state_json` é reescrita a cada mutação de qualquer categoria (não há armazenamento incremental por categoria no SQLite, só na estrutura lógica em memória). Para os volumes de decisão de edição esperados (cortes, crop, legendas de um único vídeo) isso é aceitável; não é adequado para payloads muito grandes (ex.: uma transcrição completa) — esses devem viver em outra tabela/estrutura quando o Editor (Prompt 28+) precisar deles.
- **`aggregate_fingerprint` é calculado sob demanda, não armazenado**: cada chamada relê o `Project` e recalcula a partir das categorias atuais. Simples e sempre consistente (nunca pode ficar "dessincronizado"), mas custa uma leitura + N hashes a cada chamada — aceitável para o volume de categorias esperado (dezenas, não milhares) por projeto.
- Nenhum dos riscos acima compromete a garantia central testada (mudar uma categoria nunca invalida outra) nem a resolução do risco de corrida documentado na seção 0.4.

## 12. Pendências

Nenhuma pendência dentro do escopo deste Prompt. O schema interno de cada categoria (`cuts`/`crop`/`captions`/etc.) fica para os Prompts 28+ (Editor), que também definirão como o próprio Editor consome `EditProjectManager`. Prompt 27.5 e Prompt 28 **não** foram iniciados.

## 13. Verificação da entrega (ZIP)

`device_bash` permaneceu indisponível durante todo este round ("Workspace unavailable. The isolated Linux environment on this device failed to start."), verificado no início da fase de entrega. Por isso, o ZIP de verificação abaixo foi construído localmente no sandbox de nuvem, reproduzindo `empacotar_release.py` com os 6 arquivos raiz exclusivos do Windows (`EMPACOTAR_RELEASE.bat`, `INSTALAR_DEPENDENCIAS_TESTE.bat`, `LEIA_ME_PRIMEIRO.txt`, `LIMPAR_ANTES_DE_ZIPAR.bat`, `PAINEL_OFICIAL.bat`, `RODAR_TESTES.bat`) **emprestados temporariamente** (via `device_stage_files`, somente leitura) da máquina Windows real, copiados para a raiz do projeto no sandbox só para permitir a execução do empacotador, e **removidos do sandbox logo em seguida** (não fazem parte de nenhum artefato deste round além do ZIP de verificação abaixo).

Isso significa: este ZIP comprova que `empacotar_release.py` produz um pacote consistente com o conteúdo atualmente confirmado byte-a-byte na máquina Windows (`_sistema/edit_project.py`, `tests/test_edit_project.py`, `empacotar_release.py`, e todos os arquivos protegidos/anteriores), mas **não é** o mesmo processo de build que rodaria nativamente no Windows via `EMPACOTAR_RELEASE.bat` — divulgação honesta, mesmo padrão já usado nos relatórios dos Prompts 24b/25/26 enquanto `device_bash` estava indisponível.

**SHA-256 do ZIP gerado nesta rodada:**
```
6be04b31f871e07a29f4735421dcc5ad7dd08cd55d2804f9f8fcfc6daf3090fe
```
Arquivo: `PAINEL_OFICIAL_ZIP_VERIFICACAO_27.zip` (0.86 MB, 123 arquivos).

**Saída literal de `unzip -l` (arquivos deste round destacados: `_sistema/edit_project.py`, `tests/test_edit_project.py`, `empacotar_release.py`, `PROMPT_26_MEDIA_PROBE_RELATORIO.md` já presente da rodada anterior):**
```
Archive:  PAINEL_OFICIAL_ZIP_VERIFICACAO_27.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-22 22:32   ARQUITETURA_ATUAL.md
    13132  2026-09-22 22:32   CLAUDE.md
     2687  2026-09-22 22:32   EMPACOTAR_RELEASE.bat
   169662  2026-09-22 22:32   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-22 22:32   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-22 22:32   LEIA_ME_PRIMEIRO.txt
      866  2026-09-22 22:32   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-22 22:32   MAPA_DE_DADOS.md
      686  2026-09-22 22:32   PAINEL_OFICIAL.bat
    29374  2026-09-22 22:32   PRODUCT_INVARIANTS.md
    23512  2026-09-22 22:32   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-22 22:32   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-22 22:32   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-22 22:32   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-22 22:32   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    31899  2026-09-22 22:32   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-22 22:32   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    28360  2026-09-22 22:32   PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md
    29422  2026-09-22 22:32   PROMPT_26_MEDIA_PROBE_RELATORIO.md
    24543  2026-09-22 22:32   REGRESSION_CHECKLIST.md
    26204  2026-09-22 22:32   RISCOS_ATUAIS.md
    34429  2026-09-22 22:32   ROADMAP_COMPLETO.md
     6104  2026-09-22 22:32   RODAR_TESTES.bat
    15403  2026-09-22 22:32   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-22 22:32   _sistema/agendar_tiktok.py
    91980  2026-09-22 22:32   _sistema/agendar_youtube.py
    18611  2026-09-22 22:32   _sistema/app_paths.py
    68310  2026-09-22 22:32   _sistema/batch_engine.py
    27989  2026-09-22 22:32   _sistema/circuit_breaker.py
    55733  2026-09-22 22:32   _sistema/control_manager.py
     2545  2026-09-22 22:32   _sistema/domain/__init__.py
     3307  2026-09-22 22:32   _sistema/domain/checkpoints.py
     5442  2026-09-22 22:32   _sistema/domain/job_state_machine.py
    15978  2026-09-22 22:32   _sistema/domain/models.py
    19528  2026-09-22 22:32   _sistema/edit_project.py
    17683  2026-09-22 22:32   _sistema/gerar_textos.py
    85229  2026-09-22 22:32   _sistema/job_engine.py
    27891  2026-09-22 22:32   _sistema/limpar_metadados_oficial.py
     2724  2026-09-22 22:32   _sistema/login_conta.py
    23350  2026-09-22 22:32   _sistema/media_probe.py
    44162  2026-09-22 22:32   _sistema/painel_oficial.py
    21683  2026-09-22 22:32   _sistema/publication_idempotency.py
    32885  2026-09-22 22:32   _sistema/recovery_manager.py
    48725  2026-09-22 22:32   _sistema/resource_manager.py
    27252  2026-09-22 22:32   _sistema/retry_policy.py
    17712  2026-09-22 22:32   _sistema/secrets_manager.py
    94188  2026-09-22 22:32   _sistema/shutdown_coordinator.py
    20616  2026-09-22 22:32   _sistema/source_context.py
    49320  2026-09-22 22:32   _sistema/source_import.py
     1036  2026-09-22 22:32   _sistema/state_json.py
     3365  2026-09-22 22:32   _sistema/storage/__init__.py
    19254  2026-09-22 22:32   _sistema/storage/audit.py
    51041  2026-09-22 22:32   _sistema/storage/backup.py
    27352  2026-09-22 22:32   _sistema/storage/database.py
    38955  2026-09-22 22:32   _sistema/storage/legacy_migration.py
     1266  2026-09-22 22:32   _sistema/storage/migrations/__init__.py
     7411  2026-09-22 22:32   _sistema/storage/migrations/m001_initial.py
      764  2026-09-22 22:32   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-22 22:32   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-22 22:32   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-22 22:32   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-22 22:32   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-22 22:32   _sistema/storage/migrations/m007_source_asset_declarations.py
     4856  2026-09-22 22:32   _sistema/storage/migrations/m008_source_asset_context.py
   119736  2026-09-22 22:32   _sistema/storage_manager.py
    14323  2026-09-22 22:32   _sistema/time_utils.py
    35886  2026-09-22 22:32   empacotar_release.py
       33  2026-09-22 22:32   requirements.txt
     6014  2026-09-22 22:32   tests/README.md
       65  2026-09-22 22:32   tests/__init__.py
    11123  2026-09-22 22:32   tests/fakes_playwright.py
       21  2026-09-22 22:32   tests/requirements-test.txt
    23822  2026-09-22 22:32   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-22 22:32   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-22 22:32   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-22 22:32   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-22 22:32   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-22 22:32   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-22 22:32   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-22 22:32   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-22 22:32   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-22 22:32   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-22 22:32   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-22 22:32   tests/test_ai_cache_parsing.py
    20247  2026-09-22 22:32   tests/test_app_paths.py
    31890  2026-09-22 22:32   tests/test_backup_restore.py
    70112  2026-09-22 22:32   tests/test_batch_engine.py
    19226  2026-09-22 22:32   tests/test_checkpoints.py
    26822  2026-09-22 22:32   tests/test_circuit_breaker.py
     7389  2026-09-22 22:32   tests/test_config_names_paths.py
    86602  2026-09-22 22:32   tests/test_control_manager.py
    13408  2026-09-22 22:32   tests/test_domain_models.py
    22810  2026-09-22 22:32   tests/test_edit_project.py
     6240  2026-09-22 22:32   tests/test_ffmpeg_processing.py
     4450  2026-09-22 22:32   tests/test_fingerprint_state.py
     5547  2026-09-22 22:32   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-22 22:32   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-22 22:32   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-22 22:32   tests/test_job_engine.py
     6839  2026-09-22 22:32   tests/test_job_state_machine.py
    17472  2026-09-22 22:32   tests/test_legacy_json_migration.py
     3209  2026-09-22 22:32   tests/test_limpeza_permission_error.py
    23595  2026-09-22 22:32   tests/test_media_probe.py
     5888  2026-09-22 22:32   tests/test_migrations_frozen.py
    11476  2026-09-22 22:32   tests/test_operational_audit.py
    16612  2026-09-22 22:32   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-22 22:32   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-22 22:32   tests/test_publication_idempotency.py
    36620  2026-09-22 22:32   tests/test_recovery_manager.py
    37274  2026-09-22 22:32   tests/test_release_packaging.py
   105416  2026-09-22 22:32   tests/test_resource_manager.py
     4676  2026-09-22 22:32   tests/test_resume_detection.py
    31831  2026-09-22 22:32   tests/test_retry_policy.py
     7501  2026-09-22 22:32   tests/test_schedule_slots.py
    19241  2026-09-22 22:32   tests/test_secrets_manager.py
    89276  2026-09-22 22:32   tests/test_shutdown_coordinator.py
    21566  2026-09-22 22:32   tests/test_source_context.py
    18065  2026-09-22 22:32   tests/test_source_import.py
    29758  2026-09-22 22:32   tests/test_source_import_options.py
    24664  2026-09-22 22:32   tests/test_sqlite_storage.py
     7928  2026-09-22 22:32   tests/test_state_json_persistence.py
   116323  2026-09-22 22:32   tests/test_storage_manager.py
     5928  2026-09-22 22:32   tests/test_time_utils.py
---------                     -------
  3149096                     123 files
```

**Nota**: como este relatório (`PROMPT_27_EDIT_PROJECT_RELATORIO.md`) é escrito *depois* da construção do ZIP acima, ele próprio não está incluído nesse ZIP específico — mesmo padrão já usado nos relatórios dos Prompts 25/26. O relatório é entregue separadamente à máquina Windows, com sua própria verificação byte-a-byte.

**Varredura de padrões sensíveis** (`grep -rIniE "senha|password|token|cookie|authorization|api[_-]?key|secret"` sobre o conteúdo dos dois arquivos novos deste round): apenas duas ocorrências, ambas benignas — a docstring de `edit_project.py` citando `secrets_manager` como um dos módulos que ele NUNCA importa, e o teste `test_edit_project_nao_importa_modulos_protegidos` listando `"secrets_manager"` como termo proibido a verificar. Nenhum segredo real, credencial ou dado sensível encontrado — mesmo padrão já confirmado nos Prompts 24b/25/26.

Os 6 arquivos `.bat`/`.txt` emprestados foram removidos do sandbox de nuvem imediatamente após a construção do ZIP acima.
