# -*- coding: utf-8 -*-
"""PROMPT 27B -- Promoção de SourceAsset para Video (lacuna corretiva).

Este Prompt NÃO está no roadmap original -- é corretivo, criado porque a
auditoria do Prompt 27 encontrou uma lacuna real antes de o Prompt 27.5
(Media Catalog Backend) ser iniciado: o roadmap original assume que
``Video`` existe para todo item de catálogo, mas nenhum código do
pipeline novo (Prompts 24/24b/25/26/27) cria uma entidade ``Video`` -- a
única criação de ``Video`` em todo o projeto era em
``_sistema/storage/legacy_migration.py``, migrando dados do sistema
antigo. Sem este módulo, o catálogo do 27.5 ficaria estruturalmente vazio
para qualquer coisa importada a partir de agora.

===========================================================================
0.1 -- CONFIRMAÇÃO (grep, antes de escrever qualquer linha deste módulo)
===========================================================================

``Video(`` só aparecia em ``domain/models.py`` (definição da dataclass) e
em ``storage/legacy_migration.py`` (migração de dados antigos, função
``_migrate_source_and_video``) -- nenhum outro lugar do código cria uma
instância de ``Video``. Confirmado por busca textual no código-fonte de
todo ``_sistema/`` antes de qualquer implementação, e reconfirmado por
teste AST (``test_video_promotion.py``) que nenhum OUTRO módulo além
deste e de ``legacy_migration.py`` referencia ``Video(``.

===========================================================================
0.2 -- SCHEMA JÁ EXISTENTE, NADA NOVO
===========================================================================

``Video`` (``domain/models.py``): ``source_asset_id: str | None``,
``name: str``, ``status: str = "ACTIVE"``. Tabela ``videos`` já existe
desde ``m001_initial.py`` (congelada, sem índice único em
``source_asset_id``) -- NENHUMA migration nova foi criada para este
Prompt, e ``domain/models.py``/a entidade ``Video`` NÃO foram alterados.
``status="ACTIVE"`` é o valor padrão já existente na dataclass e é o
correto para este momento do ciclo de vida (um vídeo recém-promovido está
imediatamente disponível para edição -- não há, hoje, nenhum status
intermediário definido em ``Video`` que fizesse mais sentido).

===========================================================================
0.3 -- PROMOÇÃO CONTINUA EXPLÍCITA, NUNCA AUTOMÁTICA
===========================================================================

``SourceImportManager``/``LocalFileImporter``/``FolderImporter``/
``UrlImporter`` (Prompt 24) deliberadamente NÃO criam ``Video`` -- decisão
já documentada e aprovada naquele Prompt (um ``SourceAsset`` importado não
devia virar automaticamente algo "pronto para editar"; um
``FolderImporter`` importando centenas de arquivos brutos não deveria
promover todos a ``Video`` imediatamente). Este módulo NÃO reabre nem
reverte essa decisão -- ``VideoPromotionService.promote`` é um passo
SEPARADO e EXPLÍCITO, nunca chamado automaticamente pelos importadores
(mesmo espírito de ``MediaProbe``/Prompt 26 -- "sob demanda" -- em
contraste com ``SourceContextResolver``/Prompt 25, que É automático).
Confirmado por teste AST equivalente ao já usado nos Prompts 26/27: nenhum
dos três importadores referencia ``VideoPromotionService``/
``video_promotion``/``promote(`` em seu código-fonte.

===========================================================================
0.4 / 1.3 -- DECISÃO DE IDEMPOTÊNCIA (a lacuna central deste Prompt)
===========================================================================

O modelo permite, em princípio, mais de um ``Video`` apontando para o
mesmo ``source_asset_id`` (o campo não é único/indexado como 1:1
obrigatório no schema -- confirmado lendo ``m001_initial.py``: nenhum
``UNIQUE``/índice único sobre ``videos.source_asset_id``). Não há, no
roadmap original nem no schema, nenhuma indicação de que a relação DEVA
ser estritamente 1:1 -- múltiplos vídeos a partir da mesma origem (ex.:
duas edições independentes recortadas do mesmo arquivo bruto) é um caso de
uso plausível em princípio.

DECISÃO deste módulo: ``promote()`` é IDEMPOTENTE -- no máximo 1 ``Video``
é criado por ``SourceAsset`` através deste serviço. Chamadas repetidas
(inclusive concorrentes) devolvem sempre o MESMO ``Video`` já existente,
nunca criam um segundo.

JUSTIFICATIVA:
- O Prompt 27.5 (Media Catalog Backend, ainda não iniciado) vai depender
  de uma relação PREVISÍVEL entre origem e item de catálogo -- um
  `MediaCatalogItem` por `video_id` implica que "promover uma origem" deve
  ter um resultado estável e repetível, não um efeito colateral que
  multiplica linhas a cada nova chamada acidental (ex.: um usuário
  clicando duas vezes em "promover" numa UI futura, ou uma reconciliação
  que rechama `promote()` por segurança).
- Nada no roadmap deste Prompt nem do 27.5 pede explicitamente múltiplos
  vídeos por origem -- é um caso de uso hipotético, não um requisito
  confirmado. Elevar a MULTIPLICAÇÃO a comportamento padrão sem essa
  necessidade demonstrada seria overengineering (item P do CLAUDE.md).
- Se um caso de uso real de "múltiplas edições da mesma origem" aparecer
  no futuro, ele pode ser modelado de forma EXPLÍCITA e deliberada (ex.:
  um parâmetro `force_new=True` num Prompt futuro, ou duplicar o
  `SourceAsset` antes de promover) -- não como o comportamento padrão
  silencioso de uma chamada simples chamada `promote()`.
- Sob concorrência real, a idempotência só é uma garantia de verdade se
  resolvida dentro de uma transação atômica (não por sorte de timing) --
  ver seção seguinte.

RESOLUÇÃO SOB CONCORRÊNCIA (mesma técnica de ``EditProjectManager``/
``ControlManager``): ``promote()`` verifica a existência de um ``Video``
para aquele ``source_asset_id`` e, se ausente, cria um novo, tudo dentro
da MESMA transação ``BEGIN IMMEDIATE`` (``LocalDatabase.transaction()``).
``BEGIN IMMEDIATE`` adquire o write lock do SQLite imediatamente ao abrir
a transação -- uma segunda chamada concorrente para o MESMO
``source_asset_id`` bloqueia em sua própria abertura de transação até a
primeira commitar, e só então lê o estado (já com o ``Video`` criado pela
primeira) -- nunca cria um segundo. Confirmado por teste com
``threading.Barrier`` forçando sobreposição real (seção de testes) --
nunca um teste probabilístico.

Quando MÚLTIPLOS vídeos já existem para a mesma origem (ex.: criados por
outro caminho, como ``legacy_migration.py``, que não passa por este
serviço), ``promote()`` devolve o MAIS ANTIGO (ordenado por
``created_at``, com ``id`` como desempate determinístico) -- nunca cria
um adicional. ``list_videos_for_source`` continua listando todos eles,
para que um consumidor futuro (Prompt 27.5) possa perceber e tratar esse
caso, mesmo que ``promote()`` em si nunca produza esse cenário por conta
própria.

===========================================================================
ORTOGONALIDADE E ESCOPO
===========================================================================

Este módulo nunca importa nem chama ``circuit_breaker``/``retry_policy``/
``publication_idempotency``/``secrets_manager``/``domain.job_state_machine``,
nunca importa nada de Geração 1, e nunca cria ``Job``/``Artifact``/
``Publication``/``Schedule``/``Project`` -- só ``Video``. Nunca modifica o
``SourceAsset`` original (só lê -- a linha em ``sources`` permanece
byte-a-byte idêntica antes/depois de qualquer promoção).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from .domain import SourceAsset, Video
from .storage.database import LocalDatabase

__all__ = [
    "VideoPromotionError",
    "SourceAssetNaoEncontradoError",
    "SourceAssetIdInvalidoError",
    "VideoPromotionService",
]


class VideoPromotionError(RuntimeError):
    """Base para todo erro estruturado deste módulo -- nunca uma exceção
    crua de banco (ex.: ``sqlite3.OperationalError``) escapa de um método
    público de ``VideoPromotionService``. ``code`` é a identificação
    estável e programática do erro (mesmo padrão de classes tipadas com
    ``.code`` já usado por ``edit_project.py``/``ControlManagerError``)."""

    code: ClassVar[str] = "VIDEO_PROMOTION_ERRO"


class SourceAssetNaoEncontradoError(VideoPromotionError):
    """``source_asset_id`` é um UUID válido, mas nenhum ``SourceAsset``
    corresponde a ele."""

    code: ClassVar[str] = "SOURCE_ASSET_NAO_ENCONTRADO"


class SourceAssetIdInvalidoError(VideoPromotionError):
    """``source_asset_id`` não é um UUID textual válido."""

    code: ClassVar[str] = "SOURCE_ASSET_ID_INVALIDO"


def _validate_source_asset_id(source_asset_id: object) -> str:
    if not isinstance(source_asset_id, str) or not source_asset_id:
        raise SourceAssetIdInvalidoError(
            f"source_asset_id deve ser uma string UUID não vazia (recebido: {source_asset_id!r})"
        )
    try:
        UUID(source_asset_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise SourceAssetIdInvalidoError(
            f"source_asset_id não é um UUID válido: {source_asset_id!r}"
        ) from exc
    return source_asset_id


def _derive_video_name(source: SourceAsset) -> str:
    """Deriva um nome sensato para o ``Video`` a partir do ``SourceAsset``.

    Prioridade: ``original_name`` (quando disponível) -> nome-base de
    ``local_path`` -> ``source_uri`` inteiro -> fallback genérico com os
    primeiros 8 caracteres do id (nunca uma string vazia -- ``Video.name``
    é ``str`` não-opcional). Nenhum campo novo foi inventado em
    ``SourceAsset``/``Video`` para resolver isso -- a derivação usa
    exclusivamente os campos já existentes."""
    if source.original_name:
        return source.original_name
    if source.local_path:
        # Nome-base simples, sem depender de pathlib para não presumir
        # separador de SO -- aceita tanto '/' quanto '\\'.
        candidate = source.local_path.replace("\\", "/").rsplit("/", 1)[-1]
        if candidate:
            return candidate
    if source.source_uri:
        return source.source_uri
    return f"video-sem-nome-{source.id[:8]}"


class VideoPromotionService:
    """Promove um ``SourceAsset`` a ``Video`` -- passo explícito e sob
    demanda (seção 0.3), nunca automático. Idempotente por
    ``source_asset_id`` (seção 1.3): no máximo um ``Video`` é criado por
    origem através deste serviço, mesmo sob concorrência real."""

    def __init__(self, database: LocalDatabase) -> None:
        if not isinstance(database, LocalDatabase):
            raise TypeError("database deve ser uma instância de LocalDatabase")
        self.database = database

    # -- escrita -----------------------------------------------------

    def promote(self, source_asset_id: str) -> Video:
        """Promove ``source_asset_id`` a ``Video``. Se um ``Video`` já
        existir para esta origem (criado por uma chamada anterior a
        ``promote()``, inclusive concorrente, ou por qualquer outro
        caminho como ``legacy_migration.py``), devolve o MAIS ANTIGO já
        existente -- nunca cria um segundo (ver seção 1.3 da docstring do
        módulo). Nunca modifica o ``SourceAsset`` original."""
        source_asset_id = _validate_source_asset_id(source_asset_id)

        with self.database.transaction() as conn:
            source = self.database.get(SourceAsset, source_asset_id, connection=conn)
            if source is None:
                raise SourceAssetNaoEncontradoError(
                    f"SourceAsset não encontrado: {source_asset_id}"
                )

            existing = self._existing_videos_for_source(source_asset_id, connection=conn)
            if existing:
                return existing[0]

            video = Video(
                source_asset_id=source.id,
                name=_derive_video_name(source),
                status="ACTIVE",
            )
            self.database.insert(video, connection=conn)

        return video

    # -- leitura -------------------------------------------------------

    def list_videos_for_source(self, source_asset_id: str) -> tuple[Video, ...]:
        """Lista, em ordem determinística (``created_at`` e depois ``id``
        como desempate), todos os ``Video``s já promovidos a partir de um
        dado ``SourceAsset`` -- inclusive vídeos criados por outro caminho
        (ex.: ``legacy_migration.py``). ``source_asset_id`` inexistente
        (mas UUID válido) devolve uma tupla vazia, nunca lança -- listar
        "nada encontrado" é um resultado normal, diferente de tentar
        promover uma origem que não existe."""
        source_asset_id = _validate_source_asset_id(source_asset_id)
        return self._existing_videos_for_source(source_asset_id, connection=None)

    # -- interno ---------------------------------------------------------

    def _existing_videos_for_source(
        self, source_asset_id: str, *, connection
    ) -> tuple[Video, ...]:
        """Consulta os ``Video``s existentes para uma origem, decodificados
        via a API pública ``LocalDatabase.get`` (nunca reimplementando a
        decodificação de entidade deste módulo -- reaproveita o contrato
        já usado em todo o projeto)."""
        sql = "SELECT id FROM videos WHERE source_asset_id = ? ORDER BY created_at, id"
        if connection is not None:
            rows = connection.execute(sql, (source_asset_id,)).fetchall()
            videos = [self.database.get(Video, row["id"], connection=connection) for row in rows]
        else:
            with self.database.connection() as conn:
                rows = conn.execute(sql, (source_asset_id,)).fetchall()
                videos = [self.database.get(Video, row["id"], connection=conn) for row in rows]
        return tuple(video for video in videos if video is not None)
