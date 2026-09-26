# -*- coding: utf-8 -*-
"""Testes do Circuit Breaker por connector (PROMPT 20).

Cobre, na ordem exigida pela seção 3 do Prompt:

1. Unidade de ``CircuitBreaker`` isolada (SQLite dedicado, sem JobEngine):
   abre após N falhas sistêmicas IDÊNTICAS consecutivas; falha de conteúdo
   nunca conta; sucesso reseta o contador; cooldown fecha automaticamente;
   observabilidade (audit_events); persistência entre restart (novas
   instâncias de LocalDatabase/CircuitBreaker); concorrência determinística
   (Barrier, duas instâncias contra o mesmo arquivo).
2. Integração com ``JobEngine`` (``fetch_pending_jobs``/``_claim``): um Job
   sem ``extra["connector_key"]`` nunca é bloqueado; um Job cujo connector
   está com o circuito aberto não é reivindicado.
3. Os três cenários mistos exigidos pela seção 1.1/3: Job x Job (aponta
   para o teste já existente e inequívoco, não duplicado aqui), conta x
   conta e plataforma x plataforma -- cada um provado com um único
   ``run_pending()`` processando o connector saudável normalmente enquanto
   o connector com circuito aberto fica de fora.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import threading

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.circuit_breaker import (
    STATE_CLOSED,
    STATE_OPEN,
    CIRCUIT_BREAKER_CLOSED_EVENT,
    CIRCUIT_BREAKER_OPENED_EVENT,
    CircuitBreaker,
)
from _sistema.domain import Job, JOB_PENDING, JOB_PROCESSING, JOB_READY
from _sistema.storage import LocalDatabase, OperationalAuditLog
from _sistema.job_engine import JobBlockedByCircuitBreakerError, JobEngine, JobStepResult


# ---------------------------------------------------------------------------
# Infraestrutura comum
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
    """Clock determinístico e mutável para testes de cooldown/restart."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _create_job(audit, *, operation="PUBLISH", connector_key=None):
    extra = {"connector_key": connector_key} if connector_key is not None else {}
    job = Job(operation=operation, extra=extra)
    return audit.create_job(job)


# ---------------------------------------------------------------------------
# 1. Unidade: abertura por N falhas sistêmicas idênticas consecutivas
# ---------------------------------------------------------------------------


def test_connector_nunca_visto_esta_fechado_por_padrao(local_db):
    breaker = CircuitBreaker(local_db)
    assert breaker.is_admitted("youtube:canal-a") is True
    state = breaker.get_state("youtube:canal-a")
    assert state.state == STATE_CLOSED
    assert state.consecutive_failures == 0
    assert state.is_open is False


def test_abre_somente_apos_threshold_falhas_sistemicas_identicas_consecutivas(local_db):
    breaker = CircuitBreaker(local_db, failure_threshold=3, cooldown_seconds=60)
    key = "youtube:canal-a"

    assert breaker.record_failure(key, code="connection_timeout", systemic=True) is False
    assert breaker.get_state(key).consecutive_failures == 1
    assert breaker.is_admitted(key) is True

    assert breaker.record_failure(key, code="connection_timeout", systemic=True) is False
    assert breaker.get_state(key).consecutive_failures == 2
    assert breaker.is_admitted(key) is True

    # 3ª falha idêntica consecutiva: abre agora (devolve True só nesta chamada).
    assert breaker.record_failure(key, code="connection_timeout", systemic=True) is True
    state = breaker.get_state(key)
    assert state.state == STATE_OPEN
    assert state.consecutive_failures == 3
    assert state.last_failure_code == "connection_timeout"
    assert breaker.is_admitted(key) is False


def test_falha_sistemica_com_code_diferente_reinicia_a_contagem_em_1(local_db):
    """'N consecutive IDENTICAL systemic failures (same code)' -- um code
    diferente é sistêmico, mas NÃO é a MESMA razão repetida, então reinicia
    o contador em vez de acumular com o anterior."""
    breaker = CircuitBreaker(local_db, failure_threshold=3, cooldown_seconds=60)
    key = "youtube:canal-a"

    breaker.record_failure(key, code="connection_timeout", systemic=True)
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert breaker.get_state(key).consecutive_failures == 2

    # code diferente: reinicia em 1, não acumula para 3, não abre.
    opened = breaker.record_failure(key, code="auth_expired", systemic=True)
    assert opened is False
    state = breaker.get_state(key)
    assert state.consecutive_failures == 1
    assert state.last_failure_code == "auth_expired"
    assert breaker.is_admitted(key) is True


