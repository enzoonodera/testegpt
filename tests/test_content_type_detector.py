# -*- coding: utf-8 -*-
"""PROMPT 46 -- ContentCategoryDetector (tipos de conteúdo automáticos):
testes.

Cobre: (1) vocabulário de categorias fechado; (2) nenhuma colisão com
template_engine.CONTENT_TYPES; (3) cada heurística de categoria isolada;
(4) sinal sem backend real nunca fabrica valor (gameplay); (5) nenhum
checkpoint novo gravado/referenciado; (6) override nunca apaga a
detecção automática original; (7) cache reaproveita/recalcula
corretamente; (8) encadeamento honesto até segmentação/transcrição;
(9) falha honesta; (10) restart com novas instâncias."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import _sistema.content_type_detector as ctd_module
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.captions_engine import CaptionsEngine, TranscriptionResult, TranscriptionSegment
from _sistema.content_type_detector import (
    ARTIFACT_KIND_CONTENT_CATEGORY,
    CATEGORY_GAMEPLAY,
    CATEGORY_GENERAL,
    CATEGORY_PODCAST_INTERVIEW,
    CATEGORY_TALKING_HEAD,
    CATEGORY_TUTORIAL,
    CATEGORY_VLOG,
    SMART_CLIP_CONTENT_CATEGORIES,
    SOURCE_AUTO,
    SOURCE_MANUAL_OVERRIDE,
    ActivitySample,
    ContentCategoryDetector,
    InvalidContentCategoryError,
    OPERATION_DETECT_CONTENT_CATEGORY,
    classify_segments,
    compute_cache_key,
    score_gameplay,
    score_podcast_interview,
    score_talking_head,
    score_tutorial,
    score_vlog,
    validate_content_category,
)
from _sistema.domain import (
    Artifact,
    Job,
    JOB_CHECKPOINTS,
    JOB_FAILED,
    JOB_READY,
    Project,
    SourceAsset,
    Video,
)
from _sistema.edit_project import EditProjectManager
from _sistema.smart_clip_engine import ARTIFACT_KIND_CONTENT_SEGMENTS, SmartClipEngine
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
import _sistema.template_engine as template_engine_module


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
def smart_clip_engine(manager, database, storage, app_paths, audit, captions_engine):
    return SmartClipEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, captions_engine=captions_engine,
    )


@pytest.fixture
def engine(manager, database, storage, app_paths, audit, smart_clip_engine):
    return ContentCategoryDetector(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, smart_clip_engine=smart_clip_engine,
    )


def _insert_segments_artifact(database, storage, video_, texts, *, cache_key="segments-fp-1"):
    segments = [
        {"index": i, "start": float(i * 4), "end": float(i * 4 + 3), "text": t,
         "role": "BEGINNING" if i == 0 else "DEVELOPMENT", "topic_change": i > 0,
         "split_gap_seconds": None, "split_discourse_marker": None}
        for i, t in enumerate(texts)
    ]
    payload = {
        "version": 1, "algorithm_version": "1", "transcript_artifact_id": "fake",
        "transcript_cache_key": "tk-fake", "min_topic_gap_seconds": 1.5,
        "discourse_markers": [], "segments": segments,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    temp = storage.allocate_temp(suffix=".json", create=True)
    temp.write_text(content, encoding="utf-8")
    final_path = Path(storage._app_paths.projects) / "content_segments" / video_.id / f"{cache_key}.json"
    promoted = storage.promote_to_final(temp, final_path, overwrite=True)
    artifact = Artifact(
        video_id=video_.id, kind=ARTIFACT_KIND_CONTENT_SEGMENTS, path=str(promoted),
        fingerprint=cache_key, size_bytes=len(content.encode("utf-8")),
    )
    database.insert(artifact)
    return artifact


_TUTORIAL_TEXTS = [
    "Primeiro, abra o programa no seu computador.",
    "Depois, clique em novo projeto.",
    "Em seguida, configure as opções principais.",
]

_GENERAL_TEXTS = [
    "Isso é só uma frase qualquer sem sinal nenhum.",
    "Outra frase comum, nada especial por aqui.",
]


# ---------------------------------------------------------------------------
# 1. Vocabulário fechado
# ---------------------------------------------------------------------------


def test_vocabulario_tem_exatamente_seis_categorias():
    assert SMART_CLIP_CONTENT_CATEGORIES == {
        CATEGORY_PODCAST_INTERVIEW, CATEGORY_TUTORIAL, CATEGORY_VLOG,
        CATEGORY_GAMEPLAY, CATEGORY_TALKING_HEAD, CATEGORY_GENERAL,
    }
    assert len(SMART_CLIP_CONTENT_CATEGORIES) == 6


def test_validate_content_category_aceita_vocabulario_e_rejeita_o_resto():
    for cat in SMART_CLIP_CONTENT_CATEGORIES:
        assert validate_content_category(cat) == cat
    with pytest.raises(InvalidContentCategoryError):
        validate_content_category("NAO_EXISTE")
    with pytest.raises(InvalidContentCategoryError):
        validate_content_category("FIXED_TEXT")  # nome de outro vocabulário


# ---------------------------------------------------------------------------
# 2. Nenhuma colisão com template_engine.CONTENT_TYPES
# ---------------------------------------------------------------------------


def test_vocabularios_sao_conjuntos_disjuntos():
    assert SMART_CLIP_CONTENT_CATEGORIES.isdisjoint(template_engine_module.CONTENT_TYPES)


def test_modulo_nao_importa_template_engine():
    """A docstring de módulo PODE citar ``template_engine.py`` para
    EXPLICAR a decisão de não colidir com ele (seção 0.1) -- o que este
    teste prova é que nenhuma linha de CÓDIGO real importa esse módulo."""
    codigo = _source_without_module_docstring(ctd_module)
    assert "template_engine" not in codigo


def test_modulo_nunca_usa_identificador_content_type_bruto():
    """Nenhum símbolo público do módulo se chama ``content_type`` --
    distinto de ``category``/``content category`` na prosa."""
    import inspect
    assert not hasattr(ctd_module, "content_type")
    assert not hasattr(ctd_module, "CONTENT_TYPES")
    assert not hasattr(ctd_module, "ContentTypeDetector")
    # A classe pública se chama ContentCategoryDetector, nunca ContentTypeDetector.
    assert hasattr(ctd_module, "ContentCategoryDetector")


# ---------------------------------------------------------------------------
# 3. Heurísticas isoladas
# ---------------------------------------------------------------------------


def test_score_podcast_interview_alto_com_perguntas_e_respostas_alternadas():
    score, signals = score_podcast_interview([
        "Você já parou pra pensar sobre isso?",
        "A resposta é que sim, com certeza.",
        "E por que você acha isso?",
        "Bom, no final eu percebi que sim.",
    ])
    assert score > 0.5
    assert any("question_density" in s for s in signals)


def test_score_podcast_interview_vazio_e_zero():
    score, signals = score_podcast_interview([])
    assert score == 0.0


def test_score_tutorial_alto_com_marcadores_sequenciais():
    score, _ = score_tutorial(_TUTORIAL_TEXTS)
    assert score == pytest.approx(1.0)


def test_score_vlog_alto_com_narrativa_pessoal():
    score, _ = score_vlog([
        "Hoje eu vou te mostrar como é meu dia.",
        "Cheguei em casa e minha rotina começou.",
    ])
    assert score > 0.5


def test_score_talking_head_alto_com_opiniao_direta():
    score, _ = score_talking_head([
        "Eu acho que isso é importante.",
        "Na minha opinião, esse é o ponto principal.",
    ])
    assert score > 0.5


def test_score_generico_sem_nenhum_lexico_e_zero_em_todas():
    p, _ = score_podcast_interview(_GENERAL_TEXTS)
    t, _ = score_tutorial(_GENERAL_TEXTS)
    v, _ = score_vlog(_GENERAL_TEXTS)
    g, _ = score_gameplay(_GENERAL_TEXTS)
    th, _ = score_talking_head(_GENERAL_TEXTS)
    assert p == t == v == g == th == 0.0


def test_classify_segments_cai_em_general_quando_nenhum_sinal():
    result = classify_segments(_GENERAL_TEXTS)
    assert result.category == CATEGORY_GENERAL
    assert result.confidence == pytest.approx(1.0)


def test_classify_segments_nunca_forca_categoria_com_sinal_fraco_isolado():
    """Uma única menção fraca e ambígua não deve dominar sobre GENERAL
    sem motivo -- este teste prova que o score realmente reflete a
    densidade, não presença binária isolada."""
    textos = ["Uma frase qualquer.", "Mais uma frase qualquer.", "E outra frase qualquer.",
              "Só mais uma frase qualquer.", "Nesse jogo tinha uma coisa engraçada."]
    result = classify_segments(textos)
    # 1 de 5 segmentos com léxico de gameplay -> density=0.2 * cap 0.5 = 0.1;
    # GENERAL = 1 - 0.1 = 0.9 -- GENERAL vence com folga.
    assert result.category == CATEGORY_GENERAL


# ---------------------------------------------------------------------------
# 4. Sinal sem backend real nunca fabrica valor (gameplay)
# ---------------------------------------------------------------------------


def test_score_gameplay_sem_backend_real_e_limitado_pelo_teto():
    score, signals = score_gameplay(["Nesse jogo o boss é bem difícil.", "Consegui um loot incrível."])
    assert score <= 0.5 + 1e-9
    assert "visual_signal=unavailable" in signals
    assert "audio_signal=unavailable" in signals


def test_score_gameplay_com_backend_real_disponivel_ultrapassa_o_teto():
    sample = ActivitySample(available=True, activity=0.9)
    score, signals = score_gameplay(
        ["Nesse jogo o boss é bem difícil.", "Consegui um loot incrível."],
        visual_sample=sample,
    )
    assert score > 0.5
    assert any("real_signal_component" in s for s in signals)


def test_handler_usa_backend_padrao_e_nunca_fabrica_sinal_de_gameplay(
    engine, audit, database, storage, video,
):
    _insert_segments_artifact(database, storage, video, ["Nesse jogo o boss é bem difícil."])
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result = engine.handle_detect_job(job)
    assert result.target_status == JOB_READY
    artifact = database.get(Artifact, result.data["artifact_id"])
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    gameplay_signals = payload["signals"][CATEGORY_GAMEPLAY]
    assert "visual_signal=unavailable" in gameplay_signals
    assert "audio_signal=unavailable" in gameplay_signals


# ---------------------------------------------------------------------------
# 5. Nenhum checkpoint novo
# ---------------------------------------------------------------------------


def test_handler_nunca_grava_nenhum_checkpoint(engine, audit, database, storage, video):
    _insert_segments_artifact(database, storage, video, _TUTORIAL_TEXTS)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result = engine.handle_detect_job(job)
    assert result.target_status == JOB_READY
    for checkpoint in JOB_CHECKPOINTS:
        assert not audit.has_reached_checkpoint(job.id, checkpoint)


def _source_without_module_docstring(module) -> str:
    source = Path(module.__file__).read_text(encoding="utf-8")
    first = source.find('"""')
    second = source.find('"""', first + 3)
    if first == -1 or second == -1:
        return source
    return source[:first] + source[second + 3:]


