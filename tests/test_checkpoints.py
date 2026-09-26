"""Testes do PROMPT 13 — Checkpoints persistentes por etapa de Job.

Cobre o vocabulário de domínio (`_sistema.domain.checkpoints`) e a
persistência/idempotência/concorrência de
`OperationalAuditLog.record_checkpoint` e leituras associadas
(`_sistema.storage.audit`). Reaproveita a tabela `audit_events` já criada
pelas migrations 001/002 (append-only, indexada por entidade, protegida
contra UPDATE/DELETE) — nenhuma migration nova foi criada para este prompt.
"""
import threading
from pathlib import Path
import tempfile
import uuid

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    CHECKPOINT_ANALYZED,
    CHECKPOINT_IMPORTED,
    CHECKPOINT_REMOTE_CONFIRMED,
    CHECKPOINT_RENDERED,
    CHECKPOINT_TRANSCRIBED,
    CHECKPOINT_UPLOAD_STARTED,
    CheckpointVocabulary,
    InvalidCheckpointError,
    Job,
    JOB_CHECKPOINTS,
    JOB_PENDING,
    JOB_PROCESSING,
    validate_checkpoint,
)
from _sistema.storage import (
    AUDIT_JOB_CHECKPOINT_REACHED,
    AUDIT_JOB_STATE_CHANGED,
    JobNotFoundForAuditError,
    LocalDatabase,
    OperationalAuditLog,
)
from _sistema.domain import JOB_PUBLISHING, JOB_RECOVERING
from _sistema.recovery_manager import ACTION_FLAGGED_FOR_RECONCILIATION, RecoveryManager


# ---------------------------------------------------------------------------
# Vocabulário de domínio
# ---------------------------------------------------------------------------


def test_job_checkpoints_contem_exatamente_o_vocabulario_do_roadmap():
    assert JOB_CHECKPOINTS == {
        "IMPORTED",
        "TRANSCRIBED",
        "ANALYZED",
        "EDIT_PLANNED",
        "MEDIA_PROCESSED",
        "COMPOSED",
        "RENDERED",
        "VALIDATED",
        "UPLOAD_STARTED",
        "REMOTE_CONFIRMED",
    }


@pytest.mark.parametrize("checkpoint", sorted(JOB_CHECKPOINTS))
def test_validate_checkpoint_aceita_todo_o_vocabulario(checkpoint):
    assert validate_checkpoint(checkpoint) == checkpoint
    assert CheckpointVocabulary.validate(checkpoint) == checkpoint


def test_validate_checkpoint_rejeita_nome_fora_do_vocabulario():
    with pytest.raises(InvalidCheckpointError):
        validate_checkpoint("NAO_EXISTE")
    with pytest.raises(InvalidCheckpointError):
        validate_checkpoint("")
    with pytest.raises(InvalidCheckpointError):
        validate_checkpoint("imported")  # case-sensitive: minúsculo não é aceito


# ---------------------------------------------------------------------------
# Fixtures de storage
# ---------------------------------------------------------------------------


@pytest.fixture
def local_db():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(paths=paths)
        db.initialize()
        yield db


@pytest.fixture
def audit(local_db):
    return OperationalAuditLog(local_db)


def _create_job(audit_log, *, operation="RENDER"):
    return audit_log.create_job(Job(operation=operation))


# ---------------------------------------------------------------------------
# Persistência básica
# ---------------------------------------------------------------------------


def test_record_checkpoint_persiste_evento(audit, local_db):
    job = _create_job(audit)

    created = audit.record_checkpoint(job.id, CHECKPOINT_IMPORTED)

    assert created is True
    events = audit.list_checkpoints(job.id)
    assert len(events) == 1
    assert events[0]["event_type"] == AUDIT_JOB_CHECKPOINT_REACHED
    assert events[0]["data"]["checkpoint"] == CHECKPOINT_IMPORTED
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_IMPORTED) is True
    assert audit.latest_checkpoint(job.id) == CHECKPOINT_IMPORTED


