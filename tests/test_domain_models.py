import ast
from pathlib import Path
from uuid import UUID

import pytest

from _sistema.time_utils import MissingTimezoneConfigurationError

from _sistema.domain import (
    Account,
    Artifact,
    ErrorRecord,
    Job,
    Project,
    Publication,
    Schedule,
    SourceAsset,
    Template,
    Video,
    SCHEDULE_LOCAL_PENDING,
    SCHEDULE_REMOTE_SCHEDULED,
    SCHEDULE_TIME_MANUAL,
    SCHEDULE_TIME_RECOMMENDED,
    SCHEDULE_UNKNOWN,
    JOB_PROCESSING,
)


MODEL_TYPES = [
    SourceAsset,
    Video,
    Project,
    Account,
    Job,
    Publication,
    Schedule,
    Template,
    Artifact,
    ErrorRecord,
]


@pytest.mark.parametrize("model_type", MODEL_TYPES)
def test_modelos_criam_uuid_persistivel_valido(model_type):
    obj = model_type()
    assert str(UUID(obj.id)) == obj.id

    restored = model_type.from_dict(obj.to_dict())
    assert restored.id == obj.id
    assert restored.to_dict() == obj.to_dict()


def test_instancias_novas_recebem_ids_distintos():
    assert Video().id != Video().id
    assert Job().id != Job().id
    assert Publication().id != Publication().id


def test_source_asset_video_e_project_sao_entidades_separadas():
    source = SourceAsset(
        source_uri="file:///originais/video.mp4",
        local_path=r"C:\Originais\video.mp4",
        original_name="video.mp4",
        fingerprint="abc123",
    )
    video = Video(source_asset_id=source.id, name="Vídeo 1")
    project = Project(video_id=video.id, name="Corte vertical", edit_state={"ratio": "9:16"})

    assert video.source_asset_id == source.id
    assert project.video_id == video.id
    assert source.id != video.id != project.id
    assert "edit_state" not in source.to_dict()


def test_um_video_pode_gerar_varios_jobs_e_publications():
    video = Video(name="Vídeo")
    account_a = Account(platform="youtube", name="Canal A")
    account_b = Account(platform="tiktok", name="Conta B")

    jobs = [
        Job(video_id=video.id, operation="TRANSCRIBE"),
        Job(video_id=video.id, operation="RENDER"),
    ]
    publications = [
        Publication(video_id=video.id, account_id=account_a.id),
        Publication(video_id=video.id, account_id=account_b.id),
    ]

    assert len({job.id for job in jobs}) == 2
    assert {job.video_id for job in jobs} == {video.id}
    assert len({pub.id for pub in publications}) == 2
    assert {pub.video_id for pub in publications} == {video.id}


def test_job_de_processamento_e_publication_nao_compartilham_identidade_ou_estado():
    video = Video()
    job = Job(video_id=video.id, operation="RENDER", status=JOB_PROCESSING, progress=0.5)
    publication = Publication(video_id=video.id, status="UNKNOWN")

    assert job.id != publication.id
    assert job.status == JOB_PROCESSING
    assert publication.status == "UNKNOWN"
    assert "operation" in job.to_dict()
    assert "remote_id" in publication.to_dict()


def test_artifact_e_saida_derivada_sem_substituir_source_asset():
    source = SourceAsset(local_path=r"C:\Originais\a.mp4")
    video = Video(source_asset_id=source.id)
    job = Job(video_id=video.id, operation="RENDER")
    artifact = Artifact(video_id=video.id, job_id=job.id, kind="RENDERED_VIDEO", path=r"D:\Saidas\001.mp4")

    assert artifact.job_id == job.id
    assert artifact.video_id == video.id
    assert artifact.path != source.local_path
    assert source.id != artifact.id


def test_schedule_distingue_local_remote_e_unknown():
    publication = Publication()
    local = Schedule(publication_id=publication.id, delivery_state=SCHEDULE_LOCAL_PENDING)
    remote = Schedule(publication_id=publication.id, delivery_state=SCHEDULE_REMOTE_SCHEDULED)
    unknown = Schedule(publication_id=publication.id, delivery_state=SCHEDULE_UNKNOWN)

    assert local.delivery_state == "LOCAL_PENDING"
    assert remote.delivery_state == "REMOTE_SCHEDULED"
    assert unknown.delivery_state == "UNKNOWN"
    assert len({local.delivery_state, remote.delivery_state, unknown.delivery_state}) == 3