def test_modulo_nao_referencia_checkpoints_novos():
    codigo = _source_without_module_docstring(ctd_module)
    assert ".record_checkpoint(" not in codigo
    assert not any(name.startswith("CHECKPOINT_") for name in dir(ctd_module))


# ---------------------------------------------------------------------------
# 6. Override nunca apaga a detecção automática original
# ---------------------------------------------------------------------------


def test_override_nunca_apaga_a_deteccao_automatica_original(
    engine, audit, database, storage, video,
):
    _insert_segments_artifact(database, storage, video, _TUTORIAL_TEXTS)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result = engine.handle_detect_job(job)
    assert result.data["category"] == CATEGORY_TUTORIAL

    auto_before = engine.get_latest_auto_classification(video.id)
    assert auto_before is not None
    assert auto_before["source"] == SOURCE_AUTO
    assert auto_before["category"] == CATEGORY_TUTORIAL

    engine.register_override(video.id, CATEGORY_VLOG, reason="usuário corrigiu manualmente")

    # A auto original continua exatamente a mesma, recuperável.
    auto_after = engine.get_latest_auto_classification(video.id)
    assert auto_after == auto_before

    # O valor EFETIVO agora é o override.
    effective = engine.get_latest_classification(video.id)
    assert effective["source"] == SOURCE_MANUAL_OVERRIDE
    assert effective["category"] == CATEGORY_VLOG

    # Ambos os Artifacts existem -- nada foi sobrescrito/apagado.
    todos = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CONTENT_CATEGORY]
    assert len(todos) == 2