def test_falha_de_conteudo_nunca_conta_mesmo_repetida_com_itens_diferentes(local_db):
    """Falha de CONTEÚDO (ver circuit_breaker.py): um vídeo com formato
    inválido, três vezes com vídeos diferentes por coincidência, nunca pode
    abrir o circuito -- é exatamente o oposto do que o Circuit Breaker
    protege."""
    breaker = CircuitBreaker(local_db, failure_threshold=3, cooldown_seconds=60)
    key = "tiktok:canal-b"

    for _ in range(10):
        opened = breaker.record_failure(key, code="video_format_invalid", systemic=False)
        assert opened is False

    state = breaker.get_state(key)
    assert state.state == STATE_CLOSED
    assert state.consecutive_failures == 0
    assert breaker.is_admitted(key) is True


def test_falha_de_conteudo_intercalada_nao_atrapalha_contagem_sistemica(local_db):
    """Uma falha de conteúdo no meio de falhas sistêmicas idênticas não deve
    contar nem quebrar a sequência sistêmica -- ela é um no-op estrutural."""
    breaker = CircuitBreaker(local_db, failure_threshold=3, cooldown_seconds=60)
    key = "youtube:canal-a"

    breaker.record_failure(key, code="connection_timeout", systemic=True)
    breaker.record_failure(key, code="titulo_excede_limite", systemic=False)  # conteúdo: ignorado
    assert breaker.get_state(key).consecutive_failures == 1
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    opened = breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert opened is True
    assert breaker.get_state(key).state == STATE_OPEN


def test_sucesso_reseta_contador_de_falhas_consecutivas(local_db):
    breaker = CircuitBreaker(local_db, failure_threshold=3, cooldown_seconds=60)
    key = "youtube:canal-a"

    breaker.record_failure(key, code="connection_timeout", systemic=True)
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert breaker.get_state(key).consecutive_failures == 2

    breaker.record_success(key)
    assert breaker.get_state(key).consecutive_failures == 0
    assert breaker.is_admitted(key) is True

    # depois do sucesso, precisa de novo do threshold inteiro para abrir --
    # as duas falhas anteriores ao sucesso não contam mais.
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert breaker.is_admitted(key) is True
    opened = breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert opened is True


def test_falha_sucesso_falha_falha_nao_abre_com_threshold_tres(local_db):
    """Só N CONSECUTIVAS abrem -- uma sequência falha,sucesso,falha,falha
    nunca acumula 3 consecutivas de verdade."""
    breaker = CircuitBreaker(local_db, failure_threshold=3, cooldown_seconds=60)
    key = "youtube:canal-a"

    breaker.record_failure(key, code="connection_timeout", systemic=True)
    breaker.record_success(key)
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    opened = breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert opened is False
    assert breaker.get_state(key).state == STATE_CLOSED
    assert breaker.is_admitted(key) is True


def test_falha_enquanto_ja_aberto_nao_reabre_nem_gera_segundo_evento(local_db):
    breaker = CircuitBreaker(local_db, failure_threshold=2, cooldown_seconds=60)
    key = "youtube:canal-a"
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    opened = breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert opened is True

    opened_again = breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert opened_again is False
    events = local_db.list_audit_events(entity_type="circuit_breaker", event_type=CIRCUIT_BREAKER_OPENED_EVENT)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Cooldown / half-open (decisão: cooldown simples, sem sonda) / observabilidade
# ---------------------------------------------------------------------------


def test_cooldown_ainda_nao_decorrido_mantem_bloqueado(local_db):
    clock = FakeClock()
    breaker = CircuitBreaker(local_db, failure_threshold=1, cooldown_seconds=900, clock=clock)
    key = "youtube:canal-a"
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert breaker.is_admitted(key) is False

    clock.advance(899)
    assert breaker.is_admitted(key) is False


