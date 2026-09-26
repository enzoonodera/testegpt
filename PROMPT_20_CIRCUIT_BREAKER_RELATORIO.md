# PROMPT 20 — FILA RESILIENTE E CIRCUIT BREAKER

Rodada 2026-09-21. **Este NÃO é um prompt do gate GATE 19.5** (não altera
Geração 1) — é o próximo item do roadmap ("Prompt 20"), explicitamente
autorizado a começar pelo usuário nesta rodada, com a restrição explícita
de **NÃO iniciar o Prompt 21** (retry automático) e **NÃO alterar**
`agendar_youtube.py`/`agendar_tiktok.py` (Geração 1) nem
`domain/job_state_machine.py`/os invariantes já validados de
idempotência/pause/resume/cancel.

## 1. Arquivos criados

- `_sistema/circuit_breaker.py` — módulo novo: classe `CircuitBreaker`,
  `CircuitBreakerState`, constantes `STATE_CLOSED`/`STATE_OPEN`,
  `DEFAULT_FAILURE_THRESHOLD`/`DEFAULT_COOLDOWN_SECONDS`. Contém toda a
  investigação da seção 0 documentada em docstring de módulo (conector,
  distinção sistêmico/conteúdo, N/cooldown, half-open, persistência,
  concorrência).
- `_sistema/storage/migrations/m004_circuit_breaker.py` — migration 004:
  cria a tabela `circuit_breaker_state`.
- `tests/test_circuit_breaker.py` — 26 testes novos (detalhados na seção 7).
- Este relatório (`PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md`).

## 2. Arquivos modificados

- `_sistema/storage/migrations/__init__.py` — registra `MIGRATION_004` em
  `MIGRATIONS`; `LATEST_SCHEMA_VERSION` passa de 3 para 4.
- `_sistema/job_engine.py`:
  - importa `CircuitBreaker`;
  - `JobEngine.__init__` ganha o parâmetro opcional
    `circuit_breaker: CircuitBreaker | None = None` (colaborador opcional,
    `None` por padrão — comportamento idêntico ao de antes deste Prompt
    quando omitido, mesmo padrão de `control_manager`/
    `shutdown_coordinator`/`batch_engine`);
  - novo método estático `JobEngine._resolve_connector_key(job)` (resolve
    `job.extra["connector_key"]` por convenção, `None` quando ausente/
    inválido — nunca inventa identidade);
  - novo método `JobEngine._is_blocked_by_circuit_breaker(job, *,
    connection=None)`;
  - nova exceção `JobBlockedByCircuitBreakerError(JobNotActionableError)`;
  - `fetch_pending_jobs()` passa a excluir Jobs cujo connector está com o
    circuito aberto (quando `circuit_breaker` foi fornecido);
  - `_claim()` ganha o parâmetro `connector_key` e recusa a reivindicação
    (`JobBlockedByCircuitBreakerError`, Job permanece PENDING/RETRY) sob a
    MESMA transaction `BEGIN IMMEDIATE` já usada para batch/control/
    shutdown;
  - `advance()` passa a resolver `connector_key` do Job já carregado e
    repassar para `_claim`;
  - `admit_heavy_work()` ganha a mesma checagem (revalidação imediatamente
    antes do efeito pesado real), com o novo motivo de negação
    `"CIRCUIT_BREAKER_OPEN"` — que a landing logic já existente
    (`_land_after_admission_denied`) trata genericamente: `RETRY` para
    `claims_status == PROCESSING` (autoridade temporária, mesmo
    tratamento de pause/DRAINING/batch pause), `BLOCKED` para
    PUBLISHING/RECOVERING (inalterado, nenhuma mudança nesse destino foi
    necessária).
  - **Nenhuma linha de `_land_after_admission_denied`,
    `_finalize_processing_result`, `run_pending()`,
    `_land_safely_after_claim_failure` ou de qualquer parte do módulo
    fora do listado acima foi tocada.**
- `tests/test_sqlite_storage.py` — `EXPECTED_TABLES` ganha
  `"circuit_breaker_state"`; duas funções de teste renomeadas
  (`..._para_schema3...` → `..._para_schema4...`) e seus literais `3`
  atualizados para `4` (schema agora vai até a migration 004); lista de
  versões esperada em `[1, 2, 3]` → `[1, 2, 3, 4]` em dois pontos. Nenhum
  teste foi removido nem teve sua asserção enfraquecida — só passou a
  refletir o schema real (4 migrations em vez de 3), exatamente como o
  Round 3 já precisou corrigir uma asserção de `slugify()` desatualizada.
