# -*- coding: utf-8 -*-
"""PROMPT 44 -- SmartClipEngine (base): testes.

Cobre: (1) a função pura ``segment_transcript`` isolada (transcript de um
só assunto; múltiplos assuntos via gap; múltiplos assuntos via marcador de
discurso sem gap; sem nenhum sinal perceptível -- limitação documentada);
(2) invariante de cobertura sem buraco/sobreposição; (3) caso degenerado
de um único segmento bruto; (4) o handler nunca aciona transcrição
novamente quando já existe um artifact de transcrição válido; (5) o
handler aciona a transcrição real (via backend fake) quando nenhum
artifact existe ainda; (6) ``CHECKPOINT_ANALYZED`` gravado SOMENTE no
sucesso; (7) contrato Job/checkpoint/Artifact -- restart com novas
instâncias; (8) nenhuma chamada de API externa (checagem estrutural)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.captions_engine import (
    ARTIFACT_KIND_TRANSCRIPT,
    CaptionsEngine,
    TranscriptionResult,
    TranscriptionSegment,
)
from _sistema.control_manager import ControlManager
from _sistema.domain import (
    Artifact,
    CHECKPOINT_ANALYZED,
    Job,
    JOB_FAILED,
    JOB_READY,
    Project,
    SourceAsset,
    Video,
)
from _sistema.edit_project import EditProjectManager
from _sistema.job_engine import JobEngine
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager

import _sistema.smart_clip_engine as sce
from _sistema.smart_clip_engine import (
    ARTIFACT_KIND_CONTENT_SEGMENTS,
    DEFAULT_DISCOURSE_MARKERS,
    MIN_TOPIC_GAP_SECONDS_PADRAO,
    OPERATION_ANALYZE_CONTENT,
    SEGMENT_ROLE_BEGINNING,
    SEGMENT_ROLE_CONCLUSION,
    SEGMENT_ROLE_DEVELOPMENT,
    SemanticSegment,
    SmartClipEngine,
    compute_cache_key,
    segment_transcript,
)


# ---------------------------------------------------------------------------
# Fixtures -- mesmo padrão já estabelecido por test_captions_engine.py /
# test_final_media_validator.py
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


def _fake_transcription_backend(segments=None, language="pt", calls=None):
    if segments is None:
        segments = (TranscriptionSegment(start=0.0, end=1.5, text="ola mundo"),)

    def backend(local_path, model_size, language_arg):
        if calls is not None:
            calls.append((local_path, model_size, language_arg))
        return TranscriptionResult(segments=segments, language=language)

    return backend


@pytest.fixture
def captions_engine(manager, database, storage, app_paths, audit):
    return CaptionsEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, transcription_backend=_fake_transcription_backend(),
    )


@pytest.fixture
def engine(manager, database, storage, app_paths, audit, captions_engine):
    return SmartClipEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, captions_engine=captions_engine,
    )


@pytest.fixture
def job_engine(database, audit):
    return JobEngine(database, audit_log=audit)


def _insert_transcript_artifact(database, storage, video_, segments, *, cache_key="transcript-fp-1"):
    """Escreve um Artifact ``transcript_internal`` mínimo diretamente,
    reaproveitando o schema real de ``captions_engine._segments_to_internal_json``
    (mesmo formato: lista de {"start","end","text"})."""
    from _sistema.captions_engine import _segments_to_internal_json

    payload = _segments_to_internal_json(segments, "pt")
    temp = storage.allocate_temp(suffix=".json", create=True)
    temp.write_text(payload, encoding="utf-8")
    final_path = Path(storage._app_paths.projects) / "transcripts" / video_.id / f"{cache_key}.json"
    promoted = storage.promote_to_final(temp, final_path, overwrite=True)
    artifact = Artifact(
        video_id=video_.id, kind=ARTIFACT_KIND_TRANSCRIPT, path=str(promoted),
        fingerprint=cache_key, size_bytes=len(payload.encode("utf-8")),
    )
    database.insert(artifact)
    return artifact


# ---------------------------------------------------------------------------
# 1. segment_transcript -- função pura, sem DB/FFmpeg/Whisper
# ---------------------------------------------------------------------------


def test_segmentacao_um_unico_assunto_sem_sinal_de_quebra():
    segs = [
        TranscriptionSegment(start=0.0, end=2.0, text="Hoje vamos falar de X."),
        TranscriptionSegment(start=2.1, end=4.0, text="X é muito importante."),
        TranscriptionSegment(start=4.1, end=6.0, text="Vamos continuar com X."),
    ]
    result = segment_transcript(segs)
    assert len(result) == 1
    assert result[0].role == SEGMENT_ROLE_BEGINNING
    assert result[0].topic_change is False
    assert result[0].start == 0.0
    assert result[0].end == 6.0


def test_segmentacao_multiplos_assuntos_via_gap_de_silencio():
    segs = [
        TranscriptionSegment(start=0.0, end=2.0, text="Falando de X."),
        TranscriptionSegment(start=2.1, end=4.0, text="Mais sobre X."),
        # gap de 5s >= default 1.5s -- dispara quebra
        TranscriptionSegment(start=9.0, end=11.0, text="Um trecho qualquer sobre Y."),
        TranscriptionSegment(start=11.1, end=13.0, text="Mais sobre Y."),
        TranscriptionSegment(start=13.1, end=15.0, text="Encerrando Y."),
    ]
    result = segment_transcript(segs)
    assert len(result) == 2
    assert result[0].role == SEGMENT_ROLE_BEGINNING
    assert result[0].topic_change is False
    assert result[0].start == 0.0 and result[0].end == 4.0
    assert result[1].role == SEGMENT_ROLE_CONCLUSION
    assert result[1].topic_change is True
    assert result[1].split_gap_seconds == pytest.approx(5.0)
    assert result[1].split_discourse_marker is None
    assert result[1].start == 9.0 and result[1].end == 15.0


def test_segmentacao_multiplos_assuntos_via_marcador_de_discurso_sem_gap():
    segs = [
        TranscriptionSegment(start=0.0, end=2.0, text="Hoje vamos falar de X."),
        TranscriptionSegment(start=2.0, end=4.0, text="X é interessante."),
        # sem gap perceptível (start == end anterior), mas com marcador
        TranscriptionSegment(start=4.0, end=6.0, text="Agora, vamos falar de Y."),
        TranscriptionSegment(start=6.0, end=8.0, text="Y também é interessante."),
    ]
    result = segment_transcript(segs)
    assert len(result) == 2
    assert result[1].split_gap_seconds is None
    assert result[1].split_discourse_marker == "agora,"
    assert result[1].role == SEGMENT_ROLE_CONCLUSION


def test_segmentacao_tres_assuntos_meio_e_development():
    segs = [
        TranscriptionSegment(start=0.0, end=2.0, text="Comeco."),
        TranscriptionSegment(start=10.0, end=12.0, text="Meio."),
        TranscriptionSegment(start=20.0, end=22.0, text="Fim."),
    ]
    result = segment_transcript(segs)
    assert len(result) == 3
    assert result[0].role == SEGMENT_ROLE_BEGINNING
    assert result[1].role == SEGMENT_ROLE_DEVELOPMENT
    assert result[2].role == SEGMENT_ROLE_CONCLUSION


def test_segmentacao_sem_gaps_perceptiveis_e_sem_marcadores_colapsa_em_um_segmento():
    """LIMITAÇÃO DOCUMENTADA (seção 0.3 da docstring do módulo): sem
    nenhum dos dois sinais disponíveis, o resultado é um único segmento
    cobrindo o vídeo inteiro -- não existe um terceiro sinal de "força
    bruta" (ex. teto de duração) para forçar uma quebra artificial."""
    segs = [
        TranscriptionSegment(start=float(i), end=float(i) + 0.9, text=f"frase numero {i} sem marcador")
        for i in range(20)
    ]
    result = segment_transcript(segs)
    assert len(result) == 1
    assert result[0].start == 0.0
    assert result[0].end == pytest.approx(19.9)


