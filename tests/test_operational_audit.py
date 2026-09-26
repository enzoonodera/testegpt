from contextlib import closing
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from tests.windows_tempdir import robust_temporary_directory

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHED,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_RETRY,
    JOB_UNKNOWN,
)
from _sistema.storage import (
    AUDIT_JOB_CANCELLED,
    AUDIT_JOB_CONFIRMED,
    AUDIT_JOB_CREATED,
    AUDIT_JOB_ERROR,
    AUDIT_JOB_PAUSED,
    AUDIT_JOB_PROCESSING_COMPLETED,
    AUDIT_JOB_RECONCILIATION,
    AUDIT_JOB_RECOVERY,
    AUDIT_JOB_RESUMED,
    AUDIT_JOB_RETRY,
    AUDIT_JOB_STATE_CHANGED,
    AUDIT_JOB_UPLOAD_STARTED,
    AuditHistoryError,
    LocalDatabase,
    OperationalAuditLog,
)


@pytest.fixture
def local_db():
    # ``robust_temporary_directory`` -- este arquivo abre uma segunda
    # instância de ``LocalDatabase`` contra o mesmo arquivo (restart real,
    # linha ~290) -- mesmo padrão que expôs no Windows um
    # ``PermissionError`` (``WinError 32``) transitório no teardown. Ver
    # CORRECAO_WINDOWS_SQLITE_WAL_TEARDOWN_RELATORIO.md e
    # tests/windows_tempdir.py.
    with robust_temporary_directory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(paths=paths)
        db.initialize()
        yield db


@pytest.fixture
def audit(local_db):
    return OperationalAuditLog(local_db)


def test_job_criado_e_evento_sao_atomicos(local_db, audit):
    job = Job(operation="RENDER")
    audit.create_job(job, data={"source": "test"})

    stored = local_db.get(Job, job.id)
    events = audit.list_job_events(job.id)

    assert stored is not None
    assert stored.status == JOB_PENDING
    assert [event["event_type"] for event in events] == [AUDIT_JOB_CREATED]
    assert events[0]["data"]["initial_state"] == JOB_PENDING
    assert events[0]["data"]["operation"] == "RENDER"
    assert events[0]["data"]["source"] == "test"


def test_state_change_registra_from_to_e_persiste_job(local_db, audit):
    job = Job(operation="RENDER")
    audit.create_job(job)

    updated = audit.transition_job(job.id, JOB_PROCESSING)

    assert updated.status == JOB_PROCESSING
    assert local_db.get(Job, job.id).status == JOB_PROCESSING
    events = audit.list_job_events(job.id)
    assert [event["event_type"] for event in events] == [
        AUDIT_JOB_CREATED,
        AUDIT_JOB_STATE_CHANGED,
    ]
    assert events[-1]["data"] == {
        "from_state": JOB_PENDING,
        "to_state": JOB_PROCESSING,
    }


def test_transicao_invalida_nao_altera_job_nem_audit(local_db, audit):
    job = Job(status=JOB_PUBLISHED)
    audit.create_job(job)
    before = audit.list_job_events(job.id)

    with pytest.raises(Exception):
        audit.transition_job(job.id, JOB_PENDING)

    assert local_db.get(Job, job.id).status == JOB_PUBLISHED
    assert audit.list_job_events(job.id) == before


def test_pause_resume_cancel_retry_e_processamento_concluido_geram_eventos_semanticos(local_db, audit):
    paused = Job(operation="RENDER", status=JOB_PROCESSING)
    audit.create_job(paused)
    audit.pause(paused.id)
    audit.resume(paused.id)
    audit.processing_completed(paused.id)

    retry = Job(operation="RENDER", status=JOB_FAILED)
    audit.create_job(retry)
    audit.retry(retry.id)

    cancelled = Job(operation="RENDER")
    audit.create_job(cancelled)
    audit.cancel(cancelled.id)

    paused_types = [event["event_type"] for event in audit.list_job_events(paused.id)]
    assert AUDIT_JOB_PAUSED in paused_types
    assert AUDIT_JOB_RESUMED in paused_types
    assert AUDIT_JOB_PROCESSING_COMPLETED in paused_types
    assert local_db.get(Job, paused.id).status == JOB_READY

    retry_types = [event["event_type"] for event in audit.list_job_events(retry.id)]
    assert AUDIT_JOB_RETRY in retry_types
    assert local_db.get(Job, retry.id).status == JOB_RETRY

    cancel_types = [event["event_type"] for event in audit.list_job_events(cancelled.id)]
    assert AUDIT_JOB_CANCELLED in cancel_types
    assert local_db.get(Job, cancelled.id).status == JOB_CANCELLED


