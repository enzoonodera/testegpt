# PROMPT 24b — Import Options / User Assertions — Relatório de Entrega

## 1. Arquivos criados

- `_sistema/storage/migrations/m007_source_asset_declarations.py` — nova
  migration (tabela append-only `source_asset_declarations`).
- `tests/test_source_import_options.py` — 40 testes.
- `PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `_sistema/source_import.py` — adicionado `ImportOptions`, `Declaration`,
  `AssertionStore`, constantes `DECLARATION_KIND_*`/`ORIGIN_*`/`PRESET_*`/
  `ASSERTION_*`, e o parâmetro opcional `options` em
  `LocalFileImporter.import_source`/`FolderImporter.import_source`/
  `UrlImporter.import_source`/`SourceImportManager.import_local_file`/
  `import_folder`/`import_url` (todos com default `None` — nenhuma
  assinatura pública se tornou obrigatória). `SourceImportManager` ganhou
  `assertion_store`, `apply_assertions`, `remove_assertion`.
- `_sistema/storage/migrations/__init__.py` — registrado `MIGRATION_007`.
- `tests/test_migrations_frozen.py` — hash de `m007` adicionado
  conscientemente (mesmo padrão já usado para m004/m005/m006).
- `tests/test_sqlite_storage.py` — `LATEST_SCHEMA_VERSION` avançou de 6
  para 7 (consciente); `EXPECTED_TABLES` ganhou
  `source_asset_declarations`; listas de migrations esperadas atualizadas
  para incluir `(7, "source_asset_declarations")`.
- `tests/test_backup_restore.py` — mesma atualização consciente de schema
  6→7; as "sondas" de schema futuro usadas pelos testes de
  restore/crash/backup-pre-migration (que simulavam um binário com uma
  migration hipotética no PRÓXIMO número livre) foram renumeradas de 7
  para 8, para não colidir com `m007`, que agora é real e congelada —
  mesmo cuidado documentado nos comentários originais desses testes desde
  o Prompt 22 (quando a numeração já tinha sido deslocada de 6 para 7 pela
  mesma razão).
- `empacotar_release.py` — este relatório adicionado a
  `ALLOWED_ROOT_FILES`.

Nenhum outro arquivo foi modificado. `LocalFileImporter`/
`FolderImporter`/`UrlImporter` continuam com exatamente o mesmo
comportamento de antes quando chamados sem `options` — confirmado pelos
31 testes originais de `tests/test_source_import.py`, que passam sem
nenhuma alteração de código ou de resultado esperado.

## 3. Comportamento novo

`SourceImportManager`/os três importadores aceitam um `ImportOptions`
OPCIONAL. Quando fornecido:

- Para `LocalFileImporter`/`UrlImporter`: as declarações são associadas
  ao único `SourceAsset` resultante.
- Para `FolderImporter`: o MESMO `ImportOptions` se aplica a TODOS os
  arquivos importados com sucesso daquela chamada (item 1.2 — "aplicar a
  todos durante importação").

Fora do fluxo de import, `SourceImportManager.apply_assertions`/
`remove_assertion` (e o `AssertionStore` subjacente, com
`apply`/`apply_flags`/`apply_labels`/`remove`) permitem aplicar a um
subconjunto já selecionado de `SourceAsset`s, editar em lote, e remover
uma declaração já aplicada (item 1.4).

`AssertionStore.list_active(source_asset_id)` devolve o estado ATUAL
(derivado) das declarações de um `SourceAsset`; `list_by_origin(origin,
source_asset_id=...)` filtra pelo rastro de origem.

## 4. Investigação da seção 0.1 (confirmada com o código real)

- Confirmado por grep, antes de qualquer linha escrita: nenhum conceito
  de `ImportOptions`/`user_assertions`/`SYSTEM_BADGE`/`readiness_profile`
  existia no código.
- **Alvo real das declarações é `SourceAsset`, não `Video`** — `Video`
  continua não sendo criado pelos importadores (decisão do Prompt 24,
  mantida). Documentado extensivamente na docstring do módulo. Migrar
  declarações para `Video` quando um Prompt futuro definir o ciclo de
  vida `SourceAsset -> Video` é trabalho desse Prompt futuro.
- `target_project_id`/`target_account_id`: validados como UUID textual
  (mesmo padrão `_validate_uuid` de `domain/models.py`, reproduzido
  localmente — não importado, pois é uma função privada de outro
  módulo), mas NUNCA verificados contra o banco — nenhum lookup, nenhuma
  FK. Um UUID malformado sempre falha na construção de `ImportOptions`
  (`ValueError`); um UUID válido mas inexistente é aceito silenciosamente
  — decisão documentada e testada
  (`test_target_project_id_uuid_valido_mas_inexistente_no_banco_e_aceito`).
- Não existia tabela de rótulos/flags/assertions em nenhuma migration
  m001-m006 (lidas por completo antes de desenhar `m007`).
  `m002_audit_append_only.py` foi a referência de projeto usada — ver
  seção 5 para a decisão de desenho completa.
- `source_asset_id` é sempre a chave de associação (nunca caminho de
  arquivo/URL) — `SourceAsset.id`/`fingerprint` já eram a identidade
  estável esperada.

## 5. Decisões arquiteturais

### 5.1 Desenho de dados: tabela única append-only (opção "b")

`source_asset_declarations` (`m007`) é uma ÚNICA tabela onde cada linha é
um evento (`action='ADD'`/`'REMOVE'`) para uma chave lógica
(`source_asset_id`, `kind`, `value`). O estado atual de uma chave é o
`action` do evento mais recente (maior `rowid`) para essa chave —
derivado via `ROW_NUMBER() OVER (PARTITION BY source_asset_id, kind,
value ORDER BY rowid DESC)`, nunca uma cópia mutável separada.

Preferida à alternativa (a) — tabela de estado atual + tabela de
histórico separadas — porque: (1) evita risco de split-brain entre as
duas tabelas divergindo; (2) "remover"/"editar em lote depois" (roadmap)
significam que o estado muda com o tempo, mas o histórico (quando/por
quem/com qual origem) nunca pode ser reescrito — uma única fonte
append-only torna o estado atual sempre uma PROJEÇÃO determinística do
histórico, nunca algo que possa dessincronizar dele. A tabela é
append-only por TRIGGER SQLite (mesmo padrão de `audit_events`/m002):
`UPDATE`/`DELETE` diretos são negados pelo próprio banco, e reuso de `id`
também.

### 5.2 Mapeamento de presets (decisão de produto, testada)

| Preset              | Assertions (`IMPORT_PRESET`) | Labels (`IMPORT_PRESET`) |
|----------------------|-------------------------------|----------------------------|
| `ORIGINAL`            | —                              | `ORIGINAL`                 |
| `ALREADY_EDITED`      | `EXTERNALLY_EDITED`           | —                          |
| `READY_FOR_AI`        | —                              | `READY_FOR_AI`             |
| `USER_MARKED_READY`   | `USER_MARKED_READY`           | —                          |
| `CUSTOM`              | (nenhum mapeamento automático — o chamador fornece `user_assertions`/`user_flags`/`user_labels` explicitamente) | |

Um preset expandido é sempre gravado com `origin=IMPORT_PRESET`. Qualquer
valor explícito em `user_assertions`/`user_flags`/`user_labels` é sempre
gravado com `origin=USER` — os dois vocabulários coexistem na mesma
importação quando ambos são fornecidos.

### 5.3 SYSTEM_BADGE vs. USER_ASSERTION — garantia estrutural

`origin` (`SYSTEM`/`USER`/`IMPORT_PRESET`) é uma coluna OBRIGATÓRIA em
toda escrita (`AssertionStore._write_batch` valida e rejeita qualquer
origem fora do vocabulário), nunca inferida na leitura
(`list_active`/`list_by_origin` sempre devolvem a origem exatamente como
foi gravada). Este Prompt NUNCA escreve `origin=SYSTEM` — confirmado por
teste comportamental
(`test_este_prompt_nunca_grava_origin_system`, que varre a tabela após
qualquer sequência de operações públicas) e pela ausência estrutural de
qualquer chamada com `ORIGIN_SYSTEM` no código de produção do módulo.
`ORIGIN_SYSTEM` existe no vocabulário/schema porque o roadmap pede a
distinção — nenhuma evidência operacional automática existe ainda no
produto (viria de um Prompt futuro, ex. MediaProbe/Prompt 26). Um teste
adicional prova a garantia geral (não só a ausência): gravar diretamente
uma declaração como `SYSTEM` e uma como `USER` para o mesmo
`SourceAsset` e confirmar que cada consulta por origem devolve
exatamente e somente o que foi gravado com aquela origem.

### 5.4 Regra de conteúdo (item 1.7) — ausência estrutural

`EXTERNALLY_TITLE_READY`/`EXTERNALLY_DESCRIPTION_READY`/
`EXTERNALLY_HASHTAGS_READY` são só metadados declarativos. Nenhum trecho
deste módulo lê essas assertions para preencher
`Publication.title`/`description`/hashtags — confirmado por um teste AST
que varre toda referência de atributo `.title`/`.description`/
`.hashtags` no código real (não no texto bruto do arquivo, que citaria
esses nomes na própria docstring que documenta esta garantia — por isso
a checagem usa `ast.parse`, nunca comparação de substring no arquivo
inteiro). Uma camada futura de publicação deve tratar essas assertions
como "o usuário afirma que está pronto", nunca como o conteúdo em si.

### 5.5 `target_project_id`/`target_account_id`/`readiness_profile_id` — onde ficam persistidos

Diferente de `user_assertions`/`user_flags`/`user_labels` (declarações
repetíveis, com origem/histórico, na tabela `source_asset_declarations`),
estes três campos são REFERÊNCIAS OPACAS/escalares — um valor único, sem
semântica de "declaração com origem" (não faz sentido perguntar "quem
gravou esse `target_project_id`" da mesma forma que se pergunta a origem
de uma assertion). Não existe coluna dedicada em `sources`
(`SourceAsset`) para eles, e não reabrimos esse schema já aprovado para
adicioná-las. Em vez disso, são gravados em `SourceAsset.extra["import_options"]`
— o mecanismo genérico já existente no projeto para associar dados a uma
entidade sem exigir migration nova (`Entity.extra`, preservado em
round-trips). Quando nenhuma referência é fornecida, `extra` permanece
`{}` — idêntico ao comportamento já aprovado do Prompt 24 original.
`readiness_profile_id` é armazenado do mesmo jeito, como identificador
opaco — nenhuma lógica de "perfil de prontidão" foi implementada.

### 5.6 Nenhum `Job`/`Publication`/`Schedule`/`Project`/`Video` criado

Confirmado por: (a) teste AST que varre por `"Job("`/`"Publication("`/
`"Schedule("`/`"Project("` no código-fonte real (estende o mesmo teste já
aprovado do Prompt 24 original); (b) teste comportamental que roda um
import com `ImportOptions` COMPLETO (todas as seis opções preenchidas) e
confirma que `database.list(Job/Publication/Schedule/Video)` continuam
vazios.

### 5.7 GATE ADVERSARIAL 3 (crash window) — `apply_options` é atômico

Achado adversarial durante a segunda passagem: a primeira versão de
`apply_options` chamava `apply()` uma vez POR CATEGORIA (preset
assertions, preset labels, user_assertions, user_flags, user_labels) —
até cinco transactions SQLite separadas para um único `ImportOptions`.
Um processo morto entre duas dessas chamadas deixaria um subconjunto
parcial das categorias persistido, o resto perdido silenciosamente.

Corrigido: `apply_options` agora monta uma lista única de entradas
`(kind, value, origin)` cobrindo TODAS as categorias não vazias e grava
tudo em UMA ÚNICA transaction (`AssertionStore._write_batch`,
`database.transaction()` — `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`
automático em exceção). `apply()`/`remove()` (chamadas manuais fora do
fluxo de import) também passaram a usar o mesmo `_write_batch`, cada
chamada continuando atômica em si mesma.

Provado por um teste que força uma falha REAL do SQLite no meio do
`executemany` do batch (não uma exceção sintética capturada pelo próprio
código): o trigger append-only `trg_source_asset_declarations_no_id_reuse`
(m007) aborta a transaction quando um `id` duplicado aparece dentro do
mesmo lote — o teste faz `new_uuid()` devolver sempre o mesmo valor via
monkeypatch, forçando a segunda linha do batch a colidir com a primeira
(já visível dentro da mesma transaction). A exceção
(`sqlite3.IntegrityError`) propaga sem ser capturada por
`AssertionStore` — o ROLLBACK de `LocalDatabase.transaction()` é quem
garante "tudo ou nada". O teste confirma zero linhas persistidas após o
crash simulado, e recuperação completa (as quatro categorias) numa
chamada seguinte.

### 5.8 Concorrência (GATE 6/14) — considerado e dispensado, justificado

Diferente dos Prompts 20-23 (que protegem decisões do tipo
"ler-decidir-escrever" — claim de Job, transição de estado, etc.), toda
escrita deste módulo é um INSERT append-only puro: nunca há uma leitura
prévia que decide o que escrever (não existe "se já existe X, faça Y").
Duas instâncias chamando `apply()`/`apply_options()` ao mesmo tempo para
o mesmo `SourceAsset` simplesmente produzem eventos `ADD` adicionais —
comutativos, nunca conflitantes: `list_active` deriva corretamente o
estado independentemente da ordem de chegada, e um evento `ADD`
duplicado não corrompe nada (o mesmo resultado de "ativo" já seria
alcançado por uma única chamada). Por não haver decisão baseada em
leitura prévia (TOCTOU), testes explícitos de Barrier/concorrência não
foram adicionados nesta rodada — decisão consciente, documentada aqui em
vez de simplesmente omitida.

## 6. Confirmações explícitas exigidas pelo Prompt

- **`SourceAsset` (não `Video`) é o alvo real das declarações**: seção
  4/5.5.
- **Nenhum `Job`/`Publication`/`Schedule`/`Project`/`Video` criado**:
  seção 5.6.
- **Nenhuma lógica de preenchimento automático de
  título/descrição/hashtags**: seção 5.4.
- **Comportamento já aprovado de `LocalFileImporter`/`FolderImporter`/
  `UrlImporter` sem `ImportOptions` não mudou**: os 31 testes originais
  de `tests/test_source_import.py` passam sem nenhuma alteração; a única
  mudança de assinatura é um parâmetro `options: ImportOptions | None =
  None` adicional em cada `import_source`/`import_local_file`/
  `import_folder`/`import_url`.
- **`circuit_breaker.py`/`retry_policy.py`/`publication_idempotency.py`/
  `secrets_manager.py`/`domain/job_state_machine.py`/Geração 1 não
  tocados**: confirmado por hash SHA-256 idêntico aos registros
  anteriores (colado abaixo).

```
b0ea87e441cde0aa1661dde5bb1b4d04403a637bc582ca1fd5b2ba62e48e1d9e  _sistema/circuit_breaker.py
2df58146f8588c2b78aac537476391c84927744589c2f4f135fa9070ea074493  _sistema/retry_policy.py
f118e0a62bbcf31b48c83c2d350cfcb3cb376d2953042b33715a99f78edc0c38  _sistema/publication_idempotency.py
e15aa6a8a502a3349ca15349721d4e7dec22f173315c2b37d2fc4d849dcd09e8  _sistema/secrets_manager.py
5f613154477c8b57e0af11de94d462d86a095504d6bdf7e9f37be0de764e6169  _sistema/domain/job_state_machine.py
1e25b77e1ecb5bbb06c5d8ea1f62efb948ab98dcfa5f10a9b10bd2c283c2ce14  _sistema/agendar_youtube.py
4cfc392a967180a47bd9270dbc7add1ef4207733a1e9a4e836398cc93d617110  _sistema/agendar_tiktok.py
```

`_sistema/domain/models.py` (`SourceAsset`/`Video`/`Project`/`Account`)
também permanece byte-idêntico — nenhum campo novo foi adicionado a
nenhuma entidade existente (`9d3dede98b1390c16627449c85bf91a4b4ead0263289d56617bc06c743fb9bc7`).

## 7. Migrations

Nova: `m007_source_asset_declarations.py` (versão 7). `m001`-`m006`
permanecem byte-idênticas — confirmado por `tests/test_migrations_frozen.py`
(hash de `m007` adicionado conscientemente, mesmo padrão já usado desde
o Prompt 20).

`LATEST_SCHEMA_VERSION` avançou de 6 para 7 — atualização mecânica e
consciente de `tests/test_sqlite_storage.py`/`tests/test_backup_restore.py`
(valores hardcoded de schema version/listas de migrations esperadas, e
renumeração das "sondas" de schema futuro usadas por testes de
restore/crash de 7 para 8, para não colidir com a migration real nova) —
sem alterar o comportamento de nenhum teste, só a numeração que reflete o
avanço real do schema.

## 8. Testes automatizados

`tests/test_source_import_options.py`: **40 testes**, cobrindo:

1. Import sem `ImportOptions` (omitido/`None`) — nenhuma regressão.
2. `ImportOptions` preenchido — assertions/flags/labels/referências
   escalares persistidos e recuperáveis (via releitura do banco).
3. `FolderImporter` aplica o mesmo `ImportOptions` a todos os arquivos
   importados com sucesso, isolamento preservado (arquivo inválido não
   impede a aplicação nos válidos).
4. Presets — mapeamento exato de cada um, `CUSTOM` sem mapeamento
   automático, `CUSTOM` + `user_assertions` explícitas funciona, preset
   desconhecido rejeitado na construção.
5. API pós-import — aplicar a subconjunto selecionado, editar em lote,
   remover, histórico append-only preservado após remoção, reaplicar
   após remover.
6. Distinção estrutural SYSTEM_BADGE vs. USER_ASSERTION — três ângulos
   (nunca aparece em consulta de origem errada; comportamental; este
   Prompt nunca grava SYSTEM).
7. Regra de conteúdo (item 1.7) — AST, nenhum `Job`/`Publication`/
   `Schedule`/`Project` criado por código novo, nem por comportamento com
   `ImportOptions` completo.
8. Ortogonalidade — nenhum import dos módulos protegidos.
9. Validação de UUID — malformado rejeitado, válido-mas-inexistente
   aceito.
10. Timestamp — formato `utc_now_iso()`.
11. Nenhum segredo/caminho desnecessário no histórico — colunas da
    tabela restritas exatamente às do item 1.6.
11b. **Crash window (GATE 3)** — `apply_options` atômico, provado com
    falha real do SQLite no meio do batch (não uma exceção sintética).
12. Restart — novas instâncias de `LocalDatabase`/`SourceImportManager`.
13. Normalização/validação de entradas — string vazia rejeitada, chamada
    com lista vazia é no-op seguro.

Nenhum teste existente foi removido ou enfraquecido; os 31 testes de
`tests/test_source_import.py` (Prompt 24 original) continuam passando
sem alteração.

Suíte completa: **1409 passed, 1 skipped, 36 subtests passed** (1369
anteriores + 40 novos), 0 falhas.

`python3 -m compileall -q _sistema tests`: **OK**.

## 9. Confirmação do ZIP final (saída literal de `unzip -l`)

**Aviso de honestidade explícito, exigido pelo próprio Prompt**: `device_bash`
foi checado NOVAMENTE nesta rodada, antes de gerar o ZIP, e continuou
indisponível ("Workspace unavailable. The isolated Linux environment on
this device failed to start."). Isso significa que este ZIP NÃO foi
construído rodando `EMPACOTAR_RELEASE.bat`/`empacotar_release.py`
diretamente no Windows — ou seja, **não é literalmente o mesmo arquivo
que um `EMPACOTAR_RELEASE.bat` rodado pelo usuário no Windows produziria
byte a byte** (nomes de diretório, timestamps do ZIP e, por causa disso,
o SHA-256 do ZIP em si seriam diferentes de uma execução feita
diretamente lá).

O que ESTE ZIP realmente é, e por que a verificação continua válida:
antes de gerá-lo, os 9 arquivos novos/modificados desta rodada
(`_sistema/source_import.py`, `_sistema/storage/migrations/__init__.py`,
`_sistema/storage/migrations/m007_source_asset_declarations.py`,
`tests/test_source_import_options.py`,
`tests/test_migrations_frozen.py`, `tests/test_sqlite_storage.py`,
`tests/test_backup_restore.py`, `empacotar_release.py`, e este próprio
relatório) já haviam sido entregues ao Windows via
`device_commit_files` e reconfirmados byte-idênticos via
`device_stage_files` + `cmp -s` (ver seção de entrega do chat para os
`MATCH` de cada um) — ou seja, o CONTEÚDO de cada arquivo usado para
gerar este ZIP é comprovadamente idêntico, byte a byte, ao que está
fisicamente no Windows. O ZIP foi então construído no ambiente de
auditoria (cloud sandbox), com o MESMO `empacotar_release.py` e as
MESMAS árvores `_sistema`/`tests`, tomando emprestados (somente leitura,
nunca alterados) os 6 arquivos de raiz Windows-only
(`EMPACOTAR_RELEASE.bat`, `INSTALAR_DEPENDENCIAS_TESTE.bat`,
`LEIA_ME_PRIMEIRO.txt`, `LIMPAR_ANTES_DE_ZIPAR.bat`, `PAINEL_OFICIAL.bat`,
`RODAR_TESTES.bat`) necessários para `REQUIRED_ROOT_FILES` — removidos do
sandbox logo depois, nunca persistidos ali.

Portanto: a LISTAGEM de arquivos e TAMANHOS abaixo é uma confirmação
válida de que todos os arquivos desta entrega entram no pacote e que
nenhum padrão sensível indevido foi acionado — mas o SHA-256 do ZIP em si
NÃO deve ser tratado como "o hash do ZIP que o usuário obteria rodando
`EMPACOTAR_RELEASE.bat` agora", porque não foi gerado lá. Recomendação:
quando `device_bash` voltar a funcionar (ou numa sessão futura com acesso
a ele), rodar `EMPACOTAR_RELEASE.bat` diretamente no Windows como
confirmação adicional — como já sugerido nos relatórios anteriores que
tiveram a mesma limitação.

Suíte completa + compileall rodados no sandbox imediatamente antes deste
build (idênticos aos números da seção 8): **1409 passed, 1 skipped, 36
subtests passed**, compileall OK — confirmando que o sandbox está no
mesmo estado dos arquivos entregues.

Comando executado: `python3 empacotar_release.py --out
/mnt/user-data/outputs/PAINEL_OFICIAL_ZIP_VERIFICACAO.zip`

Resultado do empacotador (build concluído com sucesso; as linhas
"dentro de arvore permitida, mas fora do manifesto de
extensoes/arquivos" são apenas a listagem informativa de `__pycache__`
sendo corretamente EXCLUÍDO do ZIP, não um erro):

```
[2/3] ZIP gerado atomicamente, com identidade revalidada por arquivo, e ja inspecionado.