def test_cooldown_decorrido_fecha_automaticamente_na_proxima_consulta(local_db):
    clock = FakeClock()
    breaker = CircuitBreaker(local_db, failure_threshold=1, cooldown_seconds=900, clock=clock)
    key = "youtube:canal-a"
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert breaker.get_state(key).state == STATE_OPEN

    clock.advance(900)
    assert breaker.is_admitted(key) is True
    state = breaker.get_state(key)
    assert state.state == STATE_CLOSED
    assert state.consecutive_failures == 0

    closed_events = local_db.list_audit_events(
        entity_type="circuit_breaker", event_type=CIRCUIT_BREAKER_CLOSED_EVENT
    )
    assert len(closed_events) == 1
    assert closed_events[0]["data"]["connector_key"] == key
    assert closed_events[0]["data"]["open_duration_seconds"] == pytest.approx(900.0)


def test_get_state_e_leitura_pura_nunca_fecha_sozinha(local_db):
    """Diferente de is_admitted()/record_success(), get_state() é só
    diagnóstico -- nunca decide admissão, então nunca fecha o circuito por
    conta própria mesmo que o cooldown já tenha decorrido."""
    clock = FakeClock()
    breaker = CircuitBreaker(local_db, failure_threshold=1, cooldown_seconds=60, clock=clock)
    key = "youtube:canal-a"
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    clock.advance(120)

    assert breaker.get_state(key).state == STATE_OPEN
    # só is_admitted()/record_success() decidem a transição real:
    assert breaker.is_admitted(key) is True
    assert breaker.get_state(key).state == STATE_CLOSED


def test_sucesso_explicito_fecha_circuito_aberto_antes_do_cooldown(local_db):
    clock = FakeClock()
    breaker = CircuitBreaker(local_db, failure_threshold=1, cooldown_seconds=900, clock=clock)
    key = "youtube:canal-a"
    breaker.record_failure(key, code="connection_timeout", systemic=True)
    assert breaker.is_admitted(key) is False

    breaker.record_success(key)
    assert breaker.get_state(key).state == STATE_CLOSED
    assert breaker.is_admitted(key) is True
    closed_events = local_db.list_audit_events(
        entity_type="circuit_breaker", event_type=CIRCUIT_BREAKER_CLOSED_EVENT
    )
    assert len(closed_events) == 1


def test_evento_de_abertura_e_persistido_e_observavel(local_db):
    breaker = CircuitBreaker(local_db, failure_threshold=2, cooldown_seconds=300)
    key = "tiktok:canal-c"
    breaker.record_failure(key, code="auth_expired", systemic=True)
    breaker.record_failure(key, code="auth_expired", systemic=True)

    events = local_db.list_audit_events(entity_type="circuit_breaker", event_type=CIRCUIT_BREAKER_OPENED_EVENT)
    assert len(events) == 1
    data = events[0]["data"]
    assert data["connector_key"] == key
    assert data["consecutive_failures"] == 2
    assert data["failure_threshold"] == 2
    assert data["last_failure_code"] == "auth_expired"
    assert data["cooldown_seconds"] == 300


# ---------------------------------------------------------------------------
# Validação de entrada
# ---------------------------------------------------------------------------