def test_segmentacao_caso_degenerado_um_unico_segmento_bruto():
    segs = [TranscriptionSegment(start=0.0, end=1.5, text="unico.")]
    result = segment_transcript(segs)
    assert len(result) == 1
    assert result[0].role == SEGMENT_ROLE_BEGINNING
    assert result[0].topic_change is False


def test_segmentacao_rejeita_lista_vazia():
    with pytest.raises(ValueError):
        segment_transcript([])


def test_segmentacao_rejeita_gap_nao_positivo():
    segs = [TranscriptionSegment(start=0.0, end=1.0, text="x")]
    with pytest.raises(ValueError):
        segment_transcript(segs, min_topic_gap_seconds=0.0)


# ---------------------------------------------------------------------------
# 2. Cobertura -- sem buraco, sem sobreposição
# ---------------------------------------------------------------------------


def test_cobertura_sem_buraco_nem_sobreposicao_e_nenhum_segmento_bruto_perdido():
    segs = [
        TranscriptionSegment(start=0.0, end=1.0, text="a"),
        TranscriptionSegment(start=1.0, end=2.0, text="b"),
        TranscriptionSegment(start=5.0, end=6.0, text="c"),  # gap
        TranscriptionSegment(start=6.0, end=7.0, text="d"),
        TranscriptionSegment(start=7.0, end=8.0, text="e"),
        TranscriptionSegment(start=20.0, end=21.0, text="f"),  # gap
    ]
    result = segment_transcript(segs)

    # Sem sobreposição, estritamente ordenado.
    for prev, cur in zip(result, result[1:]):
        assert cur.start >= prev.end

    # Cobre exatamente do início do primeiro ao fim do último segmento bruto.
    assert result[0].start == segs[0].start
    assert result[-1].end == segs[-1].end

    # Nenhum texto de segmento bruto foi perdido ou duplicado: cada
    # palavra-marcador aparece exatamente uma vez no texto concatenado
    # total.
    all_text = " ".join(s.text for s in result)
    for raw in segs:
        assert all_text.count(raw.text) == 1


