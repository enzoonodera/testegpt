import ast
import threading
from pathlib import Path
import tempfile
import uuid

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_INTERRUPTED,
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
    AUDIT_JOB_RECOVERY,
    AUDIT_JOB_RECONCILIATION,
    AUDIT_JOB_STATE_CHANGED,
    LocalDatabase,
    OperationalAuditLog,
)
from _sistema.job_engine import CLAIMABLE_STATES, JobEngine, JobStepResult
from _sistema.recovery_manager import (
    ACTION_ALREADY_RECOVERING,
    ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED,
    ACTION_FLAGGED_FOR_RECONCILIATION,
    ACTION_MARKED_FAILED_FOR_MANUAL_RETRY,
    ACTION_RESUMED_READY,
    RECOVERABLE_STATES,
    JobRecoveryError,
    RecoveryManager,
    RecoveryManagerError,
    RecoveryReport,
)


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


@pytest.fixture
def manager(local_db, audit):
    return RecoveryManager(local_db, audit_log=audit)


def _create_job(audit, *, operation="RENDER"):
    job = Job(operation=operation)
    return audit.create_job(job)


# Rótulos de "estado simulado" além dos nomes de status puros, para expressar
# origens diferentes de RECOVERING (BLOQUEADOR 2) sem reaproveitar o mesmo
# status como chave.
_RECOVERING_FROM_INTERRUPTED = "RECOVERING_FROM_INTERRUPTED"
_RECOVERING_FROM_UNKNOWN = "RECOVERING_FROM_UNKNOWN"
_RECOVERING_FROM_RETRY = "RECOVERING_FROM_RETRY"

_PATHS = {
    JOB_PROCESSING: [JOB_PROCESSING],
    JOB_INTERRUPTED: [JOB_PROCESSING, JOB_INTERRUPTED],
    JOB_PUBLISHING: [JOB_PUBLISHING],
    JOB_UNKNOWN: [JOB_PUBLISHING, JOB_UNKNOWN],
    # RECOVERING "puro" (usado só onde a origem não importa para o teste):
    # chega via o caminho local, que é o caso mais comum.
    JOB_RECOVERING: [JOB_PROCESSING, JOB_INTERRUPTED, JOB_RECOVERING],
    _RECOVERING_FROM_INTERRUPTED: [JOB_PROCESSING, JOB_INTERRUPTED, JOB_RECOVERING],
    _RECOVERING_FROM_UNKNOWN: [JOB_PUBLISHING, JOB_UNKNOWN, JOB_RECOVERING],
    # RETRY -> RECOVERING é uma aresta válida da State Machine central (ex.:
    # um handler registrado com claims_status=RECOVERING reivindicando um Job
    # que estava em RETRY). A origem semântica desse trabalho (local ou
    # remota) não é conhecida pelo RecoveryManager a partir só disso.
    _RECOVERING_FROM_RETRY: [JOB_PROCESSING, JOB_RETRY, JOB_RECOVERING],
}


def _force_status(audit, job, target):
    """Move um Job recém-criado (PENDING) até ``target`` usando somente
    transições válidas da State Machine central, para simular exatamente o
    estado (e a cadeia de transições persistida no Audit Log) em que um
    crash real teria deixado o Job."""
    current = job
    for step in _PATHS[target]:
        current = audit.transition_job(current.id, step)
    return current


# ---------------------------------------------------------------------------
# Descoberta de Jobs abandonados
# ---------------------------------------------------------------------------


def test_find_abandoned_jobs_encontra_estados_recuperaveis(manager, audit):
    pending = _create_job(audit, operation="A")
    processing = _force_status(audit, _create_job(audit, operation="B"), JOB_PROCESSING)
    publishing = _force_status(audit, _create_job(audit, operation="C"), JOB_PUBLISHING)
    interrupted = _force_status(audit, _create_job(audit, operation="D"), JOB_INTERRUPTED)
    recovering = _force_status(audit, _create_job(audit, operation="E"), JOB_RECOVERING)
    unknown = _force_status(audit, _create_job(audit, operation="F"), JOB_UNKNOWN)

    abandoned = {job.id for job in manager.find_abandoned_jobs()}

    assert abandoned == {
        processing.id,
        publishing.id,
        interrupted.id,
        recovering.id,
        unknown.id,
    }
    assert pending.id not in abandoned


