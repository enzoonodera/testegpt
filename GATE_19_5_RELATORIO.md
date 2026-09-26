# GATE 19.5 — Relatório (Estágio 1, corrigido — 3 rodadas)

**Status: NÃO APROVADO ainda.** Três rodadas de auditoria independente já
foram feitas sobre o empacotador de release. 1ª (BLOCKER de segurança:
denylist deixava vazar dados de runtime/perfil/cookie, symlink vazava
conteúdo externo) → corrigida migrando para allowlist + rejeição de link.
2ª (3 BLOCKERs de integridade: arquivo obrigatório ausente não derrubava o
build, erro ao enumerar uma árvore era engolido, falha na escrita deixava
ZIP parcial) → corrigida com `REQUIRED_*`, fail-closed em enumeração, e
build atômico. 3ª — **esta entrega** (3 BLOCKERs no vínculo entre o
arquivo INSPECIONADO e o EFETIVAMENTE GRAVADO: hardlink tratado como
arquivo regular, TOCTOU entre a coleta e `ZipFile.write`, e um REQUIRED
podendo sumir entre a validação inicial e a coleta) → corrigida com
rejeição de hardlink, leitura vinculada a identidade (`st_dev`/`st_ino`
revalidados no handle aberto, nunca reabrindo o pathname) e 3 camadas de
verificação de obrigatoriedade — ver seção 1C. As contagens de teste
foram recalculadas pelo pytest real a cada rodada (nenhum número
reaproveitado manualmente). As auditorias profundas por área (Estágio 2)
continuam pendentes — não foram tocadas nesta entrega, como pedido.

Prompt 20 **não foi iniciado**. Geração 1 e Geração 2 **não foram
integradas**. Código produtivo **não foi alterado** em nenhuma das 3
rodadas — só o empacotador e seus testes. Nenhuma migration nova;
`m001`/`m002`/`m003` verificadas por hash (ver seção 5) e essa
verificação é automática, não manual.

---

## 1. BLOCKER corrigido — empacotador migrado para allowlist/manifesto

**O que estava errado:** `empacotar_release.py` v1 trabalhava por
denylist (`project_root.rglob("*")`, copiava tudo que não batesse com uma
lista de padrões proibidos). A auditoria independente reproduziu, com
dados 100% fictícios, dois problemas reais:

1. Uma pasta LEGACY `contas/youtube/demo/` com `perfil_youtube/Cookies`,
   `videos/001.mp4`, `logs/raw.log`, mais um `.env` e `token.json` na
   raiz — tudo foi parar dentro do ZIP, e o empacotador reportou
   "sucesso", porque nada na denylist antiga previa esses nomes.
2. Um symlink dentro do projeto apontando para um arquivo fora da árvore
   fonte — `Path.rglob` segue links por padrão, então o conteúdo do
   arquivo EXTERNO foi copiado para dentro do ZIP.

**Correção arquitetural aplicada** (`empacotar_release.py` reescrito por
completo):

- **Camada 1 — allowlist estrutural** (`ALLOWED_ROOT_FILES`,
  `ALLOWED_TREE_ROOTS`, `ALLOWED_TREE_EXTENSIONS`,
  `ALLOWED_TREE_EXTRA_FILES`): só entra o que está explicitamente listado.
  Raiz do projeto: nomes exatos (17 arquivos: os `.bat`/`.md`/`.txt`/`.py`
  de infraestrutura e documentação). Árvores permitidas: só `_sistema/` e
  `tests/`, e dentro delas só `.py` (mais `tests/README.md` e
  `tests/requirements-test.txt`, nomeados explicitamente). Um diretório
  de nível superior novo (`contas`, `_removidas`, qualquer nome) é
  **ignorado por inteiro automaticamente**, com aviso explícito na saída
  — nunca copiado silenciosamente. Um arquivo novo dentro de
  `_sistema`/`tests` que não seja `.py` também é ignorado com aviso, pelo
  mesmo motivo.