def test_record_checkpoint_aceita_dados_extras():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(paths=paths)
        db.initialize()
        audit = OperationalAuditLog(db)
        job = _create_job(audit)

        audit.record_checkpoint(job.id, CHECKPOINT_TRANSCRIBED, data={"language": "pt-BR"})

        events = audit.list_checkpoints(job.id)
        assert events[0]["data"]["language"] == "pt-BR"
        assert events[0]["data"]["checkpoint"] == CHECKPOINT_TRANSCRIBED


def test_multiplos_checkpoints_diferentes_sao_todos_persistidos_em_ordem(audit):
    job = _create_job(audit)

    audit.record_checkpoint(job.id, CHECKPOINT_IMPORTED)
    audit.record_checkpoint(job.id, CHECKPOINT_TRANSCRIBED)
    audit.record_checkpoint(job.id, CHECKPOINT_ANALYZED)

    checkpoints = [event["data"]["checkpoint"] for event in audit.list_checkpoints(job.id)]
    assert checkpoints == [CHECKPOINT_IMPORTED, CHECKPOINT_TRANSCRIBED, CHECKPOINT_ANALYZED]
    assert audit.latest_checkpoint(job.id) == CHECKPOINT_ANALYZED


# ---------------------------------------------------------------------------
# Sobrevive a fechar/reabrir o banco (crash/restart)
# ---------------------------------------------------------------------------


def test_checkpoint_sobrevive_a_reabertura_do_banco():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")

        db_first_run = LocalDatabase(paths=paths)
        db_first_run.initialize()
        audit_first_run = OperationalAuditLog(db_first_run)
        job = _create_job(audit_first_run)
        audit_first_run.record_checkpoint(job.id, CHECKPOINT_IMPORTED)
        audit_first_run.record_checkpoint(job.id, CHECKPOINT_TRANSCRIBED)
        # Nenhuma conexão fica aberta entre chamadas (LocalDatabase abre e
        # fecha uma conexão por operação), então não há nada para "fechar"
        # explicitamente — mas simulamos reinício do processo com uma nova
        # instância de LocalDatabase apontando para o mesmo arquivo.
        del db_first_run, audit_first_run

        db_after_restart = LocalDatabase(paths=paths)  # não chama initialize() de novo
        audit_after_restart = OperationalAuditLog(db_after_restart)

        checkpoints = [
            event["data"]["checkpoint"] for event in audit_after_restart.list_checkpoints(job.id)
        ]
        assert checkpoints == [CHECKPOINT_IMPORTED, CHECKPOINT_TRANSCRIBED]
        assert audit_after_restart.has_reached_checkpoint(job.id, CHECKPOINT_TRANSCRIBED) is True
        assert audit_after_restart.latest_checkpoint(job.id) == CHECKPOINT_TRANSCRIBED


# ---------------------------------------------------------------------------
# Isolamento entre Jobs
# ---------------------------------------------------------------------------


def test_checkpoint_pertence_ao_job_correto_e_jobs_nao_compartilham_checkpoints(audit):
    job_a = _create_job(audit, operation="A")
    job_b = _create_job(audit, operation="B")

    audit.record_checkpoint(job_a.id, CHECKPOINT_IMPORTED)

    assert audit.has_reached_checkpoint(job_a.id, CHECKPOINT_IMPORTED) is True
    assert audit.has_reached_checkpoint(job_b.id, CHECKPOINT_IMPORTED) is False
    assert audit.list_checkpoints(job_b.id) == []
    assert audit.latest_checkpoint(job_b.id) is None

    # Registrar o mesmo checkpoint para o Job B não deve afetar o Job A.
    audit.record_checkpoint(job_b.id, CHECKPOINT_IMPORTED)
    assert len(audit.list_checkpoints(job_a.id)) == 1
    assert len(audit.list_checkpoints(job_b.id)) == 1


# ---------------------------------------------------------------------------
# Idempotência (sequencial)
# ---------------------------------------------------------------------------


