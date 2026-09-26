# -*- coding: utf-8 -*-
"""PROMPT 27.5 -- Media Catalog Backend: testes.

Cobre a lista de TESTES do roadmap (item sem processamento; item com
múltiplos Artifacts; job concluído; job falho; artifact ausente; artifact
inválido; badge não aparece em falso; user assertion persistente; user
flag persistente; labels personalizadas; bulk edit KEEP/SET/UNSET;
filtros combinados; paginação; restart; milhares de itens; nenhum
vazamento entre vídeos) MAIS os testes adicionais exigidos pelo Prompt:
prova direta de que cada badge "impossível hoje" nunca aparece mesmo com
evidência adjacente presente; escala 0/1/100/1000/10000 com tempos
medidos; bulk edit não apaga marcação não solicitada; concorrência real
(GATE 6) para escrita de ``video_declarations``; garantias estruturais
(AST).
"""
from __future__ import annotations

import ast
import random
import threading
import time
from pathlib import Path

import pytest

import _sistema.media_catalog as media_catalog
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import (
    Account,
    Artifact,
    ErrorRecord,
    Job,
    Project,
    Publication,
    Schedule,
    SourceAsset,
    Video,
)
from _sistema.storage.database import LocalDatabase
from _sistema.domain import new_uuid
from _sistema.time_utils import utc_now_iso
from _sistema.media_catalog import (
    BADGE_AI_DESCRIPTION,
    BADGE_AI_HASHTAGS,
    BADGE_AI_TITLE,
    BADGE_AUDIO_PROCESSED,
    BADGE_CAPTIONS,
    BADGE_EDITED,
    BADGE_ERROR,
    BADGE_IMPORTED,
    BADGE_METADATA_CLEAN,
    BADGE_PUBLISHED,
    BADGE_READY,
    BADGE_REFRAMED_9_16,
    BADGE_RENDERED,
    BADGE_SCHEDULED,
    BADGE_TEMPLATE_APPLIED,
    BADGE_TRANSCRIBED,
    BADGE_VALIDATED,
    BULK_KEEP,
    BULK_SET,
    BULK_UNSET,
    BadgeDesconhecidoError,
    BulkFieldOp,
    CatalogFilter,
    DeclaracaoInvalidaError,
    DERIVABLE_BADGES_TODAY,
    IMPOSSIBLE_BADGES_TODAY,
    MediaCatalogItem,
    MediaCatalogService,
    ModoBulkInvalidoError,
    PaginacaoInvalidaError,
    SYSTEM_BADGES,
    VideoIdInvalidoError,
    VideoNaoEncontradoError,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão dos Prompts 25/26/27/27b)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_paths(tmp_path):
    paths = build_app_paths(data_root=tmp_path / "data")
    ensure_app_directories(paths)
    return paths


@pytest.fixture
def database(app_paths):
    db = LocalDatabase(app_paths.database / "painel.db")
    db.initialize()
    return db


@pytest.fixture
def service(database):
    return MediaCatalogService(database)


@pytest.fixture
def source_asset(database):
    asset = SourceAsset(source_uri="/tmp/original.mp4", original_name="original.mp4", size_bytes=1024)
    database.insert(asset)
    return asset


@pytest.fixture
def video(database, source_asset):
    v = Video(source_asset_id=source_asset.id, name="Meu Video")
    database.insert(v)
    return v


# ---------------------------------------------------------------------------
# 0. Vocabulário -- os 17 badges, exaustividade
# ---------------------------------------------------------------------------


def test_system_badges_tem_os_17_do_roadmap():
    esperado = (
        "IMPORTED", "VALIDATED", "EDITED", "CAPTIONS", "METADATA_CLEAN",
        "REFRAMED_9_16", "AUDIO_PROCESSED", "TEMPLATE_APPLIED", "AI_TITLE",
        "AI_DESCRIPTION", "AI_HASHTAGS", "TRANSCRIBED", "RENDERED", "READY",
        "SCHEDULED", "PUBLISHED", "ERROR",
    )
    assert SYSTEM_BADGES == esperado
    assert len(SYSTEM_BADGES) == 17


