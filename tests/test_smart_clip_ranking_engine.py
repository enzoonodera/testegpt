# -*- coding: utf-8 -*-
"""PROMPT 47 -- ClipRankingEngine (expansão de bordas, ranking e supressão
de sobreposição): testes.

Cobre: (1) todos os campos do roadmap presentes + duration correto; (2)
cada um dos três motivos de expansão isoladamente, com teste negativo
correspondente; (3) teto de expansão nunca ultrapassado mesmo com
múltiplos motivos simultâneos; (4) expansão nunca inventa texto; (5)
ranking determinístico; (6) supressão de sobreposição -- suprimido
auditável, sobreposição leve aceita ambos; (7) nenhum checkpoint novo
gravado/referenciado; (8) cache reaproveita/recalcula/nunca duplica; (9)
encadeamento honesto até scoring/segmentação/transcrição quando nada
existe; (10) falha honesta sem video_id ou quando etapa anterior falha;
(11) restart com novas instâncias vê o mesmo resultado; (12)
construção -- validação."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import _sistema.smart_clip_ranking_engine as scr_module
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.captions_engine import CaptionsEngine, TranscriptionResult, TranscriptionSegment
from _sistema.control_manager import ControlManager
from _sistema.domain import (
    Artifact,
    Job,
    JOB_FAILED,
    JOB_READY,
    Project,
    SourceAsset,
    Video,
)
from _sistema.edit_project import EditProjectManager
from _sistema.smart_clip_engine import (
    ARTIFACT_KIND_CONTENT_SEGMENTS,
    SEGMENT_ROLE_BEGINNING,
    SEGMENT_ROLE_CONCLUSION,
    SEGMENT_ROLE_DEVELOPMENT,
    SmartClipEngine,
)
from _sistema.smart_clip_ranking_engine import (
    ARTIFACT_KIND_RANKED_CANDIDATES,
    COMPLETION_EXPANSION_THRESHOLD_PADRAO,
    MAX_OVERLAP_RATIO_PADRAO,
    OPERATION_RANK_CANDIDATES,
    ClipRankingEngine,
    RankedClipCandidate,
    coverage_ratio,
    plan_expansion,
    rank_and_suppress,
)
from _sistema.smart_clip_scoring import ARTIFACT_KIND_CLIP_CANDIDATES, SmartClipScoringEngine
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


def _fake_transcription_backend(segments=None, language="pt"):
    if segments is None:
        segments = (TranscriptionSegment(start=0.0, end=1.5, text="ola mundo"),)

    def backend(local_path, model_size, language_arg):
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
def scoring_engine(manager, database, storage, app_paths, audit, smart_clip_engine):
    return SmartClipScoringEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, smart_clip_engine=smart_clip_engine,
    )


@pytest.fixture
def engine(manager, database, storage, app_paths, audit, scoring_engine):
    return ClipRankingEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, scoring_engine=scoring_engine,
    )


# ---------------------------------------------------------------------------
# Helpers -- construção manual de ClipCandidate (dict) e inserção de Artifact
# ---------------------------------------------------------------------------


def _candidate_dict(
    index, start, end, text, role, topic_change, *,
    completion_score=0.7, hook_score=0.0, context_score=0.5, speech_score=0.5,
    visual_score=None, audio_score=None, overall_score=0.5, reason="teste",
):
    return {
        "index": index, "start": start, "end": end, "text": text, "role": role,
        "topic_change": topic_change, "hook_score": hook_score, "hook_signals": [],
        "context_score": context_score, "speech_score": speech_score,
        "completion_score": completion_score, "visual_score": visual_score,
        "audio_score": audio_score, "overall_score": overall_score, "reason": reason,
    }


def _insert_candidates_artifact(database, storage, video_, candidates, *, cache_key="candidates-fp-1"):
    """Escreve um Artifact ``clip_candidates_track`` mínimo diretamente, no
    mesmo formato produzido por ``SmartClipScoringEngine._write_and_register_artifact``."""
    payload = {
        "version": 1, "algorithm_version": "1",
        "content_segments_artifact_id": "fake",
        "content_segments_cache_key": "segments-fp-fake",
        "weights": {}, "speech_rate_min_wps": 1.5, "speech_rate_max_wps": 3.5,
        "candidates": candidates,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    temp = storage.allocate_temp(suffix=".json", create=True)
    temp.write_text(content, encoding="utf-8")
    final_path = Path(storage._app_paths.projects) / "clip_candidates" / video_.id / f"{cache_key}.json"
    promoted = storage.promote_to_final(temp, final_path, overwrite=True)
    artifact = Artifact(
        video_id=video_.id, kind=ARTIFACT_KIND_CLIP_CANDIDATES, path=str(promoted),
        fingerprint=cache_key, size_bytes=len(content.encode("utf-8")),
    )
    database.insert(artifact)
    return artifact


# ---------------------------------------------------------------------------
# 1. Todos os campos do roadmap presentes + duration correto
# ---------------------------------------------------------------------------


def test_todos_os_campos_do_roadmap_estao_presentes_e_duration_correto(engine, audit, database, storage, video):
    candidates = [
        _candidate_dict(0, 0.0, 4.0, "Você sabia que isso muda tudo?", SEGMENT_ROLE_BEGINNING, False,
                         completion_score=0.7, hook_score=0.5, context_score=0.6, speech_score=0.4,
                         overall_score=0.55, reason="hook: hook"),
    ]
    _insert_candidates_artifact(database, storage, video, candidates)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result = engine.handle_rank_job(job)
    assert result.target_status == JOB_READY
    artifact = database.get(Artifact, result.data["artifact_id"])
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    candidato = payload["candidates"][0]
    for campo in (
        "start", "end", "duration", "hook_score", "context_score", "speech_score",
        "visual_score", "completion_score", "overall_score", "reason",
    ):
        assert campo in candidato
    assert candidato["duration"] == pytest.approx(candidato["end"] - candidato["start"])
    assert candidato["duration"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# 2. Expansão -- frase cortada (estende o fim)
# ---------------------------------------------------------------------------


def _by_index(candidates):
    from _sistema.smart_clip_ranking_engine import _ParsedCandidate
    parsed = [_ParsedCandidate.from_dict(c) for c in candidates]
    return {c.index: c for c in parsed}


def test_expansao_frase_cortada_estende_o_fim_ate_pontuacao_terminal():
    candidates = [
        _candidate_dict(0, 0.0, 4.0, "frase cortada no meio", SEGMENT_ROLE_BEGINNING, False, completion_score=0.3),
        _candidate_dict(1, 4.0, 8.0, "ela termina agora.", SEGMENT_ROLE_DEVELOPMENT, True, completion_score=1.0),
    ]
    by_index = _by_index(candidates)
    start_idx, end_idx, reasons = plan_expansion(by_index, 0)
    assert start_idx == 0
    assert end_idx == 1
    assert any("frase cortada" in r for r in reasons)


def test_candidato_ja_completo_nao_e_expandido_por_frase_cortada():
    candidates = [
        _candidate_dict(0, 0.0, 4.0, "frase completa.", SEGMENT_ROLE_BEGINNING, False, completion_score=1.0),
        _candidate_dict(1, 4.0, 8.0, "outra frase.", SEGMENT_ROLE_DEVELOPMENT, True, completion_score=1.0),
    ]
    by_index = _by_index(candidates)
    start_idx, end_idx, reasons = plan_expansion(by_index, 0)
    assert start_idx == 0
    assert end_idx == 0
    assert not any("frase cortada" in r for r in reasons)


# ---------------------------------------------------------------------------
# 3. Expansão -- história sem contexto (estende o início)
# ---------------------------------------------------------------------------


def test_expansao_contexto_estende_o_inicio_para_segmento_anterior():
    candidates = [
        _candidate_dict(0, 0.0, 4.0, "abertura.", SEGMENT_ROLE_BEGINNING, False, completion_score=1.0),
        _candidate_dict(1, 4.0, 8.0, "desenvolvimento sem contexto proprio", SEGMENT_ROLE_DEVELOPMENT, False,
                         completion_score=1.0),
    ]
    by_index = _by_index(candidates)
    start_idx, end_idx, reasons = plan_expansion(by_index, 1)
    assert start_idx == 0
    assert end_idx == 1
    assert any("contexto" in r for r in reasons)


def test_candidato_beginning_autossuficiente_nunca_e_expandido_por_contexto():
    # Mesmo fornecendo um "anterior" artificial (index -1), BEGINNING nunca
    # dispara a expansão de contexto -- o guard é sobre o papel (role),
    # não sobre a mera existência de um vizinho.
    candidates = [
        _candidate_dict(-1, -4.0, 0.0, "segmento anterior artificial", SEGMENT_ROLE_BEGINNING, False),
        _candidate_dict(0, 0.0, 4.0, "abertura autossuficiente.", SEGMENT_ROLE_BEGINNING, False,
                         completion_score=1.0),
    ]
    by_index = _by_index(candidates)
    start_idx, end_idx, reasons = plan_expansion(by_index, 0)
    assert start_idx == 0
    assert not any("contexto" in r for r in reasons)


# ---------------------------------------------------------------------------
# 4. Expansão -- conclusão faltando (estende o fim)
# ---------------------------------------------------------------------------


def test_expansao_conclusao_estende_o_fim_quando_proximo_continua_a_historia():
    candidates = [
        _candidate_dict(0, 0.0, 4.0, "desenvolvimento.", SEGMENT_ROLE_DEVELOPMENT, True, completion_score=1.0),
        _candidate_dict(1, 4.0, 8.0, "continuacao da mesma historia.", SEGMENT_ROLE_CONCLUSION, False,
                         completion_score=1.0),
    ]
    by_index = _by_index(candidates)
    start_idx, end_idx, reasons = plan_expansion(by_index, 0)
    assert end_idx == 1
    assert any("conclusão" in r for r in reasons)


def test_candidato_conclusion_nunca_e_expandido_por_conclusao_faltando():
    candidates = [
        _candidate_dict(0, 0.0, 4.0, "conclusao.", SEGMENT_ROLE_CONCLUSION, True, completion_score=1.0),
        _candidate_dict(1, 4.0, 8.0, "outro segmento, mesma historia.", SEGMENT_ROLE_DEVELOPMENT, False,
                         completion_score=1.0),
    ]
    by_index = _by_index(candidates)
    start_idx, end_idx, reasons = plan_expansion(by_index, 0)
    assert end_idx == 0
    assert not any("conclusão" in r for r in reasons)


# ---------------------------------------------------------------------------
# 5. Teto de expansão -- nunca ultrapassado, mesmo com múltiplos motivos
# ---------------------------------------------------------------------------


def test_teto_de_expansao_nunca_e_ultrapassado_com_multiplos_motivos_simultaneos():
    # index0 é DEVELOPMENT+sem topic_change (contexto), não é CONCLUSION e o
    # próximo continua a historia (conclusão), e tem completion_score baixo
    # (frase cortada) -- os três motivos disparam ao mesmo tempo.
    candidates = [
        _candidate_dict(-1, -4.0, 0.0, "segmento anterior.", SEGMENT_ROLE_BEGINNING, False, completion_score=1.0),
        _candidate_dict(0, 0.0, 4.0, "meio cortado", SEGMENT_ROLE_DEVELOPMENT, False, completion_score=0.3),
        _candidate_dict(1, 4.0, 8.0, "continuacao ainda sem terminar", SEGMENT_ROLE_DEVELOPMENT, False,
                         completion_score=0.3),
        _candidate_dict(2, 8.0, 12.0, "agora sim termina.", SEGMENT_ROLE_CONCLUSION, False, completion_score=1.0),
    ]
    by_index = _by_index(candidates)
    original = by_index[0]
    for cap in (0.0, 1.0, 3.9, 4.0, 4.1, 8.0, 100.0):
        start_idx, end_idx, reasons = plan_expansion(by_index, 0, max_expansion_seconds=cap)
        start_c = by_index[start_idx]
        end_c = by_index[end_idx]
        added = (original.start - start_c.start) + (end_c.end - original.end)
        assert added <= cap + 1e-9, f"cap={cap} violado: added={added}"


# ---------------------------------------------------------------------------
# 6. Expansão nunca inventa texto
# ---------------------------------------------------------------------------


def test_expansao_nunca_inventa_texto(engine, audit, database, storage, video):
    candidates = [
        _candidate_dict(0, 0.0, 4.0, "abertura sem contexto proprio", SEGMENT_ROLE_DEVELOPMENT, False,
                         completion_score=1.0),
        _candidate_dict(1, 4.0, 8.0, "meio.", SEGMENT_ROLE_DEVELOPMENT, True, completion_score=1.0),
    ]
    _insert_candidates_artifact(database, storage, video, candidates)
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result = engine.handle_rank_job(job)
    artifact = database.get(Artifact, result.data["artifact_id"])
    payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    # index 0 é DEVELOPMENT sem topic_change -- deveria puxar o vizinho
    # anterior, mas não há nenhum (é o primeiro da lista) -- então o texto
    # final é exatamente o texto original, nunca um texto fabricado.
    final = payload["candidates"][0]
    assert final["text"] == "abertura sem contexto proprio"


def test_expansao_concatena_apenas_textos_reais_dos_segmentos_incluidos():
    candidates = [
        _candidate_dict(0, 0.0, 4.0, "PRIMEIRO", SEGMENT_ROLE_BEGINNING, False, completion_score=1.0),
        _candidate_dict(1, 4.0, 8.0, "SEGUNDO sem terminar", SEGMENT_ROLE_DEVELOPMENT, False, completion_score=0.3),
        _candidate_dict(2, 8.0, 12.0, "TERCEIRO.", SEGMENT_ROLE_CONCLUSION, True, completion_score=1.0),
    ]
    by_index = _by_index(candidates)
    start_idx, end_idx, _ = plan_expansion(by_index, 1)
    included = [by_index[i].text for i in range(start_idx, end_idx + 1)]
    text = " ".join(included)
    assert "PRIMEIRO" in text
    assert "SEGUNDO sem terminar" in text
    assert "TERCEIRO." in text
    # Nada fora dos textos originais aparece.
    assert set(text.split()) <= set("PRIMEIRO SEGUNDO sem terminar TERCEIRO.".split())


# ---------------------------------------------------------------------------
# 7. Ranking determinístico
# ---------------------------------------------------------------------------


def _ranked(index, start, end, overall_score):
    return RankedClipCandidate(
        index=index, start=start, end=end, duration=end - start, text=f"texto {index}",
        role=SEGMENT_ROLE_BEGINNING, topic_change=False, hook_score=0.0, context_score=0.0,
        speech_score=0.0, completion_score=0.0, visual_score=None, audio_score=None,
        overall_score=overall_score, reason="teste", expansion_reasons=(), source_candidate_indices=(index,),
        rank=0, accepted=True, suppressed_reason=None,
    )


def test_ranking_e_deterministico_entre_execucoes():
    candidates = [_ranked(2, 40, 44, 0.5), _ranked(0, 0, 4, 0.9), _ranked(1, 20, 24, 0.5)]
    result1 = rank_and_suppress(candidates)
    result2 = rank_and_suppress(candidates)
    ordem1 = [c.index for c in result1]
    ordem2 = [c.index for c in result2]
    assert ordem1 == ordem2
    # index0 (score 0.9) primeiro; entre index1/index2 (empatados em 0.5),
    # o de menor índice vem primeiro (desempate determinístico).
    assert ordem1 == [0, 1, 2]
    assert [c.rank for c in result1] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 8. Supressão de sobreposição -- auditável; leve aceita ambos
# ---------------------------------------------------------------------------


def test_overlap_excessivo_suprime_candidato_de_menor_rank_e_e_auditavel():
    alto = _ranked(0, 0.0, 10.0, 0.9)
    baixo = _ranked(1, 2.0, 6.0, 0.5)  # duration=4, inteiramente dentro de "alto"
    result = rank_and_suppress([alto, baixo], max_overlap_ratio=0.5)
    by_index = {c.index: c for c in result}
    assert by_index[0].accepted is True
    assert by_index[0].suppressed_reason is None
    assert by_index[1].accepted is False
    assert by_index[1].suppressed_reason is not None
    assert "#0" in by_index[1].suppressed_reason


def test_overlap_leve_aceita_ambos_os_candidatos():
    alto = _ranked(0, 0.0, 10.0, 0.9)
    leve = _ranked(1, 9.5, 20.0, 0.5)  # intersecção de 0.5s / duration 10.5 -> ~0.048
    result = rank_and_suppress([alto, leve], max_overlap_ratio=0.5)
    assert all(c.accepted for c in result)


def test_coverage_ratio_e_assimetrico_por_design():
    # Candidato curto totalmente contido no longo -> ratio alto do ponto de
    # vista do curto, baixo do ponto de vista do longo.
    curto_dentro_do_longo = coverage_ratio(2.0, 6.0, 0.0, 10.0)
    longo_contendo_curto = coverage_ratio(0.0, 10.0, 2.0, 6.0)
    assert curto_dentro_do_longo == pytest.approx(1.0)
    assert longo_contendo_curto == pytest.approx(0.4)


def test_coverage_ratio_sem_intersecao_e_zero():
    assert coverage_ratio(0.0, 4.0, 10.0, 14.0) == 0.0


# ---------------------------------------------------------------------------
# 9. Nenhum checkpoint novo gravado/referenciado
# ---------------------------------------------------------------------------


def _source_without_module_docstring(module) -> str:
    """Remove só a docstring de módulo (onde é legítimo EXPLICAR a regra) --
    nunca confunde "documentar a proibição" com "violar a proibição"."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    first = source.find('"""')
    second = source.find('"""', first + 3)
    if first == -1 or second == -1:
        return source
    return source[:first] + source[second + 3:]


