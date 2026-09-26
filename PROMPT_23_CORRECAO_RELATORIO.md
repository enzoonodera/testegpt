# PROMPT 23 — CORREÇÃO (teste frágil de DPAPI + reentrega correta do ZIP)

## 1. `test_dpapi_indisponivel_neste_ambiente_levanta_ao_construir` — corrigido

**Abordagem escolhida: (a)** dividir em dois testes.

- `test_dpapi_indisponivel_via_monkeypatch_levanta_ao_construir_em_qualquer_ambiente`
  (renomeado a partir do teste original quebrado) — usa `monkeypatch` para
  forçar `_dpapi_available() -> False`, exatamente como o teste irmão
  `test_dpapi_indisponivel_simulado_em_plataforma_windows_tambem_levanta`
  já fazia (e já passava nos dois ambientes). Prova o comportamento
  "fail closed" de forma universal — passa em CI Linux e em Windows real
  igualmente, porque não depende de qual é a plataforma real.
- `test_dpapi_real_constroi_e_faz_roundtrip_em_windows_de_verdade` (NOVO)
  — marcado com `@pytest.mark.skipif(sys.platform != "win32", ...)`.
  Roda SÓ em Windows real e prova o caminho POSITIVO: `_dpapi_available()`
  devolve `True`, `DpapiSecretsBackend` constrói com sucesso, um roundtrip
  real `store`/`retrieve` via `CryptProtectData`/`CryptUnprotectData`
  funciona, e o arquivo `.dpapi` persistido no disco NÃO contém o texto
  plano do segredo. Em CI (Linux) aparece como `SKIPPED`, nunca `FAILED`.
  Automatiza permanentemente a validação manual que o usuário fez à mão.

**Por que (a) em vez de (b)**: as duas abordagens descritas no Prompt são
equivalentes na prática — (b) descreve a mesma estrutura final, só chegando
lá por "ajustar o teste atual" em vez de "dividir". Escolhi nomear como
divisão explícita (a) porque deixa mais claro, só pelo nome de cada teste,
qual prova cada um faz: um nunca depende da plataforma real (prova
universal), o outro só roda e só prova algo em Windows real (prova
específica) — evitando que um único teste tente carregar duas
responsabilidades diferentes (fail-closed universal E funcionamento real
em Windows), que foi exatamente o que quebrou o teste original.

O teste `test_nenhum_caminho_de_codigo_grava_texto_claro_quando_dpapi_indisponivel`
(que já usava `monkeypatch`) não precisou de nenhuma mudança — continua
válido nos dois ambientes como estava.

Nenhum teste dos 25 originalmente aprovados foi removido ou enfraquecido —
só o quebrado foi corrigido (virou 1 monkeypatch + 1 novo skipif), e todos
os outros 24 continuam exatamente como estavam. `tests/test_secrets_manager.py`
tem agora 26 testes coletados: 25 rodam e passam em qualquer ambiente, 1
(`test_dpapi_real_constroi_e_faz_roundtrip_em_windows_de_verdade`) roda só
em Windows e aparece como `skipped` aqui.

## 2. `secrets_manager.py` não foi alterado

Confirmado por hash SHA-256, idêntico ao registrado na entrega original do
Prompt 23:

```
e15aa6a8a502a3349ca15349721d4e7dec22f173315c2b37d2fc4d849dcd09e8  _sistema/secrets_manager.py
```

O comportamento do módulo já estava correto — confirmado pela validação
manual real do usuário em Windows (roundtrip funcionou, arquivo `.dpapi`
gravado é ilegível como texto). Este é um Prompt de correção de TESTE e
de EMPACOTAMENTO, não de produto.

## 3. `requirements.txt`/`INSTALAR_DEPENDENCIAS_TESTE.bat` não foram alterados

Confirmado por leitura: `requirements.txt` já contém `tzdata` (linha 3)
desde antes deste Prompt, e não foi tocado (mtime inalterado desde antes
desta correção). O segundo erro relatado pelo usuário
(`ModuleNotFoundError: No module named 'tzdata'`) foi confirmado como
erro de ambiente — a suíte manual foi rodada com o Python global do
Windows em vez do `.venv-test` isolado que `INSTALAR_DEPENDENCIAS_TESTE.bat`
já mantém para isto. Nenhuma mudança foi feita nesses dois arquivos, como
instruído.

## 4. ACHADO ADICIONAL (fora do que foi pedido, mas causa raiz da reentrega
   quebrada): `empacotar_release.py` bloqueava os três arquivos deste
   Prompt por um filtro de nome

