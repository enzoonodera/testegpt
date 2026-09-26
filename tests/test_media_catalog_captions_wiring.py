# -*- coding: utf-8 -*-
"""PROMPT "CATALOG WIRING / RECONCILIATION 27.5+30+31" -- testes.

Cobre exclusivamente a integração entre o Media Catalog (Prompt 27.5) e o
CaptionsEngine (Prompt 31) -- badges ``CAPTIONS``/``TRANSCRIBED`` passando
a refletir evidência real persistida (``Artifact``/checkpoint), nunca
configuração/intenção isolada. Não reabre nem re-testa CaptionsEngine em
si (isso é ``tests/test_captions_engine.py``, intocado) -- aqui só se
prova que o CATÁLOGO lê corretamente a evidência que o CaptionsEngine já
produz, via ``JobEngine``/``CaptionsEngine`` reais (nunca mocks do
catálogo). Cobre a lista de testes obrigatórios do Prompt (seção 13) +
os cenários adversariais de arquivo órfão/estado falso (seção 7) +
restart/cache/concorrência/filtros/seleção (seções 5/6/8/9/10) +
confirmação estrutural de que ``AUDIO_PROCESSED`` permanece ``"0"``
(seção 3/19).
"""
from __future__ import annotations

import os
import threading

import pytest

import _sistema.captions_engine as captions_engine
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.captions_engine import (
    ARTIFACT_KIND_SRT,
    ARTIFACT_KIND_TRANSCRIPT,
    ARTIFACT_KIND_VTT,
    CaptionsEngine,
    TranscriptionResult,
    TranscriptionSegment,
)
from _sistema.domain import Artifact, Job, JOB_FAILED, JOB_READY, Project, SourceAsset, Video
from _sistema.edit_project import EditProjectManager
from _sistema.job_engine import JobEngine, JobHandlerError
from _sistema.media_catalog import (
    BADGE_AUDIO_PROCESSED,
    BADGE_CAPTIONS,
    BADGE_EDITED,
    BADGE_SCHEDULED,
    BADGE_TRANSCRIBED,
    BulkFieldOp,
    BULK_SET,
    CatalogFilter,
    MediaCatalogService,
)
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_captions_engine.py/test_media_catalog.py)
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


def _fake_backend(segments=None, language="pt", calls=None):
    if segments is None:
        segments = (TranscriptionSegment(start=0.0, end=1.5, text="ola mundo"),)

    def backend(local_path, model_size, language_arg):
        if calls is not None:
            calls.append((local_path, model_size, language_arg))
        return TranscriptionResult(segments=segments, language=language)

    return backend


def _raising_backend(local_path, model_size, language):
    raise RuntimeError("falha simulada de backend")


def _make_engine(manager_, database_, storage_, app_paths_, audit_, *, backend=None, model_size=None):
    kwargs = dict(
        database=database_, storage_manager=storage_, app_paths=app_paths_, audit_log=audit_,
        transcription_backend=backend or _fake_backend(),
    )
    if model_size is not None:
        kwargs["default_model_size"] = model_size
    return CaptionsEngine(manager_, **kwargs)


@pytest.fixture
def engine(manager, database, storage, app_paths, audit):
    return _make_engine(manager, database, storage, app_paths, audit)


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "original.mp4"
    path.write_bytes(b"fake video bytes")
    return path


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


def _run_real_transcription(engine_, job_engine_, video_, project_=None):
    """Roda uma transcrição REAL de ponta a ponta (nunca mock do
    catálogo) -- mesmo padrão de ``test_captions_engine.py``. Devolve o
    ``Job`` concluído."""
    job_engine_.register_handler(
        CaptionsEngine.OPERATION, engine_.handle_transcription_job, claims_status="PROCESSING"
    )
    job = engine_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id if project_ else None, operation=CaptionsEngine.OPERATION)
    )
    result = job_engine_.advance(job.id)
    return job, result


# ===========================================================================
# 1/4 -- evidencia completa acende CAPTIONS/TRANSCRIBED
# ===========================================================================


def test_transcricao_completa_acende_captions_e_transcribed(catalog, engine, job_engine, video, project):
    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY

    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS in item.system_badges
    assert BADGE_TRANSCRIBED in item.system_badges


# ===========================================================================
# 2/5 -- PENDING nao acende
# ===========================================================================