def test_modulo_nao_referencia_checkpoints_novos():
    codigo = _source_without_module_docstring(scr_module)
    assert ".record_checkpoint(" not in codigo
    assert not any(name.startswith("CHECKPOINT_") for name in dir(scr_module))


def test_modulo_nao_importa_domain_checkpoints():
    codigo = _source_without_module_docstring(scr_module)
    assert "domain.checkpoints" not in codigo
    assert "from .domain.checkpoints" not in codigo


# ---------------------------------------------------------------------------
# 10. Cache -- reaproveita/recalcula/nunca duplica
# ---------------------------------------------------------------------------


def test_cache_reaproveita_entre_dois_jobs_quando_nada_muda(engine, audit, database, storage, video):
    candidates = [_candidate_dict(0, 0.0, 4.0, "texto.", SEGMENT_ROLE_BEGINNING, False, completion_score=1.0)]
    _insert_candidates_artifact(database, storage, video, candidates)

    job1 = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result1 = engine.handle_rank_job(job1)
    assert result1.data["cache_hit"] is False

    job2 = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result2 = engine.handle_rank_job(job2)
    assert result2.data["cache_hit"] is True
    assert result2.data["cache_key"] == result1.data["cache_key"]

    ranked_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_RANKED_CANDIDATES]
    assert len(ranked_artifacts) == 1


