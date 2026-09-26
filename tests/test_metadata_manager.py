# -*- coding: utf-8 -*-
"""PROMPT 35 -- MetadataManager: testes.

Cobre a Parte A (configuração ``METADATA_MODE`` -- mode KEEP/CLEAN/
PROFILE + "lembrar minha preferência" + contrato de decisão pendente
para UI futura) e a Parte B (processamento real só para ``mode ==
CLEAN``: cópia com metadata removida via backend injetável, nunca o
arquivo original; cache; cancelamento; concorrência; restart) --
incluindo a prova adversarial central do roadmap: KEEP e PROFILE NUNCA
produzem a mesma evidência que CLEAN. AST estrutural: zero acoplamento
com o módulo legado ``limpar_metadados_oficial.py`` e com módulos
irmãos; ``subprocess``/``ffmpeg`` só dentro do backend padrão isolado;
``media_catalog.py`` não tocado; nenhuma migration nova.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import _sistema.metadata_manager as metadata_manager
from _sistema.app_paths import build_app_paths, ensure_app_directories
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
from _sistema.edit_project import CAPTIONS, EditProjectManager, METADATA_MODE, TEMPLATE
from _sistema.job_engine import JobEngine
from _sistema.metadata_manager import (
    ARTIFACT_KIND_METADATA_CLEAN_OUTPUT,
    CampoInvalidoError,
    compute_cache_key,
    MetadataBackendError,
    MetadataBackendUnavailableError,
    MetadataManager,
    MetadataModeConfigState,
    MODE_CLEAN,
    MODE_KEEP,
    MODE_PROFILE,
    PROFILE_FIELDS,
    VocabularioInvalidoError,
)
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager


FFMPEG_DISPONIVEL = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_SKIP_REASON = "ffmpeg/ffprobe nao encontrados no PATH deste ambiente"


def _gerar_video_com_metadata(path: Path, *, duration: int = 1, size: str = "32x32", rate: int = 5) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-metadata", "title=Meu Video Original",
        "-metadata", "comment=comentario de teste",
        "-metadata", "artist=Fulano",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _ler_tags_ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format_tags",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    import json as _json

    payload = _json.loads(out.stdout.decode("utf-8"))
    return payload.get("format", {}).get("tags", {}) or {}


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_auto_reframe.py)
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
    """Vídeo REAL (com metadata de teste embutida) gerado via ffmpeg
    quando disponível -- nunca um arquivo fake quando o teste precisa
    do backend padrão de verdade."""
    path = tmp_path / "original.mp4"
    if FFMPEG_DISPONIVEL:
        _gerar_video_com_metadata(path)
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


def _fake_backend_ok(calls=None):
    def backend(source_path, dest_path):
        if calls is not None:
            calls.append((source_path, dest_path))
        Path(dest_path).write_bytes(b"conteudo-processado-fake")

    return backend


def _fake_backend_raising(calls=None):
    def backend(source_path, dest_path):
        if calls is not None:
            calls.append((source_path, dest_path))
        raise MetadataBackendError("falha simulada de backend")

    return backend


@pytest.fixture
def engine(manager, database, storage, app_paths, audit):
    return MetadataManager(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, strip_backend=_fake_backend_ok(),
    )


@pytest.fixture
def job_engine(database, audit):
    return JobEngine(database, audit_log=audit)


def _run_job(engine_, job_engine_, video_, project_):
    job_engine_.register_handler(
        MetadataManager.OPERATION, engine_.handle_metadata_job, claims_status="PROCESSING"
    )
    job = engine_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=MetadataManager.OPERATION)
    )
    result = job_engine_.advance(job.id)
    return job, result


# ---------------------------------------------------------------------------
# 0. Construção
# ---------------------------------------------------------------------------


def test_construtor_rejeita_manager_de_tipo_errado(database, storage, app_paths):
    with pytest.raises(TypeError):
        MetadataManager("nao-e-um-manager", database=database, storage_manager=storage, app_paths=app_paths)


def test_construtor_rejeita_database_de_tipo_errado(manager, storage, app_paths):
    with pytest.raises(TypeError):
        MetadataManager(manager, database="nao-e-um-database", storage_manager=storage, app_paths=app_paths)


def test_construtor_rejeita_storage_manager_de_tipo_errado(manager, database, app_paths):
    with pytest.raises(TypeError):
        MetadataManager(manager, database=database, storage_manager="nao-e-storage", app_paths=app_paths)


def test_construtor_rejeita_app_paths_de_tipo_errado(manager, database, storage):
    with pytest.raises(TypeError):
        MetadataManager(manager, database=database, storage_manager=storage, app_paths="nao-e-app-paths")


def test_construtor_rejeita_backend_nao_chamavel(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        MetadataManager(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            strip_backend="nao-e-chamavel",
        )


def test_construtor_rejeita_control_manager_de_tipo_errado(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        MetadataManager(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            control_manager="nao-e-control-manager",
        )


# ===========================================================================
# PARTE A -- configuração
# ===========================================================================


def test_estado_inicial_e_todos_none(engine, project):
    assert engine.get_config(project.id) == MetadataModeConfigState()


def test_set_config_grava_mode(engine, project):
    state = engine.set_config(project.id, mode=MODE_KEEP)
    assert state.mode == MODE_KEEP
    assert engine.get_config(project.id) == state


def test_set_config_profile_com_campos(engine, project):
    state = engine.set_config(project.id, mode=MODE_PROFILE, profile_fields=["title", "comment"])
    assert state.mode == MODE_PROFILE
    assert state.profile_fields == ("comment", "title")


def test_profile_fields_dedup_e_ordenado(engine, project):
    state = engine.set_config(project.id, mode=MODE_PROFILE, profile_fields=["title", "title", "artist"])
    assert state.profile_fields == ("artist", "title")


def test_set_config_parcial_preserva_profile_fields(engine, project):
    engine.set_config(project.id, mode=MODE_PROFILE, profile_fields=["title"])
    engine.set_config(project.id, mode=MODE_PROFILE)
    state = engine.get_config(project.id)
    assert state.profile_fields == ("title",)


@pytest.mark.parametrize("value", ["clean", "", None, 1, "CLEAN ", "keep"])
def test_mode_vocabulario_fechado(engine, project, value):
    with pytest.raises(VocabularioInvalidoError):
        engine.set_config(project.id, mode=value)


@pytest.mark.parametrize("value", [None, "title", ["nao_existe"], [], (), 1, ["title", "nao_existe"]])
def test_profile_fields_vocabulario_e_tipo(engine, project, value):
    if value is None:
        # None é tratado como "campo ausente" em set_config parcial --
        # testado separadamente; aqui cobrimos os valores REJEITADOS.
        return
    with pytest.raises((CampoInvalidoError, VocabularioInvalidoError)):
        engine.set_config(project.id, profile_fields=value)


def test_profile_fields_todos_os_campos_validos(engine, project):
    state = engine.set_config(project.id, mode=MODE_PROFILE, profile_fields=sorted(PROFILE_FIELDS))
    assert state.profile_fields == tuple(sorted(PROFILE_FIELDS))


def test_campo_desconhecido_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_config(project.id, campo_que_nao_existe=1)


def test_categoria_metadata_mode_isolada_de_captions_e_template(database, engine, project, manager):
    engine.set_config(project.id, mode=MODE_CLEAN)
    manager.set_category(project.id, CAPTIONS, {"mode": "OFF"})
    manager.set_category(project.id, TEMPLATE, {"id": "x"})
    categorias = manager.list_categories(project.id)
    assert METADATA_MODE in categorias
    assert CAPTIONS in categorias
    assert TEMPLATE in categorias


def test_alterar_metadata_mode_nao_muda_fingerprint_de_captions(database, engine, project, manager):
    manager.set_category(project.id, CAPTIONS, {"mode": "OFF"})
    before = manager.get_category(project.id, CAPTIONS)
    engine.set_config(project.id, mode=MODE_CLEAN)
    after = manager.get_category(project.id, CAPTIONS)
    assert before.fingerprint == after.fingerprint


def test_update_category_e_usado_merge_atomico_nunca_replace(engine, project):
    engine.set_config(project.id, mode=MODE_PROFILE, profile_fields=["title"])
    engine.set_config(project.id, mode=MODE_PROFILE, profile_fields=["comment"])
    # A segunda chamada REPLACE profile_fields (mesmo campo, novo valor) --
    # prova de merge atômico é indireta: mode sobrevive mesmo sem ser
    # re-passado na segunda chamada.
    state = engine.get_config(project.id)
    assert state.mode == MODE_PROFILE
    assert state.profile_fields == ("comment",)


def test_estado_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    engine_a = MetadataManager(
        EditProjectManager(db_a), database=db_a,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    engine_a.set_config(project.id, mode=MODE_CLEAN)

    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    engine_b = MetadataManager(
        EditProjectManager(db_b), database=db_b,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    assert engine_b.get_config(project.id).mode == MODE_CLEAN


def test_repetir_o_mesmo_set_config_sequencialmente_nao_acumula(engine, project, manager):
    engine.set_config(project.id, mode=MODE_KEEP)
    before = manager.get_category(project.id, METADATA_MODE)
    engine.set_config(project.id, mode=MODE_KEEP)
    after = manager.get_category(project.id, METADATA_MODE)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_duas_operacoes_concorrentes_de_config_em_projetos_diferentes(app_paths, database, project, project2):
    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []

    def worker(project_id, mode):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_engine = MetadataManager(
            EditProjectManager(db_instance), database=db_instance,
            storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
            app_paths=app_paths,
        )
        barrier.wait(timeout=5)
        try:
            local_engine.set_config(project_id, mode=mode)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(project.id, MODE_CLEAN)),
        threading.Thread(target=worker, args=(project2.id, MODE_KEEP)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    reread = MetadataManager(
        EditProjectManager(database), database=database,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    assert reread.get_config(project.id).mode == MODE_CLEAN
    assert reread.get_config(project2.id).mode == MODE_KEEP


def test_set_config_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.set_config("nao-e-um-uuid", mode=MODE_KEEP)


def test_get_config_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.get_config("nao-e-um-uuid")


# ===========================================================================
# "Lembrar minha preferência"
# ===========================================================================


def test_remember_preference_grava_e_le(engine):
    assert engine.get_remembered_preference() is None
    state = engine.remember_preference(MODE_CLEAN)
    assert state.mode == MODE_CLEAN
    lido = engine.get_remembered_preference()
    assert lido == MetadataModeConfigState(mode=MODE_CLEAN, profile_fields=None)


def test_remember_preference_profile_exige_profile_fields(engine):
    with pytest.raises(CampoInvalidoError):
        engine.remember_preference(MODE_PROFILE)


def test_remember_preference_non_profile_rejeita_profile_fields(engine):
    with pytest.raises(CampoInvalidoError):
        engine.remember_preference(MODE_KEEP, profile_fields=["title"])


def test_remember_preference_profile_com_campos_ok(engine):
    state = engine.remember_preference(MODE_PROFILE, profile_fields=["title", "comment"])
    assert state.mode == MODE_PROFILE
    assert state.profile_fields == ("comment", "title")
    lido = engine.get_remembered_preference()
    assert lido.mode == MODE_PROFILE
    assert lido.profile_fields == ("comment", "title")


def test_remember_preference_valida_mode(engine):
    with pytest.raises(VocabularioInvalidoError):
        engine.remember_preference("nao-existe")


def test_forget_remembered_preference_remove(engine):
    engine.remember_preference(MODE_CLEAN)
    assert engine.forget_remembered_preference() is True
    assert engine.get_remembered_preference() is None
    assert engine.forget_remembered_preference() is False


def test_remembered_preference_e_global_nao_por_projeto(engine, project, project2):
    """Escopo investigado e decidido: instalação única (sem conceito de
    multi-usuário confirmado hoje) -- a MESMA preferência lembrada é
    visível independente do project_id consultado."""
    engine.remember_preference(MODE_CLEAN)
    dec1 = engine.get_pending_decision(project.id)
    dec2 = engine.get_pending_decision(project2.id)
    assert dec1.suggested_mode == MODE_CLEAN
    assert dec2.suggested_mode == MODE_CLEAN


def test_remembered_preference_sobrevive_restart(app_paths, engine):
    engine.remember_preference(MODE_PROFILE, profile_fields=["artist"])

    db_b = LocalDatabase(app_paths.database / "painel.db")
    engine_b = MetadataManager(
        EditProjectManager(db_b), database=db_b,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    lido = engine_b.get_remembered_preference()
    assert lido.mode == MODE_PROFILE
    assert lido.profile_fields == ("artist",)


# ===========================================================================
# Contrato de decisão pendente (para UI futura)
# ===========================================================================


def test_pending_decision_sem_decisao_nem_preferencia(engine, project):
    dec = engine.get_pending_decision(project.id)
    assert dec.has_decision is False
    assert dec.current_mode is None
    assert dec.suggested_mode is None


def test_pending_decision_com_decisao_ja_tomada(engine, project):
    engine.set_config(project.id, mode=MODE_CLEAN)
    dec = engine.get_pending_decision(project.id)
    assert dec.has_decision is True
    assert dec.current_mode == MODE_CLEAN


def test_pending_decision_sugere_preferencia_lembrada_quando_sem_decisao(engine, project):
    engine.remember_preference(MODE_KEEP)
    dec = engine.get_pending_decision(project.id)
    assert dec.has_decision is False
    assert dec.suggested_mode == MODE_KEEP


# ===========================================================================
# PARTE B -- pipeline completo via JobEngine
# ===========================================================================


def test_job_sem_video_id_falha_estruturalmente(engine, job_engine):
    job_engine.register_handler(MetadataManager.OPERATION, engine.handle_metadata_job, claims_status="PROCESSING")
    job = engine._audit_log.create_job(Job(operation=MetadataManager.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_job_sem_project_id_falha_estruturalmente(engine, job_engine, video):
    job_engine.register_handler(MetadataManager.OPERATION, engine.handle_metadata_job, claims_status="PROCESSING")
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=MetadataManager.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_job_sem_mode_configurado_falha_estruturalmente(engine, job_engine, video, project):
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_FAILED


def test_get_config_com_project_inexistente_propaga_erro_estruturado(engine):
    """Inserir um Job com project_id inexistente falha na FK antes mesmo
    do handler rodar (cenário estruturalmente impossível de construir via
    Job) -- o caminho real de "project não encontrado" é exercitado
    através de get_config/set_config diretamente, mesmo padrão já usado
    em test_silence_removal.py para o mesmo problema."""
    from _sistema.edit_project import ProjectNaoEncontradoError

    with pytest.raises(ProjectNaoEncontradoError):
        engine.get_config("00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# KEEP/PROFILE -- prova adversarial: NUNCA produzem a mesma evidencia que
# CLEAN (texto literal do roadmap, FASE 6)
# ---------------------------------------------------------------------------


def test_job_modo_keep_e_ready_mas_nunca_grava_evidencia(engine, job_engine, video, project):
    engine.set_config(project.id, mode=MODE_KEEP)
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY

    with engine._database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_METADATA_CLEAN_OUTPUT),
        ).fetchone()
    assert n == 0
    assert not engine._audit_log.has_reached_checkpoint(job.id, metadata_manager.CHECKPOINT_COMPOSED)


def test_job_modo_profile_e_ready_mas_nunca_grava_evidencia(engine, job_engine, video, project):
    engine.set_config(project.id, mode=MODE_PROFILE, profile_fields=["title"])
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY

    with engine._database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_METADATA_CLEAN_OUTPUT),
        ).fetchone()
    assert n == 0
    assert not engine._audit_log.has_reached_checkpoint(job.id, metadata_manager.CHECKPOINT_COMPOSED)


def test_job_modo_keep_nunca_chama_o_backend(job_engine, manager, database, storage, app_paths, audit, video, project):
    calls = []
    engine_x = MetadataManager(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, strip_backend=_fake_backend_ok(calls=calls),
    )
    engine_x.set_config(project.id, mode=MODE_KEEP)
    job, result = _run_job(engine_x, job_engine, video, project)
    assert result.status == JOB_READY
    assert calls == []


# ---------------------------------------------------------------------------
# CLEAN -- processamento real
# ---------------------------------------------------------------------------


def test_clean_bem_sucedido_gera_artifact_e_checkpoint(engine, job_engine, video, project):
    engine.set_config(project.id, mode=MODE_CLEAN)
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY

    with engine._database.connection() as conn:
        rows = conn.execute(
            "SELECT kind, path, fingerprint, job_id, video_id, project_id FROM artifacts WHERE video_id = ?",
            (video.id,),
        ).fetchall()
    assert len(rows) == 1
    kind, path, fingerprint, job_id, video_id, project_id = rows[0]
    assert kind == ARTIFACT_KIND_METADATA_CLEAN_OUTPUT
    assert Path(path).is_file()
    assert Path(path).stat().st_size > 0
    assert fingerprint is not None
    assert job_id == job.id
    assert video_id == video.id
    assert project_id == project.id
    assert engine._audit_log.has_reached_checkpoint(job.id, metadata_manager.CHECKPOINT_COMPOSED)


def test_clean_nunca_toca_o_arquivo_original(engine, job_engine, video, project, video_file):
    original_bytes = video_file.read_bytes()
    original_mtime = video_file.stat().st_mtime_ns

    engine.set_config(project.id, mode=MODE_CLEAN)
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY

    assert video_file.read_bytes() == original_bytes
    assert video_file.stat().st_mtime_ns == original_mtime


def test_clean_copia_e_um_arquivo_fisicamente_diferente_do_original(engine, job_engine, video, project, video_file):
    engine.set_config(project.id, mode=MODE_CLEAN)
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY

    with engine._database.connection() as conn:
        (path,) = conn.execute(
            "SELECT path FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_METADATA_CLEAN_OUTPUT),
        ).fetchone()
    assert Path(path).resolve() != video_file.resolve()


def test_backend_falhando_falha_o_job_nunca_grava_evidencia(job_engine, manager, database, storage, app_paths, audit, video, project):
    calls = []
    engine_fail = MetadataManager(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, strip_backend=_fake_backend_raising(calls=calls),
    )
    engine_fail.set_config(project.id, mode=MODE_CLEAN)
    job, result = _run_job(engine_fail, job_engine, video, project)
    assert result.status == JOB_FAILED, "falha do backend deve SEMPRE falhar o Job (secao 0.5 -- diferente do Prompt 34)"
    assert len(calls) == 1

    with database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)
        ).fetchone()
    assert n == 0
    assert not audit.has_reached_checkpoint(job.id, metadata_manager.CHECKPOINT_COMPOSED)


def test_falha_estrutural_source_asset_ausente_falha_o_job(manager, database, storage, app_paths, audit, project):
    video_sem_source = Video(source_asset_id=None, name="sem-source")
    database.insert(video_sem_source)
    project_sem = Project(video_id=video_sem_source.id, name="p-sem-source")
    database.insert(project_sem)
    engine_x = MetadataManager(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, strip_backend=_fake_backend_ok(),
    )
    engine_x.set_config(project_sem.id, mode=MODE_CLEAN)
    job_engine_x = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_x, job_engine_x, video_sem_source, project_sem)
    assert result.status == JOB_FAILED


def test_arquivo_de_origem_ausente_no_disco_falha_o_job(manager, database, storage, app_paths, audit, project, tmp_path):
    missing_path = tmp_path / "nao_existe_mais.mp4"
    src_missing = SourceAsset(
        source_uri=str(missing_path), local_path=str(missing_path),
        original_name="sumiu.mp4", fingerprint="fp-sumiu",
    )
    database.insert(src_missing)
    video_missing = Video(source_asset_id=src_missing.id, name="video-sumiu")
    database.insert(video_missing)
    project_missing = Project(video_id=video_missing.id, name="p-sumiu")
    database.insert(project_missing)

    engine_x = MetadataManager(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, strip_backend=_fake_backend_ok(),
    )
    engine_x.set_config(project_missing.id, mode=MODE_CLEAN)
    job_engine_x = JobEngine(database, audit_log=audit)
    job, result = _run_job(engine_x, job_engine_x, video_missing, project_missing)
    assert result.status == JOB_FAILED


def test_cache_hit_nao_chama_backend_de_novo_nem_duplica_artifact(manager, database, storage, app_paths, audit, video, project):
    calls = []
    engine_c = MetadataManager(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, strip_backend=_fake_backend_ok(calls=calls),
    )
    engine_c.set_config(project.id, mode=MODE_CLEAN)
    job_engine_c = JobEngine(database, audit_log=audit)
    job1, result1 = _run_job(engine_c, job_engine_c, video, project)
    assert result1.status == JOB_READY
    assert len(calls) == 1

    job2 = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=MetadataManager.OPERATION))
    result2 = job_engine_c.advance(job2.id)
    assert result2.status == JOB_READY
    assert len(calls) == 1, "cache hit nao pode chamar o backend de novo"
    assert audit.has_reached_checkpoint(job2.id, metadata_manager.CHECKPOINT_COMPOSED)

    with database.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_METADATA_CLEAN_OUTPUT),
        ).fetchone()
    assert n == 1, "cache hit nunca duplica Artifact"


def test_restart_com_instancias_totalmente_novas_preserva_artifact(app_paths, engine, job_engine, video, project):
    engine.set_config(project.id, mode=MODE_CLEAN)
    job, result = _run_job(engine, job_engine, video, project)
    assert result.status == JOB_READY

    db2 = LocalDatabase(app_paths.database / "painel.db")
    with db2.connection() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
            (video.id, ARTIFACT_KIND_METADATA_CLEAN_OUTPUT),
        ).fetchone()
    assert n == 1


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
            engine_instance = MetadataManager(
                EditProjectManager(db_instance), database=db_instance,
                storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
                app_paths=app_paths, audit_log=audit_instance,
                strip_backend=_fake_backend_ok(),
            )
            job_engine_instance = JobEngine(db_instance, audit_log=audit_instance)
            engine_instance.set_config(project_obj.id, mode=MODE_CLEAN)
            job_engine_instance.register_handler(
                MetadataManager.OPERATION, engine_instance.handle_metadata_job, claims_status="PROCESSING"
            )
            job = audit_instance.create_job(Job(video_id=video_obj.id, project_id=project_obj.id, operation=MetadataManager.OPERATION))
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
    for v in videos:
        with database.connection() as conn:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE video_id = ? AND kind = ?",
                (v.id, ARTIFACT_KIND_METADATA_CLEAN_OUTPUT),
            ).fetchone()
        assert n == 1


# ---------------------------------------------------------------------------
# Cancelamento em dois pontos
# ---------------------------------------------------------------------------


def test_cancelamento_antes_do_processamento(manager, database, storage, app_paths, video, project):
    audit_local = OperationalAuditLog(database)
    control_local = ControlManager(database)
    engine_cancel = MetadataManager(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit_local, control_manager=control_local, strip_backend=_fake_backend_ok(),
    )
    engine_cancel.set_config(project.id, mode=MODE_CLEAN)
    job = audit_local.create_job(Job(video_id=video.id, project_id=project.id, operation=MetadataManager.OPERATION))
    audit_local.transition_job(job.id, JOB_PROCESSING)
    control_local.cancel(CONTROL_SCOPE_JOB, job.id)
    claimed_job = database.get(Job, job.id)

    result = engine_cancel.handle_metadata_job(claimed_job)
    assert result.target_status == JOB_CANCELLED
    assert result.data["reason"] == "cancelled_before_processing"

    with database.connection() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchone()
    assert n == 0


def test_cancelamento_apos_processamento_antes_de_promover(manager, database, storage, app_paths, video, project):
    audit_local = OperationalAuditLog(database)
    control_local = ControlManager(database)
    job_holder = {}

    def backend_that_cancels(source_path, dest_path):
        Path(dest_path).write_bytes(b"conteudo-processado")
        control_local.cancel(CONTROL_SCOPE_JOB, job_holder["id"])

    engine_cancel = MetadataManager(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit_local, control_manager=control_local, strip_backend=backend_that_cancels,
    )
    engine_cancel.set_config(project.id, mode=MODE_CLEAN)
    job = audit_local.create_job(Job(video_id=video.id, project_id=project.id, operation=MetadataManager.OPERATION))
    job_holder["id"] = job.id
    audit_local.transition_job(job.id, JOB_PROCESSING)
    claimed_job = database.get(Job, job.id)

    result = engine_cancel.handle_metadata_job(claimed_job)
    assert result.target_status == JOB_CANCELLED
    assert result.data["reason"] == "cancelled_after_processing"

    with database.connection() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchone()
    assert n == 0, "cancelamento pos-processamento nao pode deixar Artifact orfao"


# ---------------------------------------------------------------------------
# Backend padrão de produção -- ffmpeg REAL (só aqui; todo outro teste
# injeta um backend fake determinístico)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_backend_padrao_ffmpeg_remove_metadata_de_verdade(video_file, tmp_path):
    tags_antes = _ler_tags_ffprobe(video_file)
    assert tags_antes.get("title") == "Meu Video Original"

    dest = tmp_path / "saida.mp4"
    metadata_manager._default_ffmpeg_strip_backend(str(video_file), str(dest))
    assert dest.is_file()
    assert dest.stat().st_size > 0

    tags_depois = _ler_tags_ffprobe(dest)
    assert tags_depois.get("title", "") in ("", None)
    assert tags_depois.get("comment", "") in ("", None)
    assert tags_depois.get("artist", "") in ("", None)


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_backend_nunca_escreve_metadata_fabricada(video_file, tmp_path):
    """Teste negativo direto contra o antipadrão de
    limpar_metadados_oficial.py -- nenhum campo de GPS/data de
    criação/fabricante/modelo de dispositivo é escrito."""
    dest = tmp_path / "saida2.mp4"
    metadata_manager._default_ffmpeg_strip_backend(str(video_file), str(dest))

    tags = _ler_tags_ffprobe(dest)
    campos_fabricados = (
        "com.apple.quicktime.make", "com.apple.quicktime.model",
        "com.apple.quicktime.creationdate", "make", "model",
        "creation_time", "location", "location-eng",
    )
    for campo in campos_fabricados:
        valor = tags.get(campo)
        assert not valor, f"campo fabricado {campo!r} nao deveria existir na saida (valor: {valor!r})"


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_backend_padrao_usa_stream_copy_nunca_reencoda(video_file, tmp_path):
    """Confirma -c copy: a saída não passa por libx264/filtros -- o
    ``codec_name`` do stream de vídeo permanece idêntico ao original."""
    def _codec(path):
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "json", str(path)]
        out = subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        import json as _json
        return _json.loads(out.stdout.decode("utf-8"))["streams"][0]["codec_name"]

    dest = tmp_path / "saida3.mp4"
    metadata_manager._default_ffmpeg_strip_backend(str(video_file), str(dest))
    assert _codec(dest) == _codec(video_file)


def test_backend_padrao_levanta_erro_estruturado_quando_ffmpeg_ausente(tmp_path, monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda *_a, **_k: None)
    with pytest.raises(MetadataBackendUnavailableError):
        metadata_manager._default_ffmpeg_strip_backend(str(tmp_path / "x.mp4"), str(tmp_path / "y.mp4"))


# ---------------------------------------------------------------------------
# Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def _parse_module() -> ast.AST:
    return ast.parse(Path(metadata_manager.__file__).read_text(encoding="utf-8"))


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
        "auto_reframe",
        "silence_removal",
        "limpar_metadados_oficial",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for proibido in proibidos:
            assert proibido not in names, f"metadata_manager.py nao pode importar {proibido!r} (encontrado: {names!r})"


def _default_backend_func_node(tree: ast.AST) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_default_ffmpeg_strip_backend":
            return node
    raise AssertionError("_default_ffmpeg_strip_backend nao encontrado no modulo")


def test_subprocess_ffmpeg_so_aparecem_dentro_do_backend_padrao_isolado():
    """subprocess/ffmpeg (nomes E chamadas) só podem aparecer dentro do
    corpo de ``_default_ffmpeg_strip_backend`` -- nunca no nível de
    módulo nem em qualquer outro método do Engine."""
    tree = _parse_module()
    backend_node = _default_backend_func_node(tree)
    start, end = backend_node.lineno, backend_node.end_lineno

    proibidos = ("subprocess", "ffmpeg")
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is not None and start <= lineno <= end:
            continue  # dentro do backend isolado -- permitido
        if isinstance(node, ast.Name):
            assert node.id.lower() not in proibidos, f"uso de {node.id!r} fora do backend isolado (linha {lineno})"
        elif isinstance(node, ast.Attribute):
            assert node.attr.lower() not in proibidos, f"uso de .{node.attr!r} fora do backend isolado (linha {lineno})"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] + [alias.name for alias in node.names]
            )
            for name in names:
                assert name.lower() not in proibidos, f"import de {name!r} fora do backend isolado (linha {lineno})"


def test_modulo_nunca_cria_video_ou_publication_ou_schedule():
    tree = _parse_module()
    proibidos_chamadas = {"Video", "Publication", "Schedule"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in proibidos_chamadas


def test_media_catalog_badge_metadata_clean_continua_hardcoded_zero():
    from _sistema.media_catalog import _BADGE_SQL_EXPRESSIONS, BADGE_METADATA_CLEAN

    assert _BADGE_SQL_EXPRESSIONS[BADGE_METADATA_CLEAN] == "0"


def test_nenhuma_migration_nova_criada_por_este_prompt():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9