def test_job_pending_sem_avancar_nao_acende_nenhuma_badge(catalog, engine, database, video, project):
    job = engine._audit_log.create_job(
        Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION)
    )
    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges
    assert BADGE_TRANSCRIBED not in item.system_badges


# ===========================================================================
# 3 -- FAILED nao acende (e nao apaga evidencia anterior valida -- secao 7(D))
# ===========================================================================


def test_job_failed_isolado_nao_acende_nenhuma_badge(catalog, manager, database, storage, app_paths, audit, video, project):
    engine_fail = _make_engine(manager, database, storage, app_paths, audit, backend=_raising_backend)
    job_engine_local = JobEngine(database, audit_log=audit)
    job_engine_local.register_handler(
        CaptionsEngine.OPERATION, engine_fail.handle_transcription_job, claims_status="PROCESSING"
    )
    job = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    with pytest.raises(JobHandlerError):
        job_engine_local.advance(job.id)
    reloaded = job_engine_local.get_job(job.id)
    assert reloaded.status == JOB_FAILED

    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges
    assert BADGE_TRANSCRIBED not in item.system_badges


def test_job_failed_posterior_nao_apaga_evidencia_valida_anterior(catalog, manager, database, storage, app_paths, audit, video, project):
    """Secao 7(D) do Prompt: um Job FAILED nao pode produzir falso
    NEGATIVO quando ja existe evidencia valida de uma execucao anterior
    bem-sucedida para o mesmo video (fingerprint/config diferente -- cache
    miss deliberado para nao apenas reaproveitar o cache hit)."""
    engine_ok = _make_engine(manager, database, storage, app_paths, audit, model_size="small")
    job_engine_ok = JobEngine(database, audit_log=audit)
    job_engine_ok.register_handler(
        CaptionsEngine.OPERATION, engine_ok.handle_transcription_job, claims_status="PROCESSING"
    )
    job1 = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    result1 = job_engine_ok.advance(job1.id)
    assert result1.status == JOB_READY
    assert BADGE_CAPTIONS in catalog.get_item(video.id).system_badges

    engine_fail = _make_engine(manager, database, storage, app_paths, audit, backend=_raising_backend, model_size="medium")
    job_engine_fail = JobEngine(database, audit_log=audit)
    job_engine_fail.register_handler(
        CaptionsEngine.OPERATION, engine_fail.handle_transcription_job, claims_status="PROCESSING"
    )
    job2 = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    with pytest.raises(JobHandlerError):
        job_engine_fail.advance(job2.id)
    reloaded2 = job_engine_fail.get_job(job2.id)
    assert reloaded2.status == JOB_FAILED

    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS in item.system_badges, "evidencia valida anterior nao pode ser apagada por uma falha posterior"
    assert BADGE_TRANSCRIBED in item.system_badges


# ===========================================================================
# 6 -- Artifact ausente nao acende nenhuma badge
# ===========================================================================


def test_nenhum_artifact_nenhuma_badge(catalog, video):
    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges
    assert BADGE_TRANSCRIBED not in item.system_badges


# ===========================================================================
# 7 -- arquivo orfao (existe no disco, sem Artifact no banco) nao acende
# ===========================================================================


def test_arquivo_orfao_sem_artifact_nao_acende(catalog, database, video, tmp_path):
    """Secao 7(A): um SRT/VTT/transcript real no disco, SEM nenhuma linha
    em ``artifacts``, nunca pode acender a badge -- fonte de verdade e
    SEMPRE a linha do banco, nunca 'o arquivo existe'."""
    orphan_srt = tmp_path / "orphan.srt"
    orphan_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nfake\n", encoding="utf-8")
    orphan_vtt = tmp_path / "orphan.vtt"
    orphan_vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nfake\n", encoding="utf-8")
    orphan_json = tmp_path / "orphan.json"
    orphan_json.write_text('{"segments": []}', encoding="utf-8")
    assert orphan_srt.is_file() and orphan_vtt.is_file() and orphan_json.is_file()

    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges
    assert BADGE_TRANSCRIBED not in item.system_badges


# ===========================================================================
# 8 -- Artifact sem arquivo fisico correspondente nao acende (secao 7(B))
# ===========================================================================


