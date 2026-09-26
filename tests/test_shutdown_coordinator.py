"""Testes do PROMPT 16 — Graceful Shutdown (revisado após DUAS revisões
adversariais).

Cobre: persistência de DRAINING (settings, crash-safe), atomicidade contra
claim, o contrato de ponto seguro (nenhuma transição de Job forçada pelo
coordinator), a separação entre ``admission_blocked`` (nunca reaberto
automaticamente por ``shutdown()``, mesmo drenado — Bloqueador 1 da primeira
revisão) e ``drain_status``, timeout sem sucesso/falha fictícios e sem rodar
cleanup sobre recurso ainda em uso, ownership de instância via lock de SO
como prova de exclusividade (Bloqueador 2 da primeira revisão —
``startup_reconcile()`` nunca apaga o DRAINING de outra instância viva),
TODOS os mutadores de lifecycle exigindo ownership comprovada — não só
``startup_reconcile()`` (Bloqueador 1 da segunda revisão), validação de
recovery completo (via leitura pura do SQLite, sem acoplar
``RecoveryManager``) antes de ``startup_reconcile()`` liberar admissão
(Bloqueador 2 da segunda revisão), cleanup exatamente uma vez por
``shutdown_id`` mesmo sob chamadas repetidas/concorrentes (MAJOR da segunda
revisão), identidade de sessão operacional do processo impedindo que o
MESMO processo desfaça o próprio shutdown via ``startup_reconcile()``
(Bloqueador 1 da terceira revisão), e ausência de janela TOCTOU entre
verificar ownership e mutar o lifecycle -- ``release_ownership()`` nunca
libera o lock de SO enquanto uma operação crítica ainda está em andamento
(Bloqueador 2 da terceira revisão), encerramento forçado (processo
separado, ``os._exit``) durante
operações representativas de processamento/transcrição/análise/
renderização e durante publicação, e concorrência entre shutdowns (mesmo
processo e processos diferentes, agora sob a regra de ownership).

Por padrão, o fixture ``coordinator`` já adquire ownership (reflete o
contrato de uso real: ownership é adquirida cedo na inicialização do
processo — ver docstring do módulo). Testes que precisam especificamente de
um coordinator SEM ownership (para provar a rejeição) constroem sua própria
instância "nua" em vez de usar o fixture.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    Job,
    JOB_FAILED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_UNKNOWN,
)
from _sistema.storage import LocalDatabase, OperationalAuditLog
from _sistema.job_engine import (
    JobBlockedByShutdownError,
    JobEngine,
    JobEngineError,
    JobStepResult,
)
from _sistema.recovery_manager import (
    ACTION_FLAGGED_FOR_RECONCILIATION,
    ACTION_MARKED_FAILED_FOR_MANUAL_RETRY,
    RecoveryManager,
)
from _sistema.shutdown_coordinator import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    LIFECYCLE_DRAINING_KEY,
    SAFE_ERROR_TYPES,
    OwnershipAlreadyHeldError,
    OwnershipNotHeldError,
    PreviousSessionLifecycleRequiresReconcileError,
    RecoveryIncompleteError,
    STATUS_ABORTED,
    STATUS_DRAINED,
    STATUS_IN_PROGRESS,
    STATUS_TIMED_OUT,
    ShutdownCoordinator,
    ShutdownCoordinatorError,
    _deserialize_cleanup_results,
    _UNRECOGNIZED_ERROR_TYPE,
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
def coordinator(local_db):
    """Coordinator que já adquiriu ownership — reflete o contrato real de
    uso (``acquire_ownership()`` cedo na inicialização do processo), e é o
    que a maioria dos testes deste módulo precisa para poder chamar
    ``shutdown()``/``abort_shutdown()``/``force_cleanup_after_timeout()``
    (todos exigem ownership desde a segunda revisão adversarial —
    Bloqueador 1). Testes que precisam de um coordinator SEM ownership para
    provar a rejeição constroem sua própria instância "nua"."""
    c = ShutdownCoordinator(local_db, drain_timeout_seconds=2.0, poll_interval_seconds=0.02)
    c.acquire_ownership()
    yield c
    c.release_ownership()


@pytest.fixture
def engine(local_db, audit, coordinator):
    return JobEngine(local_db, audit_log=audit, shutdown_coordinator=coordinator)


def _create_job(audit_log, *, operation="RENDER"):
    return audit_log.create_job(Job(operation=operation))


def _fresh_database(tmp_root: Path, *, initialize: bool) -> LocalDatabase:
    paths = build_app_paths(install_root=tmp_root / "install", data_root=tmp_root / "data")
    db = LocalDatabase(paths=paths)
    if initialize:
        db.initialize()
    return db


# ---------------------------------------------------------------------------
# 1/4/6 — DRAINING persistido bloqueia novo claim; PENDING não começa depois
# ---------------------------------------------------------------------------


def test_draining_persistido_antes_do_claim_impede_novo_job(local_db, audit, engine, coordinator):
    job = _create_job(audit, operation="RENDER")
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    coordinator._begin_draining(reason="teste")
    assert coordinator.is_draining() is True

    with pytest.raises(JobBlockedByShutdownError):
        engine.advance(job.id)

    assert local_db.get(Job, job.id).status == JOB_PENDING  # nenhuma transição tentada


def test_job_pending_nao_comeca_depois_de_draining(local_db, audit, engine, coordinator):
    coordinator._begin_draining(reason="fechando")
    job = _create_job(audit, operation="RENDER")
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    assert engine.fetch_pending_jobs() == []
    with pytest.raises(JobBlockedByShutdownError):
        engine.advance(job.id)


def test_novo_job_nao_inicia_enquanto_outro_esta_drenando(local_db, audit, engine, coordinator):
    """Um Job PROCESSING legitimamente em andamento (reivindicado ANTES do
    DRAINING) não bloqueia — mas nenhum job PENDING novo é reivindicado
    enquanto o DRAINING permanecer ativo."""
    processing_job = _create_job(audit, operation="RENDER")
    audit.transition_job(processing_job.id, JOB_PROCESSING)  # já reivindicado antes do shutdown
    pending_job = _create_job(audit, operation="RENDER")

    coordinator._begin_draining(reason="fechando")
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    assert [j.id for j in engine.fetch_pending_jobs()] == []
    with pytest.raises(JobBlockedByShutdownError):
        engine.advance(pending_job.id)
    assert local_db.get(Job, processing_job.id).status == JOB_PROCESSING


# ---------------------------------------------------------------------------
# BLOQUEADOR 1 — admissão nunca reabre só porque shutdown() retornou
# ---------------------------------------------------------------------------


def test_shutdown_drenado_retorna_mas_admissao_continua_bloqueada(local_db, audit, engine, coordinator):
    report = coordinator.shutdown(reason="fechando_normal", drain_timeout_seconds=0.2)
    assert report.drained_completely is True
    assert report.drain_status == STATUS_DRAINED

    # o PROCESSO continua vivo (nenhum coordinator/engine foi destruído) —
    # mesmo assim, nenhum Job novo pode ser reivindicado.
    assert coordinator.is_draining() is True
    job = _create_job(audit, operation="RENDER")
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)
    with pytest.raises(JobBlockedByShutdownError):
        engine.advance(job.id)
    assert local_db.get(Job, job.id).status == JOB_PENDING


def test_shutdown_com_timeout_retorna_e_admissao_continua_bloqueada(local_db, audit, engine, coordinator):
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)  # nunca termina

    report = coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.15)
    assert report.drained_completely is False
    assert report.requires_forced_action is True
    assert report.drain_status == STATUS_TIMED_OUT

    assert coordinator.is_draining() is True
    new_job = _create_job(audit, operation="RENDER")
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)
    with pytest.raises(JobBlockedByShutdownError):
        engine.advance(new_job.id)
    # o job que já estava travado também não foi tocado
    assert local_db.get(Job, stuck.id).status == JOB_PROCESSING


def test_somente_abort_shutdown_explicito_reabre_admissao_no_mesmo_processo(local_db, audit, engine, coordinator):
    """Aborta um ciclo AINDA sem cleanup destrutivo ter rodado (aqui, uma
    espera com timeout — nenhum hook rodou, ver bloco 'timeout + cleanup')."""
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)
    coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.1)
    assert coordinator.is_draining() is True
    assert coordinator.get_draining_state()["cleanup_attempted"] is False

    aborted = coordinator.abort_shutdown(reason="usuario_cancelou_o_fechamento")
    assert aborted is True
    assert coordinator.is_draining() is False

    job = _create_job(audit, operation="RENDER")
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)
    updated = engine.advance(job.id)
    assert updated.status == JOB_READY


def test_abort_shutdown_recusa_depois_que_cleanup_ja_rodou(coordinator):
    coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.1)  # drena e roda cleanup (DRAINED)
    aborted = coordinator.abort_shutdown(reason="tarde_demais")
    assert aborted is False
    assert coordinator.is_draining() is True  # continua bloqueado


def test_abort_shutdown_sem_draining_ativo_e_no_op(coordinator):
    assert coordinator.abort_shutdown() is False


# ---------------------------------------------------------------------------
# 5/7 — ponto seguro / timeout nunca inventa sucesso ou falha
# ---------------------------------------------------------------------------


def test_job_processing_local_ja_iniciado_pode_terminar_no_ponto_seguro(local_db, audit, coordinator):
    job = _create_job(audit, operation="RENDER")
    audit.transition_job(job.id, JOB_PROCESSING)  # simula handler já em execução

    def finish_after_delay():
        time.sleep(0.15)
        audit.transition_job(job.id, JOB_READY, data={"reason": "handler_concluiu"})

    t = threading.Thread(target=finish_after_delay)
    t.start()
    report = coordinator.shutdown(reason="fechando", drain_timeout_seconds=2.0)
    t.join(timeout=5)

    assert report.drained_completely is True
    assert report.still_active_job_ids == ()
    assert local_db.get(Job, job.id).status == JOB_READY


def test_timeout_de_draining_nao_marca_job_como_sucesso_ou_falha_ficticia(local_db, audit, coordinator):
    job = _create_job(audit, operation="RENDER")
    audit.transition_job(job.id, JOB_PROCESSING)  # nunca termina

    report = coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.2)

    assert report.drained_completely is False
    assert report.still_active_job_ids == (job.id,)
    assert local_db.get(Job, job.id).status == JOB_PROCESSING  # nenhuma transição forçada

    state = coordinator.get_draining_state()
    assert state["drain_status"] == STATUS_TIMED_OUT
    assert state["still_active_job_ids"] == [job.id]
    assert state["cleanup_attempted"] is False


def test_timeout_de_draining_publishing_nunca_vira_failed_cancelled_ou_retry(local_db, audit, coordinator):
    job = _create_job(audit, operation="PUBLISH")
    audit.transition_job(job.id, JOB_PUBLISHING)

    report = coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.2)

    assert report.drained_completely is False
    assert local_db.get(Job, job.id).status == JOB_PUBLISHING


# ---------------------------------------------------------------------------
# Timeout + cleanup: nunca desmonta recurso em uso; força só sob decisão
# explícita do chamador
# ---------------------------------------------------------------------------


def test_timeout_nao_roda_hooks_de_limpeza(local_db, audit, coordinator):
    job = _create_job(audit, operation="RENDER")
    audit.transition_job(job.id, JOB_PROCESSING)

    calls = []
    coordinator.register_cleanup("recurso_em_uso", lambda: calls.append("rodou"))

    report = coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.15)

    assert report.requires_forced_action is True
    assert report.cleanup_results == ()
    assert calls == []  # cleanup NUNCA rodou sobre Job ainda ativo


def test_force_cleanup_after_timeout_roda_cleanup_sob_decisao_explicita_sem_tocar_job(local_db, audit, coordinator):
    job = _create_job(audit, operation="RENDER")
    audit.transition_job(job.id, JOB_PROCESSING)

    calls = []
    coordinator.register_cleanup("recurso", lambda: calls.append("rodou"))
    report = coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.15)
    assert report.requires_forced_action is True

    results = coordinator.force_cleanup_after_timeout()
    assert calls == ["rodou"]
    assert all(r.ok for r in results)
    assert local_db.get(Job, job.id).status == JOB_PROCESSING  # nunca tocado

    # idempotente: chamar de novo não roda o hook outra vez para este ciclo
    # -- mas devolve o resultado JÁ PERSISTIDO daquela tentativa, nunca um
    # relato vazio que apagaria a evidência do que realmente aconteceu.
    calls.clear()
    again = coordinator.force_cleanup_after_timeout()
    assert calls == []  # hook não rodou de novo
    assert len(again) == 1
    assert again[0].name == "recurso"
    assert again[0].ok is True

    # depois de cleanup forçado, abortar deixa de ser permitido
    assert coordinator.abort_shutdown() is False
    assert coordinator.is_draining() is True


def test_force_cleanup_after_timeout_sem_draining_ativo_levanta_erro(coordinator):
    with pytest.raises(ShutdownCoordinatorError):
        coordinator.force_cleanup_after_timeout()


# ---------------------------------------------------------------------------
# BLOQUEADOR DA QUARTA REVISÃO -- falha de cleanup nunca vira sucesso em
# chamada repetida; forced action já tomada nunca volta a ser "pendente"
# ---------------------------------------------------------------------------


def test_hook_que_falha_no_shutdown_normal_e_relatado_e_persistido_honestamente(coordinator):
    calls: list[str] = []

    def bom():
        calls.append("bom")

    def ruim():
        calls.append("ruim")
        raise RuntimeError("disco sem espaço")

    coordinator.register_cleanup("bom", bom)
    coordinator.register_cleanup("ruim", ruim)

    report1 = coordinator.shutdown(reason="com_falha", drain_timeout_seconds=0.1)
    assert report1.cleanup_attempted is True
    assert report1.cleanup_ok is False  # falha real, nunca escondida
    results_by_name = {r.name: r for r in report1.cleanup_results}
    assert results_by_name["ruim"].ok is False
    assert "disco sem espaço" in (results_by_name["ruim"].error or "")
    assert results_by_name["bom"].ok is True  # hook bom continua executando mesmo com outro falhando

    # segunda chamada do MESMO shutdown_id: hooks não rodam de novo...
    calls.clear()
    report2 = coordinator.shutdown(reason="de_novo", drain_timeout_seconds=0.1)
    assert calls == []  # nenhuma duplicação de efeitos de cleanup
    assert report2.shutdown_id == report1.shutdown_id
    # ...mas o relato continua honesto: a falha anterior nunca vira sucesso.
    # O texto ORIGINAL da exceção (report1, em memória, chamada que rodou o
    # hook agora) não é o mesmo persistido/reconstruído em report2 -- ver
    # "SEGURANÇA -- TRUNCAR ERRO NÃO É SANITIZAR SEGREDO": só error_type
    # (nome da classe) sobrevive à persistência, nunca str(exc).
    assert report2.cleanup_attempted is True
    assert report2.cleanup_ok is False
    results2_by_name = {r.name: r for r in report2.cleanup_results}
    assert results2_by_name["ruim"].ok is False
    assert results2_by_name["ruim"].error_type == "RuntimeError"
    assert "disco sem espaço" not in (results2_by_name["ruim"].error or "")
    assert results2_by_name["bom"].ok is True

    persisted = coordinator.get_draining_state()
    assert persisted["cleanup_attempted"] is True
    assert persisted["cleanup_ok"] is False


def test_falha_de_cleanup_sobrevive_a_fechar_e_reabrir_o_localdatabase():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db1 = _fresh_database(root, initialize=True)
        coordinator1 = ShutdownCoordinator(db1, drain_timeout_seconds=0.2)
        coordinator1.acquire_ownership()
        try:
            dummy_secret = "sk-super-secret-dummy-12345"

            def ruim():
                raise RuntimeError(f"Authorization: Bearer {dummy_secret}")

            coordinator1.register_cleanup("ruim", ruim)
            report1 = coordinator1.shutdown(reason="com_falha")
            assert report1.cleanup_ok is False
        finally:
            coordinator1.release_ownership()
        del coordinator1, db1

        # processo/instância NOVA, mesmo arquivo -- a falha continua VISÍVEL
        # (ok=False, error_type), mas o segredo original nunca foi
        # persistido em primeiro lugar.
        db2 = _fresh_database(root, initialize=False)
        coordinator2 = ShutdownCoordinator(db2)
        state = coordinator2.get_draining_state()
        assert state["cleanup_attempted"] is True
        assert state["cleanup_ok"] is False
        persisted_results = state["cleanup_results"]
        assert len(persisted_results) == 1
        assert persisted_results[0]["name"] == "ruim"
        assert persisted_results[0]["ok"] is False
        assert persisted_results[0]["error_type"] == "RuntimeError"
        assert "error" not in persisted_results[0]  # nunca a mensagem original
        assert dummy_secret not in json.dumps(persisted_results)
        assert dummy_secret not in json.dumps(state)

        # reconstruído em memória: error genérico, nunca o segredo.
        rebuilt = coordinator2.startup_reconcile  # apenas garante que o objeto existe; não chama
        del rebuilt
        deserialized = _deserialize_cleanup_results(persisted_results)
        assert deserialized[0].ok is False
        assert deserialized[0].error_type == "RuntimeError"
        assert dummy_secret not in (deserialized[0].error or "")


def test_apos_force_cleanup_after_timeout_shutdown_repetido_nao_pede_decisao_de_novo(coordinator, audit):
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)

    report1 = coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.1)
    assert report1.drain_status == STATUS_TIMED_OUT
    assert report1.requires_forced_action is True
    assert report1.cleanup_attempted is False

    calls: list[str] = []
    coordinator.register_cleanup("recurso", lambda: calls.append("rodou"))
    results = coordinator.force_cleanup_after_timeout()
    assert calls == ["rodou"]
    assert all(r.ok for r in results)

    # shutdown() chamado de novo para o MESMO ciclo: drain_status continua
    # TIMED_OUT (resultado histórico do drain em si, nunca reescrito), mas a
    # decisão forçada NÃO volta a ser relatada como pendente, e o hook não
    # roda de novo.
    report2 = coordinator.shutdown(reason="de_novo", drain_timeout_seconds=0.1)
    assert report2.drain_status == STATUS_TIMED_OUT
    assert report2.requires_forced_action is False
    assert report2.cleanup_attempted is True
    assert report2.cleanup_ok is True
    assert calls == ["rodou"]  # nenhuma duplicação


def test_forced_cleanup_com_hook_falhando_mantem_falha_visivel_em_chamadas_repetidas(coordinator, audit):
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)
    coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.1)

    calls: list[str] = []

    def ruim():
        calls.append("ruim")
        raise RuntimeError("falha no recurso forcado")

    coordinator.register_cleanup("ruim", ruim)

    results1 = coordinator.force_cleanup_after_timeout()
    assert len(calls) == 1
    assert results1[0].ok is False

    # idempotente: não reexecuta, e a falha continua visível -- nunca vira
    # sucesso só porque o hook não rodou de novo. O texto ORIGINAL da
    # exceção (results1, em memória, chamada que rodou o hook agora) não é
    # o mesmo persistido/reconstruído em results2 -- ver "SEGURANÇA --
    # TRUNCAR ERRO NÃO É SANITIZAR SEGREDO": só error_type sobrevive à
    # persistência, nunca str(exc).
    results2 = coordinator.force_cleanup_after_timeout()
    assert len(calls) == 1  # nenhuma duplicação de efeitos
    assert results2[0].ok is False
    assert results2[0].error_type == results1[0].error_type == "RuntimeError"
    assert results2[0].error == "cleanup hook failed (see application logs for detail)"
    assert "falha no recurso forcado" not in (results2[0].error or "")

    report = coordinator.shutdown(reason="mais_uma_vez", drain_timeout_seconds=0.1)
    assert report.cleanup_ok is False
    assert report.requires_forced_action is False


# ---------------------------------------------------------------------------
# 2/3 — corrida real claim vs início de draining (ordem de commit decide)
# ---------------------------------------------------------------------------


def _run_draining_vs_claim_race(*, iterations: int = 15) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_engine = _fresh_database(root, initialize=True)
        db_shutdown = _fresh_database(root, initialize=False)

        audit_engine = OperationalAuditLog(db_engine)
        coordinator_engine = ShutdownCoordinator(db_engine)
        coordinator_shutdown = ShutdownCoordinator(db_shutdown)
        # abort_shutdown() (usado abaixo para resetar entre iterações) exige
        # ownership desde a segunda revisão adversarial (Bloqueador 1).
        coordinator_shutdown.acquire_ownership()
        try:
            engine = JobEngine(db_engine, audit_log=audit_engine, shutdown_coordinator=coordinator_engine)
            engine.register_handler(
                "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
            )

            bad_interleavings = []
            never_committed = []

            for i in range(iterations):
                operation = f"RENDER-RACE-{i}"
                job = audit_engine.create_job(Job(operation=operation))

                barrier = threading.Barrier(2)
                claim_error: list[Exception] = []

                def do_claim():
                    barrier.wait(timeout=5)
                    try:
                        engine.advance(job.id)
                    except JobEngineError as exc:
                        claim_error.append(exc)

                def do_drain():
                    barrier.wait(timeout=5)
                    coordinator_shutdown._begin_draining(reason=f"race-{i}")

                t_claim = threading.Thread(target=do_claim)
                t_drain = threading.Thread(target=do_drain)
                t_claim.start()
                t_drain.start()
                t_claim.join(timeout=10)
                t_drain.join(timeout=10)

                # DRAINING é global e persistente (Bloqueador 1: nunca se
                # autolimpa) — resetamos entre iterações só para poder isolar a
                # ordem de commit desta rodada especificamente.
                coordinator_shutdown.abort_shutdown(reason=f"reset_race_{i}")

                drain_events = [
                    e
                    for e in db_engine.list_audit_events(entity_type="Lifecycle", event_type="LIFECYCLE_DRAINING_STARTED")
                    if e["data"].get("reason") == f"race-{i}"
                ]
                if not drain_events:
                    never_committed.append(i)
                    continue
                drain_sequence = drain_events[0]["sequence"]

                claim_events = db_engine.list_audit_events(
                    entity_type="Job", entity_id=job.id, event_type="JOB_STATE_CHANGED"
                )
                claimed_successfully = bool(claim_events)
                if claimed_successfully:
                    claim_sequence = claim_events[0]["sequence"]
                    if drain_sequence < claim_sequence:
                        bad_interleavings.append(
                            {"iteration": i, "drain_sequence": drain_sequence, "claim_sequence": claim_sequence}
                        )
        finally:
            coordinator_shutdown.release_ownership()

        assert not never_committed, f"draining deveria sempre commitar: {never_committed}"
        assert not bad_interleavings, (
            "reivindicação de Job sucedeu apesar de DRAINING já ter commitado antes dela: "
            f"{bad_interleavings}"
        )


def test_corrida_real_claim_vs_inicio_de_draining_ordem_de_commit_decide():
    _run_draining_vs_claim_race()


def test_duas_instancias_processos_respeitam_o_mesmo_draining():
    """Mesmo teste de corrida acima; o ponto central é a garantia entre
    PROCESSOS DIFERENTES: engine e coordinator de shutdown usam
    ``LocalDatabase`` próprios (nenhum estado em memória compartilhado), só
    o arquivo SQLite em comum — exatamente como ``test_control_manager.py``
    já comprova para pause/stop_after_current."""
    _run_draining_vs_claim_race(iterations=10)


# ---------------------------------------------------------------------------
# 8/9 — hooks de limpeza: isolamento e idempotência
# ---------------------------------------------------------------------------


def test_excecao_em_cleanup_de_um_recurso_nao_impede_cleanup_dos_outros(coordinator):
    calls = []

    def bom_1():
        calls.append("bom_1")

    def ruim():
        calls.append("ruim")
        raise RuntimeError("falha simulada de recurso")

    def bom_2():
        calls.append("bom_2")

    coordinator.register_cleanup("bom_1", bom_1)
    coordinator.register_cleanup("ruim", ruim)
    coordinator.register_cleanup("bom_2", bom_2)

    report = coordinator.shutdown(drain_timeout_seconds=0.1)

    assert set(calls) == {"bom_1", "ruim", "bom_2"}
    results_by_name = {r.name: r for r in report.cleanup_results}
    assert results_by_name["bom_1"].ok is True
    assert results_by_name["bom_2"].ok is True
    assert results_by_name["ruim"].ok is False
    assert "falha simulada" in results_by_name["ruim"].error


def test_cleanup_roda_em_ordem_reversa_de_registro(coordinator):
    order = []
    coordinator.register_cleanup("primeiro", lambda: order.append("primeiro"))
    coordinator.register_cleanup("segundo", lambda: order.append("segundo"))
    coordinator.register_cleanup("terceiro", lambda: order.append("terceiro"))

    coordinator.shutdown(drain_timeout_seconds=0.1)

    assert order == ["terceiro", "segundo", "primeiro"]


def test_cleanup_repetido_e_idempotente_apos_abort_e_novo_ciclo(coordinator):
    counter = {"n": 0}

    def incrementa_idempotente():
        counter["n"] = 1  # idempotente por construção: sempre define o mesmo valor

    coordinator.register_cleanup("idempotente", incrementa_idempotente)
    coordinator.shutdown(drain_timeout_seconds=0.1)
    coordinator.abort_shutdown()  # única forma de permitir um novo ciclo
    coordinator.shutdown(drain_timeout_seconds=0.1)  # roda de novo (novo ciclo, pós-abort)

    assert counter["n"] == 1


def test_subprocesso_real_e_terminado_deterministicamente_no_cleanup(coordinator):
    """Garante fechamento determinístico de subprocessos de teste — infra
    genérica de registro, sem antecipar ResourceManager (PROMPT 18)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def terminate_subprocess():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    coordinator.register_cleanup("subprocesso_teste", terminate_subprocess)
    report = coordinator.shutdown(drain_timeout_seconds=0.1)

    assert report.cleanup_ok is True
    assert proc.poll() is not None  # processo realmente morreu


