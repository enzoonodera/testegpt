# -*- coding: utf-8 -*-
"""Backup/recovery local do estado operacional SQLite.

Esta camada protege somente o estado persistido no SQLite. Ela não copia vídeos
originais, renders, perfis de navegador, caches, modelos ou outros arquivos de
mídia. Backups são snapshots consistentes produzidos pela SQLite Backup API.

Restore é uma operação crash-safe baseada em journal persistente e lock de SO.
Artefatos ambíguos nunca são apagados ou interpretados como banco novo.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4

from ..app_paths import AppPaths, PATHS, ensure_app_directories
from ..time_utils import utc_now, utc_now_iso
from .migrations import LATEST_SCHEMA_VERSION, MIGRATIONS


BACKUP_FORMAT_VERSION = 1
BACKUP_DATABASE_FILENAME = "painel.db"
BACKUP_MANIFEST_FILENAME = "manifest.json"
BACKUP_METHOD_SQLITE_API = "SQLITE_BACKUP_API"
BACKUP_VALIDATION_STATUS = "VALIDATED"

BACKUP_REASON_PERIODIC = "PERIODIC"
BACKUP_REASON_PRE_MIGRATION = "PRE_MIGRATION"
BACKUP_REASON_PRE_UPDATE = "PRE_UPDATE"
BACKUP_REASON_PRE_RESTORE = "PRE_RESTORE"
BACKUP_REASON_MANUAL = "MANUAL"
BACKUP_REASONS = frozenset({
    BACKUP_REASON_PERIODIC,
    BACKUP_REASON_PRE_MIGRATION,
    BACKUP_REASON_PRE_UPDATE,
    BACKUP_REASON_PRE_RESTORE,
    BACKUP_REASON_MANUAL,
})

RESTORE_STATE_FILENAME = "restore_state.json"
RESTORE_LOCK_FILENAME = "restore.lock"
RESTORE_PHASE_PREPARED = "PREPARED"
RESTORE_PHASE_OLD_MOVED = "OLD_MOVED"
RESTORE_PHASE_NEW_PUBLISHED = "NEW_PUBLISHED"
RESTORE_PHASE_VERIFIED = "VERIFIED"
RESTORE_PHASE_ROLLED_BACK = "ROLLED_BACK"
RESTORE_PHASES = frozenset({
    RESTORE_PHASE_PREPARED,
    RESTORE_PHASE_OLD_MOVED,
    RESTORE_PHASE_NEW_PUBLISHED,
    RESTORE_PHASE_VERIFIED,
    RESTORE_PHASE_ROLLED_BACK,
})

# Metadata de manifest é deliberadamente allowlistada. Nada arbitrário como
# cookies/tokens/credentials/perfis pode ser persistido por engano.
_ALLOWED_METADATA_FIELDS: dict[str, frozenset[str]] = {
    BACKUP_REASON_PERIODIC: frozenset({"interval_seconds"}),
    BACKUP_REASON_PRE_MIGRATION: frozenset({"from_schema", "to_schema"}),
    BACKUP_REASON_PRE_UPDATE: frozenset({"update_label"}),
    BACKUP_REASON_PRE_RESTORE: frozenset({"source_backup_id", "restore_id"}),
    BACKUP_REASON_MANUAL: frozenset({"label"}),
}


class BackupError(RuntimeError):
    """Erro base do subsistema de backup local."""


class BackupValidationError(BackupError):
    """Backup/manifest não é seguro para uso ou restore."""


class RestoreError(BackupError):
    """Restore não pôde ser concluído de forma segura."""


class RestoreInProgressError(RestoreError):
    """Outra instância/processo mantém exclusão do restore deste banco."""


class RestoreRecoveryRequiredError(RestoreError):
    """Há estado de restore ambíguo que exige recuperação explícita."""


@dataclass(frozen=True)
class BackupRetentionPolicy:
    """Política opt-in para limpeza explícita futura.

    O default é preservação total. ``create_backup`` e gatilhos periódicos/
    pre-migration/pre-update/pre-restore **nunca** executam retenção
    automaticamente. ``prune_backups`` só age quando chamado deliberadamente.
    """

    max_backups: int | None = None
    max_age_days: int | None = None

    def __post_init__(self) -> None:
        if self.max_backups is not None and self.max_backups < 1:
            raise ValueError("max_backups deve ser >= 1 ou None")
        if self.max_age_days is not None and self.max_age_days < 1:
            raise ValueError("max_age_days deve ser >= 1 ou None")


@dataclass(frozen=True)
class BackupRecord:
    backup_id: str
    path: Path
    reason: str
    created_at: str
    schema_version: int
    database_sha256: str
    database_size_bytes: int


@dataclass(frozen=True)
class BackupValidation:
    record: BackupRecord
    integrity_check: str
    migration_versions: tuple[int, ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_utc(value: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp do manifest precisa ter timezone")
    return parsed.astimezone(timezone.utc)


def _fsync_directory(path: Path) -> None:
    """Persiste rename/replace do diretório quando a plataforma permite."""
    if os.name == "nt":
        # Windows não oferece fsync de diretório via os.open de forma portátil.
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync_directory(path.parent)


def _safe_scalar_metadata(reason: str, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    values = dict(metadata or {})
    allowed = _ALLOWED_METADATA_FIELDS[reason]
    unknown = set(values) - set(allowed)
    if unknown:
        raise ValueError(
            "metadata não permitida para backup "
            f"{reason}: {', '.join(sorted(str(item) for item in unknown))}"
        )

    clean: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if key in {"from_schema", "to_schema"}:
            clean[key] = int(value)
        elif key == "interval_seconds":
            numeric = float(value)
            if numeric <= 0:
                raise ValueError("interval_seconds deve ser positivo")
            clean[key] = numeric
        else:
            text = str(value).strip()
            if not text:
                continue
            if len(text) > 256:
                raise ValueError(f"metadata {key} excede 256 caracteres")
            clean[key] = text
    return clean


class _InterProcessRestoreLock:
    """Lock de SO liberado automaticamente quando o processo morre.

    O arquivo pode permanecer no disco, mas sua mera existência nunca significa
    lock ativo. A exclusão é mantida pelo kernel (msvcrt no Windows, flock em
    POSIX), portanto não há "stale lock file" permanente após crash.
    """

    def __init__(self, path: Path, *, timeout_seconds: float) -> None:
        self.path = Path(path)
        self.timeout_seconds = max(float(timeout_seconds), 0.0)
        self._handle = None

    def _try_lock(self) -> bool:
        assert self._handle is not None
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def __enter__(self) -> "_InterProcessRestoreLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            if self._try_lock():
                return self
            if time.monotonic() >= deadline:
                self._handle.close()
                self._handle = None
                raise RestoreInProgressError(
                    "outra instância/processo está executando restore neste banco"
                )
            time.sleep(0.025)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class BackupManager:
    """Cria, valida e restaura snapshots locais do SQLite."""

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        database_path: Path | str | None = None,
        retention: BackupRetentionPolicy | None = None,
        busy_timeout_ms: int = 5_000,
        restore_lock_timeout_seconds: float = 1.0,
    ) -> None:
        self.paths = paths or PATHS
        self.database_path = (
            Path(database_path).expanduser().resolve()
            if database_path is not None
            else (self.paths.database / BACKUP_DATABASE_FILENAME).resolve()
        )
        self.backup_root = (self.paths.backups / "operational").resolve()
        self.staging_root = self.backup_root / ".staging"
        self.retention = retention or BackupRetentionPolicy()
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.restore_lock_timeout_seconds = float(restore_lock_timeout_seconds)
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms deve ser >= 0")
        if self.restore_lock_timeout_seconds < 0:
            raise ValueError("restore_lock_timeout_seconds deve ser >= 0")

    @property
    def restore_state_path(self) -> Path:
        return self.database_path.parent / RESTORE_STATE_FILENAME

    @property
    def restore_lock_path(self) -> Path:
        return self.database_path.parent / RESTORE_LOCK_FILENAME

    def _ensure_directories(self) -> None:
        ensure_app_directories(self.paths)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_migration_registry() -> None:
        versions = [item.version for item in MIGRATIONS]
        expected = list(range(1, len(MIGRATIONS) + 1))
        if versions != expected:
            raise BackupValidationError(
                f"registro conhecido de migrations não é contínuo: {versions!r}"
            )

    @classmethod
    def _inspect_database(cls, path: Path) -> tuple[int, str, tuple[int, ...]]:
        """Valida integridade física e histórico conhecido sem modificar o DB."""
        path = Path(path)
        if not path.is_file():
            raise BackupValidationError(f"arquivo SQLite ausente: {path}")
        cls._validate_migration_registry()
        try:
            conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA query_only = ON")
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity.lower() != "ok":
                    raise BackupValidationError(
                        f"integrity_check do backup falhou: {integrity}"
                    )
                schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if schema_version > LATEST_SCHEMA_VERSION:
                    raise BackupValidationError(
                        f"backup usa schema v{schema_version}, mais novo que este binário "
                        f"(v{LATEST_SCHEMA_VERSION})"
                    )
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
                ).fetchone()
                if table is None:
                    raise BackupValidationError("backup não possui histórico de migrations")
                rows = conn.execute(
                    "SELECT version, name, checksum FROM migrations ORDER BY version"
                ).fetchall()
                applied = tuple(int(row["version"]) for row in rows)
                expected = tuple(item.version for item in MIGRATIONS[: len(rows)])
                if applied != expected:
                    raise BackupValidationError(
                        f"histórico de migrations não é prefixo contínuo: {applied!r}"
                    )
                for row, migration in zip(rows, MIGRATIONS):
                    if row["name"] != migration.name or row["checksum"] != migration.checksum:
                        raise BackupValidationError(
                            f"migration {int(row['version']):03d} do backup diverge do binário"
                        )
                current = applied[-1] if applied else 0
                if schema_version != current:
                    raise BackupValidationError(
                        f"user_version={schema_version} diverge do histórico v{current}"
                    )
                return schema_version, integrity, applied
            finally:
                conn.close()
        except BackupValidationError:
            raise
        except sqlite3.DatabaseError as exc:
            raise BackupValidationError(f"SQLite inválido/corrompido: {path.name}") from exc

    @staticmethod
    def _remove_temp_sidecars(path: Path) -> None:
        """Remove somente sidecars de arquivos temporários inequivocamente nossos."""
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass

    def _snapshot_sqlite(self, destination: Path) -> None:
        if not self.database_path.is_file():
            raise BackupError(f"banco operacional não existe: {self.database_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(
            str(self.database_path),
            timeout=max(self.busy_timeout_ms / 1000.0, 0.001),
            isolation_level=None,
        )
        try:
            target = sqlite3.connect(str(destination), isolation_level=None)
            try:
                source.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    def create_backup(
        self,
        *,
        reason: str = BACKUP_REASON_MANUAL,
        metadata: Mapping[str, Any] | None = None,
    ) -> BackupRecord:
        """Cria backup persistente sem executar retenção automaticamente."""
        reason = str(reason or "").strip().upper()
        if reason not in BACKUP_REASONS:
            raise ValueError(f"motivo de backup inválido: {reason}")
        safe_metadata = _safe_scalar_metadata(reason, metadata)
        self._ensure_directories()

        source_schema, source_integrity, _ = self._inspect_database(self.database_path)
        if source_integrity.lower() != "ok":
            raise BackupValidationError("banco de origem falhou no integrity_check")

        backup_id = uuid4().hex
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        dirname = f"{stamp}_{reason.lower()}_{backup_id[:12]}"
        stage = self.staging_root / backup_id
        final = self.backup_root / dirname
        if stage.exists():
            # UUID recém-gerado colidindo com staging existente é ambíguo; não apagar.
            raise BackupError(f"staging de backup já existe inesperadamente: {stage}")
        stage.mkdir(parents=True, exist_ok=False)

        snapshot = stage / BACKUP_DATABASE_FILENAME
        try:
            self._snapshot_sqlite(snapshot)
            schema_version, integrity, migrations = self._inspect_database(snapshot)
            self._remove_temp_sidecars(snapshot)
            if schema_version != source_schema:
                raise BackupValidationError(
                    f"snapshot mudou schema durante backup: origem={source_schema} snapshot={schema_version}"
                )
            size = snapshot.stat().st_size
            sha256 = _sha256_file(snapshot)
            created_at = utc_now_iso()
            manifest = {
                "manifest_version": BACKUP_FORMAT_VERSION,
                "backup_id": backup_id,
                "created_at": created_at,
                "reason": reason,
                "validation_status": BACKUP_VALIDATION_STATUS,
                "backup_method": BACKUP_METHOD_SQLITE_API,
                "database": {
                    "file": BACKUP_DATABASE_FILENAME,
                    "logical_path": f"database/{BACKUP_DATABASE_FILENAME}",
                    "size_bytes": size,
                    "sha256": sha256,
                    "schema_version": schema_version,
                    "integrity_check": integrity,
                    "migration_versions": list(migrations),
                },
                "scope": {
                    "includes": ["sqlite_operational_state"],
                    "excludes": [
                        "original_videos",
                        "rendered_media",
                        "browser_profiles",
                        "cache",
                        "temp",
                        "models",
                    ],
                },
                "metadata": safe_metadata,
            }
            _atomic_write_json(stage / BACKUP_MANIFEST_FILENAME, manifest)
            os.replace(stage, final)
            _fsync_directory(final.parent)
            validation = self.validate_backup(final)
            return validation.record
        except Exception:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            self._remove_temp_sidecars(snapshot)
            raise

    def backup_before_update(self, *, update_label: str | None = None) -> BackupRecord:
        details: dict[str, Any] = {}
        if update_label:
            details["update_label"] = str(update_label)
        return self.create_backup(reason=BACKUP_REASON_PRE_UPDATE, metadata=details)

    def backup_before_migration(self, *, from_schema: int, to_schema: int) -> BackupRecord:
        return self.create_backup(
            reason=BACKUP_REASON_PRE_MIGRATION,
            metadata={"from_schema": int(from_schema), "to_schema": int(to_schema)},
        )

    def _read_manifest(self, backup_dir: Path) -> dict[str, Any]:
        manifest_path = backup_dir / BACKUP_MANIFEST_FILENAME
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupValidationError(f"manifest inválido/ausente: {backup_dir}") from exc
        if not isinstance(payload, dict):
            raise BackupValidationError("manifest precisa ser objeto JSON")
        return payload

    def validate_backup(self, backup_dir: Path | str) -> BackupValidation:
        backup_dir = Path(backup_dir).expanduser().resolve()
        if not backup_dir.is_dir():
            raise BackupValidationError(f"diretório de backup inexistente: {backup_dir}")
        manifest = self._read_manifest(backup_dir)
        if manifest.get("manifest_version") != BACKUP_FORMAT_VERSION:
            raise BackupValidationError("versão de manifest não suportada")
        backup_id = str(manifest.get("backup_id") or "").strip()
        reason = str(manifest.get("reason") or "").strip().upper()
        created_at = str(manifest.get("created_at") or "").strip()
        if not backup_id or reason not in BACKUP_REASONS:
            raise BackupValidationError("identidade/motivo do backup inválidos")
        if manifest.get("backup_method") != BACKUP_METHOD_SQLITE_API:
            raise BackupValidationError("método de backup não suportado")
        if manifest.get("validation_status") != BACKUP_VALIDATION_STATUS:
            raise BackupValidationError("manifest não declara backup validado")
        try:
            _parse_utc(created_at)
        except (TypeError, ValueError) as exc:
            raise BackupValidationError("created_at do manifest é inválido") from exc

        database = manifest.get("database")
        if not isinstance(database, dict):
            raise BackupValidationError("manifest sem bloco database")
        if database.get("file") != BACKUP_DATABASE_FILENAME:
            raise BackupValidationError("manifest referencia arquivo de banco não permitido")
        if database.get("logical_path") != f"database/{BACKUP_DATABASE_FILENAME}":
            raise BackupValidationError("logical_path do banco é inválido")
        snapshot = backup_dir / BACKUP_DATABASE_FILENAME
        if not snapshot.is_file():
            raise BackupValidationError("snapshot SQLite ausente")
        actual_size = snapshot.stat().st_size
        actual_sha = _sha256_file(snapshot)
        if int(database.get("size_bytes", -1)) != actual_size:
            raise BackupValidationError("tamanho do snapshot diverge do manifest")
        if str(database.get("sha256") or "").lower() != actual_sha:
            raise BackupValidationError("SHA-256 do snapshot diverge do manifest")

        schema_version, integrity, migrations = self._inspect_database(snapshot)
        self._remove_temp_sidecars(snapshot)
        if int(database.get("schema_version", -1)) != schema_version:
            raise BackupValidationError("schema_version diverge do manifest")
        if str(database.get("integrity_check") or "").lower() != "ok" or integrity.lower() != "ok":
            raise BackupValidationError("backup não possui integrity_check=ok")
        manifest_migrations = tuple(int(value) for value in database.get("migration_versions", []))
        if manifest_migrations != migrations:
            raise BackupValidationError("histórico de migrations diverge do manifest")

        return BackupValidation(
            record=BackupRecord(
                backup_id=backup_id,
                path=backup_dir,
                reason=reason,
                created_at=created_at,
                schema_version=schema_version,
                database_sha256=actual_sha,
                database_size_bytes=actual_size,
            ),
            integrity_check=integrity,
            migration_versions=migrations,
        )

    def list_backups(self, *, validate: bool = True) -> list[BackupRecord]:
        """Lista backups descobertos; por padrão retorna somente backups válidos.

        ``validate=False`` existe apenas para discovery/diagnóstico explícito.
        Decisões de proteção operacional (periodicidade/restore/UI futura) devem
        usar validação completa para que snapshot/manifest corrompido nunca conte
        como proteção disponível.
        """
        self._ensure_directories()
        records: list[BackupRecord] = []
        for item in self.backup_root.iterdir():
            if not item.is_dir() or item.name.startswith("."):
                continue
            try:
                if validate:
                    record = self.validate_backup(item).record
                else:
                    manifest = self._read_manifest(item)
                    database = manifest.get("database") or {}
                    record = BackupRecord(
                        backup_id=str(manifest["backup_id"]),
                        path=item.resolve(),
                        reason=str(manifest["reason"]).upper(),
                        created_at=str(manifest["created_at"]),
                        schema_version=int(database["schema_version"]),
                        database_sha256=str(database["sha256"]),
                        database_size_bytes=int(database["size_bytes"]),
                    )
                    _parse_utc(record.created_at)
                records.append(record)
            except (BackupValidationError, KeyError, TypeError, ValueError):
                continue
        records.sort(key=lambda item: (_parse_utc(item.created_at), item.backup_id), reverse=True)
        return records

    def maybe_create_periodic(
        self,
        *,
        interval: timedelta = timedelta(hours=24),
    ) -> BackupRecord | None:
        if interval.total_seconds() <= 0:
            raise ValueError("interval precisa ser positivo")
        now = utc_now()
        periodic = [
            item
            for item in self.list_backups(validate=True)
            if item.reason == BACKUP_REASON_PERIODIC
        ]
        if periodic:
            newest = max(_parse_utc(item.created_at) for item in periodic)
            if now - newest < interval:
                return None
        return self.create_backup(
            reason=BACKUP_REASON_PERIODIC,
            metadata={"interval_seconds": interval.total_seconds()},
        )

    def prune_backups(
        self,
        *,
        policy: BackupRetentionPolicy | None = None,
        preserve: set[str] | frozenset[str] | None = None,
    ) -> tuple[Path, ...]:
        """Helper explícito; nunca é chamado automaticamente por create/restore."""
        policy = policy or self.retention
        preserve_ids = set(preserve or ())
        records = self.list_backups(validate=False)
        remove: dict[str, BackupRecord] = {}
        now = utc_now()
        if policy.max_age_days is not None:
            cutoff = now - timedelta(days=policy.max_age_days)
            for record in records:
                if record.backup_id not in preserve_ids and _parse_utc(record.created_at) < cutoff:
                    remove[record.backup_id] = record

        remaining = [item for item in records if item.backup_id not in remove]
        if policy.max_backups is not None and len(remaining) > policy.max_backups:
            keep_ids = {item.backup_id for item in remaining if item.backup_id in preserve_ids}
            target_keep = max(policy.max_backups, len(keep_ids))
            for record in remaining:
                if len(keep_ids) >= target_keep:
                    break
                keep_ids.add(record.backup_id)
            for record in remaining:
                if record.backup_id not in keep_ids:
                    remove[record.backup_id] = record

        removed: list[Path] = []
        for record in remove.values():
            try:
                self.validate_backup(record.path)
                shutil.rmtree(record.path)
                removed.append(record.path)
            except (OSError, BackupValidationError):
                continue
        return tuple(removed)

    def _checkpoint_target_for_restore(self) -> None:
        if not self.database_path.exists():
            return
        conn = sqlite3.connect(
            str(self.database_path),
            timeout=max(self.busy_timeout_ms / 1000.0, 0.001),
            isolation_level=None,
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is not None and int(row[0]) != 0:
                raise RestoreError(
                    "banco está em uso; restore exige fechar operações/conexões ativas"
                )
        except sqlite3.DatabaseError as exc:
            raise RestoreError("não foi possível preparar o banco atual para restore") from exc
        finally:
            conn.close()

    def _restore_paths(self, restore_id: str) -> tuple[Path, Path]:
        if not restore_id or any(ch not in "0123456789abcdef" for ch in restore_id.lower()):
            raise RestoreRecoveryRequiredError("restore_id inválido no journal")
        temp = self.database_path.with_name(f"{self.database_path.name}.restore_tmp.{restore_id}")
        previous = self.database_path.with_name(
            f"{self.database_path.name}.restore_previous.{restore_id}"
        )
        return temp, previous

    def _legacy_or_restore_artifacts(self) -> tuple[Path, ...]:
        parent = self.database_path.parent
        patterns = (
            f"{self.database_path.name}.restore_tmp*",
            f"{self.database_path.name}.restore_previous*",
            f"{self.database_path.name}.restore_recovery*",
        )
        found: dict[str, Path] = {}
        for pattern in patterns:
            for path in parent.glob(pattern):
                found[str(path.resolve())] = path.resolve()
        return tuple(sorted(found.values(), key=lambda item: item.name))

    def _load_restore_journal(self) -> dict[str, Any] | None:
        if not self.restore_state_path.exists():
            return None
        try:
            payload = json.loads(self.restore_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RestoreRecoveryRequiredError(
                f"journal de restore inválido: {self.restore_state_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise RestoreRecoveryRequiredError("journal de restore precisa ser objeto JSON")
        return payload

    def _validate_restore_journal(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "restore_id",
            "source_backup_id",
            "safety_backup_id",
            "expected_source_sha256",
            "expected_previous_sha256",
            "safety_backup_sha256",
            "expected_source_schema_version",
            "expected_previous_schema_version",
            "database_logical_path",
            "staging_name",
            "previous_name",
            "phase",
            "created_at",
            "updated_at",
        }
        missing = required - set(payload)
        if missing:
            raise RestoreRecoveryRequiredError(
                f"journal de restore incompleto: {', '.join(sorted(missing))}"
            )
        restore_id = str(payload["restore_id"])
        temp, previous = self._restore_paths(restore_id)
        if str(payload["staging_name"]) != temp.name or str(payload["previous_name"]) != previous.name:
            raise RestoreRecoveryRequiredError("paths do journal não correspondem ao restore_id")
        if str(payload["database_logical_path"]) != f"database/{self.database_path.name}":
            raise RestoreRecoveryRequiredError("database_logical_path do journal é inválido")
        phase = str(payload["phase"])
        if phase not in RESTORE_PHASES:
            raise RestoreRecoveryRequiredError(f"fase de restore desconhecida: {phase}")
        for key in ("created_at", "updated_at"):
            try:
                _parse_utc(str(payload[key]))
            except (TypeError, ValueError) as exc:
                raise RestoreRecoveryRequiredError(f"{key} inválido no journal") from exc
        source_sha = str(payload["expected_source_sha256"]).lower()
        previous_sha = str(payload["expected_previous_sha256"]).lower()
        safety_sha = str(payload["safety_backup_sha256"]).lower()
        if len(source_sha) != 64 or len(previous_sha) != 64 or len(safety_sha) != 64:
            raise RestoreRecoveryRequiredError("SHA esperado inválido no journal")
        return dict(payload)

    def _write_restore_journal(self, payload: Mapping[str, Any]) -> None:
        _atomic_write_json(self.restore_state_path, payload)

    def _set_restore_phase(self, journal: dict[str, Any], phase: str) -> None:
        if phase not in RESTORE_PHASES:
            raise ValueError(f"fase inválida: {phase}")
        journal["phase"] = phase
        journal["updated_at"] = utc_now_iso()
        self._write_restore_journal(journal)
        self._after_restore_phase(phase, dict(journal))

    def _after_restore_phase(self, phase: str, journal: Mapping[str, Any]) -> None:
        """Hook sem efeito usado por testes de crash em fronteiras persistidas."""
        return None

    def _find_backup_by_id(self, backup_id: str) -> BackupValidation:
        for record in self.list_backups(validate=False):
            if record.backup_id == backup_id:
                return self.validate_backup(record.path)
        raise RestoreRecoveryRequiredError(f"backup referenciado não encontrado: {backup_id}")

    def _path_matches_database(self, path: Path, *, expected_sha: str, expected_schema: int) -> bool:
        if not path.is_file():
            return False
        try:
            schema, integrity, _ = self._inspect_database(path)
            return (
                integrity.lower() == "ok"
                and schema == expected_schema
                and _sha256_file(path) == expected_sha
            )
        except BackupValidationError:
            return False

    @staticmethod
    def _move_sidecars(source_db: Path, destination_db: Path) -> None:
        for suffix in ("-wal", "-shm"):
            source = Path(str(source_db) + suffix)
            destination = Path(str(destination_db) + suffix)
            if source.exists():
                if destination.exists():
                    raise RestoreRecoveryRequiredError(
                        f"sidecar de destino já existe ambiguamente: {destination.name}"
                    )
                os.replace(source, destination)

    def _cleanup_owned_restore_artifacts(self, temp: Path, previous: Path) -> None:
        # Só chamado depois de fase conhecida/verificada. Não toca em artefatos
        # sem journal nem em WAL/SHM do banco operacional.
        for path in (temp, previous):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            self._remove_temp_sidecars(path)

    def _remove_restore_journal(self) -> None:
        try:
            self.restore_state_path.unlink(missing_ok=True)
            _fsync_directory(self.restore_state_path.parent)
        except OSError as exc:
            raise RestoreRecoveryRequiredError("não foi possível finalizar journal de restore") from exc

    def _publish_backup_snapshot_to_operational(
        self,
        validation: BackupValidation,
        *,
        target: Path,
    ) -> None:
        source = validation.record.path / BACKUP_DATABASE_FILENAME
        recovery = target.with_name(target.name + ".restore_recovery_tmp")
        if recovery.exists() or self._legacy_or_restore_artifacts() and recovery in self._legacy_or_restore_artifacts():
            raise RestoreRecoveryRequiredError(
                f"staging de recovery já existe ambiguamente: {recovery.name}"
            )
        shutil.copy2(source, recovery)
        try:
            schema, integrity, _ = self._inspect_database(recovery)
            self._remove_temp_sidecars(recovery)
            if (
                integrity.lower() != "ok"
                or schema != validation.record.schema_version
                or _sha256_file(recovery) != validation.record.database_sha256
            ):
                raise RestoreRecoveryRequiredError("snapshot de safety backup falhou na validação")
            os.replace(recovery, target)
            _fsync_directory(target.parent)
        finally:
            recovery.unlink(missing_ok=True)
            self._remove_temp_sidecars(recovery)

    def _rollback_to_previous_locked(
        self,
        journal: dict[str, Any],
        temp: Path,
        previous: Path,
    ) -> None:
        expected_previous = str(journal["expected_previous_sha256"])
        expected_previous_schema = int(journal["expected_previous_schema_version"])

        if previous.exists() and self._path_matches_database(
            previous,
            expected_sha=expected_previous,
            expected_schema=expected_previous_schema,
        ):
            if self.database_path.exists():
                # O candidato oficial pertence à tentativa conhecida; após validar
                # previous, pode ser substituído deterministicamente pelo anterior.
                self.database_path.unlink()
                self._remove_temp_sidecars(self.database_path)
            os.replace(previous, self.database_path)
            self._move_sidecars(previous, self.database_path)
            _fsync_directory(self.database_path.parent)
        else:
            safety = self._find_backup_by_id(str(journal["safety_backup_id"]))
            expected_safety = str(journal["safety_backup_sha256"])
            if safety.record.database_sha256 != expected_safety:
                raise RestoreRecoveryRequiredError(
                    "safety backup não corresponde ao SHA registrado no journal"
                )
            if self.database_path.exists():
                self.database_path.unlink()
                self._remove_temp_sidecars(self.database_path)
            self._publish_backup_snapshot_to_operational(safety, target=self.database_path)
            expected_previous = expected_safety

        if not self._path_matches_database(
            self.database_path,
            expected_sha=expected_previous,
            expected_schema=expected_previous_schema,
        ):
            raise RestoreRecoveryRequiredError("rollback do restore não pôde ser validado")
        self._set_restore_phase(journal, RESTORE_PHASE_ROLLED_BACK)
        self._cleanup_owned_restore_artifacts(temp, previous)
        self._remove_restore_journal()

    def _finalize_published_locked(
        self,
        journal: dict[str, Any],
        temp: Path,
        previous: Path,
    ) -> None:
        if not self._path_matches_database(
            self.database_path,
            expected_sha=str(journal["expected_source_sha256"]),
            expected_schema=int(journal["expected_source_schema_version"]),
        ):
            self._rollback_to_previous_locked(journal, temp, previous)
            return
        self._set_restore_phase(journal, RESTORE_PHASE_VERIFIED)
        self._cleanup_owned_restore_artifacts(temp, previous)
        self._remove_restore_journal()

    def _recover_interrupted_restore_locked(self) -> bool:
        raw = self._load_restore_journal()
        artifacts = self._legacy_or_restore_artifacts()
        if raw is None:
            if artifacts:
                names = ", ".join(path.name for path in artifacts)
                raise RestoreRecoveryRequiredError(
                    "artefatos de restore existem sem journal suficiente; "
                    f"nada foi apagado: {names}"
                )
            return False

        journal = self._validate_restore_journal(raw)
        temp, previous = self._restore_paths(str(journal["restore_id"]))
        phase = str(journal["phase"])
        expected_source = str(journal["expected_source_sha256"])
        expected_previous = str(journal["expected_previous_sha256"])
        expected_source_schema = int(journal["expected_source_schema_version"])
        expected_previous_schema = int(journal["expected_previous_schema_version"])

        # Qualquer artefato de restore não pertencente ao journal atual torna o
        # estado ambíguo. Preservamos tudo e exigimos intervenção.
        allowed = {
            temp.resolve(),
            previous.resolve(),
            Path(str(temp) + "-wal").resolve(),
            Path(str(temp) + "-shm").resolve(),
            Path(str(previous) + "-wal").resolve(),
            Path(str(previous) + "-shm").resolve(),
        }
        unexpected = [path for path in artifacts if path.resolve() not in allowed]
        if unexpected:
            raise RestoreRecoveryRequiredError(
                "artefatos de restore não pertencem ao journal atual; nada foi apagado: "
                + ", ".join(path.name for path in unexpected)
            )

        if phase == RESTORE_PHASE_PREPARED:
            # PREPARED garante que o banco antigo ainda deveria ser oficial. Se o
            # move ocorreu antes do journal avançar e houve crash, previous permite
            # rollback determinístico.
            if self._path_matches_database(
                self.database_path,
                expected_sha=expected_previous,
                expected_schema=expected_previous_schema,
            ):
                self._cleanup_owned_restore_artifacts(temp, previous)
                self._remove_restore_journal()
                return True
            self._rollback_to_previous_locked(journal, temp, previous)
            return True

        if phase == RESTORE_PHASE_OLD_MOVED:
            # Janela possível: publish ocorreu, mas NEW_PUBLISHED não foi fsyncado.
            if self._path_matches_database(
                self.database_path,
                expected_sha=expected_source,
                expected_schema=expected_source_schema,
            ):
                self._finalize_published_locked(journal, temp, previous)
            else:
                self._rollback_to_previous_locked(journal, temp, previous)
            return True

        if phase == RESTORE_PHASE_NEW_PUBLISHED:
            self._finalize_published_locked(journal, temp, previous)
            return True

        if phase == RESTORE_PHASE_VERIFIED:
            if not self._path_matches_database(
                self.database_path,
                expected_sha=expected_source,
                expected_schema=expected_source_schema,
            ):
                raise RestoreRecoveryRequiredError(
                    "journal VERIFIED existe, mas banco oficial não corresponde ao restore verificado"
                )
            self._cleanup_owned_restore_artifacts(temp, previous)
            self._remove_restore_journal()
            return True

        if phase == RESTORE_PHASE_ROLLED_BACK:
            if not self._path_matches_database(
                self.database_path,
                expected_sha=expected_previous,
                expected_schema=expected_previous_schema,
            ):
                raise RestoreRecoveryRequiredError(
                    "journal ROLLED_BACK existe, mas banco anterior não está validado"
                )
            self._cleanup_owned_restore_artifacts(temp, previous)
            self._remove_restore_journal()
            return True

        raise RestoreRecoveryRequiredError(f"fase não recuperável: {phase}")

    def recover_pending_restore(self) -> bool:
        """Recupera/recusa restore interrompido antes de qualquer initialize.

        O lock é adquirido mesmo quando ainda não há journal/artefatos. Isso fecha
        a janela em que outro processo já iniciou um restore, mas ainda não chegou
        à fase PREPARED; ``initialize`` nunca passa por cima de um restore ativo.
        """
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with _InterProcessRestoreLock(
            self.restore_lock_path,
            timeout_seconds=self.restore_lock_timeout_seconds,
        ):
            return self._recover_interrupted_restore_locked()

    def restore_backup(self, backup_dir: Path | str) -> BackupRecord:
        """Restore crash-safe com safety backup persistente e journal."""
        self._ensure_directories()
        validation = self.validate_backup(backup_dir)
        restore_id = uuid4().hex

        with _InterProcessRestoreLock(
            self.restore_lock_path,
            timeout_seconds=self.restore_lock_timeout_seconds,
        ):
            # Um novo restore nunca começa apagando artefatos antigos. Primeiro
            # recupera ou recusa qualquer operação anterior conhecida.
            self._recover_interrupted_restore_locked()

            if not self.database_path.exists():
                raise RestoreError(
                    "banco operacional atual não existe; restore destrutivo exige estado atual recuperável"
                )

            # Safety backup é obrigatório e persistente. Qualquer falha aqui aborta
            # antes de checkpoint/staging/journal/swap; o banco operacional não é
            # modificado pela tentativa de restore.
            safety = self.create_backup(
                reason=BACKUP_REASON_PRE_RESTORE,
                metadata={
                    "source_backup_id": validation.record.backup_id,
                    "restore_id": restore_id,
                },
            )
            safety_validation = self.validate_backup(safety.path)
            safety_backup_sha = safety_validation.record.database_sha256
            expected_previous_schema = safety_validation.record.schema_version
            expected_source_schema = validation.record.schema_version

            # Depois do safety backup confirmado, quiesce o WAL antes de preparar
            # qualquer swap. Falha aqui ainda não move/apaga o banco atual. O hash
            # do arquivo principal é capturado *depois* do checkpoint porque a
            # compactação do WAL pode alterar bytes sem alterar o estado lógico.
            self._checkpoint_target_for_restore()
            current_schema, current_integrity, _ = self._inspect_database(self.database_path)
            if current_integrity.lower() != "ok" or current_schema != expected_previous_schema:
                raise RestoreError("banco operacional mudou/inconsistente após safety backup")
            expected_previous_sha = _sha256_file(self.database_path)

            temp, previous = self._restore_paths(restore_id)
            # Como recover_pending confirmou ausência de artefatos, colisão aqui é
            # anomalia. Não apagar nada automaticamente.
            if temp.exists() or previous.exists():
                raise RestoreRecoveryRequiredError(
                    "staging/previous do novo restore já existem inesperadamente"
                )

            source = validation.record.path / BACKUP_DATABASE_FILENAME
            shutil.copy2(source, temp)
            try:
                temp_schema, temp_integrity, _ = self._inspect_database(temp)
                self._remove_temp_sidecars(temp)
                if (
                    temp_integrity.lower() != "ok"
                    or temp_schema != validation.record.schema_version
                    or _sha256_file(temp) != validation.record.database_sha256
                ):
                    raise BackupValidationError("staging de restore falhou na validação")

                journal: dict[str, Any] = {
                    "restore_id": restore_id,
                    "source_backup_id": validation.record.backup_id,
                    "safety_backup_id": safety.backup_id,
                    "expected_source_sha256": validation.record.database_sha256,
                    "expected_previous_sha256": expected_previous_sha,
                    "safety_backup_sha256": safety_backup_sha,
                    "expected_source_schema_version": expected_source_schema,
                    "expected_previous_schema_version": expected_previous_schema,
                    "database_logical_path": f"database/{self.database_path.name}",
                    "staging_name": temp.name,
                    "previous_name": previous.name,
                    "phase": RESTORE_PHASE_PREPARED,
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                }
                self._write_restore_journal(journal)
                self._after_restore_phase(RESTORE_PHASE_PREPARED, dict(journal))

                # Move main + sidecars depois de checkpoint. Sidecars do banco
                # operacional não são apagados cegamente; acompanham o previous.
                os.replace(self.database_path, previous)
                self._move_sidecars(self.database_path, previous)
                _fsync_directory(self.database_path.parent)
                self._set_restore_phase(journal, RESTORE_PHASE_OLD_MOVED)

                os.replace(temp, self.database_path)
                _fsync_directory(self.database_path.parent)
                self._set_restore_phase(journal, RESTORE_PHASE_NEW_PUBLISHED)

                if not self._path_matches_database(
                    self.database_path,
                    expected_sha=validation.record.database_sha256,
                    expected_schema=validation.record.schema_version,
                ):
                    raise RestoreError("banco restaurado falhou na validação pós-swap")

                self._set_restore_phase(journal, RESTORE_PHASE_VERIFIED)
                self._cleanup_owned_restore_artifacts(temp, previous)
                self._remove_restore_journal()
                return validation.record
            except Exception as exc:
                # Exceções normais tentam recovery determinístico; BaseException
                # (usado nos testes para simular queda abrupta) deixa journal/artefatos
                # exatamente como um processo morto deixaria.
                try:
                    if self.restore_state_path.exists():
                        self._recover_interrupted_restore_locked()
                    else:
                        temp.unlink(missing_ok=True)
                        self._remove_temp_sidecars(temp)
                except Exception as recovery_exc:
                    raise RestoreRecoveryRequiredError(
                        "restore falhou e recovery automático não pôde ser concluído; "
                        "artefatos foram preservados"
                    ) from recovery_exc
                if isinstance(exc, BackupError):
                    raise
                raise RestoreError("restore falhou; estado anterior foi recuperado") from exc


__all__ = [
    "BACKUP_FORMAT_VERSION",
    "BACKUP_DATABASE_FILENAME",
    "BACKUP_MANIFEST_FILENAME",
    "BACKUP_METHOD_SQLITE_API",
    "BACKUP_VALIDATION_STATUS",
    "BACKUP_REASON_PERIODIC",
    "BACKUP_REASON_PRE_MIGRATION",
    "BACKUP_REASON_PRE_UPDATE",
    "BACKUP_REASON_PRE_RESTORE",
    "BACKUP_REASON_MANUAL",
    "BACKUP_REASONS",
    "RESTORE_STATE_FILENAME",
    "RESTORE_LOCK_FILENAME",
    "RESTORE_PHASE_PREPARED",
    "RESTORE_PHASE_OLD_MOVED",
    "RESTORE_PHASE_NEW_PUBLISHED",
    "RESTORE_PHASE_VERIFIED",
    "RESTORE_PHASE_ROLLED_BACK",
    "BackupError",
    "BackupValidationError",
    "RestoreError",
    "RestoreInProgressError",
    "RestoreRecoveryRequiredError",
    "BackupRetentionPolicy",
    "BackupRecord",
    "BackupValidation",
    "BackupManager",
]