Ao executar `empacotar_release.py` para gerar o ZIP e rodar `unzip -l`
como instruído, o build teve **sucesso**, mas o ZIP gerado **não continha**
`_sistema/secrets_manager.py`, `tests/test_secrets_manager.py` nem
`PROMPT_23_SECRETS_MANAGER_RELATORIO.md`. A saída do empacotador reportou
a razão explicitamente:

```
- PROMPT_23_SECRETS_MANAGER_RELATORIO.md (bloqueado por defesa em profundidade: nome de arquivo sensivel (padrao *secret*): PROMPT_23_SECRETS_MANAGER_RELATORIO.md)
- _sistema/secrets_manager.py (bloqueado por defesa em profundidade: nome de arquivo sensivel (padrao *secret*): secrets_manager.py)
- tests/test_secrets_manager.py (bloqueado por defesa em profundidade: nome de arquivo sensivel (padrao *secret*): test_secrets_manager.py)
```

**Causa raiz identificada**: `SENSITIVE_FILENAME_PATTERNS` (camada de
"defesa em profundidade" do empacotador, endurecida em auditorias
anteriores do GATE 19.5 para nunca deixar um `.env`/`token.json`/
`*credential*` entrar num ZIP de release por engano) inclui o padrão
amplo `*secret*`. Esse padrão é de NOME, não de CONTEÚDO — e por isso
também bloqueava, por acidente, o próprio módulo `SecretsManager` do
produto: código-fonte revisado que GERENCIA segredos, nunca um arquivo
que CONTÉM um. Como nenhum dos três arquivos é `REQUIRED_*`, o
empacotamento "teve sucesso" mesmo sem eles — silenciosamente. **Esta é
exatamente a causa da entrega anterior estar incompleta.**

**Correção aplicada** (`empacotar_release.py`): uma allowlist NOMINAL
nova, `SENSITIVE_PATTERN_EXEMPTIONS`, contendo só os três caminhos
relativos exatos já revisados por este Prompt:

```python
SENSITIVE_PATTERN_EXEMPTIONS = frozenset({
    "_sistema/secrets_manager.py",
    "tests/test_secrets_manager.py",
    "PROMPT_23_SECRETS_MANAGER_RELATORIO.md",
})
```

`_forbidden_defense_in_depth` agora só aplica `SENSITIVE_FILENAME_PATTERNS`
quando o caminho NÃO está nessa lista nominal. Isto preserva o filtro
`*secret*` para QUALQUER outro arquivo — um `secrets.json`, um
`client_secret.txt`, ou um módulo futuro chamado `outro_secret_qualquer.py`
adicionado sem revisão continuam bloqueados normalmente. Não é um
enfraquecimento do padrão, é uma exceção nominal e auditável a três
caminhos específicos, com a mesma disciplina de "isto exige revisão
consciente" já usada em `LEGACY_DO_NOT_DISTRIBUTE`.

Dois testes novos em `tests/test_release_packaging.py` provam a correção
nos dois sentidos:

- `test_secrets_manager_modulo_teste_e_relatorio_sao_permitidos_apesar_do_padrao_secret`
  — os três caminhos exatos ENTRAM no ZIP.
- `test_outro_arquivo_com_secret_no_nome_fora_da_excecao_continua_bloqueado`
  — um arquivo fictício (`_sistema/outro_secret_qualquer.py`,
  `tests/test_secret_nao_relacionado.py`) FORA da exceção nominal continua
  BLOQUEADO — prova que a correção não abriu um buraco geral no filtro.

Suíte de empacotamento completa: **49 passed** (47 pré-existentes + 2
novos), nenhum teste removido ou enfraquecido.

## 5. Reentrega — confirmação do ZIP (saída literal de `unzip -l`)