- **Camada 2 — rejeição de link/reparse point**: antes de entrar em
  qualquer diretório ou copiar qualquer arquivo, cada entrada é checada
  com `Path.is_symlink()`, `Path.is_junction()` (Python 3.12+, cobre
  junctions do Windows que não aparecem como symlink) e o bit
  `FILE_ATTRIBUTE_REPARSE_POINT` via `os.lstat` quando disponível. Se
  qualquer link for encontrado em qualquer ponto da árvore permitida, o
  empacotamento inteiro **falha** com `PackagingSecurityError` e mensagem
  explícita — nunca segue o link, nunca resolve para fora da raiz, nunca
  "melhor esforço". A varredura agora é manual via `os.scandir`
  (`_walk_tree_no_links`), nunca mais `rglob` (que segue links).
- **Camada 3 — defesa em profundidade**: mesmo dentro da allowlist,
  ainda existe uma lista de nomes de diretório proibidos (`contas`,
  `_removidas`, `accounts`, `perfil_youtube`, `videos`, `logs`, `cache`,
  `database`, etc.) e padrões de nome de arquivo sensível (`.env`,
  `*token*`, `*cookie*`, `*credential*`, `*secret*`, `*.db`, `*.sqlite`,
  `*-wal`, `*-shm`) — para o caso de o manifesto um dia incluir algo
  assim por engano.
- **Camada 4 — inspeção do ZIP já gerado**: reabre o ZIP no disco e
  reaplica as regras de allowlist + camada 3 contra cada entrada. Se algo
  passar por todas as camadas anteriores e ainda assim aparecer no ZIP, o
  ZIP é apagado e o processo termina com erro.

**Testado adversarialmente nesta sessão** (reproduzindo literalmente os
dois cenários da auditoria, com dados 100% fictícios, contra o projeto
real):
- Pasta `contas/youtube/demo_ficticio/` com `perfil_youtube/Cookies`
  fictício e `videos/001.mp4` fictício, mais `.env`/`token.json`
  fictícios na raiz → **nenhum entrou no ZIP**, todos apareceram na lista
  de "ignorados" (não silencioso).
- Symlink de `_sistema/leak.py` para um arquivo fora da árvore, e symlink
  de diretório para uma pasta externa → **as duas tentativas abortaram o
  empacotamento** com `PackagingSecurityError`, nenhum ZIP foi deixado no
  disco.
- Injeção manual de `_sistema/__pycache__/evil.pyc` direto dentro de um
  ZIP já validado (simulando um bug futuro em outra etapa) → a inspeção
  pós-geração (camada 4) detectou.

---

## 1B. BLOCKERs de integridade corrigidos (2ª auditoria independente)

A allowlist/manifesto da 1ª rodada estava correta (nada de runtime
vazava mais), mas a 2ª auditoria independente provou que um empacotador
"seguro" ainda podia mentir sobre o que entregou. Três problemas
reproduzidos, todos corrigidos em `empacotar_release.py`:

**a) Arquivo obrigatório ausente não derrubava o build.** Remover
`PAINEL_OFICIAL.bat` e rodar o empacotador ainda retornava sucesso e
gerava um ZIP sem ele — a allowlist só sabia dizer "pode entrar se
existir", nunca "precisa existir". **Corrigido** com um novo conceito
explícito: `REQUIRED_ROOT_FILES` (9 itens: `PAINEL_OFICIAL.bat`,
`requirements.txt`, `CLAUDE.md`, `empacotar_release.py` e os 5 `.bat` de
infraestrutura de build/teste) e `REQUIRED_TREE_FILES` (28 módulos
produtivos de `_sistema/`, incluindo as 3 migrations congeladas). O resto
do manifesto (`ARQUITETURA_ATUAL.md`, `MAPA_DE_DADOS.md`,
`PRODUCT_INVARIANTS.md`, `REGRESSION_CHECKLIST.md`, `RISCOS_ATUAIS.md`,
`ROADMAP_COMPLETO.md`, `GATE_19_5_RELATORIO.md`, `TESTE_MANUAL_WINDOWS.md`
— documentação interna) é classificado como `OPTIONAL_ROOT_FILES`,
explicitamente documentado no código: ausência não impede o build, porque
não impede nem o produto de rodar nem o release de ser gerado. A
validação roda **antes** de qualquer seleção/zip; se faltar algo,
`PackagingIntegrityError`, exit code 1, **nenhum ZIP é gerado**, e a
mensagem lista exatamente quais caminhos faltaram.

