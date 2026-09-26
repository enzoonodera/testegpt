# -*- coding: utf-8 -*-
"""PROMPT "CATALOG WIRING -- REFRAMED_9_16 (Prompt 33)" -- testes.

Cobre exclusivamente a integração entre o Media Catalog (Prompt 27.5) e o
AutoReframeEngine (Prompt 33) -- badge ``REFRAMED_9_16`` passando a
refletir evidência real persistida (``Artifact`` ``reframe_track`` +
checkpoint ``MEDIA_PROCESSED``), nunca configuração/intenção isolada.
Não reabre nem re-testa AutoReframeEngine em si (isso é
``tests/test_auto_reframe.py``, intocado) -- aqui só se prova que o
CATÁLOGO lê corretamente a evidência que o AutoReframeEngine já produz,
via ``JobEngine``/``AutoReframeEngine`` reais (nunca mocks do catálogo).

Mesmo padrão estrutural de ``test_media_catalog_captions_wiring.py``
(precedente direto), adaptado para REFRAMED_9_16: evidência
completa acende; PENDING não acende; FAILED não acende e não apaga
evidência anterior válida; nenhum Artifact -> nenhuma badge; arquivo
órfão sem Artifact não acende; Artifact com arquivo deletado/inexistente
não acende; arquivo vazio não acende; checkpoint sozinho sem Artifact
correspondente não acende; cache hit preserva sem duplicar; restart
preserva; isolamento entre vídeos; concorrência; filtros; bulk; refresh;
UNKNOWN não acende; AUDIO_PROCESSED permanece fora de escopo; get_facets
conta corretamente -- MAIS o cenário específico desta rodada (seção 0.8
da docstring de ``media_catalog.py``): um resultado de FALLBACK (crop
central estático, sem detecção real) ainda acende a badge, por decisão
documentada.
"""
from __future__ import annotations

import os
import threading

import pytest

import _sistema.auto_reframe as auto_reframe
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.auto_reframe import (
    ARTIFACT_KIND_REFRAME_TRACK,
    ASPECT_RATIO_9_16,
    AutoReframeEngine,
    DetectionSample,
    FALLBACK_CENTER_CROP,
)
from _sistema.domain import Artifact, Job, JOB_FAILED, JOB_PROCESSING, JOB_READY, Project, SourceAsset, Video
from _sistema.edit_project import EditProjectManager
from _sistema.job_engine import JobEngine, JobHandlerError
from _sistema.media_catalog import (
    BADGE_AUDIO_PROCESSED,
    BADGE_EDITED,
    BADGE_REFRAMED_9_16,
    BADGE_SCHEDULED,
    BulkFieldOp,
    BULK_SET,
    CatalogFilter,
    MediaCatalogService,
)
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_auto_reframe.py/test_media_catalog.py)
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
def audit(database):
    return OperationalAuditLog(database)


@pytest.fixture
def storage(app_paths):
    return StorageManager(app_paths, database_path=app_paths.database / "painel.db")


@pytest.fixture
def manager(database):
    return EditProjectManager(database)


@pytest.fixture
def catalog(database):
    return MediaCatalogService(database)


@pytest.fixture
def job_engine(database, audit):
    return JobEngine(database, audit_log=audit)


def _fake_backend_found(cx=0.5, cy=0.3, kind="FACE", calls=None):
    def backend(local_path, t, width, height):
        if calls is not None:
            calls.append((local_path, t, width, height))
        return DetectionSample(found=True, cx=cx, cy=cy, kind=kind)

    return backend


def _fake_backend_not_found(calls=None):
    def backend(local_path, t, width, height):
        if calls is not None:
            calls.append((local_path, t, width, height))
        return DetectionSample(found=False)

    return backend


def _raising_backend(local_path, t, width, height):
    raise RuntimeError("falha simulada de backend")


def _make_engine(manager_, database_, storage_, app_paths_, audit_, *, backend=None, media_probe=None):
    return AutoReframeEngine(
        manager_, database=database_, storage_manager=storage_, app_paths=app_paths_,
        audit_log=audit_, detection_backend=backend or _fake_backend_found(),
        media_probe=media_probe,
    )


