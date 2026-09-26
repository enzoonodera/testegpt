# -*- coding: utf-8 -*-
"""Audit log operacional append-only para o ciclo de vida de Jobs.

A camada é local, independente de UI/connectors e usa ``audit_events`` do
SQLite operacional. Mudanças de estado persistidas por esta API são atômicas:
o Job e os eventos de auditoria são gravados na mesma transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from ..domain import (
    Job,
    JobStateMachine,
    JOB_CANCELLED,
    JOB_PAUSED,
    JOB_PROCESSING,
    JOB_PUBLISHED,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_RETRY,
    validate_checkpoint,
)
from .database import LocalDatabase


AUDIT_JOB_CREATED = "JOB_CREATED"
AUDIT_JOB_STATE_CHANGED = "JOB_STATE_CHANGED"
AUDIT_JOB_PAUSED = "JOB_PAUSED"
AUDIT_JOB_RESUMED = "JOB_RESUMED"
AUDIT_JOB_CANCELLED = "JOB_CANCELLED"
AUDIT_JOB_RETRY = "JOB_RETRY"
AUDIT_JOB_PROCESSING_COMPLETED = "JOB_PROCESSING_COMPLETED"
AUDIT_JOB_UPLOAD_STARTED = "JOB_UPLOAD_STARTED"
AUDIT_JOB_CONFIRMED = "JOB_CONFIRMED"
AUDIT_JOB_RECONCILIATION = "JOB_RECONCILIATION"
AUDIT_JOB_ERROR = "JOB_ERROR"
AUDIT_JOB_RECOVERY = "JOB_RECOVERY"
AUDIT_JOB_CHECKPOINT_REACHED = "JOB_CHECKPOINT_REACHED"

JOB_AUDIT_EVENT_TYPES = frozenset(
    {
        AUDIT_JOB_CREATED,
        AUDIT_JOB_STATE_CHANGED,
        AUDIT_JOB_PAUSED,
        AUDIT_JOB_RESUMED,
        AUDIT_JOB_CANCELLED,
        AUDIT_JOB_RETRY,
        AUDIT_JOB_PROCESSING_COMPLETED,
        AUDIT_JOB_UPLOAD_STARTED,
        AUDIT_JOB_CONFIRMED,
        AUDIT_JOB_RECONCILIATION,
        AUDIT_JOB_ERROR,
        AUDIT_JOB_RECOVERY,
        AUDIT_JOB_CHECKPOINT_REACHED,
    }
)


class OperationalAuditError(RuntimeError):
    """Erro de contrato do audit log operacional."""


class JobNotFoundForAuditError(OperationalAuditError):
    """Job solicitado não existe no storage local."""


class AuditHistoryError(OperationalAuditError):
    """Histórico append-only não permite reconstrução consistente."""


@dataclass(frozen=True)
class JobAuditHistory:
    """Visão reconstruída do histórico persistido de um Job."""

    job_id: str
    initial_state: str
    current_state: str
    state_path: tuple[str, ...]
    events: tuple[dict[str, Any], ...]


class OperationalAuditLog:
    """API operacional append-only para Jobs.

    O serviço não é um Job Engine. Ele apenas oferece primitivas atômicas de
    persistência/auditoria que um Job Engine futuro poderá consumir.
    """

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        value = str(job_id)
        try:
            UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("job_id deve ser UUID válido") from exc
        return value

    @staticmethod
    def _merge_data(base: Mapping[str, Any], extra: Mapping[str, Any] | None) -> dict[str, Any]:
        payload = dict(base)
        if extra:
            for key, value in extra.items():
                if key in payload and payload[key] != value:
                    raise ValueError(f"campo reservado do audit log não pode ser sobrescrito: {key}")
                payload[key] = value
        return payload

    def create_job(
        self,
        job: Job,
        *,
        data: Mapping[str, Any] | None = None,
        connection=None,
    ) -> Job:
        """Insere um Job e seu evento inicial na mesma transaction.

        ``connection`` (PROMPT 17, ``batch_engine.py``): quando informada,
        esta chamada roda inteiramente dentro da transaction já aberta pelo
        chamador (nenhum ``BEGIN``/``COMMIT`` próprio é emitido aqui) — mesmo
        padrão já estabelecido em ``transition_job``. Isso permite compor a
        criação de VÁRIOS Jobs (e sua membership de batch) sob o MESMO
        ``BEGIN IMMEDIATE``: ou o batch inteiro (linha do batch + todos os
        Jobs + toda a membership) é criado corretamente, ou nada é
        persistido — nunca uma membership parcialmente mentirosa. Quando
        omitida (padrão, e todo o comportamento anterior a esta opção), abre
        e comita sua própria transaction, exatamente como sempre fez.
        """
        payload = self._merge_data(
            {
                "initial_state": job.status,
                "operation": job.operation,
                "video_id": job.video_id,
                "project_id": job.project_id,
            },
            data,
        )

        def _run(conn) -> Job:
            self.database.insert(job, connection=conn)
            self.database.append_audit_event(
                AUDIT_JOB_CREATED,
                entity_type="Job",
                entity_id=job.id,
                data=payload,
                connection=conn,
            )
            return job

        if connection is not None:
            return _run(connection)
        with self.database.transaction() as conn:
            return _run(conn)

    def _get_job_locked(self, job_id: str, *, connection) -> Job:
        job_id = self._validate_job_id(job_id)
        job = self.database.get(Job, job_id, connection=connection)
        if job is None:
            raise JobNotFoundForAuditError(f"Job não encontrado: {job_id}")
        return job

    def transition_job(
        self,
        job_id: str,
        target: str,
        *,
        semantic_event: str | None = None,
        data: Mapping[str, Any] | None = None,
        connection=None,
    ) -> Job:
        """Persiste uma transição válida e seu audit trail atomicamente.

        ``connection`` (PROMPT 15, correção de corrida pause-vs-claim):
        quando informada, esta chamada roda inteiramente dentro da
        transaction já aberta pelo chamador (nenhum ``BEGIN``/``COMMIT``
        próprio é emitido aqui) — isso permite compor, sob o mesmo
        ``BEGIN IMMEDIATE`` (mesmo write lock SQLite), uma leitura de
        controle (ex.: ``ControlManager.is_execution_blocked``) com esta
        transição, para que as duas nunca sejam separáveis por um
        commit concorrente de outro escritor (ver ``JobEngine._claim``).
        Quando omitida (padrão, e todo o comportamento anterior a esta
        opção), abre e comita sua própria transaction, exatamente como
        sempre fez.
        """
        JobStateMachine.validate_state(target)

        def _run(conn) -> Job:
            job = self._get_job_locked(job_id, connection=conn)
            source = job.status
            job.transition_to(target)
            self.database.save(job, connection=conn)
            transition_data = self._merge_data(
                {"from_state": source, "to_state": target},
                data,
            )
            self.database.append_audit_event(
                AUDIT_JOB_STATE_CHANGED,
                entity_type="Job",
                entity_id=job.id,
                data=transition_data,
                connection=conn,
            )
            if semantic_event is not None:
                if semantic_event not in JOB_AUDIT_EVENT_TYPES or semantic_event == AUDIT_JOB_STATE_CHANGED:
                    raise ValueError(f"semantic_event inválido: {semantic_event!r}")
                self.database.append_audit_event(
                    semantic_event,
                    entity_type="Job",
                    entity_id=job.id,
                    data=transition_data,
                    connection=conn,
                )
            return job

        if connection is not None:
            return _run(connection)
        with self.database.transaction() as conn:
            return _run(conn)

    def pause(self, job_id: str, *, data: Mapping[str, Any] | None = None) -> Job:
        return self.transition_job(job_id, JOB_PAUSED, semantic_event=AUDIT_JOB_PAUSED, data=data)

    def resume(
        self,
        job_id: str,
        *,
        target: str = JOB_PROCESSING,
        data: Mapping[str, Any] | None = None,
    ) -> Job:
        return self.transition_job(job_id, target, semantic_event=AUDIT_JOB_RESUMED, data=data)

    def cancel(self, job_id: str, *, data: Mapping[str, Any] | None = None) -> Job:
        return self.transition_job(job_id, JOB_CANCELLED, semantic_event=AUDIT_JOB_CANCELLED, data=data)

    def retry(self, job_id: str, *, data: Mapping[str, Any] | None = None) -> Job:
        return self.transition_job(job_id, JOB_RETRY, semantic_event=AUDIT_JOB_RETRY, data=data)

    def processing_completed(self, job_id: str, *, data: Mapping[str, Any] | None = None) -> Job:
        return self.transition_job(
            job_id,
            JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data=data,
        )

    def upload_started(self, job_id: str, *, data: Mapping[str, Any] | None = None) -> Job:
        return self.transition_job(
            job_id,
            JOB_PUBLISHING,
            semantic_event=AUDIT_JOB_UPLOAD_STARTED,
            data=data,
        )

    def confirmed(self, job_id: str, *, data: Mapping[str, Any] | None = None) -> Job:
        return self.transition_job(
            job_id,
            JOB_PUBLISHED,
            semantic_event=AUDIT_JOB_CONFIRMED,
            data=data,
        )

    def reconciliation_started(self, job_id: str, *, data: Mapping[str, Any] | None = None) -> Job:
        return self.transition_job(
            job_id,
            JOB_RECOVERING,
            semantic_event=AUDIT_JOB_RECONCILIATION,
            data=self._merge_data({"phase": "STARTED"}, data),
        )

    def record_reconciliation(
        self,
        job_id: str,
        *,
        result: str,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        return self.record_event(
            job_id,
            AUDIT_JOB_RECONCILIATION,
            data=self._merge_data({"result": str(result)}, data),
        )

    def record_error(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        recoverable: bool | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {"code": str(code), "message": str(message)}
        if recoverable is not None:
            payload["recoverable"] = bool(recoverable)
        return self.record_event(
            job_id,
            AUDIT_JOB_ERROR,
            data=self._merge_data(payload, data),
        )

    def record_recovery(
        self,
        job_id: str,
        *,
        phase: str,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        return self.record_event(
            job_id,
            AUDIT_JOB_RECOVERY,
            data=self._merge_data({"phase": str(phase)}, data),
        )

    def record_event(
        self,
        job_id: str,
        event_type: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        job_id = self._validate_job_id(job_id)
        if event_type not in JOB_AUDIT_EVENT_TYPES:
            raise ValueError(f"event_type operacional inválido: {event_type!r}")
        with self.database.transaction() as conn:
            self._get_job_locked(job_id, connection=conn)
            return self.database.append_audit_event(
                event_type,
                entity_type="Job",
                entity_id=job_id,
                data=data,
                connection=conn,
            )

    def list_job_events(self, job_id: str) -> list[dict[str, Any]]:
        job_id = self._validate_job_id(job_id)
        return self.database.list_audit_events(entity_type="Job", entity_id=job_id)

    def reconstruct_job_history(self, job_id: str) -> JobAuditHistory:
        """Reconstrói a trilha de estados usando somente eventos append-only."""
        job_id = self._validate_job_id(job_id)
        events = self.list_job_events(job_id)
        created = [event for event in events if event["event_type"] == AUDIT_JOB_CREATED]
        if len(created) != 1:
            raise AuditHistoryError(
                f"histórico do Job {job_id} precisa conter exatamente um JOB_CREATED"
            )
        initial = created[0]["data"].get("initial_state")
        try:
            JobStateMachine.validate_state(initial)
        except Exception as exc:
            raise AuditHistoryError(f"estado inicial inválido no audit log: {initial!r}") from exc

        current = initial
        path = [current]
        for event in events:
            if event["event_type"] != AUDIT_JOB_STATE_CHANGED:
                continue
            source = event["data"].get("from_state")
            target = event["data"].get("to_state")
            if source != current:
                raise AuditHistoryError(
                    f"cadeia de estados inconsistente: esperado origem {current}, recebido {source}"
                )
            try:
                JobStateMachine.validate_transition(source, target)
            except Exception as exc:
                raise AuditHistoryError(
                    f"transição inválida persistida no audit log: {source} -> {target}"
                ) from exc
            current = target
            path.append(current)

        return JobAuditHistory(
            job_id=job_id,
            initial_state=initial,
            current_state=current,
            state_path=tuple(path),
            events=tuple(events),
        )

    # -- Checkpoints (FASE 3 / PROMPT 13) --------------------------------
    #
    # Um checkpoint marca uma fronteira segura de progresso *dentro* da
    # execução de um Job (ex.: TRANSCRIBED, RENDERED) e é independente do
    # `status` da State Machine central: gravar um checkpoint nunca altera
    # `Job.status`, e as duas responsabilidades continuam separadas. Reusa
    # a tabela `audit_events` já existente (migrations 001/002): ela já é
    # durável (WAL + transaction explícita), indexada por
    # `(entity_type, entity_id)`, e protegida contra UPDATE/DELETE/reuso de
    # id pelos triggers da migration 002 — exatamente as garantias que um
    # checkpoint "não pode existir só em memória" e "não pode ser apagado
    # por crash/restart" exige. Nenhuma migration nova foi necessária.

    def record_checkpoint(
        self,
        job_id: str,
        checkpoint: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> bool:
        """Registra que um Job atingiu um checkpoint de pipeline.

        Idempotente por ``(job_id, checkpoint)``: se esse checkpoint já
        havia sido registrado para o Job, nenhum evento novo é inserido — o
        chamado apenas devolve ``False``. Isso é deliberado, não apenas uma
        otimização: ``audit_events`` é append-only (UPDATE/DELETE são
        negados pelo próprio SQLite), então a única forma segura de
        "reafirmar" um fato já persistido é não duplicá-lo. Retorna
        ``True`` quando este chamado gravou o evento pela primeira vez.

        A checagem de existência e a inserção acontecem dentro da mesma
        transaction SQLite (``BEGIN IMMEDIATE``, como em
        ``transition_job``). O SQLite serializa escritores: duas chamadas
        concorrentes para o mesmo ``(job_id, checkpoint)`` — de duas
        instâncias/processos diferentes contra o mesmo arquivo — nunca
        produzem duas linhas para o mesmo checkpoint; a segunda enxerga o
        efeito já commitado da primeira antes de decidir se insere.

        Não decide nada sobre reconciliação, retry ou recuperação: apenas
        persiste o fato observado pelo chamador. Em particular, a ausência
        de um checkpoint posterior (ex.: ``REMOTE_CONFIRMED`` nunca chegou
        depois de ``UPLOAD_STARTED``) não é interpretada aqui como falha
        remota — essa decisão pertence ao ``RecoveryManager`` (PROMPT 14,
        ``recovery_manager.py``) e ao caminho já existente
        ``UNKNOWN -> RECOVERING`` da State Machine central, nunca a uma
        inferência automática feita a partir de checkpoints. O
        RecoveryManager, mesmo já implementado, também não lê checkpoints
        para decidir esse resultado — apenas garante que a incerteza fique
        marcada de forma auditável até que um Connector futuro reconcilie o
        efeito remoto real.
        """
        job_id = self._validate_job_id(job_id)
        checkpoint = validate_checkpoint(checkpoint)
        payload = self._merge_data({"checkpoint": checkpoint}, data)

        with self.database.transaction() as conn:
            self._get_job_locked(job_id, connection=conn)
            existing = self.database.list_audit_events(
                entity_type="Job",
                entity_id=job_id,
                event_type=AUDIT_JOB_CHECKPOINT_REACHED,
                connection=conn,
            )
            if any(event["data"].get("checkpoint") == checkpoint for event in existing):
                return False
            self.database.append_audit_event(
                AUDIT_JOB_CHECKPOINT_REACHED,
                entity_type="Job",
                entity_id=job_id,
                data=payload,
                connection=conn,
            )
        return True

    def list_checkpoints(self, job_id: str) -> list[dict[str, Any]]:
        """Lista os eventos de checkpoint de um Job, na ordem em que foram gravados.

        Não exige que o Job exista (mesmo comportamento de
        ``list_job_events``): um ``job_id`` válido sem nenhum checkpoint
        gravado simplesmente devolve uma lista vazia.
        """
        job_id = self._validate_job_id(job_id)
        return self.database.list_audit_events(
            entity_type="Job", entity_id=job_id, event_type=AUDIT_JOB_CHECKPOINT_REACHED
        )

    def has_reached_checkpoint(self, job_id: str, checkpoint: str) -> bool:
        """Indica se este Job já teve ``checkpoint`` persistido com sucesso."""
        checkpoint = validate_checkpoint(checkpoint)
        return any(
            event["data"].get("checkpoint") == checkpoint
            for event in self.list_checkpoints(job_id)
        )

    def latest_checkpoint(self, job_id: str) -> str | None:
        """Devolve o checkpoint mais recente gravado para o Job, ou ``None``.

        A ordem segue a sequência real de gravação no SQLite, não uma ordem
        canônica presumida entre nomes de checkpoint: este módulo não impõe
        uma sequência fixa entre checkpoints (ver
        ``_sistema.domain.checkpoints``).
        """
        events = self.list_checkpoints(job_id)
        if not events:
            return None
        return events[-1]["data"].get("checkpoint")


__all__ = [
    "AUDIT_JOB_CREATED",
    "AUDIT_JOB_STATE_CHANGED",
    "AUDIT_JOB_PAUSED",
    "AUDIT_JOB_RESUMED",
    "AUDIT_JOB_CANCELLED",
    "AUDIT_JOB_RETRY",
    "AUDIT_JOB_PROCESSING_COMPLETED",
    "AUDIT_JOB_UPLOAD_STARTED",
    "AUDIT_JOB_CONFIRMED",
    "AUDIT_JOB_RECONCILIATION",
    "AUDIT_JOB_ERROR",
    "AUDIT_JOB_RECOVERY",
    "AUDIT_JOB_CHECKPOINT_REACHED",
    "JOB_AUDIT_EVENT_TYPES",
    "OperationalAuditError",
    "JobNotFoundForAuditError",
    "AuditHistoryError",
    "JobAuditHistory",
    "OperationalAuditLog",
]
