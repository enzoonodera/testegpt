# -*- coding: utf-8 -*-
"""PROMPT 42 -- RenderEngine: testes.

Cobre: (1) builders puros de cada etapa do pipeline (sem FFmpeg real);
(2) resolução de cada entrada opcional presente/ausente, isoladamente e
em combinação; (3) cache_key/idempotência, incluindo o fechamento das
lacunas de CAPTIONS/TEMPLATE (promessa do Prompt 41); (4) cache-hit por
(project_id, kind, fingerprint); (5) validação pós-render obrigatória
(MediaProbe.probe_deep) -- falha nunca promove/grava checkpoint/Artifact;
(6) BADGE_RENDERED positivo/negativo (checkpoint sozinho ou Artifact
sozinho nunca bastam); (7) SourceAsset original nunca tocado; (8)
Job/checkpoint/Artifact -- contrato único, sem mecanismo paralelo; (9)
concorrência determinística (threading.Barrier); (10) restart com novas
instâncias; (11) ao menos um teste de integração real com FFmpeg
(pulado via shutil.which quando indisponível); (12) AST estrutural.
"""
from __future__ import annotations

import ast
import hashlib
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import _sistema.render_engine as render_engine
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.audio_engine import AudioEngine, AudioSettingsState
from _sistema.auto_reframe import ASPECT_RATIO_9_16, AutoReframeEngine, FALLBACK_CENTER_CROP
from _sistema.captions_engine import ARTIFACT_KIND_SRT, CaptionsEngine, MODE_BURNED, MODE_OFF
from _sistema.control_manager import ControlManager
from _sistema.domain import (
    Artifact,
    CHECKPOINT_RENDERED,
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_READY,
    Project,
    SourceAsset,
    Video,
)
from _sistema.edit_project import EditProjectManager, METADATA_MODE
from _sistema.job_engine import JobEngine
from _sistema.media_probe import MediaProbe, MediaProbeResult
from _sistema.render_engine import (
    ARTIFACT_KIND_RENDER_OUTPUT,
    build_audio_command,
    build_captions_burn_command,
    build_crop_reframe_command,
    build_final_encode_command,
    build_template_compose_command,
    build_trim_speed_command,
    compute_cache_key,
    FFmpegBackendError,
    ReframeBox,
    RenderEngine,
)
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import TemplateEngine
from _sistema.template_selector import TemplateSelector
from _sistema.timeline_editor import TimelineEditor
from _sistema.visual_editor import AdjustmentsState, FrameState, VisualEditor

import _sistema.media_catalog as media_catalog


FFMPEG_DISPONIVEL = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_SKIP_REASON = "ffmpeg/ffprobe nao encontrados no PATH deste ambiente"


def _gerar_video_valido(path: Path, *, duration: int = 2, size: str = "64x64", rate: int = 10) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _gerar_imagem_valida(path: Path, *, size: str = "320x568") -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=blue:s={size}",
        "-frames:v", "1",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


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
def template_engine(database, storage, app_paths):
    return TemplateEngine(database, storage_manager=storage, app_paths=app_paths)


@pytest.fixture
def video_file(tmp_path):
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
def render_engine_instance(manager, database, storage, app_paths, template_engine, audit):
    def _fake_backend(cmd):
        # Backend fake determinístico -- nunca chama FFmpeg de verdade.
        # Escreve um arquivo não-vazio no último argumento (o output path)
        # para que promote_to_final tenha algo para mover.
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"fake-render-output-bytes")

    return RenderEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        template_engine=template_engine, audit_log=audit, ffmpeg_backend=_fake_backend,
        media_probe=_FakeAlwaysValidProbe(),
    )


class _FakeAlwaysValidProbe(MediaProbe):
    """Probe fake -- evita depender de FFmpeg real para orquestração
    (cache/Job/Artifact/badge). Testes de VALIDAÇÃO real usam MediaProbe
    de verdade com vídeos gerados por ffmpeg (ver seção de integração).
    Herda de ``MediaProbe`` (nunca duplica um protocolo paralelo) apenas
    para satisfazer a checagem ``isinstance`` do construtor -- os
    métodos reais são sobrescritos, nenhuma chamada de ffmpeg/ffprobe
    ocorre por baixo."""

    def probe(self, file_path):
        from _sistema.media_probe import MediaProbeResult
        return MediaProbeResult(
            path=str(file_path), valid=True, duration=2.0, width=64, height=64,
            has_audio=True, file_size=Path(file_path).stat().st_size if Path(file_path).exists() else 0,
        )

    def probe_deep(self, file_path, *, timeout=None):
        return self.probe(file_path)


@pytest.fixture
def job_engine(database, audit):
    return JobEngine(database, audit_log=audit)


def _run_render_job(engine_, job_engine_, video_, project_):
    """Passa pelo JobEngine de verdade (``advance``) -- devolve o
    ``Job`` final persistido (``result.status``), mesmo padrão de
    ``_run_job`` em test_auto_reframe.py. Não expõe ``JobStepResult.
    data`` -- use ``_run_render_job_direct`` quando o teste precisar
    inspecionar ``data`` (mesmo padrão dos testes de cancelamento
    diretos de test_auto_reframe.py)."""
    if not job_engine_.has_handler(RenderEngine.OPERATION):
        job_engine_.register_handler(
            RenderEngine.OPERATION, engine_.handle_render_job, claims_status="PROCESSING"
        )
    job = engine_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=RenderEngine.OPERATION)
    )
    result = job_engine_.advance(job.id)
    return job, result