def test_find_abandoned_jobs_ignora_estados_terminais_e_acionaveis(manager, audit):
    _create_job(audit, operation="A")  # PENDING
    failed = _force_status(audit, _create_job(audit, operation="B"), JOB_PROCESSING)
    audit.transition_job(failed.id, JOB_FAILED)
    cancelled = _create_job(audit, operation="C")
    audit.transition_job(cancelled.id, JOB_CANCELLED)

    assert manager.find_abandoned_jobs() == []


def test_recoverable_states_inclui_unknown_alem_de_claimable_e_interrupted():
    # BLOQUEADOR 1 da revisão: UNKNOWN precisa estar aqui, senão um crash
    # entre PUBLISHING->UNKNOWN e UNKNOWN->RECOVERING deixa o Job esquecido.
    assert RECOVERABLE_STATES == CLAIMABLE_STATES | {JOB_INTERRUPTED, JOB_UNKNOWN}
    assert JOB_UNKNOWN in RECOVERABLE_STATES


# ---------------------------------------------------------------------------
# PROCESSING abandonado (trabalho local) — fronteira de crash (1)
# ---------------------------------------------------------------------------


def test_processing_abandonado_sem_validator_vira_failed_nao_retry(manager, audit):
    """Reavaliação da revisão: sem evidência positiva de conclusão, o
    destino padrão agora é FAILED (decisão explícita exigida antes de
    qualquer nova tentativa), não mais um RETRY automático — repetir um
    handler arbitrário não é necessariamente idempotente só porque o
    processamento é não destrutivo."""
    job = _force_status(audit, _create_job(audit), JOB_PROCESSING)

    report = manager.recover_at_startup()

    assert len(report.results) == 1
    result = report.results[0]
    assert result.ok
    assert result.previous_status == JOB_PROCESSING
    assert result.action == ACTION_MARKED_FAILED_FOR_MANUAL_RETRY
    assert result.job.status == JOB_FAILED

    persisted = audit.database.get(Job, job.id)
    assert persisted.status == JOB_FAILED


def test_processing_abandonado_passa_por_interrupted_e_recovering_no_audit_trail(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_PROCESSING)

    manager.recover_at_startup()

    history = audit.reconstruct_job_history(job.id)
    assert history.state_path == (
        JOB_PENDING,
        JOB_PROCESSING,
        JOB_INTERRUPTED,
        JOB_RECOVERING,
        JOB_FAILED,
    )


def test_processing_abandonado_com_validator_confirmando_vira_ready(local_db, audit):
    job = _force_status(audit, _create_job(audit), JOB_PROCESSING)
    manager = RecoveryManager(local_db, audit_log=audit, artifact_validator=lambda j: True)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.action == ACTION_RESUMED_READY
    assert result.job.status == JOB_READY


def test_processing_abandonado_com_validator_negando_vira_failed(local_db, audit):
    job = _force_status(audit, _create_job(audit), JOB_PROCESSING)
    manager = RecoveryManager(local_db, audit_log=audit, artifact_validator=lambda j: False)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.action == ACTION_MARKED_FAILED_FOR_MANUAL_RETRY
    assert result.job.status == JOB_FAILED


def test_validator_que_lanca_excecao_e_tratado_como_nao_confirmado(local_db, audit):
    job = _force_status(audit, _create_job(audit), JOB_PROCESSING)

    def broken_validator(_job):
        raise RuntimeError("disco inacessível")

    manager = RecoveryManager(local_db, audit_log=audit, artifact_validator=broken_validator)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.ok
    assert result.action == ACTION_MARKED_FAILED_FOR_MANUAL_RETRY
    assert result.job.status == JOB_FAILED


def test_validator_recebe_o_job_em_recovering(local_db, audit):
    job = _force_status(audit, _create_job(audit), JOB_PROCESSING)
    seen_status = []

    def validator(current_job):
        seen_status.append(current_job.status)
        return False

    manager = RecoveryManager(local_db, audit_log=audit, artifact_validator=validator)
    manager.recover_at_startup()

    assert seen_status == [JOB_RECOVERING]