def test_register_override_rejeita_categoria_fora_do_vocabulario(engine):
    with pytest.raises(InvalidContentCategoryError):
        engine.register_override("qualquer-video-id", "NAO_EXISTE")


def test_multiplos_overrides_preservam_todo_o_historico(engine, audit, database, storage, video):
    _insert_segments_artifact(database, storage, video, _TUTORIAL_TEXTS)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    engine.handle_detect_job(job)

    engine.register_override(video.id, CATEGORY_VLOG)
    engine.register_override(video.id, CATEGORY_GAMEPLAY)

    auto = engine.get_latest_auto_classification(video.id)
    assert auto["category"] == CATEGORY_TUTORIAL  # a auto original nunca muda

    effective = engine.get_latest_classification(video.id)
    assert effective["category"] == CATEGORY_GAMEPLAY  # o override mais recente vence

    todos = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CONTENT_CATEGORY]
    assert len(todos) == 3  # 1 auto + 2 overrides, nada apagado


# ---------------------------------------------------------------------------
# 7. Cache -- reaproveita/recalcula/nunca duplica
# ---------------------------------------------------------------------------


def test_cache_reaproveita_entre_dois_jobs_quando_nada_muda(engine, audit, database, storage, video):
    _insert_segments_artifact(database, storage, video, _TUTORIAL_TEXTS)
    job1 = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result1 = engine.handle_detect_job(job1)
    assert result1.data["cache_hit"] is False

    job2 = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result2 = engine.handle_detect_job(job2)
    assert result2.data["cache_hit"] is True
    assert result2.data["cache_key"] == result1.data["cache_key"]

    artefatos = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CONTENT_CATEGORY]
    assert len(artefatos) == 1