def test_record_checkpoint_e_idempotente_nao_duplica(audit):
    job = _create_job(audit)

    first = audit.record_checkpoint(job.id, CHECKPOINT_RENDERED)
    second = audit.record_checkpoint(job.id, CHECKPOINT_RENDERED)
    third = audit.record_checkpoint(job.id, CHECKPOINT_RENDERED, data={"attempt": 3})

    assert first is True
    assert second is False
    assert third is False

    events = audit.list_checkpoints(job.id)
    assert len(events) == 1  # nunca duplica, mesmo com payloads diferentes
    assert events[0]["data"]["checkpoint"] == CHECKPOINT_RENDERED
    assert "attempt" not in events[0]["data"]  # a 1ª evidência persistida é a que vale


def test_repeticao_idempotente_nao_corrompe_historico_de_outros_checkpoints(audit):
    job = _create_job(audit)

    audit.record_checkpoint(job.id, CHECKPOINT_IMPORTED)
    audit.record_checkpoint(job.id, CHECKPOINT_TRANSCRIBED)
    audit.record_checkpoint(job.id, CHECKPOINT_IMPORTED)  # repetição no meio

    checkpoints = [event["data"]["checkpoint"] for event in audit.list_checkpoints(job.id)]
    assert checkpoints == [CHECKPOINT_IMPORTED, CHECKPOINT_TRANSCRIBED]


# ---------------------------------------------------------------------------
# Concorrência real (dois processos/instâncias contra o mesmo arquivo)
# ---------------------------------------------------------------------------