def test_failed_apos_recuperacao_nao_e_reivindicavel_automaticamente(local_db, audit):
    """FAILED exige decisão explícita (OperationalAuditLog.retry) antes de
    qualquer nova execução — não é uma repetição automática."""
    job = _force_status(audit, _create_job(audit, operation="RENDER"), JOB_PROCESSING)
    manager = RecoveryManager(local_db, audit_log=audit)
    manager.recover_at_startup()

    engine = JobEngine(local_db, audit_log=audit)
    calls = []
    engine.register_handler(
        "RENDER",
        lambda j: calls.append(j.status) or JobStepResult(target_status=JOB_READY),
        claims_status=JOB_PROCESSING,
    )

    outcomes = engine.run_pending()

    assert outcomes == []
    assert calls == []
    assert local_db.get(Job, job.id).status == JOB_FAILED


# ---------------------------------------------------------------------------
# INTERRUPTED encontrado diretamente — fronteira de crash (2)
# ---------------------------------------------------------------------------


def test_interrupted_encontrado_diretamente_retoma_recuperacao(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_INTERRUPTED)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.ok
    assert result.previous_status == JOB_INTERRUPTED
    assert result.action == ACTION_MARKED_FAILED_FOR_MANUAL_RETRY
    assert result.job.status == JOB_FAILED


def test_interrupted_encontrado_diretamente_nao_duplica_transicao_para_interrupted(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_INTERRUPTED)

    manager.recover_at_startup()

    history = audit.reconstruct_job_history(job.id)
    # Só passou por INTERRUPTED uma vez (o setup do teste), nunca duas.
    assert history.state_path.count(JOB_INTERRUPTED) == 1
    assert history.state_path == (
        JOB_PENDING,
        JOB_PROCESSING,
        JOB_INTERRUPTED,
        JOB_RECOVERING,
        JOB_FAILED,
    )


# ---------------------------------------------------------------------------
# RECOVERING de origem LOCAL encontrado diretamente — fronteira de crash (3)
# ---------------------------------------------------------------------------


def test_recovering_de_origem_local_retoma_a_decisao_pendente(manager, audit):
    """BLOQUEADOR 2: um Job que chegou a RECOVERING via
    PROCESSING -> INTERRUPTED -> RECOVERING e travou antes da decisão final
    precisa continuar a recuperação LOCAL (não ser tratado como reconciliação
    remota)."""
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_INTERRUPTED)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.ok
    assert result.previous_status == JOB_RECOVERING
    assert result.action == ACTION_MARKED_FAILED_FOR_MANUAL_RETRY
    assert result.job.status == JOB_FAILED
    assert result.action not in (ACTION_ALREADY_RECOVERING, ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED)


def test_recovering_de_origem_local_nao_repete_transicoes_ja_persistidas(manager, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_INTERRUPTED)

    manager.recover_at_startup()

    history = audit.reconstruct_job_history(job.id)
    assert history.state_path == (
        JOB_PENDING,
        JOB_PROCESSING,
        JOB_INTERRUPTED,
        JOB_RECOVERING,
        JOB_FAILED,
    )
    assert history.state_path.count(JOB_INTERRUPTED) == 1
    assert history.state_path.count(JOB_RECOVERING) == 1


def test_recovering_de_origem_local_com_validator_confirmando_vira_ready(local_db, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_INTERRUPTED)
    manager = RecoveryManager(local_db, audit_log=audit, artifact_validator=lambda j: True)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.action == ACTION_RESUMED_READY
    assert result.job.status == JOB_READY


def test_recovering_de_origem_local_nao_aparece_como_pending_reconciliation(manager, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_INTERRUPTED)

    report = manager.recover_at_startup()

    assert job.id not in report.jobs_pending_reconciliation


# ---------------------------------------------------------------------------
# PUBLISHING abandonado — fronteira de crash (4)
# ---------------------------------------------------------------------------


def test_publishing_abandonado_nunca_vira_failed_ou_published(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_PUBLISHING)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.ok
    assert result.previous_status == JOB_PUBLISHING
    assert result.job.status not in (JOB_FAILED, JOB_PUBLISHED, JOB_RETRY)
    assert result.job.status == JOB_RECOVERING
    assert result.action == ACTION_FLAGGED_FOR_RECONCILIATION


def test_publishing_abandonado_passa_por_unknown_no_audit_trail(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_PUBLISHING)

    manager.recover_at_startup()

    history = audit.reconstruct_job_history(job.id)
    assert history.state_path == (JOB_PENDING, JOB_PUBLISHING, JOB_UNKNOWN, JOB_RECOVERING)