**b) Erro ao enumerar uma árvore permitida era engolido.**
`_walk_tree_no_links` tinha `except OSError: continue` — um
`PermissionError` simulado em `os.scandir("_sistema/storage")` produzia
um ZIP "de sucesso" sem `database.py` e sem nada daquela subárvore.
**Corrigido**: qualquer `OSError`/`PermissionError`/`FileNotFoundError`
ao enumerar uma árvore do manifesto agora aborta o empacotamento inteiro
com `PackagingIntegrityError`, informando o caminho e o tipo do erro.
Testado com os três tipos de erro (permissão, I/O genérico, diretório
que desaparece no meio da varredura).

**c) Falha na escrita deixava ZIP parcial no caminho final.** Uma
exceção simulada no meio de `ZipFile.write` interrompia corretamente,
mas o `.zip` parcial ficava no disco no mesmo nome do ZIP final —
podendo ser confundido com uma entrega válida. **Corrigido**:
`build_release_zip` agora gera sempre em um arquivo temporário
(`tempfile.mkstemp`, mesmo diretório do destino) e só promove para o
nome final com `os.replace` (atômico) depois de gerado, fechado E
inspecionado com sucesso. Qualquer exceção em qualquer etapa (seleção,
escrita, inspeção) apaga o temporário e nunca toca no caminho final — um
ZIP anterior válido no mesmo caminho também fica intacto se o build atual
falhar (testado explicitamente).

**Fail-closed em `_is_link_like` (revisão adicional pedida):** a versão
anterior usava `Path.is_symlink()` e capturava `OSError` de `os.lstat`
devolvendo `None` ("não é link"). O problema: "não consegui inspecionar"
não é o mesmo que "comprovadamente arquivo regular". **Corrigido**: a
função agora chama `os.lstat` diretamente (nunca via `Path.is_symlink()`,
que engole erro internamente) e, se a própria inspeção falhar
(`PermissionError`, corrida, etc.), levanta `PackagingSecurityError`
imediatamente — um caminho que não pode ser inspecionado nunca é tratado
como seguro. Testado com `os.lstat` falhando tanto isoladamente quanto
dentro de um build completo.

**`_sistema/defender_setup.ps1` — classificado explicitamente (não mais
"ignorado porque não é .py" silencioso).** Esse script (documentado em
`ARQUITETURA_ATUAL.md` seção 20 e `RISCOS_ATUAIS.md`) adiciona a pasta do
projeto às exclusões do Windows Defender — contraria diretamente o
princípio F do `CLAUDE.md`. Adicionado um dicionário
`LEGACY_DO_NOT_DISTRIBUTE` com o motivo documentado por item; o
empacotador agora relata explicitamente "LEGACY / NÃO DISTRIBUIR: ..."
com a justificativa completa, em vez de uma mensagem genérica de "fora do
manifesto de extensões". Continua nunca entrando no ZIP.

---

## 1C. BLOCKERs de vínculo inspeção↔gravação corrigidos (3ª auditoria)

A 2ª rodada corrigiu obrigatoriedade e atomicidade, mas a allowlist ainda
confiava que "o arquivo que eu inspecionei" e "o arquivo que
`ZipFile.write` vai efetivamente ler" eram sempre o mesmo. A 3ª auditoria
independente provou que essa suposição podia ser quebrada de três formas:

**a) Hardlink tratado como arquivo regular.** `_is_link_like` (nome
antigo) só rejeitava symlink/junction/reparse point — nunca hardlink. Um
`os.link()` de um arquivo externo para dentro de `_sistema/*.py` tem
`st_nlink == 2` e passa perfeitamente em `lstat` como "arquivo regular".
Reproduzido: `outside_secret.txt` linkado como `_sistema/innocent.py` →
conteúdo externo entrava no ZIP com sucesso reportado. **Corrigido**:
qualquer arquivo selecionado para o ZIP agora precisa ter `st_nlink <= 1`
(`_require_regular_no_hardlink`) — sem tentar decidir qual dos nomes "é o
original" (conservador: os DOIS lados de um hardlink são rejeitados).
Aplicado tanto em `REQUIRED_*` quanto em qualquer arquivo comum da
allowlist.

**b) TOCTOU entre a inspeção e `ZipFile.write`.** O fluxo antigo
inspecionava o caminho (`lstat`), guardava o `Path`, e só bem depois
`ZipFile.write(path)` reabria esse mesmo `Path` por NOME para ler os
bytes. Entre esses dois momentos, nada impedia o pathname de apontar para
outra coisa. Reproduzido: `_sistema/batch_engine.py` era arquivo regular
na coleta; antes da escrita, o arquivo era apagado e um symlink para um
arquivo externo (`SEGREDO_EXTERNO_TOCTOU_NUNCA_PODE_VAZAR`) era criado no
mesmo nome — `ZipFile.write` reabria o pathname, seguia o symlink, e o
conteúdo externo entrava no ZIP com sucesso reportado. Um segundo `lstat`
antes da escrita reduziria a janela, mas não a fecharia. **Corrigido**
com uma leitura vinculada a identidade (`_read_verified_bytes`): (1) abre
o arquivo com `O_NOFOLLOW` (se virou symlink, a abertura já falha aqui);
(2) `os.fstat` NO HANDLE aberto (nunca `lstat` do pathname de novo); (3)
confere que o handle ainda é arquivo regular; (4) confere `st_nlink <=
1` no handle; (5) compara `st_dev`/`st_ino` do handle com a identidade
capturada na coleta — se não bater, TOCTOU detectado e abortado; (6) só
então lê os bytes DESSE handle e grava via `ZipFile.writestr` —
`ZipFile.write(path)` não é mais usado em lugar nenhum do código. Handles
são abertos e fechados um arquivo por vez (nunca centenas simultâneos).
Reproduzido de novo com o fix aplicado: `PackagingSecurityError` na
abertura (`O_NOFOLLOW` rejeitando o symlink com `ELOOP`), nenhum ZIP
gerado.

**c) `REQUIRED` existia na validação e sumia antes da coleta.**
`_validate_required_paths()` confere existência no início, mas nada
impedia o arquivo de ser removido depois, antes de `_collect_allowed_files`
rodar — o build "tinha sucesso" sem ele. Reproduzido: `PAINEL_OFICIAL.bat`
existe quando a validação roda, é removido em seguida, a coleta
simplesmente não o encontra, ZIP é gerado sem ele. **Corrigido** com
**três camadas de obrigatoriedade que precisam TODAS concordar**:
  1. sistema de arquivos inicial (`_validate_required_paths`, já existia);
  2. snapshot efetivamente selecionado após a coleta
     (`_missing_required_from_selected`, NOVO — fecha a janela entre
     validação e coleta);
  3. ZIP realmente produzido, reaberto e conferido ANTES do
     `os.replace` promover para o nome final (`_missing_required_in_zip`,
     NOVO — fecha a janela entre coleta e escrita).
Qualquer uma das três camadas sozinha já aborta o build com
`PackagingIntegrityError` e nenhum ZIP é gerado/promovido.

---

## 2. Testes automatizados do empacotador

**Arquivo:** `tests/test_release_packaging.py` — agora **47 testes**
(21 da 1ª rodada + 17 da 2ª rodada + 9 novos desta 3ª rodada), todos com
dados 100% sintéticos (`tmp_path`, nunca toca dados reais):

Da 1ª rodada (segurança/allowlist):
- 2 testes de "permitido entra" (`.py` em `_sistema/`, doc de raiz).
- 5 testes de "artefato de build não entra" (`.venv-test`,
  `.pytest_cache`, `__pycache__`, `*.pyc` solto, `*.zip` antigo).