# ---------------------------------------------------------------------------
# 10 — fechamento determinístico do banco/handles no shutdown limpo
# ---------------------------------------------------------------------------


def test_sqlite_handles_fechados_deterministicamente_no_shutdown_limpo(local_db, coordinator):
    coordinator.shutdown(drain_timeout_seconds=0.1)
    conn = sqlite3.connect(str(local_db.path), timeout=0.2)
    try:
        conn.execute("PRAGMA busy_timeout = 200")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SELECT COUNT(*) FROM jobs")
        conn.execute("COMMIT")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 11 — restart após shutdown limpo mantém estado consistente
# ---------------------------------------------------------------------------


def test_restart_apos_shutdown_limpo_mantem_estado_consistente():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db1 = _fresh_database(root, initialize=True)
        audit1 = OperationalAuditLog(db1)
        # process_session_id explícito simula duas sessões operacionais
        # DISTINTAS (ver "IMPORTANTE -- IDENTIDADE DE SESSÃO" na docstring
        # do módulo) sem o custo de um subprocess real -- a garantia
        # fim-a-fim com processo real já é coberta por
        # test_crash_durante_a_propria_sequencia_de_shutdown_e_recuperavel e
        # test_processo_real_dono_vivo_impede_....
        coordinator1 = ShutdownCoordinator(db1, process_session_id="sessao_processo_1")
        coordinator1.acquire_ownership()
        try:
            job = _create_job(audit1, operation="RENDER")

            report = coordinator1.shutdown(reason="fechando_normal", drain_timeout_seconds=0.1)
            assert report.drained_completely is True
        finally:
            coordinator1.release_ownership()  # processo 1 realmente termina -- SEMPRE libera o
            # handle do lock, mesmo se uma assertion acima falhar (Windows não
            # deixa o TemporaryDirectory apagar lifecycle_instance.lock com o
            # handle ainda aberto -- ver "CORREÇÃO WINDOWS" na docstring do módulo).
        del coordinator1, audit1, db1

        db2 = _fresh_database(root, initialize=False)
        coordinator2 = ShutdownCoordinator(db2, process_session_id="sessao_processo_2")
        assert coordinator2.is_draining() is True  # ainda bloqueado (Bloqueador 1)

        reloaded_job = db2.get(Job, job.id)
        assert reloaded_job.status == JOB_PENDING

        coordinator2.acquire_ownership()
        try:
            RecoveryManager(db2, audit_log=OperationalAuditLog(db2)).recover_at_startup()
            resolution = coordinator2.startup_reconcile()
            assert resolution.resolved is True
            assert coordinator2.is_draining() is False

            engine2 = JobEngine(db2, audit_log=OperationalAuditLog(db2), shutdown_coordinator=coordinator2)
            engine2.register_handler(
                "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
            )
            updated = engine2.advance(job.id)
            assert updated.status == JOB_READY
        finally:
            coordinator2.release_ownership()