def test_artifact_com_arquivo_deletado_do_disco_nao_acende(catalog, engine, job_engine, video, project):
    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY
    item_antes = catalog.get_item(video.id)
    assert BADGE_CAPTIONS in item_antes.system_badges
    assert BADGE_TRANSCRIBED in item_antes.system_badges

    with engine._database.connection() as conn:
        rows = conn.execute(
            "SELECT path FROM artifacts WHERE video_id = ?", (video.id,)
        ).fetchall()
    for (path,) in rows:
        os.remove(path)

    item_depois = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item_depois.system_badges, "Artifact aponta pra arquivo que sumiu -- nao pode contar"
    assert BADGE_TRANSCRIBED not in item_depois.system_badges


def test_artifact_manualmente_inserido_com_path_inexistente_nao_acende(catalog, database, video):
    """Mesmo cenario da secao 7(B), mas construido diretamente (sem rodar
    o pipeline real) para provar que a checagem e sobre o PATH, nao sobre
    'quem escreveu a linha'."""
    fp = "fingerprint-fake-1"
    for kind in (ARTIFACT_KIND_SRT, ARTIFACT_KIND_VTT, ARTIFACT_KIND_TRANSCRIPT):
        database.insert(Artifact(video_id=video.id, kind=kind, path="/caminho/que/nao/existe.txt", fingerprint=fp))
    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges


# ===========================================================================
# 9 -- arquivo vazio (0 bytes) nao acende; arquivo nao-vazio "corrompido"
#      (conteudo invalido mas existente/nao-vazio) ACENDE -- decisao
#      documentada: o catalogo confirma existencia+tamanho, nunca reparsa
#      sintaxe SRT/VTT em toda consulta (secao 0.7 da docstring).
# ===========================================================================


def test_artifact_apontando_para_arquivo_vazio_nao_acende(catalog, database, video, tmp_path):
    fp = "fingerprint-fake-2"
    for kind in (ARTIFACT_KIND_SRT, ARTIFACT_KIND_VTT, ARTIFACT_KIND_TRANSCRIPT):
        p = tmp_path / f"{kind}.empty"
        p.write_bytes(b"")
        database.insert(Artifact(video_id=video.id, kind=kind, path=str(p), fingerprint=fp))
    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges


def test_artifact_com_conteudo_nao_srt_mas_arquivo_nao_vazio_ainda_acende(catalog, database, video, tmp_path):
    """Prova a decisao documentada: o catalogo NAO valida sintaxe SRT/VTT
    -- so existencia+tamanho > 0. Um arquivo com bytes de lixo (nao um SRT
    valido) ainda conta como 'evidencia de arquivo real' aqui; validar
    conteudo e responsabilidade do CaptionsEngine no momento da escrita,
    nao do catalogo em toda leitura (custaria reler N arquivos por
    consulta -- contradiria a arquitetura de duas consultas da secao 0.5)."""
    fp = "fingerprint-fake-3"
    for kind in (ARTIFACT_KIND_SRT, ARTIFACT_KIND_VTT, ARTIFACT_KIND_TRANSCRIPT):
        p = tmp_path / f"{kind}.garbage"
        p.write_bytes(b"\x00\x01lixo-binario-nao-e-srt-valido")
        database.insert(Artifact(video_id=video.id, kind=kind, path=str(p), fingerprint=fp))
    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS in item.system_badges


# ===========================================================================
# 10 -- fingerprints diferentes entre os 3 kinds nao contam como conjunto
# ===========================================================================


def test_fingerprints_diferentes_entre_os_3_kinds_nao_acendem(catalog, database, video, tmp_path):
    kinds_fps = {
        ARTIFACT_KIND_SRT: "fp-a",
        ARTIFACT_KIND_VTT: "fp-b",
        ARTIFACT_KIND_TRANSCRIPT: "fp-c",
    }
    for kind, fp in kinds_fps.items():
        p = tmp_path / f"{kind}.txt"
        p.write_text("conteudo", encoding="utf-8")
        database.insert(Artifact(video_id=video.id, kind=kind, path=str(p), fingerprint=fp))
    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges


def test_apenas_2_dos_3_kinds_presentes_nao_acende(catalog, database, video, tmp_path):
    fp = "fp-parcial"
    for kind in (ARTIFACT_KIND_SRT, ARTIFACT_KIND_VTT):
        p = tmp_path / f"{kind}.txt"
        p.write_text("conteudo", encoding="utf-8")
        database.insert(Artifact(video_id=video.id, kind=kind, path=str(p), fingerprint=fp))
    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges


# ===========================================================================
# 11 -- checkpoint sozinho, sem Artifact, nao acende TRANSCRIBED
# ===========================================================================


def test_checkpoint_sem_artifact_correspondente_nao_acende_transcribed(catalog, database, audit, video):
    """Decisao documentada (secao 0.7): checkpoint sozinho nao basta --
    tambem exige o Artifact transcript_internal com arquivo real."""
    job = audit.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    audit.record_checkpoint(job.id, captions_engine.CHECKPOINT_TRANSCRIBED, data={"cache_hit": False})
    item = catalog.get_item(video.id)
    assert BADGE_TRANSCRIBED not in item.system_badges


# ===========================================================================
# 12 -- cache hit preserva badge, nao duplica evidencia
# ===========================================================================


def test_cache_hit_preserva_badges_sem_duplicar_evidencia(catalog, database, engine, job_engine, video, project):
    calls = []
    engine._backend = _fake_backend(calls=calls)
    job1, result1 = _run_real_transcription(engine, job_engine, video, project)
    assert result1.status == JOB_READY
    assert len(calls) == 1
    item1 = catalog.get_item(video.id)
    assert BADGE_CAPTIONS in item1.system_badges
    assert BADGE_TRANSCRIBED in item1.system_badges

    job2 = engine._audit_log.create_job(
        Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION)
    )
    result2 = job_engine.advance(job2.id)
    assert result2.status == JOB_READY
    assert len(calls) == 1, "cache hit nao pode chamar o backend de novo"

    item2 = catalog.get_item(video.id)
    assert BADGE_CAPTIONS in item2.system_badges
    assert BADGE_TRANSCRIBED in item2.system_badges

    with engine._database.connection() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchone()
    assert n == 3, "cache hit nunca duplica Artifacts"


# ===========================================================================
# 13 -- restart preserva badge (instancias totalmente novas)
# ===========================================================================


def test_restart_com_instancias_totalmente_novas_preserva_badges(app_paths, engine, job_engine, video, project):
    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY

    db2 = LocalDatabase(app_paths.database / "painel.db")
    catalog2 = MediaCatalogService(db2)
    item = catalog2.get_item(video.id)
    assert BADGE_CAPTIONS in item.system_badges
    assert BADGE_TRANSCRIBED in item.system_badges


# ===========================================================================
# 14 -- isolamento entre multiplos videos (nenhum vazamento de badge)
# ===========================================================================


def test_badge_de_um_video_nao_vaza_para_outro(catalog, manager, database, storage, app_paths, audit, video, project, video_file):
    video_file2_path = video_file.parent / "outro.mp4"
    video_file2_path.write_bytes(b"outro video")
    source2 = SourceAsset(
        source_uri=str(video_file2_path), local_path=str(video_file2_path),
        original_name="outro.mp4", fingerprint="source-fp-2",
    )
    database.insert(source2)
    video2 = Video(source_asset_id=source2.id, name="v2")
    database.insert(video2)

    engine1 = _make_engine(manager, database, storage, app_paths, audit)
    job_engine1 = JobEngine(database, audit_log=audit)
    _, result1 = _run_real_transcription(engine1, job_engine1, video, project)
    assert result1.status == JOB_READY

    item1 = catalog.get_item(video.id)
    item2 = catalog.get_item(video2.id)
    assert BADGE_CAPTIONS in item1.system_badges
    assert BADGE_TRANSCRIBED in item1.system_badges
    assert BADGE_CAPTIONS not in item2.system_badges
    assert BADGE_TRANSCRIBED not in item2.system_badges


# ===========================================================================
# 15 -- concorrencia: leitura do catalogo durante processamento real
# ===========================================================================