- 7 testes de "dado de runtime/sensível fictício não entra" — reproduzem
  literalmente o cenário do BLOCKER original: `contas/` legacy,
  `_removidas/`, perfil de navegador fictício, vídeo fictício, log bruto
  fictício, `.env` fictício, `token.json`/`credential_store.json`
  fictícios, banco `.sqlite` fictício.
- 2 testes de symlink (arquivo externo e diretório externo) → devem
  lançar `PackagingSecurityError` e não deixar ZIP no disco.
- 1 teste de injeção manual pós-geração sendo detectada.
- 2 testes de SHA-256 e de "ZIP inválido é apagado" (via monkeypatch da
  inspeção).
- 1 teste de validação do manifesto (arquivos de raiz reais ⊆
  `ALLOWED_ROOT_FILES`).

Novos desta rodada (integridade — BLOCKERs 1/2/3 da 2ª auditoria):
- 5 testes de arquivo obrigatório ausente: `PAINEL_OFICIAL.bat`,
  `requirements.txt`, módulo produtivo (`batch_engine.py`), migration
  congelada (`m002_audit_append_only.py`) → todos `PackagingIntegrityError`,
  sem ZIP; mais 1 teste confirmando que um item **opcional** ausente
  (`ARQUITETURA_ATUAL.md`) **não** impede o build (comportamento
  documentado, não assumido).
- 3 testes de erro ao enumerar árvore: `PermissionError`, `OSError`
  genérico, `FileNotFoundError` no meio da travessia → todos
  `PackagingIntegrityError`, sem ZIP.
- 4 testes de build atômico: falha no primeiro arquivo da escrita, falha
  no meio da escrita, falha na própria inspeção, e confirmação de que um
  ZIP **anterior válido** no mesmo caminho não é apagado/corrompido se o
  build atual falhar — em todos, nenhum ZIP parcial nem temporário órfão
  fica no disco.
- 2 testes de fail-closed em `_is_link_like` (isolado e dentro de um
  build completo, com `os.lstat` falhando).
- 1 teste de `defender_setup.ps1` sendo ignorado com motivo LEGACY
  explícito (não genérico).
- 1 teste extra de validação do manifesto na direção oposta: todo caminho
  de `REQUIRED_TREE_FILES` realmente existe no projeto real hoje.

Novos desta 3ª rodada (vínculo inspeção↔gravação — BLOCKERs 1/2/3 da 3ª
auditoria):
- 2 testes de hardlink: arquivo externo linkado para dentro de
  `_sistema/*.py`, e hardlink entre dois arquivos internos → ambos
  `PackagingSecurityError`, sem ZIP.
- 2 testes de TOCTOU: arquivo trocado por symlink depois da coleta e
  antes da escrita → `PackagingSecurityError` (via `O_NOFOLLOW` na
  abertura); arquivo trocado por OUTRO arquivo regular (outro inode, via
  `os.replace` para garantir inode genuinamente diferente, evitando reuso
  de inode recém-liberado) → `PackagingSecurityError` por identidade
  (`st_dev`/`st_ino`) não bater mais. Nenhum dos dois usa `sleep`; a troca
  é forçada deterministicamente via monkeypatch de `_collect_allowed_files`,
  que roda antes da escrita começar.
- 4 testes das 3 camadas de obrigatoriedade: `REQUIRED` "mentido" como
  presente na validação inicial mas fisicamente ausente → camada 2
  (snapshot) pega; `REQUIRED` removido entre coleta e escrita → camada
  2/3 pega; ZIP final simulado sem um `REQUIRED` (bug hipotético em
  `_zip_files`) → camada 3 (ZIP realmente produzido) pega antes do
  `os.replace`; `st_nlink` mudando para >1 depois da coleta (hardlink
  criado após a coleta) → `PackagingSecurityError`.
- 1 teste de falha de `fstat` no handle verificado → `PackagingSecurityError`,
  nenhum ZIP parcial.

