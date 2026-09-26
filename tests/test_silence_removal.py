# -*- coding: utf-8 -*-
"""PROMPT 34 -- SilenceRemoval: testes.

Cobre a Parte A (configuração ``SILENCE_REMOVAL`` -- mesma disciplina de
fingerprint/concorrência/restart/idempotência dos Prompts 27-33) e a Parte
B (detecção real via backend injetável + aplicação de cortes via a API
pública de ``TimelineEditor``, nunca reimplementada): padding aplicado
corretamente; silêncio mais curto que ``minimum_duration`` nunca removido;
threshold conservador nunca corta fala; cortes aplicados batem com
``TimelineEditor.get_timeline``; falha/exceção do backend nunca falha o
Job (fallback trivial = nenhum corte); falhas ESTRUTURAIS continuam
falhando o Job; cache hit/miss; idempotência (rodar duas vezes não muda o
resultado); cancelamento; concorrência real; restart; testes de
SINCRONIZAÇÃO com captions (exigidos literalmente pelo roadmap); AST
estrutural (``timeline_editor`` é a ÚNICA exceção permitida entre os
módulos irmãos); confirmação de que nenhuma migration nova foi criada.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import _sistema.silence_removal as silence_removal
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.captions_engine import TranscriptionSegment
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
from _sistema.edit_project import CAPTIONS, EditProjectManager, SPEED
from _sistema.job_engine import JobEngine
from _sistema.media_probe import MediaProbe
from _sistema.silence_removal import (
    ARTIFACT_KIND_SILENCE_TRACK,
    CampoInvalidoError,
    DEFAULT_MINIMUM_DURATION_SECONDS,
    DEFAULT_PADDING_SECONDS,
    DEFAULT_THRESHOLD_DB,
    SILENCE_REMOVAL,
    SilenceRemovalConfigState,
    SilenceRemovalEngine,
    compute_cache_key,
)
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.timeline_editor import TimelineEditor


VIDEO_DURATION = 20.0


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_auto_reframe.py/test_captions_engine.py)
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
def timeline(database):
    return TimelineEditor(database)


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "original.mp4"
    path.write_bytes(b"fake video bytes -- probe is faked via injected media_probe")
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


class _FakeProbeResult:
    def __init__(self, *, valid=True, duration=VIDEO_DURATION, error_code=None):
        self.valid = valid
        self.duration = duration
        self.error_code = error_code


class _FakeMediaProbe(MediaProbe):
    """Substitui MediaProbe real -- nunca lê o arquivo de verdade (os
    testes deste módulo não precisam de ffmpeg real; a integração
    genuína com MediaProbe já foi provada em test_auto_reframe.py/
    test_media_probe.py). Subclasse real (não um duck-type solto) para
    satisfazer a validação de tipo do construtor de
    SilenceRemovalEngine, mesmo padrão de qualquer outro teste que
    precise injetar um dublê de uma dependência validada por isinstance."""

    def __init__(self, *, duration=VIDEO_DURATION, valid=True):
        super().__init__()
        self._duration = duration
        self._valid = valid
        self.calls: "list[str]" = []

    def probe(self, path):
        self.calls.append(str(path))
        if not self._valid:
            return _FakeProbeResult(valid=False, duration=None, error_code="INVALIDO")
        return _FakeProbeResult(valid=True, duration=self._duration)


def _fake_backend(intervals, calls=None):
    def backend(local_path, duration):
        if calls is not None:
            calls.append((local_path, duration))
        return tuple(intervals)

    return backend


def _fake_backend_raising(calls=None):
    def backend(local_path, duration):
        if calls is not None:
            calls.append((local_path, duration))
        raise RuntimeError("falha simulada do detector de silencio")

    return backend


@pytest.fixture
def probe(request):
    return _FakeMediaProbe()


@pytest.fixture
def engine(manager, database, storage, app_paths, audit, timeline, probe):
    return SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)]),
    )


@pytest.fixture
def job_engine(database, audit):
    return JobEngine(database, audit_log=audit)


def _set_conservative(engine_, project_id, **overrides):
    fields = dict(threshold_db=-45.0, minimum_duration_seconds=0.8, padding_seconds=0.1)
    fields.update(overrides)
    return engine_.set_config(project_id, **fields)


def _run_job(engine_, job_engine_, video_, project_):
    """Roda o Job através do JobEngine real -- devolve (Job, Job
    atualizado). ``Job`` não carrega o ``JobStepResult.data`` do handler
    (só ``status``); use ``_run_job_direct`` quando o teste precisar
    inspecionar ``removed_intervals``/``skipped_intervals``/etc."""
    job_engine_.register_handler(
        SilenceRemovalEngine.OPERATION, engine_.handle_silence_removal_job, claims_status="PROCESSING"
    )
    job = engine_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=SilenceRemovalEngine.OPERATION)
    )
    result = job_engine_.advance(job.id)
    return job, result


def _run_job_direct(engine_, audit_, video_, project_):
    """Reivindica o Job manualmente (transition_job para PROCESSING) e
    chama o handler DIRETAMENTE -- devolve (Job reivindicado,
    JobStepResult completo, com ``.data``). Mesmo padrão dos testes de
    cancelamento de test_auto_reframe.py/test_captions_engine.py."""
    job = audit_.create_job(Job(video_id=video_.id, project_id=project_.id, operation=SilenceRemovalEngine.OPERATION))
    audit_.transition_job(job.id, JOB_PROCESSING)
    claimed_job = engine_._database.get(Job, job.id)
    result = engine_.handle_silence_removal_job(claimed_job)
    return claimed_job, result


# ===========================================================================
# 0. Construção
# ===========================================================================


def test_construtor_rejeita_manager_de_tipo_errado(database, storage, app_paths):
    with pytest.raises(TypeError):
        SilenceRemovalEngine("nao-e-um-manager", database=database, storage_manager=storage, app_paths=app_paths)


def test_construtor_rejeita_database_de_tipo_errado(manager, storage, app_paths):
    with pytest.raises(TypeError):
        SilenceRemovalEngine(manager, database="nao-e-um-database", storage_manager=storage, app_paths=app_paths)


def test_construtor_rejeita_storage_manager_de_tipo_errado(manager, database, app_paths):
    with pytest.raises(TypeError):
        SilenceRemovalEngine(manager, database=database, storage_manager="nao-e-storage", app_paths=app_paths)


def test_construtor_rejeita_app_paths_de_tipo_errado(manager, database, storage):
    with pytest.raises(TypeError):
        SilenceRemovalEngine(manager, database=database, storage_manager=storage, app_paths="nao-e-app-paths")


def test_construtor_rejeita_timeline_editor_de_tipo_errado(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        SilenceRemovalEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            timeline_editor="nao-e-timeline-editor",
        )


def test_construtor_rejeita_backend_nao_chamavel(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        SilenceRemovalEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            silence_detection_backend="nao-e-chamavel",
        )


def test_construtor_rejeita_control_manager_de_tipo_errado(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        SilenceRemovalEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            control_manager="nao-e-control-manager",
        )


def test_construtor_rejeita_media_probe_de_tipo_errado(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        SilenceRemovalEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            media_probe="nao-e-media-probe",
        )


def test_construtor_aceita_sem_timeline_editor_explicito(manager, database, storage, app_paths):
    engine_default = SilenceRemovalEngine(manager, database=database, storage_manager=storage, app_paths=app_paths)
    assert isinstance(engine_default._timeline, TimelineEditor)


# ===========================================================================
# PARTE A -- configuração
# ===========================================================================


def test_estado_inicial_e_todos_none(engine, project):
    assert engine.get_config(project.id) == SilenceRemovalConfigState()


def test_set_config_grava_todos_os_campos(engine, project):
    state = _set_conservative(engine, project.id)
    assert state.threshold_db == -45.0
    assert state.minimum_duration_seconds == 0.8
    assert state.padding_seconds == 0.1
    assert engine.get_config(project.id) == state


def test_set_config_parcial_nao_apaga_outros_campos(engine, project):
    _set_conservative(engine, project.id)
    engine.set_config(project.id, padding_seconds=0.3)
    state = engine.get_config(project.id)
    assert state.threshold_db == -45.0
    assert state.padding_seconds == 0.3
    assert state.minimum_duration_seconds == 0.8


@pytest.mark.parametrize("value", [-9.9, -90.1, float("inf"), float("-inf"), float("nan"), "‑45", True])
def test_threshold_db_range_e_tipo(engine, project, value):
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, threshold_db=value)


@pytest.mark.parametrize("value", [-90.0, -10.0, -45.0])
def test_threshold_db_limites_validos(engine, project, value):
    state = engine.set_config(project.id, threshold_db=value)
    assert state.threshold_db == value


@pytest.mark.parametrize("value", [0.0, 0.04, 30.1, float("inf"), float("nan"), "1", True])
def test_minimum_duration_seconds_range_e_tipo(engine, project, value):
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, minimum_duration_seconds=value)


@pytest.mark.parametrize("value", [0.05, 30.0, 0.8])
def test_minimum_duration_seconds_limites_validos(engine, project, value):
    state = engine.set_config(project.id, minimum_duration_seconds=value)
    assert state.minimum_duration_seconds == value


@pytest.mark.parametrize("value", [-0.1, 5.1, float("inf"), float("nan"), "0.1", True])
def test_padding_seconds_range_e_tipo(engine, project, value):
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, padding_seconds=value)


@pytest.mark.parametrize("value", [0.0, 5.0, 0.1])
def test_padding_seconds_limites_validos(engine, project, value):
    state = engine.set_config(project.id, padding_seconds=value)
    assert state.padding_seconds == value


def test_campo_desconhecido_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, campo_que_nao_existe=1)


def test_update_category_e_usado_nunca_set_category_direto(engine, project):
    _set_conservative(engine, project.id)
    engine.set_config(project.id, threshold_db=-30.0)
    state = engine.get_config(project.id)
    assert state.threshold_db == -30.0
    assert state.padding_seconds == 0.1


def test_categoria_silence_removal_isolada_de_captions_e_speed(database, engine, project, manager):
    _set_conservative(engine, project.id)
    manager.set_category(project.id, CAPTIONS, {"mode": "OFF"})
    manager.set_category(project.id, SPEED, {"x": 1})
    categorias = manager.list_categories(project.id)
    assert SILENCE_REMOVAL in categorias
    assert CAPTIONS in categorias
    assert SPEED in categorias


def test_alterar_silence_removal_nao_muda_fingerprint_de_captions(database, engine, project, manager):
    manager.set_category(project.id, CAPTIONS, {"mode": "OFF"})
    before = manager.get_category(project.id, CAPTIONS)
    _set_conservative(engine, project.id)
    after = manager.get_category(project.id, CAPTIONS)
    assert before.fingerprint == after.fingerprint


def test_estado_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    engine_a = SilenceRemovalEngine(
        EditProjectManager(db_a), database=db_a,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    _set_conservative(engine_a, project.id)

    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    engine_b = SilenceRemovalEngine(
        EditProjectManager(db_b), database=db_b,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    assert engine_b.get_config(project.id).threshold_db == -45.0


def test_repetir_o_mesmo_set_config_sequencialmente_nao_acumula(engine, project, manager):
    _set_conservative(engine, project.id)
    before = manager.get_category(project.id, SILENCE_REMOVAL)
    _set_conservative(engine, project.id)
    after = manager.get_category(project.id, SILENCE_REMOVAL)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_duas_operacoes_concorrentes_de_config_em_projetos_diferentes(app_paths, database, project, project2):
    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []

    def worker(project_id, threshold):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_engine = SilenceRemovalEngine(
            EditProjectManager(db_instance), database=db_instance,
            storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
            app_paths=app_paths,
        )
        barrier.wait(timeout=5)
        try:
            local_engine.set_config(project_id, threshold_db=threshold)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(project.id, -30.0)),
        threading.Thread(target=worker, args=(project2.id, -60.0)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    reread = SilenceRemovalEngine(
        EditProjectManager(database), database=database,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    assert reread.get_config(project.id).threshold_db == -30.0
    assert reread.get_config(project2.id).threshold_db == -60.0


def test_set_config_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.set_config("nao-e-um-uuid", threshold_db=-40.0)


def test_get_config_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.get_config("nao-e-um-uuid")


# ===========================================================================
# PARTE B -- pipeline completo via JobEngine
# ===========================================================================


def test_job_sem_video_id_falha_estruturalmente(engine, job_engine):
    job_engine.register_handler(SilenceRemovalEngine.OPERATION, engine.handle_silence_removal_job, claims_status="PROCESSING")
    job = engine._audit_log.create_job(Job(operation=SilenceRemovalEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_job_sem_project_id_falha_estruturalmente(engine, job_engine, video):
    job_engine.register_handler(SilenceRemovalEngine.OPERATION, engine.handle_silence_removal_job, claims_status="PROCESSING")
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=SilenceRemovalEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_get_config_com_project_inexistente_propaga_erro_estruturado(engine):
    with pytest.raises(Exception):
        engine.get_config("00000000-0000-0000-0000-000000000000")


def test_falha_estrutural_source_asset_ausente_ainda_falha_job(manager, database, storage, app_paths, audit, timeline):
    video_sem_source = Video(source_asset_id=None, name="sem-source")
    database.insert(video_sem_source)
    project_sem = Project(video_id=video_sem_source.id, name="p-sem-source")
    database.insert(project_sem)
    engine_x = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, silence_detection_backend=_fake_backend_raising(),
    )
    _set_conservative(engine_x, project_sem.id)
    job_engine_x = JobEngine(database, audit_log=audit)
    job_engine_x.register_handler(SilenceRemovalEngine.OPERATION, engine_x.handle_silence_removal_job, claims_status="PROCESSING")
    job = audit.create_job(Job(video_id=video_sem_source.id, project_id=project_sem.id, operation=SilenceRemovalEngine.OPERATION))
    result = job_engine_x.advance(job.id)
    assert result.status == JOB_FAILED

    with database.connection() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video_sem_source.id,)).fetchone()
    assert n == 0


def test_probe_invalido_falha_estruturalmente_nunca_fallback(manager, database, storage, app_paths, audit, timeline, video, project):
    probe_bad = _FakeMediaProbe(valid=False)
    engine_bad = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe_bad,
        silence_detection_backend=_fake_backend([(5.0, 7.0)]),
    )
    _set_conservative(engine_bad, project.id)
    job_engine_bad = JobEngine(database, audit_log=audit)
    job_engine_bad.register_handler(SilenceRemovalEngine.OPERATION, engine_bad.handle_silence_removal_job, claims_status="PROCESSING")
    job = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=SilenceRemovalEngine.OPERATION))
    result = job_engine_bad.advance(job.id)
    assert result.status == JOB_FAILED


def test_deteccao_bem_sucedida_gera_artifact_checkpoint_e_corte_real(engine, job_engine, video, project):
    _set_conservative(engine, project.id)
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY

    with engine._database.connection() as conn:
        rows = conn.execute(
            "SELECT kind, path, fingerprint, job_id, video_id, project_id FROM artifacts WHERE video_id = ?",
            (video.id,),
        ).fetchall()
    assert len(rows) == 1
    kind, path, fingerprint, job_id, video_id, project_id = rows[0]
    assert kind == ARTIFACT_KIND_SILENCE_TRACK
    assert Path(path).is_file()
    assert fingerprint is not None
    assert job_id == job.id
    assert video_id == video.id
    assert project_id == project.id

    assert engine._audit_log.has_reached_checkpoint(job.id, silence_removal.CHECKPOINT_EDIT_PLANNED)

    timeline_state = engine._timeline.get_timeline(project.id)
    # backend devolveu (5.0, 7.0); padding=0.1 -> corte real [5.1, 6.9)
    covered = [(s.start, s.end) for s in timeline_state]
    assert covered == [(0.0, 5.1), (6.9, VIDEO_DURATION)]


def test_padding_encolhe_o_intervalo_nas_duas_pontas(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    engine_p = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(10.0, 12.0)]),
    )
    _set_conservative(engine_p, project.id, padding_seconds=0.5)
    job, result = _run_job_direct(engine_p, audit, video, project)
    assert result.target_status == JOB_READY
    assert result.data["removed_intervals"] == [(10.5, 11.5)]


def test_silencio_mais_curto_que_minimum_duration_nunca_e_removido(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    engine_short = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 5.3)]),  # 0.3s
    )
    _set_conservative(engine_short, project.id, minimum_duration_seconds=0.8)
    job, result = _run_job_direct(engine_short, audit, video, project)
    assert result.target_status == JOB_READY
    assert result.data["removed_intervals"] == []
    timeline_state = engine_short._timeline.get_timeline(project.id)
    assert timeline_state == (timeline_state[0],)
    assert timeline_state[0].start == 0.0
    assert timeline_state[0].end == VIDEO_DURATION


def test_threshold_conservador_nao_corta_fala_normal(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    """Simula um backend que respeita um threshold conservador -- um
    trecho de "fala baixa" (acima do threshold, portanto não é silêncio
    para esse backend) nunca é devolvido como intervalo candidato."""

    def conservative_backend(local_path, duration):
        # Este backend só reporta como "silêncio" trechos abaixo de um
        # limiar (simulado); um trecho de fala baixa nunca aparece aqui.
        return ()  # nenhum trecho é silencioso o bastante

    engine_thr = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=conservative_backend,
    )
    _set_conservative(engine_thr, project.id, threshold_db=-50.0)
    job, result = _run_job_direct(engine_thr, audit, video, project)
    assert result.target_status == JOB_READY
    assert result.data["removed_intervals"] == []


def test_backend_lancando_excecao_nunca_falha_o_job(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    """O teste mais importante do Prompt (mesmo espírito do Prompt 33):
    uma exceção do detector de silêncio NUNCA pode bloquear o Job --
    resultado equivalente a "nenhum silêncio encontrado"."""
    calls = []
    engine_raise = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend_raising(calls=calls),
    )
    _set_conservative(engine_raise, project.id)
    job, result = _run_job_direct(engine_raise, audit, video, project)
    assert result.target_status == JOB_READY, "excecao do detector de silencio NUNCA pode falhar o Job"
    assert len(calls) == 1
    assert result.data["removed_intervals"] == []
    timeline_state = engine_raise._timeline.get_timeline(project.id)
    assert len(timeline_state) == 1
    assert timeline_state[0].start == 0.0 and timeline_state[0].end == VIDEO_DURATION


def test_cortes_aplicados_batem_com_get_timeline(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    engine_multi = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(2.0, 3.0), (10.0, 11.5), (17.0, 18.0)]),
    )
    _set_conservative(engine_multi, project.id, padding_seconds=0.0, minimum_duration_seconds=0.5)
    job_engine_multi = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_multi, job_engine_multi, video, project)
    assert result.status == JOB_READY

    segments = engine_multi._timeline.get_timeline(project.id)
    covered = [(s.start, s.end) for s in segments]
    assert covered == [(0.0, 2.0), (3.0, 10.0), (11.5, 17.0), (18.0, VIDEO_DURATION)]


def test_timeline_ja_inicializada_manualmente_e_reaproveitada(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    timeline.initialize_timeline(project.id, duration_seconds=VIDEO_DURATION)
    engine_pre = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)]),
    )
    _set_conservative(engine_pre, project.id)
    job_engine_pre = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_pre, job_engine_pre, video, project)
    assert result.status == JOB_READY
    assert len(probe.calls) == 1, (
        "MediaProbe e chamado exatamente uma vez (para obter a duracao usada na deteccao) -- "
        "nunca uma segunda vez so para inicializar a timeline, que ja existia"
    )


def test_cache_hit_nao_chama_backend_de_novo(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    calls = []
    engine_c = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)], calls=calls),
    )
    _set_conservative(engine_c, project.id)
    job_engine_c = JobEngine(database, audit_log=audit)

    job1, result1 = _run_job(engine_c, job_engine_c, video, project)
    assert result1.status == JOB_READY
    n_calls_after_first = len(calls)
    assert n_calls_after_first == 1

    job2 = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=SilenceRemovalEngine.OPERATION))
    result2 = job_engine_c.advance(job2.id)
    assert result2.status == JOB_READY
    assert len(calls) == n_calls_after_first, "cache hit nunca pode chamar o backend de novo"

    with database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_SILENCE_TRACK),
        ).fetchone()
    assert n == 1, "cache hit nunca duplica Artifact"
    assert engine_c._audit_log.has_reached_checkpoint(job2.id, silence_removal.CHECKPOINT_EDIT_PLANNED)


def test_cache_hit_e_idempotente_nao_muda_cuts_na_segunda_execucao(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    """Rodar o Job duas vezes (mesmo projeto/config): a segunda execução
    reaplica os MESMOS intervalos candidatos, mas todos já foram
    consumidos -- resultado deve ser 'pulado', nunca duplicar/corromper o
    corte (idempotência, GATE ADVERSARIAL item 5)."""
    engine_i = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)]),
    )
    _set_conservative(engine_i, project.id)

    _run_job_direct(engine_i, audit, video, project)
    timeline_after_first = engine_i._timeline.get_timeline(project.id)

    _job2, result2 = _run_job_direct(engine_i, audit, video, project)
    assert result2.target_status == JOB_READY
    assert result2.data["removed_intervals"] == [], "intervalo ja consumido deve ser pulado, nunca reaplicado"
    assert result2.data["skipped_intervals"][0]["reason"] == "not_contained_in_single_segment"

    timeline_after_second = engine_i._timeline.get_timeline(project.id)
    assert timeline_after_first == timeline_after_second


def test_cache_miss_por_config_diferente_roda_nova_deteccao(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    calls = []
    engine_c = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)], calls=calls),
    )
    _set_conservative(engine_c, project.id, padding_seconds=0.1)
    job_engine_c = JobEngine(database, audit_log=audit)
    _run_job(engine_c, job_engine_c, video, project)
    n_after_first = len(calls)

    engine_c.set_config(project.id, padding_seconds=0.3)
    job2 = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=SilenceRemovalEngine.OPERATION))
    result2 = job_engine_c.advance(job2.id)
    assert result2.status == JOB_READY
    assert len(calls) > n_after_first, "config diferente deve forcar nova deteccao"

    with database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_SILENCE_TRACK),
        ).fetchone()
    assert n == 2


def test_cancelamento_antes_da_deteccao(manager, database, storage, app_paths, timeline, probe, video, project):
    audit_local = OperationalAuditLog(database)
    control_local = ControlManager(database)
    engine_cancel = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit_local, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)]),
        control_manager=control_local,
    )
    _set_conservative(engine_cancel, project.id)
    job = audit_local.create_job(Job(video_id=video.id, project_id=project.id, operation=SilenceRemovalEngine.OPERATION))
    audit_local.transition_job(job.id, JOB_PROCESSING)
    control_local.cancel(CONTROL_SCOPE_JOB, job.id)

    claimed_job = database.get(Job, job.id)
    result = engine_cancel.handle_silence_removal_job(claimed_job)
    assert result.target_status == JOB_CANCELLED
    assert result.data["reason"] == "cancelled_before_detection"

    with database.connection() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchone()
    assert n == 0


def test_cancelamento_apos_deteccao_antes_de_aplicar_cortes(manager, database, storage, app_paths, timeline, probe, video, project):
    audit_local = OperationalAuditLog(database)
    control_local = ControlManager(database)
    job_holder: "dict[str, str]" = {}

    def backend_that_cancels(local_path, duration):
        control_local.cancel(CONTROL_SCOPE_JOB, job_holder["id"])
        return ((5.0, 7.0),)

    engine_cancel = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit_local, media_probe=probe,
        silence_detection_backend=backend_that_cancels,
        control_manager=control_local,
    )
    _set_conservative(engine_cancel, project.id)
    job = audit_local.create_job(Job(video_id=video.id, project_id=project.id, operation=SilenceRemovalEngine.OPERATION))
    audit_local.transition_job(job.id, JOB_PROCESSING)
    job_holder["id"] = job.id

    claimed_job = database.get(Job, job.id)
    result = engine_cancel.handle_silence_removal_job(claimed_job)
    assert result.target_status == JOB_CANCELLED
    assert result.data["reason"] == "cancelled_after_detection"

    with database.connection() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchone()
    assert n == 0, "cancelamento apos deteccao mas antes de aplicar cortes nunca deve publicar o Artifact de cache"

    timeline_state = engine_cancel._timeline.get_timeline(project.id)
    assert timeline_state == (), "nenhum corte deve ter sido aplicado apos o cancelamento"


def test_restart_com_instancias_totalmente_novas_preserva_artifact_e_cortes(app_paths, manager, database, storage, audit, timeline, probe, video, project):
    engine_r = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)]),
    )
    _set_conservative(engine_r, project.id)
    job_engine_r = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_r, job_engine_r, video, project)
    assert result.status == JOB_READY

    db2 = LocalDatabase(app_paths.database / "painel.db")
    with db2.connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_SILENCE_TRACK),
        ).fetchall()
    assert rows[0][0] == 1
    audit2 = OperationalAuditLog(db2)
    assert audit2.has_reached_checkpoint(job.id, silence_removal.CHECKPOINT_EDIT_PLANNED)
    timeline2 = TimelineEditor(db2)
    assert [(s.start, s.end) for s in timeline2.get_timeline(project.id)] == [(0.0, 5.1), (6.9, VIDEO_DURATION)]


def test_duas_instancias_concorrentes_processando_o_mesmo_video(app_paths, manager, database, storage, audit, video, project):
    """GATE 6: duas instâncias tentando aplicar o MESMO corte no MESMO
    projeto concorrentemente -- nenhuma corrompe, nenhuma falha o Job;
    exatamente um dos dois efetivamente remove o intervalo, o outro cai
    no caminho 'pulado' (race_condition ou já removido)."""
    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []
    statuses = []

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        audit_instance = OperationalAuditLog(db_instance)
        timeline_instance = TimelineEditor(db_instance)
        probe_instance = _FakeMediaProbe()
        engine_instance = SilenceRemovalEngine(
            EditProjectManager(db_instance), database=db_instance,
            storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
            app_paths=app_paths, timeline_editor=timeline_instance, audit_log=audit_instance,
            media_probe=probe_instance, silence_detection_backend=_fake_backend([(5.0, 7.0)]),
        )
        job_engine_instance = JobEngine(db_instance, audit_log=audit_instance)
        job_engine_instance.register_handler(
            SilenceRemovalEngine.OPERATION, engine_instance.handle_silence_removal_job, claims_status="PROCESSING"
        )
        job = audit_instance.create_job(Job(video_id=video.id, project_id=project.id, operation=SilenceRemovalEngine.OPERATION))
        barrier.wait(timeout=5)
        try:
            result = job_engine_instance.advance(job.id)
            statuses.append(result.status)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    _set_conservative(SilenceRemovalEngine(manager, database=database, storage_manager=storage, app_paths=app_paths), project.id)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    assert all(s == JOB_READY for s in statuses)

    timeline_final = TimelineEditor(database)
    assert [(s.start, s.end) for s in timeline_final.get_timeline(project.id)] == [(0.0, 5.1), (6.9, VIDEO_DURATION)]


# ===========================================================================
# TESTES DE SINCRONIZAÇÃO (exigidos literalmente pelo roadmap, seção 0.10)
# ===========================================================================


def test_sincronizacao_silencio_fora_de_qualquer_legenda_nao_afeta_legendas(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    """Legendas conhecidas em [0.0, 4.0] e [8.0, 12.0]; o silêncio
    detectado/removido é [5.0, 7.0] -- fora de qualquer legenda. Prova:
    os objetos de legenda permanecem byte-a-byte idênticos, e nenhum
    segmento restante da timeline precisa de remapeamento para que as
    legendas continuem válidas no MESMO timestamp original."""
    captions_antes = (
        TranscriptionSegment(start=0.0, end=4.0, text="ola mundo"),
        TranscriptionSegment(start=8.0, end=12.0, text="segunda frase"),
    )

    engine_sync = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)]),
    )
    _set_conservative(engine_sync, project.id, padding_seconds=0.0)
    job_engine_sync = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_sync, job_engine_sync, video, project)
    assert result.status == JOB_READY

    captions_depois = (
        TranscriptionSegment(start=0.0, end=4.0, text="ola mundo"),
        TranscriptionSegment(start=8.0, end=12.0, text="segunda frase"),
    )
    assert captions_antes == captions_depois, "este modulo nunca toca artefatos de captions"

    segments = engine_sync._timeline.get_timeline(project.id)
    for caption in captions_antes:
        covering = [s for s in segments if s.start <= caption.start and caption.end <= s.end]
        assert len(covering) == 1, (
            f"legenda [{caption.start},{caption.end}] deveria continuar totalmente coberta por "
            f"um unico segmento remanescente (sem overlap com o silencio removido)"
        )


def test_sincronizacao_silencio_intersecta_legenda_nunca_trunca_a_legenda(manager, database, storage, app_paths, audit, timeline, probe, video, project):
    """Caso adversarial: o silêncio detectado [5.0, 7.0] INTERSECTA
    parcialmente uma legenda [6.0, 9.0] (só pode acontecer se o detector
    encontrar uma pausa longa DENTRO de um trecho transcrito). Decisão
    documentada (seção 0.10): SilenceRemoval NUNCA consulta nem depende
    de captions -- o corte é aplicado normalmente, e o objeto de legenda
    permanece EXATAMENTE como estava (nenhum truncamento é feito por este
    módulo; reconciliar a reprodução é responsabilidade de uma etapa de
    render futura)."""
    caption_antes = TranscriptionSegment(start=6.0, end=9.0, text="frase com pausa longa no meio")

    engine_sync = SilenceRemovalEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        timeline_editor=timeline, audit_log=audit, media_probe=probe,
        silence_detection_backend=_fake_backend([(5.0, 7.0)]),
    )
    _set_conservative(engine_sync, project.id, padding_seconds=0.0)
    job, result = _run_job_direct(engine_sync, audit, video, project)
    assert result.target_status == JOB_READY
    assert result.data["removed_intervals"] == [(5.0, 7.0)], "o corte e aplicado normalmente, sem considerar captions"

    caption_depois = TranscriptionSegment(start=6.0, end=9.0, text="frase com pausa longa no meio")
    assert caption_antes == caption_depois, "a legenda nunca e truncada/remapeada por este modulo"

    segments = engine_sync._timeline.get_timeline(project.id)
    covering = [s for s in segments if s.start <= caption_antes.start and caption_antes.end <= s.end]
    assert covering == [], (
        "documentado: uma legenda que intersecta um corte real deixa de ser coberta por um "
        "unico segmento -- reconciliacao e responsabilidade de uma etapa de renderizacao futura"
    )


# ---------------------------------------------------------------------------
# Helpers de módulo -- pipeline de intervalos / cache key
# ---------------------------------------------------------------------------


def test_compute_cache_key_e_deterministico_e_ordem_de_chaves_nao_importa():
    k1 = compute_cache_key("fp1", -45.0, 0.8, 0.1)
    k2 = compute_cache_key("fp1", -45.0, 0.8, 0.1)
    assert k1 == k2


def test_compute_cache_key_muda_com_qualquer_campo():
    base = compute_cache_key("fp1", -45.0, 0.8, 0.1)
    assert compute_cache_key("fp2", -45.0, 0.8, 0.1) != base
    assert compute_cache_key("fp1", -40.0, 0.8, 0.1) != base
    assert compute_cache_key("fp1", -45.0, 0.9, 0.1) != base
    assert compute_cache_key("fp1", -45.0, 0.8, 0.2) != base


def test_sanitize_and_clip_descarta_intervalos_invalidos():
    raw = ((1.0, 1.0), (2.0, 1.0), (float("nan"), 5.0), ("x", "y"), (3.0,), (5.0, 6.0))
    result = silence_removal._sanitize_and_clip(raw, duration=10.0)
    assert result == [(5.0, 6.0)]


def test_sanitize_and_clip_recorta_para_duracao():
    result = silence_removal._sanitize_and_clip(((-2.0, 3.0), (8.0, 15.0)), duration=10.0)
    assert result == [(0.0, 3.0), (8.0, 10.0)]


def test_apply_minimum_duration_and_padding_filtra_curtos():
    result = silence_removal._apply_minimum_duration_and_padding(
        [(0.0, 0.3), (5.0, 7.0)], minimum_duration_seconds=0.8, padding_seconds=0.1
    )
    assert result == [(5.1, 6.9)]


def test_apply_minimum_duration_and_padding_descarta_quando_padding_consome_tudo():
    result = silence_removal._apply_minimum_duration_and_padding(
        [(5.0, 5.9)], minimum_duration_seconds=0.8, padding_seconds=0.5
    )
    assert result == []


# ---------------------------------------------------------------------------
# Confirmação -- media_catalog.py não tocado / nenhuma migration nova
# ---------------------------------------------------------------------------


def test_media_catalog_nao_possui_badge_para_silencio():
    from _sistema.media_catalog import SYSTEM_BADGES

    for badge in SYSTEM_BADGES:
        assert "SILENCE" not in badge and "SILENCIO" not in badge


def test_nenhuma_migration_nova_criada_por_este_prompt():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9


# ---------------------------------------------------------------------------
# Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def _parse_module() -> ast.AST:
    return ast.parse(Path(silence_removal.__file__).read_text(encoding="utf-8"))


def test_modulo_nao_importa_modulos_protegidos_nem_irmaos_de_decisao():
    """timeline_editor É a UNICA excecao permitida (ver secao 0.1 da
    docstring do modulo) -- todos os demais modulos irmaos de
    decisao/processamento continuam proibidos."""
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
        "audio_engine",
        "auto_reframe",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for proibido in proibidos:
            assert proibido not in names, f"silence_removal.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_modulo_importa_timeline_editor_deliberadamente():
    tree = _parse_module()
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "timeline_editor":
            found = True
    assert found, "timeline_editor deveria ser importado -- excecao deliberada, ver secao 0.1 da docstring"


def test_modulo_nao_chama_subprocess_ffmpeg_diretamente():
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