def test_leituras_concorrentes_do_catalogo_durante_processamento_real(app_paths, manager, database, storage, audit, video, project):
    """Duas threads reais: uma roda a transcricao ate o fim; outra fica
    lendo ``get_item`` continuamente. Nunca deve lancar excecao, nunca
    deve observar um estado inconsistente (os 3 Artifacts sao escritos em
    UMA transaction atomica -- ver ``_write_and_register_artifacts`` --
    entao um leitor concorrente so pode ver 0 ou 3 linhas, nunca 1 ou 2)."""
    engine_local = _make_engine(manager, database, storage, app_paths, audit)
    job_engine_local = JobEngine(database, audit_log=audit)
    job_engine_local.register_handler(
        CaptionsEngine.OPERATION, engine_local.handle_transcription_job, claims_status="PROCESSING"
    )
    job = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))

    errors: "list[BaseException]" = []
    observed_partial = []
    stop = threading.Event()

    def reader():
        db_reader = LocalDatabase(app_paths.database / "painel.db")
        catalog_reader = MediaCatalogService(db_reader)
        try:
            while not stop.is_set():
                item = catalog_reader.get_item(video.id)
                if item is not None:
                    with db_reader.connection() as conn:
                        (n,) = conn.execute(
                            "SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)
                        ).fetchone()
                    if BADGE_CAPTIONS in item.system_badges and n != 3:
                        observed_partial.append(n)
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
    assert observed_partial == []
    assert result.status == JOB_READY
    assert BADGE_CAPTIONS in MediaCatalogService(database).get_item(video.id).system_badges


def test_dois_videos_processados_simultaneamente_nao_se_contaminam(app_paths, manager, database, storage, audit, video_file):
    videos = []
    for i in range(2):
        vf = video_file.parent / f"video{i}.mp4"
        vf.write_bytes(f"conteudo {i}".encode("utf-8"))
        src = SourceAsset(source_uri=str(vf), local_path=str(vf), original_name=f"v{i}.mp4", fingerprint=f"fp-{i}")
        database.insert(src)
        v = Video(source_asset_id=src.id, name=f"video-{i}")
        database.insert(v)
        videos.append(v)

    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []

    def worker(video_obj, segment_text):
        try:
            db_instance = LocalDatabase(app_paths.database / "painel.db")
            audit_instance = OperationalAuditLog(db_instance)
            engine_instance = CaptionsEngine(
                EditProjectManager(db_instance), database=db_instance,
                storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
                app_paths=app_paths, audit_log=audit_instance,
                transcription_backend=_fake_backend(segments=(TranscriptionSegment(start=0.0, end=1.0, text=segment_text),)),
            )
            job_engine_instance = JobEngine(db_instance, audit_log=audit_instance)
            job_engine_instance.register_handler(
                CaptionsEngine.OPERATION, engine_instance.handle_transcription_job, claims_status="PROCESSING"
            )
            job = audit_instance.create_job(Job(video_id=video_obj.id, operation=CaptionsEngine.OPERATION))
            barrier.wait(timeout=5)
            result = job_engine_instance.advance(job.id)
            assert result.status == JOB_READY
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(videos[0], "texto video 0")),
        threading.Thread(target=worker, args=(videos[1], "texto video 1")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    catalog_final = MediaCatalogService(database)
    item0 = catalog_final.get_item(videos[0].id)
    item1 = catalog_final.get_item(videos[1].id)
    assert BADGE_CAPTIONS in item0.system_badges
    assert BADGE_CAPTIONS in item1.system_badges


# ===========================================================================
# 16 -- filtros usam evidencia real
# ===========================================================================


def test_filtro_por_captions_so_devolve_video_com_evidencia_real(catalog, engine, job_engine, database, source, video, project):
    video_sem = Video(source_asset_id=source.id, name="sem-captions")
    database.insert(video_sem)

    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY

    filtro = CatalogFilter(badges_all=(BADGE_CAPTIONS,))
    itens = catalog.filter_items(filtro, limit=50)
    ids = {item.video_id for item in itens}
    assert video.id in ids
    assert video_sem.id not in ids


def test_filtro_combinado_captions_e_edited(catalog, database, engine, job_engine, video, project):
    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY

    database.insert(Project(video_id=video.id, edit_state={"categories": {"c": {"data": {}, "fingerprint": "f", "revision": 1, "updated_at": "now"}}}))

    filtro = CatalogFilter(badges_all=(BADGE_CAPTIONS, BADGE_EDITED))
    itens = catalog.filter_items(filtro, limit=50)
    assert {item.video_id for item in itens} == {video.id}


def test_filtro_sem_resultados_quando_nenhum_video_tem_ambas(catalog, database, engine, job_engine, video, project):
    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY

    filtro = CatalogFilter(badges_all=(BADGE_CAPTIONS, BADGE_SCHEDULED))
    assert catalog.filter_items(filtro) == ()
    assert catalog.count_by_filter(filtro) == 0


def test_filtro_todos_resultados_quando_todos_tem_evidencia(catalog, manager, database, storage, app_paths, audit, source):
    videos = []
    for i in range(3):
        v = Video(source_asset_id=source.id, name=f"video-transcrito-{i}")
        database.insert(v)
        videos.append(v)
        engine_i = _make_engine(manager, database, storage, app_paths, audit)
        job_engine_i = JobEngine(database, audit_log=audit)
        job_engine_i.register_handler(
            CaptionsEngine.OPERATION, engine_i.handle_transcription_job, claims_status="PROCESSING"
        )
        job = audit.create_job(Job(video_id=v.id, operation=CaptionsEngine.OPERATION))
        job_engine_i.advance(job.id)

    filtro = CatalogFilter(badges_all=(BADGE_CAPTIONS,))
    itens = catalog.filter_items(filtro, limit=50)
    assert {item.video_id for item in itens} == {v.id for v in videos}


# ===========================================================================
# 17 -- selecao filtrada (bulk) funciona com os novos badges
# ===========================================================================


def test_selecao_filtrada_por_captions_funciona_com_bulk_edit(catalog, database, engine, job_engine, source, video, project):
    video_sem = Video(source_asset_id=source.id, name="sem-captions-bulk")
    database.insert(video_sem)
    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY

    filtro = CatalogFilter(badges_all=(BADGE_CAPTIONS,))
    selecionados = tuple(item.video_id for item in catalog.filter_items(filtro, limit=50))
    assert selecionados == (video.id,)

    bulk_result = catalog.bulk_edit(selecionados, flags=BulkFieldOp(mode=BULK_SET, values=("REVIEWED",)))
    assert bulk_result.updated == (video.id,)
    assert bulk_result.failed == ()
    item_sem_bulk = catalog.get_item(video_sem.id)
    assert item_sem_bulk.user_flags == ()


# ===========================================================================
# 18 -- refresh durante processamento reflete o estado real a cada leitura
# ===========================================================================


def test_refresh_do_catalogo_antes_e_depois_do_processamento(catalog, engine, job_engine, video, project):
    antes = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in antes.system_badges

    job = engine._audit_log.create_job(
        Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION)
    )
    job_engine.register_handler(
        CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status="PROCESSING"
    )
    durante = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in durante.system_badges, "job so foi criado, ainda nao processado"

    result = job_engine.advance(job.id)
    assert result.status == JOB_READY
    depois = catalog.get_item(video.id)
    assert BADGE_CAPTIONS in depois.system_badges