**Arquivo novo:** `tests/test_migrations_frozen.py` — 2 testes:
verifica que `m001_initial.py`/`m002_audit_append_only.py`/
`m003_batch_engine.py` batem byte a byte (SHA-256) com os hashes
congelados, e que nenhum arquivo `m00N*.py` novo existe fora dessa lista
sem autorização explícita. Isso substitui a checagem manual "não abri
essas migrations, então não mudaram" por uma checagem automática que
roda a cada `pytest -q` — já está integrada ao gate de teste normal, não
é um passo separado que alguém pode esquecer de rodar.

---

## 3. Contagens de teste — recalculadas pelo pytest real

Nenhum número foi reaproveitado manualmente. Todos os valores abaixo
vieram de `pytest --collect-only -q` rodado agora, nesta sessão:

| Arquivo | Testes |
|---|---|
| test_storage_manager.py | 163 |
| test_control_manager.py | 88 |
| test_resource_manager.py | 73 |
| test_shutdown_coordinator.py | 72 |
| test_batch_engine.py | 65 |
| test_job_state_machine.py | 51 |
| test_recovery_manager.py | 50 |
| test_domain_models.py | 43 |
| test_job_engine.py | 36 |
| **test_release_packaging.py** | **47** |
| test_checkpoints.py | 30 |
| test_backup_restore.py | 27 |
| test_app_paths.py | 26 |
| test_time_utils.py | 22 |
| test_sqlite_storage.py | 22 |
| test_legacy_json_migration.py | 16 |
| test_fingerprint_state.py | 15 |
| test_config_names_paths.py | 14 |
| test_operational_audit.py | 13 |
| **test_migrations_frozen.py (novo)** | **2** |
| test_ai_cache_parsing.py | 9 |
| test_schedule_slots.py | 8 |
| test_ffmpeg_processing.py | 6 |
| test_resume_detection.py | 3 |
| **TOTAL** | **901** |

Conferido: `BatchEngine=65`, `ControlManager=88`, `ResourceManager=73`,
`StorageManager=163` batem exatamente com os números que a auditoria
independente reportou.

**Classificação Geração 1 / Geração 2 / compartilhado — sem soma
forçada:**

- **Geração 1** (scripts standalone reais — `agendar_youtube.py`,
  `agendar_tiktok.py`, `gerar_textos.py`, `limpar_metadados_oficial.py`):
  `test_ai_cache_parsing` (9) + `test_resume_detection` (3) +
  `test_config_names_paths` (14) + `test_schedule_slots` (8) +
  `test_ffmpeg_processing` (6) + `test_fingerprint_state` (15) =
  **55 testes**.
- **Compartilhado** (`app_paths.py` e `time_utils.py` — confirmei via
  grep que ambos são importados tanto pelos scripts da Geração 1 quanto
  usados pela suíte da Geração 2): `test_app_paths` (26) +
  `test_time_utils` (22) = **48 testes**. `test_domain_models.py` (43)
  importa um símbolo de `time_utils` mas testa exclusivamente os modelos
  de domínio (`Job`, etc.) usados só pela Geração 2 — classifiquei como
  Geração 2, não compartilhado, porque o que ele exercita é
  exclusivamente da engine nova.
- **Geração 2** (engine nova — JobEngine/BatchEngine/ResourceManager/
  ControlManager/RecoveryManager/ShutdownCoordinator/StorageManager/
  SQLite): os 14 arquivos restantes = **749 testes**.
- **Infraestrutura de empacotamento** (nem Geração 1 nem 2 — ferramenta
  de build): `test_release_packaging` (47, era 21 → 38 → 47) +
  `test_migrations_frozen` (2) = **49 testes**.

  55 + 48 + 749 + 49 = 901. Confere com o total.

---

## 4. Resultado completo

- `pytest --collect-only -q`: **901 tests collected** (Linux, Python
  3.13.13, `.venv-test`-equivalente local).
