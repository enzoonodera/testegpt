# -*- coding: utf-8 -*-
"""Testes do BatchEngine central (FASE 3 / PROMPT 17).

Cobre os 35 cenários obrigatórios do roadmap: criação em 1/10/200/500+
itens, membership persistente e determinística, isolamento entre batches,
admissão limitada (nunca todos os Jobs de uma vez), progresso honesto
(seis categorias + breakdown completo), pause/resume sem corrida,
cancelamento individual/em massa crash-safe, reprocessamento restrito a
FAILED, comportamento após crash/restart, concorrência real entre duas
instâncias, integração com ControlManager/ShutdownCoordinator, e ausência
de dependências de UI/plataforma.
"""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    Job,
    JOB_AUTH_REQUIRED,
    JOB_BLOCKED,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RETRY,
    JOB_UNKNOWN,
)
from _sistema.storage import LocalDatabase, OperationalAuditLog
from _sistema.control_manager import (
    CONTROL_SCOPE_GLOBAL,
    CONTROL_SCOPE_JOB,
    CONTROL_SCOPE_QUEUE,
    ControlManager,
)
from _sistema.job_engine import (
    JobBlockedByBatchError,
    JobEngine,
    JobStepResult,
)
from tests.windows_tempdir import robust_temporary_directory

from _sistema.batch_engine import (
    AUDIT_BATCH_CANCEL_COMPLETED,
    AUDIT_BATCH_CANCEL_REQUESTED,
    AUDIT_BATCH_CANCEL_RESUMED,
    AUDIT_BATCH_CREATED,
    AUDIT_BATCH_PAUSED,
    AUDIT_BATCH_RESUMED,
    AUDIT_BATCH_RETRY_REQUESTED,
    BATCH_CANCEL_FULLY_APPLIED,
    BatchEngine,
    BatchEngineDatabaseMismatchError,
    BatchEngineError,
    BatchNotFoundError,
    ControlManagerMismatchError,
    EmptyBatchError,
    JobAlreadyInBatchError,
    JobNotEligibleForBatchError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def local_db():
    # ``robust_temporary_directory`` (não ``tempfile.TemporaryDirectory``
    # puro) -- este arquivo tem um teste que abre uma SEGUNDA instância de
    # ``LocalDatabase`` contra o mesmo arquivo (padrão de concorrência real
    # entre duas instâncias), o que expôs no Windows um ``PermissionError``
    # (``WinError 32``) transitório no teardown -- ver
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


@pytest.fixture
def control(local_db, audit):
    return ControlManager(local_db, audit_log=audit)


@pytest.fixture
def engine(local_db, audit, control):
    return JobEngine(local_db, audit_log=audit, control_manager=control)


@pytest.fixture
def batch(local_db, audit, control, engine):
    return BatchEngine(local_db, audit_log=audit, control_manager=control, job_engine=engine)


def _register_noop_handler(job_engine, *, operation="NOOP", target_status=JOB_READY):
    def handler(job):
        return JobStepResult(target_status=target_status)

    job_engine.register_handler(operation, handler, claims_status=JOB_PROCESSING)


def _register_failing_handler(job_engine, *, operation, fails_for):
    """Handler que falha (lança exceção) para um conjunto específico de
    job_ids, e completa normalmente para os demais — usado para provar que
    a falha de um Job nunca derruba os outros."""

    def handler(job):
        if job.id in fails_for:
            raise RuntimeError(f"falha simulada para {job.id}")
        return JobStepResult(target_status=JOB_READY)

    job_engine.register_handler(operation, handler, claims_status=JOB_PROCESSING)


def _specs(n, *, operation="NOOP"):
    return [{"operation": operation} for _ in range(n)]


# ---------------------------------------------------------------------------
# 1-4: criação em 1 / 10 / 200 / 500+ itens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 10, 200, 501])
def test_criacao_de_batch_em_varias_escalas(batch, n):
    result = batch.create_batch(_specs(n))
    assert len(result.job_ids) == n
    assert len(set(result.job_ids)) == n  # sem duplicatas

    member_ids = batch.list_batch_job_ids(result.batch_id)
    assert member_ids == result.job_ids

    progress = batch.get_progress(result.batch_id)
    assert progress.total == n
    assert progress.pending == n


def test_criacao_mista_varias_funcoes_no_mesmo_batch(batch):
    """'Aplicar várias funções': um batch pode conter Jobs de operations
    diferentes, misturando specs (dict) e instâncias de Job já construídas."""
    items = [
        {"operation": "RENDER"},
        Job(operation="TRANSCRIBE"),
        {"operation": "UPLOAD"},
    ]
    result = batch.create_batch(items)
    assert len(result.job_ids) == 3
    jobs = [batch.job_engine.get_job(job_id) for job_id in result.job_ids]
    assert {job.operation for job in jobs} == {"RENDER", "TRANSCRIBE", "UPLOAD"}


# ---------------------------------------------------------------------------
# 5-6: membership persistente e determinística
# ---------------------------------------------------------------------------


def test_membership_persiste_apos_restart(local_db, audit, control, engine, batch):
    result = batch.create_batch(_specs(15))

    # "Restart": nova instância de LocalDatabase/BatchEngine contra o mesmo
    # arquivo, sem nenhum estado em memória do BatchEngine anterior.
    reopened_db = LocalDatabase(path=local_db.path, paths=local_db.paths)
    reopened_audit = OperationalAuditLog(reopened_db)
    reopened_control = ControlManager(reopened_db, audit_log=reopened_audit)
    reopened_engine = JobEngine(reopened_db, audit_log=reopened_audit, control_manager=reopened_control)
    reopened_batch = BatchEngine(
        reopened_db, audit_log=reopened_audit, control_manager=reopened_control, job_engine=reopened_engine
    )

    assert reopened_batch.list_batch_job_ids(result.batch_id) == result.job_ids
    assert reopened_batch.get_batch(result.batch_id)["job_count"] == 15


def test_ordem_de_membership_e_estavel_e_deterministica(batch):
    result = batch.create_batch(_specs(30))
    # A ordem persistida (por `position`) bate exatamente com a ordem de
    # criação, repetidamente, nunca reordenada por leitura.
    for _ in range(3):
        assert batch.list_batch_job_ids(result.batch_id) == result.job_ids


# ---------------------------------------------------------------------------
# 7: dois batches com a mesma operation não se misturam
# ---------------------------------------------------------------------------


def test_dois_batches_mesma_operation_nao_se_misturam(batch):
    first = batch.create_batch(_specs(5, operation="RENDER"))
    second = batch.create_batch(_specs(5, operation="RENDER"))

    assert set(first.job_ids).isdisjoint(second.job_ids)
    assert set(batch.list_batch_job_ids(first.batch_id)) == set(first.job_ids)
    assert set(batch.list_batch_job_ids(second.batch_id)) == set(second.job_ids)

    # Cancelar o primeiro batch não pode tocar nenhum Job do segundo.
    batch.cancel_batch(first.batch_id)
    for job_id in second.job_ids:
        assert batch.job_engine.get_job(job_id).status == JOB_PENDING


# ---------------------------------------------------------------------------
# 8-9-30: nunca iniciar todos simultaneamente / limite de admissão
# ---------------------------------------------------------------------------


def test_advance_batch_nunca_dispara_tudo_de_uma_vez(batch, engine):
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(50))

    outcomes = batch.advance_batch(result.batch_id, limit=7)
    assert len(outcomes) == 7
    assert all(outcome.ok for outcome in outcomes)

    progress = batch.get_progress(result.batch_id)
    assert progress.ready == 7
    assert progress.pending == 43


def test_advance_batch_respeita_limit_customizado(batch, engine):
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(20))

    outcomes = batch.advance_batch(result.batch_id, limit=3)
    assert len(outcomes) == 3

    with pytest.raises(ValueError):
        batch.advance_batch(result.batch_id, limit=0)


def test_lote_de_500_nao_cria_500_handlers_simultaneos(batch, engine):
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(501))

    total_outcomes = 0
    calls = 0
    while True:
        outcomes = batch.advance_batch(result.batch_id, limit=50)
        calls += 1
        total_outcomes += len(outcomes)
        if not outcomes:
            break
        assert len(outcomes) <= 50

    assert total_outcomes == 501
    assert calls > 1  # nunca processado em uma única leva
    assert batch.get_progress(result.batch_id).ready == 501


# ---------------------------------------------------------------------------
# 10: falha em um Job não bloqueia os demais
# ---------------------------------------------------------------------------