# ===========================================================================
# 19 -- UNKNOWN nao produz sucesso
# ===========================================================================


def test_job_em_status_unknown_nao_acende_nenhuma_badge(catalog, database, video, project):
    database.insert(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION, status="UNKNOWN"))
    item = catalog.get_item(video.id)
    assert BADGE_CAPTIONS not in item.system_badges
    assert BADGE_TRANSCRIBED not in item.system_badges


# ===========================================================================
# 20 -- AUDIO_PROCESSED permanece "0" mesmo com evidencia rica adjacente
#       (inclusive TRANSCRIBED verdadeiro) -- confirmacao estrutural,
#       mesmo padrao ja usado no Prompt 30 para nao regredir.
# ===========================================================================


def test_audio_processed_permanece_hardcoded_mesmo_com_transcricao_completa(catalog, engine, job_engine, video, project):
    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY
    item = catalog.get_item(video.id)
    assert BADGE_TRANSCRIBED in item.system_badges
    assert BADGE_AUDIO_PROCESSED not in item.system_badges


def test_audio_processed_expressao_sql_e_literal_zero():
    from _sistema.media_catalog import _BADGE_SQL_EXPRESSIONS

    assert _BADGE_SQL_EXPRESSIONS[BADGE_AUDIO_PROCESSED] == "0"


# ===========================================================================
# 21 -- compatibilidade -- get_facets continua contando corretamente
# ===========================================================================


def test_get_facets_conta_captions_e_transcribed_corretamente(catalog, engine, job_engine, source, database, video, project):
    video_sem = Video(source_asset_id=source.id, name="sem-facets")
    database.insert(video_sem)
    job, result = _run_real_transcription(engine, job_engine, video, project)
    assert result.status == JOB_READY

    facets = catalog.get_facets()
    assert facets[BADGE_CAPTIONS] == 1
    assert facets[BADGE_TRANSCRIBED] == 1
    assert facets[BADGE_AUDIO_PROCESSED] == 0