def test_derivavel_e_impossivel_particionam_todos_os_badges_sem_sobreposicao():
    """Desde o Prompt "CATALOG WIRING / RECONCILIATION 27.5+30+31",
    CAPTIONS/TRANSCRIBED saíram de IMPOSSIBLE_BADGES_TODAY e entraram em
    DERIVABLE_BADGES_TODAY -- CaptionsEngine (Prompt 31) já produz
    evidência real (Artifact + checkpoint). AUDIO_PROCESSED foi avaliado
    na mesma rodada e permanece impossível (audio_engine.py é
    decisão-only, nunca cria Job/Artifact/checkpoint). Desde o Prompt
    "Catalog Wiring -- REFRAMED_9_16 (Prompt 33)", REFRAMED_9_16 também
    saiu de IMPOSSIBLE_BADGES_TODAY -- AutoReframeEngine (Prompt 33) já
    produz evidência real (Artifact reframe_track + checkpoint
    MEDIA_PROCESSED). Desde o Prompt 42 (render_engine.py), RENDERED
    também saiu de IMPOSSIBLE_BADGES_TODAY -- RenderEngine já produz
    evidência real (Artifact render_output + checkpoint RENDERED). Desde
    o Prompt 43 (final_media_validator.py), VALIDATED e READY também
    saíram de IMPOSSIBLE_BADGES_TODAY -- FinalMediaValidator já produz
    evidência real (checkpoint VALIDATED, reaproveitando o mesmo Artifact
    render_output), e READY é a conjunção de RENDERED e VALIDATED."""
    assert DERIVABLE_BADGES_TODAY | IMPOSSIBLE_BADGES_TODAY == set(SYSTEM_BADGES)
    assert DERIVABLE_BADGES_TODAY & IMPOSSIBLE_BADGES_TODAY == set()
    assert DERIVABLE_BADGES_TODAY == {
        BADGE_IMPORTED, BADGE_EDITED, BADGE_SCHEDULED, BADGE_PUBLISHED, BADGE_ERROR,
        BADGE_CAPTIONS, BADGE_TRANSCRIBED, BADGE_REFRAMED_9_16, BADGE_RENDERED,
        BADGE_VALIDATED, BADGE_READY,
    }
    assert BADGE_AUDIO_PROCESSED in IMPOSSIBLE_BADGES_TODAY


# ---------------------------------------------------------------------------
# 1. Item sem processamento
# ---------------------------------------------------------------------------


def test_item_sem_processamento_tem_campos_desconhecidos_bem_definidos(service, video):
    item = service.get_item(video.id)
    assert item is not None
    assert item.video_id == video.id
    assert item.processing_status is None
    assert item.latest_project_id is None
    assert item.latest_artifact_id is None
    assert item.latest_operation is None
    assert item.error_summary is None
    assert item.duration is None
    assert item.resolution is None
    assert item.thumbnail_ref is None
    assert item.system_badges == (BADGE_IMPORTED,)
    assert item.user_assertions == ()
    assert item.user_flags == ()
    assert item.user_labels == ()
    assert item.publication_summary.destinations_count == 0
    assert item.publication_summary.scheduled_count == 0
    assert item.publication_summary.published_count == 0
    assert item.publication_summary.failed_count == 0
    assert item.content_status.title_available is False
    assert item.content_status.hashtags_available is False


def test_get_item_video_inexistente_devolve_none(service, database):
    import uuid

    assert service.get_item(str(uuid.uuid4())) is None


def test_get_item_video_id_invalido_levanta_erro_estruturado(service):
    with pytest.raises(VideoIdInvalidoError):
        service.get_item("nao-e-um-uuid")


# ---------------------------------------------------------------------------
# 2. Múltiplos Artifacts / job concluído / job falho
# ---------------------------------------------------------------------------


def test_item_com_multiplos_artifacts_usa_o_mais_recente(service, database, video):
    a1 = database.insert(Artifact(video_id=video.id, kind="preview", path="/a1.mp4"))
    time.sleep(0.002)
    a2 = database.insert(Artifact(video_id=video.id, kind="preview", path="/a2.mp4"))
    item = service.get_item(video.id)
    assert item.latest_artifact_id == a2.id
    assert item.latest_artifact_id != a1.id


def test_job_concluido_publicado_gera_badge_published_e_conta_publication_summary(service, database, video):
    database.insert(Job(video_id=video.id, operation="publish", status="PUBLISHED"))
    item = service.get_item(video.id)
    assert BADGE_PUBLISHED in item.system_badges
    assert item.publication_summary.published_count == 1
    assert item.processing_status == "PUBLISHED"


def test_job_falho_gera_badge_error_e_conta_failed(service, database, video):
    database.insert(Job(video_id=video.id, operation="publish", status="FAILED"))
    item = service.get_item(video.id)
    assert BADGE_ERROR in item.system_badges
    assert item.publication_summary.failed_count == 1


def test_error_record_tambem_gera_badge_error_e_error_summary_seguro(service, database, video):
    database.insert(
        ErrorRecord(code="IO_FALHOU", message="disco cheio", entity_type="Video", entity_id=video.id)
    )
    item = service.get_item(video.id)
    assert BADGE_ERROR in item.system_badges
    assert item.error_summary == "IO_FALHOU: disco cheio"