# ---------------------------------------------------------------------------
# BLOQUEADOR 2 — ownership: startup_reconcile nunca apaga DRAINING de
# instância viva
# ---------------------------------------------------------------------------


def test_startup_reconcile_exige_ownership_provada(local_db):
    bare = ShutdownCoordinator(local_db)  # nunca chamou acquire_ownership()
    with pytest.raises(OwnershipNotHeldError):
        bare.startup_reconcile()


def test_instancia_b_nao_pode_adquirir_ownership_enquanto_a_esta_viva(local_db):
    coordinator_a = ShutdownCoordinator(local_db)
    coordinator_a.acquire_ownership()
    try:
        coordinator_b = ShutdownCoordinator(local_db)
        with pytest.raises(OwnershipAlreadyHeldError):
            coordinator_b.acquire_ownership(timeout_seconds=0.05)
    finally:
        coordinator_a.release_ownership()


def test_instancia_b_nao_resolve_draining_de_a_enquanto_a_esta_viva(local_db, audit):
    coordinator_a = ShutdownCoordinator(local_db)
    coordinator_a.acquire_ownership()
    coordinator_a._begin_draining(reason="a_esta_fechando")
    assert coordinator_a.is_draining() is True

    try:
        coordinator_b = ShutdownCoordinator(local_db)
        with pytest.raises(OwnershipAlreadyHeldError):
            coordinator_b.acquire_ownership(timeout_seconds=0.05)
        # B nunca prova ownership, então nunca pode nem tentar reconciliar
        with pytest.raises(OwnershipNotHeldError):
            coordinator_b.startup_reconcile()
    finally:
        coordinator_a.release_ownership()

    # DRAINING de A continua intacto e bloqueando
    assert coordinator_a.is_draining() is True
    engine = JobEngine(local_db, audit_log=audit, shutdown_coordinator=coordinator_a)
    job = _create_job(audit, operation="RENDER")
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)
    with pytest.raises(JobBlockedByShutdownError):
        engine.advance(job.id)


