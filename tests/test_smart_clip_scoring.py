# -*- coding: utf-8 -*-
"""PROMPT 45 -- SmartClipScoringEngine (scores internos, sem API externa):
testes.

Cobre: (1) cada sub-score isolado (contexto, ritmo de fala, hook,
completude); (2) combinação (``combine_overall``) com e sem sinais
ausentes; (3) sinais sem backend real (visual/áudio) NUNCA fabricam
valor -- ficam ``None`` e são excluídos/renormalizados; (4) teste
estrutural: nenhum lugar do código nomeia o score como "visualizações"/
"retenção real"; (5) o handler gera exatamente um candidato por segmento
(nada descartado/duplicado); (6) cache reaproveita quando nada muda,
recalcula quando um parâmetro muda; (7) nenhum checkpoint novo é
gravado/inventado; (8) contrato Job/Artifact -- restart com novas
instâncias; (9) encadeamento honesto até a transcrição quando nada
existe ainda."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import _sistema.smart_clip_engine as sce_module
import _sistema.smart_clip_scoring as scs_module
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.captions_engine import CaptionsEngine, TranscriptionResult, TranscriptionSegment
from _sistema.control_manager import ControlManager
from _sistema.domain import (
    Artifact,
    CHECKPOINT_ANALYZED,
    Job,
    JOB_FAILED,
    JOB_READY,
    JOB_CHECKPOINTS,
    Project,
    SourceAsset,
    Video,
)
from _sistema.edit_project import EditProjectManager
from _sistema.job_engine import JobEngine
from _sistema.smart_clip_engine import (
    ARTIFACT_KIND_CONTENT_SEGMENTS,
    OPERATION_ANALYZE_CONTENT,
    SEGMENT_ROLE_BEGINNING,
    SEGMENT_ROLE_CONCLUSION,
    SEGMENT_ROLE_DEVELOPMENT,
    SmartClipEngine,
)
from _sistema.smart_clip_scoring import (
    ARTIFACT_KIND_CLIP_CANDIDATES,
    DEFAULT_SCORE_WEIGHTS,
    OPERATION_SCORE_CANDIDATES,
    ActivitySample,
    SmartClipScoringEngine,
    combine_overall,
    compute_cache_key,
    score_completion,
    score_context,
    score_hook,
    score_speech_rate,
)
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
    return SmartClipScoringEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, smart_clip_engine=smart_clip_engine,
    )


def _insert_segments_artifact(database, storage, video_, segments, *, cache_key="segments-fp-1"):
    """Escreve um Artifact ``content_segments_track`` mínimo diretamente,
    no mesmo formato produzido por ``SmartClipEngine._write_and_register_artifact``."""
    payload = {
        "version": 1,
        "algorithm_version": "1",
        "transcript_artifact_id": "fake",
        "transcript_cache_key": "tk-fake",
        "min_topic_gap_seconds": 1.5,
        "discourse_markers": [],
        "segments": segments,
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


_ONE_SEGMENT = [
    {"index": 0, "start": 0.0, "end": 4.0, "text": "Você sabia que isso vai mudar sua vida?",
     "role": SEGMENT_ROLE_BEGINNING, "topic_change": False,
     "split_gap_seconds": None, "split_discourse_marker": None},
]

_THREE_SEGMENTS = [
    {"index": 0, "start": 0.0, "end": 4.0, "text": "Você sabia que isso vai mudar sua vida?",
     "role": SEGMENT_ROLE_BEGINNING, "topic_change": False,
     "split_gap_seconds": None, "split_discourse_marker": None},
    {"index": 1, "start": 4.0, "end": 8.0, "text": "e entao a gente continuou explicando",
     "role": SEGMENT_ROLE_DEVELOPMENT, "topic_change": True,
     "split_gap_seconds": 2.0, "split_discourse_marker": None},
    {"index": 2, "start": 8.0, "end": 12.0, "text": "E foi assim que tudo terminou.",
     "role": SEGMENT_ROLE_CONCLUSION, "topic_change": True,
     "split_gap_seconds": None, "split_discourse_marker": "agora,"},
]


# ---------------------------------------------------------------------------
# 1. Sub-scores isolados
# ---------------------------------------------------------------------------


def test_score_context_beginning_sem_topic_change():
    assert score_context(SEGMENT_ROLE_BEGINNING, False) == pytest.approx(0.75)


def test_score_context_development_com_topic_change():
    assert score_context(SEGMENT_ROLE_DEVELOPMENT, True) == pytest.approx(0.7)


def test_score_context_development_sem_topic_change_e_o_mais_baixo():
    v1 = score_context(SEGMENT_ROLE_DEVELOPMENT, False)
    v2 = score_context(SEGMENT_ROLE_BEGINNING, True)
    assert v1 < v2


def test_score_speech_rate_dentro_da_faixa_ideal_e_maximo():
    assert score_speech_rate(10, 4.0, min_wps=1.5, max_wps=3.5) == 1.0


def test_score_speech_rate_muito_lento_cai_proporcionalmente():
    v = score_speech_rate(2, 4.0, min_wps=1.5, max_wps=3.5)  # 0.5 wps
    assert 0.0 < v < 1.0


def test_score_speech_rate_muito_rapido_cai_a_zero():
    v = score_speech_rate(40, 4.0, min_wps=1.5, max_wps=3.5)  # 10 wps
    assert v == 0.0


def test_score_speech_rate_sem_palavras_e_zero():
    assert score_speech_rate(0, 4.0) == 0.0


def test_score_speech_rate_duracao_zero_e_zero_nunca_divide_por_zero():
    assert score_speech_rate(5, 0.0) == 0.0


def test_score_hook_detecta_multiplas_categorias_e_reporta_sinais():
    score, signals = score_hook("Você sabia que isso vai mudar sua vida? Um dia aconteceu comigo algo incrível.")
    assert score > 0.0
    assert "question" in signals
    assert "hook" in signals
    assert "story" in signals
    assert "emotion" in signals


def test_score_hook_sem_nenhum_sinal_e_zero_honesto():
    score, signals = score_hook("Isso é uma frase qualquer sem nada especial.")
    assert score == 0.0
    assert signals == ()


def test_score_completion_termina_com_pontuacao_e_conclusion_e_maximo():
    assert score_completion("E foi assim que tudo terminou.", SEGMENT_ROLE_CONCLUSION) == 1.0


def test_score_completion_frase_cortada_no_meio_e_baixo():
    assert score_completion("e entao a gente foi", SEGMENT_ROLE_DEVELOPMENT) == pytest.approx(0.3)


def test_score_completion_pontuacao_sem_conclusion_e_intermediario():
    v = score_completion("Essa é uma frase completa.", SEGMENT_ROLE_DEVELOPMENT)
    assert v == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# 2. combine_overall -- com e sem sinais ausentes
# ---------------------------------------------------------------------------


def test_combine_overall_com_todos_os_sinais_disponiveis():
    v = combine_overall(
        {"hook_score": 1.0, "context_score": 1.0, "speech_score": 1.0,
         "completion_score": 1.0, "visual_score": 1.0, "audio_score": 1.0},
    )
    assert v == pytest.approx(1.0)


def test_combine_overall_renormaliza_quando_visual_e_audio_ausentes():
    v = combine_overall(
        {"hook_score": 1.0, "context_score": 1.0, "speech_score": 1.0,
         "completion_score": 1.0, "visual_score": None, "audio_score": None},
    )
    assert v == pytest.approx(1.0)  # todos os disponíveis são 1.0 -- renormalizado ainda dá 1.0


def test_combine_overall_ausencia_nunca_e_tratada_como_zero():
    com_ausencia = combine_overall(
        {"hook_score": 0.5, "context_score": 0.5, "speech_score": 0.5,
         "completion_score": 0.5, "visual_score": None, "audio_score": None},
    )
    com_zero_fabricado = combine_overall(
        {"hook_score": 0.5, "context_score": 0.5, "speech_score": 0.5,
         "completion_score": 0.5, "visual_score": 0.0, "audio_score": 0.0},
    )
    # Se ausência fosse tratada como 0.0 (fabricado), o resultado seria
    # menor -- a renormalização correta produz o mesmo 0.5 de todos os
    # outros sinais, nunca puxado pra baixo por um sinal que não existe.
    assert com_ausencia == pytest.approx(0.5)
    assert com_zero_fabricado < com_ausencia


def test_combine_overall_todos_ausentes_devolve_zero_sem_excecao():
    assert combine_overall({"visual_score": None, "audio_score": None}) == 0.0


# ---------------------------------------------------------------------------
# 3. compute_cache_key -- determinístico, sensível a parâmetros
# ---------------------------------------------------------------------------


def _cache_key_args(**overrides):
    base = dict(
        weights=DEFAULT_SCORE_WEIGHTS, speech_rate_min_wps=1.5, speech_rate_max_wps=3.5,
        question_markers=frozenset({"?"}), answer_cue_lexicon=frozenset(), hook_lexicon=frozenset(),
        story_lexicon=frozenset(), emotion_lexicon=frozenset(), strong_phrase_lexicon=frozenset(),
    )
    base.update(overrides)
    return base


def test_cache_key_deterministico():
    k1 = compute_cache_key("tk", **_cache_key_args())
    k2 = compute_cache_key("tk", **_cache_key_args())
    assert k1 == k2


def test_cache_key_muda_se_segments_cache_key_mudar():
    k1 = compute_cache_key("tk1", **_cache_key_args())
    k2 = compute_cache_key("tk2", **_cache_key_args())
    assert k1 != k2


def test_cache_key_muda_se_pesos_mudarem():
    outros_pesos = dict(DEFAULT_SCORE_WEIGHTS)
    outros_pesos["hook_score"] = 0.99
    k1 = compute_cache_key("tk", **_cache_key_args())
    k2 = compute_cache_key("tk", **_cache_key_args(weights=outros_pesos))
    assert k1 != k2


def test_cache_key_muda_se_faixa_de_ritmo_mudar():
    k1 = compute_cache_key("tk", **_cache_key_args())
    k2 = compute_cache_key("tk", **_cache_key_args(speech_rate_max_wps=5.0))
    assert k1 != k2


# ---------------------------------------------------------------------------
# 4. Sinais sem backend real NUNCA fabricam valor
# ---------------------------------------------------------------------------


def test_backend_padrao_visual_nunca_fabrica_valor(engine, audit, database, storage, video):
    _insert_segments_artifact(database, storage, video, _ONE_SEGMENT)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result = engine.handle_score_job(job)
    assert result.target_status == JOB_READY
    artifact = database.get(Artifact, result.data["artifact_id"])
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    assert payload["candidates"][0]["visual_score"] is None
    assert payload["candidates"][0]["audio_score"] is None


def test_backend_injetado_com_sinal_real_e_usado_e_nunca_none(
    manager, database, storage, app_paths, audit, smart_clip_engine, video,
):
    def fake_visual(local_path, start, end):
        return ActivitySample(available=True, activity=0.8)

    def fake_audio(local_path, start, end):
        return ActivitySample(available=True, activity=0.4)

    engine = SmartClipScoringEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths, audit_log=audit,
        smart_clip_engine=smart_clip_engine,
        visual_activity_backend=fake_visual, audio_activity_backend=fake_audio,
    )
    _insert_segments_artifact(database, storage, video, _ONE_SEGMENT)
    job = audit.create_job(Job(
        video_id=video.id, operation=OPERATION_SCORE_CANDIDATES, extra={"local_path": "/tmp/x.mp4"},
    ))
    result = engine.handle_score_job(job)
    assert result.target_status == JOB_READY
    artifact = database.get(Artifact, result.data["artifact_id"])
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    assert payload["candidates"][0]["visual_score"] == pytest.approx(0.8)
    assert payload["candidates"][0]["audio_score"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# 5. Teste estrutural: nunca chama o score de "visualizações"/"retenção real"
# ---------------------------------------------------------------------------


def _source_without_module_docstring(module) -> str:
    """Remove só a docstring de módulo (onde é legítimo EXPLICAR a regra
    "nunca chame de X") antes de procurar por violações reais no
    código -- nunca confunde "documentar a proibição" com "violar a
    proibição"."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    first = source.find('"""')
    second = source.find('"""', first + 3)
    if first == -1 or second == -1:
        return source
    return source[:first] + source[second + 3:]