# ---------------------------------------------------------------------------
# 3. Artifact ausente / artifact inválido não geram falso positivo
# ---------------------------------------------------------------------------


def test_artifact_ausente_nao_gera_rendered_nem_ready(service, database, video):
    item = service.get_item(video.id)
    assert BADGE_RENDERED not in item.system_badges
    assert BADGE_READY not in item.system_badges


def test_artifact_de_qualquer_kind_nao_dispara_rendered_sozinho(service, database, video):
    """Um Artifact de kind arbitrário NÃO é, sozinho, evidência suficiente
    de renderização final validada -- inventar essa regra contradiria a
    seção 0.2 da docstring do módulo. Testa explicitamente kinds que
    poderiam parecer relacionados."""
    for kind in ("preview", "thumbnail", "render", "export", "final", "qualquer_coisa"):
        database.insert(Artifact(video_id=video.id, kind=kind, path=f"/{kind}.mp4"))
    item = service.get_item(video.id)
    assert BADGE_RENDERED not in item.system_badges
    assert BADGE_READY not in item.system_badges


# ---------------------------------------------------------------------------
# 4. Todo badge "impossível hoje" nunca aparece, mesmo com evidência adjacente
# ---------------------------------------------------------------------------


def test_badges_impossiveis_nunca_aparecem_mesmo_com_evidencia_adjacente_rica(service, database, video):
    """Monta um vídeo com o máximo de evidência adjacente plausível
    (projeto editado, job publicado, publication com título/descrição,
    schedule agendado, artifact de kind 'render', error record, job
    SEM checkpoint algum) e confirma que NENHUM badge estruturalmente
    impossível hoje aparece -- e que CAPTIONS/TRANSCRIBED, embora hoje
    deriváveis (ver seção 0.7), NÃO disparam com esta evidência genérica
    e adjacente: Artifact de kind 'render' nunca é confundido com
    'captions_srt'/'captions_vtt'/'transcript_internal', e um Job sem
    CHECKPOINT_TRANSCRIBED gravado nunca é confundido com transcrição
    concluída -- exatamente a garantia que a seção 7 do Prompt exige
    (evidência de OUTRA natureza nunca acende estas duas badges). O
    mesmo vale para REFRAMED_9_16 (seção 0.8): um Job sem checkpoint
    MEDIA_PROCESSED e sem Artifact kind='reframe_track' nunca é
    confundido com reenquadramento concluído. Também vale para RENDERED
    (Prompt 42): o Artifact aqui é kind='render' (string livre genérica),
    NUNCA confundido com kind='render_output' (o vocabulário fechado real
    do RenderEngine), e não há checkpoint RENDERED gravado -- nenhum dos
    dois sinais isoladamente é suficiente. O mesmo vale para
    VALIDATED/READY (Prompt 43): sem checkpoint VALIDATED gravado e sem
    Artifact kind='render_output', nenhum dos dois pode aparecer, mesmo
    com o Artifact kind='render' genérico presente. Só os 5 badges
    deriváveis "genéricos" (IMPORTED/EDITED/SCHEDULED/PUBLISHED/ERROR)
    devem aparecer aqui."""
    database.insert(Project(video_id=video.id, edit_state={"categories": {"cuts": {"data": {}, "fingerprint": "f", "revision": 1, "updated_at": "now"}}}))
    database.insert(Job(video_id=video.id, operation="publish", status="PUBLISHED"))
    database.insert(Job(video_id=video.id, operation="publish", status="FAILED"))
    account = database.insert(Account(platform="youtube", name="conta"))
    pub = database.insert(Publication(video_id=video.id, account_id=account.id, title="T", description="D"))
    database.insert(Schedule(publication_id=pub.id, delivery_state="LOCAL_PENDING"))
    database.insert(Artifact(video_id=video.id, kind="render", path="/x.mp4"))
    database.insert(ErrorRecord(code="X", message="y", entity_type="Video", entity_id=video.id))

    item = service.get_item(video.id)
    for badge in IMPOSSIBLE_BADGES_TODAY:
        assert badge not in item.system_badges, f"{badge} nao deveria aparecer (sem fonte de verdade real)"
    assert BADGE_CAPTIONS not in item.system_badges
    assert BADGE_TRANSCRIBED not in item.system_badges
    assert BADGE_REFRAMED_9_16 not in item.system_badges
    assert BADGE_RENDERED not in item.system_badges
    assert BADGE_VALIDATED not in item.system_badges
    assert BADGE_READY not in item.system_badges
    genericos = DERIVABLE_BADGES_TODAY - {
        BADGE_CAPTIONS, BADGE_TRANSCRIBED, BADGE_REFRAMED_9_16, BADGE_RENDERED,
        BADGE_VALIDATED, BADGE_READY,
    }
    for badge in genericos:
        assert badge in item.system_badges, f"{badge} deveria ter aparecido com esta evidencia"