_OWNER_ALIVE_HOLDING_DRAINING_SCRIPT = """
import sys, time
from pathlib import Path
from _sistema.app_paths import build_app_paths
from _sistema.storage import LocalDatabase
from _sistema.shutdown_coordinator import ShutdownCoordinator

install_root, data_root, hold_seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
paths = build_app_paths(install_root=Path(install_root), data_root=Path(data_root))
db = LocalDatabase(paths=paths)
coordinator = ShutdownCoordinator(db)
coordinator.acquire_ownership()
coordinator._begin_draining(reason="instancia_real_viva")
print("READY", flush=True)
time.sleep(hold_seconds)
"""


def test_processo_real_separado_vivo_impede_reconciliacao_ate_morrer_de_verdade():
    """Requisito do Bloqueador 2: o teste de morte usa um PROCESSO REAL
    separado (não apenas um objeto Python destruído). Enquanto ele está
    vivo (segurando o lock de ownership de verdade), nenhuma outra
    instância pode reconciliar o DRAINING dele. Depois que ele morre de
    forma abrupta (kill), o próximo startup reconhece o lifecycle órfão e
    recupera de forma auditável."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_setup = _fresh_database(root, initialize=True)
        del db_setup

        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _OWNER_ALIVE_HOLDING_DRAINING_SCRIPT,
                str(root / "install"),
                str(root / "data"),
                "30",
            ],
            cwd=project_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready_line = proc.stdout.readline()
            assert ready_line.strip() == "READY", proc.stderr.read()

            db_b = _fresh_database(root, initialize=False)
            coordinator_b = ShutdownCoordinator(db_b)
            assert coordinator_b.is_draining() is True  # visível via settings normalmente
            with pytest.raises(OwnershipAlreadyHeldError):
                coordinator_b.acquire_ownership(timeout_seconds=0.3)
            with pytest.raises(OwnershipNotHeldError):
                coordinator_b.startup_reconcile()
            assert coordinator_b.is_draining() is True  # intacto
        finally:
            proc.kill()  # encerramento forçado real, nao apenas objeto destruido
            proc.wait(timeout=10)

        # depois que o dono morreu de verdade, o lock e liberado pelo SO e um
        # novo startup consegue reconhecer e recuperar o lifecycle orfao.
        db_c = _fresh_database(root, initialize=False)
        coordinator_c = ShutdownCoordinator(db_c)
        coordinator_c.acquire_ownership(timeout_seconds=5.0)
        try:
            RecoveryManager(db_c, audit_log=OperationalAuditLog(db_c)).recover_at_startup()
            resolution = coordinator_c.startup_reconcile()
            assert resolution.resolved is True
            assert resolution.shutdown_id is not None
            assert coordinator_c.is_draining() is False
        finally:
            coordinator_c.release_ownership()


def test_startup_reconcile_e_idempotente_sem_draining_ativo(coordinator):
    resolution = coordinator.startup_reconcile()
    assert resolution.resolved is False
    assert resolution.shutdown_id is None


# ---------------------------------------------------------------------------
# BLOQUEADOR 2 DA SEGUNDA REVISÃO — startup_reconcile() valida recovery
# completo (leitura pura do SQLite, sem acoplar RecoveryManager) antes de
# liberar admissão
# ---------------------------------------------------------------------------


def test_startup_reconcile_recusa_liberar_admissao_com_job_processing_abandonado_sem_recovery(local_db, audit):
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)  # abandonado por um "crash" anterior

    # sessão A cria o DRAINING e "morre" (release_ownership simula o fim de
    # verdade do processo anterior); sessão B (id distinto) é quem reconcilia
    # -- isolando a checagem de raw-abandoned-states da checagem de sessão
    # (Bloqueador 1 da terceira revisão), que teria recusado por outro
    # motivo se A e B fossem a mesma sessão.
    coordinator_a = ShutdownCoordinator(local_db, process_session_id="sessao_a")
    coordinator_a.acquire_ownership()
    try:
        coordinator_a._begin_draining(reason="crash_sem_recovery")
    finally:
        coordinator_a.release_ownership()

    coordinator_b = ShutdownCoordinator(local_db, process_session_id="sessao_b")
    coordinator_b.acquire_ownership()
    try:
        # deliberadamente NÃO roda RecoveryManager antes -- deve ser recusado.
        resolution = coordinator_b.startup_reconcile()

        assert resolution.resolved is False
        assert resolution.belongs_to_current_session is False
        assert resolution.blocked_reason is not None
        assert resolution.raw_abandoned_job_ids == (stuck.id,)
        assert coordinator_b.is_draining() is True  # admissão permanece bloqueada
        assert local_db.get(Job, stuck.id).status == JOB_PROCESSING  # ninguém tocou o Job
    finally:
        coordinator_b.release_ownership()


def test_startup_reconcile_recusa_liberar_admissao_com_job_publishing_abandonado_sem_recovery(local_db, audit):
    stuck = _create_job(audit, operation="PUBLISH")
    audit.transition_job(stuck.id, JOB_PUBLISHING)

    coordinator_a = ShutdownCoordinator(local_db, process_session_id="sessao_a")
    coordinator_a.acquire_ownership()
    try:
        coordinator_a._begin_draining(reason="crash_sem_recovery_publishing")
    finally:
        coordinator_a.release_ownership()

    coordinator_b = ShutdownCoordinator(local_db, process_session_id="sessao_b")
    coordinator_b.acquire_ownership()
    try:
        resolution = coordinator_b.startup_reconcile()

        assert resolution.resolved is False
        assert resolution.belongs_to_current_session is False
        assert resolution.raw_abandoned_job_ids == (stuck.id,)
        assert coordinator_b.is_draining() is True
    finally:
        coordinator_b.release_ownership()


def test_startup_reconcile_libera_admissao_depois_que_recovery_roda_corretamente(local_db, audit):
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)

    coordinator_a = ShutdownCoordinator(local_db, process_session_id="sessao_a")
    coordinator_a.acquire_ownership()
    try:
        coordinator_a._begin_draining(reason="crash_com_recovery")
    finally:
        coordinator_a.release_ownership()

    coordinator_b = ShutdownCoordinator(local_db, process_session_id="sessao_b")
    coordinator_b.acquire_ownership()
    try:
        # AGORA roda o RecoveryManager -- estado bruto deixa de existir.
        RecoveryManager(local_db, audit_log=audit).recover_at_startup()
        assert local_db.get(Job, stuck.id).status == JOB_FAILED  # destino conservador já aplicado

        resolution = coordinator_b.startup_reconcile()
        assert resolution.resolved is True
        assert resolution.belongs_to_current_session is False
        assert resolution.raw_abandoned_job_ids == ()
        assert coordinator_b.is_draining() is False
    finally:
        coordinator_b.release_ownership()


def test_startup_reconcile_nunca_e_bloqueado_por_recovering_remoto_ou_ambiguo(local_db, audit):
    """RECOVERING é o destino legítimo e intencionalmente não resolvido que
    o próprio RecoveryManager atribui a trabalho remoto/ambíguo -- jamais
    pode, por si só, impedir startup_reconcile() de liberar admissão (isso
    recriaria o deadlock que o RecoveryManager foi desenhado para evitar)."""
    remote_job = _create_job(audit, operation="PUBLISH")
    audit.transition_job(remote_job.id, JOB_PUBLISHING)
    audit.transition_job(remote_job.id, JOB_UNKNOWN)
    audit.transition_job(remote_job.id, JOB_RECOVERING)  # origem remota/ambígua, resta assim

    coordinator_a = ShutdownCoordinator(local_db, process_session_id="sessao_a")
    coordinator_a.acquire_ownership()
    try:
        coordinator_a._begin_draining(reason="recovering_remoto")
    finally:
        coordinator_a.release_ownership()

    coordinator_b = ShutdownCoordinator(local_db, process_session_id="sessao_b")
    coordinator_b.acquire_ownership()
    try:
        resolution = coordinator_b.startup_reconcile()

        assert resolution.resolved is True
        assert resolution.belongs_to_current_session is False
        assert resolution.raw_abandoned_job_ids == ()
        assert coordinator_b.is_draining() is False
        assert local_db.get(Job, remote_job.id).status == JOB_RECOVERING  # nunca tocado/forçado
    finally:
        coordinator_b.release_ownership()


# ---------------------------------------------------------------------------
# BLOQUEADOR 1 DA TERCEIRA REVISÃO -- startup_reconcile() nunca desfaz o
# próprio shutdown do processo que o criou
# ---------------------------------------------------------------------------


def test_startup_reconcile_recusa_lifecycle_da_propria_sessao_mesmo_apos_shutdown_drenado(local_db, audit):
    """Reprodução exata do Bloqueador 1 da terceira revisão: o MESMO
    coordinator (mesma sessão), sem nunca ter encerrado, faz shutdown()
    completo (com cleanup destrutivo real) e então chama
    startup_reconcile() -- deve ser recusado, sem reexecutar cleanup, sem
    liberar admissão, sem mexer no lifecycle."""
    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=0.2)
    coordinator.acquire_ownership()
    try:
        cleanup_calls: list[str] = []
        coordinator.register_cleanup("recurso", lambda: cleanup_calls.append("rodou"))

        report = coordinator.shutdown(reason="fechando_no_mesmo_processo")
        assert report.drained_completely is True
        assert cleanup_calls == ["rodou"]

        resolution = coordinator.startup_reconcile()

        assert resolution.resolved is False
        assert resolution.belongs_to_current_session is True
        assert resolution.blocked_reason is not None
        assert coordinator.is_draining() is True  # admission_blocked continua True
        assert cleanup_calls == ["rodou"]  # cleanup NÃO rodou de novo

        # novo Job continua bloqueado -- nada foi reaberto por baixo dos panos.
        job = _create_job(audit, operation="RENDER")
        engine = JobEngine(local_db, audit_log=audit, shutdown_coordinator=coordinator)
        engine.register_handler(
            "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
        )
        with pytest.raises(JobBlockedByShutdownError):
            engine.advance(job.id)
    finally:
        coordinator.release_ownership()


def test_release_ownership_bloqueia_ate_operacao_critica_em_andamento_terminar(local_db):
    """Requisito determinístico do BLOQUEADOR 2 DA TERCEIRA REVISÃO: prova,
    com sincronização explícita (Event, mesmo espírito de Barrier), que
    ``release_ownership()`` NUNCA completa enquanto uma operação crítica de
    lifecycle (aqui, ``shutdown()`` executando um hook de cleanup lento)
    ainda está em andamento -- e que, por consequência, NENHUM outro
    coordinator consegue adquirir ownership nesse meio-tempo. A garantia é
    CENTRALIZADA (mesmo ``_shutdown_lock`` usado por ``shutdown()``,
    ``abort_shutdown()``, ``force_cleanup_after_timeout()`` e
    ``startup_reconcile()``, e agora também por ``release_ownership()``) --
    provar isso para ``shutdown()`` (o único método com um ponto de
    lentidão controlável pelo teste, via hook de cleanup) cobre o mecanismo
    para os quatro."""
    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=2.0, poll_interval_seconds=0.02)
    coordinator.acquire_ownership()

    entered_critical_section = threading.Event()
    allow_critical_section_to_finish = threading.Event()

    def hook_lento_dentro_da_secao_critica():
        entered_critical_section.set()
        allow_critical_section_to_finish.wait(timeout=5)

    coordinator.register_cleanup("hook_lento", hook_lento_dentro_da_secao_critica)

    shutdown_finished = threading.Event()

    def do_shutdown():
        coordinator.shutdown(reason="secao_critica_longa", drain_timeout_seconds=2.0)
        shutdown_finished.set()

    t_shutdown = threading.Thread(target=do_shutdown)
    t_release: threading.Thread | None = None
    release_finished = threading.Event()
    other: ShutdownCoordinator | None = None
    try:
        t_shutdown.start()
        assert entered_critical_section.wait(timeout=5)  # shutdown() está dentro do _shutdown_lock agora

        def do_release():
            coordinator.release_ownership()
            release_finished.set()

        t_release = threading.Thread(target=do_release)
        t_release.start()

        # Enquanto a seção crítica não terminar, release_ownership() fica
        # bloqueado esperando o mesmo lock -- o lock de SO continua retido.
        time.sleep(0.3)
        assert not release_finished.is_set()
        assert coordinator.owns_instance is True

        # E, por consequência direta, nenhum outro coordinator consegue
        # adquirir ownership nesse meio-tempo -- não há janela em que A
        # continua "dono" para o lock, mas outro processo já conseguiu adquirir.
        other = ShutdownCoordinator(local_db)
        with pytest.raises(OwnershipAlreadyHeldError):
            other.acquire_ownership(timeout_seconds=0.1)

        # libera o hook -- shutdown() termina, e SÓ ENTÃO release_ownership()
        # consegue completar.
        allow_critical_section_to_finish.set()
        t_shutdown.join(timeout=5)
        assert shutdown_finished.is_set()
        t_release.join(timeout=5)
        assert release_finished.is_set()
        assert coordinator.owns_instance is False

        # agora sim, um processo novo consegue adquirir de verdade.
        other.acquire_ownership(timeout_seconds=2.0)
        other.release_ownership()
        other = None
    finally:
        # Windows não deixa o TemporaryDirectory (via fixture local_db)
        # apagar lifecycle_instance.lock com um handle ainda aberto -- se
        # qualquer assertion acima falhar no meio da coreografia de
        # threads, garantimos deterministicamente que NENHUM handle fica
        # pendurado: liberamos a seção crítica (nunca deixando um hook
        # travado para sempre), esperamos as threads terminarem, e
        # liberamos qualquer ownership que ainda esteja em aberto.
        allow_critical_section_to_finish.set()
        t_shutdown.join(timeout=5)
        if t_release is not None:
            t_release.join(timeout=5)
        if coordinator.owns_instance:
            coordinator.release_ownership()
        if other is not None and other.owns_instance:
            other.release_ownership()


def test_startup_reconcile_processo_novo_de_verdade_consegue_resolver_apos_sessao_anterior_morrer(local_db, audit):
    """Complemento real (não simulado por override de teste): depois que a
    sessão anterior de fato libera ownership (representando o fim real do
    processo), uma sessão nova consegue reconciliar normalmente -- a recusa
    acima é exclusiva de quem criou o lifecycle, nunca um bloqueio
    permanente."""
    # process_session_id explícito em ambos: mesmo dentro do mesmo processo
    # de teste, simula duas sessões DISTINTAS -- ver "IMPORTANTE --
    # IDENTIDADE DE SESSÃO" na docstring do módulo (sem override, os dois
    # objetos compartilhariam a mesma sessão real do processo de teste).
    coordinator_a = ShutdownCoordinator(local_db, drain_timeout_seconds=0.2, process_session_id="sessao_a")
    coordinator_a.acquire_ownership()
    try:
        report = coordinator_a.shutdown(reason="fechando_a")
        assert report.drained_completely is True
    finally:
        coordinator_a.release_ownership()  # processo A realmente termina

    coordinator_b = ShutdownCoordinator(local_db, process_session_id="sessao_b")
    coordinator_b.acquire_ownership()
    try:
        resolution = coordinator_b.startup_reconcile()

        assert resolution.resolved is True
        assert resolution.belongs_to_current_session is False
        assert coordinator_b.is_draining() is False
    finally:
        coordinator_b.release_ownership()


# ---------------------------------------------------------------------------
# Subprocessos: encerramento forçado real (os._exit), nunca só Exception
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_CRASH_DURING_LOCAL_WORK_SCRIPT = """
import os, sys
from pathlib import Path
from _sistema.app_paths import build_app_paths
from _sistema.storage import LocalDatabase, OperationalAuditLog
from _sistema.job_engine import JobEngine, JobStepResult
from _sistema.domain import JOB_PROCESSING, JOB_READY