def test_modulo_nunca_nomeia_score_como_visualizacoes_ou_retencao_real():
    codigo = _source_without_module_docstring(scs_module).lower()
    for termo_proibido in ("visualizações", "visualizacoes", "views", "retenção real", "retencao real"):
        assert termo_proibido not in codigo, f"termo proibido {termo_proibido!r} encontrado fora da docstring de módulo"


def test_campos_do_candidato_nunca_usam_nomes_proibidos():
    proibidos = {"views", "visualizacoes", "retencao_real", "watch_time"}
    from _sistema.smart_clip_scoring import ClipCandidate
    campos = {f for f in ClipCandidate.__dataclass_fields__}
    assert proibidos.isdisjoint(campos)


# ---------------------------------------------------------------------------
# 6. Um candidato por segmento -- nada descartado/duplicado
# ---------------------------------------------------------------------------


def test_handler_gera_exatamente_um_candidato_por_segmento(engine, audit, database, storage, video):
    _insert_segments_artifact(database, storage, video, _THREE_SEGMENTS)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result = engine.handle_score_job(job)
    assert result.target_status == JOB_READY
    assert result.data["candidate_count"] == 3
    artifact = database.get(Artifact, result.data["artifact_id"])
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    assert len(payload["candidates"]) == 3
    indices = [c["index"] for c in payload["candidates"]]
    assert indices == [0, 1, 2]  # nenhuma reordenação, nenhuma duplicata


