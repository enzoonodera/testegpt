# -*- coding: utf-8 -*-
"""RecoveryManager central — detecta e trata Jobs abandonados na inicialização
(FASE 3 / PROMPT 14).

Este módulo é deliberadamente independente de apresentação e de integrações
remotas, no mesmo espírito de ``job_engine.py``:

- não importa nem conhece UI/frontend;
- não importa nem conhece YouTube, TikTok, Instagram ou qualquer outro
  Connector (Connector Contract e reconciliação remota real são etapas
  futuras);
- lê e persiste Jobs exclusivamente através de ``LocalDatabase``/
  ``OperationalAuditLog`` (SQLite local).

PROBLEMA QUE ESTE MÓDULO RESOLVE
---------------------------------
Um ``JobEngine.advance()`` reivindica um Job (PENDING/RETRY -> estado de
trabalho ativo) e só devolve o controle depois de pousar o Job em um estado
final seguro (ver ``job_engine.py``). Isso significa que, em operação normal
dentro de um único processo em execução, nenhum Job deveria ficar parado
indefinidamente em um estado de trabalho ativo ou de transição
(``PROCESSING``, ``PUBLISHING``, ``RECOVERING``, ``UNKNOWN``) ou em
``INTERRUPTED``. Se o processo for encerrado abruptamente (queda de energia,
crash, encerramento forçado do Windows) em QUALQUER ponto entre duas
transições persistidas, o SQLite preserva exatamente o último estado
gravado — mas esse Job fica "travado": nenhum handler jamais o reivindica de
novo, porque ele não está em ``PENDING`` nem em ``RETRY``.

``RecoveryManager.recover_at_startup()`` DEVE ser chamado uma vez, antes de
qualquer ``JobEngine.run_pending()``, quando o fluxo operacional for
conectado ao startup do produto — resolvendo exatamente esse problema. Esta
etapa (PROMPT 14) entrega a classe e seu contrato; a integração real que
chama ``recover_at_startup()`` automaticamente na inicialização do produto
ainda não existe e pertence a uma etapa futura de bootstrap/orquestração.

AS SEIS FRONTEIRAS DE CRASH COBERTAS
--------------------------------------
Cada transição relevante do ciclo de vida tem dois lados: o Job pode ficar
parado ANTES dela (no estado de origem) ou DEPOIS dela mas antes da
seguinte (no estado de destino). Este módulo trata explicitamente as seis
fronteiras entre PENDING e a resolução final de um Job PROCESSING/PUBLISHING:

1. crash deixando o Job em ``PROCESSING`` (nunca chegou a ``INTERRUPTED``):
   detectado por estar em ``PROCESSING`` -> tratado por
   ``_recover_local_work`` a partir do estágio ``"PROCESSING"``.
2. crash logo depois de ``PROCESSING -> INTERRUPTED`` (nunca chegou a
   ``RECOVERING``): detectado por estar em ``INTERRUPTED`` -> tratado por
   ``_recover_local_work`` a partir do estágio ``"INTERRUPTED"``.
3. crash logo depois de ``INTERRUPTED -> RECOVERING`` (a decisão
   READY/FAILED nunca foi persistida): detectado por estar em
   ``RECOVERING`` cuja origem (ver abaixo) é classificada como LOCAL ->
   tratado por ``_recover_local_work`` a partir do estágio ``"RECOVERING"``,
   que pula as transições já feitas e retoma exatamente na decisão
   pendente.
4. crash deixando o Job em ``PUBLISHING`` (nunca chegou a ``UNKNOWN``):
   detectado por estar em ``PUBLISHING`` -> tratado por
   ``_recover_uncertain_remote_work``, que transiciona
   ``PUBLISHING -> UNKNOWN -> RECOVERING``.
5. crash logo depois de ``PUBLISHING -> UNKNOWN`` (nunca chegou a
   ``RECOVERING``): era o BLOQUEADOR 1 da revisão — antes desta correção,
   ``UNKNOWN`` não pertencia a ``RECOVERABLE_STATES`` e o Job ficava
   esquecido para sempre. Agora ``UNKNOWN`` é detectado explicitamente e
   tratado por ``_recover_pending_unknown``, que aplica a única transição
   que a State Machine central permite a partir dali:
   ``UNKNOWN -> RECOVERING``.
6. crash logo depois de ``UNKNOWN -> RECOVERING`` (a reconciliação remota
   real nunca foi persistida): detectado por estar em ``RECOVERING`` cuja
   origem é classificada como REMOTA -> tratado por
   ``_acknowledge_already_recovering``, que não força nenhuma transição
   nova (ver "RECOVERING tem mais de uma origem" abaixo).

Em nenhuma dessas seis fronteiras o Job permanece esquecido para sempre:
toda vez que ``recover_at_startup()`` roda, ele volta a encontrar o Job (em
qualquer um dos estados acima) e retoma exatamente do ponto em que parou,
de forma idempotente mesmo através de múltiplos crashes consecutivos.

RECOVERING TEM MAIS DE UMA ORIGEM (BLOQUEADOR 2 da revisão)
--------------------------------------------------------------
``RECOVERING`` é alcançado por pelo menos três caminhos com significados
diferentes:

- ``INTERRUPTED -> RECOVERING``: recuperação de trabalho LOCAL interrompido
  (fronteira 3 acima) — ainda falta decidir READY/FAILED.
- ``UNKNOWN -> RECOVERING``: reconciliação de um efeito REMOTO incerto
  (fronteira 6 acima) — a decisão pertence a um Connector futuro.
- ``RETRY -> RECOVERING`` (também uma transição válida na State Machine
  central, ex.: um handler registrado com ``claims_status=RECOVERING`` que
  reivindicou o Job e travou no meio do próprio trabalho de reconciliação):
  a origem semântica desse trabalho não é conhecida por este módulo — pode
  ser local ou remota, dependendo do que aquele handler faz. Tratar esse
  caso como se fosse necessariamente local (e arriscar decidir READY/FAILED
  sobre um efeito potencialmente remoto) ou necessariamente remoto seria
  presumir sem evidência.

Antes desta correção, TODO Job encontrado em ``RECOVERING`` na inicialização
era tratado como remoto (``_acknowledge_already_recovering``), mesmo quando
a cadeia de transições persistida no Audit Log provava que a origem era
puramente local (``PROCESSING -> INTERRUPTED -> RECOVERING``). Isso deixava
Jobs locais presos em ``RECOVERING`` para sempre, e incorretamente os listava
como pendentes de reconciliação remota.

A correção usa exclusivamente evidência já persistida — nenhum estado novo
em memória, nenhuma migration nova: ``_classify_recovering_origin`` lê
``OperationalAuditLog.list_job_events`` (a mesma trilha append-only já usada
por ``reconstruct_job_history``) e localiza o evento
``JOB_STATE_CHANGED`` mais recente cujo ``to_state`` é ``RECOVERING``. O
``from_state`` desse evento é a evidência decisiva:

- ``from_state == INTERRUPTED`` -> origem LOCAL -> retoma a decisão
  READY/FAILED via ``_recover_local_work``.
- ``from_state == UNKNOWN`` -> origem REMOTA -> apenas confirma que a
  reconciliação continua pendente, sem forçar transição
  (``_acknowledge_already_recovering``).
- qualquer outro ``from_state`` (ex.: ``RETRY``), ou nenhuma evidência
  encontrada -> origem AMBÍGUA. Este é um caso em que o RecoveryManager
  **delibera por não decidir**: nenhuma transição é forçada, o Job permanece
  em ``RECOVERING`` e é reportado separadamente
  (``ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED``) para visibilidade, em vez de
  arriscar uma decisão sem base suficiente.

POLÍTICA DE RECUPERAÇÃO DE TRABALHO LOCAL (PROCESSING/INTERRUPTED/RECOVERING-LOCAL)
--------------------------------------------------------------------------------------
Reavaliação desta revisão: a versão anterior pousava automaticamente em
``RETRY`` sempre que nenhum ``artifact_validator`` confirmava o resultado,
justificando isso apenas por "reprocessar nunca corrompe o vídeo original"
(CLAUDE.md, princípio B). Essa justificativa cobre não-destrutividade, mas
não idempotência: "não destrutivo" garante que o arquivo original nunca é
alterado, mas não garante que repetir o handler de uma ``operation``
qualquer seja seguro sem efeitos colaterais (ex.: reenviar algo a um serviço
externo não local, duplicar uma entrada de fila, incrementar um contador
observável). O RecoveryManager não conhece o que cada ``operation``
registrada faz internamente (esse é o contrato deliberado do JobEngine,
ver ``job_engine.py``), então não tem base para presumir que refazer é
seguro nesse sentido mais amplo.

Por isso, o destino padrão sem evidência positiva agora é ``FAILED``, não
``RETRY`` — exatamente a mesma política já aprovada em ``job_engine.py``
para uma exceção durante ``PROCESSING`` (trabalho local sem confirmação de
conclusão -> falha conhecida que exige decisão explícita antes de qualquer
nova tentativa). ``FAILED`` não é uma estado acionável pelo ``JobEngine``
(não está em ``_ACTIONABLE_STATES``): sair dali exige uma chamada explícita
(``OperationalAuditLog.retry``), nunca uma nova tentativa automática. Isso
não implementa uma ``RetryPolicy`` (PROMPT 17+): apenas evita repetir
automaticamente um efeito cuja segurança de repetição não foi demonstrada.

``READY`` continua sendo o destino apenas quando um ``artifact_validator``
explicitamente fornecido confirmar, com evidência positiva, que o resultado
do passo interrompido já existe e é válido — nesse caso não há repetição
alguma, então a questão de idempotência não se aplica.

VERIFICAÇÃO DE ARTIFACTS/ARQUIVOS TEMPORÁRIOS: O QUE ESTE MÓDULO PODE E NÃO
PODE FAZER NESTA ETAPA
------------------------------------------------------------------------------
O roadmap do PROMPT 14 pede "verificar artifacts" e "validar arquivos
temporários" antes de decidir a ação seguinte. Este módulo não implementa
verificação de integridade de arquivo (hash, duração de vídeo, abertura via
FFmpeg, tamanho mínimo esperado, etc.) nem um `StorageManager`/`RenderEngine`
— isso pertence a etapas futuras que efetivamente sabem produzir e
interpretar esses artifacts. O ponto de extensão é o ``artifact_validator``
opcional: um callable fornecido pelo chamador (que já conhece os artifacts
esperados de cada ``operation``) que decide, com uma verificação real, se o
resultado existe e é válido. Importante: a mera existência de um arquivo, ou
seu nome parecer correto, nunca é suficiente por si só — mas essa validação
é responsabilidade de quem implementa o ``artifact_validator``, não deste
módulo. Sem um validador (ou se ele lançar exceção), a decisão nunca presume
validade: cai no destino conservador (``FAILED``) descrito acima. Checkpoints
persistidos (PROMPT 13) também não são consultados automaticamente aqui para
decidir READY/FAILED: um checkpoint prova que uma etapa foi *alcançada* em
algum momento, não que os artifacts daquela etapa sobreviveram ao crash no
disco — continuam disponíveis, através de ``OperationalAuditLog``, para que
um ``artifact_validator`` os use como um dos sinais de sua própria decisão,
se o chamador escolher fazer isso.

POLÍTICA DE RECUPERAÇÃO DE TRABALHO REMOTO INCERTO (PUBLISHING/UNKNOWN)
---------------------------------------------------------------------------
``PUBLISHING`` e ``UNKNOWN`` podem ter iniciado, ou estar prestes a
confirmar, um efeito remoto observável fora do processo (ex.: upload em
voo quando a energia caiu). CLAUDE.md (princípio M e seção CONFIABILIDADE)
proíbe presumir que a operação remota falhou só porque o processo caiu.
Por isso este módulo:

- nunca transiciona ``PUBLISHING``/``UNKNOWN`` diretamente para ``FAILED``,
  ``PUBLISHED`` ou ``RETRY``;
- sempre os encaminha para ``RECOVERING`` (via ``UNKNOWN`` quando ainda não
  estava lá), que é o único destino que a State Machine central permite a
  partir de ``UNKNOWN``;
- não decide o resultado da reconciliação remota — isso exige um Connector
  concreto (etapa futura). O RecoveryManager apenas garante, de forma
  auditável e persistida, que o Job fique exatamente onde a reconciliação
  futura vai procurá-lo.

O QUE ESTE MÓDULO DELIBERADAMENTE NÃO FAZ
-------------------------------------------
Não decide pause/resume/stop/cancel (PROMPT 15), não implementa graceful
shutdown (PROMPT 16), não agenda nem prioriza trabalho em lote (PROMPT 17) e
não gerencia recursos (PROMPT 18). Não conhece Connectors nem tenta consultar
uma plataforma remota para confirmar o resultado de um upload. Não
implementa RetryPolicy nem verificação de idempotência por operation (isso
exigiria um contrato ainda não definido entre JobEngine e cada handler).
Não força uma transição sobre um Job em ``RECOVERING`` cuja origem não pode
ser determinada com evidência persistida — esse é um caso deliberado de
"prefiro não decidir automaticamente" (ver ``ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .domain import (
    Job,
    InvalidJobState,
    InvalidJobTransition,
    JOB_INTERRUPTED,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_UNKNOWN,
)
from .job_engine import CLAIMABLE_STATES
from .storage import (
    AUDIT_JOB_STATE_CHANGED,
    JobNotFoundForAuditError,
    LocalDatabase,
    OperationalAuditError,
    OperationalAuditLog,
)


class RecoveryManagerError(RuntimeError):
    """Erro de contrato do RecoveryManager central."""


class JobRecoveryError(RecoveryManagerError):
    """Falha ao recuperar um Job específico.

    Assim como em ``job_engine.JobRunOutcome``, essa falha é isolada por Job:
    ela nunca interrompe a recuperação dos demais Jobs abandonados.
    """


# Ações que o RecoveryManager pode ter tomado para um Job encontrado
# abandonado. São valores descritivos (não um vocabulário validado pela State
# Machine central — essa validação já acontece nas transições em si).
ACTION_MARKED_FAILED_FOR_MANUAL_RETRY = "MARKED_FAILED_FOR_MANUAL_RETRY"
ACTION_RESUMED_READY = "RESUMED_READY"
ACTION_FLAGGED_FOR_RECONCILIATION = "FLAGGED_FOR_RECONCILIATION"
ACTION_ALREADY_RECOVERING = "ALREADY_RECOVERING"
ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED = "AMBIGUOUS_RECOVERING_ACKNOWLEDGED"

# Estados que, encontrados na inicialização, indicam um Job abandonado por um
# encerramento anterior não controlado. Além dos estados de trabalho ativo
# que um JobEngine pode reivindicar (``CLAIMABLE_STATES``), inclui:
# - ``INTERRUPTED``: um Job pode ficar parado ali se o próprio processo de
#   recuperação for interrompido antes de decidir a ação segura;
# - ``UNKNOWN``: um Job pode ficar parado ali se o processo cair entre
#   ``PUBLISHING -> UNKNOWN`` e ``UNKNOWN -> RECOVERING`` (BLOQUEADOR 1).
RECOVERABLE_STATES = frozenset(CLAIMABLE_STATES | {JOB_INTERRUPTED, JOB_UNKNOWN})

# Origem classificada de um Job encontrado em RECOVERING, com base somente em
# evidência já persistida no Audit Log (ver _classify_recovering_origin).
_ORIGIN_LOCAL = "LOCAL"
_ORIGIN_REMOTE = "REMOTE"
_ORIGIN_AMBIGUOUS = "AMBIGUOUS"

# Verificador opcional fornecido pelo chamador: recebe o Job (já em
# RECOVERING) e devolve True somente se houver evidência POSITIVA e real de
# que o resultado do passo interrompido já existe e é válido (nunca apenas
# "o arquivo existe" ou "o nome parece certo" — isso é responsabilidade da
# implementação do validador, não deste módulo). Nunca é obrigatório; na
# ausência de um validador (ou se ele não confirmar, ou lançar exceção), a
# ação segura padrão para trabalho local interrompido é conservadora
# (``FAILED``, exigindo decisão explícita antes de qualquer nova tentativa),
# nunca uma repetição automática presumida como segura.
ArtifactValidator = Callable[[Job], bool]


@dataclass(frozen=True)
class JobRecoveryResult:
    """Resultado isolado da recuperação de um único Job abandonado."""

    job_id: str
    previous_status: str
    action: str | None
    job: Job | None
    error: RecoveryManagerError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class RecoveryReport:
    """Resultado agregado de ``RecoveryManager.recover_at_startup()``."""

    results: tuple[JobRecoveryResult, ...]

    @property
    def recovered_job_ids(self) -> tuple[str, ...]:
        return tuple(result.job_id for result in self.results if result.ok)

    @property
    def failed_job_ids(self) -> tuple[str, ...]:
        return tuple(result.job_id for result in self.results if not result.ok)

    @property
    def jobs_pending_reconciliation(self) -> tuple[str, ...]:
        """Jobs que ficaram (ou já estavam) aguardando alguma decisão externa:
        reconciliação remota real, ou uma origem de RECOVERING que este
        módulo deliberadamente não teve evidência suficiente para resolver.
        """
        return tuple(
            result.job_id
            for result in self.results
            if result.ok
            and result.action
            in (
                ACTION_FLAGGED_FOR_RECONCILIATION,
                ACTION_ALREADY_RECOVERING,
                ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED,
            )
        )


class RecoveryManager:
    """Detecta e trata Jobs abandonados por um encerramento não controlado.

    ``recover_at_startup()`` DEVE ser chamado uma única vez na
    inicialização, antes de qualquer ``JobEngine.run_pending()``, quando o
    fluxo operacional for conectado ao startup do produto — a ordem importa:
    só depois que todo Job abandonado tiver sido pousado em um destino
    seguro é que o JobEngine deve começar a reivindicar trabalho novo. Essa
    chamada na inicialização ainda não está integrada a nenhum bootstrap do
    produto (etapa futura); esta classe entrega o contrato e o comportamento
    a serem usados quando essa integração existir. Os quatro destinos
    possíveis são:

    - trabalho local (``PROCESSING``/``INTERRUPTED``/``RECOVERING`` de
      origem local) com resultado confirmado por um ``artifact_validator``
      -> ``READY``;
    - trabalho local sem evidência suficiente de conclusão -> ``FAILED``,
      exigindo uma decisão manual explícita antes de qualquer nova tentativa
      (nunca um ``RETRY`` automático — ver docstring do módulo);
    - efeito remoto potencialmente incerto (``PUBLISHING``/``UNKNOWN``) ->
      ``RECOVERING``, aguardando reconciliação real por um Connector futuro;
    - ``RECOVERING`` cuja origem (local ou remota) não pôde ser determinada
      com evidência persistida -> nenhuma transição forçada; o Job
      permanece em ``RECOVERING`` e é reportado separadamente como um caso
      deliberado de "não decidir automaticamente".
    """

    def __init__(
        self,
        database: LocalDatabase,
        *,
        audit_log: OperationalAuditLog | None = None,
        artifact_validator: ArtifactValidator | None = None,
    ) -> None:
        self.database = database
        self.audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self.artifact_validator = artifact_validator

    def find_abandoned_jobs(self) -> list[Job]:
        """Busca no SQLite todo Job parado em um estado recuperável.

        Sempre lê do storage: a lista de Jobs abandonados nunca é mantida em
        memória entre reinicializações.
        """
        return [job for job in self.database.list(Job) if job.status in RECOVERABLE_STATES]

    def recover_at_startup(self) -> RecoveryReport:
        """Recupera todos os Jobs abandonados encontrados, isolando falhas.

        Cada Job é tratado individualmente; uma falha ao recuperar um Job
        nunca impede a recuperação dos demais (mesmo princípio de isolamento
        de ``JobEngine.run_pending``).
        """
        results: list[JobRecoveryResult] = []
        for job in self.find_abandoned_jobs():
            previous_status = job.status
            try:
                updated, action = self._recover_job(job)
                results.append(
                    JobRecoveryResult(
                        job_id=job.id,
                        previous_status=previous_status,
                        action=action,
                        job=updated,
                        error=None,
                    )
                )
            except RecoveryManagerError as exc:
                results.append(
                    JobRecoveryResult(
                        job_id=job.id,
                        previous_status=previous_status,
                        action=None,
                        job=None,
                        error=exc,
                    )
                )
        return RecoveryReport(results=tuple(results))

    def _recover_job(self, job: Job) -> tuple[Job, str]:
        if job.status == JOB_PROCESSING:
            return self._recover_local_work(job, stage="PROCESSING")
        if job.status == JOB_INTERRUPTED:
            return self._recover_local_work(job, stage="INTERRUPTED")
        if job.status == JOB_PUBLISHING:
            return self._recover_uncertain_remote_work(job)
        if job.status == JOB_UNKNOWN:
            return self._recover_pending_unknown(job, already_unknown=True)
        if job.status == JOB_RECOVERING:
            return self._recover_found_in_recovering(job)
        # RECOVERABLE_STATES só contém os estados acima; chegar aqui
        # indicaria um novo estado adicionado a RECOVERABLE_STATES sem uma
        # política de recuperação correspondente.
        raise JobRecoveryError(
            f"Job {job.id} está em {job.status!r}, que não possui política de "
            "recuperação definida"
        )

    # -- Trabalho local (PROCESSING/INTERRUPTED/RECOVERING de origem local) --

    def _recover_local_work(self, job: Job, *, stage: str) -> tuple[Job, str]:
        """Trata trabalho local abandonado, em qualquer ponto entre
        ``PROCESSING`` e a decisão final ainda não persistida.

        ``stage`` indica de onde a recuperação está retomando, para não
        repetir uma transição que já foi persistida antes do crash:

        - ``"PROCESSING"``: nunca chegou a ``INTERRUPTED``; aplica as duas
          transições completas (``-> INTERRUPTED -> RECOVERING``).
        - ``"INTERRUPTED"``: já chegou a ``INTERRUPTED`` num run anterior;
          só falta ``-> RECOVERING``.
        - ``"RECOVERING"``: já chegou a ``RECOVERING`` (origem confirmada
          como LOCAL por ``_classify_recovering_origin``) e o crash ocorreu
          antes da decisão final ser persistida; nenhuma transição de
          entrada é necessária, só a decisão.

        Em todos os casos a decisão final é a mesma: ``READY`` somente com
        confirmação positiva de um ``artifact_validator``; caso contrário,
        ``FAILED`` (ver docstring do módulo sobre por que não é mais
        ``RETRY`` automático).
        """
        try:
            if stage == "PROCESSING":
                self.audit_log.record_recovery(job.id, phase="PROCESSING_INTERRUPTED_DETECTED")
                job = self.audit_log.transition_job(
                    job.id,
                    JOB_INTERRUPTED,
                    data={"reason": "found_processing_at_startup"},
                )
                job = self.audit_log.transition_job(
                    job.id,
                    JOB_RECOVERING,
                    data={"reason": "evaluating_safe_action"},
                )
            elif stage == "INTERRUPTED":
                self.audit_log.record_recovery(
                    job.id,
                    phase="INTERRUPTED_RECOVERY_RESUMED",
                    data={"reason": "found_interrupted_at_startup"},
                )
                job = self.audit_log.transition_job(
                    job.id,
                    JOB_RECOVERING,
                    data={"reason": "evaluating_safe_action"},
                )
            else:  # stage == "RECOVERING", origem local já confirmada
                self.audit_log.record_recovery(
                    job.id,
                    phase="LOCAL_RECOVERY_DECISION_RESUMED_AFTER_RESTART",
                    data={"reason": "found_recovering_with_local_origin_at_startup"},
                )

            artifacts_valid = self._validate_artifacts(job)
            target = JOB_READY if artifacts_valid else JOB_FAILED
            action = ACTION_RESUMED_READY if artifacts_valid else ACTION_MARKED_FAILED_FOR_MANUAL_RETRY

            job = self.audit_log.transition_job(
                job.id,
                target,
                data={"reason": "recovery_decision", "artifacts_valid": artifacts_valid},
            )
            return job, action
        except (InvalidJobState, InvalidJobTransition, JobNotFoundForAuditError, OperationalAuditError) as exc:
            raise JobRecoveryError(
                f"falha ao recuperar Job local {job.id}: {exc}"
            ) from exc

    def _validate_artifacts(self, job: Job) -> bool:
        """Consulta o ``artifact_validator`` opcional, com falha segura.

        Qualquer exceção do validador (ou a ausência de um validador) é
        tratada como "não confirmado": nunca presume conclusão sem evidência
        positiva explícita.
        """
        if self.artifact_validator is None:
            return False
        try:
            return bool(self.artifact_validator(job))
        except Exception as exc:  # noqa: BLE001 - falha do validador nunca deve derrubar a recuperação
            try:
                self.audit_log.record_error(
                    job.id,
                    code="RECOVERY_ARTIFACT_VALIDATION_FAILED",
                    message=str(exc),
                    recoverable=True,
                )
            except OperationalAuditError:
                pass
            return False

    # -- Efeito remoto incerto (PUBLISHING/UNKNOWN) --------------------------

    def _recover_uncertain_remote_work(self, job: Job) -> tuple[Job, str]:
        """Trata PUBLISHING abandonado: efeito remoto potencialmente em
        andamento no momento do crash. Nunca presume falha nem sucesso —
        marca a incerteza (``UNKNOWN``) e delega a ``_recover_pending_unknown``
        o encaminhamento final para ``RECOVERING``.
        """
        try:
            self.audit_log.record_recovery(job.id, phase="PUBLISHING_INTERRUPTED_DETECTED")
            job = self.audit_log.transition_job(
                job.id,
                JOB_UNKNOWN,
                data={"reason": "found_publishing_at_startup"},
            )
        except (InvalidJobState, InvalidJobTransition, JobNotFoundForAuditError, OperationalAuditError) as exc:
            raise JobRecoveryError(
                f"falha ao recuperar Job com publicação incerta {job.id}: {exc}"
            ) from exc
        return self._recover_pending_unknown(job, already_unknown=False)

    def _recover_pending_unknown(self, job: Job, *, already_unknown: bool) -> tuple[Job, str]:
        """Encaminha um Job em ``UNKNOWN`` para ``RECOVERING``.

        Cobre tanto o caso de ter acabado de chegar a ``UNKNOWN`` nesta
        mesma chamada (vindo de ``_recover_uncertain_remote_work``) quanto o
        caso, antes não tratado (BLOQUEADOR 1), de encontrar o Job já em
        ``UNKNOWN`` diretamente na inicialização — ou seja, o processo caiu
        exatamente entre ``PUBLISHING -> UNKNOWN`` e ``UNKNOWN -> RECOVERING``
        numa execução anterior do próprio RecoveryManager. Em ambos os casos
        a única transição permitida pela State Machine central a partir de
        ``UNKNOWN`` é ``RECOVERING``, e o resultado da reconciliação real
        continua sendo responsabilidade de um Connector futuro.
        """
        try:
            if already_unknown:
                self.audit_log.record_recovery(
                    job.id,
                    phase="UNKNOWN_FOUND_AT_STARTUP_BEFORE_RECOVERING",
                    data={"reason": "found_unknown_at_startup"},
                )
            job = self.audit_log.reconciliation_started(
                job.id,
                data={"reason": "pending_remote_reconciliation_after_restart"},
            )
            return job, ACTION_FLAGGED_FOR_RECONCILIATION
        except (InvalidJobState, InvalidJobTransition, JobNotFoundForAuditError, OperationalAuditError) as exc:
            raise JobRecoveryError(
                f"falha ao recuperar Job com reconciliação pendente {job.id}: {exc}"
            ) from exc

    # -- RECOVERING encontrado diretamente: desambiguação por evidência -----

    def _recover_found_in_recovering(self, job: Job) -> tuple[Job, str]:
        origin = self._classify_recovering_origin(job.id)
        if origin == _ORIGIN_LOCAL:
            return self._recover_local_work(job, stage="RECOVERING")
        if origin == _ORIGIN_REMOTE:
            return self._acknowledge_already_recovering(job)
        return self._acknowledge_ambiguous_recovering(job)

    def _classify_recovering_origin(self, job_id: str) -> str:
        """Determina, usando somente evidência já persistida no Audit Log, se
        um Job encontrado em ``RECOVERING`` chegou ali por recuperação de
        trabalho LOCAL (``INTERRUPTED -> RECOVERING``) ou por reconciliação
        de efeito REMOTO incerto (``UNKNOWN -> RECOVERING``).

        Localiza o evento ``JOB_STATE_CHANGED`` mais recente cujo
        ``to_state`` é ``RECOVERING`` e inspeciona seu ``from_state`` — o
        mesmo par ``from_state``/``to_state`` que
        ``reconstruct_job_history`` já usa para validar a cadeia completa de
        transições. Nenhum estado novo em memória, nenhuma migration nova:
        reaproveita integralmente a trilha append-only já existente.

        Se o ``from_state`` não for nem ``INTERRUPTED`` nem ``UNKNOWN`` (ex.:
        ``RETRY -> RECOVERING``, uma transição também válida cuja origem
        semântica este módulo não pode inferir), ou se nenhuma evidência for
        encontrada, o resultado é ``AMBIGUOUS`` — nunca presumido como um dos
        dois casos conhecidos.
        """
        events = self.audit_log.list_job_events(job_id)
        for event in reversed(events):
            if event.get("event_type") != AUDIT_JOB_STATE_CHANGED:
                continue
            data = event.get("data") or {}
            if data.get("to_state") != JOB_RECOVERING:
                continue
            source = data.get("from_state")
            if source == JOB_INTERRUPTED:
                return _ORIGIN_LOCAL
            if source == JOB_UNKNOWN:
                return _ORIGIN_REMOTE
            return _ORIGIN_AMBIGUOUS
        return _ORIGIN_AMBIGUOUS

    def _acknowledge_already_recovering(self, job: Job) -> tuple[Job, str]:
        """Trata RECOVERING de origem REMOTA encontrado diretamente na
        inicialização (crash entre ``UNKNOWN -> RECOVERING`` e a
        reconciliação real subsequente).

        Não força nenhuma transição: o Job já está exatamente no estado que
        sinaliza reconciliação pendente, e forçar uma transição aqui poderia
        descartar um resultado de reconciliação que outra parte do sistema
        ainda esteja no processo de persistir. Apenas registra, para
        diagnóstico, que ele foi encontrado nesse estado numa nova
        inicialização.
        """
        try:
            self.audit_log.record_recovery(job.id, phase="ALREADY_RECOVERING_AT_STARTUP")
        except (JobNotFoundForAuditError, OperationalAuditError) as exc:
            raise JobRecoveryError(
                f"falha ao registrar recuperação de Job já em RECOVERING {job.id}: {exc}"
            ) from exc
        return job, ACTION_ALREADY_RECOVERING

    def _acknowledge_ambiguous_recovering(self, job: Job) -> tuple[Job, str]:
        """Trata RECOVERING cuja origem não pôde ser determinada com
        evidência persistida suficiente (ex.: ``RETRY -> RECOVERING``).

        Caso deliberado de "prefiro não decidir automaticamente": nenhuma
        transição é forçada, para não arriscar tratar um efeito
        potencialmente remoto como se fosse local (ou vice-versa) sem prova.
        Reportado separadamente de ``ACTION_ALREADY_RECOVERING`` para deixar
        essa incerteza visível a quem ler o relatório.
        """
        try:
            self.audit_log.record_recovery(
                job.id,
                phase="RECOVERING_ORIGIN_AMBIGUOUS_NOT_AUTO_RESOLVED",
                data={"reason": "no_conclusive_evidence_of_origin"},
            )
        except (JobNotFoundForAuditError, OperationalAuditError) as exc:
            raise JobRecoveryError(
                f"falha ao registrar recuperação de Job em RECOVERING ambíguo {job.id}: {exc}"
            ) from exc
        return job, ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED


__all__ = [
    "RecoveryManager",
    "RecoveryManagerError",
    "JobRecoveryError",
    "JobRecoveryResult",
    "RecoveryReport",
    "RECOVERABLE_STATES",
    "ArtifactValidator",
    "ACTION_MARKED_FAILED_FOR_MANUAL_RETRY",
    "ACTION_RESUMED_READY",
    "ACTION_FLAGGED_FOR_RECONCILIATION",
    "ACTION_ALREADY_RECOVERING",
    "ACTION_AMBIGUOUS_RECOVERING_ACKNOWLEDGED",
]