@pytest.fixture
def engine(manager, database, storage, app_paths, audit):
    return _make_engine(manager, database, storage, app_paths, audit)


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "original.mp4"
    path.write_bytes(b"fake video bytes")
    return path


class _FakeMediaProbeResult:
    def __init__(self, valid=True, width=64, height=64, duration=2.0, error_code=None):
        self.valid = valid
        self.width = width
        self.height = height
        self.duration = duration
        self.error_code = error_code


class _FakeMediaProbe(auto_reframe.MediaProbe):
    """Subclasse real (isinstance exigido pelo construtor) que devolve um
    resultado válido fixo sem depender de ffmpeg/ffprobe estarem
    disponíveis no ambiente -- mesmo racional de ``_FakeMediaProbe`` em
    ``test_silence_removal.py``."""

    def __init__(self, result=None):
        super().__init__()
        self._result = result or _FakeMediaProbeResult()

    def probe(self, path):
        return self._result


@pytest.fixture
def probe():
    return _FakeMediaProbe()


@pytest.fixture
def source(database, video_file):
    src = SourceAsset(
        source_uri=str(video_file), local_path=str(video_file),
        original_name="original.mp4", fingerprint="source-fp-1",
    )
    database.insert(src)
    return src


@pytest.fixture
def video(database, source):
    v = Video(source_asset_id=source.id, name="v")
    database.insert(v)
    return v


@pytest.fixture
def project(database, video):
    p = Project(video_id=video.id, name="p")
    database.insert(p)
    return p


def _enable_reframe(engine_, project_id, **overrides):
    fields = dict(enabled=True, target_aspect_ratio=ASPECT_RATIO_9_16, smoothing_strength=0.5, fallback_strategy=FALLBACK_CENTER_CROP)
    fields.update(overrides)
    return engine_.set_config(project_id, **fields)


def _run_real_reframe(engine_, job_engine_, video_, project_):
    """Roda um AutoReframe REAL de ponta a ponta (nunca mock do catálogo)
    -- mesmo padrão de ``test_auto_reframe.py``. Devolve o ``Job``
    concluído (via ``JobEngine.advance``, só ``.status`` disponível) + o
    resultado do JobEngine."""
    _enable_reframe(engine_, project_.id)
    job_engine_.register_handler(
        AutoReframeEngine.OPERATION, engine_.handle_reframe_job, claims_status="PROCESSING"
    )
    job = engine_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=AutoReframeEngine.OPERATION)
    )
    result = job_engine_.advance(job.id)
    return job, result


def _run_real_reframe_direct(engine_, audit_, video_, project_, **config_overrides):
    """Mesmo cenário de ``_run_real_reframe``, mas reivindicando o Job
    manualmente e chamando o handler direto -- devolve o ``JobStepResult``
    completo (``.data``/``.target_status``), já que
    ``JobEngine.advance`` devolve o ``Job`` (só ``.status``). Mesmo padrão
    de ``_run_job_direct`` em ``test_silence_removal.py``."""
    _enable_reframe(engine_, project_.id, **config_overrides)
    job = audit_.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=AutoReframeEngine.OPERATION)
    )
    audit_.transition_job(job.id, JOB_PROCESSING)
    claimed_job = engine_._database.get(Job, job.id)
    return engine_.handle_reframe_job(claimed_job)


# ===========================================================================
# 1 -- evidencia completa (deteccao real, sem fallback) acende REFRAMED_9_16
# ===========================================================================


def test_reframe_com_deteccao_real_acende_reframed_9_16(catalog, engine, audit, video, project, probe):
    engine._probe = probe
    result = _run_real_reframe_direct(engine, audit, video, project)
    assert result.target_status == JOB_READY
    assert result.data.get("fallback") is False

    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 in item.system_badges


# ===========================================================================
# 2 -- resultado FALLBACK tambem acende a badge (decisao documentada -- 0.8)
# ===========================================================================