def test_publishing_abandonado_registra_evento_de_reconciliacao(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_PUBLISHING)

    manager.recover_at_startup()

    events = audit.list_job_events(job.id)
    event_types = [event["event_type"] for event in events]
    assert AUDIT_JOB_RECONCILIATION in event_types
    assert AUDIT_JOB_RECOVERY in event_types


def test_publishing_abandonado_e_artifact_validator_nunca_e_consultado(local_db, audit):
    job = _force_status(audit, _create_job(audit), JOB_PUBLISHING)
    calls = []

    def validator(current_job):
        calls.append(current_job.id)
        return True

    manager = RecoveryManager(local_db, audit_log=audit, artifact_validator=validator)
    manager.recover_at_startup()

    assert calls == []


def test_jobs_pending_reconciliation_inclui_publishing_recuperado(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_PUBLISHING)

    report = manager.recover_at_startup()

    assert job.id in report.jobs_pending_reconciliation


# ---------------------------------------------------------------------------
# UNKNOWN encontrado diretamente — fronteira de crash (5) — BLOQUEADOR 1
# ---------------------------------------------------------------------------


def test_unknown_encontrado_diretamente_nao_fica_esquecido(manager, audit):
    """Reproduz exatamente o crash window do BLOQUEADOR 1:
    PENDING -> PUBLISHING -> RecoveryManager -> PUBLISHING -> UNKNOWN ->
    processo morre ANTES de UNKNOWN -> RECOVERING. Antes da correção,
    find_abandoned_jobs() não encontrava esse Job (UNKNOWN não pertencia a
    RECOVERABLE_STATES) e ele ficava UNKNOWN para sempre."""
    job = _force_status(audit, _create_job(audit), JOB_UNKNOWN)
    assert job.status == JOB_UNKNOWN  # pré-condição: exatamente o estado do crash

    report = manager.recover_at_startup()

    assert len(report.results) == 1
    result = report.results[0]
    assert result.ok
    assert result.previous_status == JOB_UNKNOWN
    assert result.job.status == JOB_RECOVERING
    assert result.action == ACTION_FLAGGED_FOR_RECONCILIATION

    persisted = audit.database.get(Job, job.id)
    assert persisted.status == JOB_RECOVERING


def test_unknown_encontrado_diretamente_nao_pula_para_retry_ou_publishing(manager, audit):
    """UNKNOWN continua só podendo ir para RECOVERING — a correção do
    BLOQUEADOR 1 não pode reabrir a porta para repost/retry automático."""
    job = _force_status(audit, _create_job(audit), JOB_UNKNOWN)

    report = manager.recover_at_startup()

    assert report.results[0].job.status not in (JOB_RETRY, JOB_PUBLISHING, JOB_PUBLISHED, JOB_FAILED)


def test_unknown_encontrado_diretamente_registra_evento_de_diagnostico_proprio(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_UNKNOWN)

    manager.recover_at_startup()

    events = audit.list_job_events(job.id)
    recovery_events = [event for event in events if event["event_type"] == AUDIT_JOB_RECOVERY]
    assert any(
        event["data"].get("phase") == "UNKNOWN_FOUND_AT_STARTUP_BEFORE_RECOVERING"
        for event in recovery_events
    )


def test_unknown_encontrado_diretamente_esta_em_pending_reconciliation(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_UNKNOWN)

    report = manager.recover_at_startup()

    assert job.id in report.jobs_pending_reconciliation


def test_unknown_encontrado_diretamente_artifact_validator_nunca_e_consultado(local_db, audit):
    job = _force_status(audit, _create_job(audit), JOB_UNKNOWN)
    calls = []

    manager = RecoveryManager(
        local_db, audit_log=audit, artifact_validator=lambda j: calls.append(j.id) or True
    )
    manager.recover_at_startup()

    assert calls == []


# ---------------------------------------------------------------------------
# RECOVERING de origem REMOTA encontrado diretamente — fronteira de crash (6)
# ---------------------------------------------------------------------------


def test_recovering_de_origem_remota_nao_forca_transicao(manager, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_UNKNOWN)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.ok
    assert result.previous_status == JOB_RECOVERING
    assert result.action == ACTION_ALREADY_RECOVERING
    assert result.job.status == JOB_RECOVERING

    persisted = audit.database.get(Job, job.id)
    assert persisted.status == JOB_RECOVERING
    history = audit.reconstruct_job_history(job.id)
    assert history.current_state == JOB_RECOVERING