install_root, data_root, job_id, operation = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
paths = build_app_paths(install_root=Path(install_root), data_root=Path(data_root))
db = LocalDatabase(paths=paths)
audit = OperationalAuditLog(db)
engine = JobEngine(db, audit_log=audit)


def handler(job):
    audit.record_checkpoint(job.id, "IMPORTED")
    os._exit(1)
    return JobStepResult(target_status=JOB_READY)  # nunca alcancado


engine.register_handler(operation, handler, claims_status=JOB_PROCESSING)
engine.advance(job_id)
"""

_CRASH_DURING_PUBLISHING_SCRIPT = """
import os, sys
from pathlib import Path
from _sistema.app_paths import build_app_paths
from _sistema.storage import LocalDatabase, OperationalAuditLog
from _sistema.job_engine import JobEngine, JobStepResult
from _sistema.domain import JOB_PUBLISHING, JOB_PUBLISHED

install_root, data_root, job_id, operation = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
paths = build_app_paths(install_root=Path(install_root), data_root=Path(data_root))
db = LocalDatabase(paths=paths)
audit = OperationalAuditLog(db)
engine = JobEngine(db, audit_log=audit)


def handler(job):
    audit.record_checkpoint(job.id, "UPLOAD_STARTED")
    os._exit(1)
    return JobStepResult(target_status=JOB_PUBLISHED)  # nunca alcancado


engine.register_handler(operation, handler, claims_status=JOB_PUBLISHING)
engine.advance(job_id)
"""

_CRASH_DURING_SHUTDOWN_SEQUENCE_SCRIPT = """
import sys, os
from pathlib import Path
from _sistema.app_paths import build_app_paths
from _sistema.storage import LocalDatabase
from _sistema.shutdown_coordinator import ShutdownCoordinator

install_root, data_root = sys.argv[1], sys.argv[2]
paths = build_app_paths(install_root=Path(install_root), data_root=Path(data_root))
db = LocalDatabase(paths=paths)
coordinator = ShutdownCoordinator(db)
coordinator.acquire_ownership()
coordinator._begin_draining(reason="crash_no_meio_do_shutdown")
os._exit(1)
"""


def _run_subprocess(code: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-c", code, *args]
    return subprocess.run(cmd, cwd=_PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(
    "operation",
    ["PROCESS_FAKE", "TRANSCRIBE_FAKE", "ANALYZE_FAKE", "RENDER_FAKE"],
)
def test_forced_termination_durante_operacao_local_representativa(operation):
    """Requisitos #13-17: encerramento forcado real (processo separado,
    os._exit) durante operacoes representativas de processamento,
    transcricao, analise e renderizacao. Nenhum Engine futuro e
    implementado aqui -- 'operation' e apenas um handler fake registrado
    no JobEngine, como orientado pelo roadmap."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = _fresh_database(root, initialize=True)
        audit = OperationalAuditLog(db)
        job = _create_job(audit, operation=operation)

        result = _run_subprocess(
            _CRASH_DURING_LOCAL_WORK_SCRIPT,
            str(root / "install"),
            str(root / "data"),
            job.id,
            operation,
        )
        assert result.returncode != 0, result.stdout + result.stderr

        db_after = _fresh_database(root, initialize=False)
        stuck = db_after.get(Job, job.id)
        assert stuck.status == JOB_PROCESSING  # nao foi finalizado silenciosamente

        recovery = RecoveryManager(db_after)
        report = recovery.recover_at_startup()
        assert report.failed_job_ids == ()
        recovered = db_after.get(Job, job.id)
        assert recovered.status == JOB_FAILED  # sem artifact_validator: destino conservador
        assert report.results[0].action == ACTION_MARKED_FAILED_FOR_MANUAL_RETRY