def test_concorrencia_no_mesmo_checkpoint_nao_produz_linhas_duplicadas():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")

        db_a = LocalDatabase(paths=paths)
        db_a.initialize()
        db_b = LocalDatabase(paths=paths)  # simula um segundo processo/worker

        audit_a = OperationalAuditLog(db_a)
        audit_b = OperationalAuditLog(db_b)

        job = _create_job(audit_a)

        results: dict[str, bool] = {}
        barrier = threading.Barrier(2)

        def run(name, audit_log):
            barrier.wait()
            results[name] = audit_log.record_checkpoint(job.id, CHECKPOINT_RENDERED)

        t1 = threading.Thread(target=run, args=("a", audit_a))
        t2 = threading.Thread(target=run, args=("b", audit_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Diferente da reivindicação de Job (JobEngine): aqui NENHUMA das
        # duas chamadas precisa falhar — ambas terminam com sucesso, mas
        # exatamente uma persiste o evento (created=True) e a outra apenas
        # observa que já existia (created=False).
        assert set(results) == {"a", "b"}
        assert sorted(results.values()) == [False, True]

        events = db_a.list_audit_events(
            entity_type="Job", entity_id=job.id, event_type=AUDIT_JOB_CHECKPOINT_REACHED
        )
        assert len(events) == 1
        assert events[0]["data"]["checkpoint"] == CHECKPOINT_RENDERED


def test_concorrencia_em_checkpoints_diferentes_persiste_ambos(audit):
    job = _create_job(audit)
    results: dict[str, bool] = {}
    barrier = threading.Barrier(2)

    def run(name, checkpoint):
        barrier.wait()
        results[name] = audit.record_checkpoint(job.id, checkpoint)

    t1 = threading.Thread(target=run, args=("imported", CHECKPOINT_IMPORTED))
    t2 = threading.Thread(target=run, args=("transcribed", CHECKPOINT_TRANSCRIBED))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results == {"imported": True, "transcribed": True}
    checkpoints = {event["data"]["checkpoint"] for event in audit.list_checkpoints(job.id)}
    assert checkpoints == {CHECKPOINT_IMPORTED, CHECKPOINT_TRANSCRIBED}


# ---------------------------------------------------------------------------
# Entradas inválidas
# ---------------------------------------------------------------------------


def test_record_checkpoint_rejeita_nome_invalido(audit):
    job = _create_job(audit)

    with pytest.raises(InvalidCheckpointError):
        audit.record_checkpoint(job.id, "NAO_EXISTE")

    assert audit.list_checkpoints(job.id) == []


def test_record_checkpoint_job_inexistente_e_tratado_corretamente(audit):
    with pytest.raises(JobNotFoundForAuditError):
        audit.record_checkpoint(str(uuid.uuid4()), CHECKPOINT_IMPORTED)


def test_record_checkpoint_rejeita_job_id_com_formato_invalido(audit):
    with pytest.raises(ValueError):
        audit.record_checkpoint("nao-e-um-uuid", CHECKPOINT_IMPORTED)


def test_leituras_de_checkpoint_para_job_inexistente_nao_levantam_erro(audit):
    # Mesmo comportamento de list_job_events: leitura não exige que o Job
    # exista, só grava (record_checkpoint) exige.
    missing_id = str(uuid.uuid4())
    assert audit.list_checkpoints(missing_id) == []
    assert audit.has_reached_checkpoint(missing_id, CHECKPOINT_IMPORTED) is False
    assert audit.latest_checkpoint(missing_id) is None


# ---------------------------------------------------------------------------
# Checkpoint não substitui/altera Job.status
# ---------------------------------------------------------------------------


def test_record_checkpoint_nao_altera_job_status(audit, local_db):
    job = _create_job(audit)
    assert job.status == JOB_PENDING

    audit.record_checkpoint(job.id, CHECKPOINT_IMPORTED)
    audit.record_checkpoint(job.id, CHECKPOINT_TRANSCRIBED)

    stored = local_db.get(Job, job.id)
    assert stored.status == JOB_PENDING  # checkpoints não tocam o status

    audit.transition_job(job.id, JOB_PROCESSING)
    audit.record_checkpoint(job.id, CHECKPOINT_ANALYZED)

    stored_again = local_db.get(Job, job.id)
    assert stored_again.status == JOB_PROCESSING  # inalterado pelo checkpoint seguinte


# ---------------------------------------------------------------------------
# Semântica crítica de publicação: UPLOAD_STARTED != REMOTE_CONFIRMED
# ---------------------------------------------------------------------------


def test_upload_started_nao_equivale_a_remote_confirmed(audit):
    job = _create_job(audit, operation="PUBLISH")

    audit.record_checkpoint(job.id, CHECKPOINT_UPLOAD_STARTED)

    # Um upload iniciado (e possivelmente interrompido por crash) NUNCA pode
    # ser lido como confirmação remota. Nada nesta API infere isso
    # automaticamente: a ausência de REMOTE_CONFIRMED é só ausência de
    # evidência, não prova de falha nem de sucesso — decidir o que fazer com
    # essa incerteza é responsabilidade do RecoveryManager (PROMPT 14).
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_UPLOAD_STARTED) is True
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_REMOTE_CONFIRMED) is False
    assert audit.latest_checkpoint(job.id) == CHECKPOINT_UPLOAD_STARTED

    audit.record_checkpoint(job.id, CHECKPOINT_REMOTE_CONFIRMED)

    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_REMOTE_CONFIRMED) is True
    checkpoints = [event["data"]["checkpoint"] for event in audit.list_checkpoints(job.id)]
    assert checkpoints == [CHECKPOINT_UPLOAD_STARTED, CHECKPOINT_REMOTE_CONFIRMED]


# ---------------------------------------------------------------------------
# Auditabilidade: checkpoints interleaved não quebram reconstrução de estado
# ---------------------------------------------------------------------------


