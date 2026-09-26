# -*- coding: utf-8 -*-
"""Testes do Retry Inteligente por Job (PROMPT 21).

Cobre, na ordem exigida pela seção 3 do Prompt:

1. Unidade de ``RetryPolicy`` isolada (SQLite dedicado, sem JobEngine): as
   sete categorias do roadmap (TEMPORARY, PERMANENT, RATE_LIMIT,
   AUTH_REQUIRED, CONTENT_BLOCKED, USER_ACTION_REQUIRED, UNKNOWN) cada uma
   com seu pouso correto e presença/ausência de ``next_retry_at_epoch``;
   backoff cresce entre tentativas sucessivas (clock/random injetáveis,
   determinístico, sem sleep real) e respeita o teto; esgotamento do limite
   de tentativas pousa definitivamente em FAILED, com um teste que PROVA
   que a tentativa seguinte não agenda nem volta a RETRY sozinha; ``reset``
   reinicia o contador (ciclo manual).
2. Integração com ``JobEngine`` (``fetch_pending_jobs``/``_claim``): um Job
   RETRY com ``next_retry_at_epoch`` no futuro não é reivindicado; depois
   do horário, volta a ser elegível; um Job RETRY SEM agendamento (caminho
   manual pré-existente, ex.: pós ``retry_failed()``) continua imediatamente
   elegível (não-regressão); um handler que falha sem fornecer categoria
   nenhuma continua caindo em FAILED/UNKNOWN exatamente como antes, sem
   retry automático (não-regressão); a salvaguarda de ``claims_status``
   (princípio M do CLAUDE.md) é exercitada para PUBLISHING/RECOVERING.
3. ``BatchEngine.retry_failed()`` continua funcionando sem mudança de
   comportamento e reinicia ``attempt_count`` para o novo ciclo.
4. Não-regressão explícita: a suíte completa do Circuit Breaker (Prompt 20)
   continua passando sem alteração (não duplicada aqui -- ver
   ``test_circuit_breaker.py``; este arquivo só confirma, via import, que o
   módulo não foi tocado nesta rodada).
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import threading

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    Job,
    JOB_AUTH_REQUIRED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_RETRY,
    JOB_USER_ACTION_REQUIRED,
)
from _sistema.job_engine import (
    JobBlockedByRetryScheduleError,
    JobEngine,
    JobHandlerError,
    JobStepResult,
)
from _sistema.retry_policy import (
    DEFAULT_MAX_ATTEMPTS,
    ClassifiedJobFailure,
    RETRY_CATEGORY_AUTH_REQUIRED,
    RETRY_CATEGORY_CONTENT_BLOCKED,
    RETRY_CATEGORY_PERMANENT,
    RETRY_CATEGORY_RATE_LIMIT,
    RETRY_CATEGORY_TEMPORARY,
    RETRY_CATEGORY_UNKNOWN,
    RETRY_CATEGORY_USER_ACTION_REQUIRED,
    RetryPolicy,
)
from _sistema.storage import LocalDatabase, OperationalAuditLog


# ---------------------------------------------------------------------------
# Infraestrutura comum (mesmo padrão de tests/test_circuit_breaker.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(paths=paths)
        db.initialize()
        yield db.path, paths


@pytest.fixture
def local_db(db_path):
    path, paths = db_path
    return LocalDatabase(path=path, paths=paths)


@pytest.fixture
def audit(local_db):
    return OperationalAuditLog(local_db)


class FakeClock:
    """Clock determinístico e mutável para testes de backoff/elegibilidade."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FixedRandom:
    """Substituto determinístico de ``random.random()`` para testar jitter."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _create_job(audit, *, operation="PUBLISH"):
    job = Job(operation=operation)
    return audit.create_job(job)


def _fail_job(audit, job_id):
    """PENDING não tem transição direta para FAILED na State Machine --
    precisa passar por PROCESSING primeiro (mesmo caminho real que
    advance() percorre ao reivindicar um Job)."""
    audit.transition_job(job_id, JOB_PROCESSING)
    audit.transition_job(job_id, JOB_FAILED)


# ---------------------------------------------------------------------------
# 1. Unidade: as sete categorias do roadmap
# ---------------------------------------------------------------------------


def test_temporary_agenda_retry_automatico_com_next_retry_at_epoch(local_db, audit):
    job = _create_job(audit)
    clock = FakeClock()
    policy = RetryPolicy(local_db, clock=clock, random_func=FixedRandom(0.0))

    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)

    assert decision.target_status == JOB_RETRY
    assert decision.attempt_count == 1
    assert decision.next_retry_at_epoch is not None
    assert decision.next_retry_at_epoch > clock.value
    assert decision.retries_exhausted is False


def test_rate_limit_agenda_retry_automatico_com_next_retry_at_epoch(local_db, audit):
    job = _create_job(audit)
    policy = RetryPolicy(local_db, clock=FakeClock(), random_func=FixedRandom(0.0))

    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_RATE_LIMIT)

    assert decision.target_status == JOB_RETRY
    assert decision.next_retry_at_epoch is not None
    assert decision.retries_exhausted is False


def test_unknown_agenda_retry_automatico_com_next_retry_at_epoch(local_db, audit):
    job = _create_job(audit)
    policy = RetryPolicy(local_db, clock=FakeClock(), random_func=FixedRandom(0.0))

    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_UNKNOWN)

    assert decision.target_status == JOB_RETRY
    assert decision.next_retry_at_epoch is not None
    assert decision.retries_exhausted is False


def test_permanent_pousa_direto_em_failed_sem_agendamento(local_db, audit):
    job = _create_job(audit)
    policy = RetryPolicy(local_db, clock=FakeClock())

    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_PERMANENT)

    assert decision.target_status == JOB_FAILED
    assert decision.next_retry_at_epoch is None
    assert decision.retries_exhausted is True
    # PERMANENT nunca ganha nenhuma tentativa automática -- limite efetivo
    # é 0, então o contador nem chega a ser incrementado.
    assert decision.attempt_count == 0


def test_content_blocked_pousa_direto_em_failed_sem_agendamento(local_db, audit):
    job = _create_job(audit)
    policy = RetryPolicy(local_db, clock=FakeClock())

    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_CONTENT_BLOCKED)

    assert decision.target_status == JOB_FAILED
    assert decision.next_retry_at_epoch is None
    assert decision.retries_exhausted is True
    assert decision.attempt_count == 0


def test_auth_required_pousa_no_estado_dedicado_sem_agendar_retry(local_db, audit):
    job = _create_job(audit)
    policy = RetryPolicy(local_db, clock=FakeClock())

    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_AUTH_REQUIRED)

    assert decision.target_status == JOB_AUTH_REQUIRED
    assert decision.next_retry_at_epoch is None
    assert decision.retries_exhausted is False
    # Não há retry sozinho sem a ação humana que o próprio nome do estado
    # exige -- por isso nenhum agendamento é criado.


def test_user_action_required_pousa_no_estado_dedicado_sem_agendar_retry(local_db, audit):
    job = _create_job(audit)
    policy = RetryPolicy(local_db, clock=FakeClock())

    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_USER_ACTION_REQUIRED)

    assert decision.target_status == JOB_USER_ACTION_REQUIRED
    assert decision.next_retry_at_epoch is None
    assert decision.retries_exhausted is False


def test_categoria_invalida_e_rejeitada(local_db):
    policy = RetryPolicy(local_db)
    with pytest.raises(ValueError):
        policy.record_failure("job-x", category="NOT_A_REAL_CATEGORY")


def test_classified_job_failure_valida_categoria_na_construcao():
    ClassifiedJobFailure(RETRY_CATEGORY_TEMPORARY)  # não levanta
    with pytest.raises(ValueError):
        ClassifiedJobFailure("ALGO_INVENTADO")


# ---------------------------------------------------------------------------
# Backoff com jitter: crescimento entre tentativas + teto
# ---------------------------------------------------------------------------


def test_backoff_cresce_entre_tentativas_sucessivas_determinístico(local_db, audit):
    job = _create_job(audit)
    clock = FakeClock()
    # jitter fixo em 0 isola a curva exponencial pura para a asserção de
    # crescimento; um teste separado abaixo prova o jitter em si.
    policy = RetryPolicy(
        local_db,
        base_seconds=10.0,
        factor=2.0,
        max_backoff_seconds=10_000.0,
        jitter_ratio=0.0,
        clock=clock,
        random_func=FixedRandom(0.0),
    )

    delays = []
    for _ in range(4):
        decision = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
        delays.append(decision.next_retry_at_epoch - clock.value)
        # cada tentativa "acontece" no instante em que o próximo agendamento
        # foi calculado, simulando o relógio avançando até lá.
        clock.value = decision.next_retry_at_epoch

    assert delays == [10.0, 20.0, 40.0, 80.0]
    assert all(b < a for a, b in zip(delays[1:], delays[:-1]))


def test_backoff_respeita_o_teto_maximo(local_db, audit):
    job = _create_job(audit)
    clock = FakeClock()
    policy = RetryPolicy(
        local_db,
        base_seconds=100.0,
        factor=10.0,
        max_backoff_seconds=500.0,
        jitter_ratio=0.0,
        max_attempts=10,
        clock=clock,
        random_func=FixedRandom(0.0),
    )

    for _ in range(5):
        decision = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
        delay = decision.next_retry_at_epoch - clock.value
        assert delay <= 500.0
        clock.value = decision.next_retry_at_epoch


def test_jitter_e_estritamente_aditivo_nunca_reduz_abaixo_da_curva(local_db, audit):
    job = _create_job(audit)
    clock = FakeClock()
    policy = RetryPolicy(
        local_db,
        base_seconds=10.0,
        factor=2.0,
        max_backoff_seconds=10_000.0,
        jitter_ratio=0.5,
        clock=clock,
        random_func=FixedRandom(1.0),  # jitter máximo possível
    )
    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    delay = decision.next_retry_at_epoch - clock.value
    # base=10, jitter_ratio=0.5, random()=1.0 -> jitter = 10*0.5*1.0 = 5
    assert delay == pytest.approx(15.0)
    assert delay >= 10.0


# ---------------------------------------------------------------------------
# "No infinite retry": limite finito e configurável
# ---------------------------------------------------------------------------


def test_limite_de_tentativas_esgotado_pousa_definitivamente_em_failed(local_db, audit):
    job = _create_job(audit)
    clock = FakeClock()
    policy = RetryPolicy(
        local_db, max_attempts=3, clock=clock, random_func=FixedRandom(0.0)
    )

    for expected_attempt in (1, 2, 3):
        decision = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
        assert decision.target_status == JOB_RETRY
        assert decision.attempt_count == expected_attempt
        assert decision.retries_exhausted is False
        clock.value = decision.next_retry_at_epoch

    # 4ª falha: limite de 3 tentativas automáticas já foi usado -> FAILED.
    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert decision.target_status == JOB_FAILED
    assert decision.retries_exhausted is True
    assert decision.next_retry_at_epoch is None
    assert decision.attempt_count == 3  # não incrementa além do limite


def test_apos_esgotar_limite_proxima_falha_nao_agenda_nem_volta_a_retry(local_db, audit):
    """Prova explícita exigida pelo Prompt: não basta esgotar uma vez -- a
    tentativa SEGUINTE, depois de já esgotado, também não pode voltar a
    RETRY nem agendar ``next_retry_at_epoch`` sozinha."""
    job = _create_job(audit)
    clock = FakeClock()
    policy = RetryPolicy(local_db, max_attempts=1, clock=clock, random_func=FixedRandom(0.0))

    first = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert first.target_status == JOB_RETRY
    clock.value = first.next_retry_at_epoch

    second = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert second.target_status == JOB_FAILED
    assert second.retries_exhausted is True

    # Chamadas adicionais continuam presas em FAILED, nunca ressuscitam
    # sozinhas nem geram novo agendamento.
    third = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert third.target_status == JOB_FAILED
    assert third.next_retry_at_epoch is None
    assert third.attempt_count == 1

    state = policy.get_state(job.id)
    assert state.attempt_count == 1
    assert state.next_retry_at_epoch is None


def test_max_attempts_zero_significa_nenhuma_tentativa_automatica(local_db, audit):
    job = _create_job(audit)
    policy = RetryPolicy(local_db, max_attempts=0, clock=FakeClock())

    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert decision.target_status == JOB_FAILED
    assert decision.retries_exhausted is True
    assert decision.next_retry_at_epoch is None


def test_reset_reinicia_contador_para_um_novo_ciclo_manual(local_db, audit):
    job = _create_job(audit)
    clock = FakeClock()
    policy = RetryPolicy(local_db, max_attempts=1, clock=clock, random_func=FixedRandom(0.0))

    first = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    clock.value = first.next_retry_at_epoch
    exhausted = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert exhausted.target_status == JOB_FAILED

    policy.reset(job.id)
    state = policy.get_state(job.id)
    assert state.attempt_count == 0
    assert state.next_retry_at_epoch is None
    assert state.last_failure_category is None

    # Novo ciclo: ganha o limite inteiro de novo, não continua "esgotado".
    renewed = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert renewed.target_status == JOB_RETRY
    assert renewed.attempt_count == 1


def test_reset_de_job_nunca_visto_e_no_op_seguro(local_db):
    policy = RetryPolicy(local_db)
    policy.reset("job-nunca-existiu")  # não deve levantar
    state = policy.get_state("job-nunca-existiu")
    assert state.attempt_count == 0
    assert state.next_retry_at_epoch is None


# ---------------------------------------------------------------------------
# Eligibilidade: is_eligible_now / get_state para Job nunca visto
# ---------------------------------------------------------------------------


def test_job_nunca_visto_esta_sempre_elegivel(local_db):
    policy = RetryPolicy(local_db)
    assert policy.is_eligible_now("job-qualquer") is True
    state = policy.get_state("job-qualquer")
    assert state.attempt_count == 0
    assert state.next_retry_at_epoch is None
    assert state.last_failure_category is None


def test_estado_de_retry_sobrevive_a_restart_com_novas_instancias(db_path):
    """CLAUDE.md item 4 "RESTART": reabrir com NOVAS instâncias de
    LocalDatabase e RetryPolicy (nunca reuso de objeto em memória) precisa
    enxergar exatamente o mesmo estado persistido em ``job_retry_state``."""
    path, paths = db_path

    db1 = LocalDatabase(path=path, paths=paths)
    audit1 = OperationalAuditLog(db1)
    job = _create_job(audit1)
    policy1 = RetryPolicy(db1, max_attempts=5, random_func=FixedRandom(0.0))
    decision = policy1.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert decision.target_status == JOB_RETRY

    db2 = LocalDatabase(path=path, paths=paths)
    policy2 = RetryPolicy(db2, max_attempts=5)
    state = policy2.get_state(job.id)
    assert state.attempt_count == 1
    assert state.next_retry_at_epoch == decision.next_retry_at_epoch
    assert state.last_failure_category == RETRY_CATEGORY_TEMPORARY
    assert policy2.is_eligible_now(job.id) is False


def test_concorrencia_duas_instancias_gravando_falha_do_mesmo_job(db_path):
    """CLAUDE.md item 6/14: duas instâncias de RetryPolicy contra o mesmo
    SQLite, escrevendo a falha do MESMO Job ao mesmo tempo -- SQLite
    serializa via BEGIN IMMEDIATE, então o attempt_count final precisa
    refletir AMBAS as chamadas, sem perda de escrita."""
    path, paths = db_path
    setup_db = LocalDatabase(path=path, paths=paths)
    setup_audit = OperationalAuditLog(setup_db)
    job = _create_job(setup_audit)

    workers = 6
    barrier = threading.Barrier(workers)

    def worker(_: int) -> None:
        db = LocalDatabase(path=path, paths=paths, busy_timeout_ms=10_000)
        policy = RetryPolicy(db, max_attempts=100, random_func=FixedRandom(0.0))
        barrier.wait(timeout=5)
        policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    inspector = LocalDatabase(path=path, paths=paths)
    final_policy = RetryPolicy(inspector, max_attempts=100)
    assert final_policy.get_state(job.id).attempt_count == workers


# ---------------------------------------------------------------------------
# 2. Integração com JobEngine: eligibilidade por agendamento futuro
# ---------------------------------------------------------------------------


def _engine_with_policy(local_db, audit, **policy_kwargs):
    clock = policy_kwargs.pop("clock", None) or FakeClock()
    policy = RetryPolicy(local_db, clock=clock, random_func=FixedRandom(0.0), **policy_kwargs)
    engine = JobEngine(local_db, audit_log=audit, retry_policy=policy)
    return engine, policy, clock


def test_job_retry_com_next_retry_at_epoch_futuro_nao_e_reivindicado(local_db, audit):
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH")
    engine.register_handler(
        "PUBLISH", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )
    policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    # transição real para RETRY, imitando o pouso que advance() faria --
    # aqui testamos só o gate de elegibilidade, não o fluxo de advance().
    # PENDING não tem aresta direta para RETRY: precisa passar por
    # PROCESSING primeiro (mesmo caminho real de uma falha via advance()).
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.retry(job.id)

    assert engine.fetch_pending_jobs() == []
    with pytest.raises(JobBlockedByRetryScheduleError):
        engine.advance(job.id)
    assert local_db.get(Job, job.id).status == JOB_RETRY


def test_job_retry_fica_elegivel_novamente_apos_o_horario_agendado(local_db, audit):
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH")
    engine.register_handler(
        "PUBLISH", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )
    decision = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.retry(job.id)
    assert engine.fetch_pending_jobs() == []

    clock.advance((decision.next_retry_at_epoch - clock.value) + 1.0)

    pending = engine.fetch_pending_jobs()
    assert [j.id for j in pending] == [job.id]
    result = engine.advance(job.id)
    assert result.status == JOB_READY


def test_job_retry_sem_agendamento_continua_imediatamente_elegivel(local_db, audit):
    """Não-regressão: o caminho manual pré-existente (``retry_failed()``,
    que nunca cria linha em ``job_retry_state``) não pode ser bloqueado por
    este gate novo."""
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH")
    engine.register_handler(
        "PUBLISH", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )
    _fail_job(audit, job.id)
    audit.retry(job.id)  # caminho manual puro -- RetryPolicy nunca chamado

    pending = engine.fetch_pending_jobs()
    assert [j.id for j in pending] == [job.id]
    result = engine.advance(job.id)
    assert result.status == JOB_READY


def test_engine_sem_retry_policy_nao_e_afetado_pelo_gate(local_db, audit):
    """Comportamento idêntico ao de antes do Prompt 21 quando
    ``retry_policy`` é omitido (padrão)."""
    engine = JobEngine(local_db, audit_log=audit)
    job = _create_job(audit, operation="PUBLISH")
    engine.register_handler(
        "PUBLISH", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )
    _fail_job(audit, job.id)
    audit.retry(job.id)

    result = engine.advance(job.id)
    assert result.status == JOB_READY


# ---------------------------------------------------------------------------
# 2b. advance(): handler sem categoria continua no caminho cego de sempre
# ---------------------------------------------------------------------------


def test_handler_sem_categoria_continua_caindo_em_failed_sem_retry_automatico(local_db, audit):
    """Não-regressão explícita (item 1.6): um handler que levanta uma
    exceção genérica (não ``ClassifiedJobFailure``) continua caindo
    EXATAMENTE no pouso cego de sempre, mesmo com ``retry_policy``
    configurado -- nenhum retry automático "de graça" por omissão."""
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH")

    def handler(current):
        raise RuntimeError("falha genérica, não classificada")

    engine.register_handler("PUBLISH", handler, claims_status=JOB_PROCESSING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert local_db.get(Job, job.id).status == JOB_FAILED
    # RetryPolicy nunca foi consultado -- nenhuma linha de estado criada.
    state = policy.get_state(job.id)
    assert state.attempt_count == 0
    assert state.next_retry_at_epoch is None


def test_handler_com_classified_job_failure_temporary_agenda_retry_automatico(local_db, audit):
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH")

    def handler(current):
        raise ClassifiedJobFailure(RETRY_CATEGORY_TEMPORARY, message="timeout de rede")

    engine.register_handler("PUBLISH", handler, claims_status=JOB_PROCESSING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    job_after = local_db.get(Job, job.id)
    assert job_after.status == JOB_RETRY
    state = policy.get_state(job.id)
    assert state.attempt_count == 1
    assert state.next_retry_at_epoch is not None


def test_handler_com_classified_job_failure_permanent_pousa_em_failed(local_db, audit):
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH")

    def handler(current):
        raise ClassifiedJobFailure(RETRY_CATEGORY_PERMANENT, message="conteúdo inválido")

    engine.register_handler("PUBLISH", handler, claims_status=JOB_PROCESSING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert local_db.get(Job, job.id).status == JOB_FAILED


def test_handler_com_classified_job_failure_auth_required_pousa_no_estado_dedicado(local_db, audit):
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH")

    def handler(current):
        raise ClassifiedJobFailure(RETRY_CATEGORY_AUTH_REQUIRED, message="sessão expirada")

    engine.register_handler("PUBLISH", handler, claims_status=JOB_PROCESSING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert local_db.get(Job, job.id).status == JOB_AUTH_REQUIRED


def test_classified_job_failure_sem_retry_policy_configurado_cai_no_fallback_cego(local_db, audit):
    """Item 1.6: mesmo um handler que classifica a falha não ganha retry
    automático se o ``JobEngine`` não tiver ``retry_policy`` configurado
    (padrão) -- confirma que o gate é ``retry_policy is not None``, não
    apenas o tipo da exceção."""
    engine = JobEngine(local_db, audit_log=audit)
    job = _create_job(audit, operation="PUBLISH")

    def handler(current):
        raise ClassifiedJobFailure(RETRY_CATEGORY_TEMPORARY, message="timeout de rede")

    engine.register_handler("PUBLISH", handler, claims_status=JOB_PROCESSING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert local_db.get(Job, job.id).status == JOB_FAILED


def test_publishing_com_categoria_retryable_nao_reposta_automaticamente(local_db, audit):
    """CLAUDE.md princípio M: mesmo TEMPORARY (categoria "retry-able") não
    pode gerar um repost automático quando o trabalho reivindicado tinha
    efeito remoto potencialmente incerto (PUBLISHING) -- cai no mesmo
    fallback seguro (UNKNOWN) já aprovado para falha não classificada."""
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH_REMOTE")

    def handler(current):
        raise ClassifiedJobFailure(RETRY_CATEGORY_TEMPORARY, message="timeout durante upload")

    engine.register_handler("PUBLISH_REMOTE", handler, claims_status=JOB_PUBLISHING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    job_after = local_db.get(Job, job.id)
    assert job_after.status != JOB_RETRY
    from _sistema.domain import JOB_UNKNOWN

    assert job_after.status == JOB_UNKNOWN


def test_publishing_com_auth_required_pousa_no_estado_dedicado_mesmo_assim(local_db, audit):
    """Exceção deliberada à salvaguarda acima: AUTH_REQUIRED/
    USER_ACTION_REQUIRED são seguros para qualquer claims_status, porque
    não afirmam nada sobre o efeito remoto."""
    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    job = _create_job(audit, operation="PUBLISH_REMOTE")
    # Só {INTERRUPTED, RETRY, UNKNOWN, AUTH_REQUIRED, USER_ACTION_REQUIRED}
    # permitem transição para RECOVERING na State Machine central -- prepara
    # o Job em RETRY antes de reivindicá-lo com claims_status=RECOVERING.
    audit.transition_job(job.id, JOB_PROCESSING)
    audit.transition_job(job.id, JOB_RETRY)

    def handler(current):
        raise ClassifiedJobFailure(RETRY_CATEGORY_USER_ACTION_REQUIRED, message="captcha")

    engine.register_handler("PUBLISH_REMOTE", handler, claims_status=JOB_RECOVERING)

    with pytest.raises(JobHandlerError):
        engine.advance(job.id)

    assert local_db.get(Job, job.id).status == JOB_USER_ACTION_REQUIRED


# ---------------------------------------------------------------------------
# 3. BatchEngine.retry_failed(): não-regressão + reset de attempt_count
# ---------------------------------------------------------------------------


def test_retry_failed_continua_funcionando_sem_mudanca_de_comportamento(local_db, audit):
    from _sistema.batch_engine import BatchEngine

    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=5)
    batch_engine = BatchEngine(local_db, audit_log=audit, job_engine=engine)
    job = _create_job(audit, operation="PUBLISH")
    batch = batch_engine.create_batch_from_existing_jobs(job_ids=[job.id])
    _fail_job(audit, job.id)

    outcome = batch_engine.retry_failed(batch.batch_id)

    assert outcome.retried_job_ids == (job.id,)
    assert outcome.not_eligible_job_ids == ()
    assert outcome.errors == ()
    assert local_db.get(Job, job.id).status == JOB_RETRY


def test_retry_failed_reinicia_attempt_count_para_o_novo_ciclo(local_db, audit):
    from _sistema.batch_engine import BatchEngine

    engine, policy, clock = _engine_with_policy(local_db, audit, max_attempts=1)
    batch_engine = BatchEngine(local_db, audit_log=audit, job_engine=engine)
    job = _create_job(audit, operation="PUBLISH")
    batch = batch_engine.create_batch_from_existing_jobs(job_ids=[job.id])

    # Esgota o limite automático (1 tentativa) via RetryPolicy diretamente,
    # simulando o que advance() faria, e pousa manualmente em FAILED.
    first = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert first.target_status == JOB_RETRY
    clock.value = first.next_retry_at_epoch
    audit.transition_job(job.id, JOB_PROCESSING)
    exhausted = policy.record_failure(job.id, category=RETRY_CATEGORY_TEMPORARY)
    assert exhausted.target_status == JOB_FAILED
    # Já está em PROCESSING (transição acima) -- PROCESSING -> FAILED é
    # permitido diretamente, sem precisar do helper ``_fail_job`` (que
    # assume um Job ainda em PENDING).
    audit.transition_job(job.id, JOB_FAILED)
    assert policy.get_state(job.id).attempt_count == 1

    outcome = batch_engine.retry_failed(batch.batch_id)
    assert outcome.retried_job_ids == (job.id,)

    state = policy.get_state(job.id)
    assert state.attempt_count == 0
    assert state.next_retry_at_epoch is None


def test_retry_failed_sem_retry_policy_configurado_continua_identico(local_db, audit):
    """Não-regressão: quando ``retry_policy`` não está configurado no
    ``JobEngine`` (padrão, como em todo teste pré-Prompt-21),
    ``retry_failed()`` continua funcionando exatamente como antes."""
    from _sistema.batch_engine import BatchEngine

    engine = JobEngine(local_db, audit_log=audit)
    batch_engine = BatchEngine(local_db, audit_log=audit, job_engine=engine)
    job = _create_job(audit, operation="PUBLISH")
    batch = batch_engine.create_batch_from_existing_jobs(job_ids=[job.id])
    _fail_job(audit, job.id)

    outcome = batch_engine.retry_failed(batch.batch_id)
    assert outcome.retried_job_ids == (job.id,)
    assert local_db.get(Job, job.id).status == JOB_RETRY


# ---------------------------------------------------------------------------
# 4. Confirmação de que CircuitBreaker (Prompt 20) não foi tocado nem
# chamado por RetryPolicy -- import cruzado nunca acontece.
# ---------------------------------------------------------------------------


def test_retry_policy_nunca_importa_nem_referencia_circuit_breaker():
    """``retry_policy.py`` menciona ``circuit_breaker`` só na prosa da
    docstring (documentando a relação ortogonal) -- nunca em código
    executável: nenhum import, nenhum atributo, nenhuma chamada real."""
    import ast

    import _sistema.retry_policy as retry_policy_module

    assert "circuit_breaker" not in retry_policy_module.__dict__
    assert not hasattr(retry_policy_module.RetryPolicy, "circuit_breaker")

    source = Path(retry_policy_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
            assert "circuit_breaker" not in names.lower()
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
            assert "circuit_breaker" not in names.lower()
        if isinstance(node, ast.Attribute):
            assert node.attr.lower() != "circuit_breaker"
        if isinstance(node, ast.Name):
            assert "circuit_breaker" not in node.id.lower()