def test_cache_recalcula_quando_parametro_de_expansao_muda(
    manager, database, storage, app_paths, audit, scoring_engine, video,
):
    candidates = [_candidate_dict(0, 0.0, 4.0, "texto.", SEGMENT_ROLE_BEGINNING, False, completion_score=1.0)]
    _insert_candidates_artifact(database, storage, video, candidates)

    engine1 = ClipRankingEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, scoring_engine=scoring_engine,
    )
    job1 = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result1 = engine1.handle_rank_job(job1)

    engine2 = ClipRankingEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, scoring_engine=scoring_engine, max_overlap_ratio=0.9,
    )
    job2 = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result2 = engine2.handle_rank_job(job2)

    assert result2.data["cache_key"] != result1.data["cache_key"]
    ranked_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_RANKED_CANDIDATES]
    assert len(ranked_artifacts) == 2  # nunca sobrescreve, nunca reaproveita o cache errado


# ---------------------------------------------------------------------------
# 11. Encadeamento honesto até scoring/segmentação/transcrição
# ---------------------------------------------------------------------------


def test_handler_aciona_scoring_segmentacao_e_transcricao_em_cadeia_quando_nada_existe(
    engine, audit, database, video,
):
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result = engine.handle_rank_job(job)
    assert result.target_status == JOB_READY
    assert result.data["candidate_count"] >= 1

    segments_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CONTENT_SEGMENTS]
    assert len(segments_artifacts) == 1
    candidatos_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_CLIP_CANDIDATES]
    assert len(candidatos_artifacts) == 1
    ranked_artifacts = [a for a in database.list(Artifact) if a.kind == ARTIFACT_KIND_RANKED_CANDIDATES]
    assert len(ranked_artifacts) == 1