`device_bash` (shell no computador do usuário) estava indisponível no
momento desta correção ("Workspace unavailable. The isolated Linux
environment on this device failed to start."). Para não bloquear a
verificação obrigatória, os seis arquivos de raiz exigidos que só existem
no computador Windows (`EMPACOTAR_RELEASE.bat`,
`INSTALAR_DEPENDENCIAS_TESTE.bat`, `LEIA_ME_PRIMEIRO.txt`,
`LIMPAR_ANTES_DE_ZIPAR.bat`, `PAINEL_OFICIAL.bat`, `RODAR_TESTES.bat`)
foram copiados (via `device_stage_files`, leitura, nunca alterados) para
dentro do ambiente de auditoria, e `empacotar_release.py` foi executado
ali — o MESMO script, byte-idêntico ao entregue no Windows (hash
conferido abaixo), sobre a MESMA árvore `_sistema/`/`tests/` já entregue
e verificada byte-idêntica nas rodadas anteriores. Isto reproduz fielmente
o que `EMPACOTAR_RELEASE.bat` produziria no Windows. Recomendo, assim que
o computador voltar a responder por `device_bash` (ou diretamente pelo
usuário), rodar `EMPACOTAR_RELEASE.bat` no Windows real como confirmação
adicional — a lógica já está provada correta aqui.

Saída literal e completa de `unzip -l` sobre o ZIP final (inclui este
próprio relatório, já adicionado a `ALLOWED_ROOT_FILES`):
`painel_oficial_release.zip`, SHA-256
`b1edbbc80868b800655238e2c7bbd36e059909f1b2efbfa319b0748c8254d7a6`:

```
Archive:  /tmp/release_out/painel_oficial_release.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-22 18:19   ARQUITETURA_ATUAL.md
    13132  2026-09-22 18:19   CLAUDE.md
     2687  2026-09-22 18:19   EMPACOTAR_RELEASE.bat
   169662  2026-09-22 18:19   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-22 18:19   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-22 18:19   LEIA_ME_PRIMEIRO.txt
      866  2026-09-22 18:19   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-22 18:19   MAPA_DE_DADOS.md
      686  2026-09-22 18:19   PAINEL_OFICIAL.bat
    29374  2026-09-22 18:19   PRODUCT_INVARIANTS.md
    23512  2026-09-22 18:19   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-22 18:19   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-22 18:19   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    11959  2026-09-22 18:19   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-22 18:19   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    24543  2026-09-22 18:19   REGRESSION_CHECKLIST.md
    26204  2026-09-22 18:19   RISCOS_ATUAIS.md
    34429  2026-09-22 18:19   ROADMAP_COMPLETO.md
     6104  2026-09-22 18:19   RODAR_TESTES.bat
    15403  2026-09-22 18:19   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-22 18:19   _sistema/agendar_tiktok.py
    91980  2026-09-22 18:19   _sistema/agendar_youtube.py
    17894  2026-09-22 18:19   _sistema/app_paths.py
    68310  2026-09-22 18:19   _sistema/batch_engine.py
    27989  2026-09-22 18:19   _sistema/circuit_breaker.py
    55733  2026-09-22 18:19   _sistema/control_manager.py
     2545  2026-09-22 18:19   _sistema/domain/__init__.py
     3307  2026-09-22 18:19   _sistema/domain/checkpoints.py
     5442  2026-09-22 18:19   _sistema/domain/job_state_machine.py
    15978  2026-09-22 18:19   _sistema/domain/models.py
    17683  2026-09-22 18:19   _sistema/gerar_textos.py
    85229  2026-09-22 18:19   _sistema/job_engine.py
    27891  2026-09-22 18:19   _sistema/limpar_metadados_oficial.py
     2724  2026-09-22 18:19   _sistema/login_conta.py
    44162  2026-09-22 18:19   _sistema/painel_oficial.py
    21683  2026-09-22 18:19   _sistema/publication_idempotency.py
    32885  2026-09-22 18:19   _sistema/recovery_manager.py
    48725  2026-09-22 18:19   _sistema/resource_manager.py
    27252  2026-09-22 18:19   _sistema/retry_policy.py
    17712  2026-09-22 18:19   _sistema/secrets_manager.py
    94188  2026-09-22 18:19   _sistema/shutdown_coordinator.py
     1036  2026-09-22 18:19   _sistema/state_json.py
     3365  2026-09-22 18:19   _sistema/storage/__init__.py
    19254  2026-09-22 18:19   _sistema/storage/audit.py
    51041  2026-09-22 18:19   _sistema/storage/backup.py
    27352  2026-09-22 18:19   _sistema/storage/database.py
    38955  2026-09-22 18:19   _sistema/storage/legacy_migration.py
     1071  2026-09-22 18:19   _sistema/storage/migrations/__init__.py
     7411  2026-09-22 18:19   _sistema/storage/migrations/m001_initial.py
      764  2026-09-22 18:19   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-22 18:19   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-22 18:19   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-22 18:19   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-22 18:19   _sistema/storage/migrations/m006_publication_idempotency.py
   119736  2026-09-22 18:19   _sistema/storage_manager.py
    14323  2026-09-22 18:19   _sistema/time_utils.py
    35657  2026-09-22 18:19   empacotar_release.py
       33  2026-09-22 18:19   requirements.txt
     6014  2026-09-22 18:19   tests/README.md
       65  2026-09-22 18:19   tests/__init__.py
    11123  2026-09-22 18:19   tests/fakes_playwright.py
       21  2026-09-22 18:19   tests/requirements-test.txt
    23822  2026-09-22 18:19   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-22 18:19   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-22 18:19   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-22 18:19   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-22 18:19   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-22 18:19   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-22 18:19   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-22 18:19   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-22 18:19   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-22 18:19   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-22 18:19   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-22 18:19   tests/test_ai_cache_parsing.py
    20247  2026-09-22 18:19   tests/test_app_paths.py
    31890  2026-09-22 18:19   tests/test_backup_restore.py
    70112  2026-09-22 18:19   tests/test_batch_engine.py
    19226  2026-09-22 18:19   tests/test_checkpoints.py
    26822  2026-09-22 18:19   tests/test_circuit_breaker.py
     7389  2026-09-22 18:19   tests/test_config_names_paths.py
    86602  2026-09-22 18:19   tests/test_control_manager.py
    13408  2026-09-22 18:19   tests/test_domain_models.py
     6240  2026-09-22 18:19   tests/test_ffmpeg_processing.py
     4450  2026-09-22 18:19   tests/test_fingerprint_state.py
     5547  2026-09-22 18:19   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-22 18:19   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-22 18:19   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-22 18:19   tests/test_job_engine.py
     6839  2026-09-22 18:19   tests/test_job_state_machine.py
    17472  2026-09-22 18:19   tests/test_legacy_json_migration.py
     3209  2026-09-22 18:19   tests/test_limpeza_permission_error.py
     4485  2026-09-22 18:19   tests/test_migrations_frozen.py
    11476  2026-09-22 18:19   tests/test_operational_audit.py
    16612  2026-09-22 18:19   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-22 18:19   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-22 18:19   tests/test_publication_idempotency.py
    36620  2026-09-22 18:19   tests/test_recovery_manager.py
    37274  2026-09-22 18:19   tests/test_release_packaging.py
   105416  2026-09-22 18:19   tests/test_resource_manager.py
     4676  2026-09-22 18:19   tests/test_resume_detection.py
    31831  2026-09-22 18:19   tests/test_retry_policy.py
     7501  2026-09-22 18:19   tests/test_schedule_slots.py
    19241  2026-09-22 18:19   tests/test_secrets_manager.py
    89276  2026-09-22 18:19   tests/test_shutdown_coordinator.py
    24189  2026-09-22 18:19   tests/test_sqlite_storage.py
     7928  2026-09-22 18:19   tests/test_state_json_persistence.py
   116323  2026-09-22 18:19   tests/test_storage_manager.py
     5928  2026-09-22 18:19   tests/test_time_utils.py
---------                     -------
  2787474                     108 files
```

Confirmação direta com `grep -i secret` sobre a listagem completa (saída
literal):

```
    13751  2026-09-22 18:19   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    17712  2026-09-22 18:19   _sistema/secrets_manager.py
    19241  2026-09-22 18:19   tests/test_secrets_manager.py
```

Os três arquivos deste Prompt estão confirmados dentro do ZIP.

## 6. Testes obrigatórios — confirmação

- `tests/test_secrets_manager.py`: **25 passed, 1 skipped** (o `skipif`
  aparece como `SKIPPED`, nunca `FAILED`, neste ambiente Linux).
- `tests/test_release_packaging.py`: **49 passed** (47 + 2 novos).
- Suíte completa do projeto: **1338 passed, 1 skipped, 36 subtests
  passed** (1336 anteriores + 2 novos testes de empacotamento + 1 novo
  teste de DPAPI Windows-only agora skipped em vez de contar como
  "passed" antigo — total de itens coletados: 1339, dos quais 1338
  rodaram e passaram e 1 foi pulado corretamente).
- `python3 -m compileall -q _sistema tests empacotar_release.py`: **OK**.

## 7. Arquivos modificados nesta correção

- `tests/test_secrets_manager.py` — teste quebrado corrigido/dividido
  (item 1).
- `empacotar_release.py` — `SENSITIVE_PATTERN_EXEMPTIONS` adicionada
  (item 4).
- `tests/test_release_packaging.py` — 2 testes novos provando a correção
  do item 4 nos dois sentidos.
- `PROMPT_23_CORRECAO_RELATORIO.md` — este relatório (adicionado a
  `ALLOWED_ROOT_FILES` em `empacotar_release.py` para que a próxima
  geração de ZIP já o inclua).

`_sistema/secrets_manager.py` NÃO foi modificado (hash confirmado, item
2). `requirements.txt`/`INSTALAR_DEPENDENCIAS_TESTE.bat` NÃO foram
modificados (item 3).

## 8. Pendências

- Rodar `EMPACOTAR_RELEASE.bat` diretamente no Windows real (assim que
  `device_bash` voltar a responder, ou pelo próprio usuário) como
  confirmação adicional sobre a máquina real — a lógica já foi provada
  correta na reprodução fiel feita aqui.
- Nenhuma outra pendência dentro do escopo desta correção. Não iniciado
  Prompt 24, conforme instrução.