[3/3] Calculando SHA-256 do ZIP final...
      SHA-256: 9ab5c6f2421e700e92b91415e22d9f9d596dad7e777ad59fc3de73ceb67f8faf
      Tamanho: 0.79 MB

======================================================================
EMPACOTAMENTO CONCLUIDO COM SUCESSO
Arquivo: PAINEL_OFICIAL_ZIP_VERIFICACAO.zip
SHA-256: 9ab5c6f2421e700e92b91415e22d9f9d596dad7e777ad59fc3de73ceb67f8faf
======================================================================
```

Saída LITERAL e COMPLETA de `unzip -l` sobre o ZIP recém-gerado (114
arquivos):

```
Archive:  /mnt/user-data/outputs/PAINEL_OFICIAL_ZIP_VERIFICACAO.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-22 19:24   ARQUITETURA_ATUAL.md
    13132  2026-09-22 19:24   CLAUDE.md
     2687  2026-09-22 19:24   EMPACOTAR_RELEASE.bat
   169662  2026-09-22 19:24   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-22 19:24   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-22 19:24   LEIA_ME_PRIMEIRO.txt
      866  2026-09-22 19:24   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-22 19:24   MAPA_DE_DADOS.md
      686  2026-09-22 19:24   PAINEL_OFICIAL.bat
    29374  2026-09-22 19:24   PRODUCT_INVARIANTS.md
    23512  2026-09-22 19:24   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-22 19:24   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-22 19:24   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-22 19:24   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-22 19:24   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    19766  2026-09-22 19:24   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-22 19:24   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    24543  2026-09-22 19:24   REGRESSION_CHECKLIST.md
    26204  2026-09-22 19:24   RISCOS_ATUAIS.md
    34429  2026-09-22 19:24   ROADMAP_COMPLETO.md
     6104  2026-09-22 19:24   RODAR_TESTES.bat
    15403  2026-09-22 19:24   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-22 19:24   _sistema/agendar_tiktok.py
    91980  2026-09-22 19:24   _sistema/agendar_youtube.py
    18611  2026-09-22 19:24   _sistema/app_paths.py
    68310  2026-09-22 19:24   _sistema/batch_engine.py
    27989  2026-09-22 19:24   _sistema/circuit_breaker.py
    55733  2026-09-22 19:24   _sistema/control_manager.py
     2545  2026-09-22 19:24   _sistema/domain/__init__.py
     3307  2026-09-22 19:24   _sistema/domain/checkpoints.py
     5442  2026-09-22 19:24   _sistema/domain/job_state_machine.py
    15978  2026-09-22 19:24   _sistema/domain/models.py
    17683  2026-09-22 19:24   _sistema/gerar_textos.py
    85229  2026-09-22 19:24   _sistema/job_engine.py
    27891  2026-09-22 19:24   _sistema/limpar_metadados_oficial.py
     2724  2026-09-22 19:24   _sistema/login_conta.py
    44162  2026-09-22 19:24   _sistema/painel_oficial.py
    21683  2026-09-22 19:24   _sistema/publication_idempotency.py
    32885  2026-09-22 19:24   _sistema/recovery_manager.py
    48725  2026-09-22 19:24   _sistema/resource_manager.py
    27252  2026-09-22 19:24   _sistema/retry_policy.py
    17712  2026-09-22 19:24   _sistema/secrets_manager.py
    94188  2026-09-22 19:24   _sistema/shutdown_coordinator.py
    45760  2026-09-22 19:24   _sistema/source_import.py
     1036  2026-09-22 19:24   _sistema/state_json.py
     3365  2026-09-22 19:24   _sistema/storage/__init__.py
    19254  2026-09-22 19:24   _sistema/storage/audit.py
    51041  2026-09-22 19:24   _sistema/storage/backup.py
    27352  2026-09-22 19:24   _sistema/storage/database.py
    38955  2026-09-22 19:24   _sistema/storage/legacy_migration.py
     1171  2026-09-22 19:24   _sistema/storage/migrations/__init__.py
     7411  2026-09-22 19:24   _sistema/storage/migrations/m001_initial.py
      764  2026-09-22 19:24   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-22 19:24   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-22 19:24   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-22 19:24   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-22 19:24   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-22 19:24   _sistema/storage/migrations/m007_source_asset_declarations.py
   119736  2026-09-22 19:24   _sistema/storage_manager.py
    14323  2026-09-22 19:24   _sistema/time_utils.py
    35747  2026-09-22 19:24   empacotar_release.py
       33  2026-09-22 19:24   requirements.txt
     6014  2026-09-22 19:24   tests/README.md
       65  2026-09-22 19:24   tests/__init__.py
    11123  2026-09-22 19:24   tests/fakes_playwright.py
       21  2026-09-22 19:24   tests/requirements-test.txt
    23822  2026-09-22 19:24   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-22 19:24   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-22 19:24   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-22 19:24   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-22 19:24   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-22 19:24   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-22 19:24   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-22 19:24   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-22 19:24   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-22 19:24   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-22 19:24   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-22 19:24   tests/test_ai_cache_parsing.py
    20247  2026-09-22 19:24   tests/test_app_paths.py
    31899  2026-09-22 19:24   tests/test_backup_restore.py
    70112  2026-09-22 19:24   tests/test_batch_engine.py
    19226  2026-09-22 19:24   tests/test_checkpoints.py
    26822  2026-09-22 19:24   tests/test_circuit_breaker.py
     7389  2026-09-22 19:24   tests/test_config_names_paths.py
    86602  2026-09-22 19:24   tests/test_control_manager.py
    13408  2026-09-22 19:24   tests/test_domain_models.py
     6240  2026-09-22 19:24   tests/test_ffmpeg_processing.py
     4450  2026-09-22 19:24   tests/test_fingerprint_state.py
     5547  2026-09-22 19:24   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-22 19:24   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-22 19:24   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-22 19:24   tests/test_job_engine.py
     6839  2026-09-22 19:24   tests/test_job_state_machine.py
    17472  2026-09-22 19:24   tests/test_legacy_json_migration.py
     3209  2026-09-22 19:24   tests/test_limpeza_permission_error.py
     5127  2026-09-22 19:24   tests/test_migrations_frozen.py
    11476  2026-09-22 19:24   tests/test_operational_audit.py
    16612  2026-09-22 19:24   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-22 19:24   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-22 19:24   tests/test_publication_idempotency.py
    36620  2026-09-22 19:24   tests/test_recovery_manager.py
    37274  2026-09-22 19:24   tests/test_release_packaging.py
   105416  2026-09-22 19:24   tests/test_resource_manager.py
     4676  2026-09-22 19:24   tests/test_resume_detection.py
    31831  2026-09-22 19:24   tests/test_retry_policy.py
     7501  2026-09-22 19:24   tests/test_schedule_slots.py
    19241  2026-09-22 19:24   tests/test_secrets_manager.py
    89276  2026-09-22 19:24   tests/test_shutdown_coordinator.py
    18065  2026-09-22 19:24   tests/test_source_import.py
    29758  2026-09-22 19:24   tests/test_source_import_options.py
    24499  2026-09-22 19:24   tests/test_sqlite_storage.py
     7928  2026-09-22 19:24   tests/test_state_json_persistence.py
   116323  2026-09-22 19:24   tests/test_storage_manager.py
     5928  2026-09-22 19:24   tests/test_time_utils.py