- `tests/test_backup_restore.py` — mesma classe de ajuste:
  `migration_versions == [1, 2, 3]` → `[1, 2, 3, 4]`; os dois blocos que
  simulavam "um binário futuro com schema 4 ainda não implementado" (via
  `monkeypatch` de `LATEST_SCHEMA_VERSION`) foram deslocados para simular
  schema 5 (já que 4 agora é real) — 3 funções renomeadas
  (`..._schema3_previous_schema4...` → `..._schema4_previous_schema5...`)
  e todos os literais internos (`3`→`4`, `4`→`5`) ajustados
  consistentemente, preservando exatamente o mesmo cenário testado
  ("restaurar um backup de schema mais antigo enquanto o banco atual já
  está num schema mais novo, sem nenhum crash").
- `tests/test_migrations_frozen.py` — adicionada conscientemente a entrada
  `"m004_circuit_breaker.py"` em `FROZEN_MIGRATION_HASHES` com o SHA-256
  real do arquivo (`2375542d...adbc`), exatamente como a docstring do
  próprio teste exige ("Alterar os hashes ... exige uma decisão consciente
  e documentada — nunca silenciosa"). m001/m002/m003 permanecem com os
  hashes já congelados, sem nenhuma alteração.

## 3. Comportamento novo

Um `CircuitBreaker` opcional, por **connector** (ver seção 4), que decide
apenas ADMITIR ou BLOQUEAR a reivindicação de novos Jobs daquele connector
— nunca decide `Job.status` sozinho (essa continua sendo autoridade
exclusiva do `JobEngine`/`OperationalAuditLog`/State Machine central).
Abre depois de N falhas SISTÊMICAS idênticas consecutivas reportadas para
aquele connector; enquanto aberto, nenhum Job novo desse connector é
admitido em `fetch_pending_jobs()`/`_claim()`/`admit_heavy_work()`, mas
Jobs de QUALQUER OUTRO connector continuam sendo processados normalmente
no MESMO `run_pending()`. Fecha automaticamente depois do cooldown
configurado (a próxima consulta de admissão já fecha e admite) ou
imediatamente se alguém chamar `record_success()` explicitamente. Todo
evento de abertura/fechamento é persistido em `audit_events`
(`entity_type="circuit_breaker"`).

**Importante, e deliberado**: este Prompt NÃO conecta automaticamente o
Circuit Breaker a nenhuma falha real de handler. Não existe ainda nenhum
Connector real (YouTube/TikTok concretos rodando dentro do JobEngine —
isso é Prompt 57-60, fora de escopo), então não há hoje nenhum handler que
possa classificar sozinho "isto foi uma falha sistêmica de conexão" vs
"isto foi um problema de conteúdo daquele vídeo". `CircuitBreaker.
record_success`/`record_failure(code=..., systemic=...)` são a API pública
que um Connector futuro (ou um teste que o simula, como
`tests/test_circuit_breaker.py`) chama depois de classificar o resultado
real — o Circuit Breaker em si nunca adivinha a classificação a partir de
uma exceção genérica. Isso é uma decisão consciente, não uma lacuna
escondida (ver seção 8, "riscos").

## 4. Decisões arquiteturais

### 4.1 Definição de "connector" adotada, com evidência

Investigação confirmada por leitura direta do código antes de qualquer
implementação (não assumida):

- `Job` (`domain/models.py`, linhas ~196-249) **não tem** `account_id` nem
  `platform`. `Publication` tem `account_id` (FK para `Account`).
  `Account` tem `platform`. **Não existe nenhuma FK, coluna ou convenção
  de id ligando `jobs` a `publications`/`accounts` hoje** — confirmado
  lendo o schema real em `_sistema/storage/migrations/m001_initial.py`
  (tabela `jobs`: `id, video_id, project_id, operation, status, progress,
  input_artifact_ids_json, output_artifact_ids_json, extra_json` — nenhuma
  coluna de conta/plataforma/publicação).
- `ControlManager` já documentou exatamente essa mesma lacuna para os
  escopos `ACCOUNT`/`PLATFORM` (persistidos, mas nunca aplicados
  automaticamente por `JobEngine` — `MANUALLY_ENFORCED_SCOPES`), e
  `JobEngine.admit_heavy_work` já tinha a mesma observação na docstring do
  módulo ("o modelo Job não referencia conta/plataforma hoje").

Diante disso, o "connector" deste Prompt é uma **string canônica fornecida
explicitamente por quem chama** (`"youtube"` para só plataforma,
`"youtube:canal-principal"` para plataforma+conta — sem formato imposto
pelo código além de "não vazia"). A ÚNICA integração automática com
`JobEngine` é por convenção, sem schema novo: quando presente,
`job.extra["connector_key"]` (o campo genérico `extra`/`extra_json` que
`Entity` já oferece a todo modelo, sem migration) é lido por
`JobEngine._resolve_connector_key` e usado para consultar o Circuit
Breaker antes de reivindicar o Job. Um Job sem essa chave **nunca** é
bloqueado por este caminho — mesma semântica já aprovada para
ACCOUNT/PLATFORM em `ControlManager`.

Isso é deliberadamente **não** uma FK nova em `Job`/`jobs` (fora de escopo
— pertence ao futuro Connector Contract, Prompts 57-60) e não tenta
adivinhar identidade a partir de heurística nenhuma.

### 4.2 `ErrorRecord` — por que não foi reaproveitado como contador

`ErrorRecord` (`domain/models.py`) já está persistido (tabela `errors`,
`ENTITY_SPECS` em `storage/database.py`), mas confirmado por grep: é
construído SOMENTE em `storage/legacy_migration.py` (migração de dados
legados) — o caminho de falha real de `JobEngine` nunca escreve nele hoje,
só grava em `audit_events`. Reaproveitá-lo para o contador do Circuit
Breaker exigiria varrer N linhas por `job_id` a cada decisão de admissão
(sem índice por connector) ou adicionar uma coluna nova em `errors` só
para isto — nenhuma das duas reaproveita a tabela sem alterá-la/duplicar
esforço. Por isso: tabela dedicada e indexada por `connector_key`
(`circuit_breaker_state`, migration 004) para o ESTADO; os EVENTOS de
abertura/fechamento reaproveitam `audit_events` (sem tabela nova) via
`LocalDatabase.append_audit_event`, com `connector_key` dentro de
`data_json` (nunca em `entity_id`, que exige UUID válido).

### 4.3 `BatchEngine` — por que as primitivas de pause não foram reaproveitadas

`pause_batch`/`resume_batch`/`is_batch_paused`/`is_job_admission_blocked`
pausam exclusivamente por `batch_id` (chave fixa
`f"batch:{batch_id}:paused"` em `settings`) — confirmado lendo
`batch_engine.py` linhas ~940-981. Generalizar essa chave para aceitar
qualquer string exigiria alterar um componente já validado (fora do
escopo autorizado — "NÃO mover/duplicar a lógica de pause/resume/cancel do
Batch já validada"), e tratar cada connector como um "batch" fictício
confundiria duas autoridades completamente diferentes (agrupamento de Jobs
relacionados vs. saúde de um connector remoto). Por isso o Circuit Breaker
implementa sua própria autoridade de admissão, integrada a `JobEngine`
pelo MESMO padrão de colaborador opcional já usado por
`control_manager`/`shutdown_coordinator`/`batch_engine` — sem tocar em
nenhuma linha de `batch_engine.py`.

### 4.4 N (limiar) e cooldown — valores e justificativa

`DEFAULT_FAILURE_THRESHOLD = 5`, `DEFAULT_COOLDOWN_SECONDS = 900` (15
minutos), ambos configuráveis por instância (`failure_threshold`/
`cooldown_seconds` no construtor de `CircuitBreaker` — nada hardcoded na
lógica de decisão).

Justificativa (ver docstring completa em `circuit_breaker.py`): como este
Prompt explicitamente não implementa retry automático (Prompt 21), cada
falha sistêmica registrada corresponde hoje a uma tentativa real e
completa. Um limiar baixo (1-2) abriria o circuito por uma única
instabilidade transitória de rede; um limiar alto (20+) martelaria por
muito tempo um connector genuinamente quebrado (ex.: sessão expirada)
antes de proteger a fila. 5 falhas IDÊNTICAS consecutivas é o valor mínimo
que ainda distingue de forma robusta "isto está realmente quebrado" de
"uma instabilidade pontual". 900s dá tempo para a maioria das
instabilidades transitórias reais se resolverem sozinhas, ou para o
usuário corrigir manualmente (ex.: relogar) sem deixar o connector
bloqueado por horas.

### 4.5 Half-open — decisão explícita de NÃO implementar nesta rodada

Implementado apenas cooldown simples: depois de `cooldown_seconds`, a
PRÓXIMA `is_admitted()` já fecha o circuito e admite normalmente, sem
reservar "1 Job de teste" isolado dos demais. Um half-open real exigiria
selecionar QUAL Job de um connector é "o de teste" em meio a
`run_pending()` processando vários Jobs do mesmo connector sequencialmente
— e isso exigiria um estado transitório novo (uma "sonda em voo") sem
nenhum consumidor real ainda (retry automático, Prompt 21) que dependa
dele, tensionando com o princípio "AUTORIDADE ÚNICA" (uma segunda fonte de
verdade sobre o que é uma tentativa válida). Cooldown simples é mais fácil
de raciocinar, testar deterministicamente (`FakeClock` injetável) e
reavaliar quando o Prompt 21 existir. Documentado com o raciocínio
completo em `circuit_breaker.py`.

### 4.6 Distinção sistêmico vs. conteúdo — critério e exemplos

Quem chama `record_failure()` classifica EXPLICITAMENTE via
`systemic=True/False` — o Circuit Breaker nunca tenta adivinhar a partir
do `code` sozinho.

- **SISTÊMICO** (conta): timeout/falha de conexão; sessão/login expirado
  (`auth_expired`, `login_required`); elemento de navegação ESSENCIAL da
  própria plataforma não encontrado (não um elemento de um vídeo
  específico); erro 5xx/serviço indisponível; rede local indisponível.
- **CONTEÚDO** (nunca conta, mesmo repetido 3x com itens diferentes por
  coincidência): vídeo com formato/codec inválido; título/descrição excede
  limite; direitos autorais detectados NAQUELE vídeo (ortogonal à política
  de copyright das rodadas GATE 19.5); vídeo duplicado.

E, adicionalmente ao critério pedido pelo Prompt: "N consecutivas
IDÊNTICAS" significa mesmo `code` — uma falha sistêmica com `code`
diferente da anterior é sistêmica, mas reinicia a contagem em 1 (não
acumula com a anterior), porque não é a MESMA razão repetida. Provado por
`test_falha_sistemica_com_code_diferente_reinicia_a_contagem_em_1`.

### 4.7 Persistência e restart

Estado em SQLite, tabela `circuit_breaker_state` (migration 004) —
`connector_key` (PK), `state`, `consecutive_failures`,
`last_failure_code`, `last_failure_at` (TEXT ISO, diagnóstico),
`opened_at`/`opened_at_epoch`, `retry_eligible_at_epoch` (REAL, epoch —
comparação determinística contra um `clock` injetável, sem parse de string
a cada decisão). Sobrevive a restart: `test_estado_aberto_sobrevive_a_
restart_com_novas_instancias`/`test_estado_fechado_sobrevive_a_restart_
com_novas_instancias` recriam `LocalDatabase` E `CircuitBreaker` do zero
contra o MESMO arquivo (CLAUDE.md ponto 4 — nunca reuso de objeto em
memória).

## 5. Migrations

`m004_circuit_breaker.py` (versão 4) — cria somente `circuit_breaker_state`
e seu índice por `state`; não altera `jobs`/`accounts`/`publications`
(m001), `audit_events` (m001/m002) nem `batches`/`batch_jobs` (m003). Hash
SHA-256 adicionado conscientemente a
`tests/test_migrations_frozen.py::FROZEN_MIGRATION_HASHES`. m001/m002/m003
permanecem byte-idênticas (confirmado: seus hashes já congelados
continuam batendo).

## 6. Confirmação: Geração 1 e `job_state_machine.py` intocados

`sha256sum _sistema/agendar_youtube.py _sistema/agendar_tiktok.py
_sistema/domain/job_state_machine.py` conferido nesta rodada — nenhum dos
três arquivos foi aberto para escrita/edição em nenhum momento (somente os
9 arquivos listados nas seções 1/2 foram criados ou modificados). Nenhuma
linha de `control_manager.py`, `resource_manager.py`, `batch_engine.py`
(fora de `job_engine.py`, que É Geração 2 e É o escopo autorizado) foi
tocada.

## 7. Testes automatizados

`tests/test_circuit_breaker.py` — 26 testes novos, organizados em:

- **Unidade do `CircuitBreaker`** (17 testes): connector nunca visto está
  fechado por padrão; abre só ao atingir o threshold com falhas
  IDÊNTICAS consecutivas; `code` diferente reinicia a contagem em 1; falha
  de CONTEÚDO nunca conta (mesmo 10x repetida); falha de conteúdo
  intercalada não atrapalha a contagem sistêmica; sucesso reseta o
  contador; sequência falha→sucesso→falha→falha não abre; falha enquanto
  já aberto não reabre nem duplica o evento; cooldown ainda não decorrido
  mantém bloqueado; cooldown decorrido fecha automaticamente na próxima
  consulta (com evento CLOSED e `open_duration_seconds` corretos);
  `get_state()` é leitura pura (nunca fecha sozinha); sucesso explícito
  fecha antes do cooldown; evento de abertura é persistido e observável
  (todos os campos); construtor rejeita threshold/cooldown inválidos;
  `connector_key`/`code` vazios são rejeitados.
- **Restart** (2 testes, CLAUDE.md ponto 4): estado aberto e estado
  fechado sobrevivem a novas instâncias de `LocalDatabase`+
  `CircuitBreaker` contra o mesmo arquivo.
- **Concorrência** (2 testes, CLAUDE.md ponto 6): `threading.Barrier`
  determinístico, nunca probabilístico — N threads/instâncias registrando
  falha simultaneamente contra o mesmo SQLite nunca perdem contagem
  (exatamente 1 abre o circuito, contador final == N, exatamente 1 evento
  OPENED); N threads/instâncias fechando por cooldown simultaneamente
  nunca duplicam o evento CLOSED.
- **Integração com `JobEngine`** (3 testes): `_resolve_connector_key`
  cobre ausente/vazio/tipo errado/válido; Job sem `connector_key` nunca é
  bloqueado mesmo com outros circuitos abertos; Job com connector aberto
  não é reivindicado nem por `fetch_pending_jobs()` nem por `advance()`
  direto (permanece PENDING); Job com connector fechado é reivindicado
  normalmente.
- **Os 3 isolamentos obrigatórios da seção 1.1** (4 testes):
  - **Job x Job**: NÃO reimplementado — aponta para o teste já existente
    e inequívoco `tests/test_job_engine.py::test_run_pending_isola_falha_
    de_um_job_e_continua_os_demais` (confirmado passando nesta rodada,
    sem alteração).
  - **Conta x conta** (`test_isolamento_conta_x_conta_...`): circuito
    aberto para `"youtube:conta-a"` (2 falhas sistêmicas idênticas) não
    impede que `"youtube:conta-b"` seja processado no MESMO
    `run_pending()` — prova direta e inequívoca, nova (não existia
    nenhuma antes, porque nenhum código acoplava Job a conta até este
    Prompt).
  - **Plataforma x plataforma** (`test_isolamento_plataforma_x_
    plataforma_...`): circuito aberto para `"tiktok"` não impede que
    `"youtube"` seja processado no MESMO `run_pending()` — mesma estrutura,
    nova.
  - Reabertura após cooldown dentro de um `run_pending()` real (o Job
    volta a ser processado sem intervenção manual).

## 8. Suíte completa / compileall

- `python3 -m compileall -q _sistema tests` → **sucesso** (sem erros).
- Todos os 45 arquivos de teste rodados individualmente (mesma disciplina
  já estabelecida: `/root/.local/bin/pytest -q <arquivo>`, um por vez) →
  **1250 passed, 0 failed** (era 1224 antes desta rodada; +26 = exatamente
  os testes novos de `test_circuit_breaker.py`; nenhum outro arquivo
  ganhou ou perdeu teste). `tests/test_job_engine.py`,
  `tests/test_batch_engine.py`, `tests/test_control_manager.py`,
  `tests/test_resource_manager.py`, `tests/test_shutdown_coordinator.py`,
  `tests/test_recovery_manager.py` confirmados passando sem alteração
  (mostrando que a integração no `JobEngine` não regrediu nenhuma
  garantia já aprovada dessas suítes).
- `tests/test_sqlite_storage.py`, `tests/test_backup_restore.py`,
  `tests/test_migrations_frozen.py` — atualizados conscientemente (seção
  2) e confirmados passando: 22, 27 e 2 testes respectivamente (mesma
  contagem de antes — só as asserções internas mudaram para refletir o
  schema real de 4 migrations, nenhum teste removido).

## 9. Como testar manualmente

Não há UI nova para este Prompt (é puramente Geração 2/backend, sem
integração com `painel_oficial.py`). Verificação manual sugerida via
console Python, no diretório do projeto:

```python
from _sistema.storage import LocalDatabase
from _sistema.circuit_breaker import CircuitBreaker

db = LocalDatabase()
db.initialize()
breaker = CircuitBreaker(db, failure_threshold=5, cooldown_seconds=900)

breaker.record_failure("youtube:minha-conta", code="connection_timeout", systemic=True)
# repetir mais 4 vezes com o mesmo code -> circuito abre na 5a chamada
print(breaker.get_state("youtube:minha-conta"))
print(breaker.is_admitted("youtube:minha-conta"))  # False enquanto aberto
```

Reiniciar o processo Python e repetir a leitura confirma que o estado
persiste (mesmo arquivo `painel.db`).

## 10. Riscos conhecidos e dívida técnica

- **Nenhum Connector real usa isto ainda.** `record_success`/
  `record_failure` são chamados apenas por testes nesta rodada — a
  classificação sistêmico-vs-conteúdo de verdade só existirá quando um
  handler concreto (YouTube/TikTok dentro do JobEngine, Prompts 57-60) for
  implementado e puder decidir isso caso a caso. Até lá, o Circuit Breaker
  é uma autoridade de admissão completa e testada, mas SEM nenhum produtor
  de eventos real ainda — é infraestrutura pronta, não um comportamento
  visível ao usuário final nesta rodada.
- **`job.extra["connector_key"]` é uma convenção solta, não um contrato
  tipado.** Qualquer código que crie Jobs precisa lembrar de preenchê-la
  para que o Circuit Breaker (e, no futuro, os escopos ACCOUNT/PLATFORM de
  `ControlManager`) façam efeito. Isso é deliberado (evita uma FK/migration
  em `Job` antes do Connector Contract real existir), mas é dívida técnica
  explícita: quando os Prompts 57-60 definirem o Connector real, uma
  revisão deve decidir se `connector_key` continua em `extra` ou migra
  para um campo de primeira classe.
- **Cooldown fixo não se estende com falhas adicionais enquanto já
  aberto.** Uma falha sistêmica reportada enquanto o circuito já está
  `OPEN` só atualiza metadados de diagnóstico (`last_failure_code`/
  `last_failure_at`), nunca empurra `retry_eligible_at_epoch` para
  mais tarde. Decisão consciente (mantém o comportamento previsível e
  testável), mas significa que um connector que continua ativamente
  quebrado depois do cooldown vai ser readmitido, falhar de novo, e abrir
  de novo — sem um "backoff exponencial" entre aberturas sucessivas. Isso
  é aceitável para este Prompt (sem retry automático, cada readmissão
  corresponde a uma tentativa real de um Job diferente), mas deve ser
  revisitado quando o Prompt 21 (retry) existir.
- **Ambiguidade sistêmico-vs-conteúdo permanece uma decisão HUMANA/de
  handler, nunca automática** — por design. Não há hoje, e este módulo não
  cria, nenhuma heurística de string-matching sobre mensagens de erro para
  tentar adivinhar a classificação. Isso é intencional (uma heurística
  errada seria pior que exigir a classificação explícita), mas significa
  que a QUALIDADE da proteção do Circuit Breaker depende inteiramente da
  qualidade da classificação feita pelo futuro Connector — um Connector
  mal implementado que classificar erros de conteúdo como sistêmicos (ou
  vice-versa) anula a proteção sem que o Circuit Breaker tenha como
  detectar isso sozinho.
- **Sem half-open** (ver seção 4.5) — reavaliar quando o Prompt 21 existir.

## 11. Pendências

- Nenhuma pendência dentro do escopo autorizado desta rodada. Os três
  isolamentos exigidos pela seção 1.1 estão todos providos de teste
  direto e inequívoco (seção 7) — a condição explícita de não-conclusão
  do Prompt está satisfeita.
- **NÃO iniciado o Prompt 21** (retry automático), conforme instruído.
- **Geração 1 não tocada.**