# ---------------------------------------------------------------------------
# 12. Falha honesta
# ---------------------------------------------------------------------------


def test_handler_falha_sem_video_id(engine, audit):
    job = audit.create_job(Job(operation=OPERATION_RANK_CANDIDATES))
    result = engine.handle_rank_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "job_missing_video_id"


def test_handler_falha_honestamente_com_candidato_de_schema_invalido(engine, audit, database, storage, video):
    # "candidates" é uma lista não vazia (passa em _read_candidates), mas
    # um item individual não tem os campos esperados -- falha honesta,
    # nunca uma exceção não tratada nem um Job travado em PROCESSING.
    payload_bruto = {"version": 1, "algorithm_version": "1", "candidates": [{"index": 0}]}
    content = json.dumps(payload_bruto, ensure_ascii=False)
    temp = storage.allocate_temp(suffix=".json", create=True)
    temp.write_text(content, encoding="utf-8")
    final_path = Path(storage._app_paths.projects) / "clip_candidates" / video.id / "schema-invalido.json"
    promoted = storage.promote_to_final(temp, final_path, overwrite=True)
    artifact = Artifact(
        video_id=video.id, kind=ARTIFACT_KIND_CLIP_CANDIDATES, path=str(promoted),
        fingerprint="schema-invalido", size_bytes=len(content.encode("utf-8")),
    )
    database.insert(artifact)

    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result = engine.handle_rank_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "invalid_candidate_schema"