---------                     -------
  2938149                     114 files
```

Confirmação via `grep` (arquivos deste Prompt presentes no ZIP, com
tamanhos idênticos aos entregues e verificados byte-a-byte no Windows):

```
    19766  2026-09-22 19:24   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    45760  2026-09-22 19:24   _sistema/source_import.py
     4713  2026-09-22 19:24   _sistema/storage/migrations/m007_source_asset_declarations.py
    35747  2026-09-22 19:24   empacotar_release.py
    31899  2026-09-22 19:24   tests/test_backup_restore.py
     5127  2026-09-22 19:24   tests/test_migrations_frozen.py
    18065  2026-09-22 19:24   tests/test_source_import.py
    29758  2026-09-22 19:24   tests/test_source_import_options.py
    24499  2026-09-22 19:24   tests/test_sqlite_storage.py
```

Confirmação via `grep -i "secret\|token\|cookie\|credential\|\.env\b"`
sobre a listagem completa: apenas os três arquivos já aprovados desde a
correção do Prompt 23 aparecem —

```
    13751  2026-09-22 19:24   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    17712  2026-09-22 19:24   _sistema/secrets_manager.py
    19241  2026-09-22 19:24   tests/test_secrets_manager.py
```

— confirmando que nenhum arquivo novo deste Prompt colidiu com
`SENSITIVE_FILENAME_PATTERNS`.

## 10. Riscos conhecidos e dívida técnica

- **`readiness_profile_id` é só armazenamento opaco** — nenhuma lógica de
  "perfil de prontidão" existe. Um Prompt futuro que definir essa lógica
  vai ler o valor já persistido em `SourceAsset.extra["import_options"]`.
- **`target_project_id`/`target_account_id` não validados contra o
  banco** — decisão consciente (seção 4), mas significa que uma
  referência "morta" (projeto/conta que nunca existiu ou foi removido)
  pode ficar associada a um `SourceAsset` sem nenhum aviso nesta camada;
  uma camada futura que consuma essas referências precisa tratar esse
  caso.
- **`extra["import_options"]` é uma chave de convenção, não um contrato
  formal de schema** — se um Prompt futuro também precisar gravar outras
  informações em `SourceAsset.extra`, a convenção de namespace
  (`import_options`) precisa ser respeitada para não colidir.
- **Concorrência não testada com Barrier/Event explícitos** — justificado
  na seção 5.8 (nenhuma decisão TOCTOU existe neste módulo; todo write é
  um INSERT append-only comutativo).
- **Vocabulário de `user_assertions`/`user_flags`/`user_labels` não é
  fechado** — o módulo não impõe uma lista fixa de valores aceitos (são
  livres por natureza, conforme o roadmap); os `ASSERTION_*`/`PRESET_*`
  são só os valores de referência do roadmap, não uma validação
  restritiva.

## 11. Pendências

Nenhuma pendência dentro do escopo autorizado deste Prompt. Não iniciado
Prompt 25, conforme instrução.