def test_reframed_9_16_acende_mesmo_com_resultado_fallback(catalog, manager, database, storage, app_paths, audit, video, project, probe):
    """Decisao mais importante desta rodada (secao 0.8 da docstring de
    media_catalog.py): AutoReframeEngine trata fallback (crop central
    estatico, "fallback": true no Artifact) como um JOB_READY legitimo,
    produzindo evidencia real -- a badge representa 'AutoReframe rodou e
    produziu um resultado utilizavel', nao 'houve deteccao real', entao
    conta igualmente."""
    engine_fallback = _make_engine(manager, database, storage, app_paths, audit, backend=_fake_backend_not_found(), media_probe=probe)
    result = _run_real_reframe_direct(engine_fallback, audit, video, project)
    assert result.target_status == JOB_READY
    assert result.data.get("fallback") is True, "este teste precisa comprovadamente exercitar o caminho de fallback"

    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 in item.system_badges, "fallback deve acender a badge -- decisao documentada na secao 0.8"


# ===========================================================================
# 3 -- PENDING nao acende
# ===========================================================================


def test_job_pending_sem_avancar_nao_acende_badge(catalog, engine, video, project):
    _enable_reframe(engine, project.id)
    job = engine._audit_log.create_job(
        Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION)
    )
    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


# ===========================================================================
# 4 -- FAILED nao acende (e nao apaga evidencia anterior valida)
# ===========================================================================


def test_job_failed_isolado_nao_acende_badge(catalog, manager, database, storage, app_paths, audit, video, project, probe):
    # media_probe invalido faz o Job falhar estruturalmente ANTES da
    # deteccao -- usamos isso para simular uma falha estrutural real
    # (probe invalido) sem depender de excecao do backend (que nunca
    # falha o Job -- vira fallback, ver teste 2 acima).
    engine_fail = _make_engine(
        manager, database, storage, app_paths, audit, backend=_raising_backend,
        media_probe=_FakeMediaProbe(_FakeMediaProbeResult(valid=False, error_code="PROBE_INVALIDO")),
    )
    job_engine_local = JobEngine(database, audit_log=audit)
    _enable_reframe(engine_fail, project.id)
    job_engine_local.register_handler(
        AutoReframeEngine.OPERATION, engine_fail.handle_reframe_job, claims_status="PROCESSING"
    )
    job = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))
    result = job_engine_local.advance(job.id)
    assert result.status == JOB_FAILED

    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


def test_job_failed_posterior_nao_apaga_evidencia_valida_anterior(catalog, manager, database, storage, app_paths, audit, video, project, probe):
    engine_ok = _make_engine(manager, database, storage, app_paths, audit, media_probe=probe)
    job_engine_ok = JobEngine(database, audit_log=audit)
    job1, result1 = _run_real_reframe(engine_ok, job_engine_ok, video, project)
    assert result1.status == JOB_READY
    assert BADGE_REFRAMED_9_16 in catalog.get_item(video.id).system_badges

    # smoothing_strength diferente do job1 -- forca cache MISS (chave de
    # cache inclui a config), garantindo que o probe invalido seja
    # realmente exercitado em vez de a segunda execucao apenas reusar o
    # Artifact cacheado do job1 sem sequer chamar o probe.
    engine_fail = _make_engine(manager, database, storage, app_paths, audit, media_probe=_FakeMediaProbe(_FakeMediaProbeResult(valid=False)))
    job_engine_fail = JobEngine(database, audit_log=audit)
    _enable_reframe(engine_fail, project.id, smoothing_strength=0.9)
    job_engine_fail.register_handler(
        AutoReframeEngine.OPERATION, engine_fail.handle_reframe_job, claims_status="PROCESSING"
    )
    job2 = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))
    result2 = job_engine_fail.advance(job2.id)
    assert result2.status == JOB_FAILED

    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 in item.system_badges, "evidencia valida anterior nao pode ser apagada por uma falha posterior"


# ===========================================================================
# 5 -- Artifact ausente nao acende nenhuma badge
# ===========================================================================


def test_nenhum_artifact_nenhuma_badge(catalog, video):
    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


# ===========================================================================
# 6 -- arquivo orfao (existe no disco, sem Artifact no banco) nao acende
# ===========================================================================


def test_arquivo_orfao_sem_artifact_nao_acende(catalog, video, tmp_path):
    orphan = tmp_path / "orphan_reframe.json"
    orphan.write_text('{"keyframes": []}', encoding="utf-8")
    assert orphan.is_file()

    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