def test_falha_em_um_job_nao_impede_os_demais(batch, engine):
    result = batch.create_batch(_specs(5, operation="FLAKY"))
    failing_id = result.job_ids[2]
    _register_failing_handler(engine, operation="FLAKY", fails_for={failing_id})

    outcomes = batch.advance_batch(result.batch_id, limit=10)
    assert len(outcomes) == 5

    failing_outcome = next(o for o in outcomes if o.job_id == failing_id)
    assert not failing_outcome.ok

    succeeded = [o for o in outcomes if o.job_id != failing_id]
    assert all(o.ok for o in succeeded)

    progress = batch.get_progress(result.batch_id)
    assert progress.ready == 4
    assert progress.failed == 1


# ---------------------------------------------------------------------------
# 11-12: progresso honesto (seis categorias + breakdown completo)
# ---------------------------------------------------------------------------


def test_progresso_seis_categorias_mandatorias(batch, engine, audit):
    result = batch.create_batch(_specs(6, operation="MIX"))
    ids = result.job_ids

    def handler(job):
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("MIX", handler, claims_status=JOB_PROCESSING)

    # ids[0]: fica PENDING (não tocado)
    # ids[1]: RETRY (ainda pendente na categoria "pending")
    audit.transition_job(ids[1], JOB_PROCESSING)
    audit.transition_job(ids[1], JOB_FAILED)
    audit.transition_job(ids[1], JOB_RETRY)
    # ids[2]: PROCESSING (ativo)
    audit.transition_job(ids[2], JOB_PROCESSING)
    # ids[3]: READY (sucesso)
    engine.advance(ids[3])
    # ids[4]: FAILED
    audit.transition_job(ids[4], JOB_PROCESSING)
    audit.transition_job(ids[4], JOB_FAILED)
    # ids[5]: BLOCKED (attention_required)
    audit.transition_job(ids[5], JOB_BLOCKED)

    progress = batch.get_progress(result.batch_id)
    assert progress.total == 6
    assert progress.pending == 2  # PENDING + RETRY
    assert progress.processing == 1
    assert progress.ready == 1
    assert progress.failed == 1
    assert progress.attention_required == 1
    assert progress.cancelled == 0


def test_progresso_breakdown_completo_por_status_real_e_honesto(batch, audit):
    result = batch.create_batch(_specs(3, operation="X"))
    ids = result.job_ids
    audit.transition_job(ids[0], JOB_CANCELLED)
    audit.transition_job(ids[1], JOB_BLOCKED)

    progress = batch.get_progress(result.batch_id)
    assert progress.breakdown[JOB_CANCELLED] == 1
    assert progress.breakdown[JOB_BLOCKED] == 1
    assert progress.breakdown[JOB_PENDING] == 1
    # CANCELLED fica fora das seis categorias mandatórias, mas nunca escondido.
    assert progress.cancelled == 1
    assert progress.pending + progress.processing + progress.ready + progress.failed + progress.attention_required == 2
    # A soma honesta é sempre contra o breakdown completo, nunca contra as seis.
    assert progress.total == sum(progress.breakdown.values()) == 3


# ---------------------------------------------------------------------------
# 13-14-15-16: pause/resume sem recriar o Prompt 15, sem corrida
# ---------------------------------------------------------------------------


def test_pause_bloqueia_admissao_de_jobs_novos(batch, engine):
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(5))
    batch.pause_batch(result.batch_id, reason="teste")

    outcomes = batch.advance_batch(result.batch_id, limit=10)
    assert outcomes == []

    with pytest.raises(JobBlockedByBatchError):
        engine.advance(result.job_ids[0])

    progress = batch.get_progress(result.batch_id)
    assert progress.pending == 5


def test_pause_nao_falseia_estado_de_job_ja_ativo(batch, audit):
    result = batch.create_batch(_specs(2))
    active_job_id = result.job_ids[0]
    audit.transition_job(active_job_id, JOB_PROCESSING)

    batch.pause_batch(result.batch_id)

    # Pausar o batch nunca reescreve o Job já ativo.
    job = batch.job_engine.get_job(active_job_id)
    assert job.status == JOB_PROCESSING

    # E o Job ativo pode terminar normalmente (o ponto seguro já aprovado
    # continua em vigor; pause de batch não interfere na finalização).
    finished = audit.transition_job(active_job_id, JOB_READY)
    assert finished.status == JOB_READY


def test_resume_so_afeta_o_batch_pedido(batch, engine):
    _register_noop_handler(engine)
    first = batch.create_batch(_specs(3))
    second = batch.create_batch(_specs(3))

    batch.pause_batch(first.batch_id)
    batch.pause_batch(second.batch_id)
    batch.resume_batch(first.batch_id)

    assert batch.is_batch_paused(first.batch_id) is False
    assert batch.is_batch_paused(second.batch_id) is True

    first_outcomes = batch.advance_batch(first.batch_id, limit=10)
    second_outcomes = batch.advance_batch(second.batch_id, limit=10)
    assert len(first_outcomes) == 3
    assert second_outcomes == []


def test_corrida_pause_vs_claim_e_sempre_consistente(batch, engine):
    _register_noop_handler(engine)

    for _ in range(25):
        result = batch.create_batch(_specs(1))
        job_id = result.job_ids[0]
        batch_id = result.batch_id

        barrier = threading.Barrier(2)
        outcome: dict = {}

        def do_pause():
            barrier.wait(timeout=5)
            batch.pause_batch(batch_id)

        def do_advance():
            barrier.wait(timeout=5)
            try:
                outcome["job"] = engine.advance(job_id)
            except JobBlockedByBatchError:
                outcome["blocked"] = True

        t1 = threading.Thread(target=do_pause)
        t2 = threading.Thread(target=do_advance)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        job = engine.get_job(job_id)
        if "blocked" in outcome:
            assert job.status == JOB_PENDING
        else:
            assert outcome["job"].status == JOB_READY
            assert job.status == JOB_READY


# ---------------------------------------------------------------------------
# 17-18-19: cancelamento individual, em massa, crash-safe
# ---------------------------------------------------------------------------


def test_cancelamento_individual_de_item_do_batch(batch):
    result = batch.create_batch(_specs(3))
    target = result.job_ids[1]

    outcome = batch.cancel_job(result.batch_id, target)
    assert outcome.ok
    assert batch.job_engine.get_job(target).status == JOB_CANCELLED

    for job_id in (result.job_ids[0], result.job_ids[2]):
        assert batch.job_engine.get_job(job_id).status == JOB_PENDING


def test_cancelamento_individual_rejeita_job_de_outro_batch(batch):
    first = batch.create_batch(_specs(1))
    second = batch.create_batch(_specs(1))

    with pytest.raises(JobNotEligibleForBatchError):
        batch.cancel_job(first.batch_id, second.job_ids[0])


def test_cancelamento_de_batch_inteiro_nao_toca_outro_batch(batch):
    first = batch.create_batch(_specs(4))
    second = batch.create_batch(_specs(4))

    outcome = batch.cancel_batch(first.batch_id)
    assert outcome.status == BATCH_CANCEL_FULLY_APPLIED
    assert set(outcome.cancelled_job_ids) == set(first.job_ids)

    for job_id in first.job_ids:
        assert batch.job_engine.get_job(job_id).status == JOB_CANCELLED
    for job_id in second.job_ids:
        assert batch.job_engine.get_job(job_id).status == JOB_PENDING


class _SimulatedCrash(BaseException):
    """BaseException evita ser silenciosamente capturada como um erro de
    negócio comum e simula uma queda abrupta real no meio do laço."""


