from __future__ import annotations

from contextlib import closing
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from _sistema.app_paths import build_app_paths, ensure_app_directories

from tests.windows_tempdir import robust_temporary_directory

from _sistema.storage import (
    BACKUP_REASON_PERIODIC,
    BACKUP_REASON_PRE_MIGRATION,
    BACKUP_REASON_PRE_RESTORE,
    BACKUP_REASON_PRE_UPDATE,
    BackupError,
    BackupManager,
    BackupRetentionPolicy,
    BackupValidationError,
    LocalDatabase,
    LATEST_SCHEMA_VERSION,
    RestoreInProgressError,
    RestoreRecoveryRequiredError,
)
from _sistema.storage.backup import (
    BACKUP_METHOD_SQLITE_API,
    BACKUP_VALIDATION_STATUS,
    RESTORE_PHASE_NEW_PUBLISHED,
    RESTORE_PHASE_OLD_MOVED,
)
from _sistema.storage.migrations import Migration


class SimulatedProcessDeath(BaseException):
    """BaseException evita o recovery do except Exception e simula queda abrupta."""


def build_local_stack(
    root: Path,
    *,
    retention: BackupRetentionPolicy | None = None,
    restore_lock_timeout_seconds: float = 1.0,
):
    paths = build_app_paths(install_root=root / "install", data_root=root / "data")
    ensure_app_directories(paths)
    db = LocalDatabase(paths=paths)
    db.initialize()
    manager = BackupManager(
        paths=paths,
        database_path=db.path,
        retention=retention,
        restore_lock_timeout_seconds=restore_lock_timeout_seconds,
    )
    return paths, db, manager