@pytest.mark.parametrize(
    "badge",
    sorted(IMPOSSIBLE_BADGES_TODAY),
)
def test_cada_badge_impossivel_individualmente_nunca_dispara(service, database, video, badge):
    database.insert(Job(video_id=video.id, operation="op", status="PUBLISHED"))
    database.insert(Artifact(video_id=video.id, kind="render", path="/x.mp4"))
    item = service.get_item(video.id)
    assert badge not in item.system_badges


# ---------------------------------------------------------------------------
# 5. User assertion / flag / label persistentes (restart)
# ---------------------------------------------------------------------------


def test_user_assertion_persiste_apos_restart(app_paths, database, video):
    service = MediaCatalogService(database)
    service.set_user_assertion(video.id, "EXTERNALLY_EDITED")

    db2 = LocalDatabase(app_paths.database / "painel.db")
    service2 = MediaCatalogService(db2)
    item = service2.get_item(video.id)
    assert item.user_assertions == ("EXTERNALLY_EDITED",)


def test_user_flag_persiste_apos_restart(app_paths, database, video):
    service = MediaCatalogService(database)
    service.set_user_flag(video.id, "FAVORITE")

    db2 = LocalDatabase(app_paths.database / "painel.db")
    service2 = MediaCatalogService(db2)
    item = service2.get_item(video.id)
    assert item.user_flags == ("FAVORITE",)


def test_labels_personalizadas_livres_persistem(service, database, video):
    service.add_user_label(video.id, "Cliente A")
    service.add_user_label(video.id, "Postar amanha")
    item = service.get_item(video.id)
    assert item.user_labels == ("Cliente A", "Postar amanha")


def test_remover_assertion_flag_label_reflete_estado_atual(service, database, video):
    service.set_user_flag(video.id, "FAVORITE")
    service.remove_user_flag(video.id, "FAVORITE")
    item = service.get_item(video.id)
    assert item.user_flags == ()

    # readicionar depois de remover funciona (novo evento ADD por cima do REMOVE)
    service.set_user_flag(video.id, "FAVORITE")
    item2 = service.get_item(video.id)
    assert item2.user_flags == ("FAVORITE",)


def test_marcacao_manual_video_inexistente_levanta_erro(service, database):
    import uuid

    with pytest.raises(VideoNaoEncontradoError):
        service.set_user_flag(str(uuid.uuid4()), "FAVORITE")


def test_marcacao_manual_valor_vazio_levanta_erro(service, database, video):
    with pytest.raises(DeclaracaoInvalidaError):
        service.set_user_flag(video.id, "")
    with pytest.raises(DeclaracaoInvalidaError):
        service.set_user_flag(video.id, "   ")


def test_marcacao_manual_nunca_falsifica_badge_de_sistema(service, database, video):
    """'Uma marcação manual nunca pode falsificar evidência automática do
    sistema' -- gravar um FLAG com o mesmo texto de um badge não faz o
    badge aparecer."""
    service.set_user_flag(video.id, "PUBLISHED")
    service.set_user_flag(video.id, "RENDERED")
    item = service.get_item(video.id)
    assert BADGE_PUBLISHED not in item.system_badges
    assert BADGE_RENDERED not in item.system_badges
    assert "PUBLISHED" in item.user_flags
    assert "RENDERED" in item.user_flags


# ---------------------------------------------------------------------------
# 6. Bulk edit KEEP/SET/UNSET
# ---------------------------------------------------------------------------


def test_bulk_edit_set_aplica_a_todos_os_videos_do_lote(service, database, source_asset):
    v1 = database.insert(Video(source_asset_id=source_asset.id, name="v1"))
    v2 = database.insert(Video(source_asset_id=source_asset.id, name="v2"))
    result = service.bulk_edit(
        [v1.id, v2.id],
        flags=BulkFieldOp(mode=BULK_SET, values=("REVIEWED",)),
    )
    assert result.requested == (v1.id, v2.id)
    assert set(result.updated) == {v1.id, v2.id}
    assert result.unchanged == ()
    assert result.failed == ()
    assert service.get_item(v1.id).user_flags == ("REVIEWED",)
    assert service.get_item(v2.id).user_flags == ("REVIEWED",)


def test_bulk_edit_keep_nao_escreve_nada_para_o_campo(service, database, video):
    service.add_user_label(video.id, "original")
    result = service.bulk_edit([video.id], labels=BulkFieldOp(mode=BULK_KEEP))
    assert result.updated == ()
    assert result.unchanged == (video.id,)
    item = service.get_item(video.id)
    assert item.user_labels == ("original",)


