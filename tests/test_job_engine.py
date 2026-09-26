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
    AUDIT_JOB_ERROR,
    AUDIT_JOB_PROCESSING_COMPLETED,
    AUDIT_JOB_STATE_CHANGED,
    LocalDatabase,
    OperationalAuditLog,
)
from _sistema.job_engine import (
    CLAIMABLE_STATES,
    JobEngine,
    JobEngineError,
    JobHandlerError,
    JobNotActionableError,
    JobNotFoundError,
    JobStepResult,
    UnknownJobOperationError,
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
def engine(local_db, audit):
    return JobEngine(local_db, audit_log=audit)


def _create_job(audit, *, operation="RENDER"):
    job = Job(operation=operation)
    return audit.create_job(job)


def _counting_handler(calls, *, target_status=JOB_READY):
    def handler(current):
        calls.append(current.status)
        return JobStepResult(target_status=target_status)

    return handler


# ---------------------------------------------------------------------------
# Leitura básica no SQLite
# ---------------------------------------------------------------------------


def test_get_job_busca_no_sqlite(engine, audit):
    job = _create_job(audit)

    fetched = engine.get_job(job.id)

    assert fetched is not None
    assert fetched.id == job.id
    assert fetched.status == JOB_PENDING


def test_get_job_inexistente_retorna_none(engine):
    assert engine.get_job(str(uuid.uuid4())) is None


def test_fetch_pending_jobs_encontra_pending_e_retry(engine, audit, local_db):
    first = _create_job(audit, operation="A")
    second = _create_job(audit, operation="B")
    third = _create_job(audit, operation="C")
    audit.transition_job(third.id, JOB_PROCESSING)
    audit.transition_job(third.id, JOB_FAILED)
    audit.transition_job(third.id, JOB_RETRY)

    pending = engine.fetch_pending_jobs()

    # created_at tem resolução de segundos (ver _sistema/time_utils.py), então
    # Jobs criados no mesmo segundo não têm ordem relativa garantida por
    # LocalDatabase.list(); o contrato deste teste é conjunto encontrado, não
    # ordem exata de desempate.
    assert {job.id for job in pending} == {first.id, second.id, third.id}
    assert all(job.status in (JOB_PENDING, JOB_RETRY) for job in pending)


def test_fetch_pending_jobs_ignora_jobs_em_estados_nao_acionaveis(engine, audit):
    pending_job = _create_job(audit, operation="A")
    other = _create_job(audit, operation="B")
    audit.transition_job(other.id, JOB_PROCESSING)

    pending = engine.fetch_pending_jobs()

    assert [job.id for job in pending] == [pending_job.id]


def test_fetch_pending_jobs_ignora_job_failed(engine, audit):
    """FAILED nunca é tratado como trabalho pendente: sair dele exige uma
    decisão explícita (RETRY/CANCELLED), nunca um novo advance() automático.
    """
    job = _create_job(audit, operation="A")
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.transition_job(job.id, JOB_FAILED)

    assert engine.fetch_pending_jobs() == []


def test_fetch_pending_jobs_respeita_limit(engine, audit):
    _create_job(audit, operation="A")
    _create_job(audit, operation="B")

    pending = engine.fetch_pending_jobs(limit=1)

    assert len(pending) == 1


def test_fetch_pending_jobs_rejeita_limit_negativo(engine):
    with pytest.raises(ValueError):
        engine.fetch_pending_jobs(limit=-1)


# ---------------------------------------------------------------------------
# Registro de handler
# ---------------------------------------------------------------------------


def test_register_handler_rejeita_operation_vazia(engine):
    with pytest.raises(ValueError):
        engine.register_handler("", lambda current: None, claims_status=JOB_PROCESSING)


def test_register_handler_rejeita_nao_chamavel(engine):
    with pytest.raises(TypeError):
        engine.register_handler("RENDER", "nao-e-chamavel", claims_status=JOB_PROCESSING)


def test_register_handler_rejeita_claims_status_fora_do_vocabulario(engine):
    with pytest.raises(ValueError):
        engine.register_handler("RENDER", lambda current: None, claims_status=JOB_PENDING)
    with pytest.raises(ValueError):
        engine.register_handler("RENDER", lambda current: None, claims_status=JOB_READY)


def test_claimable_states_sao_todas_estados_de_trabalho_ativo():
    assert CLAIMABLE_STATES == {JOB_PROCESSING, JOB_PUBLISHING, JOB_RECOVERING}


def test_unregister_handler_remove_execucao_futura(engine, audit):
    job = _create_job(audit, operation="RENDER")
    engine.register_handler(
        "RENDER", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )
    engine.unregister_handler("RENDER")

    with pytest.raises(UnknownJobOperationError):
        engine.advance(job.id)


# ---------------------------------------------------------------------------
# advance(): casos básicos de sucesso
# ---------------------------------------------------------------------------


def test_advance_job_inexistente_levanta_erro(engine):
    with pytest.raises(JobNotFoundError):
        engine.advance(str(uuid.uuid4()))


def test_advance_sem_handler_registrado_levanta_erro(engine, audit):
    job = _create_job(audit, operation="RENDER")

    with pytest.raises(UnknownJobOperationError):
        engine.advance(job.id)


def test_advance_reivindica_antes_de_chamar_handler_e_persiste_resultado_final(engine, audit, local_db):
    job = _create_job(audit, operation="RENDER")
    calls = []
    engine.register_handler("RENDER", _counting_handler(calls), claims_status=JOB_PROCESSING)

    updated = engine.advance(job.id)

    # O handler recebeu o Job já reivindicado (persistido em PROCESSING),
    # nunca a cópia "crua" em PENDING.
    assert calls == [JOB_PROCESSING]
    assert updated.status == JOB_READY
    assert local_db.get(Job, job.id).status == JOB_READY

    events = [event["event_type"] for event in audit.list_job_events(job.id)]
    # CREATED, STATE_CHANGED (reivindicação PENDING->PROCESSING),
    # STATE_CHANGED (resultado final PROCESSING->READY).
    assert events.count(AUDIT_JOB_STATE_CHANGED) == 2


def test_advance_aceita_semantic_event_do_handler(engine, audit):
    job = _create_job(audit, operation="RENDER")
    engine.register_handler(
        "RENDER",
        lambda current: JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
        ),
        claims_status=JOB_PROCESSING,
    )

    updated = engine.advance(job.id)

    assert updated.status == JOB_READY
    events = [event["event_type"] for event in audit.list_job_events(job.id)]
    assert events[-1] == AUDIT_JOB_PROCESSING_COMPLETED