def test_cache_recalcula_quando_lexico_muda(
    manager, database, storage, app_paths, audit, smart_clip_engine, video,
):
    _insert_segments_artifact(database, storage, video, _TUTORIAL_TEXTS)

    engine1 = ContentCategoryDetector(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, smart_clip_engine=smart_clip_engine,
    )
    job1 = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result1 = engine1.handle_detect_job(job1)

    outro_lexico = frozenset({"algo-totalmente-diferente"})
    engine2 = ContentCategoryDetector(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, smart_clip_engine=smart_clip_engine, vlog_lexicon=outro_lexico,
    )
    job2 = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result2 = engine2.handle_detect_job(job2)

    assert result2.data["cache_hit"] is False
    assert result2.data["cache_key"] != result1.data["cache_key"]

    artefatos = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CONTENT_CATEGORY]
    assert len(artefatos) == 2


def test_cache_key_deterministico_e_sensivel_a_segmentacao_de_origem():
    args = dict(
        gameplay_weak_signal_cap=0.5, question_markers=frozenset({"?"}), answer_cue_lexicon=frozenset(),
        tutorial_lexicon=frozenset(), vlog_lexicon=frozenset(), gameplay_lexicon=frozenset(),
        talking_head_lexicon=frozenset(),
    )
    k1 = compute_cache_key("seg-a", **args)
    k2 = compute_cache_key("seg-a", **args)
    k3 = compute_cache_key("seg-b", **args)
    assert k1 == k2
    assert k1 != k3