def test_forced_termination_durante_publishing_nao_produz_retry_ou_repost_automatico():
    """Requisito #18: encerramento forcado durante PUBLISHING nunca reposta
    nem tenta retry automaticamente -- o Job so pode seguir para RECOVERING,
    aguardando reconciliacao real por um Connector futuro."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = _fresh_database(root, initialize=True)
        audit = OperationalAuditLog(db)
        job = _create_job(audit, operation="PUBLISH_FAKE")

        result = _run_subprocess(
            _CRASH_DURING_PUBLISHING_SCRIPT,
            str(root / "install"),
            str(root / "data"),
            job.id,
            "PUBLISH_FAKE",
        )
        assert result.returncode != 0, result.stdout + result.stderr

        db_after = _fresh_database(root, initialize=False)
        stuck = db_after.get(Job, job.id)
        assert stuck.status == JOB_PUBLISHING

        recovery = RecoveryManager(db_after)
        report = recovery.recover_at_startup()
        recovered = db_after.get(Job, job.id)
        assert recovered.status == JOB_RECOVERING  # nunca RETRY/PUBLISHING/PUBLISHED direto
        assert report.results[0].action == ACTION_FLAGGED_FOR_RECONCILIATION


def test_unknown_continua_exigindo_recovering_mesmo_apos_shutdown_infra():
    """Requisito #19: UNKNOWN nunca é reprocessado automaticamente pelo
    ShutdownCoordinator nem vira alvo de repost -- continua exigindo
    RECOVERING através do caminho já existente."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = _fresh_database(root, initialize=True)
        audit = OperationalAuditLog(db)
        coordinator = ShutdownCoordinator(db)
        coordinator.acquire_ownership()
        try:
            job = _create_job(audit, operation="PUBLISH_FAKE")
            audit.transition_job(job.id, JOB_PUBLISHING)
            audit.transition_job(job.id, JOB_UNKNOWN)

            coordinator.shutdown(reason="fechando", drain_timeout_seconds=0.1)
            assert db.get(Job, job.id).status == JOB_UNKNOWN  # nunca tocado

            recovery = RecoveryManager(db)
            recovery.recover_at_startup()
            assert db.get(Job, job.id).status == JOB_RECOVERING
        finally:
            coordinator.release_ownership()


def test_forced_termination_local_via_processo_separado_e_recuperado_por_recovery_manager():
    """Requisito #12: restart apos encerramento forcado recupera Job local
    via RecoveryManager (sem nenhum shutdown() gracioso ter sido chamado)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = _fresh_database(root, initialize=True)
        audit = OperationalAuditLog(db)
        job = _create_job(audit, operation="PROCESS_FAKE")

        result = _run_subprocess(
            _CRASH_DURING_LOCAL_WORK_SCRIPT,
            str(root / "install"),
            str(root / "data"),
            job.id,
            "PROCESS_FAKE",
        )
        assert result.returncode != 0

        db_after = _fresh_database(root, initialize=False)
        recovery = RecoveryManager(db_after)
        report = recovery.recover_at_startup()
        assert db_after.get(Job, job.id).status == JOB_FAILED
        assert report.results[0].previous_status == JOB_PROCESSING


# ---------------------------------------------------------------------------
# 20/21 — DRAINING abandonado não bloqueia o produto para sempre
# ---------------------------------------------------------------------------


def test_crash_durante_a_propria_sequencia_de_shutdown_e_recuperavel():
    """Requisito #21: o processo morre logo depois de persistir o INICIO do
    DRAINING (e adquirir ownership), antes de completar shutdown(). Na
    próxima abertura, depois de provar ownership (o antigo dono já morreu
    de verdade) e do RecoveryManager, ``startup_reconcile()`` resolve isso
    com segurança e o produto volta a aceitar trabalho novo."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = _fresh_database(root, initialize=True)

        result = _run_subprocess(
            _CRASH_DURING_SHUTDOWN_SEQUENCE_SCRIPT,
            str(root / "install"),
            str(root / "data"),
        )
        assert result.returncode != 0, result.stdout + result.stderr

        db_after = _fresh_database(root, initialize=False)
        coordinator_after = ShutdownCoordinator(db_after)
        assert coordinator_after.is_draining() is True  # flag ficou ativo, órfão

        # ORDEM: ownership -> RecoveryManager -> startup_reconcile().
        coordinator_after.acquire_ownership(timeout_seconds=5.0)
        try:
            audit_after = OperationalAuditLog(db_after)
            RecoveryManager(db_after, audit_log=audit_after).recover_at_startup()

            resolution = coordinator_after.startup_reconcile()
            assert resolution.resolved is True
            assert resolution.previous_status == "IN_PROGRESS"
            assert coordinator_after.is_draining() is False

            engine_after = JobEngine(db_after, audit_log=audit_after, shutdown_coordinator=coordinator_after)
            job = _create_job(audit_after, operation="RENDER")
            engine_after.register_handler(
                "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
            )
            updated = engine_after.advance(job.id)
            assert updated.status == JOB_READY
        finally:
            coordinator_after.release_ownership()


def test_draining_persistido_apos_crash_nao_bloqueia_produto_para_sempre_sem_recovery_previo():
    """Requisito #20: startup_reconcile() só deve ser chamado DEPOIS do
    RecoveryManager -- este teste comprova que a regra de resolução em si
    (chamada na ordem certa) sempre libera o produto, mesmo quando havia
    Jobs abandonados de verdade concorrendo pelo mesmo estado de crash."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = _fresh_database(root, initialize=True)
        audit = OperationalAuditLog(db)

        stuck_job = _create_job(audit, operation="RENDER")
        audit.transition_job(stuck_job.id, JOB_PROCESSING)

        coordinator_a = ShutdownCoordinator(db, process_session_id="sessao_a")
        coordinator_a.acquire_ownership()
        try:
            coordinator_a._begin_draining(reason="crash_concorrente")
            assert coordinator_a.is_draining() is True
        finally:
            coordinator_a.release_ownership()  # processo anterior realmente termina

        coordinator = ShutdownCoordinator(db, process_session_id="sessao_b")
        coordinator.acquire_ownership()
        try:
            RecoveryManager(db, audit_log=audit).recover_at_startup()
            assert db.get(Job, stuck_job.id).status == JOB_FAILED  # já resolvido antes do reconcile

            resolution = coordinator.startup_reconcile()
            assert resolution.resolved is True
            assert coordinator.is_draining() is False

            engine = JobEngine(db, audit_log=audit, shutdown_coordinator=coordinator)
            new_job = _create_job(audit, operation="RENDER")
            engine.register_handler(
                "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
            )
            assert engine.advance(new_job.id).status == JOB_READY
        finally:
            coordinator.release_ownership()


# ---------------------------------------------------------------------------
# 22 — dois pedidos simultâneos de shutdown são seguros/idempotentes
# ---------------------------------------------------------------------------


def test_dois_pedidos_simultaneos_de_shutdown_mesmo_processo_sao_seguros(coordinator):
    calls = {"n": 0}

    def cleanup():
        calls["n"] += 1

    coordinator.register_cleanup("recurso", cleanup)

    reports: list = []
    errors: list[Exception] = []

    def do_shutdown():
        try:
            reports.append(coordinator.shutdown(reason="fechar_duas_vezes", drain_timeout_seconds=0.5))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=do_shutdown)
    t2 = threading.Thread(target=do_shutdown)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, errors
    assert len(reports) == 2
    assert reports[0].shutdown_id == reports[1].shutdown_id  # mesmo ciclo, nunca dois
    assert all(report.drained_completely for report in reports)
    # MAJOR da segunda revisão: mesmo sob duas chamadas CONCORRENTES no
    # mesmo ciclo, o cleanup destrutivo roda EXATAMENTE uma vez -- nunca
    # dependemos apenas do hook ser reentrante.
    assert calls["n"] == 1
    assert coordinator.is_draining() is True  # continua bloqueado (Bloqueador 1)


def test_processo_real_dono_vivo_impede_nao_dono_de_abortar_forcar_ou_conduzir_shutdown():
    """Requisito do BLOQUEADOR 1 DA SEGUNDA REVISÃO, com DOIS PROCESSOS
    REAIS: A adquire ownership e inicia DRAINING de verdade e continua vivo
    (segurando o lock de SO). B (sem ownership -- sua própria
    ``acquire_ownership()`` falharia com ``OwnershipAlreadyHeldError``, como
    já provado em ``test_instancia_b_nao_pode_adquirir_ownership_...``)
    tenta ``abort_shutdown()``, ``force_cleanup_after_timeout()`` e
    ``shutdown()`` -- todos devem ser recusados com ``OwnershipNotHeldError``,
    sem alterar UM ÚNICO byte do lifecycle persistido por A, sem rodar
    nenhum hook de cleanup, e com o ``JobEngine`` continuando bloqueado.
    Substitui o teste anterior (agora com premissa arquiteturalmente
    inválida) que deixava dois coordinators diferentes chamarem
    ``shutdown()`` livremente -- desde a introdução de ownership como
    requisito de todos os mutadores, só o dono comprovado pode conduzir um
    ciclo de shutdown."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_setup = _fresh_database(root, initialize=True)
        del db_setup

        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _OWNER_ALIVE_HOLDING_DRAINING_SCRIPT,
                str(root / "install"),
                str(root / "data"),
                "30",
            ],
            cwd=project_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready_line = proc.stdout.readline()
            assert ready_line.strip() == "READY", proc.stderr.read()

            db_b = _fresh_database(root, initialize=False)
            coordinator_b = ShutdownCoordinator(db_b)
            assert coordinator_b.owns_instance is False

            b_cleanup_calls: list[str] = []
            coordinator_b.register_cleanup("hook_de_b", lambda: b_cleanup_calls.append("rodou"))

            with pytest.raises(OwnershipNotHeldError):
                coordinator_b.abort_shutdown(reason="b_tentando_abortar_de_a")
            with pytest.raises(OwnershipNotHeldError):
                coordinator_b.force_cleanup_after_timeout()
            with pytest.raises(OwnershipNotHeldError):
                coordinator_b.shutdown(reason="b_tentando_conduzir_de_a", drain_timeout_seconds=0.1)

            assert b_cleanup_calls == []  # hook de B nunca rodou

            state_after_b = coordinator_b.get_draining_state()
            assert state_after_b["admission_blocked"] is True
            assert state_after_b["drain_status"] == STATUS_IN_PROGRESS
            assert state_after_b["cleanup_attempted"] is False
            assert coordinator_b.is_draining() is True

            audit_b = OperationalAuditLog(db_b)
            engine_b = JobEngine(db_b, audit_log=audit_b, shutdown_coordinator=coordinator_b)
            job = _create_job(audit_b, operation="RENDER")
            engine_b.register_handler(
                "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
            )
            with pytest.raises(JobBlockedByShutdownError):
                engine_b.advance(job.id)
        finally:
            proc.kill()  # encerramento forçado real do dono A
            proc.wait(timeout=10)

        # o dono legítimo (um processo novo, depois que A morreu de
        # verdade) continua podendo operar normalmente -- ownership nunca
        # foi um obstáculo para quem de fato a prova.
        db_c = _fresh_database(root, initialize=False)
        coordinator_c = ShutdownCoordinator(db_c)
        coordinator_c.acquire_ownership(timeout_seconds=5.0)
        try:
            RecoveryManager(db_c, audit_log=OperationalAuditLog(db_c)).recover_at_startup()
            resolution = coordinator_c.startup_reconcile()
            assert resolution.resolved is True
            assert coordinator_c.is_draining() is False
        finally:
            coordinator_c.release_ownership()