def test_candidato_usa_exatamente_os_limites_do_segmento_de_origem_sem_expandir(
    engine, audit, database, storage, video,
):
    _insert_segments_artifact(database, storage, video, _THREE_SEGMENTS)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result = engine.handle_score_job(job)
    artifact = database.get(Artifact, result.data["artifact_id"])
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    for candidato, segmento_original in zip(payload["candidates"], _THREE_SEGMENTS):
        assert candidato["start"] == segmento_original["start"]
        assert candidato["end"] == segmento_original["end"]


# ---------------------------------------------------------------------------
# 7. Cache -- reaproveita quando nada muda, recalcula quando muda
# ---------------------------------------------------------------------------


def test_cache_reaproveita_entre_dois_jobs_quando_nada_muda(engine, audit, database, storage, video):
    _insert_segments_artifact(database, storage, video, _ONE_SEGMENT)
    job1 = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result1 = engine.handle_score_job(job1)
    assert result1.data["cache_hit"] is False

    job2 = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result2 = engine.handle_score_job(job2)
    assert result2.data["cache_hit"] is True
    assert result2.data["cache_key"] == result1.data["cache_key"]

    candidatos_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CLIP_CANDIDATES]
    assert len(candidatos_artifacts) == 1  # nunca duplica


def test_cache_recalcula_quando_pesos_mudam(
    manager, database, storage, app_paths, audit, smart_clip_engine, video,
):
    _insert_segments_artifact(database, storage, video, _ONE_SEGMENT)

    engine1 = SmartClipScoringEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths, audit_log=audit,
        smart_clip_engine=smart_clip_engine,
    )
    job1 = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result1 = engine1.handle_score_job(job1)

    outros_pesos = dict(DEFAULT_SCORE_WEIGHTS)
    outros_pesos["hook_score"] = 0.99
    engine2 = SmartClipScoringEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths, audit_log=audit,
        smart_clip_engine=smart_clip_engine, weights=outros_pesos,
    )
    job2 = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result2 = engine2.handle_score_job(job2)

    assert result2.data["cache_hit"] is False
    assert result2.data["cache_key"] != result1.data["cache_key"]

    candidatos_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CLIP_CANDIDATES]
    assert len(candidatos_artifacts) == 2  # dois cache_keys diferentes -- dois artifacts


