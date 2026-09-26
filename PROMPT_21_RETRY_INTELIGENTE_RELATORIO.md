# PROMPT 21 — RETRY INTELIGENTE — Relatório de Entrega

## 1. Arquivos criados

- `_sistema/retry_policy.py` — `RetryPolicy`, `ClassifiedJobFailure`, as sete
  constantes de categoria (`RETRY_CATEGORY_TEMPORARY`, `_PERMANENT`,
  `_RATE_LIMIT`, `_AUTH_REQUIRED`, `_CONTENT_BLOCKED`,
  `_USER_ACTION_REQUIRED`, `_UNKNOWN`), `RetryState`, `RetryDecision`,
  defaults de backoff/limite.
- `_sistema/storage/migrations/m005_retry_policy.py` — nova migration
  (`version=5`), cria a tabela `job_retry_state` + índice
  `idx_job_retry_state_next_retry`.
- `tests/test_retry_policy.py` — 35 testes novos cobrindo unidade,
  integração com `JobEngine` e não-regressão de `BatchEngine.retry_failed()`.
- `PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `_sistema/storage/migrations/__init__.py` — registra `MIGRATION_005`;
  `LATEST_SCHEMA_VERSION` passa de 4 para 5 (derivado automaticamente).
- `_sistema/job_engine.py`:
  - novo parâmetro opcional `retry_policy: RetryPolicy | None = None` em
    `JobEngine.__init__` (colaborador independente, `None` por padrão —
    nenhuma mudança de comportamento quando omitido);
  - nova exceção `JobBlockedByRetryScheduleError`;
  - novo método `_is_blocked_by_retry_schedule`, usado em
    `fetch_pending_jobs` (pré-filtro de leitura) e `_claim` (autoridade
    real, sob a mesma `BEGIN IMMEDIATE`);
  - novo método `_land_with_retry_policy`, chamado a partir do
    `except Exception as exc:` de `advance()` quando `exc` é uma
    `ClassifiedJobFailure` **e** `self.retry_policy is not None` — em
    qualquer outro caso, o caminho cego pré-existente
    (`_land_safely_after_claim_failure`) continua exatamente igual.
- `_sistema/batch_engine.py`:
  - `retry_failed()` agora chama `self.job_engine.retry_policy.reset(job_id)`
    (guardado por `is not None`) imediatamente ANTES de
    `self.audit_log.retry(job_id, ...)`, reiniciando o contador de
    tentativas automáticas para o novo ciclo manual.
- `empacotar_release.py` — `ALLOWED_ROOT_FILES` ganhou
  `PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md` (lacuna pré-existente da rodada
  anterior, nunca fechada) e `PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md`.
- `tests/test_sqlite_storage.py`, `tests/test_backup_restore.py`,
  `tests/test_migrations_frozen.py` — atualizados conscientemente para
  refletir `LATEST_SCHEMA_VERSION == 5` (mesmo padrão já usado no Prompt 20
  para a virada 3→4): nomes de teste renomeados de "schema4"/"schema5" para
  "schema5"/"schema6" onde aplicável, listas de versões de migration
  estendidas, hash SHA-256 de `m005_retry_policy.py` adicionado à lista
  congelada em `test_migrations_frozen.py` com justificativa na docstring.
  Nenhum teste foi enfraquecido — todas as asserções continuam verificando
  exatamente o que verificavam antes, apenas com o número certo.

## 3. Comportamento novo

Um handler pode agora classificar explicitamente a causa de uma falha
levantando `ClassifiedJobFailure(category, message=...)`. Se o `JobEngine`
tiver um `retry_policy` configurado, `advance()` roteia essa falha para
`RetryPolicy.record_failure()`, que decide:

- **TEMPORARY / RATE_LIMIT / UNKNOWN** → tentativa automática agendada
  (`RETRY` + `next_retry_at_epoch` com backoff exponencial + jitter), até o
  limite configurável de tentativas; esgotado o limite, `FAILED`
  definitivo (sem novo agendamento).
- **PERMANENT / CONTENT_BLOCKED** → `FAILED` direto, nunca uma tentativa
  automática (limite efetivo = 0).
- **AUTH_REQUIRED / USER_ACTION_REQUIRED** → pousa no estado dedicado já
  existente (`AUTH_REQUIRED`/`USER_ACTION_REQUIRED`), sem agendar retry —
  só uma ação externa (fora de escopo) resolve o pendente.

Um Job `RETRY` com `next_retry_at_epoch` no futuro não pode ser
reivindicado antes desse horário (`fetch_pending_jobs`/`_claim`), mas um
Job `RETRY` sem agendamento (o caminho manual pré-existente de
`retry_failed()`) continua imediatamente elegível — nenhuma regressão.

**Salvaguarda CLAUDE.md princípio M**: `RetryPolicy` é cego a
`claims_status`; é `JobEngine._land_with_retry_policy` quem aplica o filtro
— um pouso `RETRY` só vira `RETRY` de verdade quando `claims_status ==
PROCESSING` (trabalho local). Para `PUBLISHING`/`RECOVERING`, mesmo uma
categoria "retry-able" cai no mesmo fallback seguro já aprovado
(`UNKNOWN`), exceto `AUTH_REQUIRED`/`USER_ACTION_REQUIRED`, que são seguros
para qualquer `claims_status`.

## 4. Decisões arquiteturais

### 4.1 Mapeamento categoria → pouso

Adotado exatamente o mapeamento sugerido pelo Prompt (seção 1.1), sem
divergência: `TEMPORARY`/`RATE_LIMIT`/`UNKNOWN` → retry automático com
backoff até o limite, depois `FAILED`; `PERMANENT`/`CONTENT_BLOCKED` →
`FAILED` direto (limite efetivo zero); `AUTH_REQUIRED`/
`USER_ACTION_REQUIRED` → estado dedicado, sem agendamento. Justificativa:
o Prompt já apresentou este mapeamento com fundamentação equivalente ao
já aprovado no Circuit Breaker ("falha de conteúdo nunca conta") — não
havia motivo técnico para divergir.

### 4.2 Backoff + jitter

Fórmula: `delay = min(base * factor^(attempt-1), max_backoff) +
jitter_extra`, onde `jitter_extra = delay_base * jitter_ratio *
random()` (estritamente aditivo — nunca reduz abaixo da curva
exponencial pura, evitando que o jitter acidentalmente crie uma tentativa
*mais cedo* que o backoff pretendido).

Defaults escolhidos e justificados (independentemente do Circuit
Breaker — problema diferente: aqui é backoff por tentativa de UM Job, lá
era um limiar de falhas consecutivas de UM connector):

- `base_seconds=30.0`: primeira nova tentativa em ~30s — rápido o
  suficiente para não represar filas de publicação, devagar o suficiente
  para não martelar uma falha de rede momentânea.
- `factor=2.0`: crescimento exponencial padrão (30s, 60s, 120s, 240s...) —
  cede espaço rapidamente para uma instabilidade real sem exigir uma
  fórmula mais sofisticada.
- `max_backoff_seconds=1800.0` (30 min): teto que evita uma espera
  impraticavelmente longa entre tentativas dentro do mesmo ciclo
  automático, mantendo o produto responsivo mesmo sob falha persistente
  (mas ainda dentro do limite de tentativas).
- `jitter_ratio=0.2` (20%): suficiente para descorrelacionar múltiplos
  Jobs do mesmo connector que falharam juntos (ex.: instabilidade de
  rede afetando vários vídeos ao mesmo tempo) sem distorcer
  significativamente a curva de backoff pretendida.
- `max_attempts=5`: teto conservador e explícito de "no infinite retry" —
  finito, configurável, nunca resetado sozinho (só via `reset()`
  manual).

### 4.3 Persistência: tabela dedicada `job_retry_state`, não `Job.extra`

Contrastado explicitamente com a decisão do Circuit Breaker (m004,
`circuit_breaker_state`): o mesmo argumento que levou o Circuit Breaker a
preferir uma coluna real (não JSON) se aplica aqui — `next_retry_at_epoch`
é comparado contra o relógio a cada `fetch_pending_jobs()`/`_claim()`, e um
valor dentro de um `TEXT`/JSON (`Job.extra`) não é indexável nem comparável
eficientemente pelo SQLite sem decodificação em Python linha a linha.
Diferente do Circuit Breaker (que é por CONNECTOR, uma entidade nova sem
tabela própria), este Prompt tinha a opção adicional de usar `Job.extra`
(campo genérico já existente na entidade `Job`) — rejeitada pelo mesmo
motivo da comparação por relógio, e também porque `OperationalAuditLog.
transition_job`'s parâmetro `data=` (o único canal hoje para levar dados
extras numa transição) grava exclusivamente em `audit_events`, nunca em
`job.extra` — usar `Job.extra` exigiria uma mudança mais profunda na
máquina de transição já aprovada, fora do escopo autorizado deste Prompt.
Uma coluna nova literal em `jobs` (opção (a) do Prompt) foi rejeitada pelo
mesmo motivo de raio de impacto já usado para preferir uma tabela nova no
Circuit Breaker: alterar a dataclass `Job` compartilhada por toda a
Geração 2 para um dado que só faz sentido para o subsistema de retry.
`job_retry_state.job_id` é `TEXT PRIMARY KEY` sem `FOREIGN KEY` para
`jobs(id)` (mesma escolha de `circuit_breaker_state.connector_key`):
mantém o módulo testável isoladamente e evita semântica de
`ON DELETE`/`ON UPDATE` sobre uma entidade que este produto nunca apaga
fisicamente.

### 4.4 Canal handler → RetryPolicy: `ClassifiedJobFailure`

Mesmo princípio do `systemic=` explícito do Circuit Breaker: nenhuma
heurística tentando adivinhar a categoria a partir do texto de uma
exceção genérica. Um handler que quer retry inteligente levanta
`ClassifiedJobFailure(category, message=...)`; qualquer outra exceção
(comportamento de todo handler hoje) cai exatamente no caminho cego
pré-existente — confirmado por teste explícito
(`test_handler_sem_categoria_continua_caindo_em_failed_sem_retry_automatico`)
que nenhuma linha é sequer criada em `job_retry_state` nesse caso. Também
confirmado que uma `ClassifiedJobFailure` sem `retry_policy` configurado
no `JobEngine` cai no mesmo fallback cego (item 1.6, não há retry
automático "de graça" por omissão de configuração).

### 4.5 Atomicidade (correção feita durante a implementação)

A decisão de `RetryPolicy` (contador/agendamento em `job_retry_state`) e a
transição de status do Job (`audit_log.transition_job`) rodam sob a MESMA
`BEGIN IMMEDIATE` em `_land_with_retry_policy` (via `connection=conn`
roteado explicitamente) — evita a janela de crash em que
`job_retry_state` já reflete a nova decisão mas o Job ficaria preso para
sempre no status reivindicado (`PROCESSING`/`PUBLISHING`/`RECOVERING`).
`record_error` (diagnóstico, não autoritativo) permanece em transação
própria, best-effort, mesmo padrão já aprovado em
`_land_safely_after_claim_failure`.

## 5. Migration

`m005_retry_policy.py` (`version=5`): cria `job_retry_state` (PK `job_id`,
`attempt_count`, `next_retry_at_epoch`, `last_failure_category`,
`updated_at`) + índice em `next_retry_at_epoch`. Não altera nenhuma tabela
existente. Hash SHA-256 congelado em `tests/test_migrations_frozen.py`.

## 6. Confirmações explícitas exigidas pelo Prompt

- **CircuitBreaker (Prompt 20) não foi alterado nem chamado por
  RetryPolicy**: confirmado por hash SHA-256 (`_sistema/circuit_breaker.py`
  idêntico ao início desta rodada — nenhum `Edit`/`Write` foi executado
  contra este arquivo) e por teste automatizado
  (`test_retry_policy_nunca_importa_nem_referencia_circuit_breaker`, via
  AST) que garante que `retry_policy.py` nunca importa, referencia
  atributo ou chama nada de `circuit_breaker`.
- **`retry_failed()` manual continua funcionando sem mudança de
  comportamento** e agora reseta `attempt_count` para o novo ciclo —
  confirmado por `test_retry_failed_continua_funcionando_sem_mudanca_de_
  comportamento`, `test_retry_failed_reinicia_attempt_count_para_o_novo_
  ciclo` e `test_retry_failed_sem_retry_policy_configurado_continua_
  identico`.
- **Geração 1 intocada**: `_sistema/agendar_youtube.py` e
  `_sistema/agendar_tiktok.py` não foram lidos nem editados nesta rodada
  (confirmado — nenhuma chamada `Read`/`Edit`/`Write` contra eles neste
  Prompt).
- **`domain/job_state_machine.py` intocado**: nenhum estado novo, nenhuma
  transição alterada — confirmado por leitura (não escrita) no início da
  rodada.

## 7. Testes automatizados

- `tests/test_retry_policy.py`: **35 testes**, cobrindo as 7 categorias
  (pouso + presença/ausência de `next_retry_at_epoch`), backoff
  determinístico (crescimento, teto, jitter aditivo), esgotamento do
  limite (com teste explícito de que a tentativa SEGUINTE ao esgotamento
  não volta a agendar), `reset()` reiniciando o ciclo, elegibilidade
  (`is_eligible_now`/gate em `fetch_pending_jobs`/`_claim`, com e sem
  agendamento), não-regressão de handler sem categoria, salvaguarda de
  `claims_status` (PUBLISHING/RECOVERING vs. PROCESSING), integração com
  `BatchEngine.retry_failed()`, concorrência (Barrier, mesmo SQLite) e
  restart (novas instâncias de `LocalDatabase`/`RetryPolicy`).
- Suíte completa (incluindo os 3 arquivos de schema-version atualizados
  conscientemente e `test_migrations_frozen.py`): **1285 passed, 36
  subtests passed**, 0 falhas.
- `python3 -m compileall -q _sistema tests`: **OK**, sem erros de sintaxe
  ou import.

## 8. Como testar manualmente

1. Rodar `RODAR_TESTES.bat` (ou `pytest -q tests/`) e confirmar 1285
   passed.
2. Registrar um handler que levanta
   `ClassifiedJobFailure(RETRY_CATEGORY_TEMPORARY)`, configurar um
   `JobEngine` com `retry_policy=RetryPolicy(database)`, chamar
   `advance()` repetidamente e observar o Job alternando `RETRY` →
   (após `next_retry_at_epoch`) reivindicado de novo → `RETRY` → ... até
   `FAILED` no limite de tentativas.
3. Confirmar que `BatchEngine.retry_failed()` continua funcionando
   normalmente sobre Jobs `FAILED`, mesmo sem `retry_policy` configurado.

## 9. Riscos conhecidos e dívida técnica

- Nenhum Connector real (Prompts 57-60) usa `ClassifiedJobFailure` ainda
  — mesma ressalva já registrada no relatório do Circuit Breaker (Prompt
  20): a integração real só será testada de ponta a ponta quando um
  handler de publicação de verdade existir.
- `BatchEngine` não valida "mesmo SQLite" para `job_engine.retry_policy`
  (mesma lacuna pré-existente e já documentada para
  `job_engine.circuit_breaker`, nunca fechada no Prompt 20) — um
  `retry_policy` apontando para outro arquivo causaria split-brain
  silencioso. Registrado como dívida técnica herdada, não introduzida
  por este Prompt; recomenda-se fechar as duas lacunas juntas em uma
  rodada futura dedicada a hardening de `BatchEngine`.
- `record_error`/`transition_job` em `_land_with_retry_policy` continuam
  não-atômicas entre si (diagnóstico vs. transição autoritativa) — mesmo
  padrão já aprovado em `_land_safely_after_claim_failure`, não uma
  regressão nova.

## 10. Pendências

Nenhuma pendência dentro do escopo autorizado deste Prompt. Não iniciado
Prompt 22, conforme instrução explícita.