def test_shutdown_chamado_duas_vezes_sequencialmente_reusa_o_mesmo_ciclo(coordinator):
    calls: list[str] = []
    coordinator.register_cleanup("recurso_unico", lambda: calls.append("rodou"))

    report1 = coordinator.shutdown(reason="primeira", drain_timeout_seconds=0.1)
    report2 = coordinator.shutdown(reason="segunda", drain_timeout_seconds=0.1)

    assert report1.shutdown_id == report2.shutdown_id  # nenhum novo ciclo nasce sozinho
    assert report2.already_in_progress is True
    assert report1.drained_completely is True
    assert report2.drained_completely is True
    # MAJOR da segunda revisão: cleanup roda EXATAMENTE uma vez para o
    # mesmo shutdown_id -- a segunda chamada não reexecuta o hook.
    assert calls == ["rodou"]
    assert report1.cleanup_results != ()
    # report2 nunca reexecuta o hook, mas relata o resultado JÁ PERSISTIDO
    # da tentativa de report1 -- nunca um relato vazio que apagaria a
    # evidência de sucesso/falha anterior (Bloqueador da quarta revisão).
    assert report2.cleanup_attempted is True
    assert report2.cleanup_results == report1.cleanup_results
    assert report2.cleanup_ok is True


# ---------------------------------------------------------------------------
# Validação básica de contrato/API
# ---------------------------------------------------------------------------


def test_coordinator_rejeita_timeout_negativo(local_db):
    with pytest.raises(ValueError):
        ShutdownCoordinator(local_db, drain_timeout_seconds=-1.0)


def test_shutdown_rejeita_override_de_timeout_negativo(coordinator):
    with pytest.raises(ValueError):
        coordinator.shutdown(drain_timeout_seconds=-5.0)


def test_default_drain_timeout_e_positivo():
    assert DEFAULT_DRAIN_TIMEOUT_SECONDS > 0


def test_register_cleanup_rejeita_callback_nao_chamavel(coordinator):
    with pytest.raises(TypeError):
        coordinator.register_cleanup("x", "nao_e_chamavel")  # type: ignore[arg-type]


def test_register_cleanup_rejeita_nome_vazio(coordinator):
    with pytest.raises(ValueError):
        coordinator.register_cleanup("   ", lambda: None)


def test_unregister_cleanup_e_seguro_mesmo_sem_registro_previo(coordinator):
    coordinator.unregister_cleanup("nao_existe")  # não levanta


def test_acquire_ownership_e_idempotente_na_mesma_instancia(coordinator):
    coordinator.acquire_ownership()
    coordinator.acquire_ownership()  # não levanta, não deadlocka
    assert coordinator.owns_instance is True
    coordinator.release_ownership()
    assert coordinator.owns_instance is False


def test_release_ownership_sem_ter_adquirido_e_seguro(local_db):
    bare = ShutdownCoordinator(local_db)  # nunca chamou acquire_ownership()
    bare.release_ownership()  # não levanta
    assert bare.owns_instance is False


def test_hold_ownership_context_manager_libera_ao_sair(local_db):
    coordinator = ShutdownCoordinator(local_db)
    with coordinator.hold_ownership():
        assert coordinator.owns_instance is True
    assert coordinator.owns_instance is False


def test_job_engine_sem_shutdown_coordinator_comporta_se_exatamente_como_antes(local_db, audit):
    """Sem shutdown_coordinator (padrão), nenhum Job é bloqueado por
    DRAINING -- idêntico ao comportamento anterior a este Prompt."""
    engine = JobEngine(local_db, audit_log=audit)
    job = _create_job(audit, operation="RENDER")
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)
    updated = engine.advance(job.id)
    assert updated.status == JOB_READY


# ---------------------------------------------------------------------------
# BLOQUEADOR DA QUINTA REVISÃO -- sessão nova nunca assume/modifica lifecycle
# de sessão operacional ANTERIOR sem passar por startup_reconcile()
# ---------------------------------------------------------------------------

_STUCK_JOB_AND_DRAINING_SCRIPT = """
import sys, os
from pathlib import Path
from _sistema.app_paths import build_app_paths
from _sistema.storage import LocalDatabase, OperationalAuditLog
from _sistema.domain import Job, JOB_PROCESSING
from _sistema.shutdown_coordinator import ShutdownCoordinator

install_root, data_root = sys.argv[1], sys.argv[2]
paths = build_app_paths(install_root=Path(install_root), data_root=Path(data_root))
db = LocalDatabase(paths=paths)
audit = OperationalAuditLog(db)
job = audit.create_job(Job(operation="RENDER"))
audit.transition_job(job.id, JOB_PROCESSING)

coordinator = ShutdownCoordinator(db)
coordinator.acquire_ownership()
coordinator._begin_draining(reason="sessao_a_comecou_a_fechar_e_morreu")
print(job.id, flush=True)
os._exit(1)  # morte abrupta real -- sem liberar Job, sem completar shutdown
"""