# ---------------------------------------------------------------------------
# 8. Encadeamento honesto até segmentação/transcrição
# ---------------------------------------------------------------------------


def test_handler_aciona_segmentacao_e_transcricao_em_cadeia_quando_nada_existe(
    engine, audit, database, video,
):
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result = engine.handle_detect_job(job)
    assert result.target_status == JOB_READY

    segments_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CONTENT_SEGMENTS]
    assert len(segments_artifacts) == 1
    classificacoes = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CONTENT_CATEGORY]
    assert len(classificacoes) == 1


def test_handler_falha_honestamente_quando_segmentacao_falha(
    manager, database, storage, app_paths, audit, video,
):
    def backend_falho(local_path, model_size, language):
        raise RuntimeError("falha simulada de transcricao")

    captions = CaptionsEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, transcription_backend=backend_falho,
    )
    smart_clip = SmartClipEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, captions_engine=captions,
    )
    engine = ContentCategoryDetector(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, smart_clip_engine=smart_clip,
    )
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result = engine.handle_detect_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "segmentation_not_ready"


# ---------------------------------------------------------------------------
# 9. Falha honesta / construção
# ---------------------------------------------------------------------------


def test_handler_falha_sem_video_id(engine, audit):
    job = audit.create_job(Job(operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result = engine.handle_detect_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "job_missing_video_id"


def test_construcao_rejeita_manager_invalido(database, storage, app_paths):
    with pytest.raises(TypeError):
        ContentCategoryDetector("nao-e-manager", database=database, storage_manager=storage, app_paths=app_paths)


def test_construcao_rejeita_cap_fora_do_intervalo(manager, database, storage, app_paths):
    with pytest.raises(ValueError):
        ContentCategoryDetector(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            gameplay_weak_signal_cap=1.5,
        )


# ---------------------------------------------------------------------------
# 10. Restart com novas instâncias
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
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths, audit_log=audit1,
        transcription_backend=_fake_transcription_backend(
            segments=(TranscriptionSegment(start=0.0, end=4.0, text="Primeiro, abra o programa."),),
        ),
    )
    smart_clip1 = SmartClipEngine(
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths,
        audit_log=audit1, captions_engine=captions1,
    )
    engine1 = ContentCategoryDetector(
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths,
        audit_log=audit1, smart_clip_engine=smart_clip1,
    )
    job = audit1.create_job(Job(video_id=v.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result1 = engine1.handle_detect_job(job)
    assert result1.target_status == JOB_READY

    db2 = LocalDatabase(app_paths.database / "painel.db")
    manager2 = EditProjectManager(db2)
    storage2 = StorageManager(app_paths, database_path=app_paths.database / "painel.db")
    audit2 = OperationalAuditLog(db2)
    captions2 = CaptionsEngine(manager2, database=db2, storage_manager=storage2, app_paths=app_paths, audit_log=audit2)
    smart_clip2 = SmartClipEngine(
        manager2, database=db2, storage_manager=storage2, app_paths=app_paths,
        audit_log=audit2, captions_engine=captions2,
    )
    engine2 = ContentCategoryDetector(
        manager2, database=db2, storage_manager=storage2, app_paths=app_paths,
        audit_log=audit2, smart_clip_engine=smart_clip2,
    )
    job2 = audit2.create_job(Job(video_id=v.id, operation=OPERATION_DETECT_CONTENT_CATEGORY))
    result2 = engine2.handle_detect_job(job2)
    assert result2.target_status == JOB_READY
    assert result2.data["cache_hit"] is True
    assert result2.data["cache_key"] == result1.data["cache_key"]
