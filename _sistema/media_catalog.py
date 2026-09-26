# -*- coding: utf-8 -*-
"""PROMPT 27.5 -- Media Catalog Backend (MediaCatalogService).

===========================================================================
OBJETIVO (roadmap, seção "Media Catalog Backend")
===========================================================================

"Permitir consultar centenas ou milhares de vídeos como itens de catálogo,
com estado resumido, evidências de processamento, marcações do usuário,
filtros, paginação e dados suficientes para futura seleção em lote. O
catálogo NÃO deve duplicar a verdade operacional." Este módulo não cria
NENHUMA nova fonte de verdade -- ele apenas LÊ e AGREGA, via SQL
parametrizado, as fontes já existentes: ``SourceAsset``/``Video``/
``Project``/``Job``/``Artifact``/``Publication``/``Schedule`` (``domain/
models.py``), mais a nova tabela append-only ``video_declarations``
(``m009_video_declarations.py``, criada por este Prompt) para marcações
manuais do usuário (``USER_ASSERTION``/``USER_FLAG``/``USER_LABEL``).

Este módulo NÃO importa nenhum módulo de Geração 1 e NÃO chama
``subprocess``/``ffmpeg``/``ffprobe`` -- é estritamente leitura/agregação
sobre o SQLite já persistido (confirmado por teste AST em
``tests/test_media_catalog.py``, mesmo padrão já usado nos Prompts
24b/25/26/27/27b).

===========================================================================
0.2 -- FONTES DE VERDADE INEXISTENTES HOJE (``ContentAnalysis``/
       ``ContentEngine`` e todo o restante do "CATALOG EVIDENCE CONTRACT")
===========================================================================

O roadmap cita ``ContentAnalysis``/``ContentEngine`` como possíveis fontes
futuras -- confirmado por grep em todo ``_sistema/`` que NENHUM dos dois
existe hoje, nem ``CaptionsEngine``/``MetadataManager`` (Gen2)/
``AutoReframe``/``AudioEngine``/``Template``-``Render`` (Gen2). Este
módulo NÃO inventa evidência para fazer os badges correspondentes
"funcionarem" -- isso violaria diretamente a regra do próprio roadmap:
"Crash, falha parcial ou arquivo ausente não podem produzir badge
positivo falso." O mecanismo de derivação (``_BADGE_SQL_EXPRESSIONS``
abaixo) é genérico e extensível -- qualquer módulo futuro que produza
evidência estruturada suficiente pode ganhar uma entrada nova nesse
dicionário sem alterar a forma do catálogo -- mas, até que essa evidência
exista de verdade, o badge correspondente é uma expressão SQL literal
``0`` (sempre falso). Ver o relatório de entrega para a justificativa
badge a badge dos 17 badges do roadmap.

De ``media_probe.py`` (Prompt 26): confirmado por leitura da própria
docstring do módulo (linhas 79-97) que ``MediaProbe`` é deliberadamente
STATELESS -- não persiste nenhum resultado (nenhuma tabela/coluna guarda
``duration``/``resolution`` de um probe). Por isso ``MediaCatalogItem.
duration``/``resolution``/``thumbnail_ref`` são SEMPRE ``None`` hoje --
não por bug, mas porque não existe, hoje, nenhuma fonte de verdade
persistida da qual derivá-los.

===========================================================================
0.3 -- BADGES DERIVÁVEIS HOJE (fontes exatas)
===========================================================================

- IMPORTED: existe um ``Video`` cujo ``source_asset_id`` aponta para um
  ``SourceAsset`` real (join ``videos.source_asset_id -> sources.id``).
- EDITED: existe um ``Project`` com ``video_id`` apontando para este vídeo
  E pelo menos uma categoria definida em ``edit_state`` (nunca um
  ``Project`` vazio contando como editado -- checado via ``json_each``
  sobre ``edit_state_json->'$.categories'``, nunca carregando o projeto
  inteiro em Python).
- SCHEDULED: existe uma ``Schedule`` com ``delivery_state`` em
  (``LOCAL_PENDING``, ``REMOTE_SCHEDULED``) associada, via
  ``Publication.video_id -> Publication.id -> Schedule.publication_id``,
  a este vídeo.
- PUBLISHED: existe um ``Job`` com ``status == 'PUBLISHED'`` associado a
  este ``video_id`` (vocabulário fechado de ``job_state_machine.py`` --
  nunca ``Publication.status``, que hoje só tem o valor ``'PENDING'`` em
  código real e não tem vocabulário fechado confirmado).
- ERROR: existe um ``Job`` com ``status == 'FAILED'`` associado a este
  ``video_id``, e/ou um ``ErrorRecord`` cujo ``entity_id`` aponta para
  este vídeo.

Desde o Prompt "CATALOG WIRING / RECONCILIATION 27.5+30+31", ``CAPTIONS``
e ``TRANSCRIBED`` SAÍRAM desta lista -- ``CaptionsEngine`` (Prompt 31)
passou a existir e produz evidência real persistida (``Artifact`` +
checkpoint), então essas duas badges agora têm expressão SQL real em
``_BADGE_SQL_EXPRESSIONS`` (ver seção 0.7 abaixo). ``AUDIO_PROCESSED``
foi avaliado na mesma rodada e permanece deliberadamente ``"0"`` --
``audio_engine.py`` (Prompt 30) é 100% decisão/configuração (``set_*``/
``get_*`` sobre ``EditProjectManager``), nunca cria ``Job``/``Artifact``/
checkpoint; não existe hoje, em nenhum módulo, um passo real de
processamento de áudio que produza evidência. Não é omissão: é a mesma
regra que bloqueia ``RENDERED``/``READY`` abaixo, aplicada aqui de forma
explícita. Ver seção 0.7 para o que destravaria ``AUDIO_PROCESSED`` no
futuro.

Desde o Prompt "Catalog Wiring -- REFRAMED_9_16 (Prompt 33)",
``REFRAMED_9_16`` também saiu desta lista -- ``AutoReframeEngine``
(Prompt 33) produz evidência real persistida (``Artifact``
``reframe_track`` + checkpoint ``MEDIA_PROCESSED``), então essa badge
agora tem expressão SQL real em ``_BADGE_SQL_EXPRESSIONS`` (ver seção 0.8
abaixo).

Desde o Prompt 42 (``render_engine.py``), ``RENDERED`` também saiu desta
lista -- ``RenderEngine`` produz evidência real persistida (``Artifact``
``render_output`` com arquivo real no disco + checkpoint ``RENDERED``,
gravado tanto em um render novo quanto em um cache-hit que reaproveita um
``Artifact`` já existente), então essa badge agora tem expressão SQL real
em ``_BADGE_SQL_EXPRESSIONS`` (mesmo padrão duplo de ``REFRAMED_9_16``/
``TRANSCRIBED`` -- checkpoint sozinho nunca basta, ver seção 0.8 abaixo
para o motivo).

Desde o Prompt 43 (``final_media_validator.py``), ``VALIDATED`` e
``READY`` também saíram desta lista -- ``FinalMediaValidator`` produz
evidência real persistida (checkpoint ``VALIDATED``, gravado SOMENTE
quando o checklist de qualidade final passa por completo, reaproveitando
o MESMO ``Artifact`` ``render_output`` de ``RENDERED`` -- nenhum novo
``kind`` de Artifact inventado), então ``VALIDATED`` agora tem expressão
SQL real (mesmo padrão duplo de ``RENDERED``/``REFRAMED_9_16``/
``TRANSCRIBED``) e ``READY`` é a conjunção literal das expressões de
``RENDERED`` E ``VALIDATED`` -- exatamente a definição do roadmap
("READY automático não pode nascer apenas de um checkbox manual").

Os outros 6 badges (``METADATA_CLEAN``, ``AUDIO_PROCESSED``,
``TEMPLATE_APPLIED``, ``AI_TITLE``, ``AI_DESCRIPTION``,
``AI_HASHTAGS``) permanecem estruturalmente impossíveis de disparar
hoje -- nenhum módulo produz evidência persistida (``Artifact``/
checkpoint) para nenhum deles ainda. Nenhum destes é removido do
vocabulário (``SYSTEM_BADGES`` continua com os 17) -- apenas nunca
aparecem em ``MediaCatalogItem.system_badges`` até que a evidência real
exista.

===========================================================================
0.7 -- INTEGRAÇÃO COM PROMPT 31 (BADGES ``CAPTIONS``/``TRANSCRIBED``)
===========================================================================

Fonte de verdade reutilizada, NUNCA duplicada (``captions_engine.py``,
Prompt 31, não alterado por este Prompt -- só lido via SQL, read-only):

- ``CAPTIONS``: mesmo critério de cache hit que ``CaptionsEngine.
  _find_cached_artifacts`` já usa internamente -- 3 linhas em
  ``artifacts`` (``kind`` em ``transcript_internal``/``captions_srt``/
  ``captions_vtt``) para o mesmo ``video_id`` E o mesmo ``fingerprint``
  (a própria cache key do CaptionsEngine). Não reinventa esse critério:
  reproduz literalmente a mesma consulta ``WHERE video_id = ? AND kind =
  ? AND fingerprint = ?`` que já decide cache hit/miss em produção.
- ``TRANSCRIBED``: ``CHECKPOINT_TRANSCRIBED`` (``domain/checkpoints.py``)
  gravado via ``OperationalAuditLog.record_checkpoint`` (``storage/
  audit.py``) para QUALQUER ``Job`` deste vídeo -- checkpoints são
  eventos append-only em ``audit_events`` (``entity_type='Job'``,
  ``event_type='JOB_CHECKPOINT_REACHED'``, ``data_json->>'checkpoint'``),
  já protegidos contra UPDATE/DELETE pelo authorizer da própria conexão
  (ver ``storage/database.py``). Decisão documentada: o checkpoint
  SOZINHO não basta -- também exige um ``Artifact`` ``transcript_internal``
  cujo arquivo ainda exista no disco (mesma checagem usada para
  ``CAPTIONS``, ver abaixo). Motivo: um checkpoint é um FATO histórico
  imutável ("esta transcrição terminou em algum momento"), mas por si só
  não prova que a evidência persistida sobrevive até HOJE (arquivo pode
  ter sido apagado depois -- seção 7(B) do Prompt). Usar só o checkpoint
  arriscaria um falso positivo idêntico ao que a seção 7(B) proíbe
  explicitamente para ``CAPTIONS``; exigir os dois evita duplicar uma
  segunda fonte de verdade (não criamos nenhum estado novo -- só
  combinamos dois fatos que já existiam).

Checagem de arquivo no disco -- ``catalog_fs_file_ok`` (função SQL
custom, registrada via ``sqlite3.Connection.create_function`` em
``MediaCatalogService._connection``, o único lugar deste módulo que abre
conexão para leitura): confirma que ``path`` é um arquivo regular
existente e não-vazio (``os.path.isfile`` + ``getsize() > 0``). Não
reabre/parseia o conteúdo do SRT/VTT/JSON (validar sintaxe linha a linha
em toda consulta do catálogo não é papel do catálogo -- é responsabilidade
do ``CaptionsEngine`` no momento em que o arquivo é gravado; reler todo
arquivo em toda listagem também violaria a arquitetura de duas consultas
da seção 0.5, que existe justamente para não pagar custo por linha
descartada). Nunca lança: qualquer erro do SO vira "não ok" (``0``) --
uma badge problemática não pode derrubar a listagem inteira do catálogo.

Por que ``create_function`` (e não um segundo mecanismo, nem um loop
Python pós-SQL): a checagem de disco em si é necessariamente algo que só
Python/SO sabem responder -- SQL puro não tem acesso a filesystem. As
alternativas consideradas e rejeitadas: (1) resolver os candidatos via
SQL e depois filtrar em um loop Python -- reintroduziria exatamente o
padrão "carregar tudo em memória para filtrar" que a seção 0.5/1.2 deste
módulo documenta como deliberadamente evitado (``filter_items``/
``count_by_filter``/``get_facets`` dependem de uma única consulta SQL
correlacionada, não de pós-processamento); (2) inventar uma segunda
tabela/coluna cacheando "arquivo existe" -- proibido explicitamente pelo
Prompt (segunda fonte de verdade, e ficaria stale assim que o arquivo
fosse apagado por fora). Registrar uma função Python callável de dentro
do SQL preserva a MESMA arquitetura de ``_BADGE_SQL_EXPRESSIONS``
(continua sendo "uma expressão booleana por badge, avaliada pelo
SQLite") enquanto devolve uma resposta sempre fresca (nunca cacheada
entre chamadas -- cada ``MediaCatalogService`` método abre sua própria
conexão via ``self.database.connection()``, então a função é registrada
de novo a cada leitura, nunca reaproveitada entre requisições). O
authorizer de ``LocalDatabase._connect`` (``storage/database.py``) só
nega ``UPDATE``/``DELETE`` em ``audit_events`` -- não intercepta chamada
de função SQL, então nenhuma mudança foi necessária lá.

``AUDIO_PROCESSED`` permanece fora de escopo (ver acima) -- destravar no
futuro exigiria que algum módulo real de processamento de áudio
(inexistente hoje, não planejado em nenhum Prompt numerado) persistisse
evidência equivalente (``Artifact``/checkpoint) que este mecanismo
pudesse então referenciar da mesma forma.

===========================================================================
0.8 -- INTEGRAÇÃO COM PROMPT 33 (BADGE ``REFRAMED_9_16``)
===========================================================================

Fonte de verdade reutilizada, NUNCA duplicada (``auto_reframe.py``,
Prompt 33, não alterado por este Prompt -- só lido via SQL, read-only):
``CHECKPOINT_MEDIA_PROCESSED`` (``domain/checkpoints.py``) gravado via
``OperationalAuditLog.record_checkpoint`` para um ``Job`` deste vídeo, E
um ``Artifact`` ``reframe_track`` com arquivo real no disco (mesma função
``catalog_fs_file_ok`` da seção 0.7, reaproveitada -- nenhuma segunda
função criada).

RISCO IMPORTANTE, investigado e confirmado ANTES de escrever este SQL:
diferente de ``CHECKPOINT_TRANSCRIBED`` (que já nasceu exclusivo do
CaptionsEngine), ``CHECKPOINT_MEDIA_PROCESSED`` é um checkpoint do
vocabulário GERAL (``domain/checkpoints.py`` documenta explicitamente que
"cada operação decide, na prática, quais etapas percorre" -- não é
reservado a nenhum módulo por design). Confirmado por grep nesta rodada
que HOJE só ``auto_reframe.py`` grava esse checkpoint -- essa é a premissa
que torna a badge segura AGORA. Se um Prompt futuro reaproveitar
``MEDIA_PROCESSED`` para outra operação (ex.: um módulo de correção de
cor, ou qualquer outro processamento de mídia), uma badge que olhasse
SÓ para o checkpoint ficaria ambígua entre "foi reenquadrado" e "outra
operação qualquer atingiu esse checkpoint genérico". Por isso a expressão
SQL EXIGE TAMBÉM o ``Artifact`` ``reframe_track`` (mesmo padrão já
decidido para ``TRANSCRIBED`` na seção 0.7, pelo mesmo motivo estrutural:
o checkpoint sozinho é um fato histórico, mas não prova QUAL operação o
produziu nem se a evidência específica sobrevive até hoje) -- como só
``auto_reframe.py`` cria esse ``kind`` de Artifact hoje, a combinação
dos dois permanece inequívoca mesmo que o checkpoint deixe de ser
exclusivo no futuro. RECOMENDAÇÃO REGISTRADA para qualquer badge futura
baseada em checkpoint: sempre exigir TAMBÉM o Artifact correspondente,
nunca o checkpoint isolado -- exatamente a regra que já protegeu
``TRANSCRIBED`` e que agora protege ``REFRAMED_9_16`` contra a mesma
classe de ambiguidade, mesmo com uma premissa (checkpoint exclusivo) que
pode deixar de valer sem aviso.

DECISÃO DE DESIGN MAIS IMPORTANTE DESTA RODADA -- FALLBACK CONTA PARA A
BADGE: ``AutoReframeEngine`` (Prompt 33) trata um resultado de FALLBACK
(crop central estático, quando o detector não encontra nada ou lança
exceção) como um ``JOB_READY`` bem-sucedido, produzindo um ``Artifact``
``reframe_track`` real e válido (marcado ``"fallback": true`` no JSON,
mas fisicamente idêntico em estrutura a um resultado com detecção real) --
nunca uma falha, nunca um resultado degradado do ponto de vista do
próprio Job. Duas leituras possíveis foram avaliadas:

  (a) a badge representa "o AutoReframe RODOU com sucesso e produziu um
      resultado utilizável" -- fallback conta;
  (b) a badge representa "o vídeo foi genuinamente REENQUADRADO por
      detecção real (rosto/pessoa/região)" -- fallback NÃO conta.

DECISÃO: (a). Nenhuma outra badge deste catálogo distingue "qualidade"
da evidência -- ``CAPTIONS`` não olha se a transcrição saiu de um modelo
Whisper pequeno ou grande, nem se o áudio era ruim; ``EDITED`` não olha
se a edição foi "boa"; o catálogo inteiro é arquitetado para responder
"existe evidência estruturada real?", nunca "essa evidência é de alta
qualidade?" (ver seção 0.2: "o catálogo NÃO deve duplicar a verdade
operacional" -- avaliar qualidade seria inventar uma nova dimensão de
verdade que não existe hoje em nenhum outro badge). Introduzir uma
distinção de qualidade SÓ para esta badge quebraria essa consistência sem
necessidade demonstrada (CLAUDE.md: não fazer overengineering sem
benefício concreto) e exigiria decidir e expor um conceito novo ("badge
de reenquadramento degradado") que o roadmap não pede. A expressão SQL
acima, portanto, NÃO inspeciona o campo ``fallback`` do JSON -- apenas a
existência do checkpoint + Artifact com arquivo real, exatamente como
``TRANSCRIBED``. Testado explicitamente
(``test_reframed_9_16_acende_mesmo_com_resultado_fallback``).

===========================================================================
0.5 -- CAMADA DE CONSULTA
===========================================================================

Nenhum método deste módulo usa ``LocalDatabase.list()`` (que só suporta
``ORDER BY created_at, id [LIMIT ?]``, sem ``WHERE``/``OFFSET``/``JOIN``).
Toda listagem, filtro, contagem e faceta usa SQL parametrizado via
``LocalDatabase.connection()`` (devolve ``sqlite3.Connection`` cru), com
``WHERE``/``LIMIT``/``OFFSET``/``JOIN`` conforme necessário. Nomes de
badge vindos de fora (filtros) são validados contra ``SYSTEM_BADGES``
(allowlist fechada) ANTES de indexar ``_BADGE_SQL_EXPRESSIONS`` -- nenhum
fragmento SQL é construído a partir de texto arbitrário do chamador;
valores de dado (``video_id``, ``value`` de declaração, limites de
paginação) são sempre parâmetros ``?``, nunca concatenação de string.

===========================================================================
0.6 -- PERSISTÊNCIA DE MARCAÇÕES MANUAIS
===========================================================================

``video_declarations`` (``m009_video_declarations.py``) é uma tabela NOVA
append-only, keyed por ``video_id`` -- não reaproveita
``source_asset_declarations`` (m007, keyed por ``source_asset_id``,
entidade diferente, FK errada). Mesmo padrão já usado em
``source_import.py``/``m007``: cada linha é um evento
(``action='ADD'``/``'REMOVE'``) para uma chave lógica (``video_id``,
``kind``, ``value``); o estado atual é sempre derivado via
``ROW_NUMBER() OVER (PARTITION BY video_id, kind, value ORDER BY rowid
DESC)`` -- nunca ``UPDATE``/``DELETE`` diretos (a própria tabela nega via
trigger). ``origin`` é fixado em ``'USER'`` -- ``SYSTEM_BADGES`` NUNCA são
graváveis nesta tabela (nem a coluna ``kind`` aceita nada além de
``ASSERTION``/``FLAG``/``LABEL``).

Índice ``idx_schedules_publication_id`` já existe desde ``m001`` (e já é
``UNIQUE``) -- confirmado por leitura completa de ``m001_initial.py``
antes de desenhar ``m009``; nenhum índice novo foi necessário nas tabelas
já existentes para o caminho SCHEDULED. ``m009`` adiciona apenas os
índices que a NOVA tabela exige: ``(video_id)``, ``(video_id, kind,
value)`` e ``(kind, value)`` -- ver a docstring da própria migration.

===========================================================================
1.2 -- FACETS
===========================================================================

``get_facets`` roda UMA única consulta SQL com ``SUM(CASE WHEN <expr>
THEN 1 ELSE 0 END)`` por badge -- nunca carrega itens completos em
memória para contar.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import os
import sqlite3
from typing import Any, ClassVar, Iterator, Mapping, Sequence
from uuid import UUID

from .domain import new_uuid
from .storage.database import LocalDatabase
from .time_utils import utc_now_iso

# ---------------------------------------------------------------------------
# SYSTEM_BADGES -- vocabulário fechado dos 17 badges do roadmap (ordem
# literal da especificação). Nunca alterado por marcação manual.
# ---------------------------------------------------------------------------
BADGE_IMPORTED = "IMPORTED"
BADGE_VALIDATED = "VALIDATED"
BADGE_EDITED = "EDITED"
BADGE_CAPTIONS = "CAPTIONS"
BADGE_METADATA_CLEAN = "METADATA_CLEAN"
BADGE_REFRAMED_9_16 = "REFRAMED_9_16"
BADGE_AUDIO_PROCESSED = "AUDIO_PROCESSED"
BADGE_TEMPLATE_APPLIED = "TEMPLATE_APPLIED"
BADGE_AI_TITLE = "AI_TITLE"
BADGE_AI_DESCRIPTION = "AI_DESCRIPTION"
BADGE_AI_HASHTAGS = "AI_HASHTAGS"
BADGE_TRANSCRIBED = "TRANSCRIBED"
BADGE_RENDERED = "RENDERED"
BADGE_READY = "READY"
BADGE_SCHEDULED = "SCHEDULED"
BADGE_PUBLISHED = "PUBLISHED"
BADGE_ERROR = "ERROR"

SYSTEM_BADGES: tuple[str, ...] = (
    BADGE_IMPORTED,
    BADGE_VALIDATED,
    BADGE_EDITED,
    BADGE_CAPTIONS,
    BADGE_METADATA_CLEAN,
    BADGE_REFRAMED_9_16,
    BADGE_AUDIO_PROCESSED,
    BADGE_TEMPLATE_APPLIED,
    BADGE_AI_TITLE,
    BADGE_AI_DESCRIPTION,
    BADGE_AI_HASHTAGS,
    BADGE_TRANSCRIBED,
    BADGE_RENDERED,
    BADGE_READY,
    BADGE_SCHEDULED,
    BADGE_PUBLISHED,
    BADGE_ERROR,
)
_SYSTEM_BADGES_SET = frozenset(SYSTEM_BADGES)

# ---------------------------------------------------------------------------
# Função SQL custom usada por CAPTIONS/TRANSCRIBED (ver docstring 0.7) --
# registrada por conexão em ``MediaCatalogService._connection``, nunca em
# ``LocalDatabase`` (evita acoplar um módulo genérico a este mecanismo).
# ---------------------------------------------------------------------------
_CATALOG_FS_FUNCTION_NAME = "catalog_fs_file_ok"


def _fs_file_ok(path: object) -> int:
    """Confirma evidência de arquivo real no disco para uma badge.

    Usada exclusivamente como função SQL registrada via
    ``sqlite3.Connection.create_function`` -- nunca chamada diretamente
    por outro código deste módulo. Devolve ``1`` somente se ``path`` for
    uma string não vazia apontando para um arquivo regular existente e
    não-vazio; qualquer outro caso (``None``, tipo errado, caminho
    inexistente, ``OSError`` de permissão/caminho malformado no Windows)
    devolve ``0`` -- nunca lança, porque uma badge problemática não pode
    derrubar a consulta inteira do catálogo (mesma filosofia de
    isolamento de falha do CLAUDE.md, item CONFIABILIDADE)."""
    if not isinstance(path, str) or not path:
        return 0
    try:
        return 1 if os.path.isfile(path) and os.path.getsize(path) > 0 else 0
    except OSError:
        return 0


# Expressão SQL booleana (referenciando o alias ``v`` de ``videos``) para
# cada badge -- ``0`` (sempre falso) para todo badge sem fonte de verdade
# real hoje. Nenhum parâmetro externo entra nestas strings; são literais
# fixos escritos neste módulo, nunca construídos a partir de entrada do
# chamador (só o NOME do badge, validado contra ``_SYSTEM_BADGES_SET``,
# decide qual entrada fixa deste dicionário é usada).
_BADGE_SQL_EXPRESSIONS: dict[str, str] = {
    BADGE_IMPORTED: "EXISTS (SELECT 1 FROM sources src WHERE src.id = v.source_asset_id)",
    # VALIDATED -- mesmo padrão duplo de RENDERED/REFRAMED_9_16/TRANSCRIBED
    # (Prompt 43, final_media_validator.py): checkpoint VALIDATED
    # (domain/checkpoints.py) gravado via
    # OperationalAuditLog.record_checkpoint SOMENTE quando os 10 itens do
    # checklist de qualidade final passam (nenhum FAIL, nenhum
    # USER_ACTION_REQUIRED) E o MESMO Artifact render_output (kind
    # reaproveitado -- FinalMediaValidator nunca produz um Artifact
    # próprio, só valida/corrige o existente) com arquivo real no disco.
    # Checkpoint sozinho não basta -- mesma exigência já aplicada a
    # RENDERED: só prova que uma validação passou um dia, não que o
    # arquivo daquele Artifact ainda existe/é o mesmo hoje.
    BADGE_VALIDATED: (
        "(EXISTS (SELECT 1 FROM jobs j JOIN audit_events ae "
        "ON ae.entity_type = 'Job' AND ae.entity_id = j.id "
        "WHERE j.video_id = v.id AND ae.event_type = 'JOB_CHECKPOINT_REACHED' "
        "AND json_extract(ae.data_json, '$.checkpoint') = 'VALIDATED') "
        "AND EXISTS (SELECT 1 FROM artifacts a WHERE a.video_id = v.id "
        "AND a.kind = 'render_output' "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a.path) = 1))"
    ),
    BADGE_EDITED: (
        "EXISTS (SELECT 1 FROM projects p WHERE p.video_id = v.id "
        "AND EXISTS (SELECT 1 FROM json_each(json_extract(p.edit_state_json, '$.categories'))))"
    ),
    # CAPTIONS -- mesmo critério de cache hit de
    # ``CaptionsEngine._find_cached_artifacts`` (Prompt 31): as 3 linhas em
    # ``artifacts`` (transcript_internal/captions_srt/captions_vtt) para o
    # mesmo video_id E o mesmo fingerprint (cache key), cada uma com
    # arquivo real no disco (ver docstring 0.7). Nunca considera completo
    # um Job PENDING/PROCESSING/FAILED/CANCELLED/UNKNOWN por si só -- só a
    # existência dos 3 Artifacts é avaliada (mesma regra que já decide
    # cache hit/miss em produção); um Job FAILED posterior não apaga um
    # Artifact válido de uma execução anterior bem-sucedida (seção 7(D)).
    BADGE_CAPTIONS: (
        "EXISTS (SELECT 1 FROM artifacts a1 WHERE a1.video_id = v.id "
        "AND a1.kind = 'captions_srt' AND a1.fingerprint IS NOT NULL "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a1.path) = 1 "
        "AND EXISTS (SELECT 1 FROM artifacts a2 WHERE a2.video_id = v.id "
        "AND a2.kind = 'captions_vtt' AND a2.fingerprint = a1.fingerprint "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a2.path) = 1) "
        "AND EXISTS (SELECT 1 FROM artifacts a3 WHERE a3.video_id = v.id "
        "AND a3.kind = 'transcript_internal' AND a3.fingerprint = a1.fingerprint "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a3.path) = 1))"
    ),
    BADGE_METADATA_CLEAN: "0",
    # REFRAMED_9_16 -- mesmo padrão de TRANSCRIBED (Prompt "Catalog Wiring
    # 27.5+30+31"): checkpoint MEDIA_PROCESSED (domain/checkpoints.py)
    # gravado via OperationalAuditLog.record_checkpoint para QUALQUER Job
    # deste vídeo, E um Artifact reframe_track com arquivo real no disco
    # (ver docstring 0.8 -- o Artifact é o que resolve a ambiguidade do
    # checkpoint, que NÃO é exclusivo do AutoReframe). Fallback (crop
    # central estático, Artifact com "fallback": true no JSON) CONTA para
    # esta badge -- decisão documentada na seção 0.8.
    BADGE_REFRAMED_9_16: (
        "(EXISTS (SELECT 1 FROM jobs j JOIN audit_events ae "
        "ON ae.entity_type = 'Job' AND ae.entity_id = j.id "
        "WHERE j.video_id = v.id AND ae.event_type = 'JOB_CHECKPOINT_REACHED' "
        "AND json_extract(ae.data_json, '$.checkpoint') = 'MEDIA_PROCESSED') "
        "AND EXISTS (SELECT 1 FROM artifacts a WHERE a.video_id = v.id "
        "AND a.kind = 'reframe_track' "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a.path) = 1))"
    ),
    # AUDIO_PROCESSED -- deliberadamente fora de escopo (ver docstring
    # 0.2/0.7): audio_engine.py (Prompt 30) nunca cria Job/Artifact/
    # checkpoint, é 100% decisão/configuração. Não há evidência real para
    # referenciar hoje; permanece "0" até que um passo real de
    # processamento de áudio exista (nenhum planejado ainda).
    BADGE_AUDIO_PROCESSED: "0",
    BADGE_TEMPLATE_APPLIED: "0",
    BADGE_AI_TITLE: "0",
    BADGE_AI_DESCRIPTION: "0",
    BADGE_AI_HASHTAGS: "0",
    # TRANSCRIBED -- checkpoint CHECKPOINT_TRANSCRIBED (domain/checkpoints.py)
    # gravado via OperationalAuditLog.record_checkpoint para QUALQUER Job
    # deste vídeo, E um Artifact transcript_internal com arquivo real no
    # disco (decisão documentada na docstring 0.7: checkpoint sozinho não
    # basta, porque não prova que a evidência sobrevive até hoje).
    BADGE_TRANSCRIBED: (
        "(EXISTS (SELECT 1 FROM jobs j JOIN audit_events ae "
        "ON ae.entity_type = 'Job' AND ae.entity_id = j.id "
        "WHERE j.video_id = v.id AND ae.event_type = 'JOB_CHECKPOINT_REACHED' "
        "AND json_extract(ae.data_json, '$.checkpoint') = 'TRANSCRIBED') "
        "AND EXISTS (SELECT 1 FROM artifacts a WHERE a.video_id = v.id "
        "AND a.kind = 'transcript_internal' "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a.path) = 1))"
    ),
    # RENDERED -- mesmo padrão duplo de REFRAMED_9_16/TRANSCRIBED (Prompt
    # 42, render_engine.py): checkpoint RENDERED (domain/checkpoints.py)
    # gravado via OperationalAuditLog.record_checkpoint E um Artifact
    # render_output com arquivo real no disco -- checkpoint sozinho não
    # basta (exigência literal do roadmap do Prompt 42: "a existência de
    # arquivo parcial não basta"). Um cache-hit também grava este
    # checkpoint (render_engine.py) reaproveitando o Artifact já existente,
    # então a badge acende igualmente nesse caso -- correto, pois o
    # arquivo final validado já existe no disco desde o render original.
    BADGE_RENDERED: (
        "(EXISTS (SELECT 1 FROM jobs j JOIN audit_events ae "
        "ON ae.entity_type = 'Job' AND ae.entity_id = j.id "
        "WHERE j.video_id = v.id AND ae.event_type = 'JOB_CHECKPOINT_REACHED' "
        "AND json_extract(ae.data_json, '$.checkpoint') = 'RENDERED') "
        "AND EXISTS (SELECT 1 FROM artifacts a WHERE a.video_id = v.id "
        "AND a.kind = 'render_output' "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a.path) = 1))"
    ),
    # READY -- conjunção literal exigida pelo docstring histórico deste
    # módulo ("READY depende de RENDERED AND VALIDATED", Prompt 42/43):
    # a MESMA expressão de RENDERED E a MESMA expressão de VALIDATED,
    # nenhuma reimplementação/duplicação da lógica -- se qualquer uma das
    # duas mudar de definição no futuro, esta entrada muda junto
    # automaticamente (evita duas fontes de verdade divergentes, GATE
    # item 7).
    BADGE_READY: (
        "("
        "(EXISTS (SELECT 1 FROM jobs j JOIN audit_events ae "
        "ON ae.entity_type = 'Job' AND ae.entity_id = j.id "
        "WHERE j.video_id = v.id AND ae.event_type = 'JOB_CHECKPOINT_REACHED' "
        "AND json_extract(ae.data_json, '$.checkpoint') = 'RENDERED') "
        "AND EXISTS (SELECT 1 FROM artifacts a WHERE a.video_id = v.id "
        "AND a.kind = 'render_output' "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a.path) = 1))"
        " AND "
        "(EXISTS (SELECT 1 FROM jobs j JOIN audit_events ae "
        "ON ae.entity_type = 'Job' AND ae.entity_id = j.id "
        "WHERE j.video_id = v.id AND ae.event_type = 'JOB_CHECKPOINT_REACHED' "
        "AND json_extract(ae.data_json, '$.checkpoint') = 'VALIDATED') "
        "AND EXISTS (SELECT 1 FROM artifacts a WHERE a.video_id = v.id "
        "AND a.kind = 'render_output' "
        f"AND {_CATALOG_FS_FUNCTION_NAME}(a.path) = 1))"
        ")"
    ),
    BADGE_SCHEDULED: (
        "EXISTS (SELECT 1 FROM publications pub JOIN schedules sch "
        "ON sch.publication_id = pub.id WHERE pub.video_id = v.id "
        "AND sch.delivery_state IN ('LOCAL_PENDING', 'REMOTE_SCHEDULED'))"
    ),
    BADGE_PUBLISHED: "EXISTS (SELECT 1 FROM jobs j WHERE j.video_id = v.id AND j.status = 'PUBLISHED')",
    BADGE_ERROR: (
        "(EXISTS (SELECT 1 FROM jobs j WHERE j.video_id = v.id AND j.status = 'FAILED') "
        "OR EXISTS (SELECT 1 FROM errors e WHERE e.entity_id = v.id))"
    ),
}
assert frozenset(_BADGE_SQL_EXPRESSIONS) == _SYSTEM_BADGES_SET

# Badges que hoje NUNCA podem disparar (sem fonte de verdade real) -- lista
# derivada automaticamente do dicionário acima (documentação/teste usam
# esta constante para provar exaustividade, nunca uma lista solta
# separada que poderia divergir).
IMPOSSIBLE_BADGES_TODAY: frozenset[str] = frozenset(
    badge for badge, expr in _BADGE_SQL_EXPRESSIONS.items() if expr == "0"
)
DERIVABLE_BADGES_TODAY: frozenset[str] = _SYSTEM_BADGES_SET - IMPOSSIBLE_BADGES_TODAY

# ---------------------------------------------------------------------------
# video_declarations -- kind/origin (ver docstring do módulo, seção 0.6).
# ---------------------------------------------------------------------------
DECLARATION_KIND_ASSERTION = "ASSERTION"
DECLARATION_KIND_FLAG = "FLAG"
DECLARATION_KIND_LABEL = "LABEL"
DECLARATION_KINDS = frozenset(
    {DECLARATION_KIND_ASSERTION, DECLARATION_KIND_FLAG, DECLARATION_KIND_LABEL}
)
ORIGIN_USER = "USER"

# Vocabulário de exemplo (roadmap) para USER_FLAG -- livre por natureza,
# nunca uma lista fechada imposta pelo módulo (mesmo espírito de
# ``source_import.py`` para LABEL/ASSERTION).
FLAG_REVIEWED = "REVIEWED"
FLAG_APPROVED = "APPROVED"
FLAG_FAVORITE = "FAVORITE"
FLAG_NEEDS_REWORK = "NEEDS_REWORK"
FLAG_PRIORITY = "PRIORITY"
FLAG_USER_MARKED_READY = "USER_MARKED_READY"

# ---------------------------------------------------------------------------
# Bulk edit -- KEEP/SET/UNSET (roadmap).
# ---------------------------------------------------------------------------
BULK_KEEP = "KEEP"
BULK_SET = "SET"
BULK_UNSET = "UNSET"
BULK_MODES = frozenset({BULK_KEEP, BULK_SET, BULK_UNSET})


# ---------------------------------------------------------------------------
# Erros estruturados (mesmo padrão ``.code`` de edit_project.py/video_promotion.py)
# ---------------------------------------------------------------------------
class MediaCatalogError(RuntimeError):
    """Base para todo erro estruturado deste módulo."""

    code: ClassVar[str] = "MEDIA_CATALOG_ERRO"


class VideoIdInvalidoError(MediaCatalogError):
    """``video_id`` não é um UUID textual válido."""

    code: ClassVar[str] = "VIDEO_ID_INVALIDO"


class VideoNaoEncontradoError(MediaCatalogError):
    """``video_id`` é um UUID válido, mas nenhum ``Video`` corresponde."""

    code: ClassVar[str] = "VIDEO_NAO_ENCONTRADO"


class BadgeDesconhecidoError(MediaCatalogError):
    """Um filtro referencia um nome de badge fora de ``SYSTEM_BADGES``."""

    code: ClassVar[str] = "BADGE_DESCONHECIDO"


class DeclaracaoInvalidaError(MediaCatalogError):
    """``kind``/``value`` inválidos para uma marcação manual."""

    code: ClassVar[str] = "DECLARACAO_INVALIDA"


class ModoBulkInvalidoError(MediaCatalogError):
    """``mode`` de bulk edit fora de ``BULK_MODES``."""

    code: ClassVar[str] = "MODO_BULK_INVALIDO"


class PaginacaoInvalidaError(MediaCatalogError):
    """``limit``/``offset`` fora do intervalo aceito."""

    code: ClassVar[str] = "PAGINACAO_INVALIDA"


def _validate_video_id(value: object) -> str:
    if not isinstance(value, str):
        raise VideoIdInvalidoError(f"video_id deve ser string, recebido {type(value).__name__}")
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise VideoIdInvalidoError(f"video_id não é um UUID válido: {value!r}") from exc
    return value


def _validate_declaration_value(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) == 0:
        raise DeclaracaoInvalidaError(f"value deve ser uma string não vazia, recebido {value!r}")
    return value


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ContentStatus:
    """Reflete SOMENTE o que é verificável hoje (item 1.1 do Prompt).

    ``title_available``/``description_available`` derivam de
    ``Publication.title``/``Publication.description`` não-vazios
    associados ao vídeo -- evidência REAL de que um título/descrição
    existe, mas SEM proveniência de IA confirmada (não confundir com o
    badge ``AI_TITLE``/``AI_DESCRIPTION``, que exige uma fonte
    ``ContentEngine`` inexistente hoje). ``hashtags_available`` também
    fica sempre ``False`` -- ``Publication`` não tem nenhuma coluna de
    hashtags no schema atual, logo não há evidência alguma para derivar.
    ``transcript_available``/``captions_available`` ficam sempre
    ``False`` -- desde o Prompt "CATALOG WIRING / RECONCILIATION
    27.5+30+31", ``CaptionsEngine`` (Prompt 31) já produz evidência real
    (ver ``system_badges``: ``CAPTIONS``/``TRANSCRIBED``, derivadas em
    ``_BADGE_SQL_EXPRESSIONS``, docstring 0.7), mas esse Prompt teve
    escopo estritamente limitado a essas duas badges -- wire-up destes
    dois campos aqui seria expandir escopo não pedido (STOP RULE do
    Prompt), então ficam como estavam: sempre ``False``, campo por campo
    reservado a uma fonte de verdade futura de ``ContentEngine``/IA (não
    confundir com as badges CAPTIONS/TRANSCRIBED, que já refletem o
    estado real via ``system_badges``).
    """

    title_available: bool = False
    description_available: bool = False
    hashtags_available: bool = False
    transcript_available: bool = False
    captions_available: bool = False


@dataclass(frozen=True)
class PublicationSummary:
    """``published_count``/``failed_count`` usam o vocabulário fechado de
    ``Job.status`` (``PUBLISHED``/``FAILED``) -- deliberadamente NUNCA
    ``Publication.status`` (ver seção 0.3 da docstring do módulo).
    ``destinations_count`` conta contas (``account_id``) distintas com
    ``Publication`` associada a este vídeo. ``scheduled_count`` conta
    ``Publication``s com ``Schedule`` em estado pendente/agendado.
    """

    destinations_count: int = 0
    scheduled_count: int = 0
    published_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class MediaCatalogItem:
    """Read model do catálogo -- NUNCA a fonte de verdade em si (item
    OBJETIVO do roadmap: "o catálogo NÃO deve duplicar a verdade
    operacional"). Todo campo dependente de fonte ausente hoje tem um
    valor "desconhecido" bem definido (``None``/``False``/tupla vazia),
    nunca uma exceção nem um valor inventado (item 1.1 do Prompt)."""

    video_id: str
    source_asset_id: str | None
    filename: str | None
    display_name: str
    created_at: str
    duration: float | None
    resolution: str | None
    thumbnail_ref: str | None
    processing_status: str | None
    system_badges: tuple[str, ...]
    user_assertions: tuple[str, ...]
    user_flags: tuple[str, ...]
    user_labels: tuple[str, ...]
    latest_project_id: str | None
    latest_artifact_id: str | None
    latest_operation: str | None
    error_summary: str | None
    content_status: ContentStatus
    publication_summary: PublicationSummary


@dataclass(frozen=True)
class CatalogFilter:
    """Filtro combinável AND/NOT sobre ``SYSTEM_BADGES`` (roadmap: "CAPTIONS
    AND METADATA_CLEAN AND AI_TITLE AND NOT PUBLISHED"). Todo nome de
    badge é validado contra ``SYSTEM_BADGES`` em ``__post_init__`` --
    nunca aceito silenciosamente."""

    badges_all: tuple[str, ...] = ()
    badges_none: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for badge in (*self.badges_all, *self.badges_none):
            if badge not in _SYSTEM_BADGES_SET:
                raise BadgeDesconhecidoError(
                    f"badge desconhecido em filtro: {badge!r} (esperado um de {sorted(_SYSTEM_BADGES_SET)})"
                )


@dataclass(frozen=True)
class BulkFieldOp:
    """Uma operação de bulk edit sobre UM campo (assertions/flags/labels).

    ``mode=KEEP`` (padrão): este campo não é tocado -- nenhuma escrita
    acontece para ele, para NENHUM vídeo do lote (garante que uma
    operação em massa não apaga silenciosamente marcações que o usuário
    não pediu para alterar). ``mode=SET``: garante que cada valor em
    ``values`` esteja ATIVO para cada vídeo (só grava um evento ``ADD``
    se ainda não estiver ativo -- idempotente). ``mode=UNSET``: garante
    que cada valor em ``values`` esteja INATIVO (só grava ``REMOVE`` se
    estiver ativo)."""

    mode: str = BULK_KEEP
    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in BULK_MODES:
            raise ModoBulkInvalidoError(f"mode desconhecido: {self.mode!r} (esperado um de {sorted(BULK_MODES)})")
        if self.mode in (BULK_SET, BULK_UNSET):
            if not self.values:
                raise DeclaracaoInvalidaError(f"mode={self.mode} exige ao menos um valor em 'values'")
            for value in self.values:
                _validate_declaration_value(value)


@dataclass(frozen=True)
class BulkEditResult:
    """``requested``/``updated``/``unchanged``/``failed`` -- exigido
    literalmente pelo roadmap. ``failed`` traz ``(video_id, code)`` para
    permitir diagnóstico sem nunca persistir texto de exceção crua."""

    requested: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]


def _filename_and_display_name(video_name: str, original_name: str | None, local_path: str | None, video_id: str) -> tuple[str | None, str]:
    """Deriva ``filename``/``display_name`` a partir de evidência real
    (``SourceAsset.original_name``/``local_path``, ``Video.name``) --
    mesma prioridade conceitual de ``VideoPromotionService._derive_video_name``
    (Prompt 27b), reimplementada aqui (não importada: é uma função privada
    de outro módulo, e este módulo não deve acoplar-se a um símbolo
    interno de ``video_promotion.py`` que pode mudar sem contrato)."""
    filename: str | None = None
    if original_name and original_name.strip():
        filename = original_name.strip()
    elif local_path and local_path.strip():
        filename = os.path.basename(local_path.strip()) or None
    display_name = video_name.strip() if video_name and video_name.strip() else None
    if display_name is None:
        display_name = filename
    if display_name is None:
        display_name = f"video-sem-nome-{video_id[:8]}"
    return filename, display_name


# Colunas selecionadas pela consulta-base (ver ``_BASE_SELECT``) -- uma
# única fonte para list_items/filter_items/get_item, nunca duplicada.
_BASE_SELECT = (
    "SELECT v.id AS video_id, v.source_asset_id AS source_asset_id, v.name AS video_name, "
    "v.created_at AS created_at, "
    "src.original_name AS source_original_name, src.local_path AS source_local_path, "
    "(SELECT j.status FROM jobs j WHERE j.video_id = v.id ORDER BY j.created_at DESC, j.rowid DESC LIMIT 1) AS processing_status, "
    "(SELECT j.operation FROM jobs j WHERE j.video_id = v.id ORDER BY j.created_at DESC, j.rowid DESC LIMIT 1) AS latest_operation, "
    "(SELECT p.id FROM projects p WHERE p.video_id = v.id ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1) AS latest_project_id, "
    "(SELECT a.id FROM artifacts a WHERE a.video_id = v.id ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1) AS latest_artifact_id, "
    "(SELECT (e.code || ': ' || e.message) FROM errors e WHERE e.entity_id = v.id ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1) AS error_summary, "
    "(SELECT COUNT(DISTINCT pub.account_id) FROM publications pub WHERE pub.video_id = v.id) AS destinations_count, "
    "(SELECT COUNT(*) FROM publications pub JOIN schedules sch ON sch.publication_id = pub.id "
    " WHERE pub.video_id = v.id AND sch.delivery_state IN ('LOCAL_PENDING', 'REMOTE_SCHEDULED')) AS scheduled_count, "
    "(SELECT COUNT(*) FROM jobs j WHERE j.video_id = v.id AND j.status = 'PUBLISHED') AS published_count, "
    "(SELECT COUNT(*) FROM jobs j WHERE j.video_id = v.id AND j.status = 'FAILED') AS failed_count, "
    "CASE WHEN EXISTS (SELECT 1 FROM publications pub WHERE pub.video_id = v.id "
    " AND pub.title IS NOT NULL AND length(pub.title) > 0) THEN 1 ELSE 0 END AS content_title, "
    "CASE WHEN EXISTS (SELECT 1 FROM publications pub WHERE pub.video_id = v.id "
    " AND pub.description IS NOT NULL AND length(pub.description) > 0) THEN 1 ELSE 0 END AS content_description"
    + "".join(
        f", CASE WHEN {expr} THEN 1 ELSE 0 END AS badge_{name.lower()}"
        for name, expr in _BADGE_SQL_EXPRESSIONS.items()
    )
    + " FROM videos v LEFT JOIN sources src ON src.id = v.source_asset_id"
)


def _badge_where_clause(filters: "CatalogFilter | None") -> tuple[str, tuple[Any, ...]]:
    """Constrói a cláusula WHERE (sem a palavra ``WHERE``) e seus
    parâmetros para um ``CatalogFilter`` -- só usa fragmentos SQL fixos de
    ``_BADGE_SQL_EXPRESSIONS`` (badge já validado em ``CatalogFilter.
    __post_init__``); nenhuma string vinda do chamador entra na consulta."""
    if filters is None:
        return "1=1", ()
    parts: list[str] = []
    for badge in filters.badges_all:
        parts.append(f"({_BADGE_SQL_EXPRESSIONS[badge]})")
    for badge in filters.badges_none:
        parts.append(f"NOT ({_BADGE_SQL_EXPRESSIONS[badge]})")
    if not parts:
        return "1=1", ()
    return " AND ".join(parts), ()


class MediaCatalogService:
    """Serviço de leitura/agregação do catálogo (nunca escreve em nenhuma
    fonte de verdade operacional -- só escreve na sua própria tabela de
    marcações manuais, ``video_declarations``)."""

    def __init__(self, database: LocalDatabase) -> None:
        if database is None:
            raise ValueError("database é obrigatório")
        self.database = database

    @contextlib.contextmanager
    def _connection(self) -> "Iterator[sqlite3.Connection]":
        """``self.database.connection()`` com ``catalog_fs_file_ok``
        registrada (ver docstring 0.7) -- ponto único de registro para os
        4 métodos de leitura que podem avaliar ``_BADGE_SQL_EXPRESSIONS``
        (``get_item``/``filter_items``/``count_by_filter``/``get_facets``).
        Nunca registrada em ``LocalDatabase`` -- é um detalhe deste
        módulo, não do storage genérico."""
        with self.database.connection() as conn:
            conn.create_function(_CATALOG_FS_FUNCTION_NAME, 1, _fs_file_ok)
            yield conn

    # -- leitura de item único -------------------------------------------------

    def get_item(self, video_id: str) -> MediaCatalogItem | None:
        video_id = _validate_video_id(video_id)
        sql = _BASE_SELECT + " WHERE v.id = ?"
        with self._connection() as conn:
            row = conn.execute(sql, (video_id,)).fetchone()
            if row is None:
                return None
            return self._row_to_item(conn, row)

    # -- listagem / filtro / contagem / facetas --------------------------------

    def list_items(self, *, limit: int = 50, offset: int = 0) -> tuple[MediaCatalogItem, ...]:
        return self.filter_items(None, limit=limit, offset=offset)

    def filter_items(
        self,
        filters: "CatalogFilter | None" = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MediaCatalogItem, ...]:
        """Duas consultas deliberadas (nunca uma "otimização" que carrega
        tudo em memória): (1) uma consulta BARATA que só resolve QUAIS
        ``video_id`` entram nesta página, ordenando/filtrando/paginando
        inteiramente em SQL sobre a tabela ``videos`` (sem nenhuma das
        colunas ricas de ``_BASE_SELECT``); (2) uma segunda consulta que
        computa os campos ricos (badges/joins/agregações) SOMENTE para os
        ``limit`` ids já resolvidos -- nunca para linhas puladas pelo
        ``OFFSET``. Isso evita que uma página no FIM de um catálogo de
        milhares de itens pague o custo de reavaliar 17 subqueries
        correlacionadas para cada linha descartada (ver seção "DÍVIDA
        TÉCNICA" do relatório de entrega para o número medido antes desta
        otimização)."""
        _validate_pagination(limit, offset)
        where_sql, where_params = _badge_where_clause(filters)
        id_sql = f"SELECT v.id AS video_id FROM videos v WHERE {where_sql} ORDER BY v.created_at, v.id LIMIT ? OFFSET ?"
        with self._connection() as conn:
            id_rows = conn.execute(id_sql, (*where_params, limit, offset)).fetchall()
            ids = [row["video_id"] for row in id_rows]
            if not ids:
                return ()
            placeholders = ", ".join("?" for _ in ids)
            rich_sql = f"{_BASE_SELECT} WHERE v.id IN ({placeholders}) ORDER BY v.created_at, v.id"
            rows = conn.execute(rich_sql, tuple(ids)).fetchall()
            return tuple(self._row_to_item(conn, row) for row in rows)

    def count_by_filter(self, filters: "CatalogFilter | None" = None) -> int:
        where_sql, where_params = _badge_where_clause(filters)
        sql = f"SELECT COUNT(*) AS n FROM videos v WHERE {where_sql}"
        with self._connection() as conn:
            row = conn.execute(sql, where_params).fetchone()
            return int(row["n"])

    def get_facets(self, filters: "CatalogFilter | None" = None) -> dict[str, int]:
        """Uma única consulta ``SUM(CASE WHEN ...)`` por badge -- nunca
        carrega itens completos em memória para contar (item 1.2)."""
        where_sql, where_params = _badge_where_clause(filters)
        select_sums = ", ".join(
            f"SUM(CASE WHEN {expr} THEN 1 ELSE 0 END) AS facet_{name}"
            for name, expr in _BADGE_SQL_EXPRESSIONS.items()
        )
        sql = f"SELECT {select_sums} FROM videos v WHERE {where_sql}"
        with self._connection() as conn:
            row = conn.execute(sql, where_params).fetchone()
            return {
                name: int(row[f"facet_{name}"] or 0)
                for name in _BADGE_SQL_EXPRESSIONS
            }

    # -- marcações manuais individuais -----------------------------------------

    def set_user_flag(self, video_id: str, value: str) -> None:
        self._write_declaration(video_id, DECLARATION_KIND_FLAG, value, action="ADD")

    def remove_user_flag(self, video_id: str, value: str) -> None:
        self._write_declaration(video_id, DECLARATION_KIND_FLAG, value, action="REMOVE")

    def add_user_label(self, video_id: str, value: str) -> None:
        self._write_declaration(video_id, DECLARATION_KIND_LABEL, value, action="ADD")

    def remove_user_label(self, video_id: str, value: str) -> None:
        self._write_declaration(video_id, DECLARATION_KIND_LABEL, value, action="REMOVE")

    def set_user_assertion(self, video_id: str, value: str) -> None:
        self._write_declaration(video_id, DECLARATION_KIND_ASSERTION, value, action="ADD")

    def remove_user_assertion(self, video_id: str, value: str) -> None:
        self._write_declaration(video_id, DECLARATION_KIND_ASSERTION, value, action="REMOVE")

    def _write_declaration(self, video_id: str, kind: str, value: str, *, action: str) -> None:
        video_id = _validate_video_id(video_id)
        value = _validate_declaration_value(value)
        with self.database.transaction() as conn:
            self._require_video_exists(conn, video_id)
            conn.execute(
                "INSERT INTO video_declarations (id, video_id, kind, value, origin, action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_uuid(), video_id, kind, value, ORIGIN_USER, action, utc_now_iso()),
            )

    # -- bulk edit ---------------------------------------------------------------

    def bulk_edit(
        self,
        video_ids: Sequence[str],
        *,
        assertions: "BulkFieldOp | None" = None,
        flags: "BulkFieldOp | None" = None,
        labels: "BulkFieldOp | None" = None,
    ) -> BulkEditResult:
        """Aplica ``assertions``/``flags``/``labels`` (cada um KEEP por
        padrão) a todos os ``video_ids``, numa única transação atômica
        (GATE 6: duas chamadas concorrentes de bulk edit contra o mesmo
        vídeo nunca perdem uma marcação -- ``BEGIN IMMEDIATE`` serializa).
        Nunca altera ``SYSTEM_BADGES`` (nem é possível: só os três campos
        acima existem como parâmetro). Um campo em ``KEEP`` nunca gera
        NENHUMA escrita -- outras marcações não mencionadas permanecem
        intocadas."""
        requested = tuple(_dedupe_preserve_order(video_ids))
        for video_id in requested:
            _validate_video_id(video_id)
        ops: tuple[tuple[str, "BulkFieldOp | None"], ...] = (
            (DECLARATION_KIND_ASSERTION, assertions),
            (DECLARATION_KIND_FLAG, flags),
            (DECLARATION_KIND_LABEL, labels),
        )
        updated: list[str] = []
        unchanged: list[str] = []
        failed: list[tuple[str, str]] = []
        with self.database.transaction() as conn:
            for video_id in requested:
                row = conn.execute("SELECT id FROM videos WHERE id = ?", (video_id,)).fetchone()
                if row is None:
                    failed.append((video_id, VideoNaoEncontradoError.code))
                    continue
                changed = False
                for kind, op in ops:
                    if op is None or op.mode == BULK_KEEP:
                        continue
                    active = _active_values_for(conn, video_id, kind)
                    timestamp = utc_now_iso()
                    if op.mode == BULK_SET:
                        for value in op.values:
                            if value not in active:
                                conn.execute(
                                    "INSERT INTO video_declarations "
                                    "(id, video_id, kind, value, origin, action, created_at) "
                                    "VALUES (?, ?, ?, ?, ?, 'ADD', ?)",
                                    (new_uuid(), video_id, kind, value, ORIGIN_USER, timestamp),
                                )
                                changed = True
                    elif op.mode == BULK_UNSET:
                        for value in op.values:
                            if value in active:
                                conn.execute(
                                    "INSERT INTO video_declarations "
                                    "(id, video_id, kind, value, origin, action, created_at) "
                                    "VALUES (?, ?, ?, ?, ?, 'REMOVE', ?)",
                                    (new_uuid(), video_id, kind, value, ORIGIN_USER, timestamp),
                                )
                                changed = True
                if changed:
                    updated.append(video_id)
                else:
                    unchanged.append(video_id)
        return BulkEditResult(
            requested=requested,
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            failed=tuple(failed),
        )

    # -- internos -----------------------------------------------------------

    @staticmethod
    def _require_video_exists(conn: sqlite3.Connection, video_id: str) -> None:
        row = conn.execute("SELECT id FROM videos WHERE id = ?", (video_id,)).fetchone()
        if row is None:
            raise VideoNaoEncontradoError(f"nenhum Video encontrado para id={video_id!r}")

    def _row_to_item(self, conn: sqlite3.Connection, row: sqlite3.Row) -> MediaCatalogItem:
        video_id = row["video_id"]
        filename, display_name = _filename_and_display_name(
            row["video_name"], row["source_original_name"], row["source_local_path"], video_id
        )
        badges = tuple(
            name for name in _BADGE_SQL_EXPRESSIONS if row[f"badge_{name.lower()}"] == 1
        )
        assertions = _active_values_for(conn, video_id, DECLARATION_KIND_ASSERTION)
        flags = _active_values_for(conn, video_id, DECLARATION_KIND_FLAG)
        labels = _active_values_for(conn, video_id, DECLARATION_KIND_LABEL)
        return MediaCatalogItem(
            video_id=video_id,
            source_asset_id=row["source_asset_id"],
            filename=filename,
            display_name=display_name,
            created_at=row["created_at"],
            duration=None,
            resolution=None,
            thumbnail_ref=None,
            processing_status=row["processing_status"],
            system_badges=badges,
            user_assertions=tuple(sorted(assertions)),
            user_flags=tuple(sorted(flags)),
            user_labels=tuple(sorted(labels)),
            latest_project_id=row["latest_project_id"],
            latest_artifact_id=row["latest_artifact_id"],
            latest_operation=row["latest_operation"],
            error_summary=row["error_summary"],
            content_status=ContentStatus(
                title_available=bool(row["content_title"]),
                description_available=bool(row["content_description"]),
                hashtags_available=False,
                transcript_available=False,
                captions_available=False,
            ),
            publication_summary=PublicationSummary(
                destinations_count=int(row["destinations_count"] or 0),
                scheduled_count=int(row["scheduled_count"] or 0),
                published_count=int(row["published_count"] or 0),
                failed_count=int(row["failed_count"] or 0),
            ),
        )


def _validate_pagination(limit: int, offset: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise PaginacaoInvalidaError(f"limit deve ser inteiro >= 0, recebido {limit!r}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise PaginacaoInvalidaError(f"offset deve ser inteiro >= 0, recebido {offset!r}")


def _dedupe_preserve_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _active_values_for(conn: sqlite3.Connection, video_id: str, kind: str) -> tuple[str, ...]:
    """Estado atual DERIVADO de ``video_declarations`` para (``video_id``,
    ``kind``) -- mesma técnica ``ROW_NUMBER() OVER (... ORDER BY rowid
    DESC)`` já usada em ``source_import.py``/``list_active`` (Prompt
    24b)."""
    sql = (
        "SELECT value FROM ("
        "  SELECT value, action,"
        "         ROW_NUMBER() OVER ("
        "             PARTITION BY video_id, kind, value"
        "             ORDER BY rowid DESC"
        "         ) AS rn"
        "  FROM video_declarations"
        "  WHERE video_id = ? AND kind = ?"
        ") WHERE rn = 1 AND action = 'ADD'"
    )
    rows = conn.execute(sql, (video_id, kind)).fetchall()
    return tuple(row["value"] for row in rows)


__all__ = [
    "SYSTEM_BADGES",
    "DERIVABLE_BADGES_TODAY",
    "IMPOSSIBLE_BADGES_TODAY",
    "DECLARATION_KIND_ASSERTION",
    "DECLARATION_KIND_FLAG",
    "DECLARATION_KIND_LABEL",
    "DECLARATION_KINDS",
    "ORIGIN_USER",
    "BULK_KEEP",
    "BULK_SET",
    "BULK_UNSET",
    "BULK_MODES",
    "MediaCatalogError",
    "VideoIdInvalidoError",
    "VideoNaoEncontradoError",
    "BadgeDesconhecidoError",
    "DeclaracaoInvalidaError",
    "ModoBulkInvalidoError",
    "PaginacaoInvalidaError",
    "ContentStatus",
    "PublicationSummary",
    "MediaCatalogItem",
    "CatalogFilter",
    "BulkFieldOp",
    "BulkEditResult",
    "MediaCatalogService",
]