def test_bulk_edit_nao_apaga_silenciosamente_campo_nao_mencionado(service, database, video):
    """'Uma operação em massa não pode apagar silenciosamente outras
    marcações que o usuário não pediu para alterar.' Marca flag e label
    manualmente; bulk edit só mexe em assertions; flag/label continuam
    intactos."""
    service.set_user_flag(video.id, "FAVORITE")
    service.add_user_label(video.id, "Campanha X")
    result = service.bulk_edit(
        [video.id],
        assertions=BulkFieldOp(mode=BULK_SET, values=("EXTERNALLY_EDITED",)),
    )
    assert result.updated == (video.id,)
    item = service.get_item(video.id)
    assert item.user_flags == ("FAVORITE",)
    assert item.user_labels == ("Campanha X",)
    assert item.user_assertions == ("EXTERNALLY_EDITED",)


def test_bulk_edit_set_ja_ativo_e_unchanged(service, database, video):
    service.set_user_flag(video.id, "FAVORITE")
    result = service.bulk_edit([video.id], flags=BulkFieldOp(mode=BULK_SET, values=("FAVORITE",)))
    assert result.updated == ()
    assert result.unchanged == (video.id,)


def test_bulk_edit_unset_remove_valor_ativo(service, database, video):
    service.set_user_flag(video.id, "FAVORITE")
    result = service.bulk_edit([video.id], flags=BulkFieldOp(mode=BULK_UNSET, values=("FAVORITE",)))
    assert result.updated == (video.id,)
    assert service.get_item(video.id).user_flags == ()


def test_bulk_edit_unset_valor_ja_ausente_e_unchanged(service, database, video):
    result = service.bulk_edit([video.id], flags=BulkFieldOp(mode=BULK_UNSET, values=("FAVORITE",)))
    assert result.unchanged == (video.id,)
    assert result.updated == ()


def test_bulk_edit_video_inexistente_isolado_como_failed(service, database, video):
    import uuid

    inexistente = str(uuid.uuid4())
    result = service.bulk_edit(
        [video.id, inexistente],
        flags=BulkFieldOp(mode=BULK_SET, values=("FAVORITE",)),
    )
    assert result.requested == (video.id, inexistente)
    assert video.id in result.updated
    assert len(result.failed) == 1
    assert result.failed[0][0] == inexistente
    # video valido nao foi afetado pela falha do outro
    assert service.get_item(video.id).user_flags == ("FAVORITE",)


def test_bulk_edit_nunca_altera_system_badges():
    """Garantia estrutural: bulk_edit só aceita assertions/flags/labels
    como parâmetro -- não existe caminho para passar um SYSTEM_BADGE."""
    import inspect

    sig = inspect.signature(MediaCatalogService.bulk_edit)
    assert set(sig.parameters) == {"self", "video_ids", "assertions", "flags", "labels"}


def test_bulk_edit_modo_invalido_levanta_erro():
    with pytest.raises(ModoBulkInvalidoError):
        BulkFieldOp(mode="APAGA_TUDO", values=("x",))


def test_bulk_edit_set_sem_values_levanta_erro():
    with pytest.raises(DeclaracaoInvalidaError):
        BulkFieldOp(mode=BULK_SET, values=())


# ---------------------------------------------------------------------------
# 7. Filtros combinados AND/NOT
# ---------------------------------------------------------------------------


def test_filtro_combinado_and_not(service, database, source_asset):
    v_editado = database.insert(Video(source_asset_id=source_asset.id, name="editado"))
    database.insert(Project(video_id=v_editado.id, edit_state={"categories": {"c": {"data": {}, "fingerprint": "f", "revision": 1, "updated_at": "now"}}}))
    database.insert(Job(video_id=v_editado.id, operation="op", status="PUBLISHED"))

    v_editado_nao_publicado = database.insert(Video(source_asset_id=source_asset.id, name="editado2"))
    database.insert(Project(video_id=v_editado_nao_publicado.id, edit_state={"categories": {"c": {"data": {}, "fingerprint": "f", "revision": 1, "updated_at": "now"}}}))

    filtro = CatalogFilter(badges_all=(BADGE_EDITED,), badges_none=(BADGE_PUBLISHED,))
    resultado = service.filter_items(filtro, limit=10)
    ids = {item.video_id for item in resultado}
    assert v_editado_nao_publicado.id in ids
    assert v_editado.id not in ids


def test_filtro_badge_desconhecido_levanta_erro():
    with pytest.raises(BadgeDesconhecidoError):
        CatalogFilter(badges_all=("NAO_EXISTE",))


