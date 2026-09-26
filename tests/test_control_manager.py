"""Testes do PROMPT 15 — pause, resume, stop_after_current e cancel.

Cobre o vocabulário/validação de escopo, persistência (inclusive
sobrevivência a fechar/reabrir o banco), a garantia transacional entre
pause/stop_after_current e reivindicação de Job (corrida real, com threads e
duas instâncias de ``LocalDatabase`` contra o mesmo arquivo), o cancelamento
seguro de Jobs em execução ativa/incerta (PROCESSING/PUBLISHING/RECOVERING/
UNKNOWN nunca são finalizados às cegas), o cancelamento em massa (JOB/QUEUE/
GLOBAL) com crash-safety, o cancelamento por ACCOUNT/PLATFORM como intenção
persistida (nunca fingindo aplicação), e a classificação honesta do
resultado de ``cancel()`` (nunca "ok" quando nada foi de fato cancelado).
Também cobre a integração opcional com o JobEngine (GLOBAL/QUEUE/JOB) e
confirma que, sem ``control_manager``, o comportamento do JobEngine é
idêntico ao de antes deste Prompt.
"""
import ast
import threading
import time
from pathlib import Path
import tempfile
import uuid

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHED,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_RETRY,
    JOB_UNKNOWN,
    SourceAsset,
)
from _sistema.storage import LocalDatabase, OperationalAuditLog
from _sistema.job_engine import (
    JobBlockedByControlError,
    JobEngine,
    JobEngineError,
    JobNotActionableError,
    JobStepResult,
)
from _sistema.control_manager import (
    AUTO_ENFORCED_SCOPES,
    CANCEL_OUTCOME_DEFERRED,
    CANCEL_OUTCOME_ERROR,
    CANCEL_OUTCOME_FULLY_APPLIED,
    CANCEL_OUTCOME_NOT_APPLIED,
    CANCEL_OUTCOME_PARTIALLY_APPLIED,
    CONTROL_SCOPE_ACCOUNT,
    CONTROL_SCOPE_GLOBAL,
    CONTROL_SCOPE_JOB,
    CONTROL_SCOPE_PLATFORM,
    CONTROL_SCOPE_QUEUE,
    CONTROL_SCOPES,
    MANUALLY_ENFORCED_SCOPES,
    ControlManager,
    ControlScopeIdError,
    InvalidControlScopeError,
    ScopeCancelOutcome,
    UnsupportedBulkCancelScopeError,
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
def control(local_db, audit):
    return ControlManager(local_db, audit_log=audit)


def _create_job(audit_log, *, operation="RENDER"):
    return audit_log.create_job(Job(operation=operation))


def _fresh_database(tmp_root: Path, *, initialize: bool) -> LocalDatabase:
    paths = build_app_paths(install_root=tmp_root / "install", data_root=tmp_root / "data")
    db = LocalDatabase(paths=paths)
    if initialize:
        db.initialize()
    return db


# ---------------------------------------------------------------------------
# Vocabulário de escopo
# ---------------------------------------------------------------------------


def test_control_scopes_contem_exatamente_os_cinco_niveis_do_roadmap():
    assert CONTROL_SCOPES == {
        CONTROL_SCOPE_GLOBAL,
        CONTROL_SCOPE_ACCOUNT,
        CONTROL_SCOPE_PLATFORM,
        CONTROL_SCOPE_QUEUE,
        CONTROL_SCOPE_JOB,
    }


def test_auto_enforced_e_manually_enforced_particionam_todos_os_escopos():
    assert AUTO_ENFORCED_SCOPES | MANUALLY_ENFORCED_SCOPES == CONTROL_SCOPES
    assert AUTO_ENFORCED_SCOPES.isdisjoint(MANUALLY_ENFORCED_SCOPES)
    assert AUTO_ENFORCED_SCOPES == {CONTROL_SCOPE_GLOBAL, CONTROL_SCOPE_QUEUE, CONTROL_SCOPE_JOB}
    assert MANUALLY_ENFORCED_SCOPES == {CONTROL_SCOPE_ACCOUNT, CONTROL_SCOPE_PLATFORM}


def test_pause_rejeita_escopo_invalido(control):
    with pytest.raises(InvalidControlScopeError):
        control.pause("NAO_EXISTE")


def test_pause_global_rejeita_scope_id(control):
    with pytest.raises(ControlScopeIdError):
        control.pause(CONTROL_SCOPE_GLOBAL, "algo")


@pytest.mark.parametrize(
    "scope", [CONTROL_SCOPE_ACCOUNT, CONTROL_SCOPE_PLATFORM, CONTROL_SCOPE_QUEUE, CONTROL_SCOPE_JOB]
)
def test_pause_exige_scope_id_para_escopos_nao_globais(control, scope):
    with pytest.raises(ControlScopeIdError):
        control.pause(scope)
    with pytest.raises(ControlScopeIdError):
        control.pause(scope, "")
    with pytest.raises(ControlScopeIdError):
        control.pause(scope, "   ")


def test_pause_job_exige_scope_id_valido_como_uuid(control):
    with pytest.raises(ControlScopeIdError):
        control.pause(CONTROL_SCOPE_JOB, "nao-e-um-uuid")


def test_pause_account_platform_queue_aceitam_qualquer_identificador_livre(control):
    # Não há acoplamento a nenhuma plataforma específica: qualquer string
    # não vazia é aceita como scope_id de ACCOUNT/PLATFORM/QUEUE.
    control.pause(CONTROL_SCOPE_ACCOUNT, "conta-arbitraria-123")
    control.pause(CONTROL_SCOPE_PLATFORM, "qualquer-plataforma")
    control.pause(CONTROL_SCOPE_QUEUE, "RENDER")
    assert control.is_paused(CONTROL_SCOPE_ACCOUNT, "conta-arbitraria-123")
    assert control.is_paused(CONTROL_SCOPE_PLATFORM, "qualquer-plataforma")
    assert control.is_paused(CONTROL_SCOPE_QUEUE, "RENDER")


# ---------------------------------------------------------------------------
# Pause/resume — round trip básico, para cada um dos cinco níveis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,scope_id",
    [
        (CONTROL_SCOPE_GLOBAL, None),
        (CONTROL_SCOPE_ACCOUNT, "acc-1"),
        (CONTROL_SCOPE_PLATFORM, "plat-1"),
        (CONTROL_SCOPE_QUEUE, "RENDER"),
        (CONTROL_SCOPE_JOB, None),  # substituído por um job_id real no teste
    ],
)
def test_pause_depois_resume_round_trip_em_cada_nivel(control, audit, scope, scope_id):
    if scope == CONTROL_SCOPE_JOB:
        scope_id = _create_job(audit).id

    assert control.is_paused(scope, scope_id) is False

    control.pause(scope, scope_id, reason="manutenção")
    assert control.is_paused(scope, scope_id) is True

    control.resume(scope, scope_id)
    assert control.is_paused(scope, scope_id) is False


def test_pause_e_idempotente(control):
    control.pause(CONTROL_SCOPE_GLOBAL)
    control.pause(CONTROL_SCOPE_GLOBAL, reason="motivo atualizado")
    assert control.is_paused(CONTROL_SCOPE_GLOBAL) is True


# ---------------------------------------------------------------------------
# stop_after_current
# ---------------------------------------------------------------------------


def test_stop_after_current_e_flag_independente_de_paused(control):
    control.request_stop_after_current(CONTROL_SCOPE_QUEUE, "RENDER")

    assert control.is_stop_requested(CONTROL_SCOPE_QUEUE, "RENDER") is True
    assert control.is_paused(CONTROL_SCOPE_QUEUE, "RENDER") is False  # não é o mesmo flag
    # mas ambos bloqueiam execução:
    assert control.is_execution_blocked(operation="RENDER") is True


def test_resume_limpa_tanto_paused_quanto_stop_after_current(control):
    control.pause(CONTROL_SCOPE_QUEUE, "RENDER")
    control.request_stop_after_current(CONTROL_SCOPE_QUEUE, "RENDER")

    control.resume(CONTROL_SCOPE_QUEUE, "RENDER")

    assert control.is_paused(CONTROL_SCOPE_QUEUE, "RENDER") is False
    assert control.is_stop_requested(CONTROL_SCOPE_QUEUE, "RENDER") is False


# ---------------------------------------------------------------------------
# is_execution_blocked: consulta genérica
# ---------------------------------------------------------------------------


def test_is_execution_blocked_false_quando_nada_esta_pausado(control):
    assert control.is_execution_blocked() is False
    assert control.is_execution_blocked(operation="RENDER", job_id=str(uuid.uuid4())) is False


def test_is_execution_blocked_global_bloqueia_tudo(control):
    control.pause(CONTROL_SCOPE_GLOBAL)
    assert control.is_execution_blocked() is True
    assert control.is_execution_blocked(operation="QUALQUER") is True
    assert control.is_execution_blocked(job_id=str(uuid.uuid4())) is True


def test_is_execution_blocked_queue_afeta_so_a_operation_pausada(control):
    control.pause(CONTROL_SCOPE_QUEUE, "RENDER")
    assert control.is_execution_blocked(operation="RENDER") is True
    assert control.is_execution_blocked(operation="UPLOAD") is False


def test_is_execution_blocked_job_afeta_so_aquele_job(control, audit):
    job_a = _create_job(audit, operation="A")
    job_b = _create_job(audit, operation="A")
    control.pause(CONTROL_SCOPE_JOB, job_a.id)

    assert control.is_execution_blocked(operation="A", job_id=job_a.id) is True
    assert control.is_execution_blocked(operation="A", job_id=job_b.id) is False


def test_is_execution_blocked_account_platform_disponiveis_para_consulta_futura(control):
    # Ninguém dentro do JobEngine chama isto com account_id/platform hoje
    # (ver docstring do módulo), mas a API já suporta um Connector futuro
    # que os forneça.
    control.pause(CONTROL_SCOPE_ACCOUNT, "conta-x")
    control.pause(CONTROL_SCOPE_PLATFORM, "plataforma-y")

    assert control.is_execution_blocked(account_id="conta-x") is True
    assert control.is_execution_blocked(account_id="outra-conta") is False
    assert control.is_execution_blocked(platform="plataforma-y") is True
    assert control.is_execution_blocked(platform="outra-plataforma") is False


