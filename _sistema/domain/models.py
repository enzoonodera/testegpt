"""Modelos de domínio independentes da UI, storage e plataformas.

Esta camada contém somente estruturas de dados e regras mínimas de identidade.
Ela não conhece YouTube, TikTok, Playwright, SQLite ou componentes de interface.

Relações entre entidades são representadas por UUIDs persistentes. Isso evita
acoplamento entre objetos em memória e permite que a persistência futura seja
implementada sem mudar o contrato dos modelos.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, ClassVar, Mapping, TypeVar
from uuid import UUID, uuid4

from .job_state_machine import (
    JOB_PENDING,
    JobStateMachine,
)
from ..time_utils import (
    SCHEDULE_TIME_MANUAL,
    SCHEDULE_TIME_RECOMMENDED,
    MissingTimezoneConfigurationError,
    canonical_schedule_values,
    utc_now_iso,
    validate_schedule_time_origin,
    validate_timezone_name,
)


SCHEMA_VERSION = 1
SCHEDULE_LOCAL_PENDING = "LOCAL_PENDING"
SCHEDULE_REMOTE_SCHEDULED = "REMOTE_SCHEDULED"
SCHEDULE_UNKNOWN = "UNKNOWN"

T = TypeVar("T", bound="Entity")


def new_uuid() -> str:
    """Cria a identidade persistível de uma nova entidade."""
    return str(uuid4())


def _validate_uuid(value: str, field_name: str = "id") -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} deve ser um UUID textual não vazio")
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field_name} deve conter um UUID válido") from exc


def _validate_optional_uuid(value: str | None, field_name: str) -> None:
    if value is not None:
        _validate_uuid(value, field_name)


@dataclass(slots=True)
class Entity:
    """Base serializável para entidades persistentes do domínio.

    ``extra`` preserva campos desconhecidos lidos de versões futuras/anteriores.
    Eles voltam ao nível superior em ``to_dict`` para reduzir perda de dados em
    round-trips durante evolução de schema. ``DROPPED_FIELDS`` permite que cada
    modelo descarte explicitamente campos descontinuados que não podem voltar a
    ser serializados.
    """

    id: str = field(default_factory=new_uuid)
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    ENTITY_TYPE: ClassVar[str] = "Entity"
    DROPPED_FIELDS: ClassVar[frozenset[str]] = frozenset()

    def __post_init__(self) -> None:
        _validate_uuid(self.id)
        if self.schema_version < 1:
            raise ValueError("schema_version deve ser >= 1")

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        data = {
            key: deepcopy(value)
            for key, value in self.extra.items()
            if key not in self.DROPPED_FIELDS
        }
        for item in fields(self):
            if item.name == "extra":
                continue
            data[item.name] = deepcopy(getattr(self, item.name))
        data["model_type"] = self.ENTITY_TYPE
        return data

    @classmethod
    def from_dict(cls: type[T], payload: Mapping[str, Any]) -> T:
        if not isinstance(payload, Mapping):
            raise TypeError("payload deve ser um mapping")
        if "id" not in payload:
            raise ValueError("payload persistido deve conter id")
        model_type = payload.get("model_type")
        if model_type is not None and model_type != cls.ENTITY_TYPE:
            raise ValueError(
                f"model_type incompatível: esperado {cls.ENTITY_TYPE}, recebido {model_type}"
            )

        known_names = {item.name for item in fields(cls) if item.name != "extra"}
        kwargs: dict[str, Any] = {}
        unknown: dict[str, Any] = {}

        embedded_extra = payload.get("extra")
        if isinstance(embedded_extra, Mapping):
            for key, value in embedded_extra.items():
                if key not in cls.DROPPED_FIELDS:
                    unknown[key] = deepcopy(value)

        for key, value in payload.items():
            if key == "model_type" or key in cls.DROPPED_FIELDS:
                continue
            if key in known_names:
                kwargs[key] = deepcopy(value)
            elif key != "extra":
                unknown[key] = deepcopy(value)

        kwargs["extra"] = unknown
        return cls(**kwargs)


@dataclass(slots=True)
class SourceAsset(Entity):
    """Origem importada/descoberta, separada do vídeo lógico e de edições."""

    ENTITY_TYPE: ClassVar[str] = "SourceAsset"

    source_uri: str = ""
    local_path: str | None = None
    original_name: str | None = None
    fingerprint: str | None = None
    media_type: str = "video"
    size_bytes: int | None = None


@dataclass(slots=True)
class Video(Entity):
    """Vídeo lógico. Uma origem pode gerar projetos, jobs e publicações."""

    ENTITY_TYPE: ClassVar[str] = "Video"

    source_asset_id: str | None = None
    name: str = ""
    status: str = "ACTIVE"

    def __post_init__(self) -> None:
        Entity.__post_init__(self)
        _validate_optional_uuid(self.source_asset_id, "source_asset_id")


@dataclass(slots=True)
class Project(Entity):
    """Estado não destrutivo de edição associado a um vídeo."""

    ENTITY_TYPE: ClassVar[str] = "Project"

    video_id: str | None = None
    name: str = ""
    revision: int = 1
    edit_state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Entity.__post_init__(self)
        _validate_optional_uuid(self.video_id, "video_id")
        if self.revision < 1:
            raise ValueError("revision deve ser >= 1")


@dataclass(slots=True)
class Account(Entity):
    """Conta lógica. ``platform`` é dado, não dependência de plataforma."""

    ENTITY_TYPE: ClassVar[str] = "Account"

    platform: str = ""
    name: str = ""
    external_account_id: str | None = None
    local_key: str | None = None
    enabled: bool = True


@dataclass(slots=True)
class Job(Entity):
    """Unidade operacional de trabalho com ciclo de vida central validado.

    ``Publication`` continua sendo a entidade do efeito remoto/publicação. O
    ``Job`` representa a execução operacional e pode coordenar processamento,
    preparação, agendamento ou publicação sem incorporar dados específicos de
    plataforma. Mudanças de ``status`` devem passar por ``transition_to``.
    """

    ENTITY_TYPE: ClassVar[str] = "Job"

    video_id: str | None = None
    project_id: str | None = None
    operation: str = ""
    status: str = JOB_PENDING
    progress: float = 0.0
    input_artifact_ids: list[str] = field(default_factory=list)
    output_artifact_ids: list[str] = field(default_factory=list)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "status":
            JobStateMachine.validate_state(value)
            try:
                current = object.__getattribute__(self, "status")
            except AttributeError:
                current = None
            if current is not None and current != value:
                raise ValueError(
                    "status de Job não pode ser alterado diretamente; use transition_to()"
                )
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        Entity.__post_init__(self)
        JobStateMachine.validate_state(self.status)
        _validate_optional_uuid(self.video_id, "video_id")
        _validate_optional_uuid(self.project_id, "project_id")
        for index, value in enumerate(self.input_artifact_ids):
            _validate_uuid(value, f"input_artifact_ids[{index}]")
        for index, value in enumerate(self.output_artifact_ids):
            _validate_uuid(value, f"output_artifact_ids[{index}]")
        if not 0.0 <= self.progress <= 1.0:
            raise ValueError("progress deve estar entre 0.0 e 1.0")

    def transition_to(self, target: str) -> None:
        """Aplica uma transição válida e atualiza o timestamp da entidade."""
        JobStateMachine.validate_transition(self.status, target)
        object.__setattr__(self, "status", target)
        self.touch()

    def allowed_transitions(self) -> frozenset[str]:
        """Retorna os próximos estados permitidos para o estado atual."""
        return JobStateMachine.allowed_targets(self.status)


@dataclass(slots=True)
class Publication(Entity):
    """Tentativa/ciclo de publicação remoto, separado de Job de processamento."""

    ENTITY_TYPE: ClassVar[str] = "Publication"
    DROPPED_FIELDS: ClassVar[frozenset[str]] = frozenset({"schedule_id"})

    video_id: str | None = None
    account_id: str | None = None
    artifact_id: str | None = None
    status: str = "PENDING"
    remote_id: str | None = None
    title: str | None = None
    description: str | None = None
    # PROMPT 22 (Idempotência): identifica de forma estável qual tentativa
    # de publicação remota esta ``Publication`` representa. Coluna
    # dedicada + índice único PARCIAL (``m006_publication_idempotency.py``
    # -- permite múltiplos ``None``, mas no máximo uma ``Publication`` não
    # nula por chave) -- a proteção real contra duplicação é garantida
    # pelo banco, nunca só por uma checagem em Python. A fórmula da chave
    # NUNCA é decidida aqui: é sempre fornecida explicitamente por quem
    # chama (ver ``_sistema/publication_idempotency.py``,
    # ``compute_idempotency_key`` para o formulário default sugerido). Uma
    # ``Publication`` sem chave (``None``, ex.: dados legados importados
    # por ``legacy_migration.py``) nunca participa da checagem de
    # idempotência.
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        Entity.__post_init__(self)
        _validate_optional_uuid(self.video_id, "video_id")
        _validate_optional_uuid(self.account_id, "account_id")
        _validate_optional_uuid(self.artifact_id, "artifact_id")


@dataclass(slots=True)
class Schedule(Entity):
    """Agendamento associado a uma Publication com tempo canônico explícito.

    ``publication_id`` é a relação canônica com ``Publication``. A FK futura
    fica neste modelo para evitar uma relação circular com duas fontes de verdade.

    O horário preserva quatro dimensões independentes:
    - ``scheduled_local``: horário de parede pretendido, sem offset;
    - ``timezone_iana``: timezone IANA explícito desse horário;
    - ``scheduled_utc``: instante UTC correspondente;
    - ``time_origin``: MANUAL ou RECOMMENDED.

    ``delivery_state`` distingue tarefa ainda dependente do PC
    (LOCAL_PENDING), agendamento confirmado pela plataforma
    (REMOTE_SCHEDULED) e resultado remoto ainda incerto (UNKNOWN).
    """

    ENTITY_TYPE: ClassVar[str] = "Schedule"
    # Contrato antigo do modelo V7/V8. São aliases apenas de leitura no
    # ``from_dict`` abaixo e nunca voltam a ser serializados.
    DROPPED_FIELDS: ClassVar[frozenset[str]] = frozenset({"scheduled_for", "timezone_name"})

    publication_id: str | None = None
    scheduled_local: str | None = None
    timezone_iana: str | None = None
    scheduled_utc: str | None = None
    time_origin: str = SCHEDULE_TIME_MANUAL
    delivery_state: str = SCHEDULE_LOCAL_PENDING

    def __post_init__(self) -> None:
        Entity.__post_init__(self)
        _validate_optional_uuid(self.publication_id, "publication_id")
        self.time_origin = validate_schedule_time_origin(self.time_origin)
        has_scheduled_time = self.scheduled_local is not None or self.scheduled_utc is not None
        if has_scheduled_time and not self.timezone_iana:
            raise MissingTimezoneConfigurationError(
                "timezone_iana explícito é obrigatório quando Schedule possui horário"
            )
        if self.timezone_iana is not None:
            self.timezone_iana = validate_timezone_name(self.timezone_iana)
        if has_scheduled_time:
            (
                self.scheduled_local,
                self.timezone_iana,
                self.scheduled_utc,
            ) = canonical_schedule_values(
                scheduled_local=self.scheduled_local,
                timezone_iana=self.timezone_iana,
                scheduled_utc=self.scheduled_utc,
            )
        allowed_states = {
            SCHEDULE_LOCAL_PENDING,
            SCHEDULE_REMOTE_SCHEDULED,
            SCHEDULE_UNKNOWN,
        }
        if self.delivery_state not in allowed_states:
            raise ValueError(
                "delivery_state deve ser "
                f"{SCHEDULE_LOCAL_PENDING}, {SCHEDULE_REMOTE_SCHEDULED} ou {SCHEDULE_UNKNOWN}"
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Schedule":
        """Aceita aliases antigos sem reemitir campos ambíguos.

        ``scheduled_for`` era o instante do contrato anterior e
        ``timezone_name`` o timezone. Eles são convertidos para os campos
        canônicos antes do round-trip.
        """
        if not isinstance(payload, Mapping):
            raise TypeError("payload deve ser um mapping")
        normalized = dict(payload)
        if not normalized.get("timezone_iana") and normalized.get("timezone_name"):
            normalized["timezone_iana"] = normalized.get("timezone_name")
        legacy_scheduled_for = normalized.get("scheduled_for")
        if not normalized.get("scheduled_utc") and not normalized.get("scheduled_local") and legacy_scheduled_for:
            # O contrato antigo não exigia offset. Valor aware é instante;
            # valor naive é horário local no timezone legado da entidade.
            legacy_text = str(legacy_scheduled_for)
            try:
                legacy_dt = datetime.fromisoformat(legacy_text.replace("Z", "+00:00"))
            except ValueError:
                legacy_dt = None
            if legacy_dt is not None and legacy_dt.tzinfo is not None and legacy_dt.utcoffset() is not None:
                normalized["scheduled_utc"] = legacy_text
            else:
                normalized["scheduled_local"] = legacy_text
        normalized.pop("scheduled_for", None)
        normalized.pop("timezone_name", None)
        return Entity.from_dict.__func__(cls, normalized)


@dataclass(slots=True)
class Template(Entity):
    """Definição independente do editor e da plataforma."""

    ENTITY_TYPE: ClassVar[str] = "Template"

    name: str = ""
    template_type: str = "CUSTOM"
    source_path: str | None = None
    layout: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass(slots=True)
class Artifact(Entity):
    """Arquivo/resultado derivado, nunca confundido com SourceAsset original."""

    ENTITY_TYPE: ClassVar[str] = "Artifact"

    video_id: str | None = None
    project_id: str | None = None
    job_id: str | None = None
    kind: str = ""
    path: str = ""
    fingerprint: str | None = None
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        Entity.__post_init__(self)
        _validate_optional_uuid(self.video_id, "video_id")
        _validate_optional_uuid(self.project_id, "project_id")
        _validate_optional_uuid(self.job_id, "job_id")


@dataclass(slots=True)
class ErrorRecord(Entity):
    """Registro de erro associado opcionalmente a uma entidade/job."""

    ENTITY_TYPE: ClassVar[str] = "ErrorRecord"

    code: str = ""
    message: str = ""
    entity_type: str | None = None
    entity_id: str | None = None
    job_id: str | None = None
    recoverable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Entity.__post_init__(self)
        _validate_optional_uuid(self.entity_id, "entity_id")
        _validate_optional_uuid(self.job_id, "job_id")