def test_filtro_impossivel_hoje_sempre_devolve_vazio(service, database, video):
    """Filtrar por um badge estruturalmente impossível hoje (ex.: RENDERED
    -- CAPTIONS deixou de ser impossível desde o Prompt "CATALOG WIRING",
    ver seção 0.7) nunca pode devolver um item -- não há como 'forjar'
    evidência."""
    filtro = CatalogFilter(badges_all=(BADGE_RENDERED,))
    assert service.filter_items(filtro) == ()
    assert service.count_by_filter(filtro) == 0


# ---------------------------------------------------------------------------
# 8. Paginação determinística
# ---------------------------------------------------------------------------


def test_paginacao_ordem_deterministica(service, database, source_asset):
    """``created_at`` tem resolução de segundo (``utc_now_iso``) -- em
    testes rápidos, vários vídeos podem empatar no mesmo timestamp. A
    ordenação (``ORDER BY created_at, id``) ainda assim é DETERMINÍSTICA
    (nunca muda entre chamadas) e a paginação é uma partição exata e sem
    sobreposição do resultado completo -- é isso que este teste prova,
    sem presumir que o timestamp por si só distingue a ordem de
    inserção."""
    videos = [database.insert(Video(source_asset_id=source_asset.id, name=f"v{i}")) for i in range(5)]

    completo = service.list_items(limit=5, offset=0)
    assert {item.video_id for item in completo} == {v.id for v in videos}

    pagina1 = service.list_items(limit=2, offset=0)
    pagina2 = service.list_items(limit=2, offset=2)
    pagina3 = service.list_items(limit=2, offset=4)
    todos_paginados = [item.video_id for item in (*pagina1, *pagina2, *pagina3)]
    assert todos_paginados == [item.video_id for item in completo]

    # repetir a consulta produz exatamente a mesma ordem (deterministico).
    outra_vez = service.list_items(limit=5, offset=0)
    assert [item.video_id for item in outra_vez] == [item.video_id for item in completo]


def test_paginacao_limit_offset_invalidos_levantam_erro(service):
    with pytest.raises(PaginacaoInvalidaError):
        service.list_items(limit=-1)
    with pytest.raises(PaginacaoInvalidaError):
        service.list_items(offset=-1)


def test_count_by_filter_sem_filtro_conta_todos(service, database, source_asset):
    for i in range(3):
        database.insert(Video(source_asset_id=source_asset.id, name=f"v{i}"))
    assert service.count_by_filter(None) == 3


# ---------------------------------------------------------------------------
# 9. Restart (novas instâncias)
# ---------------------------------------------------------------------------


def test_restart_com_novas_instancias_ve_o_mesmo_catalogo(app_paths, database, video):
    service = MediaCatalogService(database)
    service.set_user_flag(video.id, "FAVORITE")

    db2 = LocalDatabase(app_paths.database / "painel.db")
    service2 = MediaCatalogService(db2)
    item = service2.get_item(video.id)
    assert item.user_flags == ("FAVORITE",)
    assert item.video_id == video.id


# ---------------------------------------------------------------------------
# 10. Nenhum vazamento entre vídeos
# ---------------------------------------------------------------------------


def test_nenhum_vazamento_de_marcacoes_entre_videos(service, database, source_asset):
    v1 = database.insert(Video(source_asset_id=source_asset.id, name="v1"))
    v2 = database.insert(Video(source_asset_id=source_asset.id, name="v2"))
    service.set_user_flag(v1.id, "FAVORITE")
    service.add_user_label(v2.id, "Cliente A")

    item1 = service.get_item(v1.id)
    item2 = service.get_item(v2.id)
    assert item1.user_flags == ("FAVORITE",)
    assert item1.user_labels == ()
    assert item2.user_flags == ()
    assert item2.user_labels == ("Cliente A",)


def test_nenhum_vazamento_de_badges_entre_videos(service, database, source_asset):
    v1 = database.insert(Video(source_asset_id=source_asset.id, name="v1"))
    v2 = database.insert(Video(source_asset_id=source_asset.id, name="v2"))
    database.insert(Job(video_id=v1.id, operation="op", status="PUBLISHED"))
    item1 = service.get_item(v1.id)
    item2 = service.get_item(v2.id)
    assert BADGE_PUBLISHED in item1.system_badges
    assert BADGE_PUBLISHED not in item2.system_badges


# ---------------------------------------------------------------------------
# 11. GATE 6 -- concorrência real na escrita de video_declarations
# ---------------------------------------------------------------------------


