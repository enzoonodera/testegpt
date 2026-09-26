import pytest

from _sistema.domain import (
    InvalidJobState,
    InvalidJobTransition,
    Job,
    JobStateMachine,
    JOB_AUTH_REQUIRED,
    JOB_BLOCKED,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_PAUSED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHED,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_RETRY,
    JOB_SCHEDULED,
    JOB_STATES,
    JOB_UNKNOWN,
    JOB_USER_ACTION_REQUIRED,
)


EXPECTED_STATES = {
    "PENDING",
    "PROCESSING",
    "READY",
    "PAUSED",
    "INTERRUPTED",
    "RECOVERING",
    "SCHEDULED",
    "PUBLISHING",
    "PUBLISHED",
    "RETRY",
    "FAILED",
    "BLOCKED",
    "UNKNOWN",
    "AUTH_REQUIRED",
    "USER_ACTION_REQUIRED",
    "CANCELLED",
}


def test_state_machine_expoe_todos_os_estados_minimos():
    assert JOB_STATES == EXPECTED_STATES


@pytest.mark.parametrize("state", sorted(EXPECTED_STATES))
def test_job_aceita_cada_estado_canonico_na_criacao(state):
    job = Job(status=state)
    assert job.status == state


def test_job_rejeita_estado_invalido_na_criacao():
    with pytest.raises(InvalidJobState):
        Job(status="RUNNING")


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (JOB_PENDING, JOB_PROCESSING),
        (JOB_PROCESSING, JOB_READY),
        (JOB_PENDING, JOB_PAUSED),
        (JOB_PROCESSING, JOB_INTERRUPTED),
        (JOB_INTERRUPTED, JOB_RECOVERING),
        (JOB_READY, JOB_SCHEDULED),
        (JOB_READY, JOB_PUBLISHING),
        (JOB_PUBLISHING, JOB_PUBLISHED),
        (JOB_PROCESSING, JOB_RETRY),
        (JOB_PROCESSING, JOB_FAILED),
        (JOB_PENDING, JOB_BLOCKED),
        (JOB_PUBLISHING, JOB_UNKNOWN),
        (JOB_PENDING, JOB_AUTH_REQUIRED),
        (JOB_PENDING, JOB_USER_ACTION_REQUIRED),
        (JOB_PENDING, JOB_CANCELLED),
    ],
)
def test_transicoes_representativas_sao_validas(source, target):
    assert JobStateMachine.can_transition(source, target) is True
    job = Job(status=source)
    job.transition_to(target)
    assert job.status == target


def test_published_nao_pode_voltar_para_pending():
    job = Job(status=JOB_PUBLISHED)

    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_PENDING)

    assert job.status == JOB_PUBLISHED
    assert job.allowed_transitions() == frozenset()


def test_cancelled_e_terminal():
    job = Job(status=JOB_CANCELLED)
    assert JobStateMachine.is_terminal(job.status) is True
    assert job.allowed_transitions() == frozenset()

    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_PENDING)


def test_unknown_precisa_passar_por_recovering_antes_de_retry():
    job = Job(status=JOB_UNKNOWN)

    assert job.allowed_transitions() == frozenset({JOB_RECOVERING})
    assert JobStateMachine.can_transition(JOB_UNKNOWN, JOB_RECOVERING) is True

    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_RETRY)
    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_PUBLISHING)

    job.transition_to(JOB_RECOVERING)
    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_PUBLISHING)
    job.transition_to(JOB_RETRY)
    job.transition_to(JOB_PUBLISHING)
    assert job.status == JOB_PUBLISHING


def test_recovering_pode_reconciliar_publicacao_confirmada():
    job = Job(status=JOB_UNKNOWN)
    job.transition_to(JOB_RECOVERING)
    job.transition_to(JOB_PUBLISHED)
    assert job.status == JOB_PUBLISHED


def test_recovering_pode_voltar_para_unknown_se_reconciliacao_inconclusiva():
    job = Job(status=JOB_UNKNOWN)
    job.transition_to(JOB_RECOVERING)
    job.transition_to(JOB_UNKNOWN)
    assert job.status == JOB_UNKNOWN


def test_status_nao_pode_ser_alterado_diretamente_para_contornar_maquina():
    job = Job(status=JOB_PENDING)

    with pytest.raises(ValueError, match="transition_to"):
        job.status = JOB_PUBLISHED

    assert job.status == JOB_PENDING


def test_transicao_para_estado_desconhecido_e_rejeitada():
    job = Job(status=JOB_PENDING)
    with pytest.raises(InvalidJobState):
        job.transition_to("INVALID")


def test_roundtrip_preserva_estado_valido_sem_executar_transicao():
    job = Job(operation="RENDER", status=JOB_PROCESSING, progress=0.25)
    restored = Job.from_dict(job.to_dict())

    assert restored.id == job.id
    assert restored.status == JOB_PROCESSING
    assert restored.progress == 0.25


def test_from_dict_rejeita_estado_de_job_invalido():
    job = Job()
    payload = job.to_dict()
    payload["status"] = "RUNNING"

    with pytest.raises(InvalidJobState):
        Job.from_dict(payload)


def test_todas_as_origens_da_tabela_tem_destinos_validos():
    assert set(JobStateMachine.transitions) == EXPECTED_STATES
    for source, targets in JobStateMachine.transitions.items():
        assert source in EXPECTED_STATES
        assert set(targets).issubset(EXPECTED_STATES)


def test_unknown_nao_tem_aresta_direta_para_retry_ou_publishing():
    targets = JobStateMachine.allowed_targets(JOB_UNKNOWN)
    assert JOB_RETRY not in targets
    assert JOB_PUBLISHING not in targets
    assert targets == {JOB_RECOVERING}


def test_publishing_para_retry_direto_e_rejeitado():
    job = Job(status=JOB_PUBLISHING)

    assert JobStateMachine.can_transition(JOB_PUBLISHING, JOB_RETRY) is False
    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_RETRY)

    assert job.status == JOB_PUBLISHING


def test_publishing_para_unknown_continua_permitido():
    job = Job(status=JOB_PUBLISHING)

    assert JobStateMachine.can_transition(JOB_PUBLISHING, JOB_UNKNOWN) is True
    job.transition_to(JOB_UNKNOWN)

    assert job.status == JOB_UNKNOWN


def test_unknown_para_retry_continua_rejeitado():
    job = Job(status=JOB_UNKNOWN)

    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_RETRY)

    assert job.status == JOB_UNKNOWN


def test_unknown_recovering_retry_continua_permitido():
    job = Job(status=JOB_UNKNOWN)

    job.transition_to(JOB_RECOVERING)
    job.transition_to(JOB_RETRY)

    assert job.status == JOB_RETRY


def test_publishing_failed_retry_representa_falha_conhecida():
    job = Job(status=JOB_PUBLISHING)

    job.transition_to(JOB_FAILED)
    job.transition_to(JOB_RETRY)

    assert job.status == JOB_RETRY


def test_published_permanece_terminal_sem_saida():
    job = Job(status=JOB_PUBLISHED)

    assert JobStateMachine.is_terminal(JOB_PUBLISHED) is True
    assert job.allowed_transitions() == frozenset()
    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_RETRY)


def test_cancelled_permanece_terminal_sem_saida():
    job = Job(status=JOB_CANCELLED)

    assert JobStateMachine.is_terminal(JOB_CANCELLED) is True
    assert job.allowed_transitions() == frozenset()
    with pytest.raises(InvalidJobTransition):
        job.transition_to(JOB_RETRY)
