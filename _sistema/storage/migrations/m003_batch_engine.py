"""Migration 003: identidade e membership persistente de Batches (PROMPT 17).

DECISÃO ARQUITETURAL (ver ``_sistema/batch_engine.py`` para o contrato
completo): ``batches``/``batch_jobs`` são tabelas SQL dedicadas, não um JSON
gigante em ``settings``. Um batch de 500+ itens precisa de: consulta de
membership por índice (não uma leitura+parse de um blob inteiro), ordem
estável persistida, e uma junção eficiente com ``jobs`` para progresso
(``GROUP BY jobs.status``) sem O(n²). ``settings`` (m001) continua sendo
usado, sem migration nova, apenas para os dois flags operacionais pequenos e
de leitura pontual do BatchEngine (pausa e intenção de cancelamento em
massa) — o mesmo padrão já usado por ``ControlManager``/``ShutdownCoordinator``
(chave -> valor JSON pequeno), nunca para a membership em si.

``batch_jobs.job_id`` é ``UNIQUE``: um Job pertence a NO MÁXIMO um batch.
Isso é uma decisão deliberada (ver docstring de ``JobAlreadyInBatchError``
em ``batch_engine.py``) para que "de qual batch este Job é" nunca fique
ambíguo depois de um restart, e é reforçada pelo próprio SQLite (não apenas
por lógica de aplicação) — mesmo entre duas instâncias de ``BatchEngine``
concorrentes contra o mesmo arquivo.

Não altera ``jobs`` (m001) nem ``audit_events`` (m002): nenhuma coluna nova
em ``jobs``, nenhum gatilho novo em ``audit_events``. m001/m002 permanecem
byte-idênticas.
"""
from __future__ import annotations

from . import Migration

SQL = r"""
CREATE TABLE batches (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    job_count INTEGER NOT NULL CHECK (job_count >= 0),
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE batch_jobs (
    batch_id TEXT NOT NULL,
    job_id TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL CHECK (position >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, job_id),
    FOREIGN KEY (batch_id) REFERENCES batches(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_batches_created_at ON batches(created_at);
CREATE INDEX idx_batch_jobs_batch_id ON batch_jobs(batch_id, position);
""".strip()

MIGRATION = Migration(version=3, name="batch_engine_tables", sql=SQL)
