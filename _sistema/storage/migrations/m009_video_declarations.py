# -*- coding: utf-8 -*-
"""Migration 009: marcações manuais do usuário sobre Video (PROMPT 27.5).

CONTEXTO (ver ``_sistema/media_catalog.py`` para o contrato completo do
catálogo): o roadmap do Media Catalog Backend exige persistência para
USER_ASSERTION / USER_FLAG / USER_LABEL associados a um ``video_id`` --
marcações manuais feitas pelo usuário sobre um item do catálogo (ex.:
"FAVORITE", "Cliente A", "REVIEWED"). Essas marcações são uma entidade
DIFERENTE de ``source_asset_declarations`` (m007), que é keyed por
``source_asset_id`` -- SourceAsset e Video são entidades distintas
(SourceAsset é o arquivo bruto importado; Video é o item promovido para
o catálogo/pipeline via ``VideoPromotionService``, PROMPT 27b). Reaproveitar
``source_asset_declarations`` aqui exigiria uma FOREIGN KEY apontando para
a tabela errada (``sources`` em vez de ``videos``) -- por isso esta é uma
tabela NOVA, não uma extensão da m007.

DECISÃO DE DESENHO: mesmo padrão append-only já estabelecido em m002
(``audit_events``) e m007 (``source_asset_declarations``), pelas mesmas
razões (ver docstring de m007 para a justificativa completa): cada linha
é um EVENTO (``action`` = ``ADD`` ou ``REMOVE``) para uma chave lógica
(``video_id``, ``kind``, ``value``); o estado atual de uma chave lógica é
o ``action`` do evento mais recente (maior ``rowid``/``created_at``) para
essa chave. Nunca ``UPDATE``/``DELETE`` diretos -- o histórico completo
permanece reconstruível para sempre, e a leitura do "estado atual" é
sempre uma PROJEÇÃO determinística do log, nunca uma cópia que pode
dessincronizar.

``origin`` é fixado em ``'USER'`` (CHECK de valor único) -- diferente de
m007, que precisa distinguir ``SYSTEM``/``USER``/``IMPORT_PRESET`` porque
declarações de importação podem vir de um preset ou do próprio sistema.
Aqui, por contrato do roadmap (seção "SYSTEM FACT x USER ASSERTION" do
Prompt 27.5): "Uma marcação manual nunca pode falsificar evidência
automática do sistema" -- ou seja, SYSTEM_BADGES NUNCA são gravados nesta
tabela; eles são sempre DERIVADOS em tempo de leitura a partir de outras
tabelas de verdade operacional (jobs, artifacts, publications, schedules,
projects). Fixar ``origin = 'USER'`` torna estruturalmente impossível que
um bug futuro tente gravar um "badge" aqui como se fosse um fato de
sistema -- a própria coluna não aceita outro valor.

``kind`` distingue ``ASSERTION`` (o usuário afirma algo que existe
externamente), ``FLAG`` (organização/decisão humana -- ex.: REVIEWED,
APPROVED, FAVORITE, NEEDS_REWORK, PRIORITY, USER_MARKED_READY) e
``LABEL`` (rótulo livre -- ex.: "Cliente A", "Campanha X").

``video_id`` referencia ``videos(id)`` -- ``ON DELETE RESTRICT`` (mesma
política já usada em todas as outras foreign keys deste schema desde
m001): remover um ``Video`` com marcações associadas exige decisão
explícita de uma camada futura, nunca um cascade silencioso.

Índices: a consulta mais comum do catálogo é "estado atual das marcações
de um vídeo" (``video_id`` sozinho, para reconstruir a projeção) e "quais
vídeos têm uma marcação específica" (``video_id, kind, value`` já cobre a
primeira via prefixo; adiciona-se também um índice dedicado em
``(kind, value)`` para suportar filtros/facets que buscam por marcação
sem partir de um vídeo específico, ex. contar quantos vídeos têm a FLAG
'FAVORITE'). Não é necessário nenhum índice novo nas tabelas já
existentes (``jobs``, ``artifacts``, ``publications``, ``schedules``) --
confirmado por leitura completa de m001: ``idx_jobs_video_id``,
``idx_artifacts_video_id``, ``idx_publications_video_id`` e
``idx_schedules_publication_id`` (este último já UNIQUE) já cobrem os
caminhos de join que o catálogo precisa para derivar EDITED/SCHEDULED/
PUBLISHED/ERROR.

Nenhuma coluna guarda segredo, conteúdo bruto de arquivo ou caminho de
sistema de arquivos -- apenas ``video_id``, ``kind``, ``value``,
``origin``, ``action`` e ``created_at``.
"""
from __future__ import annotations

from . import Migration

SQL = r"""
CREATE TABLE video_declarations (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ASSERTION', 'FLAG', 'LABEL')),
    value TEXT NOT NULL CHECK (length(value) > 0),
    origin TEXT NOT NULL CHECK (origin IN ('USER')),
    action TEXT NOT NULL CHECK (action IN ('ADD', 'REMOVE')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_video_declarations_video_id
    ON video_declarations(video_id);

CREATE INDEX idx_video_declarations_lookup
    ON video_declarations(video_id, kind, value);

CREATE INDEX idx_video_declarations_kind_value
    ON video_declarations(kind, value);

CREATE TRIGGER trg_video_declarations_no_update
BEFORE UPDATE ON video_declarations
BEGIN
    SELECT RAISE(ABORT, 'video_declarations is append-only: UPDATE denied');
END;

CREATE TRIGGER trg_video_declarations_no_delete
BEFORE DELETE ON video_declarations
BEGIN
    SELECT RAISE(ABORT, 'video_declarations is append-only: DELETE denied');
END;

CREATE TRIGGER trg_video_declarations_no_id_reuse
BEFORE INSERT ON video_declarations
WHEN EXISTS (SELECT 1 FROM video_declarations WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'video_declarations is append-only: duplicate id denied');
END;
""".strip()

MIGRATION = Migration(version=9, name="video_declarations", sql=SQL)