# ===========================================================================
# 7 -- Artifact sem arquivo fisico correspondente nao acende
# ===========================================================================


def test_artifact_com_arquivo_deletado_do_disco_nao_acende(catalog, engine, job_engine, video, project, probe):
    engine._probe = probe
    job, result = _run_real_reframe(engine, job_engine, video, project)
    assert result.status == JOB_READY
    assert BADGE_REFRAMED_9_16 in catalog.get_item(video.id).system_badges

    with engine._database.connection() as conn:
        rows = conn.execute(
            "SELECT path FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
        ).fetchall()
    assert rows
    for (path,) in rows:
        os.remove(path)

    item_depois = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item_depois.system_badges, "Artifact aponta pra arquivo que sumiu -- nao pode contar"


def test_artifact_manualmente_inserido_com_path_inexistente_nao_acende(catalog, database, audit, video):
    job = audit.create_job(Job(video_id=video.id, operation=AutoReframeEngine.OPERATION))
    audit.record_checkpoint(job.id, auto_reframe.CHECKPOINT_MEDIA_PROCESSED, data={"cache_hit": False, "fallback": False})
    database.insert(Artifact(video_id=video.id, kind=ARTIFACT_KIND_REFRAME_TRACK, path="/caminho/que/nao/existe.json", fingerprint="fp-fake"))
    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


# ===========================================================================
# 8 -- arquivo vazio (0 bytes) nao acende
# ===========================================================================


def test_artifact_apontando_para_arquivo_vazio_nao_acende(catalog, database, audit, video, tmp_path):
    job = audit.create_job(Job(video_id=video.id, operation=AutoReframeEngine.OPERATION))
    audit.record_checkpoint(job.id, auto_reframe.CHECKPOINT_MEDIA_PROCESSED, data={"cache_hit": False, "fallback": False})
    p = tmp_path / "reframe_track.empty"
    p.write_bytes(b"")
    database.insert(Artifact(video_id=video.id, kind=ARTIFACT_KIND_REFRAME_TRACK, path=str(p), fingerprint="fp-vazio"))
    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


# ===========================================================================
# 9 -- checkpoint sozinho, sem Artifact, nao acende REFRAMED_9_16
#      (secao 0.8: checkpoint MEDIA_PROCESSED nao e exclusivo do
#      AutoReframe -- exigir tambem o Artifact e o que resolve a
#      ambiguidade)
# ===========================================================================


def test_checkpoint_sem_artifact_correspondente_nao_acende_reframed(catalog, database, audit, video):
    job = audit.create_job(Job(video_id=video.id, operation=AutoReframeEngine.OPERATION))
    audit.record_checkpoint(job.id, auto_reframe.CHECKPOINT_MEDIA_PROCESSED, data={"cache_hit": False, "fallback": False})
    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


def test_checkpoint_de_outra_operacao_generica_nao_acende_reframed(catalog, database, audit, video):
    """Prova concreta do risco documentado na secao 0.8: mesmo que outro
    Job qualquer grave o MESMO checkpoint generico MEDIA_PROCESSED (o
    vocabulario permite -- nao e exclusivo do AutoReframe), sem o
    Artifact 'reframe_track' correspondente a badge nao acende."""
    job = audit.create_job(Job(video_id=video.id, operation="OUTRA_OPERACAO_QUALQUER"))
    audit.record_checkpoint(job.id, auto_reframe.CHECKPOINT_MEDIA_PROCESSED, data={"nota": "operacao nao relacionada ao AutoReframe"})
    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


# ===========================================================================
# 10 -- cache hit preserva badge, nao duplica evidencia
# ===========================================================================


def test_cache_hit_preserva_badge_sem_duplicar_evidencia(catalog, database, engine, audit, video, project, probe):
    engine._probe = probe
    calls = []
    engine._backend = _fake_backend_found(calls=calls)
    result1 = _run_real_reframe_direct(engine, audit, video, project)
    assert result1.target_status == JOB_READY
    n_calls_apos_1 = len(calls)
    assert n_calls_apos_1 > 0
    item1 = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 in item1.system_badges

    result2 = _run_real_reframe_direct(engine, audit, video, project)
    assert result2.target_status == JOB_READY
    assert result2.data.get("cache_hit") is True
    assert len(calls) == n_calls_apos_1, "cache hit nao pode chamar o backend de novo"

    item2 = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 in item2.system_badges

    with engine._database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
        ).fetchone()
    assert n == 1, "cache hit nunca duplica Artifact"