def test_upload_confirmacao_reconciliacao_erro_e_recovery_sao_registraveis(local_db, audit):
    publish = Job(operation="PUBLISH", status=JOB_READY)
    audit.create_job(publish)
    audit.upload_started(publish.id, data={"connector": "youtube"})
    audit.confirmed(publish.id, data={"remote_id": "abc"})

    uncertain = Job(operation="PUBLISH", status=JOB_UNKNOWN)
    audit.create_job(uncertain)
    audit.reconciliation_started(uncertain.id)
    audit.record_reconciliation(uncertain.id, result="NO_REMOTE_EFFECT")
    audit.record_error(
        uncertain.id,
        code="NETWORK_TIMEOUT",
        message="timeout durante consulta",
        recoverable=True,
    )
    audit.record_recovery(uncertain.id, phase="CHECKPOINT_RESTORED")

    publish_types = [event["event_type"] for event in audit.list_job_events(publish.id)]
    assert AUDIT_JOB_UPLOAD_STARTED in publish_types
    assert AUDIT_JOB_CONFIRMED in publish_types
    assert local_db.get(Job, publish.id).status == JOB_PUBLISHED

    uncertain_types = [event["event_type"] for event in audit.list_job_events(uncertain.id)]
    assert uncertain_types.count(AUDIT_JOB_RECONCILIATION) == 2
    assert AUDIT_JOB_ERROR in uncertain_types
    assert AUDIT_JOB_RECOVERY in uncertain_types
    assert local_db.get(Job, uncertain.id).status == JOB_RECOVERING


def test_reconstrucao_do_historico_de_job_preserva_caminho_de_estados(audit):
    job = Job(operation="RENDER")
    audit.create_job(job)
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.pause(job.id)
    audit.resume(job.id)
    audit.processing_completed(job.id)

    history = audit.reconstruct_job_history(job.id)

    assert history.job_id == job.id
    assert history.initial_state == JOB_PENDING
    assert history.current_state == JOB_READY
    assert history.state_path == (
        JOB_PENDING,
        JOB_PROCESSING,
        JOB_PAUSED,
        JOB_PROCESSING,
        JOB_READY,
    )
    sequences = [event["sequence"] for event in history.events]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))


def test_reconstrucao_detecta_cadeia_de_estado_inconsistente(local_db, audit):
    job = Job(operation="RENDER")
    audit.create_job(job)
    local_db.append_audit_event(
        AUDIT_JOB_STATE_CHANGED,
        entity_type="Job",
        entity_id=job.id,
        data={"from_state": JOB_FAILED, "to_state": JOB_RETRY},
    )

    with pytest.raises(AuditHistoryError, match="cadeia de estados inconsistente"):
        audit.reconstruct_job_history(job.id)


def test_audit_events_sao_append_only_tambem_contra_update_delete_sql(local_db, audit):
    job = Job(operation="RENDER")
    audit.create_job(job)
    event = audit.list_job_events(job.id)[0]

    with pytest.raises(sqlite3.DatabaseError):
        with local_db.transaction() as conn:
            conn.execute("UPDATE audit_events SET event_type='ALTERED' WHERE id=?", (event["id"],))

    with pytest.raises(sqlite3.DatabaseError):
        with local_db.transaction() as conn:
            conn.execute("DELETE FROM audit_events WHERE id=?", (event["id"],))

    after = audit.list_job_events(job.id)
    assert len(after) == 1
    assert after[0]["event_type"] == AUDIT_JOB_CREATED


