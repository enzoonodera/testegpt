"""Migration 004: estado persistido do Circuit Breaker por connector (PROMPT 20).

DECISÃO ARQUITETURAL (ver ``_sistema/circuit_breaker.py`` para o contrato
completo): uma tabela dedicada e indexada por ``connector_key``, não um JSON
em ``settings`` (m001) -- ao contrário do pause de ``BatchEngine`` (flag
pontual, um valor por vez), o Circuit Breaker precisa comparar
``retry_eligible_at_epoch`` contra o relógio a cada decisão de admissão, o
que se beneficia de uma coluna própria em vez de um blob JSON reencodado a
cada leitura/escrita. Eventos observáveis de abertura/fechamento (quando
abriu, quando fechou, quanto tempo ficou aberto) NÃO ganham tabela nova:
são gravados em ``audit_events`` (m001/m002, já append-only) via
``LocalDatabase.append_audit_event(event_type="CIRCUIT_BREAKER_OPENED"/
"CIRCUIT_BREAKER_CLOSED", entity_type="circuit_breaker", data={...})`` --
``connector_key`` não é um UUID, então vai em ``data_json``, nunca em
``entity_id`` (que exige UUID válido -- ver ``LocalDatabase.append_audit_event``).

``consecutive_failures``/``state`` representam o contador de falhas
SISTÊMICAS consecutivas (mesmo ``code``) do connector -- falhas de CONTEÚDO
nunca chegam a esta tabela (ver ``circuit_breaker.py``, distinção
sistêmico-vs-conteúdo). ``opened_at_epoch``/``retry_eligible_at_epoch`` são
``REAL`` (epoch em segundos) especificamente para comparação determinística
contra um clock injetável em teste, sem parse de string a cada decisão;
``opened_at``/``last_failure_at`` continuam ``TEXT`` (ISO 8601, via
``utc_now_iso()``) só para leitura humana/diagnóstico, no mesmo padrão de
todo o resto do schema.

Não altera ``jobs``/``accounts``/``publications`` (m001), ``audit_events``
(m001/m002) nem ``batches``/``batch_jobs`` (m003): nenhuma coluna nova em
tabela existente, nenhum gatilho novo. m001/m002/m003 permanecem
byte-idênticas.
"""
from __future__ import annotations

from . import Migration

SQL = r"""
CREATE TABLE circuit_breaker_state (
    connector_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'CLOSED' CHECK (state IN ('CLOSED', 'OPEN')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_failure_code TEXT,
    last_failure_at TEXT,
    opened_at TEXT,
    opened_at_epoch REAL,
    retry_eligible_at_epoch REAL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_circuit_breaker_state_state ON circuit_breaker_state(state);
""".strip()

MIGRATION = Migration(version=4, name="circuit_breaker_state", sql=SQL)