# ---------------------------------------------------------------------------
# 8. Nenhum checkpoint novo é gravado/inventado
# ---------------------------------------------------------------------------


def test_handler_nunca_grava_nenhum_checkpoint(engine, audit, database, storage, video):
    _insert_segments_artifact(database, storage, video, _ONE_SEGMENT)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result = engine.handle_score_job(job)
    assert result.target_status == JOB_READY
    for checkpoint in JOB_CHECKPOINTS:
        assert not audit.has_reached_checkpoint(job.id, checkpoint)


def test_modulo_nao_referencia_checkpoints_novos():
    """A docstring de módulo PODE explicar por que nenhum checkpoint é
    gravado (cita ``record_checkpoint``/``CHECKPOINT_ANALYZED`` só como
    prosa) -- o que este teste prova é que nenhuma CHAMADA real a
    ``record_checkpoint`` existe no código."""
    codigo = _source_without_module_docstring(scs_module)
    assert ".record_checkpoint(" not in codigo
    assert not any(name.startswith("CHECKPOINT_") for name in dir(scs_module))


# ---------------------------------------------------------------------------
# 9. Handler -- falha honesta / acionamento em cadeia até a transcrição
# ---------------------------------------------------------------------------


def test_handler_falha_sem_video_id(engine, audit):
    job = audit.create_job(Job(operation=OPERATION_SCORE_CANDIDATES))
    result = engine.handle_score_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "job_missing_video_id"