def test_recovering_de_origem_remota_registra_evento_de_diagnostico(manager, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_UNKNOWN)

    manager.recover_at_startup()

    events = audit.list_job_events(job.id)
    recovery_events = [event for event in events if event["event_type"] == AUDIT_JOB_RECOVERY]
    assert any(
        event["data"].get("phase") == "ALREADY_RECOVERING_AT_STARTUP" for event in recovery_events
    )


def test_recovering_de_origem_remota_esta_em_pending_reconciliation(manager, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_UNKNOWN)

    report = manager.recover_at_startup()

    assert job.id in report.jobs_pending_reconciliation


def test_recovering_de_origem_remota_artifact_validator_nao_e_consultado(local_db, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_UNKNOWN)
    calls = []

    manager = RecoveryManager(
        local_db, audit_log=audit, artifact_validator=lambda j: calls.append(j.id) or True
    )
    manager.recover_at_startup()

    assert calls == []


# ---------------------------------------------------------------------------
# RECOVERING de origem AMBÍGUA: caso deliberado de "não decidir"
# ---------------------------------------------------------------------------


def test_recovering_de_origem_ambigua_nao_e_tratado_como_local_nem_remoto(manager, audit):
    """RETRY -> RECOVERING é uma transição válida da State Machine central,
    mas sua origem semântica (local ou remota) não pode ser inferida só por
    isso. O RecoveryManager deve deliberadamente não resolver essa
    incerteza sozinho, em vez de arriscar uma decisão sem evidência."""
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_RETRY)

    report = manager.recover_at_startup()

    result = report.results[0]
    assert result.ok
    assert result.action == ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED
    # Nenhuma transição forçada: continua exatamente em RECOVERING.
    assert result.job.status == JOB_RECOVERING


def test_recovering_de_origem_ambigua_registra_evento_proprio(manager, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_RETRY)

    manager.recover_at_startup()

    events = audit.list_job_events(job.id)
    recovery_events = [event for event in events if event["event_type"] == AUDIT_JOB_RECOVERY]
    assert any(
        event["data"].get("phase") == "RECOVERING_ORIGIN_AMBIGUOUS_NOT_AUTO_RESOLVED"
        for event in recovery_events
    )


def test_recovering_de_origem_ambigua_esta_em_pending_reconciliation(manager, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_RETRY)

    report = manager.recover_at_startup()

    assert job.id in report.jobs_pending_reconciliation


def test_recovering_de_origem_ambigua_artifact_validator_nao_e_consultado(local_db, audit):
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_RETRY)
    calls = []

    manager = RecoveryManager(
        local_db, audit_log=audit, artifact_validator=lambda j: calls.append(j.id) or True
    )
    manager.recover_at_startup()

    assert calls == []


def test_classify_recovering_origin_sem_nenhum_evento_e_ambiguo(manager, audit):
    # Nenhum evento JOB_STATE_CHANGED com to_state=RECOVERING existe para um
    # job_id qualquer: a classificação não pode presumir nada.
    job = _create_job(audit)
    assert manager._classify_recovering_origin(job.id) == "AMBIGUOUS"


# ---------------------------------------------------------------------------
# Isolamento de falhas / múltiplos Jobs / todas as fronteiras de uma vez
# ---------------------------------------------------------------------------


def test_recover_at_startup_isola_todos_os_estados_de_uma_vez(manager, audit):
    processing = _force_status(audit, _create_job(audit, operation="A"), JOB_PROCESSING)
    publishing = _force_status(audit, _create_job(audit, operation="B"), JOB_PUBLISHING)
    interrupted = _force_status(audit, _create_job(audit, operation="C"), JOB_INTERRUPTED)
    recovering_local = _force_status(
        audit, _create_job(audit, operation="D"), _RECOVERING_FROM_INTERRUPTED
    )
    recovering_remote = _force_status(
        audit, _create_job(audit, operation="E"), _RECOVERING_FROM_UNKNOWN
    )
    unknown = _force_status(audit, _create_job(audit, operation="F"), JOB_UNKNOWN)
    pending = _create_job(audit, operation="G")

    report = manager.recover_at_startup()

    assert len(report.results) == 6
    assert {result.job_id for result in report.results} == {
        processing.id,
        publishing.id,
        interrupted.id,
        recovering_local.id,
        recovering_remote.id,
        unknown.id,
    }
    assert all(result.ok for result in report.results)

    by_id = {result.job_id: result for result in report.results}
    assert by_id[processing.id].job.status == JOB_FAILED
    assert by_id[interrupted.id].job.status == JOB_FAILED
    assert by_id[recovering_local.id].job.status == JOB_FAILED
    assert by_id[publishing.id].job.status == JOB_RECOVERING
    assert by_id[unknown.id].job.status == JOB_RECOVERING
    assert by_id[recovering_remote.id].job.status == JOB_RECOVERING

    # O Job PENDING não sofreu nenhuma ação e permanece intocado.
    assert audit.database.get(Job, pending.id).status == JOB_PENDING