def manifest_for(record) -> dict:
    return json.loads((record.path / "manifest.json").read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def assert_integrity_ok(path: Path) -> None:
    with closing(sqlite3.connect(str(path))) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_cria_snapshot_consistente_manifest_completo_e_nao_copia_video_original():
    with robust_temporary_directory() as tmp:
        paths, db, manager = build_local_stack(Path(tmp))
        db.set_setting("backup.test", {"value": 123})
        giant_video = paths.accounts / "youtube" / "Canal" / "videos" / "original.mp4"
        giant_video.parent.mkdir(parents=True, exist_ok=True)
        giant_video.write_bytes(b"video-original" * 1024)

        record = manager.create_backup()
        validation = manager.validate_backup(record.path)

        assert validation.integrity_check == "ok"
        assert {item.name for item in record.path.iterdir()} == {"painel.db", "manifest.json"}
        assert not any(path.suffix.lower() == ".mp4" for path in record.path.rglob("*"))
        manifest = manifest_for(record)
        assert manifest["backup_id"] == record.backup_id
        assert manifest["created_at"].endswith("+00:00") or manifest["created_at"].endswith("Z")
        assert manifest["reason"] == "MANUAL"
        assert manifest["backup_method"] == BACKUP_METHOD_SQLITE_API
        assert manifest["validation_status"] == BACKUP_VALIDATION_STATUS
        assert manifest["database"]["file"] == "painel.db"
        assert manifest["database"]["logical_path"] == "database/painel.db"
        assert manifest["database"]["schema_version"] == LATEST_SCHEMA_VERSION
        assert manifest["database"]["migration_versions"] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        assert manifest["database"]["integrity_check"] == "ok"
        assert manifest["database"]["sha256"] == file_sha256(record.path / "painel.db")
        assert manifest["database"]["size_bytes"] == (record.path / "painel.db").stat().st_size
        assert "original_videos" in manifest["scope"]["excludes"]
        snapshot = LocalDatabase(path=record.path / "painel.db", paths=paths)
        assert snapshot.get_setting("backup.test") == {"value": 123}
        assert giant_video.exists()


def test_manifest_rejeita_metadata_arbitraria_que_poderia_conter_secret():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        with pytest.raises(ValueError, match="metadata não permitida"):
            manager.create_backup(metadata={"token": "secret"})
        with pytest.raises(ValueError, match="metadata não permitida"):
            manager.create_backup(metadata={"cookies": "secret"})
        assert manager.list_backups() == []


def test_backup_periodico_respeita_intervalo_e_nao_duplica_sem_necessidade():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        first = manager.maybe_create_periodic(interval=timedelta(hours=24))
        second = manager.maybe_create_periodic(interval=timedelta(hours=24))
        assert first is not None
        assert first.reason == BACKUP_REASON_PERIODIC
        assert second is None


def test_backup_pre_update_registra_motivo_e_label_allowlisted_no_manifest():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        record = manager.backup_before_update(update_label="2.0.0")
        manifest = manifest_for(record)
        assert record.reason == BACKUP_REASON_PRE_UPDATE
        assert manifest["metadata"] == {"update_label": "2.0.0"}


def test_create_backup_mais_de_25_vezes_nao_apaga_nenhum_automaticamente():
    with robust_temporary_directory() as tmp:
        policy = BackupRetentionPolicy(max_backups=2, max_age_days=1)
        _paths, _db, manager = build_local_stack(Path(tmp), retention=policy)
        records = [manager.create_backup() for _ in range(26)]
        existing = manager.list_backups(validate=True)
        assert len(existing) == 26
        assert {item.backup_id for item in existing} == {item.backup_id for item in records}


def test_prune_backups_so_age_quando_chamado_explicitamente():
    with robust_temporary_directory() as tmp:
        policy = BackupRetentionPolicy(max_backups=2, max_age_days=None)
        _paths, _db, manager = build_local_stack(Path(tmp), retention=policy)
        unknown = manager.backup_root / "nao_e_backup"
        unknown.mkdir(parents=True, exist_ok=True)
        (unknown / "keep.txt").write_text("preservar", encoding="utf-8")

        manager.create_backup()
        manager.create_backup()
        manager.create_backup()
        assert len(manager.list_backups(validate=True)) == 3

        removed = manager.prune_backups()
        assert len(removed) == 1
        assert len(manager.list_backups(validate=True)) == 2
        assert (unknown / "keep.txt").read_text(encoding="utf-8") == "preservar"


def test_validacao_rejeita_manifest_com_hash_adulterado():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        record = manager.create_backup()
        manifest_path = record.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["database"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(BackupValidationError):
            manager.validate_backup(record.path)


def test_integrity_check_bloqueia_backup_corrompido_mesmo_com_manifest_recalculado():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        record = manager.create_backup()
        snapshot = record.path / "painel.db"
        data = bytearray(snapshot.read_bytes())
        data[: min(len(data), 256)] = b"X" * min(len(data), 256)
        snapshot.write_bytes(bytes(data))

        manifest_path = record.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["database"]["size_bytes"] = snapshot.stat().st_size
        manifest["database"]["sha256"] = file_sha256(snapshot)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(BackupValidationError):
            manager.validate_backup(record.path)
        with pytest.raises(BackupValidationError):
            manager.restore_backup(record.path)


def test_restore_normal_cria_pre_restore_persistente_com_estado_imediatamente_anterior():
    with robust_temporary_directory() as tmp:
        paths, db, manager = build_local_stack(Path(tmp))
        db.set_setting("restore.value", "A")
        backup_a = manager.create_backup()
        db.set_setting("restore.value", "B")

        manager.restore_backup(backup_a.path)

        restored = LocalDatabase(path=db.path, paths=paths)
        assert restored.get_setting("restore.value") == "A"
        pre = [item for item in manager.list_backups(validate=True) if item.reason == BACKUP_REASON_PRE_RESTORE]
        assert len(pre) == 1
        safety_db = LocalDatabase(path=pre[0].path / "painel.db", paths=paths)
        assert safety_db.get_setting("restore.value") == "B"
        assert pre[0].path.exists()  # não é apagado após sucesso
        assert_integrity_ok(db.path)


def test_falha_ao_criar_pre_restore_aborta_antes_de_tocar_no_banco(monkeypatch):
    with robust_temporary_directory() as tmp:
        paths, db, manager = build_local_stack(Path(tmp))
        db.set_setting("restore.value", "A")
        backup_a = manager.create_backup()
        db.set_setting("restore.value", "B")
        before_hash = file_sha256(db.path)

        original = manager.create_backup

        def fail_pre_restore(*, reason="MANUAL", metadata=None):
            if reason == BACKUP_REASON_PRE_RESTORE:
                raise BackupError("falha simulada no safety backup")
            return original(reason=reason, metadata=metadata)

        monkeypatch.setattr(manager, "create_backup", fail_pre_restore)
        with pytest.raises(BackupError, match="safety backup"):
            manager.restore_backup(backup_a.path)

        assert db.path.exists()
        assert file_sha256(db.path) == before_hash
        assert LocalDatabase(path=db.path, paths=paths).get_setting("restore.value") == "B"
        assert not manager.restore_state_path.exists()
        assert not manager._legacy_or_restore_artifacts()


def test_restore_invalido_nao_toca_no_banco_operacional_atual():
    with robust_temporary_directory() as tmp:
        paths, db, manager = build_local_stack(Path(tmp))
        db.set_setting("restore.guard", "preservar")
        record = manager.create_backup()
        snapshot = record.path / "painel.db"
        snapshot.write_bytes(b"nao-e-sqlite")
        manifest_path = record.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["database"]["size_bytes"] = snapshot.stat().st_size
        manifest["database"]["sha256"] = file_sha256(snapshot)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(BackupValidationError):
            manager.restore_backup(record.path)
        current = LocalDatabase(path=db.path, paths=paths)
        assert current.get_setting("restore.guard") == "preservar"
        assert not [item for item in manager.list_backups() if item.reason == BACKUP_REASON_PRE_RESTORE]


def test_create_backup_recusa_banco_operacional_corrompido():
    with robust_temporary_directory() as tmp:
        _paths, db, manager = build_local_stack(Path(tmp))
        for suffix in ("-wal", "-shm"):
            Path(str(db.path) + suffix).unlink(missing_ok=True)
        db.path.write_bytes(b"corrompido")
        with pytest.raises(BackupValidationError):
            manager.create_backup()
        assert manager.list_backups() == []


def test_backup_pre_migration_automatico_acontece_antes_do_ddl(monkeypatch):
    import _sistema.storage.backup as backup_module
    import _sistema.storage.database as database_module

    with robust_temporary_directory() as tmp:
        root = Path(tmp)
        paths, db, _manager = build_local_stack(root)
        db.set_setting("migration.marker", "v2")

        # PROMPT 27.5: LATEST_SCHEMA_VERSION real avançou de 8 para 9
        # (m009_video_declarations.py) -- a migration "sonda" simulada
        # aqui precisa ocupar o PRÓXIMO número livre (10), nunca reusar o
        # 9 real, senão colidiria com a migration verdadeira já registrada.
        migration10 = Migration(
            10,
            "backup_probe",
            "CREATE TABLE backup_probe(id INTEGER PRIMARY KEY, value TEXT);",
        )
        future = tuple(database_module.MIGRATIONS) + (migration10,)
        monkeypatch.setattr(database_module, "MIGRATIONS", future)
        monkeypatch.setattr(database_module, "LATEST_SCHEMA_VERSION", 10)
        monkeypatch.setattr(backup_module, "MIGRATIONS", future)
        monkeypatch.setattr(backup_module, "LATEST_SCHEMA_VERSION", 10)

        assert db.initialize() == 10
        manager = BackupManager(paths=paths, database_path=db.path)
        pre = [item for item in manager.list_backups(validate=True) if item.reason == BACKUP_REASON_PRE_MIGRATION]
        assert len(pre) >= 1
        snapshot = pre[0].path / "painel.db"
        with closing(sqlite3.connect(str(snapshot))) as conn:
            # Estado real antes da migration sonda: schema v9 (m001..m009
            # já aplicadas por build_local_stack), não v8 -- reflete o
            # avanço real do LATEST_SCHEMA_VERSION nesta rodada.
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='backup_probe'"
            ).fetchone()[0] == 0
        assert LocalDatabase(path=snapshot, paths=paths).get_setting("migration.marker") == "v2"


def test_staging_incompleto_de_backup_nao_aparece_como_backup_valido():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        manager._ensure_directories()
        crash_stage = manager.staging_root / "crash"
        crash_stage.mkdir()
        (crash_stage / "painel.db").write_bytes(b"incompleto")
        assert manager.list_backups() == []


def test_crash_apos_old_moved_startup_faz_rollback_e_nunca_cria_banco_vazio(monkeypatch):
    with robust_temporary_directory() as tmp:
        paths, db, manager = build_local_stack(Path(tmp))
        db.set_setting("restore.value", "A")
        backup_a = manager.create_backup()
        db.set_setting("restore.value", "B")

        def crash(phase, _journal):
            if phase == RESTORE_PHASE_OLD_MOVED:
                raise SimulatedProcessDeath("queda após OLD_MOVED")

        monkeypatch.setattr(manager, "_after_restore_phase", crash)
        with pytest.raises(SimulatedProcessDeath):
            manager.restore_backup(backup_a.path)

        assert not db.path.exists()
        assert manager.restore_state_path.exists()
        assert any("restore_previous" in p.name for p in manager._legacy_or_restore_artifacts())

        # initialize precisa recuperar antes de qualquer connect/criação de DB.
        restarted = LocalDatabase(path=db.path, paths=paths)
        assert restarted.initialize() == LATEST_SCHEMA_VERSION
        assert restarted.get_setting("restore.value") == "B"
        assert_integrity_ok(db.path)
        assert not manager.restore_state_path.exists()
        assert not manager._legacy_or_restore_artifacts()


def test_crash_apos_new_published_startup_valida_e_conclui_restore(monkeypatch):
    with robust_temporary_directory() as tmp:
        paths, db, manager = build_local_stack(Path(tmp))
        db.set_setting("restore.value", "A")
        backup_a = manager.create_backup()
        db.set_setting("restore.value", "B")

        def crash(phase, _journal):
            if phase == RESTORE_PHASE_NEW_PUBLISHED:
                raise SimulatedProcessDeath("queda após NEW_PUBLISHED")

        monkeypatch.setattr(manager, "_after_restore_phase", crash)
        with pytest.raises(SimulatedProcessDeath):
            manager.restore_backup(backup_a.path)

        assert db.path.exists()
        assert manager.restore_state_path.exists()
        restarted = LocalDatabase(path=db.path, paths=paths)
        assert restarted.initialize() == LATEST_SCHEMA_VERSION
        assert restarted.get_setting("restore.value") == "A"
        assert_integrity_ok(db.path)
        assert not manager.restore_state_path.exists()
        assert not manager._legacy_or_restore_artifacts()


def test_artefato_restore_sem_journal_exige_recovery_e_nada_e_apagado():
    with robust_temporary_directory() as tmp:
        paths, db, manager = build_local_stack(Path(tmp))
        db.set_setting("restore.value", "B")
        manager._checkpoint_target_for_restore()
        orphan = db.path.with_name(db.path.name + ".restore_previous.orphan")
        expected_bytes = db.path.read_bytes()
        db.path.replace(orphan)
        assert not db.path.exists()

        restarted = LocalDatabase(path=db.path, paths=paths)
        with pytest.raises(RestoreRecoveryRequiredError, match="sem journal"):
            restarted.initialize()

        assert not db.path.exists()
        assert orphan.exists()
        assert orphan.read_bytes() == expected_bytes
        assert_integrity_ok(orphan)


def test_restore_sucesso_nao_deixa_tmp_previous_journal_nem_sidecars_temporarios():
    with robust_temporary_directory() as tmp:
        _paths, db, manager = build_local_stack(Path(tmp))
        db.set_setting("restore.value", "A")
        record = manager.create_backup()
        db.set_setting("restore.value", "B")
        manager.restore_backup(record.path)

        assert not manager.restore_state_path.exists()
        assert not manager._legacy_or_restore_artifacts()
        assert not list(db.path.parent.glob("*.restore_tmp.*-wal"))
        assert not list(db.path.parent.glob("*.restore_tmp.*-shm"))
        assert not list(db.path.parent.glob("*.restore_previous.*-wal"))
        assert not list(db.path.parent.glob("*.restore_previous.*-shm"))


def test_dois_restores_simultaneos_so_um_entra_na_operacao_destrutiva(monkeypatch):
    with robust_temporary_directory() as tmp:
        root = Path(tmp)
        paths, db, manager1 = build_local_stack(root, restore_lock_timeout_seconds=1.0)
        manager2 = BackupManager(
            paths=paths,
            database_path=db.path,
            restore_lock_timeout_seconds=0.10,
        )
        db.set_setting("restore.value", "A")
        backup_a = manager1.create_backup()
        db.set_setting("restore.value", "B")

        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def hold_after_prepared(phase, _journal):
            if phase == "PREPARED":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("timeout do teste")

        monkeypatch.setattr(manager1, "_after_restore_phase", hold_after_prepared)

        def first_restore():
            try:
                manager1.restore_backup(backup_a.path)
            except BaseException as exc:  # pragma: no cover - diagnóstico do teste
                errors.append(exc)

        thread = threading.Thread(target=first_restore)
        thread.start()
        assert entered.wait(5)
        try:
            with pytest.raises(RestoreInProgressError):
                manager2.restore_backup(backup_a.path)
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
        assert errors == []
        assert LocalDatabase(path=db.path, paths=paths).get_setting("restore.value") == "A"
        assert_integrity_ok(db.path)


def test_lock_restore_bloqueia_subprocesso_independente(monkeypatch):
    with robust_temporary_directory() as tmp:
        root = Path(tmp)
        paths, db, manager = build_local_stack(root, restore_lock_timeout_seconds=1.0)
        db.set_setting("restore.value", "A")
        backup_a = manager.create_backup()
        db.set_setting("restore.value", "B")

        entered = threading.Event()
        release = threading.Event()
        parent_errors: list[BaseException] = []

        def hold_after_prepared(phase, _journal):
            if phase == "PREPARED":
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("timeout do teste")

        monkeypatch.setattr(manager, "_after_restore_phase", hold_after_prepared)

        def parent_restore():
            try:
                manager.restore_backup(backup_a.path)
            except BaseException as exc:  # pragma: no cover - diagnóstico
                parent_errors.append(exc)

        thread = threading.Thread(target=parent_restore)
        thread.start()
        assert entered.wait(5)
        child_code = "\n".join([
            "from pathlib import Path",
            "from _sistema.app_paths import build_app_paths",
            "from _sistema.storage import BackupManager, RestoreInProgressError",
            f"paths = build_app_paths(install_root=Path({str(paths.install_root)!r}), data_root=Path({str(paths.data_root)!r}))",
            f"manager = BackupManager(paths=paths, database_path=Path({str(db.path)!r}), restore_lock_timeout_seconds=0.15)",
            "try:",
            f"    manager.restore_backup(Path({str(backup_a.path)!r}))",
            "except RestoreInProgressError:",
            "    raise SystemExit(0)",
            "raise SystemExit(7)",
        ])
        try:
            completed = subprocess.run(
                [sys.executable, "-c", child_code],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert completed.returncode == 0, completed.stderr or completed.stdout
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
        assert parent_errors == []
        assert_integrity_ok(db.path)


def test_pre_restore_e_demais_gatilhos_nunca_apagam_backups_anteriores():
    with robust_temporary_directory() as tmp:
        _paths, db, manager = build_local_stack(
            Path(tmp), retention=BackupRetentionPolicy(max_backups=1, max_age_days=1)
        )
        manual = manager.create_backup()
        periodic = manager.maybe_create_periodic(interval=timedelta(seconds=1))
        update = manager.backup_before_update(update_label="x")
        db.set_setting("restore.value", "B")
        manager.restore_backup(manual.path)
        ids = {record.backup_id for record in manager.list_backups(validate=True)}
        assert manual.backup_id in ids
        assert periodic is not None and periodic.backup_id in ids
        assert update.backup_id in ids
        assert any(item.reason == BACKUP_REASON_PRE_RESTORE for item in manager.list_backups())
        assert len(ids) >= 4


def _install_future_migration_v10(monkeypatch):
    """Simula um binário futuro com schema 10 sem criar migration real no
    projeto. PROMPT 27.5: LATEST_SCHEMA_VERSION real avançou de 8 para 9
    (m009_video_declarations.py) -- a "sonda" de schema futuro usada por
    estes testes de restore/crash precisa ocupar o PRÓXIMO número livre
    real (10), nunca reusar o 9, que agora é uma migration real e
    congelada."""
    import _sistema.storage.backup as backup_module
    import _sistema.storage.database as database_module

    migration10 = Migration(
        10,
        "future_restore_probe",
        """
        CREATE TABLE future_restore_probe (
            id INTEGER PRIMARY KEY,
            value TEXT
        );
        """,
    )
    future = tuple(database_module.MIGRATIONS) + (migration10,)
    monkeypatch.setattr(database_module, "MIGRATIONS", future)
    monkeypatch.setattr(database_module, "LATEST_SCHEMA_VERSION", 10)
    monkeypatch.setattr(backup_module, "MIGRATIONS", future)
    monkeypatch.setattr(backup_module, "LATEST_SCHEMA_VERSION", 10)
    return future


def _schema_version(path: Path) -> int:
    with closing(sqlite3.connect(str(path))) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def test_restore_source_schema9_previous_schema10_sem_crash_e_initialize_sobe_para_schema10(monkeypatch):
    with robust_temporary_directory() as tmp:
        root = Path(tmp)
        paths, db, manager = build_local_stack(root)
        db.set_setting("schema.restore.marker", "SOURCE_V9")
        source_v9 = manager.create_backup()
        assert source_v9.schema_version == 9

        _install_future_migration_v10(monkeypatch)
        assert db.initialize() == 10
        db.set_setting("schema.restore.marker", "PREVIOUS_V10")
        assert _schema_version(db.path) == 10

        restored = manager.restore_backup(source_v9.path)
        assert restored.schema_version == 9
        assert _schema_version(db.path) == 9
        assert LocalDatabase(path=db.path, paths=paths).get_setting("schema.restore.marker") == "SOURCE_V9"
        assert_integrity_ok(db.path)

        # Elevação de schema após restore pertence ao initialize, não ao restore.
        restarted = LocalDatabase(path=db.path, paths=paths)
        assert restarted.initialize() == 10
        assert _schema_version(db.path) == 10
        assert restarted.get_setting("schema.restore.marker") == "SOURCE_V9"
        with closing(sqlite3.connect(str(db.path))) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='future_restore_probe'"
            ).fetchone()[0] == 1


def test_crash_old_moved_source_schema9_previous_schema10_recupera_previous_schema10(monkeypatch):
    with robust_temporary_directory() as tmp:
        root = Path(tmp)
        paths, db, manager = build_local_stack(root)
        db.set_setting("schema.restore.marker", "SOURCE_V9")
        source_v9 = manager.create_backup()

        _install_future_migration_v10(monkeypatch)
        assert db.initialize() == 10
        db.set_setting("schema.restore.marker", "PREVIOUS_V10")

        def crash(phase, _journal):
            if phase == RESTORE_PHASE_OLD_MOVED:
                raise SimulatedProcessDeath("queda schema-misto após OLD_MOVED")

        monkeypatch.setattr(manager, "_after_restore_phase", crash)
        with pytest.raises(SimulatedProcessDeath):
            manager.restore_backup(source_v9.path)

        journal = json.loads(manager.restore_state_path.read_text(encoding="utf-8"))
        assert journal["expected_source_schema_version"] == 9
        assert journal["expected_previous_schema_version"] == 10
        assert not db.path.exists()

        monkeypatch.setattr(manager, "_after_restore_phase", lambda *_args: None)
        assert manager.recover_pending_restore() is True
        assert _schema_version(db.path) == 10
        assert LocalDatabase(path=db.path, paths=paths).get_setting("schema.restore.marker") == "PREVIOUS_V10"
        assert_integrity_ok(db.path)
        assert not manager.restore_state_path.exists()


def test_crash_new_published_source_schema9_previous_schema10_mantem_source_e_initialize_migra(monkeypatch):
    with robust_temporary_directory() as tmp:
        root = Path(tmp)
        paths, db, manager = build_local_stack(root)
        db.set_setting("schema.restore.marker", "SOURCE_V9")
        source_v9 = manager.create_backup()

        _install_future_migration_v10(monkeypatch)
        assert db.initialize() == 10
        db.set_setting("schema.restore.marker", "PREVIOUS_V10")

        def crash(phase, _journal):
            if phase == RESTORE_PHASE_NEW_PUBLISHED:
                raise SimulatedProcessDeath("queda schema-misto após NEW_PUBLISHED")

        monkeypatch.setattr(manager, "_after_restore_phase", crash)
        with pytest.raises(SimulatedProcessDeath):
            manager.restore_backup(source_v9.path)

        journal = json.loads(manager.restore_state_path.read_text(encoding="utf-8"))
        assert journal["expected_source_schema_version"] == 9
        assert journal["expected_previous_schema_version"] == 10
        assert _schema_version(db.path) == 9

        monkeypatch.setattr(manager, "_after_restore_phase", lambda *_args: None)
        assert manager.recover_pending_restore() is True
        assert _schema_version(db.path) == 9
        assert LocalDatabase(path=db.path, paths=paths).get_setting("schema.restore.marker") == "SOURCE_V9"
        assert_integrity_ok(db.path)
        assert not manager.restore_state_path.exists()

        restarted = LocalDatabase(path=db.path, paths=paths)
        assert restarted.initialize() == 10
        assert _schema_version(db.path) == 10
        assert restarted.get_setting("schema.restore.marker") == "SOURCE_V9"
        assert_integrity_ok(db.path)


def test_periodic_recente_com_snapshot_corrompido_nao_bloqueia_novo_backup_valido():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        invalid = manager.maybe_create_periodic(interval=timedelta(hours=24))
        assert invalid is not None
        snapshot = invalid.path / "painel.db"
        snapshot.write_bytes(b"sqlite-corrompido")
        # Recalcula size/hash no manifest para provar que a validação completa
        # (SQLite/integrity_check), e não só checksum, invalida a proteção.
        manifest_path = invalid.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["database"]["size_bytes"] = snapshot.stat().st_size
        manifest["database"]["sha256"] = file_sha256(snapshot)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        replacement = manager.maybe_create_periodic(interval=timedelta(hours=24))
        assert replacement is not None
        assert replacement.backup_id != invalid.backup_id
        assert manager.validate_backup(replacement.path).integrity_check == "ok"
        assert invalid.path.exists()  # inválido é preservado para diagnóstico
        assert invalid.backup_id not in {item.backup_id for item in manager.list_backups()}


def test_periodic_recente_com_hash_errado_nao_bloqueia_novo_backup_valido():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        invalid = manager.maybe_create_periodic(interval=timedelta(hours=24))
        assert invalid is not None
        manifest_path = invalid.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["database"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        replacement = manager.maybe_create_periodic(interval=timedelta(hours=24))
        assert replacement is not None
        assert replacement.backup_id != invalid.backup_id
        assert manager.validate_backup(replacement.path).integrity_check == "ok"
        assert invalid.path.exists()
        assert invalid.backup_id not in {item.backup_id for item in manager.list_backups()}


def test_periodic_recente_sem_snapshot_nao_bloqueia_novo_backup_valido_e_e_preservado():
    with robust_temporary_directory() as tmp:
        _paths, _db, manager = build_local_stack(Path(tmp))
        invalid = manager.maybe_create_periodic(interval=timedelta(hours=24))
        assert invalid is not None
        (invalid.path / "painel.db").unlink()

        replacement = manager.maybe_create_periodic(interval=timedelta(hours=24))
        assert replacement is not None
        assert replacement.backup_id != invalid.backup_id
        assert manager.validate_backup(replacement.path).integrity_check == "ok"
        assert invalid.path.exists()
        assert (invalid.path / "manifest.json").exists()
        assert invalid.backup_id not in {item.backup_id for item in manager.list_backups()}