def test_schedule_aceita_unknown_e_roundtrip_preserva_unknown():
    publication = Publication()
    schedule = Schedule(publication_id=publication.id, delivery_state=SCHEDULE_UNKNOWN)

    restored = Schedule.from_dict(schedule.to_dict())

    assert restored.delivery_state == SCHEDULE_UNKNOWN
    assert restored.delivery_state != SCHEDULE_LOCAL_PENDING
    assert restored.delivery_state != SCHEDULE_REMOTE_SCHEDULED
    assert restored.publication_id == publication.id


def test_schedule_preserva_local_timezone_utc_e_origem_manual():
    publication = Publication()
    schedule = Schedule(
        publication_id=publication.id,
        scheduled_local="2026-09-17T10:00",
        timezone_iana="America/Sao_Paulo",
        time_origin=SCHEDULE_TIME_MANUAL,
    )

    assert schedule.scheduled_local == "2026-09-17T10:00:00"
    assert schedule.timezone_iana == "America/Sao_Paulo"
    assert schedule.scheduled_utc == "2026-09-17T13:00:00+00:00"
    assert schedule.time_origin == "MANUAL"

    restored = Schedule.from_dict(schedule.to_dict())
    assert restored.to_dict() == schedule.to_dict()


def test_schedule_aceita_origem_recommended_sem_implementar_recomendador():
    schedule = Schedule(
        scheduled_local="2026-09-17T10:00",
        timezone_iana="America/Sao_Paulo",
        time_origin=SCHEDULE_TIME_RECOMMENDED,
    )
    assert schedule.time_origin == "RECOMMENDED"


def test_schedule_rejeita_origem_de_horario_invalida():
    with pytest.raises(ValueError, match="MANUAL ou RECOMMENDED"):
        Schedule(time_origin="AUTO")


def test_schedule_rejeita_timezone_nao_iana():
    with pytest.raises(ValueError, match="timezone IANA"):
        Schedule(timezone_iana="Sao Paulo")


def test_schedule_rejeita_local_e_utc_inconsistentes():
    with pytest.raises(ValueError, match="não correspondem"):
        Schedule(
            scheduled_local="2026-09-17T10:00",
            timezone_iana="America/Sao_Paulo",
            scheduled_utc="2026-09-17T14:00:00+00:00",
        )


def test_schedule_payload_legado_e_convertido_sem_reemitir_aliases_antigos():
    schedule = Schedule()
    payload = schedule.to_dict()
    payload.pop("scheduled_local")
    payload.pop("scheduled_utc")
    payload.pop("timezone_iana")
    payload["scheduled_for"] = "2026-09-17T13:00:00+00:00"
    payload["timezone_name"] = "America/Sao_Paulo"

    restored = Schedule.from_dict(payload)
    serialized = restored.to_dict()

    assert restored.scheduled_local == "2026-09-17T10:00:00"
    assert restored.scheduled_utc == "2026-09-17T13:00:00+00:00"
    assert restored.timezone_iana == "America/Sao_Paulo"
    assert "scheduled_for" not in serialized
    assert "timezone_name" not in serialized


def test_schedule_rejeita_estado_de_entrega_invalido():
    with pytest.raises(ValueError):
        Schedule(delivery_state="SCHEDULED")


def test_publication_nao_possui_schedule_id_e_schedule_referencia_publication():
    publication = Publication()
    schedule = Schedule(publication_id=publication.id)

    assert not hasattr(publication, "schedule_id")
    assert "schedule_id" not in publication.to_dict()
    assert schedule.publication_id == publication.id


def test_schedule_publication_id_aceita_uuid_valido_e_rejeita_invalido():
    publication = Publication()
    schedule = Schedule(publication_id=publication.id)
    assert schedule.publication_id == publication.id

    with pytest.raises(ValueError, match="publication_id"):
        Schedule(publication_id="nao-e-uuid")


def test_referencias_uuid_invalidas_sao_rejeitadas():
    with pytest.raises(ValueError):
        Video(source_asset_id="nao-e-uuid")
    with pytest.raises(ValueError):
        Project(video_id="nao-e-uuid")
    with pytest.raises(ValueError):
        Publication(account_id="nao-e-uuid")


def test_roundtrip_preserva_campos_desconhecidos_para_compatibilidade():
    original = Video(name="Compatível")
    payload = original.to_dict()
    payload["future_field"] = {"enabled": True, "version": 2}

    restored = Video.from_dict(payload)
    serialized_again = restored.to_dict()

    assert restored.id == original.id
    assert serialized_again["future_field"] == payload["future_field"]


def test_publication_from_dict_descarta_schedule_id_legado_sem_criar_atributo():
    publication = Publication(title="Título legado")
    payload = publication.to_dict()
    payload["schedule_id"] = str(UUID(int=1))

    restored = Publication.from_dict(payload)

    assert not hasattr(restored, "schedule_id")


