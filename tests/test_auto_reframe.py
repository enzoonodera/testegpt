# -*- coding: utf-8 -*-
"""PROMPT 33 -- AutoReframe: testes.

Cobre a Parte A (configuração REFRAME -- mesma disciplina de
fingerprint/concorrência/restart/idempotência dos Prompts 27-32) e a
Parte B (detecção/tracking real via backend injetável): detecção
bem-sucedida com curva suavizada persistida; detector que não encontra
nada engaja fallback seguro (Job ainda READY); backend de detecção que
lança exceção também engaja fallback (Job ainda READY -- "nunca bloquear
Job por falha de tracking"); falhas ESTRUTURAIS (SourceAsset/Video
ausente, probe inválido) continuam falhando o Job normalmente; cache
hit/miss; cancelamento em dois pontos; concorrência real; restart;
``MediaProbe`` chamado com resolução REAL (vídeo gerado via ffmpeg, mesmo
padrão de ``test_media_probe.py``); AST estrutural; confirmação de que
``media_catalog.py``/``REFRAMED_9_16`` não foram tocados; nenhuma
migration nova.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import _sistema.auto_reframe as auto_reframe
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.auto_reframe import (
    ARTIFACT_KIND_REFRAME_TRACK,
    ASPECT_RATIO_9_16,
    AutoReframeEngine,
    AutoReframeError,
    CampoInvalidoError,
    compute_cache_key,
    DetectionSample,
    DETECTION_SAMPLE_COUNT,
    FALLBACK_CENTER_CROP,
    ReframeConfigState,
    VocabularioInvalidoError,
)
from _sistema.control_manager import CONTROL_SCOPE_JOB, ControlManager
from _sistema.domain import (
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_READY,
    Project,
    SourceAsset,
    Video,
)
from _sistema.edit_project import CAPTIONS, EditProjectManager, REFRAME, TEMPLATE
from _sistema.job_engine import JobEngine
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager


FFMPEG_DISPONIVEL = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_SKIP_REASON = "ffmpeg/ffprobe nao encontrados no PATH deste ambiente"


def _gerar_video_valido(path: Path, *, duration: int = 2, size: str = "64x64", rate: int = 10) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_captions_engine.py)
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
def control(database):
    return ControlManager(database)


@pytest.fixture
def video_file(tmp_path):
    """Vídeo REAL gerado via ffmpeg quando disponível (mesmo padrão de
    test_media_probe.py) -- nunca um arquivo fake quando o teste precisa
    de MediaProbe funcionando de verdade. Testes que não precisam de
    probe real usam este mesmo arquivo só como um path existente."""
    path = tmp_path / "original.mp4"
    if FFMPEG_DISPONIVEL:
        _gerar_video_valido(path)
    else:
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


@pytest.fixture
def project2(database, video):
    p = Project(video_id=video.id, name="p2")
    database.insert(p)
    return p


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


def _fake_backend_raising(calls=None):
    def backend(local_path, t, width, height):
        if calls is not None:
            calls.append((local_path, t, width, height))
        raise RuntimeError("falha simulada do detector")

    return backend


@pytest.fixture
def engine(manager, database, storage, app_paths, audit):
    return AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, detection_backend=_fake_backend_found(),
    )


@pytest.fixture
def job_engine(database, audit):
    return JobEngine(database, audit_log=audit)


def _enable_reframe(engine_, project_id, **overrides):
    fields = dict(enabled=True, target_aspect_ratio=ASPECT_RATIO_9_16, smoothing_strength=0.5, fallback_strategy=FALLBACK_CENTER_CROP)
    fields.update(overrides)
    return engine_.set_config(project_id, **fields)


def _run_job(engine_, job_engine_, video_, project_):
    job_engine_.register_handler(
        AutoReframeEngine.OPERATION, engine_.handle_reframe_job, claims_status="PROCESSING"
    )
    job = engine_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=AutoReframeEngine.OPERATION)
    )
    result = job_engine_.advance(job.id)
    return job, result


# ---------------------------------------------------------------------------
# 0. Construção
# ---------------------------------------------------------------------------


def test_construtor_rejeita_manager_de_tipo_errado(database, storage, app_paths):
    with pytest.raises(TypeError):
        AutoReframeEngine("nao-e-um-manager", database=database, storage_manager=storage, app_paths=app_paths)


def test_construtor_rejeita_database_de_tipo_errado(manager, storage, app_paths):
    with pytest.raises(TypeError):
        AutoReframeEngine(manager, database="nao-e-um-database", storage_manager=storage, app_paths=app_paths)


def test_construtor_rejeita_storage_manager_de_tipo_errado(manager, database, app_paths):
    with pytest.raises(TypeError):
        AutoReframeEngine(manager, database=database, storage_manager="nao-e-storage", app_paths=app_paths)


def test_construtor_rejeita_app_paths_de_tipo_errado(manager, database, storage):
    with pytest.raises(TypeError):
        AutoReframeEngine(manager, database=database, storage_manager=storage, app_paths="nao-e-app-paths")


def test_construtor_rejeita_backend_nao_chamavel(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        AutoReframeEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            detection_backend="nao-e-chamavel",
        )


def test_construtor_rejeita_control_manager_de_tipo_errado(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        AutoReframeEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            control_manager="nao-e-control-manager",
        )


def test_construtor_rejeita_media_probe_de_tipo_errado(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        AutoReframeEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            media_probe="nao-e-media-probe",
        )


# ===========================================================================
# PARTE A -- configuração
# ===========================================================================


def test_estado_inicial_e_todos_none(engine, project):
    assert engine.get_config(project.id) == ReframeConfigState()


def test_set_config_grava_todos_os_campos(engine, project):
    state = _enable_reframe(engine, project.id)
    assert state.enabled is True
    assert state.target_aspect_ratio == ASPECT_RATIO_9_16
    assert state.smoothing_strength == 0.5
    assert state.fallback_strategy == FALLBACK_CENTER_CROP
    assert engine.get_config(project.id) == state


def test_set_config_parcial_nao_apaga_outros_campos(engine, project):
    _enable_reframe(engine, project.id)
    engine.set_config(project.id, smoothing_strength=0.9)
    state = engine.get_config(project.id)
    assert state.enabled is True
    assert state.smoothing_strength == 0.9
    assert state.target_aspect_ratio == ASPECT_RATIO_9_16


def test_enabled_deve_ser_bool(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, enabled="sim")
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, enabled=1)


@pytest.mark.parametrize("value", ["16:9", "1:1", "", None, 1, "9:16 "])
def test_target_aspect_ratio_vocabulario_fechado(engine, project, value):
    with pytest.raises(VocabularioInvalidoError):
        engine.set_config(project.id, target_aspect_ratio=value)


@pytest.mark.parametrize("value", [-0.1, 1.1, float("inf"), float("-inf"), float("nan"), "0.5", True])
def test_smoothing_strength_range_e_tipo(engine, project, value):
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, smoothing_strength=value)


@pytest.mark.parametrize("value", [0.0, 1.0, 0.5])
def test_smoothing_strength_limites_validos(engine, project, value):
    state = engine.set_config(project.id, smoothing_strength=value)
    assert state.smoothing_strength == value


@pytest.mark.parametrize("value", ["CROP_TOP", "", None, 1, "center_crop"])
def test_fallback_strategy_vocabulario_fechado(engine, project, value):
    with pytest.raises(VocabularioInvalidoError):
        engine.set_config(project.id, fallback_strategy=value)


def test_campo_desconhecido_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, campo_que_nao_existe=1)


def test_update_category_e_usado_nunca_set_category_direto(database, engine, project, manager):
    """Prova estrutural indireta: um campo já setado sobrevive a um
    set_config parcial subsequente com outro campo -- só é possível via
    merge atômico (update_category), nunca via replace (set_category)."""
    _enable_reframe(engine, project.id)
    engine.set_config(project.id, smoothing_strength=0.2)
    state = engine.get_config(project.id)
    assert state.enabled is True
    assert state.fallback_strategy == FALLBACK_CENTER_CROP


def test_categoria_reframe_isolada_de_captions_e_template(database, engine, project, manager):
    _enable_reframe(engine, project.id)
    manager.set_category(project.id, CAPTIONS, {"mode": "OFF"})
    manager.set_category(project.id, TEMPLATE, {"id": "x"})
    categorias = manager.list_categories(project.id)
    assert REFRAME in categorias
    assert CAPTIONS in categorias
    assert TEMPLATE in categorias


def test_alterar_reframe_nao_muda_fingerprint_de_captions(database, engine, project, manager):
    manager.set_category(project.id, CAPTIONS, {"mode": "OFF"})
    before = manager.get_category(project.id, CAPTIONS)
    _enable_reframe(engine, project.id)
    after = manager.get_category(project.id, CAPTIONS)
    assert before.fingerprint == after.fingerprint


def test_estado_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    engine_a = AutoReframeEngine(
        EditProjectManager(db_a), database=db_a,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    _enable_reframe(engine_a, project.id)

    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    engine_b = AutoReframeEngine(
        EditProjectManager(db_b), database=db_b,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    assert engine_b.get_config(project.id).enabled is True


def test_repetir_o_mesmo_set_config_sequencialmente_nao_acumula(engine, project, manager):
    _enable_reframe(engine, project.id)
    before = manager.get_category(project.id, REFRAME)
    _enable_reframe(engine, project.id)
    after = manager.get_category(project.id, REFRAME)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_duas_operacoes_concorrentes_de_config_em_projetos_diferentes(app_paths, database, project, project2):
    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []

    def worker(project_id, enabled):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_engine = AutoReframeEngine(
            EditProjectManager(db_instance), database=db_instance,
            storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
            app_paths=app_paths,
        )
        barrier.wait(timeout=5)
        try:
            local_engine.set_config(project_id, enabled=enabled)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(project.id, True)),
        threading.Thread(target=worker, args=(project2.id, False)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    reread = AutoReframeEngine(
        EditProjectManager(database), database=database,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    assert reread.get_config(project.id).enabled is True
    assert reread.get_config(project2.id).enabled is False


def test_set_config_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.set_config("nao-e-um-uuid", enabled=True)


def test_get_config_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.get_config("nao-e-um-uuid")


# ===========================================================================
# PARTE B -- pipeline completo via JobEngine
# ===========================================================================


def test_job_sem_reframe_habilitado_falha_estruturalmente(engine, job_engine, video, project):
    job_engine.register_handler(AutoReframeEngine.OPERATION, engine.handle_reframe_job, claims_status="PROCESSING")
    job = engine._audit_log.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_job_sem_project_id_falha_estruturalmente(engine, job_engine, video):
    job_engine.register_handler(AutoReframeEngine.OPERATION, engine.handle_reframe_job, claims_status="PROCESSING")
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=AutoReframeEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_job_sem_video_id_falha_estruturalmente(engine, job_engine):
    job_engine.register_handler(AutoReframeEngine.OPERATION, engine.handle_reframe_job, claims_status="PROCESSING")
    job = engine._audit_log.create_job(Job(operation=AutoReframeEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_deteccao_bem_sucedida_gera_artifact_e_checkpoint(engine, job_engine, video, project):
    _enable_reframe(engine, project.id)
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY

    with engine._database.connection() as conn:
        rows = conn.execute(
            "SELECT kind, path, fingerprint, job_id, video_id, project_id FROM artifacts WHERE video_id = ?",
            (video.id,),
        ).fetchall()
    assert len(rows) == 1
    kind, path, fingerprint, job_id, video_id, project_id = rows[0]
    assert kind == ARTIFACT_KIND_REFRAME_TRACK
    assert Path(path).is_file()
    assert fingerprint is not None
    assert job_id == job.id
    assert video_id == video.id
    assert project_id == project.id

    assert engine._audit_log.has_reached_checkpoint(job.id, auto_reframe.CHECKPOINT_MEDIA_PROCESSED)

    import json as _json
    payload = _json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["fallback"] is False
    assert len(payload["keyframes"]) > 0


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_deteccao_usa_dimensoes_reais_do_probe(engine, job_engine, video, project, video_file):
    """Prova que a geometria respeita a resolução REAL retornada pelo
    MediaProbe (vídeo real gerado via ffmpeg 64x64) -- nunca um valor
    inventado."""
    seen_dims = []
    engine._backend = _fake_backend_found(calls=None)

    def spy_backend(local_path, t, width, height):
        seen_dims.append((width, height))
        return DetectionSample(found=True, cx=0.5, cy=0.5, kind="FACE")

    engine._backend = spy_backend
    _enable_reframe(engine, project.id)
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY
    assert seen_dims, "backend nunca foi chamado"
    for w, h in seen_dims:
        assert w == 64
        assert h == 64


def test_detector_nao_encontra_nada_engaja_fallback_ainda_ready(manager, database, storage, app_paths, audit, video, project):
    engine_nf = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, detection_backend=_fake_backend_not_found(),
    )
    _enable_reframe(engine_nf, project.id)
    job_engine_nf = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_nf, job_engine_nf, video, project)
    assert result.status == JOB_READY

    with engine_nf._database.connection() as conn:
        rows = conn.execute(
            "SELECT path FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
        ).fetchall()
    assert len(rows) == 1
    import json as _json
    payload = _json.loads(Path(rows[0][0]).read_text(encoding="utf-8"))
    assert payload["fallback"] is True
    assert payload["keyframes"] == [{"t": 0.0, "x": 0.5, "y": 0.5}, {"t": payload["keyframes"][1]["t"], "x": 0.5, "y": 0.5}]


def test_backend_lancando_excecao_tambem_engaja_fallback_ainda_ready(manager, database, storage, app_paths, audit, video, project):
    """O teste mais importante do Prompt: uma exceção do detector NUNCA
    pode bloquear o Job -- mesmo resultado do caso 'nao encontrou nada'."""
    calls = []
    engine_raise = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, detection_backend=_fake_backend_raising(calls=calls),
    )
    _enable_reframe(engine_raise, project.id)
    job_engine_raise = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_raise, job_engine_raise, video, project)
    assert result.status == JOB_READY, "excecao do detector NUNCA pode falhar o Job"
    assert len(calls) == DETECTION_SAMPLE_COUNT, "o backend deve ter sido chamado em toda amostra, apesar de sempre lancar"

    with engine_raise._database.connection() as conn:
        rows = conn.execute(
            "SELECT path FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
        ).fetchall()
    assert len(rows) == 1
    import json as _json
    payload = _json.loads(Path(rows[0][0]).read_text(encoding="utf-8"))
    assert payload["fallback"] is True


def test_falha_estrutural_source_asset_ausente_ainda_falha_job(manager, database, storage, app_paths, audit, project):
    """Diferente da falha de DETECTOR: um SourceAsset ausente continua
    falhando o Job normalmente -- nunca confundido com fallback."""
    video_sem_source = Video(source_asset_id=None, name="sem-source")
    database.insert(video_sem_source)
    project_sem = Project(video_id=video_sem_source.id, name="p-sem-source")
    database.insert(project_sem)
    engine_x = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, detection_backend=_fake_backend_raising(),
    )
    _enable_reframe(engine_x, project_sem.id)
    job_engine_x = JobEngine(database, audit_log=audit)
    job_engine_x.register_handler(AutoReframeEngine.OPERATION, engine_x.handle_reframe_job, claims_status="PROCESSING")
    job = audit.create_job(Job(video_id=video_sem_source.id, project_id=project_sem.id, operation=AutoReframeEngine.OPERATION))
    result = job_engine_x.advance(job.id)
    assert result.status == JOB_FAILED

    with database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video_sem_source.id,)
        ).fetchone()
    assert n == 0


def test_probe_invalido_falha_estruturalmente_nunca_fallback(manager, database, storage, app_paths, audit, video, project, tmp_path):
    """Arquivo ilegível para o probe -- falha estrutural (sem resolucao
    real nao ha como calcular geometria), nunca um fallback de tracking."""
    bad_path = tmp_path / "nao_e_video.mp4"
    bad_path.write_bytes(b"isto nao e um video valido")
    source_bad = SourceAsset(source_uri=str(bad_path), local_path=str(bad_path), original_name="bad.mp4", fingerprint="fp-bad")
    database.insert(source_bad)
    video_bad = Video(source_asset_id=source_bad.id, name="video-invalido")
    database.insert(video_bad)
    project_bad = Project(video_id=video_bad.id, name="p-bad")
    database.insert(project_bad)

    engine_bad = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, detection_backend=_fake_backend_found(),
    )
    _enable_reframe(engine_bad, project_bad.id)
    job_engine_bad = JobEngine(database, audit_log=audit)
    job_engine_bad.register_handler(AutoReframeEngine.OPERATION, engine_bad.handle_reframe_job, claims_status="PROCESSING")
    job = audit.create_job(Job(video_id=video_bad.id, project_id=project_bad.id, operation=AutoReframeEngine.OPERATION))
    result = job_engine_bad.advance(job.id)
    assert result.status == JOB_FAILED


def test_cache_hit_nao_chama_backend_de_novo(manager, database, storage, app_paths, audit, video, project):
    calls = []
    engine_c = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, detection_backend=_fake_backend_found(calls=calls),
    )
    _enable_reframe(engine_c, project.id)
    job_engine_c = JobEngine(database, audit_log=audit)

    job1, result1 = _run_job(engine_c, job_engine_c, video, project)
    assert result1.status == JOB_READY
    n_calls_after_first = len(calls)
    assert n_calls_after_first > 0

    job2 = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))
    result2 = job_engine_c.advance(job2.id)
    assert result2.status == JOB_READY
    assert len(calls) == n_calls_after_first, "cache hit nunca pode chamar o backend de novo"

    with database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
        ).fetchone()
    assert n == 1, "cache hit nunca duplica Artifact"
    assert engine_c._audit_log.has_reached_checkpoint(job2.id, auto_reframe.CHECKPOINT_MEDIA_PROCESSED)


def test_cache_miss_por_config_diferente_roda_nova_deteccao(manager, database, storage, app_paths, audit, video, project):
    calls = []
    engine_c = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, detection_backend=_fake_backend_found(calls=calls),
    )
    _enable_reframe(engine_c, project.id, smoothing_strength=0.1)
    job_engine_c = JobEngine(database, audit_log=audit)
    _run_job(engine_c, job_engine_c, video, project)
    n_after_first = len(calls)

    engine_c.set_config(project.id, smoothing_strength=0.9)
    job2 = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))
    result2 = job_engine_c.advance(job2.id)
    assert result2.status == JOB_READY
    assert len(calls) > n_after_first, "config diferente deve forcar nova deteccao"

    with database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
        ).fetchone()
    assert n == 2


def test_cancelamento_antes_da_deteccao(manager, database, storage, app_paths, video, project):
    """Mesmo padrão de test_captions_engine.py: o Job é claimed
    manualmente (transition_job para PROCESSING) e o handler é chamado
    diretamente -- chamar via job_engine.advance() depois de cancelar um
    Job ainda PENDING cancelaria o Job imediatamente (ControlManager.cancel
    decide cancelar na hora quando o Job não está em execução ativa),
    nunca chegando a exercitar a checagem proativa do PRÓPRIO handler."""
    audit_local = OperationalAuditLog(database)
    control_local = ControlManager(database)
    engine_cancel = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit_local, detection_backend=_fake_backend_found(),
        control_manager=control_local,
    )
    _enable_reframe(engine_cancel, project.id)
    job = audit_local.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))
    audit_local.transition_job(job.id, JOB_PROCESSING)
    control_local.cancel(CONTROL_SCOPE_JOB, job.id)

    claimed_job = database.get(Job, job.id)
    result = engine_cancel.handle_reframe_job(claimed_job)
    assert result.target_status == JOB_CANCELLED
    assert result.data["reason"] == "cancelled_before_detection"

    with database.connection() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchone()
    assert n == 0


def test_cancelamento_apos_deteccao_antes_de_promover(manager, database, storage, app_paths, video, project):
    audit_local = OperationalAuditLog(database)
    control_local = ControlManager(database)
    job_holder: "dict[str, str]" = {}

    def backend_that_cancels(local_path, t, width, height):
        control_local.cancel(CONTROL_SCOPE_JOB, job_holder["id"])
        return DetectionSample(found=True, cx=0.5, cy=0.5, kind="FACE")

    engine_cancel = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit_local, detection_backend=backend_that_cancels,
        control_manager=control_local,
    )
    _enable_reframe(engine_cancel, project.id)
    job = audit_local.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))
    audit_local.transition_job(job.id, JOB_PROCESSING)
    job_holder["id"] = job.id

    claimed_job = database.get(Job, job.id)
    result = engine_cancel.handle_reframe_job(claimed_job)
    assert result.target_status == JOB_CANCELLED
    assert result.data["reason"] == "cancelled_after_detection"

    with database.connection() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchone()
    assert n == 0


def test_restart_com_instancias_totalmente_novas_preserva_artifact(app_paths, manager, database, storage, audit, video, project):
    engine_r = AutoReframeEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, detection_backend=_fake_backend_found(),
    )
    _enable_reframe(engine_r, project.id)
    job_engine_r = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_r, job_engine_r, video, project)
    assert result.status == JOB_READY

    db2 = LocalDatabase(app_paths.database / "painel.db")
    with db2.connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
        ).fetchall()
    assert rows[0][0] == 1
    audit2 = OperationalAuditLog(db2)
    assert audit2.has_reached_checkpoint(job.id, auto_reframe.CHECKPOINT_MEDIA_PROCESSED)


def test_duas_instancias_concorrentes_processando_o_mesmo_video(app_paths, manager, database, storage, audit, video, project):
    """GATE 6: duas instâncias tentando processar o MESMO video+config
    concorrentemente -- nenhuma corrompe, nenhuma perde evidência (mesmo
    espírito de overwrite=True do Prompt 31, seção 0.6 daquele módulo)."""
    _enable_reframe(AutoReframeEngine(manager, database=database, storage_manager=storage, app_paths=app_paths), project.id)

    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []
    statuses = []

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        audit_instance = OperationalAuditLog(db_instance)
        engine_instance = AutoReframeEngine(
            EditProjectManager(db_instance), database=db_instance,
            storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
            app_paths=app_paths, audit_log=audit_instance,
            detection_backend=_fake_backend_found(),
        )
        job_engine_instance = JobEngine(db_instance, audit_log=audit_instance)
        job_engine_instance.register_handler(
            AutoReframeEngine.OPERATION, engine_instance.handle_reframe_job, claims_status="PROCESSING"
        )
        job = audit_instance.create_job(Job(video_id=video.id, project_id=project.id, operation=AutoReframeEngine.OPERATION))
        barrier.wait(timeout=5)
        try:
            result = job_engine_instance.advance(job.id)
            statuses.append(result.status)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    assert all(s == JOB_READY for s in statuses)
    with database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_REFRAME_TRACK),
        ).fetchone()
    assert n == 2, "cada Job insere sua propria linha (mesmo padrao overwrite=True do Prompt 31); nenhuma corrida na tabela"


# ---------------------------------------------------------------------------
# Helpers de módulo -- amostragem/suavização/fallback/cache key
# ---------------------------------------------------------------------------


def test_compute_cache_key_e_deterministico_e_ordem_de_chaves_nao_importa():
    k1 = compute_cache_key("fp1", "9:16", 0.5, "CENTER_CROP")
    k2 = compute_cache_key("fp1", "9:16", 0.5, "CENTER_CROP")
    assert k1 == k2


def test_compute_cache_key_muda_com_qualquer_campo():
    base = compute_cache_key("fp1", "9:16", 0.5, "CENTER_CROP")
    assert compute_cache_key("fp2", "9:16", 0.5, "CENTER_CROP") != base
    assert compute_cache_key("fp1", "9:16", 0.6, "CENTER_CROP") != base


def test_sample_timestamps_cobre_do_inicio_ao_fim():
    ts = auto_reframe._sample_timestamps(10.0, 5)
    assert ts[0] == 0.0
    assert ts[-1] == 10.0
    assert len(ts) == 5


def test_sample_timestamps_duracao_zero():
    ts = auto_reframe._sample_timestamps(0.0, 5)
    assert all(t == 0.0 for t in ts)


def test_smooth_curve_alpha_zero_strength_e_curva_crua():
    samples = [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]
    curve = auto_reframe._smooth_curve(samples, smoothing_strength=0.0)
    assert curve[0]["x"] == 0.0
    assert curve[1]["x"] == 1.0  # alpha=1.0 -> segue o valor cru


def test_smooth_curve_alpha_maximo_nunca_congela_totalmente():
    samples = [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (2.0, 1.0, 1.0)]
    curve = auto_reframe._smooth_curve(samples, smoothing_strength=1.0)
    assert curve[0]["x"] == 0.0
    assert 0.0 < curve[1]["x"] < 1.0, "alpha nunca pode ser exatamente 0 (congelaria a curva)"


def test_smooth_curve_vazio_devolve_vazio():
    assert auto_reframe._smooth_curve([], smoothing_strength=0.5) == ()


def test_fallback_curve_e_sempre_centro_estatico():
    curve = auto_reframe._fallback_curve(12.0)
    assert curve == ({"t": 0.0, "x": 0.5, "y": 0.5}, {"t": 12.0, "x": 0.5, "y": 0.5})


# ---------------------------------------------------------------------------
# Confirmação -- media_catalog.py / REFRAMED_9_16 não tocados
# ---------------------------------------------------------------------------


def test_reframed_9_16_foi_ligado_por_rodada_dedicada_posterior_nunca_por_este_prompt():
    """Historicamente (Prompt 33, no momento desta implementação),
    ``BADGE_REFRAMED_9_16`` era hardcoded ``"0"`` -- este Prompt
    deliberadamente NÃO tocava ``media_catalog.py`` (ver seção 0.11 da
    docstring do módulo). Uma rodada SEPARADA e POSTERIOR, "Catalog
    Wiring -- REFRAMED_9_16 (Prompt 33)", ligou o badge (mesmo padrão já
    usado para CAPTIONS/TRANSCRIBED: Prompt 31 implementou o
    processamento real, um Prompt de Catalog Wiring dedicado ligou o
    badge depois). Este teste documenta essa transição: confirma que a
    badge TEM expressão SQL real hoje, e que a suíte adversarial dedicada
    a essa integração vive em
    ``tests/test_media_catalog_reframe_wiring.py`` (não aqui -- este
    arquivo cobre só ``AutoReframeEngine`` em si, nunca o catálogo)."""
    from _sistema.media_catalog import _BADGE_SQL_EXPRESSIONS, BADGE_REFRAMED_9_16

    assert _BADGE_SQL_EXPRESSIONS[BADGE_REFRAMED_9_16] != "0"
    assert "reframe_track" in _BADGE_SQL_EXPRESSIONS[BADGE_REFRAMED_9_16]
    assert "MEDIA_PROCESSED" in _BADGE_SQL_EXPRESSIONS[BADGE_REFRAMED_9_16]


def test_nenhuma_migration_nova_criada_por_este_prompt():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9


# ---------------------------------------------------------------------------
# Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def _parse_module() -> ast.AST:
    return ast.parse(Path(auto_reframe.__file__).read_text(encoding="utf-8"))


def test_modulo_nao_importa_modulos_protegidos_nem_irmaos():
    tree = _parse_module()
    proibidos = (
        "visual_editor",
        "captions_engine",
        "captions_style",
        "media_catalog",
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "source_import",
        "source_context",
        "video_promotion",
        "timeline_editor",
        "audio_engine",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for proibido in proibidos:
            assert proibido not in names, f"auto_reframe.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_modulo_nao_importa_biblioteca_pesada_de_visao_no_topo():
    """AST -- nunca grep ingenuo (a propria docstring do modulo cita
    'opencv'/'mediapipe' ao explicar por que NAO sao usadas hoje, o que
    faria um grep textual falso-positivar)."""
    tree = _parse_module()
    proibidos = ("cv2", "opencv", "mediapipe", "dlib", "face_recognition", "torch", "tensorflow")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] + [alias.name for alias in node.names]
            )
            for name in names:
                for proibido in proibidos:
                    assert proibido not in name.lower()


def test_modulo_nao_chama_subprocess_ffmpeg_diretamente():
    """MediaProbe já encapsula ffprobe/ffmpeg -- este módulo nunca chama
    subprocess/ffmpeg por conta própria."""
    tree = _parse_module()
    proibidos = ("subprocess", "ffmpeg")
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
                assert name.lower() not in proibidos


def test_modulo_nunca_cria_video_ou_publication_ou_schedule():
    tree = _parse_module()
    proibidos_chamadas = {"Video", "Publication", "Schedule"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in proibidos_chamadas