def test_crash_real_durante_cancelamento_de_batch_bloqueia_tudo_ate_retomar(
    local_db, batch, engine, monkeypatch
):
    """Cenário obrigatório da revisão adversarial: uma queda REAL (não uma
    falha isolada de um item, que cancel_batch já isola via
    ScopeCancelOutcome.errors) precisa deixar cancel_request IN_PROGRESS —
    nunca COMPLETED — e essa intenção precisa bloquear TUDO até ser
    retomada: fetch_pending_jobs, advance() direto e advance_batch."""
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(4))

    real_cancel = batch.control_manager.cancel
    call_count = {"n": 0}

    def crashing_cancel(scope, scope_id=None, *, reason=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise _SimulatedCrash("queda real no meio do laço de cancelamento")
        return real_cancel(scope, scope_id, reason=reason)

    monkeypatch.setattr(batch.control_manager, "cancel", crashing_cancel)

    with pytest.raises(_SimulatedCrash):
        batch.cancel_batch(result.batch_id)

    # A intenção fica persistida como IN_PROGRESS — nunca COMPLETED por engano.
    request = batch.get_batch_cancel_request(result.batch_id)
    assert request["status"] == "IN_PROGRESS"
    # E não duplica a membership: só metadata pequena.
    assert "candidate_job_ids" not in request

    # "Reabre o banco": nova instância inteira, do zero, sem nenhum monkeypatch.
    reopened_db = LocalDatabase(path=local_db.path, paths=local_db.paths)
    reopened_audit = OperationalAuditLog(reopened_db)
    reopened_control = ControlManager(reopened_db, audit_log=reopened_audit)
    reopened_engine = JobEngine(reopened_db, audit_log=reopened_audit, control_manager=reopened_control)
    reopened_batch = BatchEngine(
        reopened_db, audit_log=reopened_audit, control_manager=reopened_control, job_engine=reopened_engine
    )

    handler_calls: list[str] = []

    def handler(job):
        handler_calls.append(job.id)
        return JobStepResult(target_status=JOB_READY)

    reopened_engine.register_handler("NOOP", handler, claims_status=JOB_PROCESSING)

    # Jobs restantes (ainda PENDING) não são oferecidos por fetch_pending_jobs.
    offered_ids = {job.id for job in reopened_engine.fetch_pending_jobs(limit=None)}
    assert offered_ids.isdisjoint(result.job_ids)

    # advance() direto sobre qualquer Job PENDING/RETRY do batch também é bloqueado.
    blocked_checked = 0
    for job_id in result.job_ids:
        job = reopened_engine.get_job(job_id)
        if job.status in (JOB_PENDING, JOB_RETRY):
            blocked_checked += 1
            with pytest.raises(JobBlockedByBatchError):
                reopened_engine.advance(job_id)
    assert blocked_checked >= 1

    # advance_batch não executa nada, e o handler nunca é chamado.
    outcomes = reopened_batch.advance_batch(result.batch_id, limit=10)
    assert outcomes == []
    assert handler_calls == []

    # Reinvocar cancel_batch retoma (mesma membership, recalculada de
    # batch_jobs) e conclui.
    second_attempt = reopened_batch.cancel_batch(result.batch_id)
    assert second_attempt.ok
    for job_id in result.job_ids:
        assert reopened_batch.job_engine.get_job(job_id).status == JOB_CANCELLED
    assert reopened_batch.get_batch_cancel_request(result.batch_id)["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# 20-21: reprocessar somente falhas
# ---------------------------------------------------------------------------


def test_retry_failed_reprocessa_somente_jobs_failed(batch, audit):
    result = batch.create_batch(_specs(4))
    ids = result.job_ids

    audit.transition_job(ids[0], JOB_PROCESSING)
    audit.transition_job(ids[0], JOB_FAILED)
    audit.transition_job(ids[1], JOB_PROCESSING)
    audit.transition_job(ids[1], JOB_FAILED)
    # ids[2] fica PENDING; ids[3] vai para BLOCKED (não é FAILED).
    audit.transition_job(ids[3], JOB_BLOCKED)

    outcome = batch.retry_failed(result.batch_id)
    assert set(outcome.retried_job_ids) == {ids[0], ids[1]}
    assert set(outcome.not_eligible_job_ids) == {ids[2], ids[3]}

    assert batch.job_engine.get_job(ids[0]).status == JOB_RETRY
    assert batch.job_engine.get_job(ids[1]).status == JOB_RETRY
    assert batch.job_engine.get_job(ids[2]).status == JOB_PENDING
    assert batch.job_engine.get_job(ids[3]).status == JOB_BLOCKED


def test_retry_failed_nunca_reprocessa_unknown_as_cegas(batch, audit):
    result = batch.create_batch(_specs(2))
    unknown_id = result.job_ids[0]
    audit.transition_job(unknown_id, JOB_PUBLISHING)
    audit.transition_job(unknown_id, JOB_UNKNOWN)

    outcome = batch.retry_failed(result.batch_id)
    assert unknown_id in outcome.not_eligible_job_ids
    assert unknown_id not in outcome.retried_job_ids
    assert batch.job_engine.get_job(unknown_id).status == JOB_UNKNOWN


# ---------------------------------------------------------------------------
# 22-23-24: comportamento após restart/crash
# ---------------------------------------------------------------------------


def test_restart_preserva_batch_pausado(local_db, batch):
    result = batch.create_batch(_specs(3))
    batch.pause_batch(result.batch_id, reason="mantido após restart")

    reopened_db = LocalDatabase(path=local_db.path, paths=local_db.paths)
    reopened_audit = OperationalAuditLog(reopened_db)
    reopened_control = ControlManager(reopened_db, audit_log=reopened_audit)
    reopened_engine = JobEngine(reopened_db, audit_log=reopened_audit, control_manager=reopened_control)
    reopened_batch = BatchEngine(
        reopened_db, audit_log=reopened_audit, control_manager=reopened_control, job_engine=reopened_engine
    )

    assert reopened_batch.is_batch_paused(result.batch_id) is True
    assert reopened_batch.list_batch_job_ids(result.batch_id) == result.job_ids


def test_restart_reconstroi_progresso_corretamente(local_db, batch, audit):
    result = batch.create_batch(_specs(4))
    audit.transition_job(result.job_ids[0], JOB_PROCESSING)
    audit.transition_job(result.job_ids[0], JOB_FAILED)

    reopened_db = LocalDatabase(path=local_db.path, paths=local_db.paths)
    reopened_audit = OperationalAuditLog(reopened_db)
    reopened_control = ControlManager(reopened_db, audit_log=reopened_audit)
    reopened_engine = JobEngine(reopened_db, audit_log=reopened_audit, control_manager=reopened_control)
    reopened_batch = BatchEngine(
        reopened_db, audit_log=reopened_audit, control_manager=reopened_control, job_engine=reopened_engine
    )

    progress = reopened_batch.get_progress(result.batch_id)
    assert progress.total == 4
    assert progress.failed == 1
    assert progress.pending == 3


# ---------------------------------------------------------------------------
# 25: concorrência real entre duas instâncias
# ---------------------------------------------------------------------------


def test_duas_instancias_de_batchengine_nunca_executam_o_mesmo_job_duas_vezes(local_db):
    audit_a = OperationalAuditLog(local_db)
    control_a = ControlManager(local_db, audit_log=audit_a)
    engine_a = JobEngine(local_db, audit_log=audit_a, control_manager=control_a)
    batch_a = BatchEngine(local_db, audit_log=audit_a, control_manager=control_a, job_engine=engine_a)

    db_b = LocalDatabase(path=local_db.path, paths=local_db.paths)
    audit_b = OperationalAuditLog(db_b)
    control_b = ControlManager(db_b, audit_log=audit_b)
    engine_b = JobEngine(db_b, audit_log=audit_b, control_manager=control_b)
    batch_b = BatchEngine(db_b, audit_log=audit_b, control_manager=control_b, job_engine=engine_b)

    executed: list[str] = []
    lock = threading.Lock()

    def handler(job):
        with lock:
            executed.append(job.id)
        return JobStepResult(target_status=JOB_READY)

    engine_a.register_handler("SHARED", handler, claims_status=JOB_PROCESSING)
    engine_b.register_handler("SHARED", handler, claims_status=JOB_PROCESSING)

    result = batch_a.create_batch(_specs(40, operation="SHARED"))

    barrier = threading.Barrier(2)

    def run(engine_instance, batch_instance):
        barrier.wait(timeout=5)
        for _ in range(10):
            batch_instance.advance_batch(result.batch_id, limit=10)

    t1 = threading.Thread(target=run, args=(engine_a, batch_a))
    t2 = threading.Thread(target=run, args=(engine_b, batch_b))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert len(executed) == 40
    assert len(set(executed)) == 40  # nunca duplicado

    progress = batch_a.get_progress(result.batch_id)
    assert progress.ready == 40


# ---------------------------------------------------------------------------
# 26: batch vazio — inválido, nunca um estado suportado
# ---------------------------------------------------------------------------


def test_batch_vazio_e_invalido(batch):
    with pytest.raises(EmptyBatchError):
        batch.create_batch([])
    with pytest.raises(EmptyBatchError):
        batch.create_batch_from_existing_jobs([])


# ---------------------------------------------------------------------------
# 27: criação parcial/erro no meio nunca deixa membership mentirosa
# ---------------------------------------------------------------------------


def test_criacao_a_partir_de_jobs_existentes_e_atomica_em_erro(batch):
    ok_result = batch.create_batch(_specs(1))
    already_member_job_id = ok_result.job_ids[0]

    # Job de verdade, ainda sem nenhum batch.
    free_job = batch.audit_log.create_job(Job(operation="FREE"))

    with pytest.raises(JobAlreadyInBatchError):
        batch.create_batch_from_existing_jobs([free_job.id, already_member_job_id])

    # A tentativa inteira foi revertida: o Job livre continua sem batch
    # nenhum (nenhuma membership parcial sobrou).
    assert batch.get_batch_id_for_job(free_job.id) is None

    with pytest.raises(JobNotEligibleForBatchError):
        batch.create_batch_from_existing_jobs(["11111111-1111-1111-1111-111111111111"])


def test_criacao_em_lote_reverte_tudo_em_erro_no_meio(batch):
    """``create_batch`` com um item inválido no meio não deve deixar nenhum
    Job nem nenhuma linha de batch/membership parcialmente persistida."""
    with pytest.raises(TypeError):
        batch.create_batch([{"operation": "A"}, {"operation_invalida_de_proposito": "B"}])

    with batch.database.connection() as conn:
        batches_count = conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
        jobs_count = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    assert batches_count == 0
    assert jobs_count == 0


# ---------------------------------------------------------------------------
# 28: shutdown DRAINING bloqueia avanço de novos batches
# ---------------------------------------------------------------------------


def test_shutdown_draining_bloqueia_avanco_de_batches(local_db, audit, control):
    from _sistema.shutdown_coordinator import ShutdownCoordinator

    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=3.0, poll_interval_seconds=0.02)
    shutdown_engine = JobEngine(
        local_db, audit_log=audit, control_manager=control, shutdown_coordinator=coordinator
    )
    shutdown_batch = BatchEngine(local_db, audit_log=audit, control_manager=control, job_engine=shutdown_engine)
    _register_noop_handler(shutdown_engine)

    result = shutdown_batch.create_batch(_specs(2))
    stuck_job_id = result.job_ids[0]
    audit.transition_job(stuck_job_id, JOB_PROCESSING)  # nunca sai: força o drain a nunca terminar sozinho

    coordinator.acquire_ownership()
    try:
        shutdown_finished = threading.Event()

        def do_shutdown():
            coordinator.shutdown(reason="teste_batch_draining")
            shutdown_finished.set()

        t = threading.Thread(target=do_shutdown)
        t.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not coordinator.is_draining():
                time.sleep(0.01)
            assert coordinator.is_draining() is True

            outcomes = shutdown_batch.advance_batch(result.batch_id, limit=10)
            assert outcomes == []
            assert shutdown_batch.job_engine.get_job(result.job_ids[1]).status == JOB_PENDING
        finally:
            audit.transition_job(stuck_job_id, JOB_FAILED)  # libera o drain
            t.join(timeout=5)
            assert shutdown_finished.is_set()
    finally:
        if coordinator.owns_instance:
            coordinator.release_ownership()


# ---------------------------------------------------------------------------
# 29: batch respeita ControlManager GLOBAL/QUEUE/JOB
# ---------------------------------------------------------------------------


def test_batch_respeita_control_manager_global(batch, engine, control):
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(3))
    control.pause(CONTROL_SCOPE_GLOBAL)

    outcomes = batch.advance_batch(result.batch_id, limit=10)
    assert outcomes == []