# ===========================================================================
# 11 -- restart preserva badge (instancias totalmente novas)
# ===========================================================================


def test_restart_com_instancias_totalmente_novas_preserva_badge(app_paths, engine, job_engine, video, project, probe):
    engine._probe = probe
    job, result = _run_real_reframe(engine, job_engine, video, project)
    assert result.status == JOB_READY

    db2 = LocalDatabase(app_paths.database / "painel.db")
    catalog2 = MediaCatalogService(db2)
    item = catalog2.get_item(video.id)
    assert BADGE_REFRAMED_9_16 in item.system_badges


# ===========================================================================
# 12 -- isolamento entre multiplos videos
# ===========================================================================


def test_badge_de_um_video_nao_vaza_para_outro(catalog, manager, database, storage, app_paths, audit, video, project, video_file, probe):
    video_file2_path = video_file.parent / "outro.mp4"
    video_file2_path.write_bytes(b"outro video")
    source2 = SourceAsset(
        source_uri=str(video_file2_path), local_path=str(video_file2_path),
        original_name="outro.mp4", fingerprint="source-fp-2",
    )
    database.insert(source2)
    video2 = Video(source_asset_id=source2.id, name="v2")
    database.insert(video2)

    engine1 = _make_engine(manager, database, storage, app_paths, audit, media_probe=probe)
    job_engine1 = JobEngine(database, audit_log=audit)
    _, result1 = _run_real_reframe(engine1, job_engine1, video, project)
    assert result1.status == JOB_READY

    item1 = catalog.get_item(video.id)
    item2 = catalog.get_item(video2.id)
    assert BADGE_REFRAMED_9_16 in item1.system_badges
    assert BADGE_REFRAMED_9_16 not in item2.system_badges


# ===========================================================================
# 13 -- concorrencia: leitura do catalogo durante processamento real
# ===========================================================================


def test_leituras_concorrentes_do_catalogo_durante_processamento_real(app_paths, manager, database, storage, audit, video, project, probe):
    engine_local = _make_engine(manager, database, storage, app_paths, audit, media_probe=probe)
    job_engine_local = JobEngine(database, audit_log=audit)
    _enable_reframe(engine_local, project.id)
    job_engine_local.register_handler(
        AutoReframeEngine.OPERATION, engine_local.handle_reframe_job, claims_status="PROCESSING"
    )
    job = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))

    errors: "list[BaseException]" = []
    observed_inconsistent = []
    stop = threading.Event()

    def reader():
        db_reader = LocalDatabase(app_paths.database / "painel.db")
        catalog_reader = MediaCatalogService(db_reader)
        try:
            while not stop.is_set():
                item = catalog_reader.get_item(video.id)
                if item is not None and BADGE_REFRAMED_9_16 in item.system_badges:
                    with db_reader.connection() as conn:
                        (n,) = conn.execute(
                            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
                            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
                        ).fetchone()
                    if n != 1:
                        observed_inconsistent.append(n)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        result = job_engine_local.advance(job.id)
    finally:
        stop.set()
        reader_thread.join(timeout=10)

    assert errors == []
    assert observed_inconsistent == []
    assert result.status == JOB_READY
    assert BADGE_REFRAMED_9_16 in MediaCatalogService(database).get_item(video.id).system_badges