def test_um_job_inexistente_apos_leitura_nao_derruba_os_demais(manager, local_db, audit):
    processing = _force_status(audit, _create_job(audit, operation="A"), JOB_PROCESSING)
    doomed = _force_status(audit, _create_job(audit, operation="B"), JOB_PUBLISHING)

    real_transition = audit.transition_job
    calls = {"count": 0}

    def flaky_transition(job_id, target, **kwargs):
        # Simula uma corrida real: o Job "doomed" é removido do storage
        # entre a leitura de find_abandoned_jobs e sua recuperação (ex.: uma
        # limpeza administrativa concorrente). O primeiro transition_job
        # chamado para esse job_id falha; os demais Jobs devem continuar
        # sendo recuperados normalmente.
        if job_id == doomed.id:
            calls["count"] += 1
            if calls["count"] == 1:
                local_db.delete(Job, doomed.id)
        return real_transition(job_id, target, **kwargs)

    audit.transition_job = flaky_transition  # type: ignore[assignment]

    report = manager.recover_at_startup()

    by_id = {result.job_id: result for result in report.results}
    assert by_id[processing.id].ok
    assert by_id[processing.id].job.status == JOB_FAILED
    assert not by_id[doomed.id].ok
    assert isinstance(by_id[doomed.id].error, JobRecoveryError)


def test_recovery_report_recovered_e_failed_job_ids(manager, local_db, audit):
    processing = _force_status(audit, _create_job(audit, operation="A"), JOB_PROCESSING)
    doomed = _force_status(audit, _create_job(audit, operation="B"), JOB_PUBLISHING)

    real_transition = audit.transition_job

    def flaky_transition(job_id, target, **kwargs):
        if job_id == doomed.id:
            local_db.delete(Job, doomed.id)
        return real_transition(job_id, target, **kwargs)

    audit.transition_job = flaky_transition  # type: ignore[assignment]

    report = manager.recover_at_startup()

    assert processing.id in report.recovered_job_ids
    assert doomed.id in report.failed_job_ids
    assert doomed.id not in report.recovered_job_ids


# ---------------------------------------------------------------------------
# Crash/restart: sobrevivência real via fechar/reabrir o banco
# ---------------------------------------------------------------------------


def test_recuperacao_sobrevive_a_fechar_e_reabrir_o_banco(local_db, audit):
    job = _force_status(audit, _create_job(audit), JOB_PROCESSING)
    paths = local_db.paths

    # Simula o processo caindo e um novo processo (nova instância de
    # LocalDatabase apontando para o mesmo arquivo) assumindo a recuperação.
    reopened_db = LocalDatabase(paths=paths)
    reopened_db.initialize()
    reopened_audit = OperationalAuditLog(reopened_db)
    manager = RecoveryManager(reopened_db, audit_log=reopened_audit)

    report = manager.recover_at_startup()

    assert report.results[0].job_id == job.id
    assert report.results[0].job.status == JOB_FAILED

    final_db = LocalDatabase(paths=paths)
    final_db.initialize()
    assert final_db.get(Job, job.id).status == JOB_FAILED


def test_crash_entre_publishing_e_unknown_sobrevive_a_reabertura_do_banco(local_db, audit):
    job = _force_status(audit, _create_job(audit), JOB_PUBLISHING)
    paths = local_db.paths

    reopened_db = LocalDatabase(paths=paths)
    reopened_db.initialize()
    reopened_audit = OperationalAuditLog(reopened_db)
    manager = RecoveryManager(reopened_db, audit_log=reopened_audit)

    report = manager.recover_at_startup()

    assert report.results[0].job.status == JOB_RECOVERING
    final_db = LocalDatabase(paths=paths)
    final_db.initialize()
    assert final_db.get(Job, job.id).status == JOB_RECOVERING