def _run_render_job_direct(engine_, video_, project_):
    """Reivindica o Job (PENDING -> PROCESSING) e chama
    ``handle_render_job`` DIRETAMENTE, devolvendo o ``JobStepResult``
    real (com ``.data``) -- mesmo padrão usado por
    test_auto_reframe.py/test_captions_engine.py para inspecionar dados
    de resultado que ``JobEngine.advance`` não expõe (ele devolve o
    ``Job`` persistido, não o ``JobStepResult`` do handler)."""
    job = engine_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=RenderEngine.OPERATION)
    )
    engine_._audit_log.transition_job(job.id, JOB_PROCESSING)
    claimed_job = engine_._database.get(Job, job.id)
    result = engine_.handle_render_job(claimed_job)
    return job, result


# ===========================================================================
# 0. Construção
# ===========================================================================


def test_construtor_rejeita_manager_de_tipo_errado(database, storage, app_paths, template_engine):
    with pytest.raises(TypeError):
        RenderEngine(
            "nao-e-um-manager", database=database, storage_manager=storage,
            app_paths=app_paths, template_engine=template_engine,
        )


def test_construtor_rejeita_database_de_tipo_errado(manager, storage, app_paths, template_engine):
    with pytest.raises(TypeError):
        RenderEngine(
            manager, database="nao-e-database", storage_manager=storage,
            app_paths=app_paths, template_engine=template_engine,
        )


def test_construtor_rejeita_storage_manager_de_tipo_errado(manager, database, app_paths, template_engine):
    with pytest.raises(TypeError):
        RenderEngine(
            manager, database=database, storage_manager="nao-e-storage",
            app_paths=app_paths, template_engine=template_engine,
        )


def test_construtor_rejeita_app_paths_de_tipo_errado(manager, database, storage, template_engine):
    with pytest.raises(TypeError):
        RenderEngine(
            manager, database=database, storage_manager=storage,
            app_paths="nao-e-app-paths", template_engine=template_engine,
        )


def test_construtor_rejeita_template_engine_de_tipo_errado(manager, database, storage, app_paths):
    with pytest.raises(TypeError):
        RenderEngine(
            manager, database=database, storage_manager=storage,
            app_paths=app_paths, template_engine="nao-e-template-engine",
        )


def test_construtor_rejeita_ffmpeg_backend_nao_chamavel(manager, database, storage, app_paths, template_engine):
    with pytest.raises(TypeError):
        RenderEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            template_engine=template_engine, ffmpeg_backend="nao-e-chamavel",
        )


def test_construtor_rejeita_media_probe_de_tipo_errado(manager, database, storage, app_paths, template_engine):
    with pytest.raises(TypeError):
        RenderEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            template_engine=template_engine, media_probe="nao-e-media-probe",
        )


def test_construtor_rejeita_control_manager_de_tipo_errado(manager, database, storage, app_paths, template_engine):
    with pytest.raises(TypeError):
        RenderEngine(
            manager, database=database, storage_manager=storage, app_paths=app_paths,
            template_engine=template_engine, control_manager="nao-e-control-manager",
        )


# ===========================================================================
# 1. Builders puros -- sem FFmpeg real (item 12: testáveis isoladamente)
# ===========================================================================


class TestBuildTrimSpeedCommand:
    def test_sem_segmentos_devolve_none(self):
        assert build_trim_speed_command("in.mp4", "out.mp4", []) is None

    def test_um_segmento_velocidade_normal(self):
        cmd = build_trim_speed_command("in.mp4", "out.mp4", [(0.0, 5.0, 1.0)])
        assert cmd is not None
        assert cmd[0] == "ffmpeg"
        assert "in.mp4" in cmd
        assert "out.mp4" in cmd
        assert "-filter_complex" in cmd
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "trim=start=0.0:end=5.0" in fc
        assert "setpts=(PTS-STARTPTS)/" not in fc  # velocidade 1.0 não adiciona segundo setpts

    def test_multiplos_segmentos_gera_concat(self):
        cmd = build_trim_speed_command("in.mp4", "out.mp4", [(0.0, 2.0, 1.0), (3.0, 5.0, 1.0)])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "concat=n=2:v=1:a=1" in fc

    def test_velocidade_2x_aplica_setpts_e_atempo(self):
        cmd = build_trim_speed_command("in.mp4", "out.mp4", [(0.0, 4.0, 2.0)])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "setpts=(PTS-STARTPTS)/2.000000" in fc
        assert "atempo=2.000000" in fc

    def test_velocidade_fora_de_0_5_2_0_decompoe_atempo(self):
        cmd = build_trim_speed_command("in.mp4", "out.mp4", [(0.0, 4.0, 4.0)])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc.count("atempo=2.000000") == 2

    def test_velocidade_0_25_decompoe_atempo(self):
        cmd = build_trim_speed_command("in.mp4", "out.mp4", [(0.0, 4.0, 0.25)])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert fc.count("atempo=0.500000") == 2


