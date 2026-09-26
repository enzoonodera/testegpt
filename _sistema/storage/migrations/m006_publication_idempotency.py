# -*- coding: utf-8 -*-
"""Migration 006: chave de idempotência dedicada em ``publications`` (PROMPT 22).

DECISÃO ARQUITETURAL (ver ``_sistema/publication_idempotency.py`` para o
contrato completo e o contraste explícito com as decisões dos Prompts
20/21): diferente do Circuit Breaker (``circuit_breaker_state``, m004) e do
RetryPolicy (``job_retry_state``, m005) -- que criaram tabelas NOVAS e
independentes porque o dado que precisavam persistir (estado por connector,
estado por Job) não tinha lugar natural em nenhuma entidade já existente
sem alterar uma dataclass compartilhada por toda a Geração 2 (``Job``) --,
este Prompt pede EXPLICITAMENTE uma coluna dedicada NA PRÓPRIA
``Publication`` (roadmap, item 1.1: "adicionar uma coluna dedicada... a
``Publication``"). Isso é coerente com a natureza do dado: a chave de
idempotência não é um estado auxiliar de OUTRO subsistema observando uma
``Publication`` de fora (como o Circuit Breaker observa um connector, ou o
RetryPolicy observa um Job) -- ela é uma PROPRIEDADE DA PRÓPRIA
``Publication`` (identifica de forma estável qual tentativa de publicação
remota ela representa), então pertence à própria entidade e sua própria
tabela, exatamente como ``remote_id``/``status`` já pertencem.

``ALTER TABLE ... ADD COLUMN`` (não ``CREATE TABLE``) porque ``publications``
já existe desde m001 -- esta é a primeira migration deste projeto a alterar
uma tabela existente em vez de criar uma nova; m001-m005 permanecem
byte-idênticas (nenhuma linha tocada, confirmado por hash no relatório de
entrega).

ÍNDICE ÚNICO PARCIAL (``WHERE idempotency_key IS NOT NULL``): mesmo padrão
já usado por ``idx_schedules_publication_id`` (m001) -- permite múltiplas
``Publication``s com ``idempotency_key IS NULL`` (nunca reivindicaram uma
chave, ex.: dados legados importados por ``legacy_migration.py``, que não
preenchem esta coluna) enquanto GARANTE, no nível do banco, que no máximo
uma ``Publication`` exista para cada chave não nula -- a proteção real
contra publicação duplicada (roadmap, item 1.1: "a proteção real... tem que
ser garantida pelo BANCO, não só por uma checagem em Python"), nunca
contornável por uma corrida entre duas transactions concorrentes.
"""
from __future__ import annotations

from . import Migration

SQL = r"""
ALTER TABLE publications ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX idx_publications_idempotency_key
    ON publications(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
""".strip()

MIGRATION = Migration(version=6, name="publication_idempotency", sql=SQL)
