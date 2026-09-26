# -*- coding: utf-8 -*-
"""Migration 008: classificação técnica automática de contexto de origem
(PROMPT 25 -- ``SourceContextResolver``).

CONTEXTO (ver ``_sistema/source_context.py`` para o contrato completo):
o roadmap pede uma classificação automática, invisível ao usuário, de cada
``SourceAsset`` em exatamente uma de quatro categorias
(``CONNECTED_CHANNEL_CONTENT``/``EXTERNAL_CONTENT``/``LOCAL_CONTENT``/
``UNKNOWN_SOURCE``) -- uma classificação TÉCNICA (qual pipeline usar),
nunca uma determinação jurídica de propriedade.

DECISÃO DE DESENHO (ver seção 0 do Prompt 25 -- decisão central desta
rodada, avaliada entre duas opções aceitáveis):

(a) Estender ``source_asset_declarations`` (m007) com um novo ``kind``
    (ex. ``CONTEXT_CLASSIFICATION``) -- rejeitada. Exigiria recriar a
    tabela inteira para alterar o CHECK constraint de ``kind`` (SQLite não
    suporta ``ALTER TABLE ... ADD CHECK``/alterar CHECK existente in-place),
    reabrindo uma migration já aprovada e congelada
    (``test_migrations_frozen.py``) só para acomodar uma semântica
    fundamentalmente diferente. Pior: a leitura de "estado ativo" de
    ``AssertionStore.list_active`` (``ROW_NUMBER() ... PARTITION BY
    source_asset_id, kind, value``) foi desenhada para "N valores
    independentes podem estar ativos ao mesmo tempo sob o mesmo kind"
    (várias labels simultâneas) -- o oposto exato do que uma classificação
    de contexto precisa (exatamente UM valor vence por ``source_asset_id``,
    substituindo o anterior). Forçar as duas semânticas na mesma tabela
    exigiria um caminho de leitura especial só para este ``kind``,
    aumentando a complexidade de um componente já aprovado sem necessidade
    (violaria "não fazer overengineering"/"não reabrir o que já foi
    entregue e auditado" do CLAUDE.md).

(b) Tabela NOVA dedicada, ``source_asset_context`` -- ESCOLHIDA. Mesmo
    espírito auditável/append-only de ``audit_events`` (m002) e
    ``source_asset_declarations`` (m007): cada linha é um EVENTO
    (``source_asset_id``, ``context``, ``created_at``), nunca um
    ``UPDATE``/``DELETE`` do evento anterior -- reclassificar é sempre um
    novo INSERT. A diferença de semântica em relação a m007 está
    inteiramente na CONSULTA de "estado atual", não no schema desta
    tabela: aqui o estado atual é o evento de MAIOR ``rowid`` para um dado
    ``source_asset_id``, SEM particionar por valor de ``context`` -- ou
    seja, reclassificar sempre substitui globalmente a classificação
    anterior daquele ``SourceAsset``, nunca acumula duas classificações
    simultâneas como se ambas estivessem ativas (testado explicitamente em
    ``tests/test_source_context.py``). Esta tabela é estritamente
    aditiva: não toca, não reabre e não enfraquece
    ``source_asset_declarations``/m007 de forma alguma.

``context`` é restrito por CHECK às quatro constantes exatas do roadmap.
``source_asset_id`` referencia ``sources(id)`` com a mesma política
``ON DELETE RESTRICT`` já usada em todas as outras foreign keys deste
schema (m001/m007) -- remover um ``SourceAsset`` com classificação
associada exige decisão explícita de uma camada futura, nunca um cascade
silencioso.

Nenhuma coluna guarda segredo, conteúdo bruto de arquivo, caminho de
sistema de arquivos ou qualquer evidência de propriedade jurídica --
apenas ``source_asset_id``, ``context`` e ``created_at`` (esta
classificação é técnica/heurística por natureza; ver docstring de
``_sistema/source_context.py``).
"""
from __future__ import annotations

from . import Migration

SQL = r"""
CREATE TABLE source_asset_context (
    id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL,
    context TEXT NOT NULL CHECK (
        context IN (
            'CONNECTED_CHANNEL_CONTENT',
            'EXTERNAL_CONTENT',
            'LOCAL_CONTENT',
            'UNKNOWN_SOURCE'
        )
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_asset_id) REFERENCES sources(id) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_source_asset_context_source_asset_id
    ON source_asset_context(source_asset_id);

CREATE TRIGGER trg_source_asset_context_no_update
BEFORE UPDATE ON source_asset_context
BEGIN
    SELECT RAISE(ABORT, 'source_asset_context is append-only: UPDATE denied');
END;

CREATE TRIGGER trg_source_asset_context_no_delete
BEFORE DELETE ON source_asset_context
BEGIN
    SELECT RAISE(ABORT, 'source_asset_context is append-only: DELETE denied');
END;

CREATE TRIGGER trg_source_asset_context_no_id_reuse
BEFORE INSERT ON source_asset_context
WHEN EXISTS (SELECT 1 FROM source_asset_context WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'source_asset_context is append-only: duplicate id denied');
END;
""".strip()

MIGRATION = Migration(version=8, name="source_asset_context", sql=SQL)