def test_advance_funciona_a_partir_de_retry(engine, audit):
    job = _create_job(audit, operation="RENDER")
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.transition_job(job.id, JOB_FAILED)
    audit.transition_job(job.id, JOB_RETRY)

    engine.register_handler(
        "RENDER", lambda current: JobStepResult(target_status=JOB_PUBLISHED), claims_status=JOB_PUBLISHING
    )
    updated = engine.advance(job.id)

    assert updated.status == JOB_PUBLISHED


# ---------------------------------------------------------------------------
# BLOQUEADOR 1 — handler não pode executar em estado não executável
# ---------------------------------------------------------------------------


def test_job_cancelled_nao_executa_handler(engine, audit):
    job = _create_job(audit, operation="RENDER")
    cancelled = audit.cancel(job.id)
    assert cancelled.status == JOB_CANCELLED

    calls = []
    engine.register_handler("RENDER", _counting_handler(calls), claims_status=JOB_PROCESSING)

    with pytest.raises(JobNotActionableError):
        engine.advance(job.id)

    assert calls == []
    assert audit.database.get(Job, job.id).status == JOB_CANCELLED


@pytest.mark.parametrize(
    ("setup_status", "operation"),
    [
        (JOB_CANCELLED, "RENDER"),
        (JOB_PUBLISHED, "PUBLISH"),
    ],
)
def test_estado_terminal_nao_executa_handler(engine, audit, setup_status, operation):
    job = _create_job(audit, operation=operation)
    if setup_status == JOB_PUBLISHED:
        audit.transition_job(job.id, JOB_PUBLISHING)
        audit.transition_job(job.id, JOB_PUBLISHED)
    else:
        audit.transition_job(job.id, setup_status)
    assert audit.database.get(Job, job.id).status == setup_status

    calls = []
    engine.register_handler(operation, _counting_handler(calls), claims_status=JOB_PROCESSING)

    with pytest.raises(JobNotActionableError):
        engine.advance(job.id)

    assert calls == []
    assert audit.database.get(Job, job.id).status == setup_status


