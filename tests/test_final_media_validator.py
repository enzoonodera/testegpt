# -*- coding: utf-8 -*-
"""PROMPT 43 -- FinalMediaValidator: testes.

Cobre: (1) construção -- validação de tipos/valores; (2) cada um dos 10
itens do checklist isoladamente, via função pura + ``MediaProbeResult``
fabricado (nenhum FFmpeg real necessário para a LÓGICA de decisão); (3)
o handler completo (localização do artifact, agregação, matriz de
decisão FAIL/USER_ACTION_REQUIRED/READY); (4) ``CHECKPOINT_VALIDATED``
gravado SOMENTE no caminho de sucesso total; (5) BADGE_VALIDATED/
BADGE_READY positivo/negativo (checkpoint sozinho ou Artifact sozinho
nunca bastam -- mesmo padrão adversarial de BADGE_RENDERED em
test_render_engine.py); (6) os 4 ramos SKIP individuais do item 8
(captions dentro do canvas); (7) o único caminho de correção automática
segura (item 10) claramente distinguido do FAIL; (8) contrato Job/
checkpoint/Artifact -- restart com novas instâncias, concorrência
determinística; (9) ao menos um teste de integração real com FFmpeg
(pulado via shutil.which quando indisponível).
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from _sistema.captions_engine import MODE_BURNED, MODE_OFF
from _sistema.captions_style import CaptionsStyleEngine
from _sistema.domain import (
    Artifact,
    CHECKPOINT_VALIDATED,
    Job,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_READY,
    JOB_USER_ACTION_REQUIRED,
    Project,
    SourceAsset,
    Video,
)
from _sistema.edit_project import CAPTIONS, EditProjectManager, TEMPLATE
from _sistema.job_engine import JobEngine
from _sistema.media_probe import MediaProbe, MediaProbeResult
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import TemplateEngine
from _sistema.template_selector import TemplateSelector

import _sistema.final_media_validator as fmv
from _sistema.final_media_validator import (
    ARTIFACT_KIND_RENDER_OUTPUT,
    CHECK_CORRECTED,
    CHECK_FAIL,
    CHECK_PASS,
    CHECK_SKIP,
    CHECK_USER_ACTION_REQUIRED,
    FinalMediaValidator,
    check_artifact_integrity,
    check_audio_present,
    check_captions_within_canvas,
    check_codec_valid,
    check_duration_valid,
    check_file_exists,
    check_not_truncated,
    check_resolution_valid,
    check_size_above_minimum,
    check_text_within_zones,
)


FFMPEG_DISPONIVEL = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_SKIP_REASON = "ffmpeg/ffprobe nao encontrados no PATH deste ambiente"


def _gerar_video_valido(path: Path, *, duration: int = 1, size: str = "64x64", rate: int = 10) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_render_engine.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_paths(tmp_path):
    from _sistema.app_paths import build_app_paths, ensure_app_directories

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
def template_engine(database, storage, app_paths):
    return TemplateEngine(database, storage_manager=storage, app_paths=app_paths)


@pytest.fixture
def source(database, tmp_path):
    video_file = tmp_path / "original.mp4"
    video_file.write_bytes(b"fake-source-bytes")
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


class _FakeAlwaysValidProbe(MediaProbe):
    """Probe fake determinístico -- mesmo espírito de
    ``_FakeAlwaysValidProbe`` em test_render_engine.py: herda de
    ``MediaProbe`` só para satisfazer o ``isinstance`` do construtor,
    nenhuma chamada real de ffmpeg/ffprobe ocorre."""

    def __init__(self, *, source_has_audio: bool = True, **result_overrides):
        super().__init__()
        self._source_has_audio = source_has_audio
        self._result_overrides = result_overrides

    def _make_result(self, file_path):
        base = dict(
            path=str(file_path), valid=True, container="mp4", codec="h264",
            duration=1.0, fps=10.0, width=64, height=64, has_audio=True,
            audio_stream_count=1,
            file_size=Path(file_path).stat().st_size if Path(file_path).exists() else 0,
            deep_checked=True,
        )
        base.update(self._result_overrides)
        return MediaProbeResult(**base)

    def probe(self, file_path):
        if str(file_path).endswith("original.mp4"):
            return MediaProbeResult(
                path=str(file_path), valid=True, has_audio=self._source_has_audio,
                file_size=Path(file_path).stat().st_size if Path(file_path).exists() else 0,
            )
        return self._make_result(file_path)

    def probe_deep(self, file_path, *, timeout=None):
        return self._make_result(file_path)


@pytest.fixture
def validator(manager, database, template_engine, audit):
    return FinalMediaValidator(
        manager, database=database, template_engine=template_engine,
        audit_log=audit, media_probe=_FakeAlwaysValidProbe(),
    )


@pytest.fixture
def job_engine(database, audit):
    return JobEngine(database, audit_log=audit)


_FAKE_RENDER_CONTENT = b"fake-render-output-bytes" * 100  # > MIN_OUTPUT_SIZE_BYTES_PADRAO (2048)
_MATCH = object()  # sentinela: grava size_bytes igual ao tamanho real do arquivo escrito


def _insert_render_artifact(database, video_, project_, path, *, size_bytes=_MATCH, content=_FAKE_RENDER_CONTENT):
    Path(path).write_bytes(content)
    if size_bytes is _MATCH:
        size_bytes = len(content)
    artifact = Artifact(
        video_id=video_.id, project_id=project_.id, kind=ARTIFACT_KIND_RENDER_OUTPUT,
        path=str(path), fingerprint="cache-key-x", size_bytes=size_bytes,
    )
    database.insert(artifact)
    return artifact


def _run_validate_job(validator_, job_engine_, video_, project_):
    if not job_engine_.has_handler(FinalMediaValidator.OPERATION):
        job_engine_.register_handler(
            FinalMediaValidator.OPERATION, validator_.handle_validate_job, claims_status="PROCESSING"
        )
    job = validator_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=FinalMediaValidator.OPERATION)
    )
    result = job_engine_.advance(job.id)
    return job, result


def _run_validate_job_direct(validator_, video_, project_):
    job = validator_._audit_log.create_job(
        Job(video_id=video_.id, project_id=project_.id, operation=FinalMediaValidator.OPERATION)
    )
    validator_._audit_log.transition_job(job.id, JOB_PROCESSING)
    claimed_job = validator_._database.get(Job, job.id)
    result = validator_.handle_validate_job(claimed_job)
    return job, result


# ===========================================================================
# 0. Construção
# ===========================================================================


def test_construtor_rejeita_manager_de_tipo_errado(database, template_engine):
    with pytest.raises(TypeError):
        FinalMediaValidator("nao-e-um-manager", database=database, template_engine=template_engine)


def test_construtor_rejeita_database_de_tipo_errado(manager, template_engine):
    with pytest.raises(TypeError):
        FinalMediaValidator(manager, database="nao-e-um-database", template_engine=template_engine)


def test_construtor_rejeita_template_engine_de_tipo_errado(manager, database):
    with pytest.raises(TypeError):
        FinalMediaValidator(manager, database=database, template_engine="nao-e-um-template-engine")


def test_construtor_rejeita_media_probe_de_tipo_errado(manager, database, template_engine):
    with pytest.raises(TypeError):
        FinalMediaValidator(
            manager, database=database, template_engine=template_engine, media_probe="nao-e-um-probe"
        )


def test_construtor_rejeita_min_output_size_bytes_nao_inteiro(manager, database, template_engine):
    with pytest.raises(TypeError):
        FinalMediaValidator(
            manager, database=database, template_engine=template_engine, min_output_size_bytes=1.5
        )


def test_construtor_rejeita_min_output_size_bytes_negativo(manager, database, template_engine):
    with pytest.raises(ValueError):
        FinalMediaValidator(
            manager, database=database, template_engine=template_engine, min_output_size_bytes=-1
        )


def test_construtor_aceita_dependencias_validas(manager, database, template_engine):
    v = FinalMediaValidator(manager, database=database, template_engine=template_engine)
    assert v is not None


# ===========================================================================
# 1. check_file_exists
# ===========================================================================


def test_check_file_exists_pass(tmp_path):
    f = tmp_path / "out.mp4"
    f.write_bytes(b"x")
    assert check_file_exists(str(f)).status == CHECK_PASS


def test_check_file_exists_fail_quando_ausente(tmp_path):
    f = tmp_path / "nao-existe.mp4"
    assert check_file_exists(str(f)).status == CHECK_FAIL


# ===========================================================================
# 2. check_duration_valid
# ===========================================================================


def test_check_duration_valid_pass():
    r = MediaProbeResult(path="x", valid=True, duration=2.5)
    assert check_duration_valid(r).status == CHECK_PASS


def test_check_duration_valid_fail_none():
    r = MediaProbeResult(path="x", valid=True, duration=None)
    assert check_duration_valid(r).status == CHECK_FAIL


def test_check_duration_valid_fail_zero():
    r = MediaProbeResult(path="x", valid=True, duration=0.0)
    assert check_duration_valid(r).status == CHECK_FAIL


# ===========================================================================
# 3. check_resolution_valid
# ===========================================================================


def test_check_resolution_valid_pass_sem_template():
    r = MediaProbeResult(path="x", valid=True, width=64, height=64)
    assert check_resolution_valid(r).status == CHECK_PASS


def test_check_resolution_valid_fail_zero():
    r = MediaProbeResult(path="x", valid=True, width=0, height=0)
    assert check_resolution_valid(r).status == CHECK_FAIL


def test_check_resolution_valid_pass_bate_com_template():
    r = MediaProbeResult(path="x", valid=True, width=1080, height=1920)
    result = check_resolution_valid(r, expected_width=1080, expected_height=1920)
    assert result.status == CHECK_PASS


def test_check_resolution_valid_fail_diverge_do_template():
    r = MediaProbeResult(path="x", valid=True, width=640, height=480)
    result = check_resolution_valid(r, expected_width=1080, expected_height=1920)
    assert result.status == CHECK_FAIL


# ===========================================================================
# 4. check_codec_valid
# ===========================================================================


def test_check_codec_valid_pass_h264():
    r = MediaProbeResult(path="x", valid=True, codec="h264")
    assert check_codec_valid(r).status == CHECK_PASS


def test_check_codec_valid_fail_outro_codec():
    r = MediaProbeResult(path="x", valid=True, codec="vp9")
    assert check_codec_valid(r).status == CHECK_FAIL


def test_check_codec_valid_fail_none():
    r = MediaProbeResult(path="x", valid=True, codec=None)
    assert check_codec_valid(r).status == CHECK_FAIL


# ===========================================================================
# 5. check_audio_present
# ===========================================================================


def test_check_audio_present_skip_sem_source_probe():
    r = MediaProbeResult(path="x", valid=True, has_audio=False)
    assert check_audio_present(r, source_probe=None).status == CHECK_SKIP


def test_check_audio_present_skip_source_invalido():
    r = MediaProbeResult(path="x", valid=True, has_audio=False)
    source = MediaProbeResult(path="orig", valid=False)
    assert check_audio_present(r, source_probe=source).status == CHECK_SKIP


def test_check_audio_present_fail_perdeu_audio():
    r = MediaProbeResult(path="x", valid=True, has_audio=False)
    source = MediaProbeResult(path="orig", valid=True, has_audio=True)
    assert check_audio_present(r, source_probe=source).status == CHECK_FAIL


def test_check_audio_present_pass_source_sem_audio():
    r = MediaProbeResult(path="x", valid=True, has_audio=False)
    source = MediaProbeResult(path="orig", valid=True, has_audio=False)
    assert check_audio_present(r, source_probe=source).status == CHECK_PASS


def test_check_audio_present_pass_ambos_com_audio():
    r = MediaProbeResult(path="x", valid=True, has_audio=True)
    source = MediaProbeResult(path="orig", valid=True, has_audio=True)
    assert check_audio_present(r, source_probe=source).status == CHECK_PASS


# ===========================================================================
# 6. check_size_above_minimum
# ===========================================================================


def test_check_size_above_minimum_pass():
    r = MediaProbeResult(path="x", valid=True, file_size=4096)
    assert check_size_above_minimum(r, min_output_size_bytes=2048).status == CHECK_PASS


def test_check_size_above_minimum_fail_igual_ao_minimo():
    r = MediaProbeResult(path="x", valid=True, file_size=2048)
    assert check_size_above_minimum(r, min_output_size_bytes=2048).status == CHECK_FAIL


def test_check_size_above_minimum_fail_abaixo():
    r = MediaProbeResult(path="x", valid=True, file_size=10)
    assert check_size_above_minimum(r, min_output_size_bytes=2048).status == CHECK_FAIL


def test_check_size_above_minimum_fail_none():
    r = MediaProbeResult(path="x", valid=True, file_size=None)
    assert check_size_above_minimum(r, min_output_size_bytes=2048).status == CHECK_FAIL


# ===========================================================================
# 7. check_not_truncated
# ===========================================================================


def test_check_not_truncated_pass():
    r = MediaProbeResult(path="x", valid=True, deep_checked=True)
    assert check_not_truncated(r).status == CHECK_PASS


def test_check_not_truncated_fail_invalido():
    r = MediaProbeResult(path="x", valid=False, deep_checked=True, error_message="decode falhou")
    assert check_not_truncated(r).status == CHECK_FAIL


def test_check_not_truncated_fail_nao_foi_decodificado_fundo():
    r = MediaProbeResult(path="x", valid=True, deep_checked=False)
    assert check_not_truncated(r).status == CHECK_FAIL


# ===========================================================================
# 8. check_captions_within_canvas -- 4 ramos SKIP + PASS + USER_ACTION_REQUIRED
# ===========================================================================


def test_check_captions_skip_modo_nao_burned():
    result = check_captions_within_canvas(
        captions_mode=MODE_OFF, caption_zone={"x": 0, "y": 0, "width": 1, "height": 1},
        style=None,
    )
    assert result.status == CHECK_SKIP


def test_check_captions_skip_sem_template_ou_sem_zona_caption():
    result = check_captions_within_canvas(captions_mode=MODE_BURNED, caption_zone=None, style=None)
    assert result.status == CHECK_SKIP


def test_check_captions_skip_sem_posicao_explicita():
    from _sistema.captions_style import CaptionsStyleState

    style = CaptionsStyleState()  # position=None por padrão
    result = check_captions_within_canvas(
        captions_mode=MODE_BURNED, caption_zone={"x": 0, "y": 0, "width": 1, "height": 1}, style=style,
    )
    assert result.status == CHECK_SKIP


def test_check_captions_skip_posicao_incompleta():
    from _sistema.captions_style import CaptionsStyleState

    style = CaptionsStyleState(position={"x": 0.5})  # sem "y"
    result = check_captions_within_canvas(
        captions_mode=MODE_BURNED, caption_zone={"x": 0, "y": 0, "width": 1, "height": 1}, style=style,
    )
    assert result.status == CHECK_SKIP


def test_check_captions_pass_dentro_da_zona():
    from _sistema.captions_style import CaptionsStyleState

    style = CaptionsStyleState(position={"x": 0.5, "y": 0.9})
    result = check_captions_within_canvas(
        captions_mode=MODE_BURNED, caption_zone={"x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2}, style=style,
    )
    assert result.status == CHECK_PASS


def test_check_captions_user_action_required_fora_da_zona():
    from _sistema.captions_style import CaptionsStyleState

    style = CaptionsStyleState(position={"x": 0.5, "y": 0.1})
    result = check_captions_within_canvas(
        captions_mode=MODE_BURNED, caption_zone={"x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2}, style=style,
    )
    assert result.status == CHECK_USER_ACTION_REQUIRED


# ===========================================================================
# 9. check_text_within_zones -- sempre SKIP (sem produtor real)
# ===========================================================================


def test_check_text_within_zones_sempre_skip_mesmo_com_dados():
    assert check_text_within_zones(text_layers_data=None).status == CHECK_SKIP
    assert check_text_within_zones(text_layers_data={"algo": "nao vazio"}).status == CHECK_SKIP


# ===========================================================================
# 10. check_artifact_integrity -- unico caminho de correcao automatica
# ===========================================================================


def test_check_artifact_integrity_pass_tamanhos_iguais():
    result = check_artifact_integrity(recorded_size_bytes=100, real_size_bytes=100)
    assert result.status == CHECK_PASS


def test_check_artifact_integrity_corrected_quando_size_bytes_e_none():
    result = check_artifact_integrity(recorded_size_bytes=None, real_size_bytes=100)
    assert result.status == CHECK_CORRECTED


def test_check_artifact_integrity_fail_tamanhos_divergem():
    result = check_artifact_integrity(recorded_size_bytes=100, real_size_bytes=50)
    assert result.status == CHECK_FAIL


# ===========================================================================
# 11. Handler -- resolucao/pre-condicoes
# ===========================================================================


def test_handler_falha_sem_video_id(validator, database, project):
    job = Job(project_id=project.id, operation=FinalMediaValidator.OPERATION)
    result = validator.handle_validate_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "job_missing_video_id"


def test_handler_falha_sem_project_id(validator, video):
    job = Job(video_id=video.id, operation=FinalMediaValidator.OPERATION)
    result = validator.handle_validate_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "job_missing_project_id"


def test_handler_falha_video_inexistente(validator, database, project):
    import uuid

    job = Job(video_id=str(uuid.uuid4()), project_id=project.id, operation=FinalMediaValidator.OPERATION)
    result = validator.handle_validate_job(job)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "video_not_found"


def test_handler_falha_sem_artifact_render_output(validator, video, project):
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "no_render_output_artifact_found"


def test_handler_escolhe_artifact_mais_recente_do_project(validator, database, video, project, tmp_path):
    _insert_render_artifact(database, video, project, tmp_path / "old.mp4")
    import time

    time.sleep(0.002)
    newest = _insert_render_artifact(database, video, project, tmp_path / "new.mp4")
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_READY
    assert result.data["artifact_id"] == newest.id


def test_handler_nao_confunde_artifact_de_outro_project(validator, database, video, project, tmp_path):
    other_project = Project(video_id=video.id, name="outro")
    database.insert(other_project)
    _insert_render_artifact(database, video, other_project, tmp_path / "other.mp4")
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_FAILED
    assert result.data["reason"] == "no_render_output_artifact_found"


# ===========================================================================
# 12. Handler -- sucesso total / CHECKPOINT_VALIDATED
# ===========================================================================


def test_handler_sucesso_total_grava_checkpoint_validated_e_devolve_ready(validator, audit, video, project, tmp_path):
    artifact = _insert_render_artifact(database=validator._database, video_=video, project_=project, path=tmp_path / "out.mp4")
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_READY
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_VALIDATED)
    checklist = {item["name"]: item["status"] for item in result.data["checklist"]}
    assert checklist["file_exists"] == CHECK_PASS


def test_handler_falha_nunca_grava_checkpoint_validated(validator, audit, video, project, tmp_path):
    f = tmp_path / "out.mp4"
    f.write_bytes(b"x" * 25)
    artifact = Artifact(
        video_id=video.id, project_id=project.id, kind=ARTIFACT_KIND_RENDER_OUTPUT,
        path=str(f), fingerprint="k", size_bytes=999999,  # tamanho divergente -> FAIL
    )
    validator._database.insert(artifact)
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_FAILED
    assert not audit.has_reached_checkpoint(job.id, CHECKPOINT_VALIDATED)


def test_handler_user_action_required_nunca_grava_checkpoint_validated(manager, database, template_engine, audit, video, project, tmp_path):
    tpl = template_engine.create_template(
        name="T", layout={"zones": [
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "VIDEO", "x": 0.0, "y": 0.0, "width": 1.0, "height": 0.8},
            {"zone_type": "CAPTION", "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2},
        ]},
        extra={"width": 64, "height": 64},
    )
    selector = TemplateSelector(manager, template_engine)
    selector.set_template(project.id, tpl.id)
    manager.set_category(project.id, CAPTIONS, {"mode": MODE_BURNED})
    style_engine = CaptionsStyleEngine(manager)
    style_engine.set_style(project.id, position={"x": 0.5, "y": 0.1})  # fora da zona CAPTION

    v = FinalMediaValidator(
        manager, database=database, template_engine=template_engine, audit_log=audit,
        media_probe=_FakeAlwaysValidProbe(),
    )
    _insert_render_artifact(database, video, project, tmp_path / "out.mp4")
    job, result = _run_validate_job_direct(v, video, project)
    assert result.target_status == JOB_USER_ACTION_REQUIRED
    assert "captions_within_canvas" in result.data["needs_user_action_items"]
    assert not audit.has_reached_checkpoint(job.id, CHECKPOINT_VALIDATED)


def test_handler_arquivo_ausente_reporta_demais_itens_como_skip_sem_fabricar_dado(validator, video, project, tmp_path):
    missing = Artifact(
        video_id=video.id, project_id=project.id, kind=ARTIFACT_KIND_RENDER_OUTPUT,
        path=str(tmp_path / "nunca-existiu.mp4"), fingerprint="k",
    )
    validator._database.insert(missing)
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_FAILED
    checklist = {item["name"]: item["status"] for item in result.data["checklist"]}
    assert checklist["file_exists"] == CHECK_FAIL
    for name, status in checklist.items():
        if name != "file_exists":
            assert status == CHECK_SKIP


# ===========================================================================
# 13. Correcao automatica segura (item 10) vs FAIL -- distincao clara
# ===========================================================================


def test_handler_corrige_size_bytes_ausente_e_persiste(validator, video, project, tmp_path):
    f = tmp_path / "out.mp4"
    artifact = _insert_render_artifact(validator._database, video, project, f, size_bytes=None)
    assert artifact.size_bytes is None

    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_READY
    assert "artifact_integrity" in result.data["corrected_items"]

    reloaded = validator._database.get(Artifact, artifact.id)
    assert reloaded.size_bytes == f.stat().st_size


def test_handler_size_bytes_divergente_falha_e_nao_corrige(validator, video, project, tmp_path):
    f = tmp_path / "out.mp4"
    f.write_bytes(b"x" * 25)
    artifact = Artifact(
        video_id=video.id, project_id=project.id, kind=ARTIFACT_KIND_RENDER_OUTPUT,
        path=str(f), fingerprint="k", size_bytes=999,
    )
    validator._database.insert(artifact)
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_FAILED

    reloaded = validator._database.get(Artifact, artifact.id)
    assert reloaded.size_bytes == 999  # nunca corrigido silenciosamente


# ===========================================================================
# 14. BADGE_VALIDATED/BADGE_READY -- positivo/negativo (mesmo padrao
#     adversarial de BADGE_RENDERED em test_render_engine.py)
# ===========================================================================


def _catalog_row(database, video_id):
    from _sistema.media_catalog import MediaCatalogService

    catalog = MediaCatalogService(database)
    return catalog.get_item(video_id)


def test_badge_validated_e_ready_acendem_com_checkpoint_e_artifact(validator, video, project, tmp_path):
    _insert_render_artifact(validator._database, video, project, tmp_path / "out.mp4")
    from _sistema.domain import CHECKPOINT_RENDERED

    validator._audit_log.record_checkpoint(
        validator._audit_log.create_job(Job(video_id=video.id, operation="RENDER_VIDEO")).id,
        CHECKPOINT_RENDERED, data={},
    )
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_READY

    item = _catalog_row(validator._database, video.id)
    assert "VALIDATED" in item.system_badges
    assert "READY" in item.system_badges


def test_badge_validated_nao_acende_so_com_checkpoint_sem_artifact(database, audit, video):
    job = audit.create_job(Job(video_id=video.id, operation="VALIDATE_MEDIA"))
    audit.record_checkpoint(job.id, CHECKPOINT_VALIDATED, data={})
    item = _catalog_row(database, video.id)
    assert "VALIDATED" not in item.system_badges
    assert "READY" not in item.system_badges


def test_badge_validated_nao_acende_so_com_artifact_sem_checkpoint(database, video, project):
    artifact = Artifact(
        video_id=video.id, project_id=project.id, kind=ARTIFACT_KIND_RENDER_OUTPUT,
        path="/tmp/nao-existe-de-verdade.mp4", fingerprint="x",
    )
    database.insert(artifact)
    item = _catalog_row(database, video.id)
    assert "VALIDATED" not in item.system_badges
    assert "READY" not in item.system_badges


def test_badge_validated_nao_acende_se_artifact_aponta_para_arquivo_apagado(validator, video, project, tmp_path):
    artifact = _insert_render_artifact(validator._database, video, project, tmp_path / "out.mp4")
    job, result = _run_validate_job_direct(validator, video, project)
    assert result.target_status == JOB_READY
    Path(artifact.path).unlink()
    item = _catalog_row(validator._database, video.id)
    assert "VALIDATED" not in item.system_badges
    assert "READY" not in item.system_badges


# ===========================================================================
# 15. Contrato Job/checkpoint/Artifact -- restart, concorrencia
# ===========================================================================


def test_restart_com_novas_instancias_ve_checkpoint_ja_gravado(app_paths, video, project, tmp_path):
    db1 = LocalDatabase(app_paths.database / "painel.db")
    db1.initialize()
    audit1 = OperationalAuditLog(db1)
    manager1 = EditProjectManager(db1)
    storage1 = StorageManager(app_paths, database_path=app_paths.database / "painel.db")
    template_engine1 = TemplateEngine(db1, storage_manager=storage1, app_paths=app_paths)

    # Reinsere as entidades no banco novo (fixtures video/project usam um
    # database de outra sessão de conexão -- reproduzimos os dados aqui,
    # mesmo padrão de restart de test_render_engine.py).
    source = SourceAsset(source_uri="s", local_path=str(tmp_path / "orig.mp4"), fingerprint="f")
    (tmp_path / "orig.mp4").write_bytes(b"x")
    db1.insert(source)
    v = Video(source_asset_id=source.id, name="v")
    db1.insert(v)
    p = Project(video_id=v.id, name="p")
    db1.insert(p)

    validator1 = FinalMediaValidator(
        manager1, database=db1, template_engine=template_engine1, audit_log=audit1,
        media_probe=_FakeAlwaysValidProbe(),
    )
    _insert_render_artifact(db1, v, p, tmp_path / "out.mp4")
    job, result = _run_validate_job_direct(validator1, v, p)
    assert result.target_status == JOB_READY

    db2 = LocalDatabase(app_paths.database / "painel.db")
    audit2 = OperationalAuditLog(db2)
    assert audit2.has_reached_checkpoint(job.id, CHECKPOINT_VALIDATED)


def test_duas_instancias_concorrentes_nao_corrompem_size_bytes(app_paths, tmp_path):
    db_path = app_paths.database / "painel.db"
    db0 = LocalDatabase(db_path)
    db0.initialize()

    source = SourceAsset(source_uri="s", local_path=str(tmp_path / "orig.mp4"), fingerprint="f")
    (tmp_path / "orig.mp4").write_bytes(b"x")
    db0.insert(source)
    v = Video(source_asset_id=source.id, name="v")
    db0.insert(v)
    p = Project(video_id=v.id, name="p")
    db0.insert(p)
    out = tmp_path / "out.mp4"
    out.write_bytes(_FAKE_RENDER_CONTENT)
    artifact = Artifact(
        video_id=v.id, project_id=p.id, kind=ARTIFACT_KIND_RENDER_OUTPUT,
        path=str(out), fingerprint="k", size_bytes=None,
    )
    db0.insert(artifact)

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        try:
            db = LocalDatabase(db_path)
            audit_ = OperationalAuditLog(db)
            manager_ = EditProjectManager(db)
            storage_ = StorageManager(app_paths, database_path=db_path)
            template_engine_ = TemplateEngine(db, storage_manager=storage_, app_paths=app_paths)
            validator_ = FinalMediaValidator(
                manager_, database=db, template_engine=template_engine_, audit_log=audit_,
                media_probe=_FakeAlwaysValidProbe(),
            )
            barrier.wait(timeout=5)
            job, result = _run_validate_job_direct(validator_, v, p)
            results.append(result.target_status)
        except Exception as exc:  # pragma: no cover -- diagnostico de falha do teste
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert results == [JOB_READY, JOB_READY]
    final = db0.get(Artifact, artifact.id)
    assert final.size_bytes == out.stat().st_size


# ===========================================================================
# 16. Integracao real com FFmpeg (pulado quando indisponivel)
# ===========================================================================


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_integracao_real_ffmpeg_video_valido_passa_em_todos_os_itens_aplicaveis(validator, video, project, tmp_path):
    real_probe = MediaProbe()
    v = FinalMediaValidator(
        validator._manager, database=validator._database, template_engine=validator._template_engine,
        audit_log=validator._audit_log, media_probe=real_probe,
    )
    out = tmp_path / "out.mp4"
    _gerar_video_valido(out)
    artifact = Artifact(
        video_id=video.id, project_id=project.id, kind=ARTIFACT_KIND_RENDER_OUTPUT,
        path=str(out), fingerprint="k", size_bytes=None,
    )
    v._database.insert(artifact)
    job, result = _run_validate_job_direct(v, video, project)
    checklist = {item["name"]: item["status"] for item in result.data["checklist"]}
    assert checklist["file_exists"] == CHECK_PASS
    assert checklist["duration_valid"] == CHECK_PASS
    assert checklist["codec_valid"] == CHECK_PASS
    assert checklist["not_truncated"] == CHECK_PASS
    assert result.target_status == JOB_READY


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_integracao_real_ffmpeg_video_truncado_falha_item_not_truncated(validator, video, project, tmp_path):
    real_probe = MediaProbe()
    v = FinalMediaValidator(
        validator._manager, database=validator._database, template_engine=validator._template_engine,
        audit_log=validator._audit_log, media_probe=real_probe,
    )
    out = tmp_path / "out.mp4"
    _gerar_video_valido(out, duration=2)
    original_bytes = out.read_bytes()
    # Trunca o arquivo pela metade -- container inválido/incompleto,
    # ffprobe raso normalmente já rejeita; garante que o item de decode
    # profundo (ou a checagem de existência/arquivo válido) capture o
    # problema, nunca promovendo um FAIL como se fosse PASS.
    out.write_bytes(original_bytes[: len(original_bytes) // 2])
    artifact = Artifact(
        video_id=video.id, project_id=project.id, kind=ARTIFACT_KIND_RENDER_OUTPUT,
        path=str(out), fingerprint="k", size_bytes=None,
    )
    v._database.insert(artifact)
    job, result = _run_validate_job_direct(v, video, project)
    assert result.target_status == JOB_FAILED


# ===========================================================================
# 17. Compilacao -- garante que o modulo nao tem erro de sintaxe/import
#     (redundante com compileall da entrega, mas roda junto da suite)
# ===========================================================================


def test_modulo_importa_sem_erro():
    assert fmv.FinalMediaValidator is not None
    assert fmv.OPERATION_VALIDATE_MEDIA == "VALIDATE_MEDIA"


def test_modulo_nao_importa_storage_manager_nem_app_paths():
    """Decisão de design (ver seção 0.1 da docstring do módulo):
    ``FinalMediaValidator`` nunca aloca/escreve arquivos de mídia -- só
    lê e, no máximo, corrige um metadado via ``LocalDatabase.save``.
    ``StorageManager``/``AppPaths`` são dependências deliberadamente
    omitidas, nunca esquecidas -- este teste estrutural (AST, não regex)
    prova isso a nível de import do módulo."""
    import ast

    source = Path(fmv.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])
    assert "StorageManager" not in imported_names
    assert "AppPaths" not in imported_names
    assert "build_app_paths" not in imported_names