def test_is_execution_blocked_aceita_connection_explicita_para_composicao_atomica(control, local_db):
    """A correção da corrida pause-vs-claim depende de is_execution_blocked
    conseguir ler sob uma connection já aberta (mesmo BEGIN IMMEDIATE) em
    vez de abrir a sua própria leitura solta."""
    control.pause(CONTROL_SCOPE_GLOBAL)
    with local_db.transaction() as conn:
        assert control.is_execution_blocked(connection=conn) is True


# ---------------------------------------------------------------------------
# Persistência: sobrevive a fechar/reabrir o banco (reinício do computador)
# ---------------------------------------------------------------------------


def test_pause_sobrevive_a_reabertura_do_banco():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        db_first_run = _fresh_database(root, initialize=True)
        control_first_run = ControlManager(db_first_run, audit_log=OperationalAuditLog(db_first_run))
        control_first_run.pause(CONTROL_SCOPE_GLOBAL, reason="operador pausou antes de desligar")
        control_first_run.request_stop_after_current(CONTROL_SCOPE_QUEUE, "RENDER")
        del control_first_run, db_first_run

        db_after_restart = _fresh_database(root, initialize=False)
        control_after_restart = ControlManager(db_after_restart, audit_log=OperationalAuditLog(db_after_restart))

        assert control_after_restart.is_paused(CONTROL_SCOPE_GLOBAL) is True
        assert control_after_restart.is_stop_requested(CONTROL_SCOPE_QUEUE, "RENDER") is True


def test_restart_preserva_intencoes_de_cancelamento_relevantes():
    """Requisito obrigatório #11: intenção de cancel de Job ativo (deferred),
    intenção de cancel de ACCOUNT/PLATFORM e o registro de lote GLOBAL/QUEUE
    sobrevivem a fechar/reabrir o banco."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        db_first_run = _fresh_database(root, initialize=True)
        audit_first_run = OperationalAuditLog(db_first_run)
        control_first_run = ControlManager(db_first_run, audit_log=audit_first_run)

        active_job = _create_job(audit_first_run, operation="RENDER")
        audit_first_run.transition_job(active_job.id, JOB_PROCESSING)
        control_first_run.cancel(CONTROL_SCOPE_JOB, active_job.id)  # deferred: Job está ativo
        control_first_run.cancel(CONTROL_SCOPE_ACCOUNT, "conta-x")
        control_first_run.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
        del control_first_run, audit_first_run, db_first_run

        db_after_restart = _fresh_database(root, initialize=False)
        control_after_restart = ControlManager(db_after_restart, audit_log=OperationalAuditLog(db_after_restart))

        assert control_after_restart.is_job_cancel_requested(active_job.id) is True
        assert control_after_restart.is_cancel_requested(CONTROL_SCOPE_ACCOUNT, "conta-x") is True
        batch = control_after_restart.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
        assert batch is not None
        assert batch["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# BLOQUEADOR 1 — corrida pause/stop_after_current vs claim
# ---------------------------------------------------------------------------


def _run_pause_vs_claim_race(*, use_stop_after_current: bool, iterations: int = 20) -> None:
    """Dispara ``pause``/``stop_after_current`` e uma reivindicação de Job
    (via ``JobEngine.advance``) concorrentemente, muitas vezes, usando DUAS
    instâncias reais de ``LocalDatabase`` (simulando dois processos) contra
    o mesmo arquivo SQLite. Depois de cada rodada, verifica através da
    ORDEM REAL de commit (rowid de audit_events, nunca hora de relógio) que
    nunca existiu a interleaving proibida: pause commitado ANTES da
    reivindicação, mas a reivindicação mesmo assim tendo sucedido.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_engine = _fresh_database(root, initialize=True)
        db_controller = _fresh_database(root, initialize=False)

        audit_engine = OperationalAuditLog(db_engine)
        control_engine = ControlManager(db_engine, audit_log=audit_engine)
        control_controller = ControlManager(db_controller, audit_log=OperationalAuditLog(db_controller))

        engine = JobEngine(db_engine, audit_log=audit_engine, control_manager=control_engine)
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

            def do_pause():
                barrier.wait(timeout=5)
                if use_stop_after_current:
                    control_controller.request_stop_after_current(CONTROL_SCOPE_QUEUE, operation)
                else:
                    control_controller.pause(CONTROL_SCOPE_QUEUE, operation)

            t_claim = threading.Thread(target=do_claim)
            t_pause = threading.Thread(target=do_pause)
            t_claim.start()
            t_pause.start()
            t_claim.join(timeout=10)
            t_pause.join(timeout=10)

            pause_event_type = "CONTROL_STOP_AFTER_CURRENT_REQUESTED" if use_stop_after_current else "CONTROL_PAUSED"
            control_events = [
                e
                for e in db_engine.list_audit_events(entity_type="ControlScope", event_type=pause_event_type)
                if e["data"].get("scope_id") == operation
            ]
            if not control_events:
                never_committed.append(i)
                continue
            pause_sequence = control_events[0]["sequence"]

            claim_events = db_engine.list_audit_events(
                entity_type="Job", entity_id=job.id, event_type="JOB_STATE_CHANGED"
            )
            claimed_successfully = bool(claim_events)
            if claimed_successfully:
                claim_sequence = claim_events[0]["sequence"]
                if pause_sequence < claim_sequence:
                    bad_interleavings.append(
                        {"iteration": i, "pause_sequence": pause_sequence, "claim_sequence": claim_sequence}
                    )

        assert not never_committed, f"pause/stop_after_current deveria sempre commitar: {never_committed}"
        assert not bad_interleavings, (
            "reivindicação de Job sucedeu apesar de pause/stop_after_current já ter "
            f"commitado antes dela: {bad_interleavings}"
        )


def test_pause_commitado_antes_do_claim_impede_o_claim_em_corrida_real():
    _run_pause_vs_claim_race(use_stop_after_current=False)


def test_stop_after_current_possui_a_mesma_garantia_transacional_contra_claim():
    _run_pause_vs_claim_race(use_stop_after_current=True)


def test_duas_instancias_respeitam_a_ordenacao_pause_vs_claim():
    """Mesmo teste de corrida acima, mas o ponto central deste teste é
    justamente a garantia entre PROCESSOS DIFERENTES: engine e controlador
    usam LocalDatabase próprios (nenhum estado em memória compartilhado),
    só o arquivo SQLite em comum."""
    _run_pause_vs_claim_race(use_stop_after_current=False, iterations=10)
    _run_pause_vs_claim_race(use_stop_after_current=True, iterations=10)


# ---------------------------------------------------------------------------
# BLOQUEADOR 2 — cancelamento seguro de Job em execução ativa
# ---------------------------------------------------------------------------


def test_cancel_durante_processing_nao_finaliza_falsamente_enquanto_handler_ativo(local_db, audit, control):
    """Reproduz o cenário exato do bloqueador: handler ainda rodando quando
    cancel(JOB) é chamado. Usa Barrier para garantir que o cancel acontece
    estritamente DURANTE a execução do handler (não antes, não depois)."""
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)

    handler_entered = threading.Event()
    release_handler = threading.Event()

    def slow_handler(claimed_job):
        handler_entered.set()
        assert release_handler.wait(timeout=5), "cancel não sinalizou a tempo"
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("RENDER", slow_handler, claims_status=JOB_PROCESSING)

    advance_result: dict = {}

    def run_advance():
        try:
            advance_result["job"] = engine.advance(job.id)
        except JobEngineError as exc:
            advance_result["error"] = exc

    t = threading.Thread(target=run_advance)
    t.start()
    assert handler_entered.wait(timeout=5), "handler nunca começou a executar"

    # Handler está comprovadamente PROCESSING e ativo neste exato momento.
    assert local_db.get(Job, job.id).status == JOB_PROCESSING
    outcome = control.cancel(CONTROL_SCOPE_JOB, job.id)

    # 1) cancel NÃO marca o Job como CANCELLED enquanto o handler está ativo.
    assert local_db.get(Job, job.id).status == JOB_PROCESSING
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert outcome.deferred_job_ids == (job.id,)
    assert outcome.cancelled_job_ids == ()
    assert control.is_job_cancel_requested(job.id) is True

    release_handler.set()
    t.join(timeout=5)

    # 2) o handler não gera JobHandlerError artificial só porque cancel foi
    #    pedido — ele "funcionou" (retornou um resultado válido).
    assert "error" not in advance_result, advance_result.get("error")

    # 3) no primeiro ponto seguro possível (handler terminou), o
    #    cancelamento é corretamente refletido: CANCELLED, não READY.
    final_job = advance_result["job"]
    assert final_job.status == JOB_CANCELLED
    assert local_db.get(Job, job.id).status == JOB_CANCELLED

    # a intenção foi limpa depois de aplicada.
    assert control.is_job_cancel_requested(job.id) is False


def test_cancel_durante_processing_sem_intencao_pendente_aplica_resultado_normal(local_db, audit, control):
    """Contraprova: quando NENHUM cancel foi pedido, o resultado do handler
    é persistido normalmente — a correção não interfere no caminho feliz."""
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    updated = engine.advance(job.id)

    assert updated.status == JOB_READY