def test_construtor_rejeita_threshold_e_cooldown_invalidos(local_db):
    with pytest.raises(ValueError):
        CircuitBreaker(local_db, failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(local_db, cooldown_seconds=0)
    with pytest.raises(ValueError):
        CircuitBreaker(local_db, cooldown_seconds=-1)


def test_connector_key_e_code_vazios_sao_rejeitados(local_db):
    breaker = CircuitBreaker(local_db)
    with pytest.raises(ValueError):
        breaker.is_admitted("   ")
    with pytest.raises(ValueError):
        breaker.record_failure("youtube", code="", systemic=True)
    with pytest.raises(ValueError):
        breaker.record_failure("  ", code="x", systemic=True)


# ---------------------------------------------------------------------------
# CLAUDE.md ponto 4 "RESTART": novas instâncias, nunca objetos em memória
# ---------------------------------------------------------------------------


def test_estado_aberto_sobrevive_a_restart_com_novas_instancias(db_path):
    path, paths = db_path
    key = "youtube:canal-a"

    db1 = LocalDatabase(path=path, paths=paths)
    breaker1 = CircuitBreaker(db1, failure_threshold=2, cooldown_seconds=900)
    breaker1.record_failure(key, code="connection_timeout", systemic=True)
    breaker1.record_failure(key, code="connection_timeout", systemic=True)
    assert breaker1.get_state(key).state == STATE_OPEN

    # Novas instâncias de LocalDatabase E de CircuitBreaker -- nunca reuso
    # de objeto em memória (CLAUDE.md, ponto 4 "RESTART").
    db2 = LocalDatabase(path=path, paths=paths)
    breaker2 = CircuitBreaker(db2, failure_threshold=2, cooldown_seconds=900)
    state = breaker2.get_state(key)
    assert state.state == STATE_OPEN
    assert state.consecutive_failures == 2
    assert state.last_failure_code == "connection_timeout"
    assert breaker2.is_admitted(key) is False


def test_estado_fechado_sobrevive_a_restart_com_novas_instancias(db_path):
    path, paths = db_path
    key = "youtube:canal-a"

    db1 = LocalDatabase(path=path, paths=paths)
    breaker1 = CircuitBreaker(db1, failure_threshold=5, cooldown_seconds=900)
    breaker1.record_failure(key, code="connection_timeout", systemic=True)

    db2 = LocalDatabase(path=path, paths=paths)
    breaker2 = CircuitBreaker(db2, failure_threshold=5, cooldown_seconds=900)
    state = breaker2.get_state(key)
    assert state.state == STATE_CLOSED
    assert state.consecutive_failures == 1
    assert breaker2.is_admitted(key) is True


# ---------------------------------------------------------------------------
# CLAUDE.md ponto 6 "CONCORRÊNCIA": duas instâncias contra o mesmo SQLite,
# corrida determinística via Barrier (nunca teste probabilístico).
# ---------------------------------------------------------------------------


def test_duas_instancias_concorrentes_registrando_falha_nao_perdem_contagem(db_path):
    path, paths = db_path
    key = "youtube:canal-a"
    workers = 6
    barrier = threading.Barrier(workers)
    results: list[bool] = [False] * workers

    def worker(index: int) -> None:
        db = LocalDatabase(path=path, paths=paths, busy_timeout_ms=10_000)
        breaker = CircuitBreaker(db, failure_threshold=workers, cooldown_seconds=900)
        barrier.wait(timeout=5)
        results[index] = breaker.record_failure(key, code="connection_timeout", systemic=True)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # SQLite serializa os escritores via BEGIN IMMEDIATE: as N falhas
    # concorrentes nunca se perdem (nenhuma leitura stale), e exatamente UMA
    # delas -- a que efetivamente atinge o threshold -- abre o circuito.
    assert sum(1 for opened in results if opened) == 1

    inspector = LocalDatabase(path=path, paths=paths)
    final_breaker = CircuitBreaker(inspector, failure_threshold=workers, cooldown_seconds=900)
    state = final_breaker.get_state(key)
    assert state.state == STATE_OPEN
    assert state.consecutive_failures == workers
    events = inspector.list_audit_events(entity_type="circuit_breaker", event_type=CIRCUIT_BREAKER_OPENED_EVENT)
    assert len(events) == 1


def test_duas_instancias_concorrentes_is_admitted_no_fechamento_por_cooldown(db_path):
    """Duas instâncias tentando fechar o mesmo circuito (cooldown já
    decorrido) ao mesmo tempo não podem gerar dois eventos CLOSED nem
    deixar o estado inconsistente -- mesma técnica BEGIN IMMEDIATE."""
    path, paths = db_path
    key = "youtube:canal-a"
    clock = FakeClock()

    setup_db = LocalDatabase(path=path, paths=paths)
    setup_breaker = CircuitBreaker(setup_db, failure_threshold=1, cooldown_seconds=60, clock=clock)
    setup_breaker.record_failure(key, code="connection_timeout", systemic=True)
    clock.advance(60)

    workers = 6
    barrier = threading.Barrier(workers)
    admitted: list[bool] = [False] * workers

    def worker(index: int) -> None:
        db = LocalDatabase(path=path, paths=paths, busy_timeout_ms=10_000)
        breaker = CircuitBreaker(db, failure_threshold=1, cooldown_seconds=60, clock=clock)
        barrier.wait(timeout=5)
        admitted[index] = breaker.is_admitted(key)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(admitted)
    inspector = LocalDatabase(path=path, paths=paths)
    events = inspector.list_audit_events(entity_type="circuit_breaker", event_type=CIRCUIT_BREAKER_CLOSED_EVENT)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 2. Integração com JobEngine: resolução de connector_key e bloqueio
# ---------------------------------------------------------------------------


def _engine_with_breaker(local_db, audit, **breaker_kwargs):
    breaker = CircuitBreaker(local_db, **breaker_kwargs)
    engine = JobEngine(local_db, audit_log=audit, circuit_breaker=breaker)
    return engine, breaker


def test_resolve_connector_key_none_para_job_sem_extra_ou_com_extra_invalido():
    assert JobEngine._resolve_connector_key(Job(operation="X")) is None
    assert JobEngine._resolve_connector_key(Job(operation="X", extra={})) is None
    assert JobEngine._resolve_connector_key(Job(operation="X", extra={"connector_key": ""})) is None
    assert JobEngine._resolve_connector_key(Job(operation="X", extra={"connector_key": "   "})) is None
    assert JobEngine._resolve_connector_key(Job(operation="X", extra={"connector_key": 123})) is None
    assert (
        JobEngine._resolve_connector_key(Job(operation="X", extra={"connector_key": "youtube:c1"}))
        == "youtube:c1"
    )


def test_job_sem_connector_key_nunca_e_bloqueado_mesmo_com_circuito_global_aberto(local_db, audit):
    engine, breaker = _engine_with_breaker(local_db, audit, failure_threshold=1, cooldown_seconds=900)
    # Abre o circuito de QUALQUER connector plausível -- irrelevante, porque
    # o Job não carrega connector_key algum.
    breaker.record_failure("youtube:canal-a", code="connection_timeout", systemic=True)
    breaker.record_failure("youtube", code="connection_timeout", systemic=True)

    job = _create_job(audit, operation="SEM_CONNECTOR")
    engine.register_handler(
        "SEM_CONNECTOR", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    pending = engine.fetch_pending_jobs()
    assert [j.id for j in pending] == [job.id]

    result = engine.advance(job.id)
    assert result.status == JOB_READY


def test_job_com_connector_key_e_circuito_aberto_nao_e_reivindicado(local_db, audit):
    engine, breaker = _engine_with_breaker(local_db, audit, failure_threshold=1, cooldown_seconds=900)
    breaker.record_failure("youtube:canal-a", code="connection_timeout", systemic=True)
    assert breaker.get_state("youtube:canal-a").state == STATE_OPEN

    job = _create_job(audit, operation="PUBLISH", connector_key="youtube:canal-a")
    engine.register_handler(
        "PUBLISH", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    # fetch_pending_jobs já exclui o Job (pré-filtro de leitura).
    assert engine.fetch_pending_jobs() == []

    # advance() direto (contornando fetch_pending_jobs) também é recusado --
    # a autoridade real é _claim(), sob a mesma transaction.
    with pytest.raises(JobBlockedByCircuitBreakerError):
        engine.advance(job.id)

    # Nunca transiciona: o Job permanece exatamente PENDING.
    assert local_db.get(Job, job.id).status == JOB_PENDING


def test_job_com_connector_key_e_circuito_fechado_e_reivindicado_normalmente(local_db, audit):
    engine, breaker = _engine_with_breaker(local_db, audit, failure_threshold=3, cooldown_seconds=900)
    breaker.record_failure("youtube:canal-a", code="connection_timeout", systemic=True)  # 1 de 3: não abre

    job = _create_job(audit, operation="PUBLISH", connector_key="youtube:canal-a")
    engine.register_handler(
        "PUBLISH", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    result = engine.advance(job.id)
    assert result.status == JOB_READY


# ---------------------------------------------------------------------------
# 3. Cenários mistos obrigatórios (seções 1.1 e 3 do Prompt) -- um único
# run_pending() processando o connector saudável enquanto o outro, com
# circuito aberto, fica de fora. Isolamento Job x Job já é provado sem
# ambiguidade por tests/test_job_engine.py::
# test_run_pending_isola_falha_de_um_job_e_continua_os_demais -- não
# duplicado aqui.
# ---------------------------------------------------------------------------


def test_isolamento_conta_x_conta_circuito_aberto_de_uma_conta_nao_bloqueia_outra(local_db, audit):
    """1.1.b: erro no connector 'youtube:conta-a' não pode impedir que
    'youtube:conta-b' (mesma plataforma, conta diferente) seja processado
    no MESMO run_pending()."""
    engine, breaker = _engine_with_breaker(local_db, audit, failure_threshold=2, cooldown_seconds=900)
    breaker.record_failure("youtube:conta-a", code="connection_timeout", systemic=True)
    breaker.record_failure("youtube:conta-a", code="connection_timeout", systemic=True)
    assert breaker.get_state("youtube:conta-a").state == STATE_OPEN
    assert breaker.get_state("youtube:conta-b").state == STATE_CLOSED

    blocked_job = _create_job(audit, operation="PUBLISH", connector_key="youtube:conta-a")
    healthy_job = _create_job(audit, operation="PUBLISH", connector_key="youtube:conta-b")

    calls = []

    def handler(current):
        calls.append(current.id)
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("PUBLISH", handler, claims_status=JOB_PROCESSING)

    outcomes = engine.run_pending()

    # O Job bloqueado nem aparece nos outcomes: fetch_pending_jobs já o
    # excluiu -- exatamente como os demais JobBlockedBy*Error já aprovados
    # (batch pausado, pause/stop_after_current) fazem hoje.
    assert [outcome.job_id for outcome in outcomes] == [healthy_job.id]
    assert calls == [healthy_job.id]

    assert local_db.get(Job, blocked_job.id).status == JOB_PENDING
    assert local_db.get(Job, healthy_job.id).status == JOB_READY


def test_isolamento_plataforma_x_plataforma_circuito_aberto_de_uma_plataforma_nao_bloqueia_outra(
    local_db, audit
):
    """1.1.c: erro na plataforma 'tiktok' não pode impedir que 'youtube'
    seja processado no MESMO run_pending()."""
    engine, breaker = _engine_with_breaker(local_db, audit, failure_threshold=2, cooldown_seconds=900)
    breaker.record_failure("tiktok", code="connection_timeout", systemic=True)
    breaker.record_failure("tiktok", code="connection_timeout", systemic=True)
    assert breaker.get_state("tiktok").state == STATE_OPEN
    assert breaker.get_state("youtube").state == STATE_CLOSED

    blocked_job = _create_job(audit, operation="PUBLISH", connector_key="tiktok")
    healthy_job = _create_job(audit, operation="PUBLISH", connector_key="youtube")

    calls = []

    def handler(current):
        calls.append(current.id)
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("PUBLISH", handler, claims_status=JOB_PROCESSING)

    outcomes = engine.run_pending()

    assert [outcome.job_id for outcome in outcomes] == [healthy_job.id]
    assert calls == [healthy_job.id]

    assert local_db.get(Job, blocked_job.id).status == JOB_PENDING
    assert local_db.get(Job, healthy_job.id).status == JOB_READY


def test_circuito_reabre_normalmente_apos_cooldown_dentro_do_run_pending(local_db, audit):
    """Depois do cooldown decorrido, o Job antes bloqueado volta a ser
    reivindicado/avançado normalmente -- sem nenhuma intervenção manual,
    mesma semântica já aprovada para pause/resume/batch pause (RETRY já em
    _ACTIONABLE_STATES)."""
    clock = FakeClock()
    engine, breaker = _engine_with_breaker(
        local_db, audit, failure_threshold=1, cooldown_seconds=300, clock=clock
    )
    breaker.record_failure("youtube:conta-a", code="connection_timeout", systemic=True)

    job = _create_job(audit, operation="PUBLISH", connector_key="youtube:conta-a")
    engine.register_handler(
        "PUBLISH", lambda current: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING
    )

    assert engine.run_pending() == []
    assert local_db.get(Job, job.id).status == JOB_PENDING

    clock.advance(300)
    outcomes = engine.run_pending()
    assert len(outcomes) == 1
    assert outcomes[0].ok is True
    assert local_db.get(Job, job.id).status == JOB_READY
