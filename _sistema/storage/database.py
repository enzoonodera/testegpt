# -*- coding: utf-8 -*-
"""SQLite operacional exclusivamente local.

Esta camada é deliberadamente independente dos engines legados. Ela fornece:
- schema versionado com migrations numeradas e checksum;
- foreign keys por conexão;
- WAL para banco em arquivo;
- transações explícitas;
- CRUD dos modelos de domínio;
- settings e audit_events locais.

Nenhum dado é enviado para rede por este módulo.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator, Mapping, TypeVar
from uuid import UUID, uuid4

from ..app_paths import AppPaths, PATHS, ensure_app_directories
from ..domain import (
    Account,
    Artifact,
    Entity,
    ErrorRecord,
    Job,
    Project,
    Publication,
    Schedule,
    SourceAsset,
    Template,
    Video,
)
from ..time_utils import utc_now_iso
from .migrations import LATEST_SCHEMA_VERSION, MIGRATIONS


DEFAULT_DATABASE_FILENAME = "painel.db"
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class DatabaseError(RuntimeError):
    """Erro de contrato/configuração da camada SQLite."""


class MigrationIntegrityError(DatabaseError):
    """Migration aplicada diverge do código imutável esperado."""


class UnsupportedSchemaVersionError(DatabaseError):
    """Banco foi criado por schema mais novo que o binário atual entende."""


@dataclass(frozen=True)
class EntitySpec:
    model: type[Entity]
    table: str
    json_fields: frozenset[str] = frozenset()
    bool_fields: frozenset[str] = frozenset()

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in fields(self.model) if item.name != "extra")


ENTITY_SPECS: tuple[EntitySpec, ...] = (
    EntitySpec(SourceAsset, "sources"),
    EntitySpec(Video, "videos"),
    EntitySpec(Project, "projects", frozenset({"edit_state"})),
    EntitySpec(Account, "accounts", bool_fields=frozenset({"enabled"})),
    EntitySpec(
        Job,
        "jobs",
        frozenset({"input_artifact_ids", "output_artifact_ids"}),
    ),
    EntitySpec(Publication, "publications"),
    EntitySpec(Schedule, "schedules"),
    EntitySpec(Template, "templates", frozenset({"layout"}), frozenset({"enabled"})),
    EntitySpec(Artifact, "artifacts"),
    EntitySpec(
        ErrorRecord,
        "errors",
        frozenset({"details"}),
        frozenset({"recoverable"}),
    ),
)

_SPEC_BY_MODEL: dict[type[Entity], EntitySpec] = {spec.model: spec for spec in ENTITY_SPECS}

TEntity = TypeVar("TEntity", bound=Entity)


def default_database_path(paths: AppPaths | None = None) -> Path:
    paths = paths or PATHS
    return paths.database / DEFAULT_DATABASE_FILENAME



def _split_sql_statements(sql: str) -> tuple[str, ...]:
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate.rstrip().rstrip(";").strip())
            buffer.clear()
    remainder = "\n".join(buffer).strip()
    if remainder:
        if not sqlite3.complete_statement(remainder + ";"):
            raise DatabaseError("migration contém SQL incompleto")
        statements.append(remainder.rstrip().rstrip(";").strip())
    return tuple(statement for statement in statements if statement)

def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | bytes | None, *, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _spec_for_model(model: type[TEntity] | TEntity) -> EntitySpec:
    cls = model if isinstance(model, type) else type(model)
    try:
        return _SPEC_BY_MODEL[cls]
    except KeyError as exc:
        raise TypeError(f"modelo não suportado pelo SQLite local: {cls.__name__}") from exc


def _column_name(field_name: str, spec: EntitySpec) -> str:
    if field_name in spec.json_fields:
        return f"{field_name}_json"
    return field_name


def _encode_entity(entity: Entity, spec: EntitySpec) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field_name in spec.field_names:
        value = getattr(entity, field_name)
        column = _column_name(field_name, spec)
        if field_name in spec.json_fields:
            value = _json_dumps(value)
        elif field_name in spec.bool_fields:
            value = int(bool(value))
        values[column] = value
    values["extra_json"] = _json_dumps(entity.extra)
    return values


def _decode_entity(row: sqlite3.Row, spec: EntitySpec) -> Entity:
    payload: dict[str, Any] = {"model_type": spec.model.ENTITY_TYPE}
    keys = set(row.keys())
    for field_name in spec.field_names:
        column = _column_name(field_name, spec)
        value = row[column]
        if field_name in spec.json_fields:
            value = _json_loads(value, default=[] if field_name.endswith("_ids") else {})
        elif field_name in spec.bool_fields:
            value = bool(value)
        payload[field_name] = value
    if "extra_json" in keys:
        payload["extra"] = _json_loads(row["extra_json"], default={})
    return spec.model.from_dict(payload)


def _local_database_authorizer(action_code: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None) -> int:
    """Impede mutação destrutiva de ``audit_events`` nas conexões do produto.

    O audit log é append-only: INSERT continua permitido, mas UPDATE/DELETE
    diretos são negados pelo próprio SQLite antes de executar a instrução.
    """
    if action_code in {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and arg1 == "audit_events":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class LocalDatabase:
    """Facade transacional do banco operacional local."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        paths: AppPaths | None = None,
    ) -> None:
        self.paths = paths or PATHS
        self.path = Path(path).expanduser().resolve() if path is not None else default_database_path(self.paths)
        self.busy_timeout_ms = int(busy_timeout_ms)
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms deve ser >= 0")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=max(self.busy_timeout_ms / 1000.0, 0.001),
            isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.set_authorizer(_local_database_authorizer)
            return conn
        except Exception:
            conn.close()
            raise

    def initialize(self) -> int:
        """Cria/atualiza o schema local de forma concorrente e idempotente.

        A decisão sobre migrations pendentes é tomada somente *depois* de
        ``BEGIN IMMEDIATE`` adquirir o write lock do SQLite. Assim, duas
        instâncias inicializando o mesmo banco novo nunca trabalham com uma
        versão stale observada antes do lock.
        """
        ensure_app_directories(self.paths)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_migration_registry()

        # CRÍTICO: recovery de restore precisa acontecer antes de qualquer
        # sqlite3.connect() capaz de criar um painel.db vazio. Um journal ou
        # artefato de restore interrompido nunca pode ser confundido com
        # primeira execução. Import local evita ciclo no carregamento do módulo.
        from .backup import BackupManager

        BackupManager(
            paths=self.paths,
            database_path=self.path,
            busy_timeout_ms=self.busy_timeout_ms,
        ).recover_pending_restore()

        conn = self._connect()
        try:
            # journal_mode não pode ser alterado dentro de transaction. Em arquivo,
            # WAL permite leitores enquanto um writer realiza commit. O busy_timeout
            # da conexão também vale para a serialização dessa operação.
            declared_before_lock = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if declared_before_lock > LATEST_SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"schema local v{declared_before_lock} é mais novo que este binário "
                    f"(v{LATEST_SCHEMA_VERSION})"
                )

            self._ensure_wal_mode(conn)

            # Em upgrades de schema já existentes, cria snapshot antes de qualquer
            # DDL. Esta leitura pode ficar stale entre instâncias, mas isso só pode
            # gerar um backup redundante: a decisão de migration continua sendo
            # refeita exclusivamente sob BEGIN IMMEDIATE logo abaixo.
            if 0 < declared_before_lock < LATEST_SCHEMA_VERSION:
                from .backup import BackupManager

                BackupManager(
                    paths=self.paths,
                    database_path=self.path,
                    busy_timeout_ms=self.busy_timeout_ms,
                ).backup_before_migration(
                    from_schema=declared_before_lock,
                    to_schema=LATEST_SCHEMA_VERSION,
                )

            try:
                # Exclusão SQLite vem ANTES da leitura que decide o que aplicar.
                conn.execute("BEGIN IMMEDIATE")
                declared_locked = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if declared_locked > LATEST_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"schema local v{declared_locked} é mais novo que este binário "
                        f"(v{LATEST_SCHEMA_VERSION})"
                    )

                self._ensure_migration_table(conn)
                current = self._validate_applied_migrations(conn)

                # current foi obtido sob o mesmo write lock mantido até o COMMIT.
                # Uma segunda instância que chegar aqui depois apenas verá o prefixo
                # já aplicado e não repetirá DDL.
                for migration in MIGRATIONS[current:]:
                    self._apply_migration_locked(conn, migration.version, migration.name, migration.sql, migration.checksum)
                    current = migration.version

                # Revalida o histórico inteiro ainda sob lock antes de publicar.
                verified = self._validate_applied_migrations(conn)
                if verified != current:
                    raise MigrationIntegrityError(
                        f"histórico de migrations inconsistente após aplicação: esperado v{current}, obtido v{verified}"
                    )
                conn.execute("COMMIT")
                return current
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _ensure_wal_mode(self, conn: sqlite3.Connection) -> None:
        """Habilita WAL de forma tolerante à corrida entre inicializadores.

        ``PRAGMA journal_mode=WAL`` pode retornar ``database is locked`` em um
        banco recém-criado quando outra instância está negociando o mesmo modo.
        Esse lock acontece antes da transaction de migrations, então fazemos um
        retry curto e limitado pelo mesmo busy timeout da conexão.
        """
        deadline = time.monotonic() + max(self.busy_timeout_ms / 1000.0, 0.1)
        while True:
            try:
                current = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if current == "wal":
                    return
                mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
                if mode == "wal":
                    return
                raise DatabaseError(f"não foi possível habilitar WAL: journal_mode={mode}")
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if not ("locked" in message or "busy" in message):
                    raise
                if time.monotonic() >= deadline:
                    raise DatabaseError(
                        "timeout aguardando exclusão para habilitar WAL"
                    ) from exc
                time.sleep(0.01)

    @staticmethod
    def _validate_migration_registry() -> None:
        versions = [migration.version for migration in MIGRATIONS]
        expected = list(range(1, len(MIGRATIONS) + 1))
        if versions != expected:
            raise MigrationIntegrityError(
                f"registro de migrations conhecido não é contínuo: {versions!r}; esperado {expected!r}"
            )

    @staticmethod
    def _ensure_migration_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _current_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM migrations").fetchone()
        return int(row["version"])

    @staticmethod
    def _validate_applied_migrations(conn: sqlite3.Connection) -> int:
        """Valida nome/checksum e exige prefixo contínuo das migrations conhecidas."""
        rows = conn.execute(
            "SELECT version, name, checksum FROM migrations ORDER BY version"
        ).fetchall()
        applied_versions = [int(row["version"]) for row in rows]

        if applied_versions and applied_versions[-1] > LATEST_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"schema local v{applied_versions[-1]} é mais novo que este binário "
                f"(v{LATEST_SCHEMA_VERSION})"
            )

        expected_prefix = [migration.version for migration in MIGRATIONS[: len(rows)]]
        if applied_versions != expected_prefix:
            raise MigrationIntegrityError(
                f"histórico de migrations não forma prefixo contínuo: "
                f"aplicadas={applied_versions!r}, esperado={expected_prefix!r}"
            )

        for row, migration in zip(rows, MIGRATIONS):
            version = int(row["version"])
            if row["name"] != migration.name or row["checksum"] != migration.checksum:
                raise MigrationIntegrityError(
                    f"migration {version:03d} aplicada diverge do código esperado"
                )

        current = applied_versions[-1] if applied_versions else 0
        declared = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if declared > LATEST_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"schema local v{declared} é mais novo que este binário (v{LATEST_SCHEMA_VERSION})"
            )
        if declared != current:
            raise MigrationIntegrityError(
                f"PRAGMA user_version={declared} diverge do histórico de migrations v{current}"
            )
        return current

    @staticmethod
    def _validate_known_migrations(conn: sqlite3.Connection) -> None:
        """Compatibilidade interna: valida o mesmo contrato do histórico completo."""
        LocalDatabase._validate_migration_registry()
        LocalDatabase._validate_applied_migrations(conn)

    @staticmethod
    def _apply_migration_locked(
        conn: sqlite3.Connection,
        version: int,
        name: str,
        sql: str,
        checksum: str,
    ) -> None:
        """Aplica uma migration usando a transaction/lock já adquiridos por initialize()."""
        if not conn.in_transaction:
            raise DatabaseError("migration precisa ser aplicada dentro de transaction exclusiva")
        # sqlite3.executescript faz controle próprio de transaction. O splitter
        # baseado em complete_statement preserva a transaction externa e continua
        # seguro se migrations futuras tiverem strings/triggers com ';'.
        for statement in _split_sql_statements(sql):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (version, name, checksum, utc_now_iso()),
        )
        conn.execute(f"PRAGMA user_version = {version}")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Abre conexão configurada para leitura/diagnóstico."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Transação write explícita com rollback automático em erro."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.connection() as conn:
            self._ensure_migration_table(conn)
            return self._current_version(conn)

    def journal_mode(self) -> str:
        with self.connection() as conn:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def foreign_keys_enabled(self) -> bool:
        with self.connection() as conn:
            return bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])

    def table_names(self) -> tuple[str, ...]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return tuple(row["name"] for row in rows)

    def _write_entity(self, entity: Entity, *, insert_only: bool, connection: sqlite3.Connection | None) -> None:
        spec = _spec_for_model(entity)
        values = _encode_entity(entity, spec)
        columns = tuple(values.keys())
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)
        params = tuple(values[column] for column in columns)
        if insert_only:
            sql = f"INSERT INTO {spec.table} ({column_sql}) VALUES ({placeholders})"
        else:
            updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "id")
            sql = (
                f"INSERT INTO {spec.table} ({column_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}"
            )
        if connection is not None:
            connection.execute(sql, params)
            return
        with self.transaction() as conn:
            conn.execute(sql, params)

    def insert(self, entity: TEntity, *, connection: sqlite3.Connection | None = None) -> TEntity:
        self._write_entity(entity, insert_only=True, connection=connection)
        return entity

    def save(self, entity: TEntity, *, connection: sqlite3.Connection | None = None) -> TEntity:
        self._write_entity(entity, insert_only=False, connection=connection)
        return entity

    def get(self, model: type[TEntity], entity_id: str, *, connection: sqlite3.Connection | None = None) -> TEntity | None:
        spec = _spec_for_model(model)
        try:
            UUID(str(entity_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("entity_id deve ser UUID válido") from exc
        if connection is not None:
            row = connection.execute(f"SELECT * FROM {spec.table} WHERE id = ?", (entity_id,)).fetchone()
        else:
            with self.connection() as conn:
                row = conn.execute(f"SELECT * FROM {spec.table} WHERE id = ?", (entity_id,)).fetchone()
        return None if row is None else _decode_entity(row, spec)  # type: ignore[return-value]

    def list(self, model: type[TEntity], *, limit: int | None = None) -> list[TEntity]:
        spec = _spec_for_model(model)
        sql = f"SELECT * FROM {spec.table} ORDER BY created_at, id"
        params: tuple[Any, ...] = ()
        if limit is not None:
            if limit < 0:
                raise ValueError("limit deve ser >= 0")
            sql += " LIMIT ?"
            params = (limit,)
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_decode_entity(row, spec) for row in rows]  # type: ignore[list-item]

    def delete(self, model: type[TEntity], entity_id: str, *, connection: sqlite3.Connection | None = None) -> bool:
        spec = _spec_for_model(model)
        if connection is not None:
            cursor = connection.execute(f"DELETE FROM {spec.table} WHERE id = ?", (entity_id,))
            return cursor.rowcount > 0
        with self.transaction() as conn:
            cursor = conn.execute(f"DELETE FROM {spec.table} WHERE id = ?", (entity_id,))
            return cursor.rowcount > 0

    def set_setting(self, key: str, value: Any, *, connection: sqlite3.Connection | None = None) -> None:
        key = str(key).strip()
        if not key:
            raise ValueError("setting key não pode ser vazio")
        params = (key, _json_dumps(value), utc_now_iso())
        sql = (
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at"
        )
        if connection is not None:
            connection.execute(sql, params)
        else:
            with self.transaction() as conn:
                conn.execute(sql, params)

    def get_setting(
        self,
        key: str,
        default: Any = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> Any:
        """Lê um valor de ``settings``.

        ``connection``, assim como em ``insert``/``save``/``get``/``delete``/
        ``append_audit_event``/``list_audit_events``, permite compor esta
        leitura dentro de uma transaction já aberta pelo chamador (ex.: um
        check-then-write atômico contra corrida entre processos — ver
        ``ControlManager``/``JobEngine._claim``). Quando omitido (padrão),
        abre sua própria conexão de leitura, como sempre fez.
        """
        if connection is not None:
            row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        else:
            with self.connection() as conn:
                row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return _json_loads(row["value_json"], default=default)

    def delete_setting(self, key: str, *, connection: sqlite3.Connection | None = None) -> bool:
        if connection is not None:
            cursor = connection.execute("DELETE FROM settings WHERE key = ?", (key,))
            return cursor.rowcount > 0
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return cursor.rowcount > 0

    def append_audit_event(
        self,
        event_type: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        data: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        event_type = str(event_type).strip()
        if not event_type:
            raise ValueError("event_type não pode ser vazio")
        event_id = str(event_id or uuid4())
        UUID(event_id)
        if entity_id is not None:
            UUID(str(entity_id))
        params = (
            event_id,
            event_type,
            entity_type,
            entity_id,
            _json_dumps(dict(data or {})),
            utc_now_iso(),
        )
        sql = (
            "INSERT INTO audit_events(id, event_type, entity_type, entity_id, data_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        if connection is not None:
            connection.execute(sql, params)
        else:
            with self.transaction() as conn:
                conn.execute(sql, params)
        return event_id

    def list_audit_events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Lista eventos na ordem exata em que foram anexados ao SQLite.

        ``connection``, assim como em ``insert``/``save``/``get``/``delete``/
        ``append_audit_event``, permite compor esta leitura dentro de uma
        transaction já aberta pelo chamador (ex.: um check-then-insert
        atômico). Quando omitido (padrão), abre sua própria conexão de
        leitura, como sempre fez.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            UUID(str(entity_id))
            clauses.append("entity_id = ?")
            params.append(str(entity_id))
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(str(event_type))

        sql = "SELECT rowid AS sequence, * FROM audit_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY sequence"
        if limit is not None:
            if limit < 0:
                raise ValueError("limit deve ser >= 0")
            sql += " LIMIT ?"
            params.append(limit)
        if connection is not None:
            rows = connection.execute(sql, tuple(params)).fetchall()
        else:
            with self.connection() as conn:
                rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "id": row["id"],
                "event_type": row["event_type"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "data": _json_loads(row["data_json"], default={}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]



__all__ = [
    "DEFAULT_DATABASE_FILENAME",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DatabaseError",
    "MigrationIntegrityError",
    "UnsupportedSchemaVersionError",
    "LocalDatabase",
    "default_database_path",
]
