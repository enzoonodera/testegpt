"""State Machine central e independente para o ciclo de vida de Job.

A máquina define o vocabulário canônico de estados e as transições permitidas.
Ela não conhece UI, engines, plataformas, SQLite ou persistência legada.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import ClassVar, Mapping


JOB_PENDING = "PENDING"
JOB_PROCESSING = "PROCESSING"
JOB_READY = "READY"
JOB_PAUSED = "PAUSED"
JOB_INTERRUPTED = "INTERRUPTED"
JOB_RECOVERING = "RECOVERING"
JOB_SCHEDULED = "SCHEDULED"
JOB_PUBLISHING = "PUBLISHING"
JOB_PUBLISHED = "PUBLISHED"
JOB_RETRY = "RETRY"
JOB_FAILED = "FAILED"
JOB_BLOCKED = "BLOCKED"
JOB_UNKNOWN = "UNKNOWN"
JOB_AUTH_REQUIRED = "AUTH_REQUIRED"
JOB_USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
JOB_CANCELLED = "CANCELLED"

JOB_STATES = frozenset(
    {
        JOB_PENDING,
        JOB_PROCESSING,
        JOB_READY,
        JOB_PAUSED,
        JOB_INTERRUPTED,
        JOB_RECOVERING,
        JOB_SCHEDULED,
        JOB_PUBLISHING,
        JOB_PUBLISHED,
        JOB_RETRY,
        JOB_FAILED,
        JOB_BLOCKED,
        JOB_UNKNOWN,
        JOB_AUTH_REQUIRED,
        JOB_USER_ACTION_REQUIRED,
        JOB_CANCELLED,
    }
)

# Transições explícitas. Ausência de aresta significa transição proibida.
# UNKNOWN é deliberadamente restrito a RECOVERING: antes de retry/publicação,
# o resultado incerto precisa passar por reconciliação.
_TRANSITIONS = {
    JOB_PENDING: frozenset(
        {
            JOB_PROCESSING,
            JOB_SCHEDULED,
            JOB_PUBLISHING,
            JOB_PAUSED,
            JOB_BLOCKED,
            JOB_AUTH_REQUIRED,
            JOB_USER_ACTION_REQUIRED,
            JOB_CANCELLED,
        }
    ),
    JOB_PROCESSING: frozenset(
        {
            JOB_READY,
            JOB_PAUSED,
            JOB_INTERRUPTED,
            JOB_RETRY,
            JOB_FAILED,
            JOB_BLOCKED,
            JOB_AUTH_REQUIRED,
            JOB_USER_ACTION_REQUIRED,
            JOB_CANCELLED,
        }
    ),
    JOB_READY: frozenset(
        {
            JOB_SCHEDULED,
            JOB_PUBLISHING,
            JOB_CANCELLED,
        }
    ),
    JOB_PAUSED: frozenset(
        {
            JOB_PENDING,
            JOB_PROCESSING,
            JOB_PUBLISHING,
            JOB_CANCELLED,
        }
    ),
    JOB_INTERRUPTED: frozenset({JOB_RECOVERING, JOB_CANCELLED}),
    JOB_RECOVERING: frozenset(
        {
            JOB_PROCESSING,
            JOB_READY,
            JOB_SCHEDULED,
            JOB_PUBLISHED,
            JOB_RETRY,
            JOB_FAILED,
            JOB_BLOCKED,
            JOB_AUTH_REQUIRED,
            JOB_USER_ACTION_REQUIRED,
            JOB_UNKNOWN,
            JOB_CANCELLED,
        }
    ),
    JOB_SCHEDULED: frozenset(
        {
            JOB_PUBLISHING,
            JOB_PUBLISHED,
            JOB_UNKNOWN,
            JOB_FAILED,
            JOB_BLOCKED,
            JOB_AUTH_REQUIRED,
            JOB_USER_ACTION_REQUIRED,
            JOB_CANCELLED,
        }
    ),
    JOB_PUBLISHING: frozenset(
        {
            JOB_PUBLISHED,
            JOB_UNKNOWN,
            JOB_FAILED,
            JOB_BLOCKED,
            JOB_AUTH_REQUIRED,
            JOB_USER_ACTION_REQUIRED,
        }
    ),
    JOB_PUBLISHED: frozenset(),
    JOB_RETRY: frozenset(
        {
            JOB_PROCESSING,
            JOB_PUBLISHING,
            JOB_RECOVERING,
            JOB_CANCELLED,
        }
    ),
    JOB_FAILED: frozenset({JOB_RETRY, JOB_CANCELLED}),
    JOB_BLOCKED: frozenset({JOB_RETRY, JOB_CANCELLED}),
    JOB_UNKNOWN: frozenset({JOB_RECOVERING}),
    JOB_AUTH_REQUIRED: frozenset({JOB_RETRY, JOB_RECOVERING, JOB_CANCELLED}),
    JOB_USER_ACTION_REQUIRED: frozenset({JOB_RETRY, JOB_RECOVERING, JOB_CANCELLED}),
    JOB_CANCELLED: frozenset(),
}

JOB_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(_TRANSITIONS)
JOB_TERMINAL_STATES = frozenset({JOB_PUBLISHED, JOB_CANCELLED})


class InvalidJobState(ValueError):
    """Estado de Job fora do vocabulário canônico."""


class InvalidJobTransition(ValueError):
    """Transição de Job não permitida pela State Machine."""


class JobStateMachine:
    """Validação central do ciclo de vida de ``Job``."""

    states: ClassVar[frozenset[str]] = JOB_STATES
    transitions: ClassVar[Mapping[str, frozenset[str]]] = JOB_TRANSITIONS
    terminal_states: ClassVar[frozenset[str]] = JOB_TERMINAL_STATES

    @classmethod
    def validate_state(cls, state: str) -> str:
        if state not in cls.states:
            raise InvalidJobState(f"estado de Job inválido: {state!r}")
        return state

    @classmethod
    def allowed_targets(cls, state: str) -> frozenset[str]:
        cls.validate_state(state)
        return cls.transitions[state]

    @classmethod
    def can_transition(cls, source: str, target: str) -> bool:
        cls.validate_state(source)
        cls.validate_state(target)
        return target in cls.transitions[source]

    @classmethod
    def validate_transition(cls, source: str, target: str) -> None:
        cls.validate_state(source)
        cls.validate_state(target)
        if target not in cls.transitions[source]:
            raise InvalidJobTransition(
                f"transição de Job não permitida: {source} -> {target}"
            )

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        cls.validate_state(state)
        return state in cls.terminal_states