def test_audit_append_only_persistente_rejeita_update_delete_e_insert_or_replace(local_db, audit):
    job = Job(operation="RENDER")
    audit.create_job(job)
    original = audit.list_job_events(job.id)[0]

    # Usa conexão sqlite3 crua para provar que a proteção está persistida no
    # schema e não depende apenas do authorizer de LocalDatabase.
    with closing(sqlite3.connect(str(local_db.path), isolation_level=None)) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE audit_events SET event_type='ALTERED' WHERE id=?",
                (original["id"],),
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM audit_events WHERE id=?", (original["id"],))
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "INSERT OR REPLACE INTO audit_events(id, event_type, entity_type, entity_id, data_json, created_at) "
                "VALUES (?, 'ALTERED', 'Job', ?, '{}', ?)",
                (original["id"], job.id, original["created_at"]),
            )

        fresh_id = str(__import__("uuid").uuid4())
        conn.execute(
            "INSERT INTO audit_events(id, event_type, entity_type, entity_id, data_json, created_at) "
            "VALUES (?, 'EXTERNAL_APPEND_TEST', 'Job', ?, '{}', ?)",
            (fresh_id, job.id, original["created_at"]),
        )

    after = local_db.list_audit_events(entity_type="Job", entity_id=job.id)
    assert after[0]["id"] == original["id"]
    assert after[0]["event_type"] == AUDIT_JOB_CREATED
    assert after[0]["data"] == original["data"]
    assert after[0]["created_at"] == original["created_at"]
    assert any(event["id"] == fresh_id and event["event_type"] == "EXTERNAL_APPEND_TEST" for event in after)

    # A reconstrução do histórico ignora evento semântico desconhecido e
    # preserva o caminho canônico do Job.
    history = audit.reconstruct_job_history(job.id)
    assert history.state_path == (JOB_PENDING,)
    assert history.current_state == JOB_PENDING


def test_listagem_pode_filtrar_por_job_e_event_type(local_db, audit):
    first = Job(operation="A")
    second = Job(operation="B")
    audit.create_job(first)
    audit.create_job(second)
    audit.transition_job(first.id, JOB_PROCESSING)

    first_events = local_db.list_audit_events(entity_type="Job", entity_id=first.id)
    transitions = local_db.list_audit_events(
        entity_type="Job",
        entity_id=first.id,
        event_type=AUDIT_JOB_STATE_CHANGED,
    )

    assert len(first_events) == 2
    assert len(transitions) == 1
    assert transitions[0]["entity_id"] == first.id



def test_historico_pode_ser_reconstruido_em_nova_instancia_do_storage(local_db, audit):
    job = Job(operation="RENDER")
    audit.create_job(job)
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.processing_completed(job.id)

    restarted_db = LocalDatabase(path=local_db.path, paths=local_db.paths)
    restarted_db.initialize()
    restarted_audit = OperationalAuditLog(restarted_db)
    history = restarted_audit.reconstruct_job_history(job.id)

    assert history.state_path == (JOB_PENDING, JOB_PROCESSING, JOB_READY)
    assert history.current_state == JOB_READY


def test_timestamps_de_audit_sao_utc_aware(audit):
    job = Job(operation="RENDER")
    audit.create_job(job)
    event = audit.list_job_events(job.id)[0]

    parsed = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
    assert parsed.utcoffset().total_seconds() == 0


def test_evento_semantico_e_state_change_sao_atomicos(local_db, audit, monkeypatch):
    job = Job(operation="RENDER", status=JOB_PROCESSING)
    audit.create_job(job)
    original_append = local_db.append_audit_event
    calls = {"count": 0}

    def failing_append(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("falha simulada no evento semântico")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(local_db, "append_audit_event", failing_append)

    with pytest.raises(RuntimeError, match="falha simulada"):
        audit.pause(job.id)

    assert local_db.get(Job, job.id).status == JOB_PROCESSING
    events = audit.list_job_events(job.id)
    assert [event["event_type"] for event in events] == [AUDIT_JOB_CREATED]