- `pytest -q`: **901 passed, 0 failed**, ~53s.
- `python -m compileall -q _sistema tests empacotar_release.py`: sem erros.
- Baseline da rodada anterior deste mesmo gate: 892. Nova baseline: 901
  (892 + 9 testes novos de vínculo inspeção↔gravação desta 3ª rodada).
  Baseline original pré-GATE-19.5: 852 — 901 > 852, requisito satisfeito.
  **Nenhum teste antigo foi removido**; 3 testes da 2ª rodada precisaram
  ser ATUALIZADOS (não removidos) porque seu mecanismo interno mudou:
  `test_falha_apos_alguns_arquivos_escritos_...` agora monkeypatcha
  `_read_verified_bytes` em vez de `ZipFile.write` (que não é mais usado
  pelo código); o teste de fail-closed em `lstat` foi renomeado de
  `test_is_link_like_...` para `test_lstat_or_fail_...` porque a função
  `_is_link_like` foi substituída por `_lstat_or_fail`/`_classify`; e o
  teste de identidade trocada passou a usar `os.replace` em vez de
  `unlink`+`write_text` para garantir um inode genuinamente diferente
  (evitando o caso raro de reuso do mesmo número de inode recém-liberado
  no mesmo diretório, que tornava o teste instável). Em todos os três
  casos a INTENÇÃO e a cobertura do teste são as mesmas de antes — só o
  detalhe de implementação que ele exercita mudou junto com o código.

Isto foi executado aqui (Linux); preciso que você rode
`RODAR_TESTES.bat` no Windows real para confirmar 901 lá.

---

## 5. Migrations — hashes verificados (automatizado agora)

```
m001_initial.py            3c78c665c1bae7c50482d513c9e7534d5586caa23513a1e4457712502a806e70
m002_audit_append_only.py  1ba5a8422900e8ac35e25e44d2940cfb10687b508313fe0e5d158b521b6e6a36
m003_batch_engine.py       ab99085a0d77f1e260119d353b25e619769c05f5802d8190b0e88471a76b33e1
```

Todos batem exatamente com os hashes que você forneceu. Nenhum `m004`
existe. Calculei isso agora com `sha256sum` diretamente E via o novo
`tests/test_migrations_frozen.py`, que roda automaticamente toda vez que
a suíte roda — não é mais uma verificação manual que alguém pode
esquecer.

---

## 6. Arquivos modificados/criados (acumulado das 3 rodadas deste gate)

**Modificado nesta 3ª rodada:**
- `empacotar_release.py` — `_is_link_like` foi substituída por
  `_lstat_or_fail` (fail-closed puro) + `_classify` (classificação
  symlink/junction/reparse/dir/regular/other) + `_require_regular_no_hardlink`
  (rejeita qualquer `st_nlink > 1`, conservador). Nova classe
  `_FileIdentity` (NamedTuple com `st_dev`/`st_ino`) capturada na coleta.
  Nova função `_read_verified_bytes` (abre com `O_NOFOLLOW`, `fstat` no
  handle, confere regular + `st_nlink<=1` + identidade, só então lê os
  bytes) — `_zip_files` agora grava via `ZipFile.writestr` com esses
  bytes, nunca mais `ZipFile.write(path)`. Novas
  `_missing_required_from_selected` (camada 2/3 de obrigatoriedade) e
  `_missing_required_in_zip` (camada 3/3, antes do `os.replace`).
  `_collect_allowed_files`, `_walk_tree_no_links` e
  `_validate_required_paths` reescritos para propagar `_FileIdentity`
  junto com cada arquivo selecionado. Nenhuma mudança na allowlist
  estrutural (camada 1) nem nos padrões sensíveis (camada 3 de defesa em
  profundidade) herdados das rodadas anteriores.
- `tests/test_release_packaging.py` — 9 testes novos (hardlink externo/
  interno, TOCTOU via symlink, TOCTOU via identidade, as 3 camadas de
  `REQUIRED`, `st_nlink` mudando pós-coleta, falha de `fstat`); mais 3
  testes já existentes ATUALIZADOS para continuar testando a MESMA coisa
  através do novo mecanismo interno (detalhado na seção 4).