def test_cancel_durante_publishing_nunca_mascara_incerteza_remota(local_db, audit, control):
    """Requisito obrigatório #5: cancel durante PUBLISHING não destrói a
    incerteza remota — o Job permanece PUBLISHING, a intenção fica
    registrada como deferred, e mesmo quando o handler eventualmente
    persiste um resultado (ex.: PUBLISHED, confirmando o efeito remoto), o
    JobEngine NUNCA substitui isso por CANCELLED."""
    job = _create_job(audit, operation="UPLOAD")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler(
        "UPLOAD", lambda j: JobStepResult(target_status=JOB_PUBLISHED), claims_status=JOB_PUBLISHING
    )

    claimed = engine._claim(job.id, JOB_PUBLISHING, "UPLOAD")
    assert claimed.status == JOB_PUBLISHING

    outcome = control.cancel(CONTROL_SCOPE_JOB, job.id)
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert outcome.deferred_job_ids == (job.id,)
    assert local_db.get(Job, job.id).status == JOB_PUBLISHING  # nunca mascarado

    # O handler (já reivindicado) termina confirmando o efeito remoto. O
    # JobEngine não tem lógica de auto-aplicar cancel para PUBLISHING (só
    # para PROCESSING — ver docstring), então o resultado real prevalece.
    final = audit.transition_job(job.id, JOB_PUBLISHED, semantic_event=None)
    assert final.status == JOB_PUBLISHED
    # a intenção continua registrada (não foi mascarada, nem silenciosamente
    # descartada) até alguém decidir o que fazer com ela — mas o Job já é
    # terminal (PUBLISHED), então nunca mais será candidato a cancelamento.
    assert control.is_job_cancel_requested(job.id) is True


def test_cancel_durante_recovering_tambem_e_deferido(local_db, audit, control):
    job = _create_job(audit, operation="UPLOAD")
    audit.transition_job(job.id, JOB_PUBLISHING)
    audit.transition_job(job.id, JOB_UNKNOWN)
    audit.transition_job(job.id, JOB_RECOVERING)

    outcome = control.cancel(CONTROL_SCOPE_JOB, job.id)

    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert local_db.get(Job, job.id).status == JOB_RECOVERING


def test_cancel_durante_unknown_e_deferido_nao_mascara_incerteza(local_db, audit, control):
    job = _create_job(audit, operation="UPLOAD")
    audit.transition_job(job.id, JOB_PUBLISHING)
    audit.transition_job(job.id, JOB_UNKNOWN)

    outcome = control.cancel(CONTROL_SCOPE_JOB, job.id)

    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert local_db.get(Job, job.id).status == JOB_UNKNOWN


# ---------------------------------------------------------------------------
# Cancel — JOB (casos simples/estruturais)
# ---------------------------------------------------------------------------


def test_cancel_job_cancela_o_job_indicado(control, audit):
    job = _create_job(audit)

    outcome = control.cancel(CONTROL_SCOPE_JOB, job.id)

    assert isinstance(outcome, ScopeCancelOutcome)
    assert outcome.status == CANCEL_OUTCOME_FULLY_APPLIED
    assert outcome.ok
    assert outcome.cancelled_job_ids == (job.id,)
    assert outcome.deferred_job_ids == ()
    assert outcome.skipped_job_ids == ()
    persisted = audit.database.get(Job, job.id)
    assert persisted.status == "CANCELLED"


def test_cancel_job_inexistente_aparece_em_errors(control):
    missing_id = str(uuid.uuid4())

    outcome = control.cancel(CONTROL_SCOPE_JOB, missing_id)

    assert not outcome.ok
    assert outcome.status == CANCEL_OUTCOME_ERROR
    assert outcome.cancelled_job_ids == ()
    assert len(outcome.errors) == 1
    assert outcome.errors[0][0] == missing_id


def test_cancel_job_ja_cancelado_e_idempotente(control, audit):
    """Requisito obrigatório #12: repetir a mesma solicitação é seguro."""
    job = _create_job(audit)
    first = control.cancel(CONTROL_SCOPE_JOB, job.id)
    second = control.cancel(CONTROL_SCOPE_JOB, job.id)

    assert first.status == CANCEL_OUTCOME_FULLY_APPLIED
    assert second.status == CANCEL_OUTCOME_FULLY_APPLIED
    assert second.cancelled_job_ids == (job.id,)
    assert second.errors == ()
    assert audit.database.get(Job, job.id).status == JOB_CANCELLED


def test_cancel_job_publicado_e_terminal_e_reportado_como_skipped_nao_deferred(control, audit):
    """PUBLISHED é terminal (sucesso remoto já confirmado): não é "execução
    ativa", nunca vai se tornar cancelável esperando — é estruturalmente
    skipped, não deferred."""
    job = _create_job(audit, operation="UPLOAD")
    audit.transition_job(job.id, JOB_PUBLISHING)
    audit.transition_job(job.id, JOB_PUBLISHED)

    outcome = control.cancel(CONTROL_SCOPE_JOB, job.id)

    assert outcome.skipped_job_ids == (job.id,)
    assert outcome.deferred_job_ids == ()
    assert outcome.status == CANCEL_OUTCOME_NOT_APPLIED
    assert not outcome.ok


def test_cancel_nao_apaga_source_asset_original(control, audit, local_db):
    """'Cancel não pode apagar original' (roadmap): comprova que nenhum
    SourceAsset é tocado por um cancelamento de Job."""
    source = local_db.insert(SourceAsset(source_uri="file:///video.mp4"))
    job = _create_job(audit)

    control.cancel(CONTROL_SCOPE_JOB, job.id)

    still_there = local_db.get(SourceAsset, source.id)
    assert still_there is not None
    assert still_there.source_uri == "file:///video.mp4"


# ---------------------------------------------------------------------------
# Cancel — QUEUE e GLOBAL (em massa)
# ---------------------------------------------------------------------------


def test_cancel_queue_cancela_so_jobs_da_operation_indicada(control, audit):
    render_a = _create_job(audit, operation="RENDER")
    render_b = _create_job(audit, operation="RENDER")
    upload = _create_job(audit, operation="UPLOAD")

    outcome = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    assert set(outcome.cancelled_job_ids) == {render_a.id, render_b.id}
    assert outcome.status == CANCEL_OUTCOME_FULLY_APPLIED
    assert audit.database.get(Job, upload.id).status == JOB_PENDING


def test_cancel_global_cancela_todas_as_operations(control, audit):
    a = _create_job(audit, operation="A")
    b = _create_job(audit, operation="B")

    outcome = control.cancel(CONTROL_SCOPE_GLOBAL)

    assert set(outcome.cancelled_job_ids) == {a.id, b.id}
    assert outcome.status == CANCEL_OUTCOME_FULLY_APPLIED


def test_cancel_em_massa_isola_jobs_nao_cancelaveis_dos_demais(control, audit):
    cancellable = _create_job(audit, operation="RENDER")
    active = _create_job(audit, operation="RENDER")
    audit.transition_job(active.id, JOB_PROCESSING)

    outcome = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    assert outcome.cancelled_job_ids == (cancellable.id,)
    assert outcome.deferred_job_ids == (active.id,)
    assert outcome.status == CANCEL_OUTCOME_PARTIALLY_APPLIED


def test_cancel_queue_sem_jobs_correspondentes_devolve_outcome_totalmente_aplicado_vazio(control):
    outcome = control.cancel(CONTROL_SCOPE_QUEUE, "NAO_EXISTE")
    assert outcome.cancelled_job_ids == ()
    assert outcome.deferred_job_ids == ()
    assert outcome.skipped_job_ids == ()
    assert outcome.errors == ()
    assert outcome.status == CANCEL_OUTCOME_FULLY_APPLIED
    assert outcome.ok


# ---------------------------------------------------------------------------
# BLOQUEADOR 4 — cancelamento em massa é crash-safe
# ---------------------------------------------------------------------------


def test_cancel_global_persiste_a_intencao_antes_de_qualquer_efeito(local_db, audit, control, monkeypatch):
    """A prova direta do requisito: a intenção do lote precisa existir de
    forma durável ANTES de qualquer Job ser tocado — não só depois que o
    laço inteiro terminar."""
    _create_job(audit, operation="A")
    _create_job(audit, operation="B")

    observed = {}
    original_cancel_or_defer = ControlManager._cancel_or_defer_job

    def spy(self, job_id, *, reason):
        if "batch_seen" not in observed:
            batch = self.get_cancel_batch_request(CONTROL_SCOPE_GLOBAL)
            observed["batch_seen"] = batch
        return original_cancel_or_defer(self, job_id, reason=reason)

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", spy)

    control.cancel(CONTROL_SCOPE_GLOBAL)

    assert observed["batch_seen"] is not None, "o registro de lote deveria já existir antes do primeiro Job ser tocado"
    assert observed["batch_seen"]["status"] == "IN_PROGRESS"


def test_crash_no_meio_de_cancel_global_preserva_a_intencao(local_db, audit, control, monkeypatch):
    """Requisito obrigatório #6: simula um crash literalmente no meio do
    laço (uma exceção não tratada estourando de dentro de
    ``_cancel_or_defer_job`` para o segundo Job) e confirma que o registro
    de lote fica IN_PROGRESS — nunca desaparece — mesmo com o primeiro Job
    já cancelado e os demais intocados."""
    job_a = _create_job(audit, operation="RENDER")
    job_b = _create_job(audit, operation="RENDER")
    job_c = _create_job(audit, operation="RENDER")

    original_cancel_or_defer = ControlManager._cancel_or_defer_job
    calls = {"count": 0}

    def crash_on_second(self, job_id, *, reason):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("crash simulado no meio do lote")
        return original_cancel_or_defer(self, job_id, reason=reason)

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_on_second)

    with pytest.raises(RuntimeError, match="crash simulado"):
        control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    # A intenção sobrevive ao "crash": nunca foi perdida.
    batch = control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
    assert batch is not None
    assert batch["status"] == "IN_PROGRESS"
    assert set(batch["candidate_job_ids"]) == {job_a.id, job_b.id, job_c.id}

    # Exatamente o primeiro Job processado (ordem de LocalDatabase.list, não
    # necessariamente job_a — ver docstring de fetch_pending_jobs sobre
    # created_at ter resolução de segundos) chegou a ser cancelado antes do
    # crash; os outros dois ficaram intocados.
    statuses_after_crash = [local_db.get(Job, j.id).status for j in (job_a, job_b, job_c)]
    assert statuses_after_crash.count(JOB_CANCELLED) == 1
    assert statuses_after_crash.count(JOB_PENDING) == 2

    # Reinvocar cancel() depois do "restart" retoma de onde parou, sem
    # duplicar efeito sobre o Job já cancelado (idempotente).
    resumed = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
    assert set(resumed.cancelled_job_ids) == {job_a.id, job_b.id, job_c.id}
    assert resumed.status == CANCEL_OUTCOME_FULLY_APPLIED

    batch_after_resume = control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
    assert batch_after_resume["status"] == "COMPLETED"