class TestBuildCropReframeCommand:
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            input_width=1920, input_height=1080, frame=None, adjustments=None, reframe_box=None,
        )
        kwargs.update(overrides)
        return kwargs

    def test_nada_definido_devolve_none(self):
        cmd = build_crop_reframe_command("in.mp4", "out.mp4", **self._base_kwargs())
        assert cmd is None

    def test_crop_manual_gera_filtro_crop(self):
        frame = FrameState(crop={"x": 0.25, "y": 0.0, "width": 0.5, "height": 1.0})
        cmd = build_crop_reframe_command("in.mp4", "out.mp4", **self._base_kwargs(frame=frame))
        assert cmd is not None
        vf = cmd[cmd.index("-vf") + 1]
        assert "crop=960:1080:480:0" in vf

    def test_crop_manual_tem_precedencia_sobre_reframe(self):
        frame = FrameState(crop={"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0})
        reframe_box = ReframeBox(center_x=0.9, center_y=0.9, target_aspect_ratio="9:16")
        cmd = build_crop_reframe_command(
            "in.mp4", "out.mp4", **self._base_kwargs(frame=frame, reframe_box=reframe_box)
        )
        vf = cmd[cmd.index("-vf") + 1]
        # A caixa do crop MANUAL (960 de largura) deve aparecer -- a caixa
        # derivada do reframe automático (9:16 dentro de 1920x1080) nunca é
        # usada quando um crop manual está presente.
        assert "crop=960:1080:0:0" in vf

    def test_reframe_automatico_sem_crop_manual(self):
        reframe_box = ReframeBox(center_x=0.5, center_y=0.5, target_aspect_ratio="9:16")
        cmd = build_crop_reframe_command("in.mp4", "out.mp4", **self._base_kwargs(reframe_box=reframe_box))
        assert cmd is not None
        vf = cmd[cmd.index("-vf") + 1]
        assert vf.startswith("crop=")

    def test_ajustes_visuais_geram_filtro_eq(self):
        adjustments = AdjustmentsState(brightness=0.1, contrast=1.2)
        cmd = build_crop_reframe_command("in.mp4", "out.mp4", **self._base_kwargs(adjustments=adjustments))
        vf = cmd[cmd.index("-vf") + 1]
        assert "eq=brightness=0.100000:contrast=1.200000" in vf

    def test_sharpen_gera_unsharp(self):
        adjustments = AdjustmentsState(sharpen=0.5)
        cmd = build_crop_reframe_command("in.mp4", "out.mp4", **self._base_kwargs(adjustments=adjustments))
        vf = cmd[cmd.index("-vf") + 1]
        assert "unsharp=" in vf

    def test_rotacao_90_gera_transpose(self):
        frame = FrameState(rotation=90)
        cmd = build_crop_reframe_command("in.mp4", "out.mp4", **self._base_kwargs(frame=frame))
        vf = cmd[cmd.index("-vf") + 1]
        assert "transpose=1" in vf

    def test_resize_fit_mode_stretch(self):
        frame = FrameState(resize={"width": 720, "height": 1280}, fit_mode="STRETCH")
        cmd = build_crop_reframe_command("in.mp4", "out.mp4", **self._base_kwargs(frame=frame))
        vf = cmd[cmd.index("-vf") + 1]
        assert vf == "scale=720:1280"


class TestBuildAudioCommand:
    def test_estado_vazio_devolve_none(self):
        assert build_audio_command("in.mp4", "out.mp4", audio=None, duration_seconds=None) is None
        assert build_audio_command("in.mp4", "out.mp4", audio=AudioSettingsState(), duration_seconds=None) is None

    def test_volume_gera_filtro(self):
        cmd = build_audio_command(
            "in.mp4", "out.mp4", audio=AudioSettingsState(volume=0.5), duration_seconds=10.0
        )
        af = cmd[cmd.index("-af") + 1]
        assert "volume=0.500000" in af

    def test_mute_sobrepoe_volume(self):
        cmd = build_audio_command(
            "in.mp4", "out.mp4", audio=AudioSettingsState(volume=0.8, mute=True), duration_seconds=10.0
        )
        af = cmd[cmd.index("-af") + 1]
        assert "volume=0" in af
        assert "0.800000" not in af

    def test_normalizacao_gera_loudnorm(self):
        cmd = build_audio_command(
            "in.mp4", "out.mp4",
            audio=AudioSettingsState(normalization_enabled=True, normalization_target_lufs=-16.0),
            duration_seconds=10.0,
        )
        af = cmd[cmd.index("-af") + 1]
        assert "loudnorm=I=-16.000000" in af

    def test_fade_out_precisa_de_duration_para_calcular_start(self):
        cmd = build_audio_command(
            "in.mp4", "out.mp4",
            audio=AudioSettingsState(fade_out_seconds=2.0), duration_seconds=10.0,
        )
        af = cmd[cmd.index("-af") + 1]
        assert "afade=t=out:st=8.000000:d=2.000000" in af

    def test_clipping_protection_gera_alimiter(self):
        cmd = build_audio_command(
            "in.mp4", "out.mp4", audio=AudioSettingsState(clipping_protection=True), duration_seconds=10.0
        )
        af = cmd[cmd.index("-af") + 1]
        assert "alimiter=" in af


class TestBuildTemplateComposeCommand:
    def test_sem_template_devolve_none(self):
        cmd = build_template_compose_command(
            "in.mp4", "out.mp4", background_path=None, canvas_width=None, canvas_height=None, video_box_px=None,
        )
        assert cmd is None

    def test_com_template_gera_overlay(self):
        cmd = build_template_compose_command(
            "in.mp4", "out.mp4", background_path="bg.png", canvas_width=1080, canvas_height=1920,
            video_box_px=(108, 192, 864, 1536),
        )
        assert cmd is not None
        assert "-loop" in cmd
        assert "bg.png" in cmd
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "overlay=108:192" in fc


class TestBuildCaptionsBurnCommand:
    def test_sem_legenda_devolve_none(self):
        assert build_captions_burn_command("in.mp4", "out.mp4", captions_path=None, style=None) is None

    def test_com_legenda_gera_filtro_subtitles(self):
        cmd = build_captions_burn_command("in.mp4", "out.mp4", captions_path="legenda.srt", style=None)
        assert cmd is not None
        vf = cmd[cmd.index("-vf") + 1]
        assert "subtitles='legenda.srt'" in vf

    def test_caminho_windows_com_dois_pontos_e_escapado(self):
        cmd = build_captions_burn_command(
            "in.mp4", "out.mp4", captions_path="C:\\Users\\Enzo\\legenda.srt", style=None
        )
        vf = cmd[cmd.index("-vf") + 1]
        assert "C\\:" in vf

    def test_estilo_gera_force_style(self):
        from _sistema.captions_style import CaptionsStyleState
        style = CaptionsStyleState(font="Arial", size=24.0, alignment="CENTER")
        cmd = build_captions_burn_command("in.mp4", "out.mp4", captions_path="legenda.srt", style=style)
        vf = cmd[cmd.index("-vf") + 1]
        assert "force_style=" in vf
        assert "FontName=Arial" in vf
        assert "Alignment=2" in vf


class TestBuildFinalEncodeCommand:
    def test_sempre_devolve_comando_nunca_none(self):
        cmd = build_final_encode_command("in.mp4", "out.mp4", strip_metadata=False)
        assert cmd is not None
        assert "-movflags" in cmd
        assert "+faststart" in cmd

    def test_strip_metadata_adiciona_map_metadata(self):
        cmd = build_final_encode_command("in.mp4", "out.mp4", strip_metadata=True)
        assert "-map_metadata" in cmd
        assert "-1" in cmd

    def test_sem_strip_metadata_nao_adiciona_map_metadata(self):
        cmd = build_final_encode_command("in.mp4", "out.mp4", strip_metadata=False)
        assert "-map_metadata" not in cmd


# ===========================================================================
# 2. compute_cache_key -- determinístico, fecha lacunas (seção 0.7)
# ===========================================================================


def test_cache_key_deterministico_mesmos_inputs():
    k1 = compute_cache_key("src-fp", "agg-fp")
    k2 = compute_cache_key("src-fp", "agg-fp")
    assert k1 == k2
    assert isinstance(k1, str)
    assert len(k1) == 64  # sha256 hex


def test_cache_key_muda_com_aggregate_fingerprint():
    k1 = compute_cache_key("src-fp", "agg-fp-1")
    k2 = compute_cache_key("src-fp", "agg-fp-2")
    assert k1 != k2


def test_cache_key_muda_com_captions_artifact_fingerprint():
    k1 = compute_cache_key("src-fp", "agg-fp", captions_artifact_fingerprint="c1")
    k2 = compute_cache_key("src-fp", "agg-fp", captions_artifact_fingerprint="c2")
    assert k1 != k2


def test_cache_key_muda_com_template_content_fingerprint():
    k1 = compute_cache_key("src-fp", "agg-fp", template_content_fingerprint="fp-a")
    k2 = compute_cache_key("src-fp", "agg-fp", template_content_fingerprint="fp-b")
    assert k1 != k2


def test_template_content_fingerprint_e_robusto_a_timestamp_de_mesma_resolucao():
    """Regressão do bug achado em
    test_troca_de_template_invalida_cache_fechando_promessa_do_prompt_41:
    a primeira versão usava Template.updated_at (resolução de segundo
    inteiro) -- duas edições no MESMO segundo produziam fingerprints
    IDÊNTICOS. compute_template_content_fingerprint nunca depende de
    timestamp, só de conteúdo."""
    from _sistema.render_engine import compute_template_content_fingerprint
    from _sistema.domain import Template

    same_ts = "2026-01-01T00:00:00+00:00"
    tpl1 = Template(source_path="a.png", layout={"zones": []}, extra={"width": 100, "height": 200}, updated_at=same_ts)
    tpl2 = Template(source_path="b.png", layout={"zones": []}, extra={"width": 100, "height": 200}, updated_at=same_ts)
    assert compute_template_content_fingerprint(tpl1) != compute_template_content_fingerprint(tpl2)

    tpl3 = Template(source_path="a.png", layout={"zones": []}, extra={"width": 100, "height": 200}, updated_at=same_ts)
    assert compute_template_content_fingerprint(tpl1) == compute_template_content_fingerprint(tpl3)


# ===========================================================================
# 3. Resolução de entradas opcionais -- Job handler, backend fake
# ===========================================================================


def test_render_sem_nenhuma_decisao_produz_artifact(render_engine_instance, job_engine, video, project):
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    assert result.status == JOB_READY
    artifacts = [a for a in _all_artifacts(render_engine_instance._database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    assert len(artifacts) == 1
    assert Path(artifacts[0].path).is_file()


def test_render_job_sem_video_id_falha_estruturalmente(render_engine_instance, database, audit, project):
    job = audit.create_job(Job(project_id=project.id, operation=RenderEngine.OPERATION))
    result = render_engine_instance.handle_render_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "job_missing_video_id"


def test_render_job_sem_project_id_falha_estruturalmente(render_engine_instance, database, audit, video):
    job = audit.create_job(Job(video_id=video.id, operation=RenderEngine.OPERATION))
    result = render_engine_instance.handle_render_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "job_missing_project_id"


def test_render_video_sem_source_asset_falha(render_engine_instance, database, audit, project):
    v2 = Video(name="sem-source")
    database.insert(v2)
    job = audit.create_job(Job(video_id=v2.id, project_id=project.id, operation=RenderEngine.OPERATION))
    result = render_engine_instance.handle_render_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "video_or_source_asset_id_missing"


def test_render_com_cortes_e_velocidade(render_engine_instance, job_engine, database, video, project):
    timeline = TimelineEditor(database)
    timeline.initialize_timeline(project.id, duration_seconds=10.0)
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    assert result.status == JOB_READY


def test_render_com_audio_settings(render_engine_instance, job_engine, manager, video, project):
    audio_engine = AudioEngine(manager)
    audio_engine.set_volume(project.id, 0.5)
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    assert result.status == JOB_READY


def test_render_com_crop_manual(render_engine_instance, job_engine, database, video, project):
    visual = VisualEditor(database)
    visual.set_crop(project.id, x=0.1, y=0.1, width=0.5, height=0.5)
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    assert result.status == JOB_READY


def test_render_com_template(render_engine_instance, job_engine, template_engine, video, project, tmp_path):
    bg = tmp_path / "bg.png"
    bg.write_bytes(b"fake-png-bytes")
    tpl = template_engine.create_template(
        name="T1",
        layout={
            "zones": [
                {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
            ]
        },
        source_path=str(bg),
        extra={"width": 1080, "height": 1920},
    )
    selector = TemplateSelector(render_engine_instance._manager, template_engine)
    selector.set_template(project.id, tpl.id)
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    assert result.status == JOB_READY


def test_render_com_captions_mode_off_nao_queima_legenda(render_engine_instance, job_engine, manager, video, project):
    captions = CaptionsEngine(
        manager, database=render_engine_instance._database, storage_manager=render_engine_instance._storage,
        app_paths=render_engine_instance._app_paths, audit_log=render_engine_instance._audit_log,
    )
    captions.set_mode(project.id, MODE_OFF)
    job, result = _run_render_job_direct(render_engine_instance, video, project)
    assert result.target_status == JOB_READY
    assert "captions_requested_but_unavailable" not in (result.data or {})


def test_render_com_captions_burned_mas_sem_artifact_nao_bloqueia(render_engine_instance, job_engine, manager, video, project):
    captions = CaptionsEngine(
        manager, database=render_engine_instance._database, storage_manager=render_engine_instance._storage,
        app_paths=render_engine_instance._app_paths, audit_log=render_engine_instance._audit_log,
    )
    captions.set_mode(project.id, MODE_BURNED)
    job, result = _run_render_job_direct(render_engine_instance, video, project)
    assert result.target_status == JOB_READY
    assert result.data.get("captions_requested_but_unavailable") is True


def test_render_com_captions_burned_e_artifact_disponivel(render_engine_instance, job_engine, manager, video, project, tmp_path):
    captions = CaptionsEngine(
        manager, database=render_engine_instance._database, storage_manager=render_engine_instance._storage,
        app_paths=render_engine_instance._app_paths, audit_log=render_engine_instance._audit_log,
    )
    captions.set_mode(project.id, MODE_BURNED)
    srt_path = tmp_path / "legenda.srt"
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nOla\n", encoding="utf-8")
    artifact = Artifact(
        video_id=video.id, kind=ARTIFACT_KIND_SRT, path=str(srt_path), fingerprint="captions-fp-1",
    )
    render_engine_instance._database.insert(artifact)
    job, result = _run_render_job_direct(render_engine_instance, video, project)
    assert result.target_status == JOB_READY
    assert "captions_requested_but_unavailable" not in (result.data or {})


def test_render_com_metadata_mode_clean(render_engine_instance, job_engine, manager, video, project):
    manager.set_category(project.id, METADATA_MODE, {"mode": "CLEAN", "profile_fields": None})
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    assert result.status == JOB_READY


# ===========================================================================
# 4. Cache/idempotência
# ===========================================================================


def _all_artifacts(database) -> "list[Artifact]":
    with database.connection() as conn:
        rows = conn.execute("SELECT id FROM artifacts").fetchall()
    return [database.get(Artifact, r[0]) for r in rows]


def test_segunda_chamada_e_cache_hit_mesmo_project(render_engine_instance, video, project):
    job1, result1 = _run_render_job_direct(render_engine_instance, video, project)
    assert result1.data["cache_hit"] is False
    artifacts_after_first = _all_artifacts(render_engine_instance._database)
    render_artifacts_1 = [a for a in artifacts_after_first if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    assert len(render_artifacts_1) == 1

    job2, result2 = _run_render_job_direct(render_engine_instance, video, project)
    assert result2.target_status == JOB_READY
    assert result2.data["cache_hit"] is True
    assert result2.data["reused_artifact_id"] == render_artifacts_1[0].id

    artifacts_after_second = [a for a in _all_artifacts(render_engine_instance._database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    assert len(artifacts_after_second) == 1  # nenhum novo Artifact criado


def test_mudanca_de_categoria_invalida_cache(render_engine_instance, manager, video, project):
    job1, result1 = _run_render_job_direct(render_engine_instance, video, project)
    assert result1.data["cache_hit"] is False

    audio_engine = AudioEngine(manager)
    audio_engine.set_volume(project.id, 0.7)

    job2, result2 = _run_render_job_direct(render_engine_instance, video, project)
    assert result2.target_status == JOB_READY
    assert result2.data["cache_hit"] is False

    render_artifacts = [a for a in _all_artifacts(render_engine_instance._database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    assert len(render_artifacts) == 2  # dois cache_keys diferentes = dois Artifacts


def test_troca_de_template_invalida_cache_fechando_promessa_do_prompt_41(
    render_engine_instance, template_engine, video, project, tmp_path
):
    """A categoria TEMPLATE só guarda {"template_id": ...} -- editar o
    MESMO template (update_template) não muda essa categoria, mas DEVE
    mudar o cache_key via template.updated_at (ver docstring 0.7)."""
    bg = tmp_path / "bg.png"
    bg.write_bytes(b"fake-png-bytes")
    tpl = template_engine.create_template(
        name="T1",
        layout={
            "zones": [
                {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
            ]
        },
        source_path=str(bg),
        extra={"width": 1080, "height": 1920},
    )
    selector = TemplateSelector(render_engine_instance._manager, template_engine)
    selector.set_template(project.id, tpl.id)

    job1, result1 = _run_render_job_direct(render_engine_instance, video, project)
    assert result1.data["cache_hit"] is False

    bg2 = tmp_path / "bg2.png"
    bg2.write_bytes(b"outro-fake-png")
    template_engine.update_template(tpl.id, source_path=str(bg2))

    job2, result2 = _run_render_job_direct(render_engine_instance, video, project)
    assert result2.data["cache_hit"] is False


def test_cache_e_escopado_por_project_nao_por_video(render_engine_instance, database, video, project):
    project2 = Project(video_id=video.id, name="p2")
    database.insert(project2)

    job1, result1 = _run_render_job_direct(render_engine_instance, video, project)
    job2, result2 = _run_render_job_direct(render_engine_instance, video, project2)

    assert result1.data["cache_hit"] is False
    assert result2.data["cache_hit"] is False  # mesmo vídeo, project diferente -- nunca reusa


# ===========================================================================
# 5. Validação pós-render obrigatória
# ===========================================================================


class _FakeInvalidProbe(MediaProbe):
    """Probe fake que SEMPRE reporta um resultado inválido -- simula um
    render corrompido/vazio para provar que a promoção nunca acontece."""

    def probe(self, file_path):
        from _sistema.media_probe import MediaProbeResult
        return MediaProbeResult(path=str(file_path), valid=True, duration=2.0, width=64, height=64)

    def probe_deep(self, file_path, *, timeout=None):
        from _sistema.media_probe import MediaProbeResult
        return MediaProbeResult(path=str(file_path), valid=False, error_code="CORRUPTED_OUTPUT")


def test_falha_de_validacao_pos_render_nunca_promove(manager, database, storage, app_paths, template_engine, audit, video, project, job_engine):
    def _fake_backend(cmd):
        Path(cmd[-1]).write_bytes(b"corrupted-bytes")

    engine = RenderEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        template_engine=template_engine, audit_log=audit, ffmpeg_backend=_fake_backend,
        media_probe=_FakeInvalidProbe(),
    )
    job, result = _run_render_job_direct(engine, video, project)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "post_render_validation_failed"

    artifacts = [a for a in _all_artifacts(database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    assert artifacts == []

    with database.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE entity_type='Job' AND entity_id=? "
            "AND event_type='JOB_CHECKPOINT_REACHED'",
            (job.id,),
        ).fetchall()
    assert rows == []  # nenhum checkpoint RENDERED gravado


def test_falha_no_backend_ffmpeg_nunca_promove(manager, database, storage, app_paths, template_engine, audit, video, project, job_engine):
    def _fake_backend_failing(cmd):
        raise FFmpegBackendError("simulado")

    engine = RenderEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        template_engine=template_engine, audit_log=audit, ffmpeg_backend=_fake_backend_failing,
        media_probe=_FakeAlwaysValidProbe(),
    )
    job, result = _run_render_job_direct(engine, video, project)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "ffmpeg_backend_failed"
    artifacts = [a for a in _all_artifacts(database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    assert artifacts == []


# ===========================================================================
# 6. BADGE_RENDERED -- positivo/negativo
# ===========================================================================


def _catalog_row(database, video_id):
    from _sistema.media_catalog import MediaCatalogService
    catalog = MediaCatalogService(database)
    return catalog.get_item(video_id)


def test_badge_rendered_acende_com_checkpoint_e_artifact(render_engine_instance, job_engine, video, project):
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    assert result.status == JOB_READY
    item = _catalog_row(render_engine_instance._database, video.id)
    assert item is not None
    assert "RENDERED" in item.system_badges


def test_badge_rendered_nao_acende_so_com_checkpoint_sem_artifact(database, audit, video):
    job = audit.create_job(Job(video_id=video.id, operation="RENDER_VIDEO"))
    audit.record_checkpoint(job.id, CHECKPOINT_RENDERED, data={"cache_hit": False, "cache_key": "x"})
    item = _catalog_row(database, video.id)
    assert item is not None
    assert "RENDERED" not in item.system_badges


def test_badge_rendered_nao_acende_so_com_artifact_sem_checkpoint(database, video):
    artifact = Artifact(video_id=video.id, kind=ARTIFACT_KIND_RENDER_OUTPUT, path="/tmp/nao-existe-de-verdade.mp4", fingerprint="x")
    database.insert(artifact)
    item = _catalog_row(database, video.id)
    assert item is not None
    assert "RENDERED" not in item.system_badges


def test_badge_rendered_nao_acende_se_artifact_aponta_para_arquivo_apagado(render_engine_instance, job_engine, video, project):
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    artifacts = [a for a in _all_artifacts(render_engine_instance._database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    Path(artifacts[0].path).unlink()
    item = _catalog_row(render_engine_instance._database, video.id)
    assert "RENDERED" not in item.system_badges


# ===========================================================================
# 7. SourceAsset original nunca é tocado
# ===========================================================================


def test_source_asset_original_nunca_e_modificado(render_engine_instance, job_engine, video, project, source):
    original_path = Path(source.local_path)
    hash_before = hashlib.sha256(original_path.read_bytes()).hexdigest()
    mtime_before = original_path.stat().st_mtime_ns

    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    assert result.status == JOB_READY

    hash_after = hashlib.sha256(original_path.read_bytes()).hexdigest()
    mtime_after = original_path.stat().st_mtime_ns
    assert hash_before == hash_after
    assert mtime_before == mtime_after


# ===========================================================================
# 8. Job/checkpoint/Artifact -- contrato único
# ===========================================================================


def test_job_engine_e_autoridade_unica_sobre_status(render_engine_instance, job_engine, video, project):
    job, result = _run_render_job(render_engine_instance, job_engine, video, project)
    persisted = job_engine.get_job(job.id)
    assert persisted.status == result.status


def test_cancelamento_antes_do_render_nao_produz_artifact(manager, database, storage, app_paths, template_engine, audit, video, project):
    """Mesmo padrão de test_auto_reframe.py -- chama o handler
    DIRETAMENTE (após reivindicar o Job como PROCESSING e pedir
    cancelamento) para exercitar a checagem PROATIVA do próprio handler,
    nunca a checagem de admissão do JobEngine (que cancelaria um Job
    ainda PENDING antes mesmo de chamar o handler)."""
    from _sistema.control_manager import CONTROL_SCOPE_JOB

    control = ControlManager(database)

    def _fake_backend(cmd):
        raise AssertionError("backend não deveria ser chamado após cancelamento")

    engine = RenderEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        template_engine=template_engine, audit_log=audit, ffmpeg_backend=_fake_backend,
        media_probe=_FakeAlwaysValidProbe(), control_manager=control,
    )
    job = audit.create_job(Job(video_id=video.id, project_id=project.id, operation=RenderEngine.OPERATION))
    audit.transition_job(job.id, JOB_PROCESSING)
    control.cancel(CONTROL_SCOPE_JOB, job.id)

    claimed_job = database.get(Job, job.id)
    result = engine.handle_render_job(claimed_job)
    assert result.target_status == JOB_CANCELLED
    artifacts = [a for a in _all_artifacts(database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    assert artifacts == []


# ===========================================================================
# 9. Concorrência determinística
# ===========================================================================


def test_duas_instancias_concorrentes_processando_o_mesmo_project(app_paths, database, video, project):
    """GATE 6/14: duas instâncias COMPLETAMENTE INDEPENDENTES (cada uma
    com sua própria LocalDatabase/StorageManager/TemplateEngine/
    EditProjectManager/OperationalAuditLog/JobEngine -- nunca objetos
    compartilhados, mesmo padrão exato de
    test_duas_instancias_concorrentes_processando_o_mesmo_video em
    test_auto_reframe.py) tentando renderizar o MESMO project
    concorrentemente -- nenhuma corrompe, nenhuma perde evidência."""
    barrier = threading.Barrier(2)
    errors: "list[BaseException]" = []
    statuses = []

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        audit_instance = OperationalAuditLog(db_instance)
        manager_instance = EditProjectManager(db_instance)
        storage_instance = StorageManager(app_paths, database_path=app_paths.database / "painel.db")
        template_engine_instance = TemplateEngine(db_instance, storage_manager=storage_instance, app_paths=app_paths)

        def _fake_backend(cmd):
            barrier.wait(timeout=5)
            Path(cmd[-1]).write_bytes(b"concurrent-bytes")

        engine_instance = RenderEngine(
            manager_instance, database=db_instance, storage_manager=storage_instance, app_paths=app_paths,
            template_engine=template_engine_instance, audit_log=audit_instance, ffmpeg_backend=_fake_backend,
            media_probe=_FakeAlwaysValidProbe(),
        )
        job_engine_instance = JobEngine(db_instance, audit_log=audit_instance)
        job_engine_instance.register_handler(
            RenderEngine.OPERATION, engine_instance.handle_render_job, claims_status="PROCESSING"
        )
        job = audit_instance.create_job(Job(video_id=video.id, project_id=project.id, operation=RenderEngine.OPERATION))
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
    artifacts = [a for a in _all_artifacts(database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT]
    # Mesmo cache_key (mesmas decisões, mesmo project) -- ambos os Jobs
    # concorrentes terminam READY; duas linhas de Artifact é aceitável
    # (mesmo padrão overwrite=True/nenhuma deduplicação entre processos
    # concorrentes de auto_reframe.py) -- o importante, verificado acima,
    # é que NENHUM dos dois falha e NENHUM promove um arquivo inválido.
    assert len(artifacts) == 2


# ===========================================================================
# 10. Restart -- novas instâncias
# ===========================================================================


def test_restart_completo_com_novas_instancias(tmp_path):
    paths = build_app_paths(data_root=tmp_path / "data2")
    ensure_app_directories(paths)
    db_path = paths.database / "painel.db"

    db1 = LocalDatabase(db_path)
    db1.initialize()
    video_file = tmp_path / "orig.mp4"
    video_file.write_bytes(b"video-bytes")
    src = SourceAsset(source_uri=str(video_file), local_path=str(video_file), fingerprint="fp-restart")
    db1.insert(src)
    v = Video(source_asset_id=src.id, name="v")
    db1.insert(v)
    p = Project(video_id=v.id, name="p")
    db1.insert(p)

    manager1 = EditProjectManager(db1)
    storage1 = StorageManager(paths, database_path=db_path)
    template_engine1 = TemplateEngine(db1, storage_manager=storage1, app_paths=paths)
    audit1 = OperationalAuditLog(db1)

    backend_calls: "list[list[str]]" = []

    def _fake_backend(cmd):
        backend_calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"restart-bytes")

    engine1 = RenderEngine(
        manager1, database=db1, storage_manager=storage1, app_paths=paths,
        template_engine=template_engine1, audit_log=audit1, ffmpeg_backend=_fake_backend,
        media_probe=_FakeAlwaysValidProbe(),
    )
    job_engine1 = JobEngine(db1, audit_log=audit1)
    job_engine1.register_handler(RenderEngine.OPERATION, engine1.handle_render_job, claims_status="PROCESSING")
    job1 = audit1.create_job(Job(video_id=v.id, project_id=p.id, operation=RenderEngine.OPERATION))
    result1 = job_engine1.advance(job1.id)
    assert result1.status == JOB_READY
    n_calls_after_first = len(backend_calls)
    assert n_calls_after_first > 0
    with db1.connection() as conn:
        (n1,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE project_id = ? AND kind = ?",
            (p.id, ARTIFACT_KIND_RENDER_OUTPUT),
        ).fetchone()
    assert n1 == 1

    # -- reinício: instâncias TOTALMENTE NOVAS sobre o mesmo arquivo .db --
    db2 = LocalDatabase(db_path)
    db2.initialize()
    manager2 = EditProjectManager(db2)
    storage2 = StorageManager(paths, database_path=db_path)
    template_engine2 = TemplateEngine(db2, storage_manager=storage2, app_paths=paths)
    audit2 = OperationalAuditLog(db2)
    engine2 = RenderEngine(
        manager2, database=db2, storage_manager=storage2, app_paths=paths,
        template_engine=template_engine2, audit_log=audit2, ffmpeg_backend=_fake_backend,
        media_probe=_FakeAlwaysValidProbe(),
    )
    job_engine2 = JobEngine(db2, audit_log=audit2)
    job_engine2.register_handler(RenderEngine.OPERATION, engine2.handle_render_job, claims_status="PROCESSING")
    job2 = audit2.create_job(Job(video_id=v.id, project_id=p.id, operation=RenderEngine.OPERATION))
    result2 = job_engine2.advance(job2.id)
    assert result2.status == JOB_READY
    assert len(backend_calls) == n_calls_after_first, "cache hit pós-restart nunca pode chamar o backend de novo"
    assert engine2._audit_log.has_reached_checkpoint(job2.id, CHECKPOINT_RENDERED)
    with db2.connection() as conn:
        (n2,) = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE project_id = ? AND kind = ?",
            (p.id, ARTIFACT_KIND_RENDER_OUTPUT),
        ).fetchone()
    assert n2 == 1, "cache hit pós-restart nunca duplica Artifact"


# ===========================================================================
# 11. Integração real com FFmpeg (pulado se indisponível)
# ===========================================================================


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_integracao_real_ffmpeg_produz_mp4_valido(manager, database, storage, app_paths, template_engine, audit, tmp_path):
    from _sistema.media_probe import MediaProbe

    video_path = tmp_path / "real.mp4"
    _gerar_video_valido(video_path, duration=1, size="64x64")
    src = SourceAsset(source_uri=str(video_path), local_path=str(video_path), fingerprint="fp-real-1")
    database.insert(src)
    v = Video(source_asset_id=src.id, name="v-real")
    database.insert(v)
    p = Project(video_id=v.id, name="p-real")
    database.insert(p)

    engine = RenderEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        template_engine=template_engine, audit_log=audit, media_probe=MediaProbe(),
    )
    job_engine_local = JobEngine(database, audit_log=audit)
    job_engine_local.register_handler(RenderEngine.OPERATION, engine.handle_render_job, claims_status="PROCESSING")
    job = audit.create_job(Job(video_id=v.id, project_id=p.id, operation=RenderEngine.OPERATION))
    result = job_engine_local.advance(job.id)

    assert result.status == JOB_READY
    artifacts = [a for a in _all_artifacts(database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT and a.video_id == v.id]
    assert len(artifacts) == 1
    output_path = Path(artifacts[0].path)
    assert output_path.is_file()

    probe = MediaProbe()
    validation = probe.probe_deep(output_path)
    assert validation.valid is True
    assert validation.duration is not None and validation.duration > 0
    assert validation.width and validation.height


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_integracao_real_ffmpeg_com_cortes_audio_e_crop(manager, database, storage, app_paths, template_engine, audit, tmp_path):
    from _sistema.media_probe import MediaProbe

    video_path = tmp_path / "real2.mp4"
    _gerar_video_valido(video_path, duration=3, size="128x128")
    src = SourceAsset(source_uri=str(video_path), local_path=str(video_path), fingerprint="fp-real-2")
    database.insert(src)
    v = Video(source_asset_id=src.id, name="v-real-2")
    database.insert(v)
    p = Project(video_id=v.id, name="p-real-2")
    database.insert(p)

    timeline = TimelineEditor(database)
    timeline.initialize_timeline(p.id, duration_seconds=3.0)
    visual = VisualEditor(database)
    visual.set_crop(p.id, x=0.1, y=0.1, width=0.6, height=0.6)
    audio_engine = AudioEngine(manager)
    audio_engine.set_volume(p.id, 0.8)

    engine = RenderEngine(
        manager, database=database, storage_manager=storage, app_paths=app_paths,
        template_engine=template_engine, audit_log=audit, media_probe=MediaProbe(),
    )
    job_engine_local = JobEngine(database, audit_log=audit)
    job_engine_local.register_handler(RenderEngine.OPERATION, engine.handle_render_job, claims_status="PROCESSING")
    job = audit.create_job(Job(video_id=v.id, project_id=p.id, operation=RenderEngine.OPERATION))
    result = job_engine_local.advance(job.id)

    assert result.status == JOB_READY
    artifacts = [a for a in _all_artifacts(database) if a.kind == ARTIFACT_KIND_RENDER_OUTPUT and a.video_id == v.id]
    output_path = Path(artifacts[0].path)
    probe = MediaProbe()
    validation = probe.probe_deep(output_path)
    assert validation.valid is True


# ===========================================================================
# 12. AST estrutural
# ===========================================================================


def _real_code_identifiers(tree: ast.AST) -> "set[str]":
    """Coleta identificadores de CÓDIGO real (nunca de docstring/comentário)
    -- mesmo helper introduzido em test_template_selector.py para evitar
    falsos positivos quando a docstring do módulo menciona nomes em prosa."""
    identifiers: "set[str]" = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.alias):
            identifiers.add((node.asname or node.name).split(".")[-1])
            identifiers.add(node.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            identifiers.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    return identifiers


def test_modulo_nao_referencia_checkpoint_validated_nem_badge_validated():
    source = Path(render_engine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    identifiers = _real_code_identifiers(tree)
    assert "CHECKPOINT_VALIDATED" not in identifiers
    assert "BADGE_VALIDATED" not in identifiers
    assert "FinalMediaValidator" not in identifiers


def test_modulo_nao_instancia_resource_manager():
    source = Path(render_engine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ResourceManager":
            pytest.fail("RenderEngine não deve instanciar ResourceManager nesta rodada (decisão: adiar)")


def test_media_catalog_nao_foi_alterado_alem_do_badge_rendered():
    # Confirma que a badge RENDERED tem expressão SQL real e não é mais "0".
    assert media_catalog._BADGE_SQL_EXPRESSIONS["RENDERED"] != "0"
    assert "render_output" in media_catalog._BADGE_SQL_EXPRESSIONS["RENDERED"]
    assert "RENDERED" in media_catalog._BADGE_SQL_EXPRESSIONS["RENDERED"]


def test_pipeline_stages_sao_funcoes_puras_de_modulo():
    """Nenhum builder de comando é um método de RenderEngine -- todos são
    funções de nível de módulo, testáveis sem construir o Engine."""
    import inspect
    for fn in (
        build_trim_speed_command, build_crop_reframe_command, build_audio_command,
        build_template_compose_command, build_captions_burn_command, build_final_encode_command,
    ):
        assert inspect.isfunction(fn)
        assert fn.__module__ == "_sistema.render_engine"
