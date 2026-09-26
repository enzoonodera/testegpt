# -*- coding: utf-8 -*-
"""Migração controlada dos estados JSON legados para o SQLite local.

A migração é deliberadamente separada dos engines: ela lê somente uma allowlist
conhecida de JSONs operacionais, cria um backup byte-a-byte antes de qualquer
write no banco e então importa tudo dentro de uma única transaction SQLite.
Os arquivos legados originais nunca são removidos ou modificados aqui.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from ..app_paths import AppPaths, PATHS, account_paths, ensure_app_directories
from ..domain import (
    Account,
    Artifact,
    ErrorRecord,
    Job,
    Publication,
    Schedule,
    SourceAsset,
    Video,
    SCHEDULE_UNKNOWN,
)
from ..domain.job_state_machine import JOB_FAILED, JOB_PENDING, JOB_READY
from ..time_utils import SCHEDULE_TIME_MANUAL, utc_now_iso
from .database import LocalDatabase
from .backup import _InterProcessRestoreLock, RestoreInProgressError


LEGACY_MIGRATION_VERSION = 1
LEGACY_MIGRATION_NAME = "legacy_json_state_to_sqlite_v1"
LEGACY_BACKUP_DIRNAME = "legacy_json_migration"
LEGACY_REPORT_FILENAME = "legacy_json_to_sqlite_v1_report.json"
LEGACY_STATE_SETTING = "legacy_migration/v1/state"
LEGACY_RAW_SETTING_PREFIX = "legacy_json/v1/"
LEGACY_MIGRATION_LOCK_FILENAME = "legacy_json_migration.lock"
DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS = 30.0

# Apenas estado operacional conhecido. JSONs de perfil de navegador nunca são
# descobertos por glob genérico. Transcrições são cache/conteúdo e permanecem
# fora desta migration de estado; os originais também nunca são apagados.
KNOWN_STATE_FILES = (
    "config_canal.json",
    "dados/limpeza_estado.json",
    "dados/textos_postagem.json",
    "dados/estado_youtube.json",
    "dados/estado_tiktok.json",
)


class LegacyJsonMigrationError(RuntimeError):
    """Erro base da migration de estados JSON legados."""


class LegacyJsonValidationError(LegacyJsonMigrationError):
    """Um JSON legado conhecido não pôde ser validado de forma estrita."""


class LegacyJsonBackupError(LegacyJsonMigrationError):
    """O snapshot de segurança dos JSONs não pôde ser criado/validado."""


class LegacyJsonChangedAfterMigrationError(LegacyJsonMigrationError):
    """Os JSONs mudaram após uma migration concluída; não mesclar silenciosamente."""


class LegacyJsonMigrationInProgressError(LegacyJsonMigrationError):
    """Outra instância/processo está executando a migration legada."""


@dataclass(frozen=True)
class LegacyJsonSnapshot:
    relative_path: str
    source_path: Path
    backup_path: Path
    sha256: str
    size_bytes: int
    modified_at_utc: str
    payload: Any


@dataclass(frozen=True)
class LegacyMigrationReport:
    migration: str
    migration_version: int
    status: str
    started_at: str
    completed_at: str
    source_manifest_sha256: str
    backup_path: str
    files_discovered: int
    files_backed_up: int
    raw_records_imported: int
    accounts_imported: int
    cleanup_items_imported: int
    publications_imported: int
    schedules_imported: int
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["warnings"] = list(self.warnings)
        return data


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    with tmp.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _stable_uuid(*parts: object) -> str:
    key = "|".join(str(part) for part in parts)
    return str(uuid5(NAMESPACE_URL, f"painel-oficial:{LEGACY_MIGRATION_NAME}:{key}"))


def _file_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _account_roots(paths: AppPaths) -> list[Path]:
    if not paths.accounts.exists():
        return []
    roots: set[Path] = set()
    for config in paths.accounts.glob("*/*/config_canal.json"):
        if ".migration_staging" in config.parts:
            continue
        roots.add(config.parent.resolve())
    # Algumas contas legadas podem ter estado mas config ausente. Elas ainda
    # precisam ter seus JSONs preservados/migrados sem inventar configuração.
    for platform in ("youtube", "tiktok"):
        base = paths.accounts / platform
        if not base.exists():
            continue
        for candidate in base.iterdir():
            if candidate.is_dir() and not candidate.name.startswith("."):
                roots.add(candidate.resolve())
    return sorted(roots, key=lambda item: str(item).casefold())


def discover_legacy_state_files(paths: AppPaths | None = None) -> tuple[Path, ...]:
    """Descobre somente JSONs operacionais conhecidos; nunca perfis Chrome."""
    paths = paths or PATHS
    discovered: list[Path] = []
    for root in _account_roots(paths):
        for relative in KNOWN_STATE_FILES:
            candidate = root / relative
            if candidate.is_file():
                discovered.append(candidate.resolve())
    return tuple(sorted(set(discovered), key=lambda item: str(item).casefold()))


def _relative_to_accounts(path: Path, paths: AppPaths) -> str:
    try:
        return path.resolve().relative_to(paths.accounts.resolve()).as_posix()
    except ValueError as exc:
        raise LegacyJsonValidationError(f"arquivo legado fora de accounts/: {path}") from exc


def _manifest_digest(entries: Iterable[tuple[str, str]]) -> str:
    canonical = json.dumps(sorted(entries), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical)


def _strict_json_bytes(data: bytes, *, label: str) -> Any:
    try:
        text = data.decode("utf-8-sig")
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyJsonValidationError(f"JSON legado inválido: {label}: {exc}") from exc


def _backup_manifest_path(backup_dir: Path) -> Path:
    return backup_dir / "manifest.json"


def _validate_existing_backup(backup_dir: Path, expected_files: Mapping[str, str], digest: str) -> None:
    manifest_path = _backup_manifest_path(backup_dir)
    if not manifest_path.is_file():
        raise LegacyJsonBackupError(f"backup legado existente sem manifest: {backup_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LegacyJsonBackupError(f"manifest do backup legado inválido: {backup_dir}") from exc
    if manifest.get("source_manifest_sha256") != digest:
        raise LegacyJsonBackupError("backup legado existente possui digest diferente")
    listed = manifest.get("files")
    if not isinstance(listed, list) or len(listed) != len(expected_files):
        raise LegacyJsonBackupError("backup legado existente possui contagem divergente")
    for relative, expected_sha in expected_files.items():
        target = backup_dir / "accounts" / Path(relative)
        if not target.is_file() or _sha256_file(target) != expected_sha:
            raise LegacyJsonBackupError(f"backup legado inválido para {relative}")


def create_legacy_json_backup(paths: AppPaths | None = None) -> tuple[Path, tuple[LegacyJsonSnapshot, ...], str]:
    """Copia os JSONs conhecidos para snapshot local antes de qualquer write SQLite."""
    paths = paths or PATHS
    ensure_app_directories(paths)
    sources = discover_legacy_state_files(paths)

    captured: list[tuple[Path, str, bytes, str, str]] = []
    for source in sources:
        relative = _relative_to_accounts(source, paths)
        data = source.read_bytes()
        sha = _sha256_bytes(data)
        modified = _file_timestamp(source)
        captured.append((source, relative, data, sha, modified))

    digest = _manifest_digest((relative, sha) for _, relative, _, sha, _ in captured)
    backup_root = paths.backups / LEGACY_BACKUP_DIRNAME
    backup_dir = backup_root / digest
    expected = {relative: sha for _, relative, _, sha, _ in captured}

    if backup_dir.exists():
        _validate_existing_backup(backup_dir, expected, digest)
    else:
        staging = backup_root / ".staging" / digest
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=False)
        try:
            manifest_files: list[dict[str, Any]] = []
            for source, relative, data, sha, modified in captured:
                target = staging / "accounts" / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if _sha256_file(target) != sha:
                    raise LegacyJsonBackupError(f"checksum divergiu ao copiar {relative}")
                # Detecta alteração concorrente do arquivo original durante o snapshot.
                if _sha256_file(source) != sha:
                    raise LegacyJsonBackupError(f"arquivo legado mudou durante backup: {relative}")
                manifest_files.append({
                    "relative_path": relative,
                    "sha256": sha,
                    "size_bytes": len(data),
                    "modified_at_utc": modified,
                })
            manifest = {
                "format_version": 1,
                "migration": LEGACY_MIGRATION_NAME,
                "migration_version": LEGACY_MIGRATION_VERSION,
                "created_at": utc_now_iso(),
                "source_manifest_sha256": digest,
                "files": manifest_files,
                "scope": "KNOWN_OPERATIONAL_JSON_ONLY",
                "originals_modified": False,
                "videos_copied": False,
            }
            _atomic_write_json(_backup_manifest_path(staging), manifest)
            backup_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, backup_dir)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    snapshots: list[LegacyJsonSnapshot] = []
    for source, relative, _, sha, modified in captured:
        backed = backup_dir / "accounts" / Path(relative)
        raw = backed.read_bytes()
        if _sha256_bytes(raw) != sha:
            raise LegacyJsonBackupError(f"checksum inválido no snapshot de {relative}")
        payload = _strict_json_bytes(raw, label=relative)
        snapshots.append(LegacyJsonSnapshot(
            relative_path=relative,
            source_path=source,
            backup_path=backed,
            sha256=sha,
            size_bytes=len(raw),
            modified_at_utc=modified,
            payload=payload,
        ))
    return backup_dir, tuple(snapshots), digest


def _account_info(snapshot: LegacyJsonSnapshot, paths: AppPaths) -> tuple[str, str, Path]:
    rel = Path(snapshot.relative_path)
    if len(rel.parts) < 3:
        raise LegacyJsonValidationError(f"caminho de conta inesperado: {snapshot.relative_path}")
    platform = rel.parts[0]
    account_name = rel.parts[1]
    root = (paths.accounts / platform / account_name).resolve()
    return platform, account_name, root


def _snapshot_map(snapshots: tuple[LegacyJsonSnapshot, ...]) -> dict[str, LegacyJsonSnapshot]:
    return {item.relative_path: item for item in snapshots}


def _group_accounts(snapshots: tuple[LegacyJsonSnapshot, ...], paths: AppPaths) -> dict[tuple[str, str], dict[str, LegacyJsonSnapshot]]:
    grouped: dict[tuple[str, str], dict[str, LegacyJsonSnapshot]] = {}
    for snapshot in snapshots:
        platform, account_name, _ = _account_info(snapshot, paths)
        root_prefix = f"{platform}/{account_name}/"
        logical = snapshot.relative_path[len(root_prefix):]
        grouped.setdefault((platform, account_name), {})[logical] = snapshot
    return grouped


def _setting_key(snapshot: LegacyJsonSnapshot) -> str:
    return LEGACY_RAW_SETTING_PREFIX + snapshot.relative_path


def _legacy_envelope(snapshot: LegacyJsonSnapshot) -> dict[str, Any]:
    return {
        "migration_version": LEGACY_MIGRATION_VERSION,
        "relative_path": snapshot.relative_path,
        "sha256": snapshot.sha256,
        "size_bytes": snapshot.size_bytes,
        "modified_at_utc": snapshot.modified_at_utc,
        "payload": snapshot.payload,
    }


def _account_entity(platform: str, name: str, files: Mapping[str, LegacyJsonSnapshot]) -> Account:
    config_snapshot = files.get("config_canal.json")
    config = config_snapshot.payload if config_snapshot and isinstance(config_snapshot.payload, Mapping) else {}
    timestamp = config_snapshot.modified_at_utc if config_snapshot else utc_now_iso()
    account_id = _stable_uuid("account", platform, name)
    return Account(
        id=account_id,
        created_at=timestamp,
        updated_at=timestamp,
        platform=str(config.get("plataforma") or platform),
        name=str(config.get("nome_conta") or config.get("nome_canal") or name),
        local_key=f"{platform}/{name}",
        enabled=True,
        extra={
            "legacy_migration": LEGACY_MIGRATION_NAME,
            "legacy_config": dict(config),
        },
    )


def _ensure_video_entities(
    db: LocalDatabase,
    conn,
    *,
    platform: str,
    account_name: str,
    account_root: Path,
    fingerprint: str | None,
    file_name: str | None,
    source_path: str | None,
    size_bytes: int | None,
    timestamp: str,
    source_is_original: bool = False,
) -> tuple[SourceAsset, Video]:
    """Cria/reusa SourceAsset+Video sem degradar informação de origem.

    Estados de publicação normalmente conhecem apenas o nome do arquivo derivado
    em ``videos/``. Eles nunca podem substituir path/nome/tamanho de origem já
    obtidos do estado de limpeza. Informação explícita de origem pode preencher
    campos ausentes ou elevar uma entidade previamente criada só com dado derivado.
    """
    identity = str(fingerprint or file_name or source_path or "unknown")
    source_id = _stable_uuid("source", platform, account_name, identity)
    video_id = _stable_uuid("video", platform, account_name, identity)
    derived_path = str(account_root / "videos" / file_name) if file_name else None
    incoming_path = source_path or derived_path

    existing_source = db.get(SourceAsset, source_id, connection=conn)
    if existing_source is None:
        extra = {
            "legacy_migration": LEGACY_MIGRATION_NAME,
            "legacy_source_confidence": "ORIGINAL" if source_is_original else "DERIVED_REFERENCE",
        }
        source = SourceAsset(
            id=source_id,
            created_at=timestamp,
            updated_at=timestamp,
            source_uri=incoming_path or "",
            local_path=incoming_path,
            original_name=file_name,
            fingerprint=fingerprint,
            size_bytes=size_bytes,
            extra=extra,
        )
    else:
        source = existing_source
        confidence = str(source.extra.get("legacy_source_confidence") or "")
        if source_is_original:
            # Origem explícita é mais forte que referência derivada. Nunca faça o
            # inverso quando publication/output chegar depois.
            if source_path and (not source.local_path or confidence != "ORIGINAL"):
                source.local_path = source_path
                source.source_uri = source_path
            if file_name and (not source.original_name or confidence != "ORIGINAL"):
                source.original_name = file_name
            if size_bytes is not None and source.size_bytes is None:
                source.size_bytes = size_bytes
            source.extra["legacy_source_confidence"] = "ORIGINAL"
        else:
            # Dado derivado só preenche lacunas; nunca degrada origem existente.
            if not source.local_path and incoming_path:
                source.local_path = incoming_path
            if not source.source_uri and incoming_path:
                source.source_uri = incoming_path
            if not source.original_name and file_name:
                source.original_name = file_name
            if source.size_bytes is None and size_bytes is not None:
                source.size_bytes = size_bytes
            source.extra.setdefault("legacy_source_confidence", "DERIVED_REFERENCE")
        if not source.fingerprint and fingerprint:
            source.fingerprint = fingerprint
        source.updated_at = max(str(source.updated_at), str(timestamp))

    existing_video = db.get(Video, video_id, connection=conn)
    if existing_video is None:
        video = Video(
            id=video_id,
            created_at=timestamp,
            updated_at=timestamp,
            source_asset_id=source.id,
            name=file_name or identity,
            status="ACTIVE",
            extra={"legacy_migration": LEGACY_MIGRATION_NAME},
        )
    else:
        video = existing_video
        if not video.source_asset_id:
            video.source_asset_id = source.id
        if not video.name and file_name:
            video.name = file_name
        video.updated_at = max(str(video.updated_at), str(timestamp))

    db.save(source, connection=conn)
    db.save(video, connection=conn)
    return source, video


def _import_cleanup(
    db: LocalDatabase,
    conn,
    snapshot: LegacyJsonSnapshot,
    *,
    platform: str,
    account_name: str,
    account_root: Path,
    planned_ids: dict[str, set[str]],
    warnings: list[str],
) -> int:
    payload = snapshot.payload
    if not isinstance(payload, Mapping):
        raise LegacyJsonValidationError(f"limpeza_estado deve ser objeto: {snapshot.relative_path}")
    items = payload.get("items", {})
    if not isinstance(items, Mapping):
        raise LegacyJsonValidationError(f"limpeza_estado.items deve ser objeto: {snapshot.relative_path}")
    imported = 0
    for map_key, raw in items.items():
        if not isinstance(raw, Mapping):
            raise LegacyJsonValidationError(f"item de limpeza inválido: {snapshot.relative_path}:{map_key}")
        fingerprint = str(raw.get("fingerprint") or map_key or "").strip() or None
        file_name = str(raw.get("source_name") or "").strip() or None
        source_path = str(raw.get("source_path") or "").strip() or None
        try:
            size_bytes = int(raw["source_size"]) if raw.get("source_size") is not None else None
        except (TypeError, ValueError) as exc:
            raise LegacyJsonValidationError(f"source_size inválido em {map_key}") from exc
        timestamp = str(raw.get("reserved_at") or raw.get("last_seen_at") or snapshot.modified_at_utc)
        source, video = _ensure_video_entities(
            db, conn,
            platform=platform,
            account_name=account_name,
            account_root=account_root,
            fingerprint=fingerprint,
            file_name=file_name,
            source_path=source_path,
            size_bytes=size_bytes,
            timestamp=timestamp,
            source_is_original=True,
        )
        planned_ids["sources"].add(source.id)
        planned_ids["videos"].add(video.id)

        legacy_status = str(raw.get("status") or "pending").lower()
        output = str(raw.get("output") or "").strip()
        artifact_path = account_root / "videos" / output if output else None
        output_is_valid = bool(
            legacy_status == "done"
            and artifact_path is not None
            and artifact_path.is_file()
            and artifact_path.stat().st_size > 0
        )
        if legacy_status == "done":
            job_status = JOB_READY if output_is_valid else JOB_PENDING
        elif legacy_status == "failed":
            job_status = JOB_FAILED
        else:
            job_status = JOB_PENDING

        job_id = _stable_uuid("job", "cleanup", platform, account_name, fingerprint or map_key)
        job = Job(
            id=job_id,
            created_at=timestamp,
            updated_at=str(raw.get("completed_at") or raw.get("last_attempt_at") or timestamp),
            video_id=video.id,
            operation="LEGACY_CLEANUP",
            status=job_status,
            progress=1.0 if output_is_valid else 0.0,
            extra={"legacy_state": dict(raw), "legacy_migration": LEGACY_MIGRATION_NAME},
        )
        db.save(job, connection=conn)
        planned_ids["jobs"].add(job.id)

        if output_is_valid and artifact_path is not None:
            artifact_id = _stable_uuid("artifact", "cleanup", platform, account_name, fingerprint or map_key, output)
            artifact = Artifact(
                id=artifact_id,
                created_at=timestamp,
                updated_at=str(raw.get("completed_at") or timestamp),
                video_id=video.id,
                job_id=job.id,
                kind="LEGACY_CLEANED_VIDEO",
                path=str(artifact_path),
                fingerprint=fingerprint,
                size_bytes=artifact_path.stat().st_size,
                extra={"legacy_migration": LEGACY_MIGRATION_NAME},
            )
            db.save(artifact, connection=conn)
            planned_ids["artifacts"].add(artifact.id)
            job.output_artifact_ids = [artifact.id]
            db.save(job, connection=conn)
        elif legacy_status == "done":
            inconsistency = (
                f"{snapshot.relative_path}:{map_key}: status done sem output válido "
                f"({output or 'output ausente'}); importado como PENDING sem Artifact"
            )
            warnings.append(inconsistency)
            error_id = _stable_uuid("error", "cleanup-output", platform, account_name, fingerprint or map_key, output or "missing")
            error = ErrorRecord(
                id=error_id,
                created_at=str(raw.get("completed_at") or timestamp),
                updated_at=str(raw.get("completed_at") or timestamp),
                code="LEGACY_OUTPUT_MISSING_OR_EMPTY",
                message=inconsistency,
                entity_type="Video",
                entity_id=video.id,
                job_id=job.id,
                recoverable=True,
                details={"legacy_state": dict(raw), "reserved_output": output or None},
                extra={"legacy_migration": LEGACY_MIGRATION_NAME},
            )
            db.save(error, connection=conn)
            planned_ids["errors"].add(error.id)

        last_error = str(raw.get("last_error") or "").strip()
        if last_error:
            error_id = _stable_uuid("error", "cleanup", platform, account_name, fingerprint or map_key, last_error)
            error = ErrorRecord(
                id=error_id,
                created_at=str(raw.get("last_attempt_at") or timestamp),
                updated_at=str(raw.get("last_attempt_at") or timestamp),
                code="LEGACY_CLEANUP_ERROR",
                message=last_error,
                entity_type="Video",
                entity_id=video.id,
                job_id=job.id,
                recoverable=legacy_status != "done",
                details={"legacy_state": dict(raw)},
                extra={"legacy_migration": LEGACY_MIGRATION_NAME},
            )
            db.save(error, connection=conn)
            planned_ids["errors"].add(error.id)
        imported += 1
    return imported


def _import_publication_state(
    db: LocalDatabase,
    conn,
    snapshot: LegacyJsonSnapshot,
    *,
    platform: str,
    account_name: str,
    account_root: Path,
    account_id: str,
    config: Mapping[str, Any],
    planned_ids: dict[str, set[str]],
    warnings: list[str],
) -> tuple[int, int]:
    payload = snapshot.payload
    if not isinstance(payload, Mapping):
        raise LegacyJsonValidationError(f"estado de publicação deve ser objeto: {snapshot.relative_path}")
    scheduled = payload.get("scheduled", [])
    if not isinstance(scheduled, list):
        raise LegacyJsonValidationError(f"scheduled deve ser lista: {snapshot.relative_path}")
    publication_count = 0
    schedule_count = 0
    for index, raw in enumerate(scheduled):
        if not isinstance(raw, Mapping):
            raise LegacyJsonValidationError(f"scheduled[{index}] inválido: {snapshot.relative_path}")
        fingerprint = str(raw.get("fingerprint") or "").strip() or None
        file_name = str(raw.get("file") or "").strip() or None
        identity = fingerprint or file_name or f"index:{index}"
        timestamp = str(raw.get("registered_at") or snapshot.modified_at_utc)
        source, video = _ensure_video_entities(
            db, conn,
            platform=platform,
            account_name=account_name,
            account_root=account_root,
            fingerprint=fingerprint,
            file_name=file_name,
            source_path=None,
            size_bytes=None,
            timestamp=timestamp,
            source_is_original=False,
        )
        planned_ids["sources"].add(source.id)
        planned_ids["videos"].add(video.id)

        raw_hash = _sha256_bytes(json.dumps(dict(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        publication_id = _stable_uuid("publication", platform, account_name, identity, raw_hash, index)
        description = raw.get("description") if platform == "youtube" else raw.get("caption")
        publication = Publication(
            id=publication_id,
            created_at=timestamp,
            updated_at=timestamp,
            video_id=video.id,
            account_id=account_id,
            status="UNKNOWN",
            title=str(raw.get("title")) if raw.get("title") is not None else None,
            description=str(description) if description is not None else None,
            extra={
                "legacy_state": dict(raw),
                "legacy_migration": LEGACY_MIGRATION_NAME,
                "confidence_note": "legacy scheduled entry imported as UNKNOWN pending reconciliation",
            },
        )
        db.save(publication, connection=conn)
        planned_ids["publications"].add(publication.id)
        publication_count += 1

        scheduled_local = raw.get("scheduled_local") or raw.get("datetime")
        scheduled_utc = raw.get("scheduled_utc")
        timezone_iana = raw.get("timezone_iana") or config.get("timezone_iana")
        time_origin = str(raw.get("time_origin") or SCHEDULE_TIME_MANUAL)
        if (scheduled_local is not None or scheduled_utc is not None) and not timezone_iana:
            warnings.append(
                f"{snapshot.relative_path}: scheduled[{index}] sem timezone_iana; Publication importada como UNKNOWN sem Schedule canônico"
            )
            continue
        schedule_id = _stable_uuid("schedule", publication.id)
        try:
            schedule = Schedule(
                id=schedule_id,
                created_at=timestamp,
                updated_at=timestamp,
                publication_id=publication.id,
                scheduled_local=str(scheduled_local) if scheduled_local is not None else None,
                timezone_iana=str(timezone_iana) if timezone_iana is not None else None,
                scheduled_utc=str(scheduled_utc) if scheduled_utc is not None else None,
                time_origin=time_origin,
                delivery_state=SCHEDULE_UNKNOWN,
                extra={"legacy_migration": LEGACY_MIGRATION_NAME},
            )
        except Exception as exc:
            warnings.append(
                f"{snapshot.relative_path}: scheduled[{index}] não pôde ser canonizado ({exc}); Publication preservada como UNKNOWN"
            )
            continue
        db.save(schedule, connection=conn)
        planned_ids["schedules"].add(schedule.id)
        schedule_count += 1
    return publication_count, schedule_count


def _validate_transaction_counts(conn, *, snapshots: tuple[LegacyJsonSnapshot, ...], planned_ids: Mapping[str, set[str]]) -> None:
    raw_count = int(conn.execute(
        "SELECT COUNT(*) FROM settings WHERE key LIKE ?",
        (LEGACY_RAW_SETTING_PREFIX + "%",),
    ).fetchone()[0])
    if raw_count != len(snapshots):
        raise LegacyJsonMigrationError(
            f"validação de contagem falhou: raw settings={raw_count}, esperado {len(snapshots)}"
        )
    for table, ids in planned_ids.items():
        if not ids:
            continue
        placeholders = ",".join("?" for _ in ids)
        count = int(conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE id IN ({placeholders})",
            tuple(sorted(ids)),
        ).fetchone()[0])
        if count != len(ids):
            raise LegacyJsonMigrationError(
                f"validação de contagem falhou para {table}: {count} != {len(ids)}"
            )


def _write_report(paths: AppPaths, report: LegacyMigrationReport) -> None:
    _atomic_write_json(paths.support / LEGACY_REPORT_FILENAME, report.to_dict())


def _validate_noop_state(db: LocalDatabase, snapshots: tuple[LegacyJsonSnapshot, ...], digest: str) -> bool:
    state = db.get_setting(LEGACY_STATE_SETTING)
    if not isinstance(state, Mapping):
        return False
    if state.get("source_manifest_sha256") != digest:
        raise LegacyJsonChangedAfterMigrationError(
            "os JSONs legados mudaram após uma migration concluída; uma nova estratégia incremental precisa ser autorizada"
        )
    files = state.get("files")
    if not isinstance(files, Mapping) or len(files) != len(snapshots):
        raise LegacyJsonMigrationError("estado de migration SQLite não corresponde aos arquivos legados")
    for snapshot in snapshots:
        if files.get(snapshot.relative_path) != snapshot.sha256:
            raise LegacyJsonMigrationError(f"checksum persistido diverge para {snapshot.relative_path}")
        raw = db.get_setting(_setting_key(snapshot))
        if not isinstance(raw, Mapping) or raw.get("sha256") != snapshot.sha256:
            raise LegacyJsonMigrationError(f"snapshot SQLite ausente/divergente para {snapshot.relative_path}")
    return True


def migrate_legacy_json_to_sqlite(
    *,
    paths: AppPaths | None = None,
    database: LocalDatabase | None = None,
    lock_timeout_seconds: float = DEFAULT_MIGRATION_LOCK_TIMEOUT_SECONDS,
) -> LegacyMigrationReport:
    """Migra uma vez os JSONs operacionais conhecidos para SQLite.

    Toda a operação é serializada entre processos. Após adquirir o lock o estado
    SQLite é relido antes da decisão MIGRATED/NOOP. Arquivos idênticos na segunda
    execução não produzem novas writes. Zero arquivos é NO_DATA e não grava marker
    nem audit event, permitindo que dados legados apareçam posteriormente.
    """
    paths = paths or PATHS
    ensure_app_directories(paths)
    db = database or LocalDatabase(paths=paths)
    lock_path = paths.support / LEGACY_MIGRATION_LOCK_FILENAME
    try:
        with _InterProcessRestoreLock(lock_path, timeout_seconds=lock_timeout_seconds):
            db.initialize()
            started_at = utc_now_iso()
            discovered = discover_legacy_state_files(paths)
            empty_digest = _manifest_digest(())
            if not discovered:
                # Se já havia migration concluída, o desaparecimento de todos os
                # JSONs é uma mudança da fonte e deve continuar explícito.
                if db.get_setting(LEGACY_STATE_SETTING) is not None:
                    _validate_noop_state(db, (), empty_digest)
                report = LegacyMigrationReport(
                    migration=LEGACY_MIGRATION_NAME, migration_version=LEGACY_MIGRATION_VERSION,
                    status="NO_DATA", started_at=started_at, completed_at=utc_now_iso(),
                    source_manifest_sha256=empty_digest, backup_path="",
                    files_discovered=0, files_backed_up=0, raw_records_imported=0,
                    accounts_imported=0, cleanup_items_imported=0, publications_imported=0,
                    schedules_imported=0, warnings=(),
                )
                _write_report(paths, report)
                return report

            backup_dir, snapshots, digest = create_legacy_json_backup(paths)
            # Relê o marker APÓS lock + snapshot; outra instância não pode escrever
            # durante esta decisão.
            if _validate_noop_state(db, snapshots, digest):
                report = LegacyMigrationReport(
                    migration=LEGACY_MIGRATION_NAME, migration_version=LEGACY_MIGRATION_VERSION,
                    status="NOOP", started_at=started_at, completed_at=utc_now_iso(),
                    source_manifest_sha256=digest, backup_path=str(backup_dir),
                    files_discovered=len(snapshots), files_backed_up=len(snapshots),
                    raw_records_imported=len(snapshots), accounts_imported=0,
                    cleanup_items_imported=0, publications_imported=0, schedules_imported=0, warnings=(),
                )
                _write_report(paths, report)
                return report

            grouped = _group_accounts(snapshots, paths)
            planned_ids: dict[str, set[str]] = {
                "sources": set(), "videos": set(), "accounts": set(), "jobs": set(),
                "artifacts": set(), "publications": set(), "schedules": set(), "errors": set(),
            }
            warnings: list[str] = []
            cleanup_count = publication_count = schedule_count = account_count = 0

            with db.transaction() as conn:
                for snapshot in snapshots:
                    db.set_setting(_setting_key(snapshot), _legacy_envelope(snapshot), connection=conn)

                for (platform, account_name), files in sorted(grouped.items()):
                    account = _account_entity(platform, account_name, files)
                    db.save(account, connection=conn)
                    planned_ids["accounts"].add(account.id)
                    account_count += 1
                    account_root = (paths.accounts / platform / account_name).resolve()
                    config_snapshot = files.get("config_canal.json")
                    config = config_snapshot.payload if config_snapshot and isinstance(config_snapshot.payload, Mapping) else {}

                    cleanup = files.get("dados/limpeza_estado.json")
                    if cleanup is not None:
                        cleanup_count += _import_cleanup(
                            db, conn, cleanup, platform=platform, account_name=account_name,
                            account_root=account_root, planned_ids=planned_ids, warnings=warnings,
                        )

                    for state_name in ("dados/estado_youtube.json", "dados/estado_tiktok.json"):
                        state = files.get(state_name)
                        if state is None:
                            continue
                        state_platform = "youtube" if state_name.endswith("estado_youtube.json") else "tiktok"
                        p_count, s_count = _import_publication_state(
                            db, conn, state, platform=state_platform, account_name=account_name,
                            account_root=account_root, account_id=account.id, config=config,
                            planned_ids=planned_ids, warnings=warnings,
                        )
                        publication_count += p_count
                        schedule_count += s_count

                _validate_transaction_counts(conn, snapshots=snapshots, planned_ids=planned_ids)
                migration_state = {
                    "migration": LEGACY_MIGRATION_NAME, "migration_version": LEGACY_MIGRATION_VERSION,
                    "source_manifest_sha256": digest,
                    "files": {item.relative_path: item.sha256 for item in snapshots},
                    "counts": {"files": len(snapshots), "accounts": account_count,
                               "cleanup_items": cleanup_count, "publications": publication_count,
                               "schedules": schedule_count},
                    "completed_at": utc_now_iso(), "backup_path": str(backup_dir),
                }
                db.set_setting(LEGACY_STATE_SETTING, migration_state, connection=conn)
                db.append_audit_event(
                    "LEGACY_JSON_MIGRATION_COMPLETED",
                    data={"migration_version": LEGACY_MIGRATION_VERSION,
                          "source_manifest_sha256": digest, "files": len(snapshots),
                          "accounts": account_count, "cleanup_items": cleanup_count,
                          "publications": publication_count, "schedules": schedule_count},
                    event_id=_stable_uuid("audit", digest), connection=conn,
                )

            report = LegacyMigrationReport(
                migration=LEGACY_MIGRATION_NAME, migration_version=LEGACY_MIGRATION_VERSION,
                status="MIGRATED", started_at=started_at, completed_at=utc_now_iso(),
                source_manifest_sha256=digest, backup_path=str(backup_dir),
                files_discovered=len(snapshots), files_backed_up=len(snapshots),
                raw_records_imported=len(snapshots), accounts_imported=account_count,
                cleanup_items_imported=cleanup_count, publications_imported=publication_count,
                schedules_imported=schedule_count, warnings=tuple(warnings),
            )
            _write_report(paths, report)
            return report
    except RestoreInProgressError as exc:
        raise LegacyJsonMigrationInProgressError(str(exc).replace("restore", "migration legada")) from exc


def main() -> int:
    try:
        report = migrate_legacy_json_to_sqlite()
    except Exception as exc:
        print(f"[ERRO] Migration JSON -> SQLite falhou: {exc}")
        return 1
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LEGACY_MIGRATION_VERSION",
    "LEGACY_MIGRATION_NAME",
    "LEGACY_REPORT_FILENAME",
    "LegacyJsonMigrationError",
    "LegacyJsonMigrationInProgressError",
    "LegacyJsonValidationError",
    "LegacyJsonBackupError",
    "LegacyJsonChangedAfterMigrationError",
    "LegacyJsonSnapshot",
    "LegacyMigrationReport",
    "discover_legacy_state_files",
    "create_legacy_json_backup",
    "migrate_legacy_json_to_sqlite",
]