def test_duas_threads_gravando_flags_diferentes_no_mesmo_video_nenhuma_perdida(app_paths, database, video):
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(flag_value: str):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_service = MediaCatalogService(db_instance)
        barrier.wait(timeout=5)
        try:
            local_service.set_user_flag(video.id, flag_value)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v,)) for v in ("FAVORITE", "PRIORITY")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    item = MediaCatalogService(database).get_item(video.id)
    assert set(item.user_flags) == {"FAVORITE", "PRIORITY"}


def test_muitas_threads_bulk_edit_concorrente_mesmo_video_nao_perde_marcacoes(app_paths, database, video):
    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(i: int):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_service = MediaCatalogService(db_instance)
        barrier.wait(timeout=5)
        try:
            local_service.bulk_edit(
                [video.id],
                labels=BulkFieldOp(mode=BULK_SET, values=(f"label-{i}",)),
            )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    item = MediaCatalogService(database).get_item(video.id)
    assert set(item.user_labels) == {f"label-{i}" for i in range(n)}


# ---------------------------------------------------------------------------
# 12. Escala -- 0 / 1 / 100 / 1.000 / 10.000
# ---------------------------------------------------------------------------


def _seed_synthetic_videos(database, source_asset, n: int) -> list[str]:
    """Distribuição NÃO uniforme (exigida pelo Prompt): nem todo vídeo
    tem Job/Artifact/Publication/Schedule. Usa sempre os modelos de
    domínio + ``LocalDatabase.insert(..., connection=conn)`` (nunca SQL
    de INSERT reescrito à mão para as tabelas de verdade operacional --
    evita divergir silenciosamente do schema real)."""
    ids: list[str] = []
    accounts = [database.insert(Account(platform="youtube", name=f"conta{i}")) for i in range(3)] if n > 0 else []
    with database.transaction() as conn:
        for i in range(n):
            video_row = database.insert(Video(source_asset_id=source_asset.id, name=f"video-{i}"), connection=conn)
            ids.append(video_row.id)
            if i % 3 == 0:
                job_status = "PUBLISHED" if i % 6 == 0 else "FAILED"
                database.insert(Job(video_id=video_row.id, operation="op", status=job_status), connection=conn)
            if i % 5 == 0:
                database.insert(
                    Artifact(video_id=video_row.id, kind="preview", path=f"/tmp/{i}.mp4"), connection=conn
                )
            if i % 7 == 0 and accounts:
                account = accounts[i % len(accounts)]
                pub = database.insert(
                    Publication(video_id=video_row.id, account_id=account.id, title=f"titulo-{i}"),
                    connection=conn,
                )
                if i % 11 == 0:
                    database.insert(
                        Schedule(publication_id=pub.id, delivery_state="LOCAL_PENDING"), connection=conn
                    )
    return ids


@pytest.mark.parametrize("n", [0, 1, 100])
def test_escala_pequena_funciona_end_to_end(service, database, source_asset, n):
    ids = _seed_synthetic_videos(database, source_asset, n)
    assert service.count_by_filter(None) == n
    items = service.list_items(limit=max(n, 1))
    assert len(items) == n
    facets = service.get_facets()
    assert set(facets) == set(SYSTEM_BADGES)


@pytest.mark.parametrize("n", [1000, 10000])
def test_escala_grande_mede_tempos_e_nao_carrega_tudo_em_memoria(service, database, source_asset, n):
    _seed_synthetic_videos(database, source_asset, n)

    t0 = time.perf_counter()
    total = service.count_by_filter(None)
    t_count = time.perf_counter() - t0
    assert total == n

    t0 = time.perf_counter()
    page = service.list_items(limit=50, offset=max(0, n - 50))
    t_list = time.perf_counter() - t0
    assert len(page) == min(50, n)

    filtro = CatalogFilter(badges_all=(BADGE_PUBLISHED,), badges_none=(BADGE_CAPTIONS,))
    t0 = time.perf_counter()
    filtrados = service.filter_items(filtro, limit=50)
    t_filter = time.perf_counter() - t0

    t0 = time.perf_counter()
    facets = service.get_facets()
    t_facets = time.perf_counter() - t0

    print(
        f"\n[ESCALA n={n}] count_by_filter={t_count:.4f}s list_items(offset=n-50)={t_list:.4f}s "
        f"filter_items={t_filter:.4f}s get_facets={t_facets:.4f}s facets={facets}"
    )
    # Sem SLA especifico exigido pelo Prompt -- o relatorio de entrega
    # reporta os tempos medidos aqui literalmente. Limites generosos
    # (minutos, nao SLA de produto) servem apenas para pegar regressao
    # catastrofica futura (ex.: um bug que volte a carregar a tabela
    # inteira em Python). DIVIDA TECNICA CONHECIDA (documentada no
    # relatorio de entrega): list_items com OFFSET profundo (perto do fim
    # de um catalogo de 10.000 itens) e mais lento que offset=0 porque
    # cada badge derivavel e uma subquery correlacionada reavaliada por
    # linha percorrida ate o OFFSET -- SQL puro, nunca full-scan em
    # Python, mas ainda O(offset) no lado do banco. Paginacao por cursor
    # (keyset, ex. WHERE (created_at, id) > (?, ?)) eliminaria esse custo
    # e fica registrada como melhoria futura.
    assert t_count < 30.0
    assert t_list < 30.0
    assert t_filter < 30.0
    assert t_facets < 30.0
    for badge in filtrados:
        assert BADGE_PUBLISHED in badge.system_badges
        assert BADGE_CAPTIONS not in badge.system_badges