def test_handler_falha_honestamente_quando_scoring_falha(
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
    scoring = SmartClipScoringEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, smart_clip_engine=smart_clip,
    )
    engine = ClipRankingEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        audit_log=audit, scoring_engine=scoring,
    )
    job = audit.create_job(Job(video_id=video.id, operation=OPERATION_RANK_CANDIDATES))
    result = engine.handle_rank_job(job)
    assert result.target_status == JOB_FAILED
    # Ver test_handler_falha_honestamente_quando_segmentacao_falha em
    # test_smart_clip_scoring.py para a explicação completa: o JobEngine
    # interno de scoring já captura a falha de transcrição e pousa o Job
    # de scoring em FAILED sozinho -- por isso, do ponto de vista deste
    # módulo (um nível acima), o motivo observado é "scoring_not_ready"
    # (scoring nunca chegou a READY), nunca "scoring_trigger_failed".
    assert result.data["reason"] == "scoring_not_ready"


# ---------------------------------------------------------------------------
# 13. Construção -- validação
# ---------------------------------------------------------------------------


def test_construcao_rejeita_manager_invalido(database, storage, app_paths):
    with pytest.raises(TypeError):
        ClipRankingEngine("nao-e-manager", database=database, storage_manager=storage, app_paths=app_paths)