def test_batch_respeita_control_manager_queue(batch, engine, control):
    _register_noop_handler(engine, operation="RENDER")
    result = batch.create_batch(_specs(3, operation="RENDER"))
    control.pause(CONTROL_SCOPE_QUEUE, "RENDER")

    outcomes = batch.advance_batch(result.batch_id, limit=10)
    assert outcomes == []


def test_batch_respeita_control_manager_job(batch, engine, control):
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(3))
    control.pause(CONTROL_SCOPE_JOB, result.job_ids[0])

    outcomes = batch.advance_batch(result.batch_id, limit=10)
    outcome_ids = {o.job_id for o in outcomes}
    assert result.job_ids[0] not in outcome_ids
    assert len(outcomes) == 2


# ---------------------------------------------------------------------------
# BatchNotFoundError / validações auxiliares
# ---------------------------------------------------------------------------


def test_operacoes_em_batch_inexistente_levantam_batch_not_found(batch):
    fake_id = "22222222-2222-2222-2222-222222222222"
    with pytest.raises(BatchNotFoundError):
        batch.get_progress(fake_id)
    with pytest.raises(BatchNotFoundError):
        batch.pause_batch(fake_id)
    with pytest.raises(BatchNotFoundError):
        batch.advance_batch(fake_id)
    with pytest.raises(BatchNotFoundError):
        batch.retry_failed(fake_id)
    with pytest.raises(BatchNotFoundError):
        batch.cancel_batch(fake_id)


# ---------------------------------------------------------------------------
# 31: nenhuma dependência de UI/plataforma/segundo mecanismo de recovery
# ---------------------------------------------------------------------------


def test_batch_engine_nao_importa_ui_nem_connectors_de_plataforma():
    module_path = Path(__file__).parents[1] / "_sistema" / "batch_engine.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"playwright", "tkinter", "PyQt5", "PySide6"}
    assert imported.isdisjoint(forbidden)
    for platform_module in ("agendar_youtube", "agendar_tiktok", "painel_oficial"):
        assert platform_module not in imported


def test_batch_engine_nunca_cria_um_segundo_mecanismo_de_recovery():
    """BatchEngine não pode IMPORTAR ``recovery_manager``: a recuperação
    após crash continua sendo autoridade exclusiva do RecoveryManager já
    aprovado (PROMPT 14), batch ou não (menções em docstring/comentário
    explicando essa fronteira não contam como dependência real)."""
    module_path = Path(__file__).parents[1] / "_sistema" / "batch_engine.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
    assert "recovery_manager" not in imported


def test_batch_engine_nunca_altera_job_status_diretamente():
    """Varredura estática: nenhuma linha de batch_engine.py atribui
    ``.status =`` em um Job (a única forma permitida de mudar o status é
    através de ``OperationalAuditLog``/``JobEngine``, nunca diretamente)."""
    module_path = Path(__file__).parents[1] / "_sistema" / "batch_engine.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "status":
                    pytest.fail("BatchEngine não pode atribuir Job.status diretamente")


# ---------------------------------------------------------------------------
# BLOQUEADOR 1 (revisão adversarial): cancel_batch IN_PROGRESS bloqueia
# admissão de Jobs candidatos ainda não resolvidos pelo laço.
# ---------------------------------------------------------------------------


def test_cancel_batch_in_progress_bloqueia_claims_de_candidatos_pendentes(batch, engine, monkeypatch):
    """Reproduz literalmente o cenário do Bloqueador 1: o processo cai
    exatamente entre persistir IN_PROGRESS e resolver todos os candidatos
    (aqui, o primeiro candidato já foi cancelado antes da queda). Enquanto
    IN_PROGRESS persistir, nenhum candidato ainda PENDING pode ser
    reivindicado — nem via JobEngine.advance direto, nem via advance_batch."""
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(3))

    real_cancel = batch.control_manager.cancel

    def crash_on_second_candidate(scope, scope_id=None, *, reason=None):
        raise _StopMidLoop()

    monkeypatch.setattr(batch.control_manager, "cancel", crash_on_second_candidate)
    with pytest.raises(_StopMidLoop):
        batch.cancel_batch(result.batch_id)
    monkeypatch.setattr(batch.control_manager, "cancel", real_cancel)

    request = batch.get_batch_cancel_request(result.batch_id)
    assert request["status"] == "IN_PROGRESS"

    # Todos os candidatos ainda PENDING nunca podem iniciar trabalho novo
    # enquanto o cancel_request do batch continuar IN_PROGRESS.
    for job_id in result.job_ids:
        job = engine.get_job(job_id)
        if job.status == JOB_PENDING:
            with pytest.raises(JobBlockedByBatchError):
                engine.advance(job_id)

    outcomes = batch.advance_batch(result.batch_id, limit=10)
    assert outcomes == []

    # Retomar conclui e libera tudo.
    resumed = batch.cancel_batch(result.batch_id)
    assert resumed.ok
    assert batch.get_batch_cancel_request(result.batch_id)["status"] == "COMPLETED"
    assert batch.is_job_admission_blocked(result.job_ids[0]) is False


class _StopMidLoop(BaseException):
    pass


# ---------------------------------------------------------------------------
# BLOQUEADOR 2 (revisão adversarial): ControlManager nunca split-brain.
# ---------------------------------------------------------------------------


def test_job_engine_sem_control_manager_e_batch_engine_global_pause_bloqueia(local_db):
    """Cenário 1 exigido pela revisão: JobEngine sem ControlManager +
    BatchEngine -> GLOBAL pause do manager do BatchEngine bloqueia execução."""
    bare_engine = JobEngine(local_db)  # deliberadamente sem control_manager
    assert bare_engine.control_manager is None

    batch_engine = BatchEngine(local_db, job_engine=bare_engine)
    # BatchEngine anexou seu próprio ControlManager ao JobEngine fornecido.
    assert bare_engine.control_manager is batch_engine.control_manager
    _register_noop_handler(bare_engine)

    result = batch_engine.create_batch(_specs(3))
    batch_engine.control_manager.pause(CONTROL_SCOPE_GLOBAL)

    outcomes = batch_engine.advance_batch(result.batch_id, limit=10)
    assert outcomes == []
    for job_id in result.job_ids:
        assert bare_engine.get_job(job_id).status == JOB_PENDING