# ---------------------------------------------------------------------------
# 3. compute_cache_key -- determinístico, sensível a parâmetros
# ---------------------------------------------------------------------------


def test_cache_key_deterministico_para_mesmos_argumentos():
    k1 = compute_cache_key("t1", min_topic_gap_seconds=1.5, discourse_markers=frozenset({"a", "b"}))
    k2 = compute_cache_key("t1", min_topic_gap_seconds=1.5, discourse_markers=frozenset({"b", "a"}))
    assert k1 == k2


def test_cache_key_muda_se_transcript_cache_key_mudar():
    k1 = compute_cache_key("t1", min_topic_gap_seconds=1.5, discourse_markers=frozenset())
    k2 = compute_cache_key("t2", min_topic_gap_seconds=1.5, discourse_markers=frozenset())
    assert k1 != k2


def test_cache_key_muda_se_min_topic_gap_seconds_mudar():
    k1 = compute_cache_key("t1", min_topic_gap_seconds=1.5, discourse_markers=frozenset())
    k2 = compute_cache_key("t1", min_topic_gap_seconds=2.0, discourse_markers=frozenset())
    assert k1 != k2


def test_cache_key_muda_se_discourse_markers_mudarem():
    k1 = compute_cache_key("t1", min_topic_gap_seconds=1.5, discourse_markers=frozenset({"a"}))
    k2 = compute_cache_key("t1", min_topic_gap_seconds=1.5, discourse_markers=frozenset({"a", "b"}))
    assert k1 != k2


# ---------------------------------------------------------------------------
# 4. SemanticSegment -- validação
# ---------------------------------------------------------------------------


def test_semantic_segment_rejeita_role_desconhecido():
    with pytest.raises(ValueError):
        SemanticSegment(
            index=0, start=0.0, end=1.0, text="x", role="NAO_EXISTE",
            topic_change=False, split_gap_seconds=None, split_discourse_marker=None,
        )


# ---------------------------------------------------------------------------
# 5. Construção do SmartClipEngine
# ---------------------------------------------------------------------------


def test_construcao_rejeita_manager_invalido(database, storage, app_paths):
    with pytest.raises(TypeError):
        SmartClipEngine("nao-e-manager", database=database, storage_manager=storage, app_paths=app_paths)


def test_construcao_rejeita_min_topic_gap_seconds_nao_positivo(manager, database, storage, app_paths):
    with pytest.raises(ValueError):
        SmartClipEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            min_topic_gap_seconds=0.0,
        )


# ---------------------------------------------------------------------------
# 6. Handler -- falha honesta sem video_id
# ---------------------------------------------------------------------------


