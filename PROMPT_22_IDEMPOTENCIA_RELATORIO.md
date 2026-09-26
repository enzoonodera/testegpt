# PROMPT 22 — IDEMPOTÊNCIA — Relatório de Entrega

## 1. Arquivos criados

- `_sistema/publication_idempotency.py` — `PublicationIdempotencyGuard`,
  `compute_idempotency_key`, `IdempotencyCheck`, as três constantes de
  resultado (`OUTCOME_NUNCA_TENTADO`, `OUTCOME_JA_PUBLICADO`,
  `OUTCOME_INCONCLUSIVO`), `PublicationIdempotencyError`,
  `PublicationIdempotencyConflictError`.
- `_sistema/storage/migrations/m006_publication_idempotency.py` — nova
  migration (`version=6`), primeira do projeto a alterar uma tabela
  existente (`ALTER TABLE publications ADD COLUMN idempotency_key TEXT`)
  em vez de criar uma nova, mais um índice único parcial.
- `tests/test_publication_idempotency.py` — 26 testes cobrindo as três
  etapas do fluxo, concorrência, restart, não-interferência e não-regressão.
- `PROMPT_22_IDEMPOTENCIA_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `_sistema/domain/models.py` — `Publication` ganhou o campo
  `idempotency_key: str | None = None` (último campo, com default —
  aditivo, não quebra nenhuma construção existente, incluindo
  `legacy_migration.py`, que nunca o preenche).
- `_sistema/storage/migrations/__init__.py` — registra `MIGRATION_006`;
  `LATEST_SCHEMA_VERSION` passa de 5 para 6 (derivado automaticamente).
- `empacotar_release.py` — `ALLOWED_ROOT_FILES` ganhou
  `PROMPT_22_IDEMPOTENCIA_RELATORIO.md`.
- `tests/test_sqlite_storage.py`, `tests/test_backup_restore.py`,
  `tests/test_migrations_frozen.py` — atualizados conscientemente para
  refletir `LATEST_SCHEMA_VERSION == 6` (mesmo padrão já usado nos Prompts
  20/21 para as viradas anteriores): nomes de teste renomeados
  ("schema5"/"schema6" → "schema6"/"schema7" onde aplicável, já que a
  "sonda" de schema futuro usada pelos testes de restore precisou avançar
  de 6 para 7 para não colidir com a migration real nova), listas de
  versões de migration estendidas, hash SHA-256 de
  `m006_publication_idempotency.py` adicionado à lista congelada em
  `test_migrations_frozen.py` com justificativa na docstring. Nenhum teste
  foi enfraquecido — todas as asserções continuam verificando exatamente o
  que verificavam antes, apenas com o número certo.

## 3. Comportamento novo

Antes de publicar (ou de decidir se deve publicar), quem chamar
`PublicationIdempotencyGuard.claim(idempotency_key, ...)` recebe um dos
três resultados distintos e nunca ambíguos:

- **`NUNCA_TENTADO`**: nenhuma `Publication` conhecida para esta chave —
  uma `Publication` placeholder (`status="PENDING"`, `remote_id=None`) é
  reservada atomicamente, e o chamador está livre para tentar a
  publicação real.
- **`JA_PUBLICADO`**: existe uma `Publication` conhecida com `remote_id`
  preenchido (prova de sucesso já confirmado) — nada é escrito; o
  chamador recebe a publicação existente e NUNCA deve publicar de novo.
- **`INCONCLUSIVO`**: existe uma `Publication` conhecida SEM `remote_id`
  (uma tentativa anterior começou e não se sabe se terminou — o cenário
  "crash pós-upload") — nada é escrito; o chamador NÃO deve publicar de
  novo (arriscaria duplicar) nem considerar sucesso. É uma pendência que
  exige decisão externa.

`PublicationIdempotencyGuard.check(idempotency_key)` oferece a mesma
consulta em modo somente-leitura (nunca reserva nada), útil para
diagnóstico. `mark_published(publication_id, remote_id=...)` fecha o ciclo
quando um sucesso real é confirmado (nenhuma chamada de rede acontece
aqui — é só persistência de uma prova que o chamador já obteve).

## 4. Decisões arquiteturais

### 4.1 Fórmula(s) de chave de idempotência

`compute_idempotency_key(*, operation, account_id, video_id)` é um HELPER
opcional oferecendo a fórmula default sugerida pelo roadmap
(`video_id + account_id + operation`), devolvida como um SHA-256 hex (64
chars) de uma codificação JSON canônica dos três componentes — não uma
concatenação ingênua por separador, para eliminar qualquer ambiguidade de
colisão entre componentes que viessem a conter o separador no futuro.

Ela é **parametrizável por operação, não fixa**: nenhum método de
`PublicationIdempotencyGuard` calcula uma chave sozinho — todos tomam
`idempotency_key: str` explícito (mesmo princípio já usado em
`systemic=`/`category=` nos Prompts 20/21). `compute_idempotency_key` é só
uma conveniência; uma republicação de descrição/thumbnail, por exemplo,
pode compor sua própria fórmula (incluindo, por exemplo, um hash do
conteúdo específico sendo republicado) sem qualquer mudança neste módulo.
Testado explicitamente
(`test_mesma_chave_base_operation_diferente_nao_colide`): a mesma conta e
o mesmo vídeo, mas `operation` diferente, produzem chaves diferentes e
nunca colidem.

### 4.2 Migration: coluna dedicada em `Publication`, não tabela nova

Contrastado explicitamente com as decisões dos Prompts 20/21 (tabelas
NOVAS e independentes — `circuit_breaker_state`, `job_retry_state`):
aqueles casos precisavam de um lugar para um subsistema OBSERVAR de fora
uma entidade (connector, Job) sem alterar a dataclass compartilhada. Aqui
o roadmap pede explicitamente (item 1.1) uma coluna NA PRÓPRIA
`Publication`, e isso é coerente com a natureza do dado: a chave de
idempotência é uma PROPRIEDADE da própria `Publication` (qual tentativa de
publicação ela representa), exatamente como `remote_id`/`status` já são —
não um estado auxiliar de outro subsistema. `ALTER TABLE` (primeira vez
neste projeto, m001-m005 sempre usaram `CREATE TABLE`) porque
`publications` já existe desde m001.

### 4.3 Índice único PARCIAL como proteção real

`CREATE UNIQUE INDEX idx_publications_idempotency_key ON
publications(idempotency_key) WHERE idempotency_key IS NOT NULL` — mesmo
padrão já usado por `idx_schedules_publication_id` (m001). Permite
múltiplas `Publication`s com `idempotency_key IS NULL` (dados legados
importados por `legacy_migration.py`, que nunca preenchem esta coluna)
enquanto GARANTE, no nível do banco, que no máximo uma `Publication` exista
por chave não nula — a proteção real (roadmap item 1.1: "tem que ser
garantida pelo BANCO, não só por uma checagem em Python"), nunca
contornável por uma corrida entre duas transactions concorrentes.
`claim()` fecha o TOCTOU por software também (SELECT+INSERT na mesma
`BEGIN IMMEDIATE`, mesmo padrão TOCTOU-safe já aprovado em
`CircuitBreaker`/`RetryPolicy`), mas o índice único continua sendo a
última linha de defesa — provado por dois testes de concorrência
distintos: um via `create_publication()` (o caminho "ingênuo", sem SELECT
prévio, 8 threads disputando a mesma chave — exatamente 1 sobrevive, as
outras 7 recebem `PublicationIdempotencyConflictError`) e outro via
`claim()` (o caminho de produção real, TOCTOU-safe — exatamente 1
`NUNCA_TENTADO`, as demais `INCONCLUSIVO`).

### 4.4 Nenhuma FK nova entre `Job` e `Publication`

Confirmado por investigação (item 0 do Prompt) e por teste explícito
(`test_publication_nunca_cria_job_id_nem_fk_nova_para_job`): `Publication`
continua sem `job_id`, `Job` continua sem `publication_id`. O contrato de
idempotência funciona inteiramente em termos dos campos que `Publication`
já tem — a ligação real com um Job específico só existirá quando um
Connector concreto (Prompts 57-60) decidir criar/consultar uma
`Publication` a partir de um Job.

### 4.5 Erro de conflito nunca confundido com outras violações de integridade

`sqlite3.IntegrityError` de uma violação de FK (ex.: `video_id`
inexistente) é distinguido de uma violação do índice único de
`idempotency_key` inspecionando a mensagem do erro (`"idempotency_key" in
str(exc)`) — testado explicitamente
(`test_violacao_de_fk_nunca_e_confundida_com_conflito_de_idempotencia`):
uma FK inválida propaga como `sqlite3.IntegrityError` cru (o erro real, de
configuração do chamador), nunca mascarado como
`PublicationIdempotencyConflictError`.

## 5. Migration

`m006_publication_idempotency.py` (`version=6`): `ALTER TABLE publications
ADD COLUMN idempotency_key TEXT` + índice único parcial. Não altera
nenhuma outra tabela. Hash SHA-256 congelado em
`tests/test_migrations_frozen.py`. m001-m005 confirmadas byte-idênticas
(hash comparado, nenhuma linha tocada nesta rodada).

## 6. Os três resultados possíveis

Implementados como `IdempotencyCheck(outcome: str, publication: Publication
| None)`, com `outcome` sempre um de `OUTCOME_NUNCA_TENTADO`,
`OUTCOME_JA_PUBLICADO`, `OUTCOME_INCONCLUSIVO` (nunca um booleano único,
roadmap item 1.3). Cada um testado individualmente (seção 3), incluindo o
cenário "crash pós-upload" explícito exigido pelos testes obrigatórios:
uma `Publication` sem `remote_id` já existente para a chave → nova
tentativa de `claim()` devolve `INCONCLUSIVO`, sem criar segunda linha nem
afirmar sucesso (verificado por contagem de linhas antes/depois e por
inspeção direta da `Publication` original, que continua exatamente como
estava).

## 7. Confirmações explícitas exigidas pelo Prompt

- **Nenhuma chamada real de rede/Connector foi implementada**: o módulo
  não importa `requests`/`urllib`/Playwright nem qualquer coisa de rede;
  "verificar publicação conhecida" consulta exclusivamente o SQLite local.
- **`circuit_breaker.py`/`retry_policy.py`/`job_state_machine.py`/Geração
  1 não foram tocados**: confirmado por hash SHA-256 (idêntico ao final do
  Prompt 21 — nenhum `Edit`/`Write` foi executado contra nenhum destes
  arquivos nesta rodada) e por teste automatizado (via AST,
  `test_publication_idempotency_nunca_importa_circuit_breaker_nem_retry_policy`)
  que garante que `publication_idempotency.py` nunca importa, referencia
  atributo ou chama nada de `circuit_breaker`/`retry_policy`.
- **O caso INCONCLUSIVO nunca resulta em segunda publicação criada
  automaticamente**: `_claim_locked` só escreve quando
  `existing.outcome == OUTCOME_NUNCA_TENTADO` — para `JA_PUBLICADO`/
  `INCONCLUSIVO`, a função retorna imediatamente sem tocar o banco. Não
  existe nenhum caminho de código (nem em `claim()`, nem em `check()`, nem
  em `create_publication()`, que sempre levanta um erro claro em vez de
  duplicar) que crie uma segunda `Publication` para uma chave já em uso —
  o índice único do banco é a garantia estrutural final disso, independente
  de qualquer bug futuro na camada Python.

## 8. Testes automatizados

- `tests/test_publication_idempotency.py`: **26 testes**, cobrindo as três
  etapas do fluxo (nunca tentado / já publicado / inconclusivo, cada uma
  via `check()` e `claim()`), o cenário crash-pós-upload explícito,
  fechamento do ciclo via `mark_published()`, índice único sob inserção
  direta e sob concorrência real (dois testes de concorrência
  determinística com `Barrier`, 8 threads cada), não-interferência entre
  chaves diferentes (vídeos diferentes, operações diferentes), validação
  de entrada, restart (novas instâncias), roundtrip com FKs reais
  (`video_id`/`account_id`), distinção entre violação de FK e conflito de
  idempotência, e não-regressão (import/AST confirmando isolamento de
  `circuit_breaker`/`retry_policy`, e confirmação de que nenhuma FK nova
  Job↔Publication foi criada).
- Suíte completa (incluindo os 3 arquivos de schema-version atualizados
  conscientemente e `test_migrations_frozen.py`): **1311 passed, 36
  subtests passed**, 0 falhas.
- `python3 -m compileall -q _sistema tests`: **OK**, sem erros de sintaxe
  ou import.

## 9. Como testar manualmente

1. Rodar `RODAR_TESTES.bat` (ou `pytest -q tests/`) e confirmar 1311
   passed.
2. `from _sistema.publication_idempotency import PublicationIdempotencyGuard, compute_idempotency_key`;
   criar um `LocalDatabase` inicializado, instanciar o guard, chamar
   `claim(compute_idempotency_key(operation="PUBLISH", account_id=..., video_id=...))`
   — primeira chamada devolve `NUNCA_TENTADO` com uma `Publication` nova;
   chamar de novo com a mesma chave, sem `mark_published()` no meio,
   devolve `INCONCLUSIVO`; depois de `mark_published(pub.id, remote_id=...)`,
   uma nova chamada devolve `JA_PUBLICADO`.
3. Tentar inserir diretamente duas `Publication`s com o mesmo
   `idempotency_key` via `LocalDatabase.insert` e confirmar que a segunda
   levanta `sqlite3.IntegrityError`.

## 10. Riscos conhecidos e dívida técnica

- **O caso INCONCLUSIVO ainda não tem, nesta rodada, nenhum mecanismo
  automático de resolução.** Isso é deliberado e exigido pelo próprio
  Prompt: só um Connector real futuro, que possa perguntar à plataforma
  remota se a publicação realmente aconteceu, poderia resolver essa
  pendência com segurança. Este Prompt garante apenas que o produto nunca
  finge ter certeza quando não tem — a pendência fica visível
  (`INCONCLUSIVO`, publicação sem `remote_id` persistida e consultável) e
  auditável, nunca resolvida por adivinhação.
- Nenhuma integração automática com `Job`/`JobEngine`/`RecoveryManager`
  existe ainda (por design — sem FK Job↔Publication, ver seção 4.4); um
  Connector futuro (Prompts 57-60) é quem vai chamar `claim()` antes de
  publicar e `mark_published()` depois de confirmar sucesso remoto real.
- Este módulo não escreve `audit_events` próprios (diferente do Circuit
  Breaker, que audita abertura/fechamento de circuito) — a própria linha
  de `Publication` (com `created_at`/`updated_at`/`status`/`remote_id`)
  já serve como trilha auditável do que aconteceu com aquela chave,
  evitando duplicar essa informação em outro lugar sem necessidade
  arquitetural demonstrada (CLAUDE.md, "não fazer overengineering sem
  benefício concreto"). Se um Connector futuro precisar de um evento mais
  granular, pode ser adicionado então, quando o caso de uso real existir.

## 11. Pendências

Nenhuma pendência dentro do escopo autorizado deste Prompt. Não iniciado
Prompt 23, conforme instrução explícita.