def test_dois_videos_processados_simultaneamente_nao_se_contaminam(app_paths, manager, database, storage, audit, video_file):
    videos = []
    projects = []
    for i in range(2):
        vf = video_file.parent / f"video{i}.mp4"
        vf.write_bytes(f"conteudo {i}".encode("utf-8"))
        src = SourceAsset(source_uri=str(vf), local_path=str(vf), original_name=f"v{i}.mp4", fingerprint=f"fp-{i}")
        database.insert(src)
        v = Video(source_asset_id=src.id, name=f"video-{i}")
        database.insert(v)
        p = Project(video_id=v.id, name=f"proj-{i}")
        database.insert(p)
        videos.append(v)
        projects.append(p)

    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []

    def worker(video_obj, project_obj):
        try:
            db_instance = LocalDatabase(app_paths.database / "painel.db")
            audit_instance = OperationalAuditLog(db_instance)
            engine_instance = AutoReframeEngine(
                EditProjectManager(db_instance), database=db_instance,
                storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
                app_paths=app_paths, audit_log=audit_instance,
                detection_backend=_fake_backend_found(), media_probe=_FakeMediaProbe(),
            )
            job_engine_instance = JobEngine(db_instance, audit_log=audit_instance)
            _enable_reframe(engine_instance, project_obj.id)
            job_engine_instance.register_handler(
                AutoReframeEngine.OPERATION, engine_instance.handle_reframe_job, claims_status="PROCESSING"
            )
            job = audit_instance.create_job(Job(video_id=video_obj.id, project_id=project_obj.id, operation=AutoReframeEngine.OPERATION))
            barrier.wait(timeout=5)
            result = job_engine_instance.advance(job.id)
            assert result.status == JOB_READY
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(videos[0], projects[0])),
        threading.Thread(target=worker, args=(videos[1], projects[1])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    catalog_final = MediaCatalogService(database)
    item0 = catalog_final.get_item(videos[0].id)
    item1 = catalog_final.get_item(videos[1].id)
    assert BADGE_REFRAMED_9_16 in item0.system_badges
    assert BADGE_REFRAMED_9_16 in item1.system_badges


# ===========================================================================
# 14 -- filtros usam evidencia real
# ===========================================================================


def test_filtro_por_reframed_so_devolve_video_com_evidencia_real(catalog, engine, job_engine, database, source, video, project, probe):
    engine._probe = probe
    video_sem = Video(source_asset_id=source.id, name="sem-reframe")
    database.insert(video_sem)

    job, result = _run_real_reframe(engine, job_engine, video, project)
    assert result.status == JOB_READY

    filtro = CatalogFilter(badges_all=(BADGE_REFRAMED_9_16,))
    itens = catalog.filter_items(filtro, limit=50)
    ids = {item.video_id for item in itens}
    assert video.id in ids
    assert video_sem.id not in ids


def test_filtro_combinado_reframed_e_edited(catalog, database, engine, job_engine, video, project, probe):
    engine._probe = probe
    job, result = _run_real_reframe(engine, job_engine, video, project)
    assert result.status == JOB_READY

    database.insert(Project(video_id=video.id, edit_state={"categories": {"c": {"data": {}, "fingerprint": "f", "revision": 1, "updated_at": "now"}}}))

    filtro = CatalogFilter(badges_all=(BADGE_REFRAMED_9_16, BADGE_EDITED))
    itens = catalog.filter_items(filtro, limit=50)
    assert {item.video_id for item in itens} == {video.id}


def test_filtro_sem_resultados_quando_nenhum_video_tem_ambas(catalog, database, engine, job_engine, video, project, probe):
    engine._probe = probe
    job, result = _run_real_reframe(engine, job_engine, video, project)
    assert result.status == JOB_READY

    filtro = CatalogFilter(badges_all=(BADGE_REFRAMED_9_16, BADGE_SCHEDULED))
    assert catalog.filter_items(filtro) == ()
    assert catalog.count_by_filter(filtro) == 0


def test_filtro_todos_resultados_quando_todos_tem_evidencia(catalog, manager, database, storage, app_paths, audit, source):
    videos = []
    for i in range(3):
        v = Video(source_asset_id=source.id, name=f"video-reframed-{i}")
        database.insert(v)
        p = Project(video_id=v.id, name=f"proj-{i}")
        database.insert(p)
        videos.append(v)
        engine_i = _make_engine(manager, database, storage, app_paths, audit, media_probe=_FakeMediaProbe())
        job_engine_i = JobEngine(database, audit_log=audit)
        _enable_reframe(engine_i, p.id)
        job_engine_i.register_handler(
            AutoReframeEngine.OPERATION, engine_i.handle_reframe_job, claims_status="PROCESSING"
        )
        job = audit.create_job(Job(video_id=v.id, project_id=p.id, operation=AutoReframeEngine.OPERATION))
        job_engine_i.advance(job.id)

    filtro = CatalogFilter(badges_all=(BADGE_REFRAMED_9_16,))
    itens = catalog.filter_items(filtro, limit=50)
    assert {item.video_id for item in itens} == {v.id for v in videos}


# ===========================================================================
# 15 -- selecao filtrada (bulk) funciona com o novo badge
# ===========================================================================


def test_selecao_filtrada_por_reframed_funciona_com_bulk_edit(catalog, database, engine, job_engine, source, video, project, probe):
    engine._probe = probe
    video_sem = Video(source_asset_id=source.id, name="sem-reframe-bulk")
    database.insert(video_sem)
    job, result = _run_real_reframe(engine, job_engine, video, project)
    assert result.status == JOB_READY

    filtro = CatalogFilter(badges_all=(BADGE_REFRAMED_9_16,))
    selecionados = tuple(item.video_id for item in catalog.filter_items(filtro, limit=50))
    assert selecionados == (video.id,)

    bulk_result = catalog.bulk_edit(selecionados, flags=BulkFieldOp(mode=BULK_SET, values=("REVIEWED",)))
    assert bulk_result.updated == (video.id,)
    assert bulk_result.failed == ()
    item_sem_bulk = catalog.get_item(video_sem.id)
    assert item_sem_bulk.user_flags == ()


# ===========================================================================
# 16 -- refresh durante processamento reflete o estado real a cada leitura
# ===========================================================================


def test_refresh_do_catalogo_antes_e_depois_do_processamento(catalog, engine, job_engine, video, project, probe):
    engine._probe = probe
    _enable_reframe(engine, project.id)
    antes = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in antes.system_badges

    job = engine._audit_log.create_job(
        Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION)
    )
    job_engine.register_handler(
        AutoReframeEngine.OPERATION, engine.handle_reframe_job, claims_status="PROCESSING"
    )
    durante = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in durante.system_badges, "job so foi criado, ainda nao processado"

    result = job_engine.advance(job.id)
    assert result.status == JOB_READY
    depois = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 in depois.system_badges