def test_checkpoints_interleaved_nao_quebram_reconstrucao_do_historico_de_estado(audit):
    job = _create_job(audit)

    audit.record_checkpoint(job.id, CHECKPOINT_IMPORTED)
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.record_checkpoint(job.id, CHECKPOINT_TRANSCRIBED)
    audit.record_checkpoint(job.id, CHECKPOINT_ANALYZED)

    # reconstruct_job_history só olha JOB_CREATED/JOB_STATE_CHANGED; eventos
    # de checkpoint interleaved não podem quebrá-la.
    history = audit.reconstruct_job_history(job.id)
    assert history.state_path == (JOB_PENDING, JOB_PROCESSING)

    all_events = audit.list_job_events(job.id)
    event_types = [event["event_type"] for event in all_events]
    assert event_types.count(AUDIT_JOB_STATE_CHANGED) == 1
    assert event_types.count(AUDIT_JOB_CHECKPOINT_REACHED) == 3

    # E os checkpoints continuam consultáveis isoladamente, na ordem certa.
    checkpoints = [event["data"]["checkpoint"] for event in audit.list_checkpoints(job.id)]
    assert checkpoints == [CHECKPOINT_IMPORTED, CHECKPOINT_TRANSCRIBED, CHECKPOINT_ANALYZED]


# ---------------------------------------------------------------------------
# Integração com o RecoveryManager (PROMPT 14): checkpoint sozinho nunca
# decide o destino do Job — a incerteza remota continua sendo resolvida
# exclusivamente pela State Machine + RecoveryManager, nunca inferida a
# partir da presença/ausência de um checkpoint.
# ---------------------------------------------------------------------------


def test_upload_started_sem_remote_confirmed_nao_e_lido_como_falha_pelo_recovery_manager(local_db, audit):
    """UPLOAD_STARTED sem REMOTE_CONFIRMED não prova falha: um Job cujo
    processo caiu em PUBLISHING, com apenas o checkpoint UPLOAD_STARTED
    registrado, precisa ser recuperado como incerteza preservada
    (RECOVERING, via UNKNOWN), nunca como FAILED e nunca republicado."""
    job = _create_job(audit, operation="PUBLISH")
    audit.transition_job(job.id, JOB_PUBLISHING)
    audit.record_checkpoint(job.id, CHECKPOINT_UPLOAD_STARTED)
    # Processo "cai" aqui: nem REMOTE_CONFIRMED nem a transição final de Job
    # chegaram a ser persistidos.

    manager = RecoveryManager(local_db, audit_log=audit)
    report = manager.recover_at_startup()

    assert len(report.results) == 1
    result = report.results[0]
    assert result.ok
    assert result.action == ACTION_FLAGGED_FOR_RECONCILIATION
    assert result.job.status == JOB_RECOVERING

    # A incerteza sobre o upload continua exatamente como estava: nenhum
    # REMOTE_CONFIRMED foi inventado, e o checkpoint UPLOAD_STARTED permanece
    # como a única evidência registrada.
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_UPLOAD_STARTED) is True
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_REMOTE_CONFIRMED) is False


def test_remote_confirmed_persistido_nao_e_usado_para_auto_publicar_pelo_recovery_manager(local_db, audit):
    """Mesmo quando REMOTE_CONFIRMED já foi persistido antes do crash, o
    RecoveryManager desta etapa não lê checkpoints para decidir o status
    final do Job automaticamente — essa reconciliação (usar REMOTE_CONFIRMED
    como evidência para fechar o Job como PUBLISHED) é responsabilidade do
    Connector Contract de uma etapa futura, não deste módulo. O Job
    continua sendo encaminhado, de forma conservadora e auditável, para
    RECOVERING."""
    job = _create_job(audit, operation="PUBLISH")
    audit.transition_job(job.id, JOB_PUBLISHING)
    audit.record_checkpoint(job.id, CHECKPOINT_UPLOAD_STARTED)
    audit.record_checkpoint(job.id, CHECKPOINT_REMOTE_CONFIRMED)
    # Processo cai antes de a transição de Job para PUBLISHED ser persistida.

    manager = RecoveryManager(local_db, audit_log=audit)
    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.ok
    # Não inventa PUBLISHED/SCHEDULED automaticamente a partir do checkpoint:
    # preserva a situação conservadora e auditável.
    assert result.job.status == JOB_RECOVERING
    assert audit.has_reached_checkpoint(job.id, CHECKPOINT_REMOTE_CONFIRMED) is True