def test_cancel_requested_pelo_batch_engine_e_respeitado_pelo_job_engine(local_db):
    """Cenário 2 exigido pela revisão: cancel_requested criado pelo
    BatchEngine/ControlManager é respeitado pelo JobEngine ao finalizar um
    Job que estava ativo no momento do cancelamento."""
    bare_engine = JobEngine(local_db)
    batch_engine = BatchEngine(local_db, job_engine=bare_engine)

    def slow_handler(job):
        # Cancela o Job (via BatchEngine/ControlManager) enquanto o handler
        # já está "rodando" — mesmo ponto seguro de PROCESSING ativo já
        # aprovado no Prompt 15, agora acionado a partir do control_manager
        # do BatchEngine.
        batch_engine.cancel_job(result.batch_id, job.id)
        return JobStepResult(target_status=JOB_READY)

    bare_engine.register_handler("SLOW", slow_handler, claims_status=JOB_PROCESSING)
    result = batch_engine.create_batch(_specs(1, operation="SLOW"))

    updated = bare_engine.advance(result.job_ids[0])
    # O resultado do handler (READY) é substituído por CANCELLED no ponto
    # seguro já aprovado — prova de que o cancel_requested persistido pelo
    # control_manager do BatchEngine é o MESMO que o JobEngine consulta.
    assert updated.status == JOB_CANCELLED


def test_control_manager_incompativel_levanta_erro_claro(local_db, audit):
    control_a = ControlManager(local_db, audit_log=audit)
    control_b = ControlManager(local_db, audit_log=audit)
    assert control_a is not control_b

    engine_with_a = JobEngine(local_db, audit_log=audit, control_manager=control_a)

    with pytest.raises(ControlManagerMismatchError):
        BatchEngine(local_db, audit_log=audit, control_manager=control_b, job_engine=engine_with_a)

    # Passar a MESMA instância (ou omitir) nunca é um erro.
    batch_ok = BatchEngine(local_db, audit_log=audit, control_manager=control_a, job_engine=engine_with_a)
    assert batch_ok.control_manager is control_a
    assert engine_with_a.control_manager is control_a


# ---------------------------------------------------------------------------
# Auditoria mínima de batch (revisão adversarial)
# ---------------------------------------------------------------------------


def test_trilha_de_auditoria_minima_do_batch(batch, engine):
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(3))
    batch.pause_batch(result.batch_id, reason="manutencao")
    batch.resume_batch(result.batch_id, reason="ok")
    batch.cancel_batch(result.batch_id, reason="desistiu")

    events = batch.list_batch_events(result.batch_id)
    event_types = [event["event_type"] for event in events]

    assert event_types == [
        AUDIT_BATCH_CREATED,
        AUDIT_BATCH_PAUSED,
        AUDIT_BATCH_RESUMED,
        AUDIT_BATCH_CANCEL_REQUESTED,
        AUDIT_BATCH_CANCEL_COMPLETED,
    ]

    created = events[0]
    assert created["data"]["job_count"] == 3
    completed = events[-1]
    assert completed["data"]["cancelled"] == 3
    assert completed["data"]["status"] == BATCH_CANCEL_FULLY_APPLIED

    # Nenhum evento carrega uma lista de job_ids — payload sempre pequeno.
    for event in events:
        assert "job_ids" not in event["data"]
        assert "candidate_job_ids" not in event["data"]


def test_retry_failed_produz_evento_de_auditoria_resumido(batch, audit):
    result = batch.create_batch(_specs(2))
    audit.transition_job(result.job_ids[0], JOB_PROCESSING)
    audit.transition_job(result.job_ids[0], JOB_FAILED)

    batch.retry_failed(result.batch_id, reason="tentar de novo")

    events = batch.list_batch_events(result.batch_id)
    retry_events = [e for e in events if e["event_type"] == AUDIT_BATCH_RETRY_REQUESTED]
    assert len(retry_events) == 1
    assert retry_events[0]["data"]["retried"] == 1
    assert retry_events[0]["data"]["not_eligible"] == 1


def test_evento_de_batch_e_membership_commitam_na_mesma_transaction(batch):
    """create_batch grava batches + jobs + batch_jobs + BATCH_CREATED em UMA
    única transaction: não há como existir um sem o outro."""
    result = batch.create_batch(_specs(2))
    events = batch.list_batch_events(result.batch_id)
    assert any(e["event_type"] == AUDIT_BATCH_CREATED for e in events)
    assert batch.get_batch(result.batch_id) is not None
    assert len(batch.list_batch_job_ids(result.batch_id)) == 2


# ---------------------------------------------------------------------------
# Reavaliação: settings nunca guarda a membership duplicada
# ---------------------------------------------------------------------------


def test_cancel_request_em_settings_nunca_duplica_membership(batch):
    """Reavaliação da revisão adversarial: settings guarda só metadata
    pequena do cancel_request — a lista de candidatos vem sempre de
    batch_jobs (imutável), nunca de uma cópia em JSON."""
    result = batch.create_batch(_specs(500))
    batch.cancel_batch(result.batch_id)

    request = batch.get_batch_cancel_request(result.batch_id)
    assert "candidate_job_ids" not in request
    assert set(request.keys()) <= {"status", "reason", "requested_at", "completed_at"}


# ---------------------------------------------------------------------------
# SEGUNDA REVISÃO ADVERSARIAL
# Bloqueador 1: colaboradores precisam apontar para o mesmo SQLite lógico
# ---------------------------------------------------------------------------


@pytest.fixture
def two_dbs(tmp_path):
    """Dois arquivos SQLite DISTINTOS, cada um com seu próprio schema
    inicializado — usados para provar que BatchEngine rejeita colaboradores
    apontando para bancos diferentes."""
    db_a = LocalDatabase(path=tmp_path / "a.db")
    db_a.initialize()
    db_b = LocalDatabase(path=tmp_path / "b.db")
    db_b.initialize()
    return db_a, db_b


def test_batch_engine_rejeita_job_engine_com_database_diferente(two_dbs):
    db_a, db_b = two_dbs
    engine_a = JobEngine(db_a)

    with pytest.raises(BatchEngineDatabaseMismatchError):
        BatchEngine(db_b, job_engine=engine_a)

    # Construção inválida não pode ter mutado o job_engine fornecido.
    assert engine_a.control_manager is None
    assert getattr(engine_a, "batch_engine", None) is None


def test_batch_engine_rejeita_control_manager_com_database_diferente(two_dbs):
    db_a, db_b = two_dbs
    audit_a = OperationalAuditLog(db_a)
    control_a = ControlManager(db_a, audit_log=audit_a)

    with pytest.raises(BatchEngineDatabaseMismatchError):
        BatchEngine(db_b, control_manager=control_a)


def test_batch_engine_rejeita_audit_log_com_database_diferente(two_dbs):
    db_a, db_b = two_dbs
    audit_a = OperationalAuditLog(db_a)

    with pytest.raises(BatchEngineDatabaseMismatchError):
        BatchEngine(db_b, audit_log=audit_a)


def test_batch_engine_aceita_facades_distintas_do_mesmo_arquivo(tmp_path):
    """Duas instâncias Python de LocalDatabase diferentes, mas apontando
    para EXATAMENTE o mesmo arquivo .db, são uma configuração legítima —
    não split-brain. Precisa funcionar de ponta a ponta."""
    db_path = tmp_path / "painel.db"
    db_engine_side = LocalDatabase(path=db_path)
    db_engine_side.initialize()
    db_batch_side = LocalDatabase(path=db_path)  # facade distinta, mesmo arquivo

    engine = JobEngine(db_engine_side)
    _register_noop_handler(engine)
    batch_engine = BatchEngine(db_batch_side, job_engine=engine)

    result = batch_engine.create_batch(_specs(3))
    outcomes = batch_engine.advance_batch(result.batch_id, limit=10)
    assert len(outcomes) == 3
    progress = batch_engine.get_progress(result.batch_id)
    assert progress.ready == 3