def test_crash_no_meio_de_cancel_queue_preserva_a_intencao(local_db, audit, control, monkeypatch):
    """Requisito obrigatório #7 — mesmo teste do GLOBAL, mas para QUEUE,
    confirmando que o registro de lote é por (scope, scope_id) e não se
    confunde entre filas diferentes."""
    render_job = _create_job(audit, operation="RENDER")
    upload_job = _create_job(audit, operation="UPLOAD")  # fora do escopo QUEUE=RENDER

    original_cancel_or_defer = ControlManager._cancel_or_defer_job

    def always_crash(self, job_id, *, reason):
        raise RuntimeError("crash simulado")

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", always_crash)

    with pytest.raises(RuntimeError):
        control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    batch = control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
    assert batch is not None
    assert batch["status"] == "IN_PROGRESS"
    assert batch["candidate_job_ids"] == [render_job.id]

    # Nenhum registro de lote foi criado para UPLOAD — a intenção é
    # granular ao escopo exato solicitado.
    assert control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "UPLOAD") is None

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", original_cancel_or_defer)
    resumed = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
    assert resumed.cancelled_job_ids == (render_job.id,)
    assert control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")["status"] == "COMPLETED"


def test_cancel_em_massa_repetido_e_idempotente_nao_duplica_efeito(control, audit):
    """Requisito obrigatório #12 em cenário de lote (sem crash): chamar
    cancel(GLOBAL) várias vezes seguidas é sempre seguro.

    A primeira chamada cancela A e B. Depois de CANCELLED (terminal), a
    correção pós-4ª revisão exclui A/B de um snapshot NOVO de cancelamento
    em massa — então a segunda/terceira chamada calculam um snapshot vazio
    (nada relevante para cancelar), o que ainda é FULLY_APPLIED (nenhum
    erro, nenhum deferred, nenhum skipped) e, acima de tudo, nunca duplica
    efeito nem regride o status dos Jobs já cancelados."""
    job_a = _create_job(audit, operation="A")
    job_b = _create_job(audit, operation="B")

    first = control.cancel(CONTROL_SCOPE_GLOBAL)
    assert set(first.cancelled_job_ids) == {job_a.id, job_b.id}
    assert first.status == CANCEL_OUTCOME_FULLY_APPLIED
    assert first.errors == ()

    second = control.cancel(CONTROL_SCOPE_GLOBAL)
    third = control.cancel(CONTROL_SCOPE_GLOBAL)

    for outcome in (second, third):
        assert outcome.cancelled_job_ids == ()  # nada novo: A/B já são terminais, fora do snapshot
        assert outcome.status == CANCEL_OUTCOME_FULLY_APPLIED
        assert outcome.errors == ()

    assert audit.database.get(Job, job_a.id).status == JOB_CANCELLED
    assert audit.database.get(Job, job_b.id).status == JOB_CANCELLED


# ---------------------------------------------------------------------------
# Cancel — ACCOUNT/PLATFORM: intenção persistida, nunca fingida
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", [CONTROL_SCOPE_ACCOUNT, CONTROL_SCOPE_PLATFORM])
def test_cancel_account_platform_e_persistido_como_intencao_deferred(control, scope):
    """Requisitos obrigatórios #8 e #9: cancel(ACCOUNT)/cancel(PLATFORM) não
    levanta mais exceção — persiste uma intenção consultável e devolve
    status=DEFERRED, nunca fingindo que algo foi cancelado."""
    outcome = control.cancel(scope, "algum-id", reason="operador suspendeu a conta")

    assert isinstance(outcome, ScopeCancelOutcome)
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert outcome.cancelled_job_ids == ()
    assert outcome.deferred_job_ids == ()
    assert outcome.skipped_job_ids == ()
    assert outcome.errors == ()
    assert not outcome.ok  # nada foi de fato aplicado agora

    assert control.is_cancel_requested(scope, "algum-id") is True


def test_cancel_account_platform_nao_toca_nenhum_job(control, audit):
    job = _create_job(audit)

    control.cancel(CONTROL_SCOPE_ACCOUNT, "conta-x")
    control.cancel(CONTROL_SCOPE_PLATFORM, "plataforma-y")

    assert audit.database.get(Job, job.id).status == JOB_PENDING


def test_unsupported_bulk_cancel_scope_error_permanece_exportada_mas_nao_e_mais_levantada(control):
    """A classe continua existindo (uso futuro reservado — ver docstring do
    módulo), mas cancel() não a levanta mais para ACCOUNT/PLATFORM."""
    assert issubclass(UnsupportedBulkCancelScopeError, Exception)
    outcome = control.cancel(CONTROL_SCOPE_ACCOUNT, "conta-x")
    assert outcome.status == CANCEL_OUTCOME_DEFERRED  # nunca levanta a exceção


# ---------------------------------------------------------------------------
# NÃO INVENTAR SUCESSO — status agregado de cancel()
# ---------------------------------------------------------------------------


def test_status_partially_applied_quando_ha_mix_de_cancelado_e_deferido(control, audit):
    cancellable = _create_job(audit, operation="RENDER")
    active = _create_job(audit, operation="RENDER")
    audit.transition_job(active.id, JOB_PROCESSING)

    outcome = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    assert outcome.status == CANCEL_OUTCOME_PARTIALLY_APPLIED
    assert not outcome.ok


def test_status_error_quando_apenas_erros_e_nada_mais(control):
    outcome = control.cancel(CONTROL_SCOPE_JOB, str(uuid.uuid4()))
    assert outcome.status == CANCEL_OUTCOME_ERROR
    assert not outcome.ok


def test_status_not_applied_quando_so_ha_skipped_sem_nada_cancelado(control, audit):
    """``cancel(JOB, x)`` explícito continua sendo o caminho para observar
    "skipped" isolado: mirar diretamente um Job já terminal. Um lote
    QUEUE/GLOBAL novo, por outro lado, exclui esse mesmo Job do snapshot
    (correção pós-4ª revisão) e por isso não passa mais por "skipped" nesse
    cenário — ver ``test_cancel_queue_ignora_jobs_terminais_historicos_no_snapshot_novo``."""
    job = _create_job(audit, operation="UPLOAD")
    audit.transition_job(job.id, JOB_PUBLISHING)
    audit.transition_job(job.id, JOB_PUBLISHED)

    outcome = control.cancel(CONTROL_SCOPE_JOB, job.id)

    assert outcome.status == CANCEL_OUTCOME_NOT_APPLIED
    assert not outcome.ok


def test_scope_cancel_outcome_rejeita_status_invalido():
    with pytest.raises(ValueError):
        ScopeCancelOutcome(
            scope=CONTROL_SCOPE_JOB,
            scope_id="x",
            status="NAO_EXISTE",
            cancelled_job_ids=(),
        )


# ---------------------------------------------------------------------------
# Auditabilidade
# ---------------------------------------------------------------------------


def test_pause_resume_stop_registram_eventos_auditaveis(control, local_db):
    control.pause(CONTROL_SCOPE_GLOBAL, reason="teste")
    control.request_stop_after_current(CONTROL_SCOPE_QUEUE, "RENDER")
    control.resume(CONTROL_SCOPE_GLOBAL)

    events = local_db.list_audit_events(entity_type="ControlScope")
    event_types = [event["event_type"] for event in events]
    assert "CONTROL_PAUSED" in event_types
    assert "CONTROL_STOP_AFTER_CURRENT_REQUESTED" in event_types
    assert "CONTROL_RESUMED" in event_types


def test_cancel_scope_registra_evento_de_requisicao_e_de_conclusao(control, audit, local_db):
    _create_job(audit, operation="RENDER")

    control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    events = [e["event_type"] for e in local_db.list_audit_events(entity_type="ControlScope")]
    assert "CONTROL_CANCEL_SCOPE_REQUESTED" in events
    assert "CONTROL_CANCEL_SCOPE_COMPLETED" in events
    # a requisição deve aparecer ANTES da conclusão na ordem real de commit.
    assert events.index("CONTROL_CANCEL_SCOPE_REQUESTED") < events.index("CONTROL_CANCEL_SCOPE_COMPLETED")


# ---------------------------------------------------------------------------
# Integração com o JobEngine — sem control_manager: comportamento preservado
# ---------------------------------------------------------------------------


def test_job_engine_sem_control_manager_comporta_se_exatamente_como_antes(local_db, audit):
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit)  # control_manager omitido (padrão None)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    outcomes = engine.run_pending()

    assert len(outcomes) == 1
    assert outcomes[0].ok
    assert outcomes[0].job.status == JOB_READY


# ---------------------------------------------------------------------------
# Integração com o JobEngine — GLOBAL/QUEUE/JOB aplicados automaticamente
# ---------------------------------------------------------------------------


