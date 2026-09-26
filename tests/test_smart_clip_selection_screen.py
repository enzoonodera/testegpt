# -*- coding: utf-8 -*-
"""PROMPT 48 -- ClipSelectionScreen (tela de seleção do Smart Clip): testes.

Cobre: (1) apresentação padrão sem campos técnicos / visão avançada com;
(2) só ``accepted=True`` do Prompt 47 aparecem e aceitam decisão; (3)
``decide`` -- aceitar cria exatamente um Video/Project, rejeitar nada,
histórico append-only; (4) ``edit_bounds`` -- validação e limites
originais recuperáveis; (5) ``select_all``; (6) garantias estruturais
(AST) -- nunca ``VideoPromotionService.promote()``, recorte só via
``TimelineEditor``; (7) idempotência sequencial e concorrente (Barrier);
(8) nenhum checkpoint novo; (9) restart com novas instâncias; (10) crash
REAL (``os._exit`` em subprocesso) antes e depois do COMMIT; (11) funções
puras de título/etiqueta/intervalo."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import _sistema.smart_clip_selection_screen as screen_module
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Artifact, Job, JOB_READY, Project, SourceAsset, Video
from _sistema.edit_project import EditProjectManager
from _sistema.smart_clip_engine import SEGMENT_ROLE_CONCLUSION
from _sistema.smart_clip_ranking_engine import (
    ARTIFACT_KIND_RANKED_CANDIDATES,
    OPERATION_RANK_CANDIDATES,
    ClipRankingEngine,
)
from _sistema.smart_clip_scoring import ARTIFACT_KIND_CLIP_CANDIDATES
from _sistema.smart_clip_selection_screen import (
    DECISION_ACCEPTED,
    DECISION_PENDING,
    DECISION_REJECTED,
    EVENT_BOUNDS_EDITED,
    EVENT_ENTITY_TYPE,
    EVENT_PROMOTED,
    EVENT_USER_DECISION,
    ClipSelectionScreen,
    CorteJaPromovidoError,
    CorteNaoEncontradoError,
    CorteSuprimidoError,
    DecisaoInvalidaError,
    LimitesInvalidosError,
    RankingIlegivelError,
    RankingNaoEncontradoError,
    derive_title,
    format_time_range,
    promotion_ids,
    quality_label_for,
)
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.timeline_editor import TimelineEditor

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures -- ranking REAL produzido pelo ClipRankingEngine (Prompt 47)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_paths(tmp_path):
    paths = build_app_paths(data_root=tmp_path / "data")
    ensure_app_directories(paths)
    return paths


@pytest.fixture
def db_path(app_paths):
    return app_paths.database / "painel.db"


@pytest.fixture
def database(db_path):
    db = LocalDatabase(db_path)
    db.initialize()
    return db


@pytest.fixture
def storage(app_paths, db_path):
    return StorageManager(app_paths, database_path=db_path)


@pytest.fixture
def source(database, tmp_path):
    video_file = tmp_path / "longo.mp4"
    video_file.write_bytes(b"fake video bytes")
    src = SourceAsset(
        source_uri=str(video_file), local_path=str(video_file),
        original_name="longo.mp4", fingerprint="source-fp-48",
    )
    database.insert(src)
    return src


@pytest.fixture
def video(database, source):
    v = Video(source_asset_id=source.id, name="Entrevista longa")
    database.insert(v)
    return v


def _candidate(index, start, end, text, overall_score):
    # completion 1.0 + CONCLUSION + topic_change: nenhuma expansão dispara.
    return {
        "index": index, "start": start, "end": end, "text": text, "role": SEGMENT_ROLE_CONCLUSION,
        "topic_change": True, "hook_score": 0.5, "hook_signals": [], "context_score": 0.5,
        "speech_score": 0.5, "completion_score": 1.0, "visual_score": None, "audio_score": None,
        "overall_score": overall_score, "reason": "teste",
    }


# 0: [0,10] 0.80 aceito | 1: [2,9] 0.50 SUPRIMIDO (100% coberto pelo 0)
# 2: [20,30] 0.65 aceito | 3: [741.2,784.3] 0.30 aceito
CANDIDATES = [
    _candidate(0, 0.0, 10.0, "Você sabia que isso muda tudo? Depois explico o resto.", 0.80),
    _candidate(1, 2.0, 9.0, "Trecho redundante.", 0.50),
    _candidate(2, 20.0, 30.0, "Segundo assunto importante.", 0.65),
    _candidate(3, 741.2, 784.3, "Conclusão final do episódio.", 0.30),
]


def _build_ranking(database, storage, app_paths, video_):
    payload = {
        "version": 1, "algorithm_version": "1", "content_segments_artifact_id": "fake",
        "content_segments_cache_key": "segments-fp-fake", "weights": {},
        "speech_rate_min_wps": 1.5, "speech_rate_max_wps": 3.5, "candidates": CANDIDATES,
    }
    content = json.dumps(payload, ensure_ascii=False)
    temp = storage.allocate_temp(suffix=".json", create=True)
    temp.write_text(content, encoding="utf-8")
    final = Path(app_paths.projects) / "clip_candidates" / video_.id / "cand-fp-48.json"
    promoted = storage.promote_to_final(temp, final, overwrite=True)
    database.insert(Artifact(
        video_id=video_.id, kind=ARTIFACT_KIND_CLIP_CANDIDATES, path=str(promoted),
        fingerprint="cand-fp-48", size_bytes=len(content.encode("utf-8")),
    ))
    audit = OperationalAuditLog(database)
    engine = ClipRankingEngine(
        EditProjectManager(database), database=database, storage_manager=storage,
        app_paths=app_paths, audit_log=audit,
    )
    job = audit.create_job(Job(video_id=video_.id, operation=OPERATION_RANK_CANDIDATES))
    result = engine.handle_rank_job(job)
    assert result.target_status == JOB_READY
    return result.data["artifact_id"]


@pytest.fixture
def ranked_id(database, storage, app_paths, video):
    return _build_ranking(database, storage, app_paths, video)


@pytest.fixture
def screen(database):
    return ClipSelectionScreen(database)


def _clip_videos(database, source_video):
    return [v for v in database.list(Video) if v.id != source_video.id]


# ---------------------------------------------------------------------------
# 1. Apresentação
# ---------------------------------------------------------------------------


def test_fixture_usa_ranking_real_do_prompt_47_com_um_suprimido(database, ranked_id):
    artifact = database.get(Artifact, ranked_id)
    assert artifact.kind == ARTIFACT_KIND_RANKED_CANDIDATES
    raw = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
    by_index = {c["index"]: c for c in raw["candidates"]}
    assert by_index[1]["accepted"] is False
    assert all(by_index[i]["accepted"] for i in (0, 2, 3))


def test_apresentacao_padrao_nunca_inclui_campos_tecnicos(screen, ranked_id):
    clips = screen.list_clips(ranked_id)
    assert clips
    proibidos = {"reason", "expansion_reasons", "rank", "role", "original_start", "original_end"}
    for clip in clips:
        assert not any("score" in key for key in clip), clip.keys()
        assert not proibidos & set(clip)
        assert not any("score" in key for key in clip["preview"])


def test_visao_avancada_explicita_inclui_campos_tecnicos(screen, ranked_id):
    clip = screen.list_clips(ranked_id, include_scores=True)[0]
    for key in ("overall_score", "hook_score", "context_score", "speech_score", "completion_score",
                "visual_score", "audio_score", "reason", "expansion_reasons", "rank",
                "original_start", "original_end"):
        assert key in clip
    assert clip["overall_score"] == pytest.approx(0.80)


def test_formato_do_roadmap_corte_titulo_intervalo_etiqueta(screen, ranked_id):
    clips = screen.list_clips(ranked_id)
    assert [c["label"] for c in clips] == ["Corte 1", "Corte 2", "Corte 3"]
    assert [c["candidate_index"] for c in clips] == [0, 2, 3]
    first = clips[0]
    assert first["title"] == "Você sabia que isso muda tudo?"
    assert first["time_range"] == "00:00:00 → 00:00:10"
    assert first["quality_label"] == "Excelente corte"
    assert first["user_decision"] == DECISION_PENDING
    assert first["preview"] == {"source_video_id": first["preview"]["source_video_id"], "start": 0.0, "end": 10.0}
    assert clips[1]["quality_label"] == "Ótimo corte"
    assert clips[2]["time_range"] == "00:12:21 → 00:13:05"
    assert clips[2]["quality_label"] == "Corte possível"


# ---------------------------------------------------------------------------
# 2. Só accepted=True do Prompt 47
# ---------------------------------------------------------------------------


def test_suprimido_nunca_e_apresentado(screen, ranked_id):
    assert 1 not in [c["candidate_index"] for c in screen.list_clips(ranked_id)]


def test_decidir_sobre_suprimido_falha_honestamente_e_nada_e_gravado(screen, database, ranked_id, video):
    with pytest.raises(CorteSuprimidoError):
        screen.decide(ranked_id, 1, DECISION_ACCEPTED)
    with pytest.raises(CorteSuprimidoError):
        screen.edit_bounds(ranked_id, 1, start=1.0, end=5.0)
    with pytest.raises(CorteSuprimidoError):
        screen.original_bounds(ranked_id, 1)
    assert screen.history(ranked_id) == []
    assert _clip_videos(database, video) == []


def test_corte_inexistente_e_ranking_inexistente_falham_estruturados(screen, ranked_id, database, video):
    with pytest.raises(CorteNaoEncontradoError):
        screen.decide(ranked_id, 99, DECISION_ACCEPTED)
    with pytest.raises(CorteNaoEncontradoError):
        screen.decide(ranked_id, True, DECISION_ACCEPTED)
    with pytest.raises(RankingNaoEncontradoError):
        screen.list_clips("nao-e-uuid")
    with pytest.raises(RankingNaoEncontradoError):
        screen.list_clips("00000000-0000-4000-8000-000000000000")
    other = Artifact(video_id=video.id, kind="outro_kind", path="x")
    database.insert(other)
    with pytest.raises(RankingNaoEncontradoError):
        screen.list_clips(other.id)


def test_ranking_ilegivel_falha_sem_vazar_conteudo(screen, database, ranked_id):
    artifact = database.get(Artifact, ranked_id)
    Path(artifact.path).write_text("{segredo: token=abc", encoding="utf-8")
    with pytest.raises(RankingIlegivelError) as info:
        screen.list_clips(ranked_id)
    assert "token" not in str(info.value)


def test_decisao_invalida_rejeitada(screen, ranked_id):
    for bad in ("accepted", "", None, "PENDING", 1):
        with pytest.raises(DecisaoInvalidaError):
            screen.decide(ranked_id, 0, bad)
    assert screen.history(ranked_id) == []


# ---------------------------------------------------------------------------
# 3. decide
# ---------------------------------------------------------------------------


def test_aceitar_cria_exatamente_um_video_e_um_project_com_timeline_do_corte(screen, database, ranked_id, video):
    clip = screen.decide(ranked_id, 2, DECISION_ACCEPTED)
    assert clip["user_decision"] == DECISION_ACCEPTED

    novos = _clip_videos(database, video)
    assert len(novos) == 1
    novo = novos[0]
    assert novo.id == clip["promoted_video_id"]
    assert novo.source_asset_id == video.source_asset_id
    assert "corte 00:00:20-00:00:30" in novo.name

    projects = database.list(Project)
    assert [p.id for p in projects] == [clip["promoted_project_id"]]
    assert projects[0].video_id == novo.id

    segments = TimelineEditor(database).get_timeline(projects[0].id)
    assert len(segments) == 1
    assert (segments[0].start, segments[0].end, segments[0].speed) == (20.0, 30.0, 1.0)


def test_aceitar_nunca_cria_job_nem_artifact_nem_render(screen, database, ranked_id):
    jobs_antes = len(database.list(Job))
    artifacts_antes = len(database.list(Artifact))
    screen.decide(ranked_id, 0, DECISION_ACCEPTED)
    assert len(database.list(Job)) == jobs_antes
    assert len(database.list(Artifact)) == artifacts_antes


def test_rejeitar_nunca_cria_video_nem_project(screen, database, ranked_id, video):
    clip = screen.decide(ranked_id, 0, DECISION_REJECTED)
    assert clip["user_decision"] == DECISION_REJECTED
    assert clip["promoted_video_id"] is None
    assert _clip_videos(database, video) == []
    assert database.list(Project) == []


def test_decisao_e_auditavel_e_mudar_de_ideia_nao_apaga_historico(screen, database, ranked_id, video):
    screen.decide(ranked_id, 0, DECISION_REJECTED)
    screen.decide(ranked_id, 0, DECISION_ACCEPTED)
    screen.decide(ranked_id, 0, DECISION_REJECTED)

    events = screen.history(ranked_id, 0)
    decisions = [e["data"]["decision"] for e in events if e["event_type"] == EVENT_USER_DECISION]
    assert decisions == [DECISION_REJECTED, DECISION_ACCEPTED, DECISION_REJECTED]
    assert [e["event_type"] for e in events].count(EVENT_PROMOTED) == 1
    assert all(e["entity_type"] == EVENT_ENTITY_TYPE for e in events)
    assert all(e["created_at"] for e in events)

    clip = screen.get_clip(ranked_id, 0)
    assert clip["user_decision"] == DECISION_REJECTED
    # Não destrutivo: rejeitar depois de aceitar nunca apaga o Video criado.
    assert len(_clip_videos(database, video)) == 1

    # Histórico é append-only no próprio SQLite.
    import sqlite3
    with database.connection() as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM audit_events WHERE entity_type = ?", (EVENT_ENTITY_TYPE,))


def test_repetir_mesma_decisao_e_noop_sem_evento_extra(screen, ranked_id):
    screen.decide(ranked_id, 0, DECISION_REJECTED)
    screen.decide(ranked_id, 0, DECISION_REJECTED)
    assert len(screen.history(ranked_id, 0)) == 1


# ---------------------------------------------------------------------------
# 4. edit_bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start,end", [
    (5.0, 5.0), (6.0, 5.0), (-1.0, 5.0), (0.0, -2.0), (float("nan"), 5.0),
    (0.0, float("inf")), (True, 5.0), ("1", 5.0), (None, 5.0),
])
def test_edit_bounds_rejeita_limites_invalidos(screen, ranked_id, start, end):
    with pytest.raises(LimitesInvalidosError):
        screen.edit_bounds(ranked_id, 0, start=start, end=end)
    assert screen.history(ranked_id) == []


def test_edit_bounds_preserva_limites_originais_recuperaveis(screen, ranked_id):
    clip = screen.edit_bounds(ranked_id, 2, start=18.5, end=33.0)
    assert (clip["start"], clip["end"]) == (18.5, 33.0)
    assert clip["bounds_edited"] is True
    assert clip["time_range"] == "00:00:18 → 00:00:33"
    assert screen.original_bounds(ranked_id, 2) == (20.0, 30.0)
    advanced = screen.get_clip(ranked_id, 2, include_scores=True)
    assert (advanced["original_start"], advanced["original_end"]) == (20.0, 30.0)

    screen.edit_bounds(ranked_id, 2, start=21.0, end=29.0)
    edits = [e["data"] for e in screen.history(ranked_id, 2) if e["event_type"] == EVENT_BOUNDS_EDITED]
    assert [(e["start"], e["end"], e["previous_start"], e["previous_end"]) for e in edits] == [
        (18.5, 33.0, 20.0, 30.0), (21.0, 29.0, 18.5, 33.0),
    ]
    assert screen.original_bounds(ranked_id, 2) == (20.0, 30.0)


def test_promocao_usa_limites_editados_e_edicao_posterior_e_recusada(screen, database, ranked_id):
    screen.edit_bounds(ranked_id, 2, start=18.5, end=33.0)
    clip = screen.decide(ranked_id, 2, DECISION_ACCEPTED)
    segments = TimelineEditor(database).get_timeline(clip["promoted_project_id"])
    assert [(s.start, s.end) for s in segments] == [(18.5, 33.0)]

    with pytest.raises(CorteJaPromovidoError):
        screen.edit_bounds(ranked_id, 2, start=19.0, end=30.0)
    assert (screen.get_clip(ranked_id, 2)["start"], screen.get_clip(ranked_id, 2)["end"]) == (18.5, 33.0)


def test_corte_comecando_em_zero_nao_precisa_de_trim(screen, database, ranked_id):
    clip = screen.decide(ranked_id, 0, DECISION_ACCEPTED)
    segments = TimelineEditor(database).get_timeline(clip["promoted_project_id"])
    assert [(s.start, s.end) for s in segments] == [(0.0, 10.0)]


# ---------------------------------------------------------------------------
# 5. select_all
# ---------------------------------------------------------------------------


def test_select_all_aplica_a_todos_elegiveis_pendentes_nunca_aos_suprimidos(screen, database, ranked_id, video):
    screen.decide(ranked_id, 3, DECISION_REJECTED)
    affected = screen.select_all(ranked_id)
    assert affected == (0, 2)

    by_index = {c["candidate_index"]: c for c in screen.list_clips(ranked_id)}
    assert by_index[0]["user_decision"] == DECISION_ACCEPTED
    assert by_index[2]["user_decision"] == DECISION_ACCEPTED
    assert by_index[3]["user_decision"] == DECISION_REJECTED  # decisão do usuário preservada
    assert screen.history(ranked_id, 1) == []  # suprimido nunca tocado
    assert len(_clip_videos(database, video)) == 2

    assert screen.select_all(ranked_id) == ()  # nada mais pendente
    assert len(_clip_videos(database, video)) == 2


def test_select_all_rejeitar_nao_cria_nada(screen, database, ranked_id, video):
    assert screen.select_all(ranked_id, DECISION_REJECTED) == (0, 2, 3)
    assert _clip_videos(database, video) == []
    assert database.list(Project) == []


# ---------------------------------------------------------------------------
# 6. Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def _module_tree():
    return ast.parse(Path(screen_module.__file__).read_text(encoding="utf-8"))


def _code_names(tree):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Name):
            names.append(node.id)
    return [n.lower() for n in names]


def test_promocao_nunca_reaproveita_video_promotion_service():
    names = _code_names(_module_tree())
    for termo in ("videopromotionservice", "video_promotion", "promote"):
        assert not any(termo == n or termo in n.split(".") for n in names), termo
    assert "video_promotion" not in " ".join(names)


def test_recorte_so_via_timeline_editor_nunca_segunda_representacao():
    tree = _module_tree()
    names = _code_names(tree)
    assert "timelineeditor" in names
    assert "initialize_timeline" in names and "trim" in names
    # Nunca escreve edit_state/categorias diretamente nem define outro Segment.
    for proibido in ("edit_state", "set_category", "update_category", "editprojectmanager", "segment", "cuts"):
        assert proibido not in names, proibido
    class_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert not any("segment" in n.lower() for n in class_names)


def test_modulo_fora_de_escopo_nao_e_importado():
    names = " ".join(_code_names(_module_tree()))
    for proibido in ("smart_clip_scoring", "content_type_detector", "smart_clip_engine",
                     "checkpoints", "render_engine", "job_engine", "requests", "urllib", "socket", "http"):
        assert proibido not in names, proibido


# ---------------------------------------------------------------------------
# 7. Idempotência / concorrência
# ---------------------------------------------------------------------------


def test_aceitar_repetido_sequencialmente_nao_duplica(screen, database, ranked_id, video):
    first = screen.decide(ranked_id, 0, DECISION_ACCEPTED)
    second = screen.decide(ranked_id, 0, DECISION_ACCEPTED)
    screen.select_all(ranked_id)
    assert first["promoted_video_id"] == second["promoted_video_id"]
    assert first["promoted_video_id"] == promotion_ids(ranked_id, 0)[0]
    assert len(_clip_videos(database, video)) == 3  # corte 0 + select_all nos pendentes 2 e 3
    assert sum(1 for v in _clip_videos(database, video) if v.id == first["promoted_video_id"]) == 1


def test_aceitar_concorrente_com_duas_instancias_cria_um_unico_video(db_path, database, ranked_id, video):
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker():
        screen = ClipSelectionScreen(LocalDatabase(db_path))
        barrier.wait()
        try:
            results.append(screen.decide(ranked_id, 2, DECISION_ACCEPTED))
        except Exception as exc:  # pragma: no cover - falha do teste
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len({r["promoted_video_id"] for r in results}) == 1
    assert len(_clip_videos(database, video)) == 1
    assert len(database.list(Project)) == 1
    events = ClipSelectionScreen(database).history(ranked_id, 2)
    assert [e["event_type"] for e in events] == [EVENT_USER_DECISION, EVENT_PROMOTED]
    segments = TimelineEditor(database).get_timeline(results[0]["promoted_project_id"])
    assert [(s.start, s.end) for s in segments] == [(20.0, 30.0)]


def test_select_all_concorrente_nao_duplica(db_path, database, ranked_id, video):
    barrier = threading.Barrier(2)
    affected, errors = [], []

    def worker():
        screen = ClipSelectionScreen(LocalDatabase(db_path))
        barrier.wait()
        try:
            affected.append(screen.select_all(ranked_id))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(affected, key=len) == [(), (0, 2, 3)]
    assert len(_clip_videos(database, video)) == 3
    assert len(database.list(Project)) == 3


def test_edit_bounds_concorrente_com_aceitar_fica_consistente(db_path, database, ranked_id):
    barrier = threading.Barrier(2)
    outcomes = {}

    def accept():
        screen = ClipSelectionScreen(LocalDatabase(db_path))
        barrier.wait()
        outcomes["accept"] = screen.decide(ranked_id, 2, DECISION_ACCEPTED)

    def edit():
        screen = ClipSelectionScreen(LocalDatabase(db_path))
        barrier.wait()
        try:
            outcomes["edit"] = screen.edit_bounds(ranked_id, 2, start=22.0, end=28.0)
        except CorteJaPromovidoError:
            outcomes["edit"] = "refused"

    threads = [threading.Thread(target=accept), threading.Thread(target=edit)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    promoted = [e["data"] for e in ClipSelectionScreen(database).history(ranked_id, 2) if e["event_type"] == EVENT_PROMOTED]
    assert len(promoted) == 1
    segments = TimelineEditor(database).get_timeline(promoted[0]["project_id"])
    if outcomes["edit"] == "refused":
        assert (promoted[0]["start"], promoted[0]["end"]) == (20.0, 30.0)
    else:
        assert (promoted[0]["start"], promoted[0]["end"]) == (22.0, 28.0)
    assert [(s.start, s.end) for s in segments] == [(promoted[0]["start"], promoted[0]["end"])]


def test_insert_duplicado_do_mesmo_corte_e_recusado_pela_primary_key(database, ranked_id, video):
    """Defesa em profundidade: mesmo contornando o evento, a PK
    determinística recusa um segundo Video do mesmo corte."""
    ClipSelectionScreen(database).decide(ranked_id, 0, DECISION_ACCEPTED)
    video_id = promotion_ids(ranked_id, 0)[0]
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        database.insert(Video(id=video_id, source_asset_id=video.source_asset_id, name="dup"))


# ---------------------------------------------------------------------------
# 8. Nenhum checkpoint novo
# ---------------------------------------------------------------------------


def test_nenhum_checkpoint_referenciado_nem_gravado(screen, database, ranked_id):
    source = Path(screen_module.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert "CHECKPOINT_" not in code
    assert "record_checkpoint" not in code
    screen.select_all(ranked_id)
    assert database.list_audit_events(event_type="JOB_CHECKPOINT_REACHED") == []


def test_nenhuma_migration_nova():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9


# ---------------------------------------------------------------------------
# 9. Restart
# ---------------------------------------------------------------------------


def test_restart_com_novas_instancias_ve_o_mesmo_estado(db_path, database, ranked_id):
    screen1 = ClipSelectionScreen(database)
    screen1.edit_bounds(ranked_id, 3, start=740.0, end=790.0)
    screen1.decide(ranked_id, 2, DECISION_ACCEPTED)
    screen1.decide(ranked_id, 0, DECISION_REJECTED)
    before = screen1.list_clips(ranked_id, include_scores=True)
    history_before = screen1.history(ranked_id)

    db2 = LocalDatabase(db_path)
    screen2 = ClipSelectionScreen(db2)
    assert screen2.list_clips(ranked_id, include_scores=True) == before
    assert screen2.history(ranked_id) == history_before
    assert screen2.latest_ranked_artifact_id(before[0]["preview"]["source_video_id"]) == ranked_id
    # Idempotência pós-restart.
    again = screen2.decide(ranked_id, 2, DECISION_ACCEPTED)
    assert again["promoted_video_id"] == before[1]["promoted_video_id"]
    assert len(db2.list(Project)) == 1


# ---------------------------------------------------------------------------
# 10. Crash REAL (os._exit em subprocesso -- nunca exceção capturada)
# ---------------------------------------------------------------------------


def _run_crashing_child(db_path, ranked_id, patch_code):
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from _sistema.storage.database import LocalDatabase
        import _sistema.smart_clip_selection_screen as m
        {patch_code}
        screen = m.ClipSelectionScreen(LocalDatabase({str(db_path)!r}))
        screen.decide({ranked_id!r}, 3, "ACCEPTED")
        sys.exit(0)
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 17, proc.stderr


def test_crash_depois_do_commit_antes_da_timeline_e_recuperado(db_path, database, ranked_id, video):
    _run_crashing_child(db_path, ranked_id, textwrap.dedent("""
        def _crash(self, *a, **k):
            os._exit(17)
        m.TimelineEditor.initialize_timeline = _crash
    """).replace("\n", "\n        "))

    db2 = LocalDatabase(db_path)
    screen2 = ClipSelectionScreen(db2)
    clip = screen2.get_clip(ranked_id, 3)
    assert clip["user_decision"] == DECISION_ACCEPTED
    project_id = clip["promoted_project_id"]
    assert TimelineEditor(db2).get_timeline(project_id) == ()  # fase 2 não rodou

    assert screen2.resume_pending_promotions(ranked_id) == (3,)
    segments = TimelineEditor(db2).get_timeline(project_id)
    assert [(s.start, s.end) for s in segments] == [(741.2, 784.3)]
    assert len(_clip_videos(db2, video)) == 1
    # Retomar de novo é no-op.
    screen2.resume_pending_promotions(ranked_id)
    assert TimelineEditor(db2).get_timeline(project_id) == segments


def test_crash_entre_initialize_e_trim_e_recuperado_por_decide_repetido(db_path, database, ranked_id, video):
    _run_crashing_child(db_path, ranked_id, textwrap.dedent("""
        def _crash(self, *a, **k):
            os._exit(17)
        m.TimelineEditor.trim = _crash
    """).replace("\n", "\n        "))

    db2 = LocalDatabase(db_path)
    project_id = promotion_ids(ranked_id, 3)[1]
    assert [(s.start, s.end) for s in TimelineEditor(db2).get_timeline(project_id)] == [(0.0, 784.3)]
    ClipSelectionScreen(db2).decide(ranked_id, 3, DECISION_ACCEPTED)
    assert [(s.start, s.end) for s in TimelineEditor(db2).get_timeline(project_id)] == [(741.2, 784.3)]
    assert len(_clip_videos(db2, video)) == 1


def test_crash_antes_do_commit_nao_deixa_nada(db_path, database, ranked_id, video):
    _run_crashing_child(db_path, ranked_id, textwrap.dedent("""
        _orig = LocalDatabase.append_audit_event
        def _crash(self, event_type, **k):
            if event_type == m.EVENT_PROMOTED:
                os._exit(17)
            return _orig(self, event_type, **k)
        LocalDatabase.append_audit_event = _crash
    """).replace("\n", "\n        "))

    db2 = LocalDatabase(db_path)
    assert ClipSelectionScreen(db2).history(ranked_id) == []
    assert _clip_videos(db2, video) == []
    assert db2.list(Project) == []


def test_resume_nao_sobrescreve_edicao_do_usuario_no_project(screen, database, ranked_id):
    clip = screen.decide(ranked_id, 3, DECISION_ACCEPTED)
    project_id = clip["promoted_project_id"]
    editor = TimelineEditor(database)
    segment_id = editor.get_timeline(project_id)[0].segment_id
    editor.split(project_id, segment_id, 760.0)
    edited = editor.get_timeline(project_id)
    screen.resume_pending_promotions(ranked_id)
    screen.decide(ranked_id, 3, DECISION_ACCEPTED)
    assert editor.get_timeline(project_id) == edited


# ---------------------------------------------------------------------------
# 11. Funções puras
# ---------------------------------------------------------------------------


def test_derive_title_e_recorte_literal_deterministico():
    assert derive_title("  Primeira frase.  Segunda frase. ") == "Primeira frase."
    assert derive_title("") == "Trecho sem fala detectada"
    assert derive_title(None) == "Trecho sem fala detectada"
    longo = "palavra " * 30
    title = derive_title(longo)
    assert title.endswith("…") and len(title) <= 61
    assert title[:-1].strip() in longo
    assert derive_title("v1.2 é a versão final") == "v1.2 é a versão final"
    assert derive_title(longo) == derive_title(longo)


def test_faixas_da_etiqueta_qualitativa():
    assert quality_label_for(1.0) == "Excelente corte"
    assert quality_label_for(0.75) == "Excelente corte"
    assert quality_label_for(0.7499) == "Ótimo corte"
    assert quality_label_for(0.60) == "Ótimo corte"
    assert quality_label_for(0.45) == "Bom corte"
    assert quality_label_for(0.0) == "Corte possível"
    for label in ("Excelente corte", "Ótimo corte", "Bom corte", "Corte possível"):
        assert "assistido" not in label.lower()


def test_intervalo_no_formato_literal_do_roadmap():
    assert format_time_range(741.0, 784.0) == "00:12:21 → 00:13:04"
    assert format_time_range(3600.4, 3725.2) == "01:00:00 → 01:02:06"


def test_construcao_rejeita_database_invalido():
    with pytest.raises(TypeError):
        ClipSelectionScreen("painel.db")