def test_job_engine_fornecido_nao_e_mutado_apos_tentativa_invalida(two_dbs):
    """Depois de uma construção rejeitada, o job_engine original precisa
    permanecer exatamente como estava — inclusive quando ele JÁ tinha um
    control_manager/batch_engine anexado de uma configuração válida
    anterior, para provar que a validação roda antes de qualquer mutação."""
    db_a, db_b = two_dbs
    audit_a = OperationalAuditLog(db_a)
    control_a = ControlManager(db_a, audit_log=audit_a)
    engine_a = JobEngine(db_a, audit_log=audit_a, control_manager=control_a)
    valid_batch = BatchEngine(db_a, audit_log=audit_a, control_manager=control_a, job_engine=engine_a)

    assert engine_a.control_manager is control_a
    assert engine_a.batch_engine is valid_batch

    with pytest.raises(BatchEngineDatabaseMismatchError):
        BatchEngine(db_b, job_engine=engine_a)

    # Estado da configuração válida anterior preservado intacto.
    assert engine_a.control_manager is control_a
    assert engine_a.batch_engine is valid_batch


# ---------------------------------------------------------------------------
# SEGUNDA REVISÃO ADVERSARIAL
# Bloqueador 2: retomada de cancel_batch preserva o reason original
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resume_reason", ["CHANGED", None])
def test_retomada_de_cancel_batch_preserva_reason_original(
    local_db, batch, engine, monkeypatch, resume_reason
):
    """Cenário obrigatório da segunda revisão adversarial: um crash real no
    meio de cancel_batch(reason="ORIGINAL") não pode ter o reason
    sobrescrito por uma retomada com um reason diferente (ou None) — o
    pedido IN_PROGRESS já tem identidade própria."""
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(4))

    real_cancel = batch.control_manager.cancel
    call_count = {"n": 0}

    def crashing_cancel(scope, scope_id=None, *, reason=None):
        call_count["n"] += 1
        outcome = real_cancel(scope, scope_id, reason=reason)
        if call_count["n"] == 1:
            # Primeiro Job cancelado de verdade (reason ORIGINAL já
            # persistido no evento de auditoria dele), ENTÃO crasheia.
            raise _SimulatedCrash("queda real depois de cancelar o 1º candidato")
        return outcome

    monkeypatch.setattr(batch.control_manager, "cancel", crashing_cancel)

    with pytest.raises(_SimulatedCrash):
        batch.cancel_batch(result.batch_id, reason="ORIGINAL")

    request = batch.get_batch_cancel_request(result.batch_id)
    assert request["status"] == "IN_PROGRESS"
    assert request["reason"] == "ORIGINAL"

    # O primeiro Job, cancelado antes do crash, já tem reason=ORIGINAL.
    first_job_events = batch.audit_log.list_job_events(result.job_ids[0])
    cancelled_event = next(e for e in first_job_events if e["event_type"] == "JOB_CANCELLED")
    assert cancelled_event["data"]["reason"] == "ORIGINAL"

    # Retomada com um reason diferente (ou None) não pode mudar a operação.
    resumed = batch.cancel_batch(result.batch_id, reason=resume_reason)
    assert resumed.ok

    final_request = batch.get_batch_cancel_request(result.batch_id)
    assert final_request["status"] == "COMPLETED"
    assert final_request["reason"] == "ORIGINAL"

    # TODOS os Jobs cancelados nesta operação usam o reason ORIGINAL,
    # inclusive os que só foram resolvidos na retomada.
    for job_id in result.job_ids:
        assert batch.job_engine.get_job(job_id).status == JOB_CANCELLED
        events = batch.audit_log.list_job_events(job_id)
        cancelled = next(e for e in events if e["event_type"] == "JOB_CANCELLED")
        assert cancelled["data"]["reason"] == "ORIGINAL"


# ---------------------------------------------------------------------------
# TERCEIRA REVISÃO ADVERSARIAL
# Bloqueador 1: job_engine.audit_log (e o audit_log do ControlManager
# resolvido) também precisam apontar para o mesmo SQLite lógico
# ---------------------------------------------------------------------------


def test_batch_engine_rejeita_job_engine_com_audit_log_em_outro_database(two_dbs):
    """Reprodução exata da revisão: JobEngine.database == BatchEngine.database,
    mas JobEngine.audit_log aponta para outro arquivo — split-brain real, já
    que _land_safely_after_claim_failure grava pelo audit_log interno sem
    connection externa."""
    db_a, db_b = two_dbs
    audit_b = OperationalAuditLog(db_b)
    engine = JobEngine(db_a, audit_log=audit_b)

    with pytest.raises(BatchEngineDatabaseMismatchError):
        BatchEngine(db_a, job_engine=engine)

    # Construção inválida não pode ter mutado o job_engine fornecido.
    assert engine.control_manager is None
    assert getattr(engine, "batch_engine", None) is None
    assert engine.audit_log is audit_b


def test_batch_engine_rejeita_control_manager_do_job_engine_com_audit_log_em_outro_database(two_dbs):
    db_a, db_b = two_dbs
    audit_a = OperationalAuditLog(db_a)
    control_mismatched_audit = ControlManager(db_a, audit_log=OperationalAuditLog(db_b))
    engine = JobEngine(db_a, audit_log=audit_a, control_manager=control_mismatched_audit)

    with pytest.raises(BatchEngineDatabaseMismatchError):
        BatchEngine(db_a, job_engine=engine)

    assert engine.control_manager is control_mismatched_audit
    assert getattr(engine, "batch_engine", None) is None


def test_batch_engine_rejeita_control_manager_explicito_com_audit_log_em_outro_database(two_dbs):
    db_a, db_b = two_dbs
    control_a = ControlManager(db_a, audit_log=OperationalAuditLog(db_b))

    with pytest.raises(BatchEngineDatabaseMismatchError):
        BatchEngine(db_a, control_manager=control_a)


def test_batch_engine_aceita_audit_log_facade_distinta_do_mesmo_arquivo(tmp_path):
    """JobEngine e OperationalAuditLog usando facades LocalDatabase
    distintas, mas apontando para EXATAMENTE o mesmo arquivo .db, continuam
    válidos — e precisam funcionar de ponta a ponta, inclusive no caminho de
    falha de handler que usa audit_log sem connection externa."""
    db_path = tmp_path / "painel.db"
    LocalDatabase(path=db_path).initialize()

    engine = JobEngine(LocalDatabase(path=db_path), audit_log=OperationalAuditLog(LocalDatabase(path=db_path)))

    def failing_handler(job):
        raise RuntimeError("falha simulada no handler")

    engine.register_handler("NOOP", failing_handler, claims_status=JOB_PROCESSING)

    batch_engine = BatchEngine(LocalDatabase(path=db_path), job_engine=engine)
    result = batch_engine.create_batch(_specs(1))

    outcomes = batch_engine.advance_batch(result.batch_id, limit=10)
    assert len(outcomes) == 1
    # O Job pousa corretamente em FAILED (não fica abandonado em
    # PROCESSING) porque job_engine.audit_log aponta para o mesmo arquivo.
    assert batch_engine.job_engine.get_job(result.job_ids[0]).status == JOB_FAILED


# ---------------------------------------------------------------------------
# TERCEIRA REVISÃO ADVERSARIAL
# Bloqueador 2: abertura/retomada e completion de cancel_batch são atômicas
# entre instâncias concorrentes — identidade histórica única
# ---------------------------------------------------------------------------