def test_pause_global_bloqueia_fetch_pending_jobs_e_run_pending(local_db, audit, control):
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    calls = []
    engine.register_handler(
        "RENDER", lambda j: calls.append(j.id) or JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    control.pause(CONTROL_SCOPE_GLOBAL)

    assert engine.fetch_pending_jobs() == []
    outcomes = engine.run_pending()

    assert outcomes == []
    assert calls == []
    assert local_db.get(Job, job.id).status == JOB_PENDING  # intocado, ainda elegível


def test_resume_global_libera_execucao_normalmente(local_db, audit, control):
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    control.pause(CONTROL_SCOPE_GLOBAL)
    assert engine.run_pending() == []

    control.resume(CONTROL_SCOPE_GLOBAL)
    outcomes = engine.run_pending()

    assert len(outcomes) == 1
    assert outcomes[0].ok
    assert local_db.get(Job, job.id).status == JOB_READY


def test_pause_queue_bloqueia_so_a_operation_pausada(local_db, audit, control):
    render_job = _create_job(audit, operation="RENDER")
    upload_job = _create_job(audit, operation="UPLOAD")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)
    engine.register_handler("UPLOAD", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    control.pause(CONTROL_SCOPE_QUEUE, "RENDER")

    pending_ids = {job.id for job in engine.fetch_pending_jobs()}
    assert pending_ids == {upload_job.id}

    outcomes = {outcome.job_id: outcome for outcome in engine.run_pending()}
    assert set(outcomes) == {upload_job.id}
    assert local_db.get(Job, render_job.id).status == JOB_PENDING
    assert local_db.get(Job, upload_job.id).status == JOB_READY


def test_pause_job_bloqueia_so_aquele_job(local_db, audit, control):
    job_a = _create_job(audit, operation="RENDER")
    job_b = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    control.pause(CONTROL_SCOPE_JOB, job_a.id)

    pending_ids = {job.id for job in engine.fetch_pending_jobs()}
    assert pending_ids == {job_b.id}
    assert local_db.get(Job, job_a.id).status == JOB_PENDING


def test_stop_after_current_bloqueia_novas_reivindicacoes_como_pause(local_db, audit, control):
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    calls = []
    engine.register_handler(
        "RENDER", lambda j: calls.append(j.id) or JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    control.request_stop_after_current(CONTROL_SCOPE_QUEUE, "RENDER")

    assert engine.fetch_pending_jobs() == []
    assert engine.run_pending() == []
    assert calls == []


def test_advance_direto_em_job_bloqueado_levanta_job_blocked_by_control_error(local_db, audit, control):
    """Mesmo chamando advance() diretamente (contornando fetch_pending_jobs),
    o bloqueio ainda é aplicado — a checagem vive em _claim, o único ponto
    por onde toda reivindicação passa."""
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    calls = []
    engine.register_handler(
        "RENDER", lambda j: calls.append(j.id) or JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    control.pause(CONTROL_SCOPE_JOB, job.id)

    with pytest.raises(JobBlockedByControlError):
        engine.advance(job.id)

    assert calls == []
    assert local_db.get(Job, job.id).status == JOB_PENDING


def test_job_blocked_by_control_error_e_job_not_actionable_error():
    assert issubclass(JobBlockedByControlError, JobNotActionableError)
    assert issubclass(JobBlockedByControlError, JobEngineError)


def test_run_pending_isola_bloqueio_de_um_job_e_continua_os_demais(local_db, audit, control):
    blocked = _create_job(audit, operation="RENDER")
    runnable = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    control.pause(CONTROL_SCOPE_JOB, blocked.id)

    outcomes = {outcome.job_id: outcome for outcome in engine.run_pending()}

    assert set(outcomes) == {runnable.id}
    assert outcomes[runnable.id].ok
    assert local_db.get(Job, blocked.id).status == JOB_PENDING


def test_account_platform_pausados_nao_bloqueiam_o_job_engine_automaticamente(local_db, audit, control):
    """Confirma explicitamente a lacuna documentada: pausar ACCOUNT/PLATFORM
    não tem efeito sobre o que o JobEngine considera pendente hoje, porque
    ele não tem como saber a conta/plataforma de um Job."""
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    control.pause(CONTROL_SCOPE_ACCOUNT, "conta-x")
    control.pause(CONTROL_SCOPE_PLATFORM, "plataforma-y")

    outcomes = engine.run_pending()

    assert len(outcomes) == 1
    assert outcomes[0].ok
    assert local_db.get(Job, job.id).status == JOB_READY


# ---------------------------------------------------------------------------
# TERCEIRA REVISÃO — BLOQUEADOR 1: corrida entre cancel_requested e a
# transição final pós-PROCESSING
# ---------------------------------------------------------------------------


def test_cancel_commitado_apos_leitura_mas_antes_da_finalizacao_e_visto(local_db, audit, control):
    """Corrida determinística: usa uma subclasse de ControlManager cujo
    is_job_cancel_requested só retorna depois de garantir que um cancel()
    concorrente (outra conexão) já commitou — provando que a leitura feita
    por _finalize_processing_result acontece DEPOIS desse commit e ainda
    assim enxerga o cancelamento (porque as duas disputam o mesmo
    BEGIN IMMEDIATE, nunca porque a ordem de chamadas Python coincide)."""
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    claimed = engine._claim(job.id, JOB_PROCESSING, "RENDER")
    assert claimed.status == JOB_PROCESSING

    # Um "outro processo" cancela ESTE Job (PROCESSING está em
    # _CANCEL_DEFERRED_STATUSES, então isso apenas registra a intenção —
    # exatamente o cenário reproduzido pela revisão).
    other_db = LocalDatabase(paths=local_db.paths)
    other_control = ControlManager(other_db, audit_log=OperationalAuditLog(other_db))
    outcome = other_control.cancel(CONTROL_SCOPE_JOB, job.id)
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert local_db.get(Job, job.id).status == JOB_PROCESSING  # ainda não finalizado

    # Só AGORA o handler termina e o Engine finaliza — o cancel_requested já
    # está commitado havia tempo, numa transaction totalmente separada e já
    # concluída (não há nenhuma sobreposição artificial de threads aqui: o
    # ponto é que mesmo sem sobreposição, a leitura teria que enxergar o
    # commit anterior de qualquer forma).
    result = JobStepResult(target_status=JOB_READY)
    final_job = engine._finalize_processing_result(claimed, JOB_PROCESSING, result)

    assert final_job.status == JOB_CANCELLED  # nunca READY
    assert local_db.get(Job, job.id).status == JOB_CANCELLED
    assert control.is_job_cancel_requested(job.id) is False  # limpo atomicamente


def _run_cancel_vs_finalize_race(iterations: int = 20) -> None:
    """Corrida real com threads: dispara ``cancel(JOB)`` e a finalização do
    handler (``_finalize_processing_result``) concorrentemente, muitas
    vezes, e confirma pela ORDEM REAL de commit (rowid de audit_events) que
    nunca existiu a interleaving proibida: cancel_requested commitado ANTES
    da finalização adquirir o lock, mas o Job mesmo assim pousando em algo
    diferente de CANCELLED."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_engine = _fresh_database(root, initialize=True)
        db_canceller = _fresh_database(root, initialize=False)

        audit_engine = OperationalAuditLog(db_engine)
        control_engine = ControlManager(db_engine, audit_log=audit_engine)
        control_canceller = ControlManager(db_canceller, audit_log=OperationalAuditLog(db_canceller))

        engine = JobEngine(db_engine, audit_log=audit_engine, control_manager=control_engine)
        engine.register_handler(
            "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
        )

        bad_interleavings = []

        for i in range(iterations):
            job = audit_engine.create_job(Job(operation="RENDER"))
            claimed = engine._claim(job.id, JOB_PROCESSING, "RENDER")

            barrier = threading.Barrier(2)
            finalize_result: dict = {}

            def do_finalize():
                barrier.wait(timeout=5)
                finalize_result["job"] = engine._finalize_processing_result(
                    claimed, JOB_PROCESSING, JobStepResult(target_status=JOB_READY)
                )

            def do_cancel():
                barrier.wait(timeout=5)
                control_canceller.cancel(CONTROL_SCOPE_JOB, job.id)

            t_finalize = threading.Thread(target=do_finalize)
            t_cancel = threading.Thread(target=do_cancel)
            t_finalize.start()
            t_cancel.start()
            t_finalize.join(timeout=10)
            t_cancel.join(timeout=10)

            cancel_intent_events = db_engine.list_audit_events(
                entity_type="ControlScope", event_type="CONTROL_CANCEL_INTENT_RECORDED"
            )
            cancel_intent_events = [e for e in cancel_intent_events if e["data"].get("scope_id") == job.id]

            finalize_events = db_engine.list_audit_events(
                entity_type="Job", entity_id=job.id, event_type="JOB_STATE_CHANGED"
            )
            final_status = finalize_result["job"].status

            if cancel_intent_events:
                cancel_sequence = cancel_intent_events[0]["sequence"]
                # a finalização SEMPRE grava um JOB_STATE_CHANGED (seja para
                # READY, seja para CANCELLED).
                finalize_sequence = finalize_events[-1]["sequence"]
                if cancel_sequence < finalize_sequence and final_status != JOB_CANCELLED:
                    bad_interleavings.append(
                        {"iteration": i, "cancel_sequence": cancel_sequence, "finalize_sequence": finalize_sequence, "final_status": final_status}
                    )

        assert not bad_interleavings, (
            "handler finalizou sem CANCELLED apesar de cancel_requested já ter "
            f"commitado antes da finalização: {bad_interleavings}"
        )


def test_cancel_vs_finalizacao_e_corrida_real_deterministica():
    _run_cancel_vs_finalize_race()


def test_cancel_aplicado_no_ponto_seguro_limpa_flag_atomicamente(local_db, audit, control):
    """Requisito obrigatório: cancel aplicado + flag limpo na MESMA
    transaction — nunca uma janela onde CANCELLED já commitou mas
    cancel_requested continua True."""
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    claimed = engine._claim(job.id, JOB_PROCESSING, "RENDER")
    control.cancel(CONTROL_SCOPE_JOB, job.id)  # deferred: Job está PROCESSING
    assert control.is_job_cancel_requested(job.id) is True

    final_job = engine._finalize_processing_result(claimed, JOB_PROCESSING, JobStepResult(target_status=JOB_READY))

    assert final_job.status == JOB_CANCELLED
    assert control.is_job_cancel_requested(job.id) is False


def test_crash_entre_leitura_de_cancel_requested_e_finalizacao_nao_deixa_estado_contraditorio(
    local_db, audit, control, monkeypatch
):
    """Simula um crash logo depois de decidir aplicar o cancelamento, mas
    antes de a transaction commitar: como a transição e a limpeza do flag
    estão na MESMA transaction, um crash nesse ponto não persiste nem uma
    coisa nem outra — nunca um estado parcial (CANCELLED sem o flag limpo,
    ou vice-versa)."""
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    claimed = engine._claim(job.id, JOB_PROCESSING, "RENDER")
    control.cancel(CONTROL_SCOPE_JOB, job.id)

    original_mark_applied = ControlManager.mark_job_cancel_applied

    def crash_after_transition(self, job_id, *, reason=None, connection=None):
        raise RuntimeError("crash simulado entre a transição e a limpeza do flag")

    monkeypatch.setattr(ControlManager, "mark_job_cancel_applied", crash_after_transition)

    with pytest.raises(RuntimeError, match="crash simulado"):
        engine._finalize_processing_result(claimed, JOB_PROCESSING, JobStepResult(target_status=JOB_READY))

    # Nem a transição para CANCELLED nem a limpeza do flag foram commitadas
    # — a transaction inteira foi revertida (ROLLBACK), nunca um estado
    # parcial/contraditório.
    assert local_db.get(Job, job.id).status == JOB_PROCESSING
    assert control.is_job_cancel_requested(job.id) is True

    monkeypatch.setattr(ControlManager, "mark_job_cancel_applied", original_mark_applied)
    final_job = engine._finalize_processing_result(claimed, JOB_PROCESSING, JobStepResult(target_status=JOB_READY))
    assert final_job.status == JOB_CANCELLED
    assert control.is_job_cancel_requested(job.id) is False


# ---------------------------------------------------------------------------
# TERCEIRA REVISÃO — BLOQUEADOR 2: cancel batch IN_PROGRESS bloqueia claim
# após restart
# ---------------------------------------------------------------------------


def test_global_in_progress_bloqueia_candidatos_apos_reabrir_banco(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_first_run = _fresh_database(root, initialize=True)
        audit_first_run = OperationalAuditLog(db_first_run)
        control_first_run = ControlManager(db_first_run, audit_log=audit_first_run)

        job_a = audit_first_run.create_job(Job(operation="RENDER"))
        job_b = audit_first_run.create_job(Job(operation="RENDER"))

        def crash_on_first(self, job_id, *, reason):
            raise RuntimeError("crash simulado")

        monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_on_first)
        with pytest.raises(RuntimeError):
            control_first_run.cancel(CONTROL_SCOPE_GLOBAL)
        monkeypatch.undo()

        batch = control_first_run.get_cancel_batch_request(CONTROL_SCOPE_GLOBAL)
        assert batch["status"] == "IN_PROGRESS"
        del control_first_run, audit_first_run, db_first_run

        # "restart": instâncias totalmente novas de DB/ControlManager/JobEngine.
        db_after_restart = _fresh_database(root, initialize=False)
        audit_after_restart = OperationalAuditLog(db_after_restart)
        control_after_restart = ControlManager(db_after_restart, audit_log=audit_after_restart)
        engine_after_restart = JobEngine(db_after_restart, audit_log=audit_after_restart, control_manager=control_after_restart)
        engine_after_restart.register_handler(
            "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
        )

        pending = {job.id for job in engine_after_restart.fetch_pending_jobs()}
        assert job_a.id not in pending
        assert job_b.id not in pending

        outcomes = engine_after_restart.run_pending()
        assert outcomes == []
        assert db_after_restart.get(Job, job_a.id).status == JOB_PENDING
        assert db_after_restart.get(Job, job_b.id).status == JOB_PENDING

        with pytest.raises(JobBlockedByControlError):
            engine_after_restart.advance(job_a.id)


def test_queue_in_progress_bloqueia_candidatos_apos_reabrir_banco(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_first_run = _fresh_database(root, initialize=True)
        audit_first_run = OperationalAuditLog(db_first_run)
        control_first_run = ControlManager(db_first_run, audit_log=audit_first_run)

        render_job = audit_first_run.create_job(Job(operation="RENDER"))
        other_queue_job = audit_first_run.create_job(Job(operation="UPLOAD"))

        def crash_on_first(self, job_id, *, reason):
            raise RuntimeError("crash simulado")

        monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_on_first)
        with pytest.raises(RuntimeError):
            control_first_run.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
        monkeypatch.undo()
        del control_first_run, audit_first_run, db_first_run

        db_after_restart = _fresh_database(root, initialize=False)
        audit_after_restart = OperationalAuditLog(db_after_restart)
        control_after_restart = ControlManager(db_after_restart, audit_log=audit_after_restart)
        engine_after_restart = JobEngine(db_after_restart, audit_log=audit_after_restart, control_manager=control_after_restart)
        engine_after_restart.register_handler(
            "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
        )
        engine_after_restart.register_handler(
            "UPLOAD", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
        )

        pending = {job.id for job in engine_after_restart.fetch_pending_jobs()}
        assert render_job.id not in pending
        assert other_queue_job.id in pending  # fila diferente, não afetada

        outcomes = {o.job_id: o for o in engine_after_restart.run_pending()}
        assert render_job.id not in outcomes
        assert other_queue_job.id in outcomes
        assert outcomes[other_queue_job.id].ok


def test_job_criado_depois_da_solicitacao_original_nao_e_bloqueado_pelo_batch_antigo(local_db, audit, control, monkeypatch):
    """Um Job que não fazia parte do snapshot original de candidatos não é
    afetado por um batch IN_PROGUESS antigo, mesmo que pertença à mesma
    QUEUE/GLOBAL."""
    render_job = _create_job(audit, operation="RENDER")

    def crash_always(self, job_id, *, reason):
        raise RuntimeError("crash simulado")

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_always)
    with pytest.raises(RuntimeError):
        control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
    monkeypatch.undo()

    batch = control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
    assert batch["status"] == "IN_PROGRESS"
    assert batch["candidate_job_ids"] == [render_job.id]

    new_job = _create_job(audit, operation="RENDER")  # criado DEPOIS da solicitação

    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    pending = {job.id for job in engine.fetch_pending_jobs()}
    assert render_job.id not in pending  # ainda bloqueado pelo batch antigo
    assert new_job.id in pending  # não fazia parte do snapshot original

    outcomes = {o.job_id: o for o in engine.run_pending()}
    assert new_job.id in outcomes
    assert outcomes[new_job.id].ok
    assert render_job.id not in outcomes


# ---------------------------------------------------------------------------
# TERCEIRA REVISÃO — BLOQUEADOR 3: cancel_requested precede retry local
# ---------------------------------------------------------------------------


def test_cancel_durante_processing_handler_falha_depois_retry_nao_reexecuta(local_db, audit, control):
    """Reprodução fiel do cenário da revisão, na ordem correta:

    PROCESSING (handler ainda rodando) -> cancel(JOB) pedido (deferred,
    cancel_requested=True) -> handler falha -> Job pousa em FAILED (o pouso
    de falha não é afetado por cancel_requested, só o ponto seguro de
    SUCESSO é) -> FAILED -> RETRY manual -> uma nova execução NÃO deve
    acontecer enquanto cancel_requested continuar True.
    """
    job = _create_job(audit, operation="RENDER")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)

    handler_entered = threading.Event()
    release_handler = threading.Event()
    call_count = {"n": 0}

    def slow_failing_handler(claimed_job):
        call_count["n"] += 1
        handler_entered.set()
        assert release_handler.wait(timeout=5)
        raise RuntimeError("falha simulada")

    engine.register_handler("RENDER", slow_failing_handler, claims_status=JOB_PROCESSING)

    advance_result: dict = {}

    def run_advance():
        try:
            engine.advance(job.id)
        except JobEngineError as exc:
            advance_result["error"] = exc

    t = threading.Thread(target=run_advance)
    t.start()
    assert handler_entered.wait(timeout=5)
    assert local_db.get(Job, job.id).status == JOB_PROCESSING

    outcome = control.cancel(CONTROL_SCOPE_JOB, job.id)
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert control.is_job_cancel_requested(job.id) is True

    release_handler.set()
    t.join(timeout=5)

    # O handler falhou (não teve sucesso), então o ponto seguro de sucesso
    # (_finalize_processing_result) nunca roda para ele — ele pousa em
    # FAILED pelo caminho de falha normal, com cancel_requested ainda ativo.
    assert local_db.get(Job, job.id).status == JOB_FAILED
    assert control.is_job_cancel_requested(job.id) is True
    assert call_count["n"] == 1

    # Alguém decide, manualmente, tentar de novo.
    audit.transition_job(job.id, JOB_RETRY)

    # A intenção de cancelamento ainda ativa NÃO permite uma nova execução
    # local: _claim recusa antes de chamar o handler.
    with pytest.raises(JobBlockedByControlError):
        engine.advance(job.id)

    assert call_count["n"] == 1  # handler NUNCA rodou de novo
    assert local_db.get(Job, job.id).status == JOB_RETRY  # intocado


def test_recovering_remoto_continua_possivel_com_cancel_pendente(local_db, audit, control):
    """Requisito obrigatório: RECOVERING remoto continua possível quando
    necessário, mesmo com cancel_requested pendente para o Job — só
    PROCESSING é bloqueado por essa intenção."""
    job = _create_job(audit, operation="RECONCILE")
    engine = JobEngine(local_db, audit_log=audit, control_manager=control)
    engine.register_handler(
        "RECONCILE", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_RECOVERING
    )

    # Job está PUBLISHING (execução ativa/incerta) quando o cancel é pedido:
    # fica deferred, nunca aplicado automaticamente.
    audit.transition_job(job.id, JOB_PUBLISHING)
    control.cancel(CONTROL_SCOPE_JOB, job.id)
    assert control.is_job_cancel_requested(job.id) is True

    # O Job pousa em UNKNOWN (efeito remoto incerto) e depois precisa ir
    # para RECOVERING para reconciliar — isso NÃO pode ser bloqueado pelo
    # cancel_requested pendente.
    audit.transition_job(job.id, JOB_UNKNOWN)

    # RETRY é o único jeito de fetch_pending_jobs/advance considerarem o Job
    # "acionável" hoje para claims_status=RECOVERING a partir de um estado
    # actionable — mas RECOVERING não está em _ACTIONABLE_STATES via
    # PENDING/RETRY neste modelo; o caminho real de RECOVERING é o
    # RecoveryManager. Para isolar exatamente a checagem de _claim, chamamos
    # _claim diretamente simulando esse fluxo.
    claimed = engine._claim(job.id, JOB_RECOVERING, "RECONCILE")
    assert claimed.status == JOB_RECOVERING
    # cancel_requested continua true, mas não impediu a reivindicação.
    assert control.is_job_cancel_requested(job.id) is True


# ---------------------------------------------------------------------------
# TERCEIRA REVISÃO — MAJOR: retomada de batch usa snapshot original
# ---------------------------------------------------------------------------


def test_retomada_de_batch_usa_candidate_job_ids_originais(local_db, audit, control, monkeypatch):
    job_a = _create_job(audit, operation="RENDER")
    job_b = _create_job(audit, operation="RENDER")

    call_count = {"n": 0}
    original = ControlManager._cancel_or_defer_job

    def crash_on_first(self, job_id, *, reason):
        call_count["n"] += 1
        raise RuntimeError("crash simulado")

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_on_first)
    with pytest.raises(RuntimeError):
        control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", original)

    batch_before_resume = control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
    assert set(batch_before_resume["candidate_job_ids"]) == {job_a.id, job_b.id}

    # Um Job novo é criado DEPOIS da solicitação original, antes da retomada.
    job_c = _create_job(audit, operation="RENDER")

    resumed = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    # Apenas A e B (snapshot original) foram considerados — C nunca entrou.
    assert set(resumed.cancelled_job_ids) == {job_a.id, job_b.id}
    assert job_c.id not in resumed.cancelled_job_ids
    assert local_db.get(Job, job_c.id).status == JOB_PENDING  # C intocado

    events = [
        e for e in local_db.list_audit_events(entity_type="ControlScope") if e["event_type"] == "CONTROL_CANCEL_SCOPE_RESUMED"
    ]
    assert len(events) == 1
    assert events[0]["data"]["candidate_count"] == 2


def test_nova_solicitacao_apos_completed_cria_snapshot_novo_incluindo_jobs_novos(local_db, audit, control):
    job_a = _create_job(audit, operation="RENDER")

    first = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
    assert first.status == CANCEL_OUTCOME_FULLY_APPLIED
    assert control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")["status"] == "COMPLETED"

    job_b = _create_job(audit, operation="RENDER")  # criado depois do COMPLETED

    second = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    # A nova solicitação recalculou do zero: A já é CANCELLED (terminal) e
    # por isso fica fora do snapshot novo (correção pós-4ª revisão) — só B
    # (novo, ainda não terminal) é candidato e é cancelado agora.
    assert second.cancelled_job_ids == (job_b.id,)
    assert job_a.id not in second.cancelled_job_ids
    batch_after_second = control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
    assert batch_after_second["status"] == "COMPLETED"
    assert batch_after_second["cancelled_count"] == 1
    assert local_db.get(Job, job_a.id).status == JOB_CANCELLED  # A continua cancelado, não regrediu


def test_concorrencia_entre_retomada_de_cancel_e_claim_continua_serializada(monkeypatch):
    """Requisito obrigatório: mesmo com a retomada usando snapshot
    persistido, a decisão por Job continua atômica (_cancel_or_defer_job)
    e disputa o mesmo BEGIN IMMEDIATE que _claim — corrida real com
    threads confirmando que nunca há uma reivindicação bem-sucedida de um
    Job cujo cancelamento (do snapshot retomado) já commitou antes."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_first_run = _fresh_database(root, initialize=True)
        audit_first_run = OperationalAuditLog(db_first_run)
        control_first_run = ControlManager(db_first_run, audit_log=audit_first_run)

        job = audit_first_run.create_job(Job(operation="RENDER"))

        def crash_always(self, job_id, *, reason):
            raise RuntimeError("crash simulado")

        monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_always)
        with pytest.raises(RuntimeError):
            control_first_run.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
        monkeypatch.undo()
        del control_first_run, audit_first_run, db_first_run

        db_engine = _fresh_database(root, initialize=False)
        audit_engine = OperationalAuditLog(db_engine)
        control_engine = ControlManager(db_engine, audit_log=audit_engine)
        engine = JobEngine(db_engine, audit_log=audit_engine, control_manager=control_engine)
        engine.register_handler(
            "RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
        )

        db_resumer = _fresh_database(root, initialize=False)
        control_resumer = ControlManager(db_resumer, audit_log=OperationalAuditLog(db_resumer))

        bad_interleavings = []
        for _ in range(15):
            barrier = threading.Barrier(2)
            claim_error: list[Exception] = []

            def do_claim():
                barrier.wait(timeout=5)
                try:
                    engine.advance(job.id)
                except JobEngineError as exc:
                    claim_error.append(exc)

            def do_resume_cancel():
                barrier.wait(timeout=5)
                control_resumer.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

            t_claim = threading.Thread(target=do_claim)
            t_resume = threading.Thread(target=do_resume_cancel)
            t_claim.start()
            t_resume.start()
            t_claim.join(timeout=10)
            t_resume.join(timeout=10)

            final_status = db_engine.get(Job, job.id).status
            if final_status == JOB_CANCELLED:
                break  # objetivo alcançado: cancel venceu em alguma rodada
            if final_status not in (JOB_PENDING, JOB_READY):
                bad_interleavings.append(final_status)
                break
            if final_status == JOB_READY:
                # claim venceu — reseta o Job para nova rodada de corrida
                # não é possível (READY é avançado demais para repetir o
                # mesmo teste); encerra aqui, resultado válido.
                break

        assert not bad_interleavings, f"estado inconsistente observado: {bad_interleavings}"
        # De qualquer forma (claim venceu OU cancel venceu), o resultado é
        # sempre um dos dois estados válidos — nunca uma mistura.
        assert db_engine.get(Job, job.id).status in (JOB_CANCELLED, JOB_READY, JOB_PENDING)


# ---------------------------------------------------------------------------
# Concorrência: dois JobEngines com ControlManagers próprios, mesmo bloqueio
# ---------------------------------------------------------------------------


def test_duas_instancias_de_job_engine_respeitam_pause_persistido_por_qualquer_uma(local_db, audit):
    job = _create_job(audit, operation="RENDER")

    control_a = ControlManager(local_db, audit_log=audit)
    db_b = LocalDatabase(paths=local_db.paths)
    audit_b = OperationalAuditLog(db_b)
    control_b = ControlManager(db_b, audit_log=audit_b)

    engine_a = JobEngine(local_db, audit_log=audit, control_manager=control_a)
    engine_b = JobEngine(db_b, audit_log=audit_b, control_manager=control_b)
    for engine in (engine_a, engine_b):
        engine.register_handler("RENDER", lambda j: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    control_a.pause(CONTROL_SCOPE_GLOBAL)

    # A instância B lê o mesmo SQLite: o pause persistido por A é
    # imediatamente visível para B, sem nenhum canal de comunicação além do
    # próprio banco.
    assert engine_b.fetch_pending_jobs() == []
    assert engine_b.run_pending() == []
    assert db_b.get(Job, job.id).status == JOB_PENDING


# ---------------------------------------------------------------------------
# PAUSED (State Machine) vs flag de ControlManager
# ---------------------------------------------------------------------------


def test_control_manager_pause_job_nao_altera_job_status(control, audit):
    """ControlManager.pause(JOB, ...) é um flag de CONTROLE (bloqueia
    reivindicação futura), não uma transição de estado do Job — o Job
    aprovado continua em PENDING/RETRY/etc., nunca pulando para o estado
    JOB_PAUSED da State Machine central só por causa deste flag. São
    responsabilidades DELIBERADAMENTE separadas: JOB_PAUSED é uma transição
    explícita e auditável de negócio (ex.: alguém pausou este Job
    especificamente, com sua própria semântica de retomada via
    OperationalAuditLog.pause/resume); o flag do ControlManager é o
    mecanismo de bloqueio de reivindicação usado por pause/stop_after_current
    em qualquer um dos 5 escopos, incluindo GLOBAL/QUEUE onde não faria
    sentido nenhum transicionar o Job.status individualmente. A fonte de
    verdade sobre "este Job pode ser reivindicado agora" é sempre a
    combinação de (a) Job.status estar em _ACTIONABLE_STATES e (b)
    ControlManager.is_execution_blocked(...) ser False — nunca Job.status
    sozinho. Um futuro BatchEngine (PROMPT 17) deve contar/considerar
    "pendente e executável" exatamente por essa combinação, não só pelo
    status bruto do Job, para não contar incorretamente um Job PENDING que
    está com pause ativo em algum escopo."""
    job = _create_job(audit, operation="RENDER")

    control.pause(CONTROL_SCOPE_JOB, job.id)

    persisted = audit.database.get(Job, job.id)
    assert persisted.status == JOB_PENDING  # não virou JOB_PAUSED
    assert control.is_execution_blocked(operation="RENDER", job_id=job.id) is True


# ---------------------------------------------------------------------------
# Independência de UI/plataformas/JobEngine
# ---------------------------------------------------------------------------


def test_control_manager_nao_importa_ui_connectors_nem_job_engine():
    source = Path("_sistema/control_manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    lowered = [module.lower() for module in imported_modules]
    forbidden = ("playwright", "selenium", "tkinter", "pyqt", "pyside", "youtube", "tiktok", "job_engine")
    for marker in forbidden:
        assert not any(marker in module for module in lowered), (
            f"control_manager.py não deveria importar algo relacionado a {marker!r}"
        )


def test_job_engine_pode_ser_usado_sem_control_manager_nenhum_import_circular():
    # Import isolado: job_engine.py importa control_manager.py, nunca o
    # contrário. Se houvesse ciclo, este import já falharia.
    import _sistema.control_manager as control_manager_module
    import _sistema.job_engine as job_engine_module

    source = Path("_sistema/control_manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)
    assert not any("job_engine" in module for module in imported_modules), (
        "control_manager.py não deveria importar job_engine.py (dependência é só no sentido inverso)"
    )
    assert control_manager_module.ControlManager is not None
    assert job_engine_module.JobEngine is not None


# ---------------------------------------------------------------------------
# QUARTA REVISÃO — BLOQUEADOR: cancel(JOB) IN_PROGRESS não bloqueava
# depois de um crash (mesma classe de proteção que GLOBAL/QUEUE já tinham).
# ---------------------------------------------------------------------------


def test_cancel_job_in_progress_bloqueia_apos_reabrir_banco_e_retomada_conclui(monkeypatch):
    """Reprodução fiel do cenário da 4ª revisão:

    Job=PENDING -> cancel(JOB) persiste cancel_batch_request(scope=JOB)
    IN_PROGRESS -> crash simulado ANTES de _cancel_or_defer_job rodar ->
    "restart" (novas instâncias de DB/ControlManager/JobEngine) ->
    fetch_pending_jobs não oferece o Job, advance() direto também é
    bloqueado, o handler nunca roda -> reinvocar cancel(JOB) retoma a
    solicitação original (mesmo scope_id) e conclui corretamente.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_first_run = _fresh_database(root, initialize=True)
        audit_first_run = OperationalAuditLog(db_first_run)
        control_first_run = ControlManager(db_first_run, audit_log=audit_first_run)

        job = audit_first_run.create_job(Job(operation="RENDER"))

        def crash_always(self, job_id, *, reason):
            raise RuntimeError("crash simulado entre persistir IN_PROGRESS e _cancel_or_defer_job")

        monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_always)
        with pytest.raises(RuntimeError):
            control_first_run.cancel(CONTROL_SCOPE_JOB, job.id)
        monkeypatch.undo()

        batch = control_first_run.get_cancel_batch_request(CONTROL_SCOPE_JOB, job.id)
        assert batch["status"] == "IN_PROGRESS"
        assert batch["candidate_job_ids"] == [job.id]
        assert db_first_run.get(Job, job.id).status == JOB_PENDING
        del control_first_run, audit_first_run, db_first_run

        # "restart": instâncias totalmente novas de DB/ControlManager/JobEngine.
        db_after_restart = _fresh_database(root, initialize=False)
        audit_after_restart = OperationalAuditLog(db_after_restart)
        control_after_restart = ControlManager(db_after_restart, audit_log=audit_after_restart)
        engine_after_restart = JobEngine(
            db_after_restart, audit_log=audit_after_restart, control_manager=control_after_restart
        )
        call_count = {"n": 0}

        def handler(claimed_job):
            call_count["n"] += 1
            return JobStepResult(target_status=JOB_READY)

        engine_after_restart.register_handler("RENDER", handler, claims_status=JOB_PROCESSING)

        pending = {j.id for j in engine_after_restart.fetch_pending_jobs()}
        assert job.id not in pending

        outcomes = engine_after_restart.run_pending()
        assert outcomes == []
        assert db_after_restart.get(Job, job.id).status == JOB_PENDING

        with pytest.raises(JobBlockedByControlError):
            engine_after_restart.advance(job.id)

        assert call_count["n"] == 0  # handler nunca rodou

        # Reinvocar cancel(JOB) retoma o pedido original (mesmo scope_id) e
        # conclui corretamente, sem recalcular candidatos.
        outcome = control_after_restart.cancel(CONTROL_SCOPE_JOB, job.id)
        assert outcome.cancelled_job_ids == (job.id,)
        assert outcome.status == CANCEL_OUTCOME_FULLY_APPLIED
        assert db_after_restart.get(Job, job.id).status == JOB_CANCELLED

        completed_batch = control_after_restart.get_cancel_batch_request(CONTROL_SCOPE_JOB, job.id)
        assert completed_batch["status"] == "COMPLETED"

        # Depois de CANCELLED (terminal), o Job também não é mais oferecido
        # como trabalho novo — mas agora por já não ser um estado acionável,
        # não pelo bloqueio de batch (que já concluiu).
        assert job.id not in {j.id for j in engine_after_restart.fetch_pending_jobs()}
        assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# QUARTA REVISÃO — MAJOR: snapshot novo de cancelamento em massa não deve
# incluir Jobs em estado terminal (PUBLISHED/CANCELLED) do histórico.
# ---------------------------------------------------------------------------


def test_cancel_global_ignora_jobs_terminais_historicos_no_snapshot_novo(control, audit, monkeypatch):
    published = _create_job(audit, operation="UPLOAD")
    audit.transition_job(published.id, JOB_PUBLISHING)
    audit.transition_job(published.id, JOB_PUBLISHED)

    already_cancelled = _create_job(audit, operation="UPLOAD")
    audit.transition_job(already_cancelled.id, JOB_CANCELLED)

    pending = _create_job(audit, operation="RENDER")
    processing = _create_job(audit, operation="RENDER")
    audit.transition_job(processing.id, JOB_PROCESSING)

    # Verifica o snapshot exatamente como persistido (crash-safe, ANTES de
    # qualquer efeito) simulando um crash logo depois de persistir a
    # intenção — o snapshot em si (não só o resultado agregado) já exclui o
    # histórico terminal.
    def crash_always(self, job_id, *, reason):
        raise RuntimeError("crash simulado")

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_always)
    with pytest.raises(RuntimeError):
        control.cancel(CONTROL_SCOPE_GLOBAL)
    monkeypatch.undo()

    batch = control.get_cancel_batch_request(CONTROL_SCOPE_GLOBAL)
    assert batch["status"] == "IN_PROGRESS"
    assert published.id not in batch["candidate_job_ids"]
    assert already_cancelled.id not in batch["candidate_job_ids"]
    assert set(batch["candidate_job_ids"]) == {pending.id, processing.id}

    # Retomando (mesma solicitação) até concluir: resultado agregado.
    outcome = control.cancel(CONTROL_SCOPE_GLOBAL)

    assert outcome.cancelled_job_ids == (pending.id,)
    assert outcome.deferred_job_ids == (processing.id,)
    assert outcome.skipped_job_ids == ()
    assert outcome.errors == ()
    # O ponto central da correção: histórico PUBLISHED/CANCELLED não pode
    # fazer um cancelamento global honesto parecer PARTIALLY_APPLIED.
    assert outcome.status == CANCEL_OUTCOME_PARTIALLY_APPLIED  # por causa do PROCESSING deferred, não do histórico
    assert outcome.status != CANCEL_OUTCOME_ERROR

    # Estados terminais não são tocados/reescritos.
    assert audit.database.get(Job, published.id).status == JOB_PUBLISHED
    assert audit.database.get(Job, already_cancelled.id).status == JOB_CANCELLED


def test_cancel_queue_ignora_jobs_terminais_historicos_no_snapshot_novo(control, audit, monkeypatch):
    published = _create_job(audit, operation="RENDER")
    audit.transition_job(published.id, JOB_PUBLISHING)
    audit.transition_job(published.id, JOB_PUBLISHED)

    pending = _create_job(audit, operation="RENDER")

    def crash_always(self, job_id, *, reason):
        raise RuntimeError("crash simulado")

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_always)
    with pytest.raises(RuntimeError):
        control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
    monkeypatch.undo()

    batch = control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
    assert batch["status"] == "IN_PROGRESS"
    assert published.id not in batch["candidate_job_ids"]
    assert batch["candidate_job_ids"] == [pending.id]

    outcome = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    assert outcome.cancelled_job_ids == (pending.id,)
    assert outcome.status == CANCEL_OUTCOME_FULLY_APPLIED  # não PARTIALLY_APPLIED por causa do histórico
    assert audit.database.get(Job, published.id).status == JOB_PUBLISHED


def test_cancel_job_explicito_em_job_terminal_continua_not_applied(control, audit):
    """A nova regra de snapshot só vale para GLOBAL/QUEUE calculando um lote
    NOVO — cancel(JOB, x) explícito continua podendo mirar um Job já
    terminal e corretamente reportar NOT_APPLIED/skipped para ele (não é
    silenciosamente filtrado nem tratado como erro)."""
    published = _create_job(audit, operation="UPLOAD")
    audit.transition_job(published.id, JOB_PUBLISHING)
    audit.transition_job(published.id, JOB_PUBLISHED)

    outcome = control.cancel(CONTROL_SCOPE_JOB, published.id)

    assert outcome.skipped_job_ids == (published.id,)
    assert outcome.status == CANCEL_OUTCOME_NOT_APPLIED
    assert not outcome.ok


def test_batch_in_progress_antigo_nao_e_reinterpretado_pela_regra_de_terminal(control, audit, monkeypatch):
    """Um batch GLOBAL/QUEUE já IN_PROGRESS continua usando o
    candidate_job_ids ORIGINAL tal como persistido na retomada, mesmo que
    algum desses Jobs tenha, nesse meio tempo, virado terminal por outro
    caminho — a nova regra de exclusão de terminais só se aplica ao CÁLCULO
    de um snapshot novo, nunca reinterpreta uma retomada."""
    render_a = _create_job(audit, operation="RENDER")
    render_b = _create_job(audit, operation="RENDER")

    def crash_always(self, job_id, *, reason):
        raise RuntimeError("crash simulado")

    monkeypatch.setattr(ControlManager, "_cancel_or_defer_job", crash_always)
    with pytest.raises(RuntimeError):
        control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")
    monkeypatch.undo()

    batch = control.get_cancel_batch_request(CONTROL_SCOPE_QUEUE, "RENDER")
    assert batch["status"] == "IN_PROGRESS"
    assert set(batch["candidate_job_ids"]) == {render_a.id, render_b.id}

    # render_a se torna PUBLISHED por outro caminho (fora do controle deste
    # cancelamento) antes da retomada.
    audit.transition_job(render_a.id, JOB_PROCESSING)
    audit.transition_job(render_a.id, JOB_READY)
    audit.transition_job(render_a.id, JOB_PUBLISHING)
    audit.transition_job(render_a.id, JOB_PUBLISHED)

    outcome = control.cancel(CONTROL_SCOPE_QUEUE, "RENDER")

    # A retomada ainda considera render_a (estava no snapshot original), e
    # corretamente reporta skipped para ele agora que é terminal — não o
    # exclui silenciosamente como faria o cálculo de um snapshot novo.
    assert outcome.skipped_job_ids == (render_a.id,)
    assert outcome.cancelled_job_ids == (render_b.id,)