**Não tocados nesta rodada nem nas anteriores:** `EMPACOTAR_RELEASE.bat`
(a interface de chamada não mudou — continua chamando
`empacotar_release.py` com o mesmo contrato de CLI), `INSTALAR_DEPENDENCIAS_TESTE.bat`,
`RODAR_TESTES.bat`, `LIMPAR_ANTES_DE_ZIPAR.bat`, `tests/test_migrations_frozen.py`,
qualquer arquivo produtivo em `_sistema/`, migrations, `TESTE_MANUAL_WINDOWS.md`.

---

## 7. Confirmações do gate

- `.venv-test` = 0, `.pytest_cache` = 0, `__pycache__` = 0, `*.pyc` = 0
  — confirmado tanto pela inspeção manual quanto pelo próprio
  empacotador (camada 6).
- `contas/`/`_removidas/`/perfis/vídeos/logs de runtime = 0 — testado
  com dados fictícios reproduzindo o cenário exato do BLOCKER de
  segurança original (seção 1), confirmado 0 no ZIP resultante.
- **Reprodução dos 3 BLOCKERs de integridade da 2ª auditoria**, contra um
  projeto ISOLADO (cópia completa de `_sistema/`+`tests/` mais
  placeholders para os `.bat`/`.txt` que só existem no seu Windows, nunca
  contra dados reais de conta): remover `PAINEL_OFICIAL.bat` → `exit=1`,
  nenhum ZIP criado; `PermissionError` simulado em
  `os.scandir("_sistema/storage")` → `PackagingIntegrityError`, nenhum
  ZIP; falha simulada no meio da escrita → `OSError` propagado, nenhum
  ZIP final nem temporário órfão; `PermissionError` simulado em
  `os.lstat` → `PackagingSecurityError` (fail-closed), nenhum ZIP.
- **Reprodução dos 3 BLOCKERs desta 3ª auditoria**, contra o mesmo
  projeto isolado: hardlink de `outside_secret.txt` para
  `_sistema/innocent.py` (`st_nlink=2`) → `[ERRO DE SEGURANCA] hardlink
  detectado (st_nlink=2) ... Empacotamento CONSERVADOR`, `exit=2`, nenhum
  ZIP; `PAINEL_OFICIAL.bat` "mentido" como presente na validação inicial
  mas fisicamente removido antes da coleta → `PackagingIntegrityError`
  ("removidos entre a validação e a coleta"), nenhum ZIP; TOCTOU real —
  `_sistema/batch_engine.py` trocado por um symlink para
  `/tmp/toctou_secret.txt` DEPOIS da coleta e ANTES da escrita → aberto
  com `O_NOFOLLOW`, `OSError: [Errno 40] Too many levels of symbolic
  links` capturado e relançado como `PackagingSecurityError`, conteúdo
  externo NUNCA lido, nenhum ZIP.
- Build limpo de verificação (mesmo projeto isolado, sem falhas
  simuladas, com os 6 placeholders de `.bat`/`.txt`): **65 arquivos**,
  zero artefatos suspeitos, `SHA-256 fb736e1e343c005c246db7b0f8384f41c45064401018508b33cc7315de1ea0aa`.
- Migrations: verificadas por hash automaticamente (seção 5). Sem
  `m004`.
- Prompt 20: **não iniciado**. Geração 1/2: **não integradas**. Código
  produtivo: **não alterado**.

---

## 8. Estágio 2 — continua pendente, não iniciado nesta entrega

Sem mudança em relação ao relatório anterior: auditoria profunda de
scheduler/timezone além do já testado, testes de canais alternados sem
vazamento cruzado, testes de borda de Whisper/FFmpeg/Ollama com falha
real, persistência/restart ponta a ponta na Geração 1, smoke test
automatizado, e a decisão arquitetural sobre ligar Geração 1 à Geração 2
(que a auditoria independente reconfirmou: zero imports cruzados hoje).

Aguardando sua validação no Windows real (`RODAR_TESTES.bat` e
`EMPACOTAR_RELEASE.bat`, incluindo o teste com pasta `contas` fictícia
pedido) antes de seguir para o Estágio 2.