def test_sessao_nova_nao_assume_lifecycle_de_sessao_anterior_sem_reconciliar():
    """Reprodução exata do Bloqueador da quinta revisão, com PROCESSO REAL
    separado: processo A adquire ownership, cria um Job em PROCESSING, entra
    em DRAINING e morre de forma abrupta (``os._exit``) sem completar o
    shutdown nem liberar o Job. Processo B (esta sessão de teste, com
    ``process_session_id`` real e distinto de A) prova ownership do lock de
    SO -- mas isso NUNCA basta para assumir o lifecycle de A: só
    ``startup_reconcile()``, depois de ``RecoveryManager.recover_at_startup()``,
    pode resolvê-lo (ver "BLOQUEADOR DA QUINTA REVISÃO" na docstring do
    módulo)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_setup = _fresh_database(root, initialize=True)
        del db_setup

        result = _run_subprocess(
            _STUCK_JOB_AND_DRAINING_SCRIPT,
            str(root / "install"),
            str(root / "data"),
        )
        assert result.returncode != 0, result.stdout + result.stderr
        stuck_job_id = result.stdout.strip().splitlines()[-1]

        db_b = _fresh_database(root, initialize=False)
        audit_b = OperationalAuditLog(db_b)
        coordinator_b = ShutdownCoordinator(db_b)

        # B prova ownership do lock de SO normalmente -- o lock foi
        # liberado pelo SO quando A morreu de verdade.
        coordinator_b.acquire_ownership(timeout_seconds=5.0)
        try:
            assert coordinator_b.is_draining() is True  # lifecycle de A, órfão, ainda ativo

            state_before = coordinator_b.get_draining_state()
            assert state_before["owner_session_id"] != coordinator_b.process_session_id

            calls: list[str] = []
            coordinator_b.register_cleanup("hook_da_sessao_b", lambda: calls.append("rodou"))

            # (3) ANTES de recovery/reconcile: as três operações mutantes são
            # recusadas com o erro dedicado -- nunca assumem/abortam/completam o
            # ciclo de A, nunca rodam os hooks de B sobre o shutdown_id de A.
            with pytest.raises(PreviousSessionLifecycleRequiresReconcileError):
                coordinator_b.shutdown(reason="sessao_b_tentando_fechar")
            with pytest.raises(PreviousSessionLifecycleRequiresReconcileError):
                coordinator_b.abort_shutdown()
            with pytest.raises(PreviousSessionLifecycleRequiresReconcileError):
                coordinator_b.force_cleanup_after_timeout()

            assert calls == []  # hooks de B nunca rodaram
            assert coordinator_b.is_draining() is True  # admissão continua bloqueada
            state_untouched = coordinator_b.get_draining_state()
            assert state_untouched == state_before  # nada foi alterado por nenhuma tentativa
            assert db_b.get(Job, stuck_job_id).status == JOB_PROCESSING  # Job de A intocado

            # JobEngine desta sessão continua bloqueado pelo DRAINING órfão de A.
            engine_b = JobEngine(db_b, audit_log=audit_b, shutdown_coordinator=coordinator_b)
            new_job = _create_job(audit_b, operation="RENDER")
            engine_b.register_handler(
                "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
            )
            with pytest.raises(JobBlockedByShutdownError):
                engine_b.advance(new_job.id)

            # (4) RecoveryManager roda; (5) startup_reconcile resolve.
            RecoveryManager(db_b, audit_log=audit_b).recover_at_startup()
            assert db_b.get(Job, stuck_job_id).status == JOB_FAILED  # destino conservador

            resolution = coordinator_b.startup_reconcile()
            assert resolution.resolved is True
            assert coordinator_b.is_draining() is False

            # (6) B opera normalmente agora.
            updated = engine_b.advance(new_job.id)
            assert updated.status == JOB_READY

            # (7) um shutdown posterior de B cria um shutdown_id NOVO, com
            # owner_session_id de B -- nunca reaproveita o ciclo de A.
            report = coordinator_b.shutdown(reason="fechamento_normal_de_b", drain_timeout_seconds=0.2)
            assert report.shutdown_id != state_before["shutdown_id"]
            final_state = coordinator_b.get_draining_state()
            assert final_state["owner_session_id"] == coordinator_b.process_session_id
        finally:
            coordinator_b.release_ownership()


def test_sessao_nova_nao_roda_force_cleanup_sobre_ciclo_timed_out_de_sessao_anterior(local_db, audit):
    """Variante com lifecycle antigo em TIMED_OUT (não apenas IN_PROGRESS):
    uma sessão nova não pode rodar seus próprios hooks via
    ``force_cleanup_after_timeout()`` por cima de um ciclo TIMED_OUT que
    pertence a uma sessão operacional anterior -- mesmo essa sendo
    exatamente a situação que ``force_cleanup_after_timeout()`` existe para
    tratar (só que para o PRÓPRIO ciclo, nunca o de outra sessão)."""
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)

    coordinator_a = ShutdownCoordinator(local_db, process_session_id="sessao_a_timeout")
    coordinator_a.acquire_ownership()
    try:
        report = coordinator_a.shutdown(reason="vai_dar_timeout", drain_timeout_seconds=0.1)
        assert report.drain_status == STATUS_TIMED_OUT
        assert report.requires_forced_action is True
    finally:
        coordinator_a.release_ownership()  # processo A termina sem forçar cleanup

    coordinator_b = ShutdownCoordinator(local_db, process_session_id="sessao_b_timeout")
    coordinator_b.acquire_ownership()
    try:
        calls: list[str] = []
        coordinator_b.register_cleanup("hook_de_b", lambda: calls.append("rodou"))

        with pytest.raises(PreviousSessionLifecycleRequiresReconcileError):
            coordinator_b.force_cleanup_after_timeout()
        assert calls == []
        assert coordinator_b.is_draining() is True

        # depois de reconciliar, B pode operar normalmente com um ciclo próprio.
        RecoveryManager(local_db, audit_log=audit).recover_at_startup()
        resolution = coordinator_b.startup_reconcile()
        assert resolution.resolved is True
        assert coordinator_b.is_draining() is False
    finally:
        coordinator_b.release_ownership()


# ---------------------------------------------------------------------------
# SEGURANÇA DA QUINTA REVISÃO -- truncar mensagem de erro não é sanitizar
# segredo: nenhum valor persistido pode conter o texto original da exceção
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret_message",
    [
        "Authorization: Bearer sk-dummy-secret-abc123",
        "token=dummy-secret-token-xyz789",
        "password=dummy-secret-password-999",
        "cookie=session_id=dummy-secret-cookie-value",
    ],
)
def test_mensagem_de_erro_de_cleanup_com_segredo_nunca_e_persistida(local_db, audit, secret_message):
    """Requisito de segurança da quinta revisão: qualquer segredo presente
    em ``str(exc)`` de um hook de cleanup que falha NUNCA pode sobreviver na
    forma persistida (SQLite) do lifecycle nem nos audit_events -- truncar
    não é sanitizar. Apenas ``error_type`` (nome da classe da exceção) é
    persistido; o texto genérico fixo substitui qualquer conteúdo original
    ao ser reconstruído."""
    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=0.2)
    coordinator.acquire_ownership()
    try:
        def hook_vazador():
            raise RuntimeError(secret_message)

        coordinator.register_cleanup("hook_vazador", hook_vazador)
        coordinator.shutdown(reason="com_segredo_no_erro", drain_timeout_seconds=0.1)

        state = coordinator.get_draining_state()
        dumped_state = json.dumps(state)
        assert secret_message not in dumped_state

        audit_events = local_db.list_audit_events(entity_type="Lifecycle")
        dumped_events = json.dumps(audit_events)
        assert secret_message not in dumped_events

        persisted_results = state["cleanup_results"]
        assert persisted_results[0]["ok"] is False
        assert persisted_results[0]["error_type"] == "RuntimeError"
        assert "error" not in persisted_results[0]
    finally:
        coordinator.release_ownership()


# ---------------------------------------------------------------------------
# SEXTA REVISÃO -- error_type precisa ser uma allowlist REAL (fechada e
# estática), nunca o nome cru de type(exc).__name__
# ---------------------------------------------------------------------------


def test_nome_de_classe_de_excecao_fora_da_allowlist_nunca_e_persistido(local_db, audit):
    """Reprodução exata da sexta revisão: uma exceção com uma classe criada
    DINAMICAMENTE via ``type()``, cujo próprio nome carrega um dummy-secret,
    NUNCA pode fazer esse dummy-secret aparecer em ``lifecycle:draining``,
    em ``cleanup_results`` persistido, nem em ``audit_events`` -- só porque
    ``type(exc).__name__`` não está na allowlist fechada, o nome inteiro é
    substituído pela categoria interna fixa."""
    dummy_secret = "Bearer_SK_SUPER_SECRET_XYZ"
    SecretExc = type(dummy_secret, (Exception,), {})
    assert dummy_secret not in SAFE_ERROR_TYPES  # a allowlist é fechada e estática

    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=0.2)
    coordinator.acquire_ownership()
    try:
        def hook_com_classe_dinamica():
            raise SecretExc("mensagem")

        coordinator.register_cleanup("hook_com_classe_dinamica", hook_com_classe_dinamica)
        coordinator.shutdown(reason="classe_dinamica_com_segredo_no_nome", drain_timeout_seconds=0.1)

        state = coordinator.get_draining_state()
        persisted_results = state["cleanup_results"]
        assert persisted_results[0]["ok"] is False
        assert persisted_results[0]["error_type"] == _UNRECOGNIZED_ERROR_TYPE
        assert persisted_results[0]["error_type"] != dummy_secret

        dumped_state = json.dumps(state)
        assert dummy_secret not in dumped_state

        audit_events = local_db.list_audit_events(entity_type="Lifecycle")
        dumped_events = json.dumps(audit_events)
        assert dummy_secret not in dumped_events

        # nem módulo, nem repr(exc), nem args, nem mensagem -- só a categoria fixa.
        assert "mensagem" not in dumped_state
        assert "mensagem" not in dumped_events

        # reconstrução (chamada repetida / reabertura) também nunca expõe o nome.
        deserialized = _deserialize_cleanup_results(persisted_results)
        assert deserialized[0].error_type == _UNRECOGNIZED_ERROR_TYPE
        assert deserialized[0].error_type != dummy_secret
    finally:
        coordinator.release_ownership()


def test_error_type_reconhecido_da_allowlist_continua_visivel(local_db, audit):
    """Contraprova: um ``error_type`` que ESTÁ na allowlist fechada continua
    sendo persistido normalmente (a allowlist não vira uma redação total --
    só bloqueia o que não está explicitamente permitido)."""
    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=0.2)
    coordinator.acquire_ownership()
    try:
        def hook_timeout():
            raise TimeoutError("recurso não respondeu a tempo")

        coordinator.register_cleanup("hook_timeout", hook_timeout)
        coordinator.shutdown(reason="timeout_reconhecido", drain_timeout_seconds=0.1)

        state = coordinator.get_draining_state()
        assert state["cleanup_results"][0]["error_type"] == "TimeoutError"
    finally:
        coordinator.release_ownership()


# ---------------------------------------------------------------------------
# SEXTA REVISÃO -- reason de chamada repetida nunca reescreve o motivo
# histórico do mesmo shutdown_id
# ---------------------------------------------------------------------------


def test_reason_de_chamada_repetida_nao_reescreve_motivo_historico_apos_drained(coordinator):
    report1 = coordinator.shutdown(reason="ORIGINAL", drain_timeout_seconds=0.1)
    assert report1.reason == "ORIGINAL"
    assert report1.drained_completely is True

    report2 = coordinator.shutdown(reason="CHANGED", drain_timeout_seconds=0.1)
    assert report2.shutdown_id == report1.shutdown_id
    assert report2.reason == "ORIGINAL"  # nunca "CHANGED"

    state = coordinator.get_draining_state()
    assert state["reason"] == "ORIGINAL"


def test_reason_de_chamada_repetida_nao_reescreve_motivo_historico_apos_timeout(local_db, audit):
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)

    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=0.2)
    coordinator.acquire_ownership()
    try:
        report1 = coordinator.shutdown(reason="ORIGINAL_TIMEOUT", drain_timeout_seconds=0.1)
        assert report1.drain_status == STATUS_TIMED_OUT
        assert report1.reason == "ORIGINAL_TIMEOUT"

        # segunda chamada, mesmo shutdown_id ainda TIMED_OUT sem cleanup tentado
        # -- ainda assim, o reason nunca troca.
        report2 = coordinator.shutdown(reason="CHANGED_TIMEOUT", drain_timeout_seconds=0.1)
        assert report2.shutdown_id == report1.shutdown_id
        assert report2.reason == "ORIGINAL_TIMEOUT"

        state = coordinator.get_draining_state()
        assert state["reason"] == "ORIGINAL_TIMEOUT"
    finally:
        coordinator.release_ownership()


def test_reason_de_chamada_repetida_nao_reescreve_motivo_historico_apos_forced_cleanup(local_db, audit):
    stuck = _create_job(audit, operation="RENDER")
    audit.transition_job(stuck.id, JOB_PROCESSING)

    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=0.2)
    coordinator.acquire_ownership()
    try:
        report1 = coordinator.shutdown(reason="ORIGINAL_FORCED", drain_timeout_seconds=0.1)
        assert report1.drain_status == STATUS_TIMED_OUT
        coordinator.force_cleanup_after_timeout()

        report2 = coordinator.shutdown(reason="CHANGED_FORCED", drain_timeout_seconds=0.1)
        assert report2.shutdown_id == report1.shutdown_id
        assert report2.reason == "ORIGINAL_FORCED"

        state = coordinator.get_draining_state()
        assert state["reason"] == "ORIGINAL_FORCED"
    finally:
        coordinator.release_ownership()


def test_reason_do_ciclo_que_reaproveita_shutdown_id_apos_timeout_e_depois_drena(local_db, audit):
    """Caso mais sutil: o ciclo é reaberto (mesmo shutdown_id, sem cleanup
    ainda tentado) e desta vez DRENA COM SUCESSO e roda os hooks de verdade
    -- mesmo essa chamada que efetivamente executa os hooks agora deve
    relatar o reason ORIGINAL do ciclo, não o novo argumento passado."""
    job = _create_job(audit, operation="RENDER")
    audit.transition_job(job.id, JOB_PROCESSING)

    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=0.2)
    coordinator.acquire_ownership()
    try:
        report1 = coordinator.shutdown(reason="ORIGINAL_ANTES_DE_DRENAR", drain_timeout_seconds=0.05)
        assert report1.drain_status == STATUS_TIMED_OUT
        assert report1.requires_forced_action is True

        audit.transition_job(job.id, JOB_READY)  # agora o Job libera o ponto seguro

        report2 = coordinator.shutdown(reason="NOVO_MOTIVO_QUE_NAO_DEVE_APARECER", drain_timeout_seconds=1.0)
        assert report2.shutdown_id == report1.shutdown_id
        assert report2.drain_status == STATUS_DRAINED
        assert report2.cleanup_attempted is True
        assert report2.reason == "ORIGINAL_ANTES_DE_DRENAR"

        state = coordinator.get_draining_state()
        assert state["reason"] == "ORIGINAL_ANTES_DE_DRENAR"
    finally:
        coordinator.release_ownership()