# ===========================================================================
# 17 -- UNKNOWN nao produz sucesso
# ===========================================================================


def test_job_em_status_unknown_nao_acende_badge(catalog, database, video, project):
    database.insert(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION, status="UNKNOWN"))
    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 not in item.system_badges


# ===========================================================================
# 18 -- AUDIO_PROCESSED permanece "0" mesmo com evidencia rica adjacente
#       (inclusive REFRAMED_9_16 verdadeiro) -- confirmacao estrutural,
#       mesmo padrao ja usado nas rodadas anteriores para nao regredir.
# ===========================================================================


def test_audio_processed_permanece_hardcoded_mesmo_com_reframe_completo(catalog, engine, job_engine, video, project, probe):
    engine._probe = probe
    job, result = _run_real_reframe(engine, job_engine, video, project)
    assert result.status == JOB_READY
    item = catalog.get_item(video.id)
    assert BADGE_REFRAMED_9_16 in item.system_badges
    assert BADGE_AUDIO_PROCESSED not in item.system_badges


def test_audio_processed_expressao_sql_e_literal_zero():
    from _sistema.media_catalog import _BADGE_SQL_EXPRESSIONS

    assert _BADGE_SQL_EXPRESSIONS[BADGE_AUDIO_PROCESSED] == "0"


# ===========================================================================
# 19 -- compatibilidade -- get_facets continua contando corretamente
# ===========================================================================


def test_get_facets_conta_reframed_corretamente(catalog, engine, job_engine, source, database, video, project, probe):
    engine._probe = probe
    video_sem = Video(source_asset_id=source.id, name="sem-facets-reframe")
    database.insert(video_sem)
    job, result = _run_real_reframe(engine, job_engine, video, project)
    assert result.status == JOB_READY

    facets = catalog.get_facets()
    assert facets[BADGE_REFRAMED_9_16] == 1
    assert facets[BADGE_AUDIO_PROCESSED] == 0