def test_handler_falha_sem_video_id(engine, audit):
    job = audit.create_job(Job(operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "job_missing_video_id"


# ---------------------------------------------------------------------------
# 7. Handler -- nunca reaciona transcrição quando artifact válido já existe
#    (teste OBRIGATÓRIO explícito do Prompt)
# ---------------------------------------------------------------------------


def test_handler_reusa_transcript_existente_sem_disparar_nova_transcricao(
    engine, audit, database, storage, video, video_file,
):
    calls = []
    engine._captions_engine._backend = _fake_transcription_backend(calls=calls)

    segments = (
        TranscriptionSegment(start=0.0, end=2.0, text="ola"),
        TranscriptionSegment(start=2.0, end=4.0, text="mundo"),
    )
    _insert_transcript_artifact(database, storage, video, segments, cache_key="ja-existe")

    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)

    assert result.target_status == JOB_READY
    assert calls == []  # nenhuma chamada ao backend de transcrição


def test_handler_segundo_job_com_mesmo_video_reusa_analise_ja_cacheada(
    engine, audit, database, storage, video,
):
    segments = (TranscriptionSegment(start=0.0, end=2.0, text="ola"),)
    _insert_transcript_artifact(database, storage, video, segments, cache_key="tk-1")

    job1 = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result1 = engine.handle_analyze_job(job1)
    assert result1.target_status == JOB_READY
    assert result1.data["cache_hit"] is False

    job2 = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result2 = engine.handle_analyze_job(job2)
    assert result2.target_status == JOB_READY
    assert result2.data["cache_hit"] is True
    assert result2.data["cache_key"] == result1.data["cache_key"]

    total_artifacts = database.list(Artifact)
    content_artifacts = [a for a in total_artifacts if a.kind == ARTIFACT_KIND_CONTENT_SEGMENTS]
    assert len(content_artifacts) == 1  # nunca duplica o artifact de análise


# ---------------------------------------------------------------------------
# 8. Handler -- aciona transcrição real (via backend fake) quando ausente
# ---------------------------------------------------------------------------


def test_handler_aciona_transcricao_quando_nenhum_artifact_existe(
    engine, audit, database, video,
):
    calls = []
    engine._captions_engine._backend = _fake_transcription_backend(
        segments=(
            TranscriptionSegment(start=0.0, end=2.0, text="Introducao do video."),
            TranscriptionSegment(start=9.0, end=11.0, text="Agora, o segundo assunto."),
        ),
        calls=calls,
    )

    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)

    assert result.target_status == JOB_READY
    assert len(calls) == 1  # transcrição foi de fato acionada

    artifact = database.get(Artifact, result.data["artifact_id"])
    assert artifact.kind == ARTIFACT_KIND_CONTENT_SEGMENTS
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    assert len(payload["segments"]) == 2
    assert payload["segments"][0]["role"] == SEGMENT_ROLE_BEGINNING
    assert payload["segments"][1]["role"] == SEGMENT_ROLE_CONCLUSION

    # a transcrição em si também foi persistida como artifact próprio
    transcript_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_TRANSCRIPT]
    assert len(transcript_artifacts) == 1


def test_handler_video_curto_um_unico_segmento_bruto_produz_um_unico_segmento_semantico(
    engine, audit, database, video,
):
    engine._captions_engine._backend = _fake_transcription_backend(
        segments=(TranscriptionSegment(start=0.0, end=3.0, text="video curto."),),
    )
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)
    assert result.target_status == JOB_READY
    artifact = database.get(Artifact, result.data["artifact_id"])
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    assert len(payload["segments"]) == 1
    assert payload["segments"][0]["role"] == SEGMENT_ROLE_BEGINNING


def test_handler_falha_honestamente_quando_transcricao_falha(engine, audit, video):
    def backend_falho(local_path, model_size, language):
        raise RuntimeError("falha simulada de transcricao")

    engine._captions_engine._backend = backend_falho
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "transcription_trigger_failed"


# ---------------------------------------------------------------------------
# 9. CHECKPOINT_ANALYZED -- somente no sucesso
# ---------------------------------------------------------------------------


def test_checkpoint_analyzed_gravado_somente_no_sucesso(engine, audit, video):
    engine._captions_engine._backend = _fake_transcription_backend(
        segments=(TranscriptionSegment(start=0.0, end=2.0, text="ok"),),
    )
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)
    assert result.target_status == JOB_READY
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_ANALYZED)


def test_checkpoint_analyzed_gravado_quando_transcript_ja_existia_e_analise_e_nova(
    engine, audit, database, storage, video,
):
    """Transcript já existente (reaproveitado, nunca re-transcrito) mas a
    ANÁLISE em si ainda não tinha sido feita -- ``cache_hit`` aqui se
    refere ao cache_key desta análise, não ao da transcrição (por isso
    ``False`` -- é a primeira vez que a segmentação semântica roda)."""
    segments = (TranscriptionSegment(start=0.0, end=2.0, text="ok"),)
    _insert_transcript_artifact(database, storage, video, segments, cache_key="tk-cache")
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)
    assert result.target_status == JOB_READY
    assert result.data["cache_hit"] is False
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_ANALYZED)

    # Rodar de novo com um NOVO Job para o mesmo vídeo agora sim reaproveita
    # a análise já persistida (cache_hit no nível da análise).
    job2 = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result2 = engine.handle_analyze_job(job2)
    assert result2.target_status == JOB_READY
    assert result2.data["cache_hit"] is True
    assert audit.has_reached_checkpoint(job2.id, CHECKPOINT_ANALYZED)


