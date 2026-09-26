"""Migration 005: estado persistido de retry automático por Job (PROMPT 21).

DECISÃO ARQUITETURAL (ver ``_sistema/retry_policy.py`` para o contrato
completo e o contraste explícito com a decisão do Prompt 20): uma tabela
dedicada e indexada por ``job_id``, NÃO uma coluna nova em ``jobs`` (m001)
e NÃO ``Job.extra``. ``next_retry_at_epoch`` é comparado contra o relógio a
cada ``fetch_pending_jobs()``/``_claim()`` -- o mesmo argumento que levou o
Circuit Breaker (m004) a preferir uma coluna real a um JSON reencodado se
aplica aqui, mas em vez de alterar a entidade compartilhada ``Job``
(``domain/models.py``, usada por TODO o Geração 2) para adicionar uma
coluna "literal" em ``jobs``, esta migration cria uma tabela nova e
independente -- exatamente o mesmo padrão que ``circuit_breaker_state``
(m004) já estabeleceu para o problema análogo (conector em vez de Job).

``job_id`` é ``TEXT PRIMARY KEY`` SEM ``FOREIGN KEY`` para ``jobs(id)`` --
decisão deliberada (mesma escolha já feita para
``circuit_breaker_state.connector_key``): mantém o módulo testável de
forma isolada (testes de unidade de ``RetryPolicy`` não precisam persistir
um ``Job`` real primeiro) e evita qualquer semântica de
``ON DELETE``/``ON UPDATE`` sobre uma entidade que este produto nunca
apaga fisicamente (o ciclo de vida de um Job termina em um estado
terminal, nunca em uma linha removida).

Não altera ``jobs``/``accounts``/``publications`` (m001), ``audit_events``
(m001/m002), ``batches``/``batch_jobs`` (m003) nem
``circuit_breaker_state`` (m004): nenhuma coluna nova em tabela existente,
nenhum gatilho novo. m001/m002/m003/m004 permanecem byte-idênticas.
"""
from __future__ import annotations

from . import Migration

SQL = r"""
CREATE TABLE job_retry_state (
    job_id TEXT PRIMARY KEY,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_retry_at_epoch REAL,
    last_failure_category TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_job_retry_state_next_retry ON job_retry_state(next_retry_at_epoch);
""".strip()

MIGRATION = Migration(version=5, name="job_retry_state", sql=SQL)