def test_estado_incompativel_com_claims_status_nao_executa_handler(engine, audit):
    """READY não pode reivindicar PROCESSING (não é aresta válida da State
    Machine): o handler não deve rodar mesmo sem o Job ser terminal.
    """
    job = _create_job(audit, operation="RENDER")
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.transition_job(job.id, JOB_READY)

    calls = []
    engine.register_handler("RENDER", _counting_handler(calls), claims_status=JOB_PROCESSING)

    with pytest.raises(JobNotActionableError):
        engine.advance(job.id)

    assert calls == []
    assert audit.database.get(Job, job.id).status == JOB_READY


# ---------------------------------------------------------------------------
# BLOQUEADOR 2 — concorrência não pode duplicar execução
# ---------------------------------------------------------------------------


def test_duas_instancias_concorrentes_nao_executam_o_mesmo_job_duas_vezes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")

        db_a = LocalDatabase(paths=paths)
        db_a.initialize()
        db_b = LocalDatabase(paths=paths)  # simula um segundo processo/engine contra o mesmo SQLite

        audit_a = OperationalAuditLog(db_a)
        audit_b = OperationalAuditLog(db_b)
        engine_a = JobEngine(db_a, audit_log=audit_a)
        engine_b = JobEngine(db_b, audit_log=audit_b)

        job = _create_job(audit_a, operation="RENDER")

        call_count = 0
        call_lock = threading.Lock()

        def handler(current):
            nonlocal call_count
            with call_lock:
                call_count += 1
            return JobStepResult(target_status=JOB_READY)

        engine_a.register_handler("RENDER", handler, claims_status=JOB_PROCESSING)
        engine_b.register_handler("RENDER", handler, claims_status=JOB_PROCESSING)

        barrier = threading.Barrier(2)
        results: dict[str, tuple[str, object]] = {}

        def run(name, target_engine):
            barrier.wait()
            try:
                results[name] = ("ok", target_engine.advance(job.id))
            except JobEngineError as exc:
                results[name] = ("error", exc)

        t1 = threading.Thread(target=run, args=("a", engine_a))
        t2 = threading.Thread(target=run, args=("b", engine_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert set(results) == {"a", "b"}
        # O handler com efeitos só pode ter rodado uma vez, não importa a
        # ordem de chegada das duas threads.
        assert call_count == 1

        kinds = sorted(kind for kind, _ in results.values())
        assert kinds == ["error", "ok"]

        error = next(value for kind, value in results.values() if kind == "error")
        assert isinstance(error, JobNotActionableError)

        succeeded = next(value for kind, value in results.values() if kind == "ok")
        assert succeeded.status == JOB_READY
        assert db_a.get(Job, job.id).status == JOB_READY

        # O audit trail continua reconstruível e consistente após a corrida:
        # exatamente uma reivindicação (PENDING->PROCESSING) e uma transição
        # final (PROCESSING->READY), nunca duas.
        history = audit_a.reconstruct_job_history(job.id)
        assert history.state_path == (JOB_PENDING, JOB_PROCESSING, JOB_READY)


# ---------------------------------------------------------------------------
# BLOQUEADOR 3 — falha após reivindicação não pode virar repetição cega
# ---------------------------------------------------------------------------


def test_excecao_do_handler_marca_job_failed_em_vez_de_repeticao_cega(engine, audit, local_db):
    job = _create_job(audit, operation="RENDER")
    calls = []

    def handler(current):
        calls.append(current.status)
        raise RuntimeError("falha simulada após início do trabalho")

    engine.register_handler("RENDER", handler, claims_status=JOB_PROCESSING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    # O handler rodou exatamente uma vez (já reivindicado).
    assert calls == [JOB_PROCESSING]

    stored = local_db.get(Job, job.id)
    assert stored.status == JOB_FAILED  # nunca volta para PENDING silenciosamente

    events = [event["event_type"] for event in audit.list_job_events(job.id)]
    assert AUDIT_JOB_ERROR in events
    assert events.count(AUDIT_JOB_STATE_CHANGED) == 2  # reivindicação + FAILED

    # FAILED não é reprocessado automaticamente: fetch_pending_jobs não o
    # inclui, e uma nova chamada a advance() não executa o handler de novo.
    assert job.id not in {candidate.id for candidate in engine.fetch_pending_jobs()}
    with pytest.raises(JobNotActionableError):
        engine.advance(job.id)
    assert calls == [JOB_PROCESSING]  # handler não foi chamado de novo


def test_resultado_invalido_do_handler_tambem_marca_job_failed(engine, audit, local_db):
    job = _create_job(audit, operation="RENDER")
    engine.register_handler("RENDER", lambda current: JOB_PROCESSING, claims_status=JOB_PROCESSING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert local_db.get(Job, job.id).status == JOB_FAILED


def test_transicao_final_invalida_apos_processing_tambem_marca_job_failed(engine, audit, local_db):
    """Se o handler devolver um target_status que a State Machine não aceita a
    partir de um claims_status de trabalho LOCAL (PROCESSING), o Job também
    não pode ficar preso silenciosamente em um estado de trabalho ativo: vira
    FAILED, auditável. Ver os testes de PUBLISHING/RECOVERING abaixo para o
    caso de efeito potencialmente remoto, que pousa em UNKNOWN em vez disso.
    """
    job = _create_job(audit, operation="RENDER")
    # PUBLISHING não pode ser alcançado de PROCESSING via retorno direto do
    # handler nesta State Machine (PROCESSING -> PUBLISHING não é aresta
    # válida), então este resultado é sempre inválido a partir de PROCESSING.
    engine.register_handler(
        "RENDER", lambda current: JobStepResult(target_status=JOB_PUBLISHING), claims_status=JOB_PROCESSING
    )

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert local_db.get(Job, job.id).status == JOB_FAILED
    history = audit.reconstruct_job_history(job.id)
    assert history.current_state == JOB_FAILED


def test_audit_trail_consistente_apos_falha_de_processing(engine, audit):
    """O histórico append-only continua reconstruível (sem AuditHistoryError)
    mesmo depois de uma falha de handler pós-reivindicação em PROCESSING.
    """
    job = _create_job(audit, operation="RENDER")

    def handler(current):
        raise RuntimeError("boom")

    engine.register_handler("RENDER", handler, claims_status=JOB_PROCESSING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    history = audit.reconstruct_job_history(job.id)
    assert history.state_path == (JOB_PENDING, JOB_PROCESSING, JOB_FAILED)
    assert history.current_state == JOB_FAILED


# ---------------------------------------------------------------------------
# BLOQUEADOR (2ª revisão) — exceção durante efeito potencialmente remoto
# (PUBLISHING/RECOVERING) deve preservar incerteza (UNKNOWN), nunca virar
# FAILED. FAILED significa falha conhecida; uma exceção depois que uma
# operação remota começou não prova que ela falhou de fato.
# ---------------------------------------------------------------------------


def test_excecao_durante_publishing_pousa_em_unknown_nunca_em_failed(engine, audit, local_db):
    job = _create_job(audit, operation="PUBLISH")
    calls = []

    def handler(current):
        calls.append(current.status)
        raise ConnectionError("conexão caiu durante o upload")

    engine.register_handler("PUBLISH", handler, claims_status=JOB_PUBLISHING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert calls == [JOB_PUBLISHING]
    stored = local_db.get(Job, job.id)
    assert stored.status == JOB_UNKNOWN
    assert stored.status != JOB_FAILED

    events = [event["event_type"] for event in audit.list_job_events(job.id)]
    assert AUDIT_JOB_ERROR in events


def test_job_unknown_apos_falha_de_publishing_nao_aparece_em_fetch_pending(engine, audit):
    job = _create_job(audit, operation="PUBLISH")
    engine.register_handler(
        "PUBLISH",
        lambda current: (_ for _ in ()).throw(ConnectionError("timeout")),
        claims_status=JOB_PUBLISHING,
    )

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert audit.database.get(Job, job.id).status == JOB_UNKNOWN
    assert job.id not in {candidate.id for candidate in engine.fetch_pending_jobs()}


def test_advance_nao_consegue_republicar_job_unknown_diretamente(engine, audit):
    """Depois que PUBLISHING vira UNKNOWN, uma nova chamada de advance() com o
    mesmo handler de publicação (claims_status=PUBLISHING) não pode
    reivindicar o Job de novo: UNKNOWN só permite ir para RECOVERING.
    """
    job = _create_job(audit, operation="PUBLISH")
    publish_calls = []

    def publish_handler(current):
        publish_calls.append(current.status)
        raise ConnectionError("timeout")

    engine.register_handler("PUBLISH", publish_handler, claims_status=JOB_PUBLISHING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)
    assert publish_calls == [JOB_PUBLISHING]
    assert audit.database.get(Job, job.id).status == JOB_UNKNOWN

    # Nova tentativa "cega" de publicar de novo: rejeitada antes de chamar o
    # handler pela segunda vez.
    with pytest.raises(JobNotActionableError):
        engine.advance(job.id)
    assert publish_calls == [JOB_PUBLISHING]  # handler de publicação não rodou de novo


def test_unknown_so_avanca_via_recovering_conforme_state_machine(engine, audit):
    """UNKNOWN respeita a State Machine central: só uma operation cujo
    claims_status seja RECOVERING consegue reivindicar um Job UNKNOWN.
    """
    job = _create_job(audit, operation="RECONCILE")
    audit.transition_job(job.id, JOB_PUBLISHING)
    audit.transition_job(job.id, JOB_UNKNOWN)

    reconcile_calls = []

    def reconcile_handler(current):
        reconcile_calls.append(current.status)
        return JobStepResult(target_status=JOB_PUBLISHED)

    engine.register_handler("RECONCILE", reconcile_handler, claims_status=JOB_RECOVERING)

    updated = engine.advance(job.id)

    assert reconcile_calls == [JOB_RECOVERING]
    assert updated.status == JOB_PUBLISHED
    history = audit.reconstruct_job_history(job.id)
    assert history.state_path == (
        JOB_PENDING,
        JOB_PUBLISHING,
        JOB_UNKNOWN,
        JOB_RECOVERING,
        JOB_PUBLISHED,
    )


def test_falha_de_reconciliacao_nao_torna_resultado_remoto_conhecido(engine, audit, local_db):
    """Uma tentativa de reconciliação (RECOVERING) que lança exceção não pode
    transformar a incerteza remota em FAILED: continua UNKNOWN, preservando
    que o resultado remoto real ainda não foi determinado.
    """
    job = _create_job(audit, operation="RECONCILE")
    audit.transition_job(job.id, JOB_PUBLISHING)
    audit.transition_job(job.id, JOB_UNKNOWN)

    def reconcile_handler(current):
        raise TimeoutError("não foi possível consultar o status remoto")

    engine.register_handler("RECONCILE", reconcile_handler, claims_status=JOB_RECOVERING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    stored = local_db.get(Job, job.id)
    assert stored.status == JOB_UNKNOWN
    assert stored.status != JOB_FAILED

    history = audit.reconstruct_job_history(job.id)
    assert history.state_path == (
        JOB_PENDING,
        JOB_PUBLISHING,
        JOB_UNKNOWN,
        JOB_RECOVERING,
        JOB_UNKNOWN,
    )
    events = [event["event_type"] for event in audit.list_job_events(job.id)]
    assert AUDIT_JOB_ERROR in events


def test_handler_pode_declarar_falha_conhecida_explicitamente_mesmo_em_publishing(engine, audit):
    """Um handler que SABE que nenhuma ação remota ocorreu (ex.: validação
    falhou antes de qualquer chamada de rede) continua livre para declarar
    isso via JobStepResult, sem passar pelo pouso automático em UNKNOWN.
    """
    job = _create_job(audit, operation="PUBLISH")
    engine.register_handler(
        "PUBLISH",
        lambda current: JobStepResult(target_status=JOB_FAILED),
        claims_status=JOB_PUBLISHING,
    )

    updated = engine.advance(job.id)

    assert updated.status == JOB_FAILED


# ---------------------------------------------------------------------------
# MAJOR 4 — run_pending não pode parar no primeiro Job com erro
# ---------------------------------------------------------------------------


def test_run_pending_isola_falha_de_um_job_e_continua_os_demais(engine, audit):
    failing = _create_job(audit, operation="FAILING")
    ok_job = _create_job(audit, operation="OK")

    ok_calls = []

    def failing_handler(current):
        raise RuntimeError("job A falha")

    def ok_handler(current):
        ok_calls.append(current.id)
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("FAILING", failing_handler, claims_status=JOB_PROCESSING)
    engine.register_handler("OK", ok_handler, claims_status=JOB_PROCESSING)

    outcomes = engine.run_pending()

    assert len(outcomes) == 2
    # Job B foi executado mesmo com a falha do Job A em algum ponto do laço.
    assert ok_calls == [ok_job.id]

    by_id = {outcome.job_id: outcome for outcome in outcomes}
    assert by_id[failing.id].ok is False
    assert isinstance(by_id[failing.id].error, JobHandlerError)
    assert by_id[ok_job.id].ok is True
    assert by_id[ok_job.id].job.status == JOB_READY

    assert audit.database.get(Job, failing.id).status == JOB_FAILED
    assert audit.database.get(Job, ok_job.id).status == JOB_READY


def test_run_pending_respeita_limit(engine, audit):
    _create_job(audit, operation="RENDER")
    _create_job(audit, operation="RENDER")
    engine.register_handler(
        "RENDER", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    outcomes = engine.run_pending(limit=1)

    assert len(outcomes) == 1
    assert outcomes[0].ok is True


def test_run_pending_sem_handler_tambem_e_isolado(engine, audit):
    unknown = _create_job(audit, operation="SEM_HANDLER")
    known = _create_job(audit, operation="RENDER")
    engine.register_handler(
        "RENDER", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    outcomes = engine.run_pending()

    by_id = {outcome.job_id: outcome for outcome in outcomes}
    assert isinstance(by_id[unknown.id].error, UnknownJobOperationError)
    assert by_id[known.id].ok is True
    # Job sem handler não foi reivindicado: continua PENDING, elegível assim
    # que um handler for registrado.
    assert audit.database.get(Job, unknown.id).status == JOB_PENDING


# ---------------------------------------------------------------------------
# Independência de UI/plataformas
# ---------------------------------------------------------------------------


def test_job_engine_nao_importa_ui_nem_connectors_de_plataforma():
    engine_path = Path(__file__).parents[1] / "_sistema" / "job_engine.py"
    tree = ast.parse(engine_path.read_text(encoding="utf-8"))
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