def test_checkpoint_analyzed_nao_gravado_em_falha(engine, audit, video):
    def backend_falho(local_path, model_size, language):
        raise RuntimeError("falha simulada")

    engine._captions_engine._backend = backend_falho
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)
    assert result.target_status == JOB_FAILED
    assert not audit.has_reached_checkpoint(job.id, CHECKPOINT_ANALYZED)


def test_checkpoint_analyzed_nao_gravado_quando_job_missing_video_id(engine, audit):
    job = audit.create_job(Job(operation=OPERATION_ANALYZE_CONTENT))
    result = engine.handle_analyze_job(job)
    assert result.target_status == JOB_FAILED
    assert not audit.has_reached_checkpoint(job.id, CHECKPOINT_ANALYZED)


# ---------------------------------------------------------------------------
# 10. Contrato Job/checkpoint/Artifact -- restart com novas instâncias
# ---------------------------------------------------------------------------


def test_restart_com_novas_instancias_ve_o_mesmo_resultado(app_paths, video_file):
    db1 = LocalDatabase(app_paths.database / "painel.db")
    db1.initialize()
    audit1 = OperationalAuditLog(db1)
    manager1 = EditProjectManager(db1)
    storage1 = StorageManager(app_paths, database_path=app_paths.database / "painel.db")

    src = SourceAsset(
        source_uri=str(video_file), local_path=str(video_file),
        original_name="original.mp4", fingerprint="source-fp-1",
    )
    db1.insert(src)
    v = Video(source_asset_id=src.id, name="v")
    db1.insert(v)

    captions1 = CaptionsEngine(
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths,
        audit_log=audit1, transcription_backend=_fake_transcription_backend(
            segments=(TranscriptionSegment(start=0.0, end=2.0, text="ola"),),
        ),
    )
    engine1 = SmartClipEngine(
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths,
        audit_log=audit1, captions_engine=captions1,
    )
    job = audit1.create_job(Job(video_id=v.id, operation=OPERATION_ANALYZE_CONTENT))
    result1 = engine1.handle_analyze_job(job)
    assert result1.target_status == JOB_READY

    # Reabre com instâncias TOTALMENTE novas (mesmo arquivo .db).
    db2 = LocalDatabase(app_paths.database / "painel.db")
    manager2 = EditProjectManager(db2)
    storage2 = StorageManager(app_paths, database_path=app_paths.database / "painel.db")
    audit2 = OperationalAuditLog(db2)
    captions2 = CaptionsEngine(
        manager2, database=db2, storage_manager=storage2, app_paths=app_paths, audit_log=audit2,
    )
    engine2 = SmartClipEngine(
        manager2, database=db2, storage_manager=storage2, app_paths=app_paths,
        audit_log=audit2, captions_engine=captions2,
    )

    job2 = audit2.create_job(Job(video_id=v.id, operation=OPERATION_ANALYZE_CONTENT))
    result2 = engine2.handle_analyze_job(job2)
    assert result2.target_status == JOB_READY
    assert result2.data["cache_hit"] is True
    assert result2.data["cache_key"] == result1.data["cache_key"]


# ---------------------------------------------------------------------------
# 11. Nenhuma chamada de API externa (checagem estrutural, honesta)
# ---------------------------------------------------------------------------


def test_modulo_nao_referencia_bibliotecas_de_rede():
    source = Path(sce.__file__).read_text(encoding="utf-8")
    proibidos = ["requests", "httpx", "urllib.request", "socket.", "openai", "anthropic"]
    for termo in proibidos:
        assert termo not in source, f"referência inesperada a {termo!r} em smart_clip_engine.py"


def test_modulo_nunca_reimplementa_transcricao_le_apenas_o_backend_de_captions():
    """A docstring do módulo pode MENCIONAR "faster-whisper" só como
    contexto descritivo (é o backend que ``CaptionsEngine`` já usa) --
    o que este teste realmente prova é que nenhum CÓDIGO deste módulo
    importa/invoca um backend de transcrição diretamente."""
    source = Path(sce.__file__).read_text(encoding="utf-8")
    assert "import faster_whisper" not in source
    assert "from faster_whisper" not in source
    assert "import whisper" not in source.lower()
