# -*- coding: utf-8 -*-
"""Migration 007: declarações declarativas de importação (PROMPT 24b).

CONTEXTO (ver ``_sistema/source_import.py`` para o contrato completo):
``ImportOptions``/``user_assertions``/``user_flags``/``user_labels`` não
tinham nenhum lugar de persistência -- não existia, antes desta migration,
nenhuma tabela de "rótulos"/"flags"/"assertions" no schema (confirmado por
leitura de m001-m006 antes de desenhar esta).

DECISÃO DE DESENHO (ver seção 0.1 do Prompt 24b): tabela ÚNICA
append-only, de onde o ESTADO ATUAL de uma declaração é sempre DERIVADO
(opção "b" das duas descritas pelo Prompt), em vez de uma tabela de estado
atual + uma tabela de histórico separadas (opção "a"). Justificativa:

- O roadmap pede que uma declaração possa ser aplicada, removida e editada
  em lote depois -- ou seja, o ESTADO ATUAL de uma declaração
  (``source_asset_id`` + ``kind`` + ``value``) muda ao longo do tempo, mas
  o HISTÓRICO de quando/por quem/com qual origem cada mudança aconteceu
  nunca pode ser reescrito ou apagado (mesmo espírito de
  ``audit_events``/m002: nunca ``UPDATE``/``DELETE`` diretos).
- Uma tabela única evita o risco de "split-brain" entre uma tabela de
  estado e uma tabela de histórico divergindo (ex.: um bug que atualiza o
  histórico mas esquece de atualizar o estado, ou vice-versa) -- com uma
  única fonte de verdade append-only, o estado atual é sempre uma
  PROJEÇÃO determinística do histórico, nunca uma cópia que pode
  dessincronizar.
- Cada linha é um EVENTO (``action`` = ``ADD`` ou ``REMOVE``) para uma
  chave lógica (``source_asset_id``, ``kind``, ``value``). O estado atual
  de uma chave lógica é o ``action`` do evento mais recente (maior
  ``rowid``) para essa chave -- se o mais recente for ``ADD``, a
  declaração está ativa; se for ``REMOVE``, não está. "Remover" uma
  declaração é sempre um INSERT de um novo evento ``REMOVE``, nunca um
  ``UPDATE``/``DELETE`` do evento ``ADD`` original -- o histórico completo
  (quando foi declarada, por quem, quando foi removida) permanece
  reconstruível para sempre.

``origin`` (``SYSTEM``/``USER``/``IMPORT_PRESET``) é uma coluna
OBRIGATÓRIA em toda escrita -- nunca inferida/adivinhada na leitura. Isso
torna estruturalmente impossível gravar uma ``USER_ASSERTION`` e lê-la de
volta como se fosse ``SYSTEM``: a origem está gravada no próprio evento,
não deduzida por um código de leitura que poderia errar.

``source_asset_id`` referencia ``sources(id)`` (identidade estável de
``SourceAsset`` -- nunca caminho de arquivo/URL, que podem mudar). Sem
``ON DELETE CASCADE``: mesma política ``RESTRICT`` já usada em todas as
outras foreign keys deste schema (m001) -- remover um ``SourceAsset`` com
declarações associadas exige decisão explícita de uma camada futura, não
um cascade silencioso.

Nenhuma coluna guarda segredo, conteúdo bruto de arquivo ou caminho de
sistema de arquivos -- apenas ``source_asset_id``, ``kind``, ``value``,
``origin``, ``action`` e ``created_at`` (item 1.6 do Prompt).
"""
from __future__ import annotations

from . import Migration

SQL = r"""
CREATE TABLE source_asset_declarations (
    id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ASSERTION', 'FLAG', 'LABEL')),
    value TEXT NOT NULL CHECK (length(value) > 0),
    origin TEXT NOT NULL CHECK (origin IN ('SYSTEM', 'USER', 'IMPORT_PRESET')),
    action TEXT NOT NULL CHECK (action IN ('ADD', 'REMOVE')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_asset_id) REFERENCES sources(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_source_asset_declarations_source_asset_id
    ON source_asset_declarations(source_asset_id);

CREATE INDEX idx_source_asset_declarations_lookup
    ON source_asset_declarations(source_asset_id, kind, value);

CREATE INDEX idx_source_asset_declarations_origin
    ON source_asset_declarations(origin);

CREATE TRIGGER trg_source_asset_declarations_no_update
BEFORE UPDATE ON source_asset_declarations
BEGIN
    SELECT RAISE(ABORT, 'source_asset_declarations is append-only: UPDATE denied');
END;

CREATE TRIGGER trg_source_asset_declarations_no_delete
BEFORE DELETE ON source_asset_declarations
BEGIN
    SELECT RAISE(ABORT, 'source_asset_declarations is append-only: DELETE denied');
END;

CREATE TRIGGER trg_source_asset_declarations_no_id_reuse
BEFORE INSERT ON source_asset_declarations
WHEN EXISTS (SELECT 1 FROM source_asset_declarations WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'source_asset_declarations is append-only: duplicate id denied');
END;
""".strip()

MIGRATION = Migration(version=7, name="source_asset_declarations", sql=SQL)