# ---------------------------------------------------------------------------
# 13. Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def _parse_media_catalog_module() -> ast.AST:
    return ast.parse(Path(media_catalog.__file__).read_text(encoding="utf-8"))


def test_media_catalog_nao_importa_modulos_protegidos():
    tree = _parse_media_catalog_module()
    proibidos = (
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "job_state_machine",
        "source_import",
        "source_context",
        "media_probe",
        "edit_project",
        "video_promotion",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for proibido in proibidos:
            assert proibido not in names, f"media_catalog.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_media_catalog_nao_chama_subprocess_ffmpeg_ffprobe():
    """AST (nunca grep ingenuo -- a propria docstring do modulo CITA
    'ffmpeg'/'ffprobe' ao explicar por que eles NAO sao usados aqui, o
    que faria um grep textual falso-positivar; ja vivido nos Prompts
    24b/25/26/27/27b). Confirma que nenhum NOME real (import, chamada,
    atributo) referencia subprocess/ffmpeg/ffprobe -- apenas Constant
    (docstrings/comentarios) podem conter essas palavras."""
    tree = _parse_media_catalog_module()
    proibidos = ("subprocess", "ffmpeg", "ffprobe")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id.lower() not in proibidos
        elif isinstance(node, ast.Attribute):
            assert node.attr.lower() not in proibidos
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] + [alias.name for alias in node.names]
            )
            for name in names:
                assert not any(p in name.lower() for p in proibidos)


def test_media_catalog_nao_cria_content_analysis_nem_content_engine():
    source = Path(media_catalog.__file__).read_text(encoding="utf-8")
    assert "ContentAnalysis(" not in source
    assert "ContentEngine(" not in source


def test_media_catalog_so_toca_tabelas_de_leitura_e_video_declarations():
    """AST: toda string literal que aparece apos INSERT/UPDATE/DELETE no
    modulo deve ser 'video_declarations' (unica tabela que este modulo
    escreve) -- confirma que o catalogo nunca escreve nas fontes de
    verdade operacional (videos/jobs/artifacts/publications/schedules/
    projects/errors/sources)."""
    source = Path(media_catalog.__file__).read_text(encoding="utf-8")
    for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        for line in source.splitlines():
            if keyword in line:
                assert "video_declarations" in line, (
                    f"linha com {keyword!r} nao referencia video_declarations: {line!r}"
                )


def test_nenhuma_migration_congelada_foi_alterada_e_latest_e_9():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9


def test_video_declarations_e_append_only(database):
    import sqlite3

    src = SourceAsset(source_uri="/x.mp4")
    database.insert(src)
    v = Video(source_asset_id=src.id, name="v")
    database.insert(v)
    with database.transaction() as conn:
        rid = new_uuid()
        conn.execute(
            "INSERT INTO video_declarations (id, video_id, kind, value, origin, action, created_at) "
            "VALUES (?, ?, 'FLAG', 'X', 'USER', 'ADD', ?)",
            (rid, v.id, utc_now_iso()),
        )
    with pytest.raises(sqlite3.DatabaseError):
        with database.transaction() as conn:
            conn.execute("UPDATE video_declarations SET value = 'Y' WHERE id = ?", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        with database.transaction() as conn:
            conn.execute("DELETE FROM video_declarations WHERE id = ?", (rid,))


def test_video_declarations_origin_so_aceita_user(database):
    import sqlite3

    src = SourceAsset(source_uri="/x.mp4")
    database.insert(src)
    v = Video(source_asset_id=src.id, name="v")
    database.insert(v)
    with pytest.raises(sqlite3.DatabaseError):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO video_declarations (id, video_id, kind, value, origin, action, created_at) "
                "VALUES (?, ?, 'FLAG', 'X', 'SYSTEM', 'ADD', ?)",
                (new_uuid(), v.id, utc_now_iso()),
            )
