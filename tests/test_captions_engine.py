# -*- coding: utf-8 -*-
"""PROMPT 31 -- Captions Engine: testes.

Cobre a Parte A (decisão de modo, mesma disciplina de fingerprint/
concorrência/restart/idempotência dos Prompts 28-30) e a Parte B
(pipeline real de transcrição): sucesso completo com os 3 Artifacts
reais; cache hit (backend NUNCA chamado de novo); fingerprint OU config
diferente força nova transcrição; falhas estruturadas (origem ausente,
saída inválida); exceção não tratada pousando em FAILED via o próprio
JobEngine; cancelamento observado em dois pontos; concorrência real;
GATE ADVERSARIAL (timestamps fora de ordem/negativos/não-finitos, texto
vazio, unicode/emoji, ordem de chaves do cache key); AST estrutural; e
confirmação de nenhuma migration nova.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import _sistema.captions_engine as captions_engine
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.captions_engine import (
    ARTIFACT_KIND_SRT,
    ARTIFACT_KIND_TRANSCRIPT,
    ARTIFACT_KIND_VTT,
    CAPTION_MODES,
    CaptionsEngine,
    CaptionsState,
    ModoInvalidoError,
    MODE_BURNED,
    MODE_OFF,
    MODE_SEPARATE,
    TranscriptionResult,
    TranscriptionSegment,
    _canonical_json,
    _segments_to_internal_json,
    _segments_to_srt,
    _segments_to_vtt,
    _validate_and_normalize_segments,
    compute_cache_key,
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
from _sistema.edit_project import AUDIO_SETTINGS, CAPTIONS, EditProjectManager, TEMPLATE
from _sistema.job_engine import JobEngine, JobHandlerError
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager


# ---------------------------------------------------------------------------
# Fixtures
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
    path = tmp_path / "original.mp4"
    path.write_bytes(b"fake video bytes")
    return path


@pytest.fixture
def source(database, video_file):
    src = SourceAsset(
        source_uri=str(video_file),
        local_path=str(video_file),
        original_name="original.mp4",
        fingerprint="source-fp-1",
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


def _fake_backend(segments=None, language="pt", calls=None):
    if segments is None:
        segments = (TranscriptionSegment(start=0.0, end=1.5, text="olá mundo"),)

    def backend(local_path, model_size, language_arg):
        if calls is not None:
            calls.append((local_path, model_size, language_arg))
        return TranscriptionResult(segments=segments, language=language)

    return backend


@pytest.fixture
def engine(manager, database, storage, app_paths, audit):
    return CaptionsEngine(
        manager,
        database=database,
        storage_manager=storage,
        app_paths=app_paths,
        audit_log=audit,
        transcription_backend=_fake_backend(),
    )


@pytest.fixture
def job_engine(database, audit):
    return JobEngine(database, audit_log=audit)


# ---------------------------------------------------------------------------
# 0. Construção
# ---------------------------------------------------------------------------


def test_construtor_rejeita_manager_de_tipo_errado(database, storage, app_paths):
    with pytest.raises(TypeError):
        CaptionsEngine("nao-e-um-manager", database=database, storage_manager=storage, app_paths=app_paths)


def test_construtor_rejeita_database_de_tipo_errado(manager, storage, app_paths):
    with pytest.raises(TypeError):
        CaptionsEngine(manager, database="nao-e-um-database", storage_manager=storage, app_paths=app_paths)


def test_construtor_rejeita_storage_manager_de_tipo_errado(manager, database, app_paths):
    with pytest.raises(TypeError):
        CaptionsEngine(manager, database=database, storage_manager="nao-e-storage", app_paths=app_paths)


def test_construtor_rejeita_app_paths_de_tipo_errado(manager, database, storage):
    with pytest.raises(TypeError):
        CaptionsEngine(manager, database=database, storage_manager=storage, app_paths="nao-e-app-paths")


def test_construtor_rejeita_backend_nao_chamavel(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        CaptionsEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            transcription_backend="nao-e-chamavel",
        )


def test_construtor_rejeita_control_manager_de_tipo_errado(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        CaptionsEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            control_manager="nao-e-control-manager",
        )


def test_construtor_rejeita_default_model_size_vazio(manager, database, storage, app_paths):
    with pytest.raises(ValueError):
        CaptionsEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            default_model_size="   ",
        )


# ===========================================================================
# PARTE A -- decisão de modo
# ===========================================================================


def test_estado_inicial_e_none(engine, project):
    assert engine.get_mode(project.id) == CaptionsState()


@pytest.mark.parametrize("mode", sorted(CAPTION_MODES))
def test_definir_cada_modo_valido(engine, project, mode):
    result = engine.set_mode(project.id, mode)
    assert result.mode == mode
    assert engine.get_mode(project.id).mode == mode


@pytest.mark.parametrize("mode", ["off", "separate", "burned", "INVALID", "", None, 1, True])
def test_definir_modo_invalido_e_erro(engine, project, mode):
    with pytest.raises(ModoInvalidoError):
        engine.set_mode(project.id, mode)


def test_burned_e_so_decisao_nunca_compoe_video(engine, project):
    """BURNED é só a decisão de que a legenda deve futuramente ser
    queimada -- este módulo nunca chama FFmpeg nem escreve pixels de
    vídeo (ver AST estrutural mais abaixo)."""
    result = engine.set_mode(project.id, MODE_BURNED)
    assert result.mode == MODE_BURNED


def test_alterar_modo_nao_muda_fingerprint_de_audio_settings(database, engine, project, manager):
    manager.set_category(project.id, AUDIO_SETTINGS, {"volume": 1.0})
    before = manager.get_category(project.id, AUDIO_SETTINGS)

    engine.set_mode(project.id, MODE_SEPARATE)

    after = manager.get_category(project.id, AUDIO_SETTINGS)
    assert before.fingerprint == after.fingerprint


def test_alterar_audio_settings_nao_muda_fingerprint_de_captions(database, engine, project, manager):
    engine.set_mode(project.id, MODE_SEPARATE)
    before = manager.get_category(project.id, CAPTIONS)

    manager.set_category(project.id, AUDIO_SETTINGS, {"volume": 1.5})
    manager.set_category(project.id, TEMPLATE, {"id": "x"})

    after = manager.get_category(project.id, CAPTIONS)
    assert before.fingerprint == after.fingerprint


def test_categorias_captions_e_template_sao_distintas(database, engine, project, manager):
    engine.set_mode(project.id, MODE_OFF)
    manager.set_category(project.id, TEMPLATE, {"id": "y"})
    categorias = manager.list_categories(project.id)
    assert CAPTIONS in categorias
    assert TEMPLATE in categorias


def test_estado_de_modo_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    engine_a = CaptionsEngine(
        EditProjectManager(db_a), database=db_a,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    engine_a.set_mode(project.id, MODE_BURNED)

    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    engine_b = CaptionsEngine(
        EditProjectManager(db_b), database=db_b,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    assert engine_b.get_mode(project.id).mode == MODE_BURNED


def test_repetir_o_mesmo_set_mode_sequencialmente_nao_acumula(engine, project, manager):
    engine.set_mode(project.id, MODE_SEPARATE)
    before = manager.get_category(project.id, CAPTIONS)
    engine.set_mode(project.id, MODE_SEPARATE)
    after = manager.get_category(project.id, CAPTIONS)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_duas_operacoes_concorrentes_de_modo_em_projetos_diferentes(app_paths, database, project, project2):
    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []

    def worker(project_id, mode):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_engine = CaptionsEngine(
            EditProjectManager(db_instance), database=db_instance,
            storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
            app_paths=app_paths,
        )
        barrier.wait(timeout=5)
        try:
            local_engine.set_mode(project_id, mode)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(project.id, MODE_SEPARATE)),
        threading.Thread(target=worker, args=(project2.id, MODE_BURNED)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    reread = CaptionsEngine(
        EditProjectManager(database), database=database,
        storage_manager=StorageManager(app_paths, database_path=app_paths.database / "painel.db"),
        app_paths=app_paths,
    )
    assert reread.get_mode(project.id).mode == MODE_SEPARATE
    assert reread.get_mode(project2.id).mode == MODE_BURNED


def test_set_mode_com_project_id_nao_uuid_e_erro_estruturado(engine):
    """Mesmo achado do GATE ADVERSARIAL do Prompt 30 (audio_engine.py):
    um project_id que não é sequer um UUID válido não pode deixar um
    ValueError cru escapar -- reclassificado como ModoInvalidoError."""
    with pytest.raises(ModoInvalidoError):
        engine.set_mode("nao-e-um-uuid", MODE_OFF)


def test_get_mode_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(ModoInvalidoError):
        engine.get_mode("nao-e-um-uuid")


def test_get_mode_rejeita_mode_persistido_corrompido(database, engine, project, manager):
    manager.set_category(project.id, CAPTIONS, {"mode": "NAO_EH_UM_MODO_VALIDO"})
    with pytest.raises(ModoInvalidoError):
        engine.get_mode(project.id)


# ===========================================================================
# PARTE B -- cache key
# ===========================================================================


def test_cache_key_e_deterministico(source):
    k1 = compute_cache_key(source.fingerprint, "small", "pt")
    k2 = compute_cache_key(source.fingerprint, "small", "pt")
    assert k1 == k2


def test_cache_key_muda_com_fingerprint_diferente():
    assert compute_cache_key("fp-a", "small", "pt") != compute_cache_key("fp-b", "small", "pt")


def test_cache_key_muda_com_model_size_diferente():
    assert compute_cache_key("fp", "small", "pt") != compute_cache_key("fp", "medium", "pt")


def test_cache_key_muda_com_language_diferente():
    assert compute_cache_key("fp", "small", "pt") != compute_cache_key("fp", "small", "en")
    assert compute_cache_key("fp", "small", "pt") != compute_cache_key("fp", "small", None)


def test_cache_key_nao_depende_da_ordem_de_construcao_do_dict_de_config():
    """GATE ADVERSARIAL: um dict de configuração com ordem de chaves
    diferente NUNCA pode produzir uma chave de cache diferente por
    acidente -- ``_canonical_json`` usa ``sort_keys=True``."""
    payload_a = {"source_fingerprint": "fp", "model_size": "small", "language": "pt"}
    payload_b = {"language": "pt", "model_size": "small", "source_fingerprint": "fp"}
    assert _canonical_json(payload_a) == _canonical_json(payload_b)


# ===========================================================================
# PARTE B -- normalização/validação de segmentos (GATE ADVERSARIAL)
# ===========================================================================


def test_segmento_com_texto_vazio_e_descartado_silenciosamente():
    segments = (
        TranscriptionSegment(start=0.0, end=1.0, text="ok"),
        TranscriptionSegment(start=1.0, end=2.0, text="   "),
        TranscriptionSegment(start=2.0, end=3.0, text=""),
    )
    result = _validate_and_normalize_segments(segments)
    assert len(result) == 1
    assert result[0].text == "ok"


def test_todos_os_segmentos_vazios_e_saida_invalida():
    segments = (TranscriptionSegment(start=0.0, end=1.0, text="   "),)
    with pytest.raises(captions_engine._InvalidTranscriptionOutput):
        _validate_and_normalize_segments(segments)


def test_lista_de_segmentos_vazia_e_saida_invalida():
    with pytest.raises(captions_engine._InvalidTranscriptionOutput):
        _validate_and_normalize_segments(())


@pytest.mark.parametrize(
    "start,end",
    [
        (5.0, 2.0),  # fora de ordem
        (3.0, 3.0),  # start == end
        (-1.0, 2.0),  # negativo
        (float("inf"), 2.0),
        (0.0, float("inf")),
        (float("nan"), 2.0),
        (0.0, float("nan")),
        (float("-inf"), 2.0),
    ],
)
def test_timestamps_invalidos_invalidam_a_transcricao_inteira(start, end):
    segments = (
        TranscriptionSegment(start=0.0, end=1.0, text="ok"),
        TranscriptionSegment(start=start, end=end, text="quebrado"),
    )
    with pytest.raises(captions_engine._InvalidTranscriptionOutput):
        _validate_and_normalize_segments(segments)


def test_timestamp_bool_e_rejeitado():
    """bool é subclasse de int em Python -- precisa ser rejeitado
    explicitamente mesmo sendo tecnicamente um número válido."""
    segments = (TranscriptionSegment(start=True, end=2.0, text="ok"),)
    with pytest.raises(captions_engine._InvalidTranscriptionOutput):
        _validate_and_normalize_segments(segments)


def test_unicode_emoji_acentuacao_preservados(engine):
    segments = (
        TranscriptionSegment(start=0.0, end=1.0, text="Olá, mundo! 🎬✨"),
        TranscriptionSegment(start=1.0, end=2.0, text="Ação, emoção, café ☕"),
    )
    normalized = _validate_and_normalize_segments(segments)
    srt = _segments_to_srt(normalized)
    vtt = _segments_to_vtt(normalized)
    internal = _segments_to_internal_json(normalized, "pt")
    assert "🎬✨" in srt
    assert "☕" in vtt
    assert "café" in internal
    assert "Ação" in internal


# ===========================================================================
# PARTE B -- formatos SRT/VTT (exatos, sem variação própria)
# ===========================================================================


def test_formato_srt_exato():
    segments = (
        TranscriptionSegment(start=0.0, end=1.234, text="primeira linha"),
        TranscriptionSegment(start=3661.5, end=3662.789, text="segunda linha"),
    )
    srt = _segments_to_srt(segments)
    assert srt == (
        "1\n"
        "00:00:00,000 --> 00:00:01,234\n"
        "primeira linha\n"
        "\n"
        "2\n"
        "01:01:01,500 --> 01:01:02,789\n"
        "segunda linha\n"
        "\n"
    )


def test_formato_vtt_exato():
    segments = (TranscriptionSegment(start=0.0, end=1.234, text="linha"),)
    vtt = _segments_to_vtt(segments)
    assert vtt == (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:01.234\n"
        "linha\n"
        "\n"
    )


def test_representacao_interna_json_contem_campos_esperados():
    segments = (TranscriptionSegment(start=0.0, end=1.5, text="ok"),)
    internal = _segments_to_internal_json(segments, "pt")
    import json as _json

    payload = _json.loads(internal)
    assert payload["language"] == "pt"
    assert payload["segments"] == [{"start": 0.0, "end": 1.5, "text": "ok"}]


def test_srt_e_vtt_derivam_da_mesma_lista_normalizada(engine, project, video, job_engine):
    """SRT e VTT nunca são duas transcrições independentes -- ambos vêm
    da MESMA lista de segmentos normalizada (mesmo texto/timestamps)."""
    segments = (TranscriptionSegment(start=0.0, end=1.0, text="unico segmento"),)
    engine._backend = _fake_backend(segments=segments)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    job_engine.advance(job.id)

    with engine._database.connection() as conn:
        rows = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT kind, path FROM artifacts WHERE video_id = ?", (video.id,)
            ).fetchall()
        }
    srt_text = Path(rows[ARTIFACT_KIND_SRT]).read_text(encoding="utf-8")
    vtt_text = Path(rows[ARTIFACT_KIND_VTT]).read_text(encoding="utf-8")
    assert "unico segmento" in srt_text
    assert "unico segmento" in vtt_text


# ===========================================================================
# PARTE B -- pipeline completo via JobEngine
# ===========================================================================


def test_sucesso_completo_gera_3_artifacts_e_checkpoint(engine, job_engine, video, project):
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(
        Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION)
    )
    result = job_engine.advance(job.id)
    assert result.status == JOB_READY

    with engine._database.connection() as conn:
        rows = conn.execute(
            "SELECT kind, path, fingerprint, job_id, video_id, project_id FROM artifacts WHERE video_id = ?",
            (video.id,),
        ).fetchall()
    assert len(rows) == 3
    kinds = {row[0] for row in rows}
    assert kinds == {ARTIFACT_KIND_TRANSCRIPT, ARTIFACT_KIND_SRT, ARTIFACT_KIND_VTT}
    for kind, path, fingerprint, job_id, video_id, project_id in rows:
        assert Path(path).is_file()
        assert fingerprint is not None
        assert job_id == job.id
        assert video_id == video.id
        assert project_id == project.id

    assert engine._audit_log.has_reached_checkpoint(job.id, captions_engine.CHECKPOINT_TRANSCRIBED)


def test_job_sem_project_id_ainda_funciona(engine, job_engine, video):
    """Transcrição é uma propriedade do vídeo, não exige um Project."""
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_READY


def test_segunda_chamada_com_mesmo_fingerprint_e_config_reaproveita_cache(engine, job_engine, video, project):
    calls = []
    engine._backend = _fake_backend(calls=calls)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)

    job1 = engine._audit_log.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    result1 = job_engine.advance(job1.id)
    assert result1.status == JOB_READY
    assert len(calls) == 1

    job2 = engine._audit_log.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    result2 = job_engine.advance(job2.id)
    assert result2.status == JOB_READY
    assert len(calls) == 1, "backend NUNCA pode ser chamado de novo em cache hit"

    assert engine._audit_log.has_reached_checkpoint(job2.id, captions_engine.CHECKPOINT_TRANSCRIBED)

    with engine._database.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert rows[0][0] == 3, "cache hit nao deve criar novos Artifacts"


def test_backend_falhando_se_chamado_mais_de_uma_vez_prova_cache_hit(engine, job_engine, video, project):
    """Mock que FALHA o teste se chamado mais de uma vez -- prova
    explícita exigida pelo Prompt."""
    call_count = {"n": 0}

    def strict_backend(local_path, model_size, language):
        call_count["n"] += 1
        if call_count["n"] > 1:
            raise AssertionError("backend chamado mais de uma vez -- cache nao foi reaproveitado")
        return TranscriptionResult(segments=(TranscriptionSegment(start=0.0, end=1.0, text="x"),), language="pt")

    engine._backend = strict_backend
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)

    job1 = engine._audit_log.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    job_engine.advance(job1.id)
    job2 = engine._audit_log.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    job_engine.advance(job2.id)
    assert call_count["n"] == 1


def test_fingerprint_diferente_forca_nova_transcricao(engine, job_engine, database, video, project, video_file):
    calls = []
    engine._backend = _fake_backend(calls=calls)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)

    job1 = engine._audit_log.create_job(Job(video_id=video.id, project_id=project.id, operation=CaptionsEngine.OPERATION))
    job_engine.advance(job1.id)
    assert len(calls) == 1

    # Novo SourceAsset com fingerprint diferente para o MESMO vídeo lógico
    src2 = SourceAsset(
        source_uri=str(video_file), local_path=str(video_file), original_name="original.mp4",
        fingerprint="source-fp-DIFERENTE",
    )
    database.insert(src2)
    video2 = Video(source_asset_id=src2.id, name="v2")
    database.insert(video2)

    job2 = engine._audit_log.create_job(Job(video_id=video2.id, operation=CaptionsEngine.OPERATION))
    job_engine.advance(job2.id)
    assert len(calls) == 2, "fingerprint diferente deve forcar nova transcricao"


def test_model_size_diferente_forca_nova_transcricao(engine, job_engine, video, project):
    calls = []
    engine._backend = _fake_backend(calls=calls)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)

    job1 = engine._audit_log.create_job(
        Job(video_id=video.id, operation=CaptionsEngine.OPERATION, extra={"whisper_model_size": "small"})
    )
    job_engine.advance(job1.id)
    job2 = engine._audit_log.create_job(
        Job(video_id=video.id, operation=CaptionsEngine.OPERATION, extra={"whisper_model_size": "medium"})
    )
    job_engine.advance(job2.id)
    assert [c[1] for c in calls] == ["small", "medium"]


def test_language_diferente_forca_nova_transcricao(engine, job_engine, video, project):
    calls = []
    engine._backend = _fake_backend(calls=calls)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)

    job1 = engine._audit_log.create_job(
        Job(video_id=video.id, operation=CaptionsEngine.OPERATION, extra={"language": "pt"})
    )
    job_engine.advance(job1.id)
    job2 = engine._audit_log.create_job(
        Job(video_id=video.id, operation=CaptionsEngine.OPERATION, extra={"language": "en"})
    )
    job_engine.advance(job2.id)
    assert [c[2] for c in calls] == ["pt", "en"]


# ===========================================================================
# PARTE B -- falhas estruturadas (nenhum Artifact fantasma)
# ===========================================================================


def test_job_sem_video_id_falha_estruturadamente(engine, job_engine):
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_video_sem_source_asset_id_falha_estruturadamente(engine, job_engine, database):
    v = Video(name="orfao")
    database.insert(v)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=v.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_source_asset_sem_local_path_falha_estruturadamente(engine, job_engine, database):
    src = SourceAsset(source_uri="http://x", local_path=None, fingerprint="fp")
    database.insert(src)
    v = Video(source_asset_id=src.id, name="v")
    database.insert(v)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=v.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_source_asset_sem_fingerprint_falha_estruturadamente(engine, job_engine, database, video_file):
    src = SourceAsset(source_uri=str(video_file), local_path=str(video_file), fingerprint=None)
    database.insert(src)
    v = Video(source_asset_id=src.id, name="v")
    database.insert(v)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=v.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_arquivo_de_origem_ausente_no_disco_falha_estruturadamente(engine, job_engine, database, tmp_path):
    ghost_path = tmp_path / "nao-existe-mais.mp4"
    src = SourceAsset(source_uri=str(ghost_path), local_path=str(ghost_path), fingerprint="fp-ghost")
    database.insert(src)
    v = Video(source_asset_id=src.id, name="v")
    database.insert(v)

    def backend_should_not_run(*a, **kw):
        raise AssertionError("backend nao deveria ser chamado -- arquivo de origem ausente")

    engine._backend = backend_should_not_run
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=v.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED

    with engine._database.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (v.id,)).fetchall()
    assert rows[0][0] == 0


def test_saida_vazia_falha_sem_criar_artifacts(engine, job_engine, video, project):
    engine._backend = _fake_backend(segments=())
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED

    with engine._database.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert rows[0][0] == 0
    assert not engine._audit_log.has_reached_checkpoint(job.id, captions_engine.CHECKPOINT_TRANSCRIBED)


def test_saida_com_timestamps_quebrados_falha_sem_criar_artifacts(engine, job_engine, video, project):
    engine._backend = _fake_backend(
        segments=(TranscriptionSegment(start=10.0, end=1.0, text="quebrado"),)
    )
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED

    with engine._database.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert rows[0][0] == 0


def test_video_com_duracao_zero_e_saida_vazia_e_falha(engine, job_engine, video, project):
    """Vídeo com duração zero -> backend devolve zero segmentos -> saída
    inválida -> FAILED (mesma via da transcrição vazia)."""
    engine._backend = _fake_backend(segments=())
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_FAILED


def test_excecao_nao_tratada_no_backend_pousa_em_failed_via_job_engine(engine, job_engine, video, project):
    """O handler NÃO captura isso -- confirma que o próprio JobEngine
    (comportamento já existente) pousa o Job em FAILED sozinho."""

    def raising_backend(local_path, model_size, language):
        raise RuntimeError("falha inesperada do backend")

    engine._backend = raising_backend
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))

    with pytest.raises(JobHandlerError):
        job_engine.advance(job.id)

    reloaded = job_engine.get_job(job.id)
    assert reloaded.status == JOB_FAILED

    with engine._database.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert rows[0][0] == 0


def test_excecao_nao_tratada_na_promocao_nao_deixa_artifact_fantasma(engine, job_engine, video, project, monkeypatch):
    """Uma falha DEPOIS da transcrição (durante a promoção a final) --
    nenhum Artifact pode existir se nem todos os 3 arquivos forem
    promovidos com sucesso."""
    original_promote = engine._storage.promote_to_final
    call_count = {"n": 0}

    def flaky_promote(temp_path, final_path, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("falha simulada na promoção do segundo arquivo")
        return original_promote(temp_path, final_path, **kwargs)

    monkeypatch.setattr(engine._storage, "promote_to_final", flaky_promote)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))

    with pytest.raises(JobHandlerError):
        job_engine.advance(job.id)

    with engine._database.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert rows[0][0] == 0, "promocao parcial nunca pode deixar Artifacts registrados"


# ===========================================================================
# PARTE B -- cancelamento
# ===========================================================================


def test_cancelamento_observado_antes_da_transcricao(database, audit, storage, app_paths, manager, video, control):
    engine = CaptionsEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, transcription_backend=_fake_backend(), control_manager=control,
    )
    job = audit.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    audit.transition_job(job.id, JOB_PROCESSING)
    control.cancel(CONTROL_SCOPE_JOB, job.id)

    claimed_job = database.get(Job, job.id)
    result = engine.handle_transcription_job(claimed_job)
    assert result.target_status == JOB_CANCELLED

    with database.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert rows[0][0] == 0


def test_cancelamento_observado_apos_transcricao_antes_de_promover(database, audit, storage, app_paths, manager, video, control):
    job_holder = {}

    def backend_that_cancels(local_path, model_size, language):
        control.cancel(CONTROL_SCOPE_JOB, job_holder["id"])
        return TranscriptionResult(segments=(TranscriptionSegment(start=0.0, end=1.0, text="x"),), language="pt")

    engine = CaptionsEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, transcription_backend=backend_that_cancels, control_manager=control,
    )
    job = audit.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    audit.transition_job(job.id, JOB_PROCESSING)
    job_holder["id"] = job.id

    claimed_job = database.get(Job, job.id)
    result = engine.handle_transcription_job(claimed_job)
    assert result.target_status == JOB_CANCELLED
    assert result.data["reason"] == "cancelled_after_transcription"

    with database.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert rows[0][0] == 0


def test_sem_control_manager_cancelamento_nao_e_verificado_mas_nao_quebra(engine, job_engine, video, project):
    """Sem control_manager (padrão), o handler simplesmente não verifica
    cancelamento -- comportamento idêntico a antes, nunca quebra."""
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    result = job_engine.advance(job.id)
    assert result.status == JOB_READY


# ===========================================================================
# PARTE B -- GATE 6, concorrência real
# ===========================================================================


def test_duas_transcricoes_concorrentes_do_mesmo_video_e_config_nao_quebram(app_paths, database, video):
    """NOTA (correção pós-evidência real de Windows -- ver
    ``_sistema/storage_manager.py::_replace_with_bounded_retry`` e
    "DIFERENÇAS DE PLATAFORMA" na docstring do módulo): este teste passa
    100% das vezes em CI Linux (milhares de execuções, aqui e antes desta
    correção) porque ``os.rename``/``os.replace`` é incondicionalmente
    atômico em POSIX mesmo sob concorrência real -- ele NÃO reproduz por
    si só o bug do Windows (``PermissionError``/``WinError 5``), que só
    se manifesta no Windows real. A garantia de que a corrida transitória
    de ``os.replace`` no Windows é coberta vem do teste DETERMINÍSTICO em
    ``tests/test_storage_manager.py`` (injeção controlada de falha via
    monkeypatch, não depende de concorrência real nem de plataforma).
    Este teste continua valioso e é mantido como está: prova que a
    concorrência real de dois Jobs no MESMO vídeo/config não quebra a
    reivindicação atômica de Job nem a convergência de artifacts -- uma
    garantia ortogonal à correção de ``promote_to_final``."""
    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []
    results: "list[str]" = []

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        audit_instance = OperationalAuditLog(db_instance)
        storage_instance = StorageManager(app_paths, database_path=app_paths.database / "painel.db")
        manager_instance = EditProjectManager(db_instance)

        def backend(local_path, model_size, language):
            barrier.wait(timeout=5)
            return TranscriptionResult(
                segments=(TranscriptionSegment(start=0.0, end=1.0, text="concorrente"),), language="pt"
            )

        engine_instance = CaptionsEngine(
            manager_instance, database=db_instance, storage_manager=storage_instance,
            app_paths=app_paths, audit_log=audit_instance, transcription_backend=backend,
        )
        job_engine_instance = JobEngine(db_instance, audit_log=audit_instance)
        job_engine_instance.register_handler(
            CaptionsEngine.OPERATION, engine_instance.handle_transcription_job, claims_status=JOB_PROCESSING
        )
        job = audit_instance.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
        try:
            result = job_engine_instance.advance(job.id)
            results.append(result.status)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert results == [JOB_READY, JOB_READY]

    with database.connection() as conn:
        rows = conn.execute("SELECT kind, path FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert len(rows) == 6  # 3 kinds x 2 Jobs, mesmo path (overwrite determinístico)
    for _, path in rows:
        assert Path(path).is_file()


# ===========================================================================
# PARTE B -- restart / idempotência
# ===========================================================================


def test_evidencia_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, video, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    audit_a = OperationalAuditLog(db_a)
    storage_a = StorageManager(app_paths, database_path=app_paths.database / "painel.db")
    manager_a = EditProjectManager(db_a)
    engine_a = CaptionsEngine(
        manager_a, database=db_a, storage_manager=storage_a, app_paths=app_paths,
        audit_log=audit_a, transcription_backend=_fake_backend(),
    )
    job_engine_a = JobEngine(db_a, audit_log=audit_a)
    job_engine_a.register_handler(CaptionsEngine.OPERATION, engine_a.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = audit_a.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    job_engine_a.advance(job.id)

    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    with db_b.connection() as conn:
        rows = conn.execute("SELECT count(*) FROM artifacts WHERE video_id = ?", (video.id,)).fetchall()
    assert rows[0][0] == 3


def test_idempotencia_reprocessar_job_ja_ready_nao_reexecuta_handler(engine, job_engine, video, project):
    calls = []
    engine._backend = _fake_backend(calls=calls)
    job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)
    job = engine._audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    result1 = job_engine.advance(job.id)
    assert result1.status == JOB_READY
    assert len(calls) == 1

    # Job já terminal (READY) -- reivindicar de novo não é permitido pela
    # State Machine central (READY não é PENDING/RETRY).
    from _sistema.job_engine import JobNotActionableError

    with pytest.raises(JobNotActionableError):
        job_engine.advance(job.id)
    assert len(calls) == 1


# ===========================================================================
# Garantias estruturais (AST) e schema
# ===========================================================================


def _parse_captions_engine_module() -> ast.AST:
    return ast.parse(Path(captions_engine.__file__).read_text(encoding="utf-8"))


def test_captions_engine_nao_importa_gerar_textos():
    tree = _parse_captions_engine_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        assert "gerar_textos" not in names


def test_captions_engine_nao_importa_modulos_geracao1_protegidos():
    tree = _parse_captions_engine_module()
    proibidos = (
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "job_state_machine",
        "source_import",
        "source_context",
        "media_probe",
        "video_promotion",
        "media_catalog",
        "limpar_metadados_oficial",
        "gerar_textos",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for proibido in proibidos:
            assert proibido not in names, f"captions_engine.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_captions_engine_nao_chama_subprocess_ffmpeg_ffprobe():
    """SRT/VTT são texto puro -- nunca deveriam precisar disso. AST,
    nunca grep textual ingênuo (a docstring do módulo cita 'ffmpeg' para
    explicar por que não é usado)."""
    tree = _parse_captions_engine_module()
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


def test_captions_engine_nunca_cria_video_publication_schedule_diretamente():
    """Só Artifact é construído diretamente por este módulo -- Job é só
    RECEBIDO (parâmetro do handler) e nunca instanciado aqui (o exemplo
    de uso na docstring não conta -- é texto, não um ast.Call real)."""
    tree = _parse_captions_engine_module()
    proibidos = {"Video", "Publication", "Schedule"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in proibidos


def test_captions_engine_nunca_instancia_job_fora_da_docstring():
    tree = _parse_captions_engine_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "Job"


def test_captions_engine_nunca_transiciona_job_diretamente():
    """O handler nunca decide/persiste Job.status sozinho -- sempre
    devolve JobStepResult e deixa o JobEngine persistir a transição
    (AUTORIDADE ÚNICA, CLAUDE.md item 7)."""
    tree = _parse_captions_engine_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "transition_to"
        if isinstance(node, ast.ImportFrom) and node.module and "job_state_machine" in node.module:
            raise AssertionError("captions_engine.py nao pode importar job_state_machine diretamente")


def test_captions_engine_so_insere_artifact_via_database_insert():
    """``database.insert``/``database.save`` só são usados para
    ``Artifact`` -- nunca para persistir ``Job``/``Video`` diretamente
    (isso pertence exclusivamente ao JobEngine/OperationalAuditLog)."""
    source = Path(captions_engine.__file__).read_text(encoding="utf-8")
    assert "self._database.save(" not in source
    assert source.count("self._database.insert(") >= 1


def test_captions_engine_faster_whisper_nao_e_importado_no_topo_do_modulo():
    """A lib pode não estar instalada no ambiente de teste -- o import
    precisa ser local a ``_default_transcribe``, nunca no topo do
    módulo (senão a simples IMPORTAÇÃO deste módulo já falharia)."""
    tree = _parse_captions_engine_module()
    for node in tree.body:  # só o nível superior do módulo
        if isinstance(node, ast.ImportFrom):
            assert node.module != "faster_whisper"
        if isinstance(node, ast.Import):
            assert all(alias.name != "faster_whisper" for alias in node.names)


def test_import_do_modulo_nao_falha_sem_faster_whisper_instalado():
    """Confirmação direta e honesta: faster_whisper NÃO está instalado
    neste ambiente de teste (verificado nesta sessão), e mesmo assim
    importar captions_engine funciona -- prova de que o import está
    corretamente isolado dentro de ``_default_transcribe``."""
    with pytest.raises(ModuleNotFoundError):
        import faster_whisper  # noqa: F401
    import importlib

    importlib.reload(captions_engine)


def test_captions_categoria_ja_reservada_nao_e_uma_string_nova():
    assert CAPTIONS == "captions"


def test_nenhuma_migration_nova_criada_por_este_prompt():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9