def test_crash_entre_unknown_e_recovering_sobrevive_a_reabertura_do_banco(local_db, audit):
    """Reproduz o BLOQUEADOR 1 através de um restart real (fechar/reabrir o
    banco), não só chamando o manager duas vezes no mesmo processo."""
    job = _force_status(audit, _create_job(audit), JOB_UNKNOWN)
    paths = local_db.paths

    reopened_db = LocalDatabase(paths=paths)
    reopened_db.initialize()
    reopened_audit = OperationalAuditLog(reopened_db)
    manager = RecoveryManager(reopened_db, audit_log=reopened_audit)

    report = manager.recover_at_startup()

    assert report.results[0].job.status == JOB_RECOVERING
    final_db = LocalDatabase(paths=paths)
    final_db.initialize()
    assert final_db.get(Job, job.id).status == JOB_RECOVERING


def test_dois_crashes_consecutivos_nao_deixam_job_preso(local_db, audit):
    """PROCESSING -> crash (fica em INTERRUPTED por um RecoveryManager
    parcial simulado) -> segundo crash -> um novo RecoveryManager ainda
    consegue terminar a recuperação a partir de INTERRUPTED."""
    job = _force_status(audit, _create_job(audit), JOB_INTERRUPTED)

    manager = RecoveryManager(local_db, audit_log=audit)
    report = manager.recover_at_startup()

    assert report.results[0].job.status == JOB_FAILED
    assert local_db.get(Job, job.id).status == JOB_FAILED


def test_tres_crashes_consecutivos_ate_recovering_local_nao_deixam_job_preso(local_db, audit):
    """PENDING -> PROCESSING -> crash -> INTERRUPTED -> crash -> RECOVERING
    (origem local) -> crash -> um novo RecoveryManager ainda termina a
    decisão pendente."""
    job = _force_status(audit, _create_job(audit), _RECOVERING_FROM_INTERRUPTED)

    manager = RecoveryManager(local_db, audit_log=audit)
    report = manager.recover_at_startup()

    assert report.results[0].job.status == JOB_FAILED
    assert local_db.get(Job, job.id).status == JOB_FAILED


# ---------------------------------------------------------------------------
# Nunca reivindicável pelo JobEngine antes da recuperação decidir
# ---------------------------------------------------------------------------


def test_publishing_abandonado_nao_e_reivindicavel_direto_pelo_job_engine(local_db, audit):
    job = _force_status(audit, _create_job(audit, operation="UPLOAD"), JOB_PUBLISHING)
    manager = RecoveryManager(local_db, audit_log=audit)
    manager.recover_at_startup()

    engine = JobEngine(local_db, audit_log=audit)
    engine.register_handler(
        "UPLOAD", lambda j: JobStepResult(target_status=JOB_PUBLISHED), claims_status=JOB_PUBLISHING
    )

    # O Job está em RECOVERING (aguardando reconciliação), não em
    # PENDING/RETRY: run_pending não o encontra, então nenhum handler roda
    # sobre uma incerteza remota ainda não resolvida.
    outcomes = engine.run_pending()

    assert outcomes == []
    assert local_db.get(Job, job.id).status == JOB_RECOVERING


def test_unknown_abandonado_nao_e_reivindicavel_direto_pelo_job_engine(local_db, audit):
    job = _force_status(audit, _create_job(audit, operation="UPLOAD"), JOB_UNKNOWN)
    manager = RecoveryManager(local_db, audit_log=audit)
    manager.recover_at_startup()

    engine = JobEngine(local_db, audit_log=audit)
    engine.register_handler(
        "UPLOAD", lambda j: JobStepResult(target_status=JOB_PUBLISHED), claims_status=JOB_PUBLISHING
    )

    outcomes = engine.run_pending()

    assert outcomes == []
    assert local_db.get(Job, job.id).status == JOB_RECOVERING


# ---------------------------------------------------------------------------
# Concorrência básica
# ---------------------------------------------------------------------------