def test_cancel_batch_concorrente_entre_duas_instancias_preserva_identidade_unica(local_db):
    audit_a = OperationalAuditLog(local_db)
    control_a = ControlManager(local_db, audit_log=audit_a)
    engine_a = JobEngine(local_db, audit_log=audit_a, control_manager=control_a)
    batch_a = BatchEngine(local_db, audit_log=audit_a, control_manager=control_a, job_engine=engine_a)

    db_b = LocalDatabase(path=local_db.path, paths=local_db.paths)
    audit_b = OperationalAuditLog(db_b)
    control_b = ControlManager(db_b, audit_log=audit_b)
    engine_b = JobEngine(db_b, audit_log=audit_b, control_manager=control_b)
    batch_b = BatchEngine(db_b, audit_log=audit_b, control_manager=control_b, job_engine=engine_b)

    handler_calls: list[str] = []

    def handler(job):
        handler_calls.append(job.id)
        return JobStepResult(target_status=JOB_READY)

    engine_a.register_handler("NOOP", handler, claims_status=JOB_PROCESSING)
    engine_b.register_handler("NOOP", handler, claims_status=JOB_PROCESSING)

    result = batch_a.create_batch(_specs(20))

    barrier = threading.Barrier(2)
    results: dict[str, BatchCancelOutcome] = {}
    thread_errors: dict[str, BaseException] = {}

    def run(batch_instance, reason, key):
        try:
            barrier.wait(timeout=5)
            results[key] = batch_instance.cancel_batch(result.batch_id, reason=reason)
        except BaseException as exc:  # nenhuma exceção deve escapar
            thread_errors[key] = exc

    t1 = threading.Thread(target=run, args=(batch_a, "A", "a"))
    t2 = threading.Thread(target=run, args=(batch_b, "B", "b"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not thread_errors, f"chamada concorrente levantou exceção: {thread_errors}"
    assert "a" in results and "b" in results

    events = batch_a.list_batch_events(result.batch_id)
    requested_events = [e for e in events if e["event_type"] == AUDIT_BATCH_CANCEL_REQUESTED]
    completed_events = [e for e in events if e["event_type"] == AUDIT_BATCH_CANCEL_COMPLETED]
    assert len(requested_events) == 1
    assert len(completed_events) == 1

    winning_reason = requested_events[0]["data"]["reason"]
    assert winning_reason in ("A", "B")

    final_request = batch_a.get_batch_cancel_request(result.batch_id)
    assert final_request["status"] == "COMPLETED"
    assert final_request["reason"] == winning_reason

    for job_id in result.job_ids:
        assert batch_a.job_engine.get_job(job_id).status == JOB_CANCELLED
        job_events = audit_a.list_job_events(job_id)
        cancelled = next(e for e in job_events if e["event_type"] == "JOB_CANCELLED")
        assert cancelled["data"]["reason"] == winning_reason

    assert handler_calls == []


# ---------------------------------------------------------------------------
# TERCEIRA REVISÃO ADVERSARIAL
# Bloqueador 3: retry_failed concorrente não propaga InvalidJobTransition
# ---------------------------------------------------------------------------


def test_retry_failed_concorrente_nao_derruba_a_operacao(local_db):
    audit_a = OperationalAuditLog(local_db)
    control_a = ControlManager(local_db, audit_log=audit_a)
    engine_a = JobEngine(local_db, audit_log=audit_a, control_manager=control_a)
    batch_a = BatchEngine(local_db, audit_log=audit_a, control_manager=control_a, job_engine=engine_a)

    db_b = LocalDatabase(path=local_db.path, paths=local_db.paths)
    audit_b = OperationalAuditLog(db_b)
    control_b = ControlManager(db_b, audit_log=audit_b)
    engine_b = JobEngine(db_b, audit_log=audit_b, control_manager=control_b)
    batch_b = BatchEngine(db_b, audit_log=audit_b, control_manager=control_b, job_engine=engine_b)

    result = batch_a.create_batch(_specs(1))
    job_id = result.job_ids[0]
    audit_a.transition_job(job_id, JOB_PROCESSING)
    audit_a.transition_job(job_id, JOB_FAILED)

    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    thread_errors: dict[str, BaseException] = {}

    def run(batch_instance, key):
        try:
            barrier.wait(timeout=5)
            outcomes[key] = batch_instance.retry_failed(result.batch_id)
        except BaseException as exc:  # nenhuma thread pode levantar sem tratamento
            thread_errors[key] = exc

    t1 = threading.Thread(target=run, args=(batch_a, "a"))
    t2 = threading.Thread(target=run, args=(batch_b, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not thread_errors, f"retry_failed concorrente levantou exceção: {thread_errors}"
    assert set(outcomes) == {"a", "b"}

    retried_calls = sum(1 for outcome in outcomes.values() if job_id in outcome.retried_job_ids)
    not_eligible_calls = sum(1 for outcome in outcomes.values() if job_id in outcome.not_eligible_job_ids)
    error_calls = sum(1 for outcome in outcomes.values() if any(j == job_id for j, _ in outcome.errors))

    assert retried_calls == 1
    assert not_eligible_calls == 1
    assert error_calls == 0

    final_job = batch_a.job_engine.get_job(job_id)
    assert final_job.status == JOB_RETRY

    retry_events = [e for e in audit_a.list_job_events(job_id) if e["event_type"] == "JOB_RETRY"]
    assert len(retry_events) == 1


def test_retry_failed_concorrendo_com_cancel_job_nao_derruba_o_batch(local_db):
    """Se o Job deixar de ser FAILED (por um cancel_job concorrente) antes
    do retry_failed conseguir transicioná-lo, a chamada inteira não pode
    explodir — o Job só deixa de ser elegível."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    batch_engine = BatchEngine(local_db, audit_log=audit, control_manager=control, job_engine=engine)

    result = batch_engine.create_batch(_specs(1))
    job_id = result.job_ids[0]
    audit.transition_job(job_id, JOB_PROCESSING)
    audit.transition_job(job_id, JOB_FAILED)

    real_retry = audit.retry
    cancelled_first = {"done": False}

    def retry_after_cancel(job_id_arg, **kwargs):
        if not cancelled_first["done"]:
            cancelled_first["done"] = True
            batch_engine.cancel_job(result.batch_id, job_id_arg)
        return real_retry(job_id_arg, **kwargs)

    import unittest.mock as mock

    with mock.patch.object(audit, "retry", side_effect=retry_after_cancel):
        outcome = batch_engine.retry_failed(result.batch_id)

    assert job_id in outcome.not_eligible_job_ids
    assert outcome.errors == ()
    assert batch_engine.job_engine.get_job(job_id).status == JOB_CANCELLED


# ---------------------------------------------------------------------------
# QUARTA REVISÃO ADVERSARIAL
# Bloqueador 1: cancelamento de batch é monotônico — padrão ABA de caller
# atrasado não pode completar uma operação diferente; BATCH_CANCEL_RESUMED
# não pode aparecer historicamente depois de BATCH_CANCEL_COMPLETED
# ---------------------------------------------------------------------------


def test_cancel_batch_ordering_requested_resumed_completed(batch, engine, monkeypatch):
    """A ordem histórica precisa ser sempre REQUESTED -> RESUMED ->
    COMPLETED, nunca REQUESTED -> COMPLETED -> RESUMED (TOCTOU de auditoria
    corrigido: RESUMED é gravado na MESMA transaction que detecta
    IN_PROGRESS)."""
    _register_noop_handler(engine)
    result = batch.create_batch(_specs(3))

    real_cancel = batch.control_manager.cancel

    def crash_on_first_call(scope, scope_id=None, *, reason=None):
        raise _StopMidLoop("crash antes de qualquer efeito")

    monkeypatch.setattr(batch.control_manager, "cancel", crash_on_first_call)
    with pytest.raises(_StopMidLoop):
        batch.cancel_batch(result.batch_id, reason="ORIGINAL")
    monkeypatch.setattr(batch.control_manager, "cancel", real_cancel)

    # Retomada real: agora precisa concluir de verdade.
    outcome = batch.cancel_batch(result.batch_id, reason="IGNORADO_NA_RETOMADA")
    assert outcome.ok

    events = batch.list_batch_events(result.batch_id)
    cancel_event_types = [e["event_type"] for e in events if e["event_type"].startswith("BATCH_CANCEL")]
    assert cancel_event_types == [
        AUDIT_BATCH_CANCEL_REQUESTED,
        AUDIT_BATCH_CANCEL_RESUMED,
        AUDIT_BATCH_CANCEL_COMPLETED,
    ]

    final_request = batch.get_batch_cancel_request(result.batch_id)
    assert final_request["reason"] == "ORIGINAL"


def test_cancel_batch_caller_atrasado_padrao_aba_nao_completa_operacao_diferente(local_db):
    """Reprodução determinística (Events, sem sleep probabilístico) do
    padrão ABA da quarta revisão adversarial:

    A abre cancel_request reason=A (crasheia antes de qualquer efeito).
    B detecta/retoma essa MESMA operação e fica deliberadamente parado
    ANTES de aplicar qualquer efeito (bloqueado num Event).
    A (mesma intenção histórica) termina normalmente -> COMPLETED reason=A.
    C chama cancel_batch(reason=C) depois de A já COMPLETED.
    B finalmente é liberado e continua.

    Com o cancelamento de batch monotônico, nem C nem o B atrasado podem
    abrir/completar uma operação diferente com reason=C: só existe UMA
    intenção histórica (reason=A) para este batch."""
    audit_a = OperationalAuditLog(local_db)
    control_a = ControlManager(local_db, audit_log=audit_a)
    engine_a = JobEngine(local_db, audit_log=audit_a, control_manager=control_a)
    batch_a = BatchEngine(local_db, audit_log=audit_a, control_manager=control_a, job_engine=engine_a)

    db_b = LocalDatabase(path=local_db.path, paths=local_db.paths)
    audit_b = OperationalAuditLog(db_b)
    control_b = ControlManager(db_b, audit_log=audit_b)
    engine_b = JobEngine(db_b, audit_log=audit_b, control_manager=control_b)
    batch_b = BatchEngine(db_b, audit_log=audit_b, control_manager=control_b, job_engine=engine_b)

    db_c = LocalDatabase(path=local_db.path, paths=local_db.paths)
    audit_c = OperationalAuditLog(db_c)
    control_c = ControlManager(db_c, audit_log=audit_c)
    engine_c = JobEngine(db_c, audit_log=audit_c, control_manager=control_c)
    batch_c = BatchEngine(db_c, audit_log=audit_c, control_manager=control_c, job_engine=engine_c)

    handler_calls: list[str] = []

    def handler(job):
        handler_calls.append(job.id)
        return JobStepResult(target_status=JOB_READY)

    for e in (engine_a, engine_b, engine_c):
        e.register_handler("NOOP", handler, claims_status=JOB_PROCESSING)

    result = batch_a.create_batch(_specs(6))

    # A abre a operação reason=A e crasheia antes de qualquer efeito —
    # cancel_request fica IN_PROGRESS reason=A.
    real_cancel_a = batch_a.control_manager.cancel

    def crash_first(scope, scope_id=None, *, reason=None):
        raise _StopMidLoop("A crasheia ao abrir, antes de qualquer efeito")

    batch_a.control_manager.cancel = crash_first
    with pytest.raises(_StopMidLoop):
        batch_a.cancel_batch(result.batch_id, reason="A")
    batch_a.control_manager.cancel = real_cancel_a

    request_after_open = batch_a.get_batch_cancel_request(result.batch_id)
    assert request_after_open["status"] == "IN_PROGRESS"
    assert request_after_open["reason"] == "A"

    # B detecta/retoma a MESMA operação e fica parado deliberadamente antes
    # de aplicar qualquer efeito.
    b_ready = threading.Event()
    release_b = threading.Event()
    real_cancel_b = batch_b.control_manager.cancel

    def blocking_cancel_b(scope, scope_id=None, *, reason=None):
        b_ready.set()
        release_b.wait(timeout=10)
        return real_cancel_b(scope, scope_id, reason=reason)

    batch_b.control_manager.cancel = blocking_cancel_b
    b_outcome_holder: dict[str, Any] = {}
    b_error_holder: dict[str, BaseException] = {}

    def run_b():
        try:
            b_outcome_holder["outcome"] = batch_b.cancel_batch(result.batch_id, reason="B")
        except BaseException as exc:
            b_error_holder["error"] = exc

    t_b = threading.Thread(target=run_b)
    t_b.start()
    assert b_ready.wait(timeout=10), "B nunca chegou a bloquear no primeiro efeito"

    # B já retomou (BATCH_CANCEL_RESUMED já deve estar gravado) e está
    # parado ANTES de qualquer efeito.
    events_while_b_blocked = batch_a.list_batch_events(result.batch_id)
    assert any(e["event_type"] == AUDIT_BATCH_CANCEL_RESUMED for e in events_while_b_blocked)
    assert not any(e["event_type"] == AUDIT_BATCH_CANCEL_COMPLETED for e in events_while_b_blocked)

    # A (a mesma intenção histórica) agora termina de verdade.
    a_outcome = batch_a.cancel_batch(result.batch_id, reason="A")
    assert a_outcome.ok

    final_request_after_a = batch_a.get_batch_cancel_request(result.batch_id)
    assert final_request_after_a["status"] == "COMPLETED"
    assert final_request_after_a["reason"] == "A"

    # C chama cancel_batch DEPOIS de A já COMPLETED — monotônico: não abre
    # uma segunda geração, não usa reason=C em lugar nenhum.
    c_outcome = batch_c.cancel_batch(result.batch_id, reason="C")
    assert c_outcome.ok

    request_after_c = batch_c.get_batch_cancel_request(result.batch_id)
    assert request_after_c["status"] == "COMPLETED"
    assert request_after_c["reason"] == "A"  # nunca "C"

    # Libera o B atrasado, que agora só encontra a MESMA intenção histórica.
    release_b.set()
    t_b.join(timeout=15)

    assert not b_error_holder, f"B atrasado levantou exceção: {b_error_holder}"
    assert "outcome" in b_outcome_holder
    assert b_outcome_holder["outcome"].ok

    # Identidade histórica final: reason=A, nunca B nem C.
    final_request = batch_a.get_batch_cancel_request(result.batch_id)
    assert final_request["status"] == "COMPLETED"
    assert final_request["reason"] == "A"

    events = batch_a.list_batch_events(result.batch_id)
    requested_events = [e for e in events if e["event_type"] == AUDIT_BATCH_CANCEL_REQUESTED]
    completed_events = [e for e in events if e["event_type"] == AUDIT_BATCH_CANCEL_COMPLETED]
    assert len(requested_events) == 1
    assert requested_events[0]["data"]["reason"] == "A"
    assert len(completed_events) == 1

    for job_id in result.job_ids:
        assert batch_a.job_engine.get_job(job_id).status == JOB_CANCELLED
        job_events = audit_a.list_job_events(job_id)
        cancelled = next(e for e in job_events if e["event_type"] == "JOB_CANCELLED")
        assert cancelled["data"]["reason"] == "A"  # nunca "B" nem "C"

    assert handler_calls == []


# ---------------------------------------------------------------------------
# QUARTA REVISÃO ADVERSARIAL
# Bloqueador 2: job_engine.shutdown_coordinator também precisa apontar para
# o mesmo SQLite lógico
# ---------------------------------------------------------------------------


def test_batch_engine_rejeita_shutdown_coordinator_com_database_diferente(two_dbs):
    from _sistema.shutdown_coordinator import ShutdownCoordinator

    db_a, db_b = two_dbs
    coordinator_b = ShutdownCoordinator(db_b)
    engine = JobEngine(db_a, shutdown_coordinator=coordinator_b)

    with pytest.raises(BatchEngineDatabaseMismatchError):
        BatchEngine(db_a, job_engine=engine)

    # Construção inválida não pode ter mutado o job_engine fornecido.
    assert engine.control_manager is None
    assert getattr(engine, "batch_engine", None) is None
    assert engine.shutdown_coordinator is coordinator_b


def test_batch_engine_aceita_shutdown_coordinator_facade_distinta_do_mesmo_arquivo(tmp_path):
    from _sistema.shutdown_coordinator import ShutdownCoordinator

    db_path = tmp_path / "painel.db"
    LocalDatabase(path=db_path).initialize()

    coordinator = ShutdownCoordinator(
        LocalDatabase(path=db_path), drain_timeout_seconds=3.0, poll_interval_seconds=0.02
    )
    engine = JobEngine(LocalDatabase(path=db_path), shutdown_coordinator=coordinator)
    _register_noop_handler(engine)

    batch_engine = BatchEngine(LocalDatabase(path=db_path), job_engine=engine)
    result = batch_engine.create_batch(_specs(2))
    outcomes = batch_engine.advance_batch(result.batch_id, limit=10)
    assert len(outcomes) == 2


def test_batch_engine_com_shutdown_coordinator_correto_continua_bloqueando_em_draining(tmp_path):
    """Regressão positiva: com o coordinator apontando para o MESMO
    arquivo (facade Python distinta), BatchEngine continua respeitando
    DRAINING normalmente — a validação nova não pode ter quebrado o
    comportamento já aprovado no Prompt 16/17."""
    from _sistema.shutdown_coordinator import ShutdownCoordinator

    db_path = tmp_path / "painel.db"
    LocalDatabase(path=db_path).initialize()

    audit = OperationalAuditLog(LocalDatabase(path=db_path))
    control = ControlManager(LocalDatabase(path=db_path), audit_log=audit)
    coordinator = ShutdownCoordinator(
        LocalDatabase(path=db_path), drain_timeout_seconds=3.0, poll_interval_seconds=0.02
    )
    engine = JobEngine(
        LocalDatabase(path=db_path), audit_log=audit, control_manager=control, shutdown_coordinator=coordinator
    )
    batch_engine = BatchEngine(LocalDatabase(path=db_path), audit_log=audit, control_manager=control, job_engine=engine)
    _register_noop_handler(engine)

    result = batch_engine.create_batch(_specs(2))
    stuck_job_id = result.job_ids[0]
    audit.transition_job(stuck_job_id, JOB_PROCESSING)  # nunca sai: força o drain a nunca terminar sozinho

    coordinator.acquire_ownership()
    try:
        shutdown_finished = threading.Event()

        def do_shutdown():
            coordinator.shutdown(reason="teste_bloqueador2_quarta_revisao")
            shutdown_finished.set()

        t = threading.Thread(target=do_shutdown)
        t.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not coordinator.is_draining():
                time.sleep(0.01)
            assert coordinator.is_draining() is True

            outcomes = batch_engine.advance_batch(result.batch_id, limit=10)
            assert outcomes == []
            assert batch_engine.job_engine.get_job(result.job_ids[1]).status == JOB_PENDING
        finally:
            audit.transition_job(stuck_job_id, JOB_FAILED)  # libera o drain
            t.join(timeout=5)
            assert shutdown_finished.is_set()
    finally:
        if coordinator.owns_instance:
            coordinator.release_ownership()