def test_construcao_rejeita_completion_threshold_fora_da_faixa(manager, database, storage, app_paths):
    with pytest.raises(ValueError):
        ClipRankingEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            completion_expansion_threshold=1.5,
        )


def test_construcao_rejeita_max_expansion_seconds_negativo(manager, database, storage, app_paths):
    with pytest.raises(ValueError):
        ClipRankingEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            max_expansion_seconds=-1.0,
        )


def test_construcao_rejeita_max_overlap_ratio_fora_da_faixa(manager, database, storage, app_paths):
    with pytest.raises(ValueError):
        ClipRankingEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            max_overlap_ratio=1.5,
        )


# ---------------------------------------------------------------------------
# 14. Restart com novas instâncias
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
    scoring1 = SmartClipScoringEngine(
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths,
        audit_log=audit1, smart_clip_engine=smart_clip1,
    )
    engine1 = ClipRankingEngine(
        manager1, database=db1, storage_manager=storage1, app_paths=app_paths,
        audit_log=audit1, scoring_engine=scoring1,
    )
    job = audit1.create_job(Job(video_id=v.id, operation=OPERATION_RANK_CANDIDATES))
    result1 = engine1.handle_rank_job(job)
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
    scoring2 = SmartClipScoringEngine(
        manager2, database=db2, storage_manager=storage2, app_paths=app_paths,
        audit_log=audit2, smart_clip_engine=smart_clip2,
    )
    engine2 = ClipRankingEngine(
        manager2, database=db2, storage_manager=storage2, app_paths=app_paths,
        audit_log=audit2, scoring_engine=scoring2,
    )
    job2 = audit2.create_job(Job(video_id=v.id, operation=OPERATION_RANK_CANDIDATES))
    result2 = engine2.handle_rank_job(job2)
    assert result2.target_status == JOB_READY
    assert result2.data["cache_hit"] is True
    assert result2.data["cache_key"] == result1.data["cache_key"]