def test_handler_aciona_segmentacao_e_transcricao_em_cadeia_quando_nada_existe(
    engine, audit, database, video,
):
    """Nem o content_segments_track NEM o transcript_internal existem
    ainda -- o handler precisa acionar Segmentação (Prompt 44), que por
    sua vez aciona Transcrição (Prompt 31), sem duplicar nenhuma das
    duas lógicas."""
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result = engine.handle_score_job(job)
    assert result.target_status == JOB_READY
    assert result.data["candidate_count"] >= 1

    segments_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CONTENT_SEGMENTS]
    assert len(segments_artifacts) == 1
    candidatos_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CLIP_CANDIDATES]
    assert len(candidatos_artifacts) == 1


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
    engine = SmartClipScoringEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, smart_clip_engine=smart_clip,
    )
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_SCORE_CANDIDATES))
    result = engine.handle_score_job(job)
    assert result.target_status == JOB_FAILED
    # JobEngine.advance() já captura a exceção do handler de transcrição e
    # pousa aquele Job em FAILED sozinho (nunca propaga como
    # JobHandlerError até aqui) -- por isso o motivo observado é
    # "segmentation_not_ready" (a segmentação nunca terminou com
    # sucesso), não "segmentation_trigger_failed" (que cobriria uma
    # falha de CONTRATO do JobEngine em si, não uma exceção de
    # aplicação). Ambos os ramos são honestos; este é o que de fato
    # ocorre aqui.
    assert result.data["reason"] == "segmentation_not_ready"


# ---------------------------------------------------------------------------
# 10. Construção -- validação
# ---------------------------------------------------------------------------


def test_construcao_rejeita_manager_invalido(database, storage, app_paths):
    with pytest.raises(TypeError):
        SmartClipScoringEngine("nao-e-manager", database=database, storage_manager=storage, app_paths=app_paths)


def test_construcao_rejeita_faixa_de_ritmo_invertida(manager, database, storage, app_paths):
    with pytest.raises(ValueError):
        SmartClipScoringEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            speech_rate_min_wps=5.0, speech_rate_max_wps=1.0,
        )


def test_construcao_rejeita_weights_vazio(manager, database, storage, app_paths):
    with pytest.raises(ValueError):
        SmartClipScoringEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths, weights={},
        )


# ---------------------------------------------------------------------------
# 11. Contrato Job/Artifact -- restart com novas instâncias
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
            segments=(TranscriptionSegment(start=0.0, end=4.0, text="Você sabia que isso muda tudo?"),),
        ),
    )
    smart_clip1 = SmartClipEngine(
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths,
        audit_log=audit1, captions_engine=captions1,
    )
    engine1 = SmartClipScoringEngine(
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths,
        audit_log=audit1, smart_clip_engine=smart_clip1,
    )
    job = audit1.create_job(Job(video_id=v.id, operation=OPERATION_SCORE_CANDIDATES))
    result1 = engine1.handle_score_job(job)
    assert result1.target_status == JOB_READY

    # Reabre com instâncias TOTALMENTE novas (mesmo arquivo .db).
    db2 = LocalDatabase(app_paths.database / "painel.db")
    manager2 = EditProjectManager(db2)
    storage2 = StorageManager(app_paths, database_path=app_paths.database / "painel.db")
    audit2 = OperationalAuditLog(db2)
    captions2 = CaptionsEngine(manager2, database=db2, storage_manager=storage2, app_paths=app_paths, audit_log=audit2)
    smart_clip2 = SmartClipEngine(
        manager2, database=db2, storage_manager=storage2, app_paths=app_paths,
        audit_log=audit2, captions_engine=captions2,
    )
    engine2 = SmartClipScoringEngine(
        manager2, database=db2, storage_manager=storage2, app_paths=app_paths,
        audit_log=audit2, smart_clip_engine=smart_clip2,
    )
    job2 = audit2.create_job(Job(video_id=v.id, operation=OPERATION_SCORE_CANDIDATES))
    result2 = engine2.handle_score_job(job2)
    assert result2.target_status == JOB_READY
    assert result2.data["cache_hit"] is True
    assert result2.data["cache_key"] == result1.data["cache_key"]