def test_duas_instancias_concorrentes_nao_duplicam_recuperacao_do_mesmo_job(local_db):
    setup_audit = OperationalAuditLog(local_db)
    job = _force_status(setup_audit, _create_job(setup_audit), JOB_PROCESSING)

    # db_a já foi inicializado pela fixture local_db (arquivo/tabelas já
    # existem). db_b simula um segundo processo/instância contra o mesmo
    # arquivo SQLite, sem precisar reinicializar o schema (mesmo padrão de
    # tests/test_job_engine.py).
    db_b = LocalDatabase(paths=local_db.paths)
    audit_a = OperationalAuditLog(local_db)
    audit_b = OperationalAuditLog(db_b)
    manager_a = RecoveryManager(local_db, audit_log=audit_a)
    manager_b = RecoveryManager(db_b, audit_log=audit_b)

    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def worker(name, worker_manager):
        try:
            barrier.wait(timeout=5)
            report = worker_manager.recover_at_startup()
            results[name] = report
        except Exception as exc:  # pragma: no cover - só para diagnóstico em falha
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("a", manager_a)),
        threading.Thread(target=worker, args=("b", manager_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f"threads falharam: {errors}"

    # A soma de resultados "ok" com ação efetiva (não apenas leitura vazia)
    # entre as duas threads não pode indicar dupla recuperação do mesmo Job:
    # a segunda a tentar a transição PROCESSING->INTERRUPTED encontra o Job
    # já fora de PROCESSING e sua tentativa falha isoladamente, sem afetar a
    # outra thread.
    total_successful_actions = sum(
        1
        for report in results.values()
        for result in report.results
        if result.ok and result.job_id == job.id
    )
    assert total_successful_actions == 1

    final = LocalDatabase(paths=local_db.paths)
    final.initialize()
    assert final.get(Job, job.id).status == JOB_FAILED


def test_duas_instancias_concorrentes_nao_produzem_decisoes_incompativeis_apos_crash_em_unknown(local_db):
    """Mesmo teste de concorrência, mas exatamente na fronteira do
    BLOQUEADOR 1 (crash entre PUBLISHING->UNKNOWN e UNKNOWN->RECOVERING):
    duas instâncias correndo recover_at_startup() ao mesmo tempo contra o
    mesmo Job em UNKNOWN não podem produzir estados finais diferentes nem
    aplicar a transição UNKNOWN->RECOVERING duas vezes de forma
    inconsistente."""
    setup_audit = OperationalAuditLog(local_db)
    job = _force_status(setup_audit, _create_job(setup_audit), JOB_UNKNOWN)

    db_b = LocalDatabase(paths=local_db.paths)
    audit_a = OperationalAuditLog(local_db)
    audit_b = OperationalAuditLog(db_b)
    manager_a = RecoveryManager(local_db, audit_log=audit_a)
    manager_b = RecoveryManager(db_b, audit_log=audit_b)

    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def worker(name, worker_manager):
        try:
            barrier.wait(timeout=5)
            results[name] = worker_manager.recover_at_startup()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("a", manager_a)),
        threading.Thread(target=worker, args=("b", manager_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f"threads falharam: {errors}"

    final = LocalDatabase(paths=local_db.paths)
    final.initialize()
    assert final.get(Job, job.id).status == JOB_RECOVERING

    # A cadeia de transições persistida precisa continuar válida e única
    # (sem UNKNOWN->RECOVERING duplicado incoerente): reconstruct_job_history
    # já valida isso lançando AuditHistoryError se a cadeia for inconsistente.
    history = OperationalAuditLog(local_db).reconstruct_job_history(job.id)
    assert history.current_state == JOB_RECOVERING
    assert history.state_path.count(JOB_RECOVERING) == 1


# ---------------------------------------------------------------------------
# Erros de contrato
# ---------------------------------------------------------------------------


def test_job_recovery_error_e_recovery_manager_error():
    assert issubclass(JobRecoveryError, RecoveryManagerError)


def test_recovery_report_e_imutavel(manager, audit):
    job = _force_status(audit, _create_job(audit), JOB_PROCESSING)
    report = manager.recover_at_startup()

    with pytest.raises(Exception):
        report.results = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Independência de UI/plataformas
# ---------------------------------------------------------------------------


def test_recovery_manager_nao_importa_ui_nem_connectors_de_plataforma():
    source = Path("_sistema/recovery_manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_markers = ("playwright", "selenium", "tkinter", "PyQt", "youtube", "tiktok", "connector")

    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    lowered = [module.lower() for module in imported_modules]
    for marker in forbidden_markers:
        assert not any(marker in module for module in lowered), (
            f"recovery_manager.py não deveria importar algo relacionado a {marker!r}"
        )