def test_publication_schedule_id_legado_nao_entra_em_extra():
    publication = Publication(title="Título legado")
    payload = publication.to_dict()
    payload["schedule_id"] = str(UUID(int=2))

    restored = Publication.from_dict(payload)

    assert "schedule_id" not in restored.extra


def test_publication_to_dict_nao_reemite_schedule_id_legado():
    publication = Publication(title="Título legado")
    payload = publication.to_dict()
    payload["schedule_id"] = str(UUID(int=3))

    restored = Publication.from_dict(payload)
    serialized_again = restored.to_dict()

    assert "schedule_id" not in serialized_again


def test_publication_tombstone_preserva_demais_campos_desconhecidos_do_payload():
    publication = Publication(title="Título legado")
    payload = publication.to_dict()
    payload["schedule_id"] = str(UUID(int=4))
    payload["legacy_note"] = {"source": "v7", "keep": True}

    restored = Publication.from_dict(payload)
    serialized_again = restored.to_dict()

    assert restored.title == "Título legado"
    assert restored.extra["legacy_note"] == payload["legacy_note"]
    assert serialized_again["legacy_note"] == payload["legacy_note"]
    assert "schedule_id" not in serialized_again


def test_publication_tombstone_tambem_remove_schedule_id_embutido_em_extra():
    publication = Publication(title="Título legado")
    payload = publication.to_dict()
    payload["extra"] = {
        "schedule_id": str(UUID(int=5)),
        "future_flag": True,
    }

    restored = Publication.from_dict(payload)
    serialized_again = restored.to_dict()

    assert restored.extra == {"future_flag": True}
    assert serialized_again["future_flag"] is True
    assert "schedule_id" not in serialized_again


def test_from_dict_nao_regenera_uuid_existente():
    video = Video(name="Persistente")
    payload = video.to_dict()

    first = Video.from_dict(payload)
    second = Video.from_dict(payload)

    assert first.id == video.id
    assert second.id == video.id


def test_template_independente_de_video_conta_e_plataforma():
    template = Template(name="Vertical 01", layout={"video": [0, 0, 1080, 1920]})
    data = template.to_dict()

    assert "video_id" not in data
    assert "account_id" not in data
    assert "platform" not in data


def test_error_record_pode_apontar_para_job_sem_depender_da_ui():
    job = Job(operation="TRANSCRIBE")
    err = ErrorRecord(
        code="TRANSCRIBE_FAILED",
        message="Falha controlada",
        entity_type="Job",
        entity_id=job.id,
        job_id=job.id,
        recoverable=True,
        details={"attempt": 1},
    )

    restored = ErrorRecord.from_dict(err.to_dict())
    assert restored.job_id == job.id
    assert restored.entity_type == "Job"
    assert restored.recoverable is True
    assert restored.details == {"attempt": 1}


def test_from_dict_exige_id_persistido_em_vez_de_regenerar_identidade():
    with pytest.raises(ValueError, match="deve conter id"):
        Video.from_dict({"name": "Sem identidade"})


def test_from_dict_rejeita_payload_de_outro_tipo_de_entidade():
    publication = Publication()
    with pytest.raises(ValueError, match="model_type incompatível"):
        Video.from_dict(publication.to_dict())


def test_camada_de_dominio_nao_importa_ui_plataformas_ou_persistencia():
    models_path = Path(__file__).parents[1] / "_sistema" / "domain" / "models.py"
    tree = ast.parse(models_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"playwright", "sqlite3", "tkinter", "PyQt5", "PySide6"}
    assert imported.isdisjoint(forbidden)
    assert "agendar_youtube" not in imported
    assert "agendar_tiktok" not in imported


def test_schedule_com_local_sem_timezone_explicito_e_rejeitado():
    with pytest.raises(MissingTimezoneConfigurationError, match="timezone_iana"):
        Schedule(scheduled_local="2026-09-17T10:00")


def test_schedule_com_utc_sem_timezone_explicito_e_rejeitado():
    with pytest.raises(MissingTimezoneConfigurationError, match="timezone_iana"):
        Schedule(scheduled_utc="2026-09-17T13:00:00+00:00")


def test_schedule_sem_horario_pode_existir_sem_timezone():
    schedule = Schedule()
    assert schedule.scheduled_local is None
    assert schedule.scheduled_utc is None
    assert schedule.timezone_iana is None


def test_schedule_com_timezone_explicito_funciona_normalmente():
    schedule = Schedule(
        scheduled_local="2026-09-17T10:00",
        timezone_iana="America/Sao_Paulo",
    )
    assert schedule.timezone_iana == "America/Sao_Paulo"
    assert schedule.scheduled_utc == "2026-09-17T13:00:00+00:00"
