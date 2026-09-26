from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import threading
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    Account,
    Artifact,
    ErrorRecord,
    Job,
    Project,
    Publication,
    Schedule,
    SourceAsset,
    Template,
    Video,
    SCHEDULE_LOCAL_PENDING,
    SCHEDULE_TIME_MANUAL,
)
from _sistema.storage import (
    LATEST_SCHEMA_VERSION,
    LocalDatabase,
    Migration,
    MigrationIntegrityError,
    UnsupportedSchemaVersionError,
    BackupManager,
    BACKUP_REASON_PRE_MIGRATION,
)
import _sistema.storage.database as database_module
from _sistema.storage.migrations.m001_initial import MIGRATION as MIGRATION_001
from _sistema.time_utils import utc_now_iso


EXPECTED_TABLES = {
    "videos",
    "sources",
    "projects",
    "accounts",
    "jobs",
    "publications",
    "schedules",
    "templates",
    "artifacts",
    "errors",
    "settings",
    "audit_events",
    "migrations",
    "batches",
    "batch_jobs",
    "circuit_breaker_state",
    "job_retry_state",
    "source_asset_declarations",
    "source_asset_context",
    "video_declarations",
}


@pytest.fixture
def local_db():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(paths=paths)
        assert db.initialize() == LATEST_SCHEMA_VERSION
        yield db


def build_complete_graph():
    source = SourceAsset(
        source_uri="file:///original.mp4",
        local_path=r"C:\Videos\original.mp4",
        original_name="original.mp4",
        fingerprint="fp-001",
        size_bytes=12345,
        extra={"future_source_field": "preserved"},
    )
    video = Video(source_asset_id=source.id, name="Vídeo 1")
    project = Project(
        video_id=video.id,
        name="Projeto 1",
        revision=2,
        edit_state={"cuts": [{"start": 1.0, "end": 8.5}]},
    )
    account = Account(platform="youtube", name="Canal", local_key="youtube/canal")
    artifact = Artifact(
        video_id=video.id,
        project_id=project.id,
        kind="render",
        path=r"C:\Exports\video.mp4",
        fingerprint="artifact-fp",
        size_bytes=999,
    )
    job = Job(
        video_id=video.id,
        project_id=project.id,
        operation="render",
        progress=0.25,
        output_artifact_ids=[artifact.id],
        extra={"worker_hint": "cpu"},
    )
    artifact.job_id = job.id
    publication = Publication(
        video_id=video.id,
        account_id=account.id,
        artifact_id=artifact.id,
        title="Título",
        description="Descrição",
    )
    schedule = Schedule(
        publication_id=publication.id,
        scheduled_local="2026-09-17T10:00:00",
        timezone_iana="America/Sao_Paulo",
        time_origin=SCHEDULE_TIME_MANUAL,
        delivery_state=SCHEDULE_LOCAL_PENDING,
    )
    template = Template(
        name="Template oficial",
        template_type="OFFICIAL",
        source_path=r"C:\Templates\oficial.png",
        layout={"video": {"x": 0, "y": 0, "w": 1080, "h": 1920}},
    )
    error = ErrorRecord(
        code="TEST_ERROR",
        message="erro controlado",
        entity_type="Job",
        entity_id=job.id,
        job_id=job.id,
        recoverable=True,
        details={"attempt": 1},
    )
    return source, video, project, account, job, template, artifact, publication, schedule, error


def persist_complete_graph(db: LocalDatabase):
    entities = build_complete_graph()
    # Ordem respeita as FKs relacionais.
    for entity in entities[:6]:
        db.insert(entity)
    db.insert(entities[6])  # artifact depende do Job
    db.insert(entities[7])  # publication depende do artifact/account/video
    db.insert(entities[8])  # schedule depende da publication
    db.insert(entities[9])  # error depende do Job
    return entities


def test_schema_inicial_cria_todas_as_tabelas_e_versao(local_db):
    # PROMPT 27.5: LATEST_SCHEMA_VERSION avançou de 8 para 9
    # conscientemente (nova migration m009_video_declarations.py -- ver
    # tests/test_migrations_frozen.py, que já foi atualizada com o hash
    # congelado desta migration na mesma rodada). Tabela nova
    # (``video_declarations``, adicionada a EXPECTED_TABLES).
    assert set(local_db.table_names()) == EXPECTED_TABLES
    assert local_db.schema_version() == LATEST_SCHEMA_VERSION == 9
    with local_db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        rows = conn.execute("SELECT version, name, checksum FROM migrations ORDER BY version").fetchall()
        assert [(row["version"], row["name"]) for row in rows] == [
            (1, "initial_local_schema"),
            (2, "audit_append_only"),
            (3, "batch_engine_tables"),
            (4, "circuit_breaker_state"),
            (5, "job_retry_state"),
            (6, "publication_idempotency"),
            (7, "source_asset_declarations"),
            (8, "source_asset_context"),
            (9, "video_declarations"),
        ]
        assert all(len(row["checksum"]) == 64 for row in rows)


def test_initialize_e_idempotente(local_db):
    assert local_db.initialize() == LATEST_SCHEMA_VERSION
    assert local_db.initialize() == LATEST_SCHEMA_VERSION
    with local_db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == LATEST_SCHEMA_VERSION


def test_upgrade_schema1_para_schema8_cria_pre_migration_e_e_idempotente():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        database_path = paths.database / "upgrade-v1.db"
        database_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(sqlite3.connect(str(database_path))) as conn:
            with conn:
                conn.executescript(MIGRATION_001.sql)
                conn.execute(
                    "CREATE TABLE migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (1, MIGRATION_001.name, MIGRATION_001.checksum, utc_now_iso()),
                )
                conn.execute("PRAGMA user_version = 1")

        db = LocalDatabase(path=database_path, paths=paths)
        assert db.initialize() == 9

        manager = BackupManager(paths=paths, database_path=database_path)
        pre = [record for record in manager.list_backups() if record.reason == BACKUP_REASON_PRE_MIGRATION]
        assert len(pre) == 1
        assert pre[0].schema_version == 1

        with db.connection() as conn:
            rows = conn.execute("SELECT version, name FROM migrations ORDER BY version").fetchall()
            assert [(row["version"], row["name"]) for row in rows] == [
                (1, "initial_local_schema"),
                (2, "audit_append_only"),
                (3, "batch_engine_tables"),
                (4, "circuit_breaker_state"),
                (5, "job_retry_state"),
                (6, "publication_idempotency"),
                (7, "source_asset_declarations"),
                (8, "source_asset_context"),
                (9, "video_declarations"),
            ]
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            triggers = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
            }
            assert {
                "trg_audit_events_no_update",
                "trg_audit_events_no_delete",
                "trg_audit_events_no_id_reuse",
            } <= triggers
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            assert {"batches", "batch_jobs", "circuit_breaker_state", "job_retry_state", "source_asset_declarations", "video_declarations"} <= tables
            publications_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(publications)")
            }
            assert "idempotency_key" in publications_columns

        # Segunda inicialização não reaplica 002..008 nem cria outro PRE_MIGRATION.
        assert db.initialize() == 9
        with db.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM migrations WHERE version=2").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM migrations WHERE version=3").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM migrations WHERE version=4").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM migrations WHERE version=5").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM migrations WHERE version=6").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM migrations WHERE version=7").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM migrations WHERE version=8").fetchone()[0] == 1
        pre_after = [record for record in manager.list_backups() if record.reason == BACKUP_REASON_PRE_MIGRATION]
        assert len(pre_after) == 1


def test_upgrade_schema2_para_schema9_preserva_m001_m002_byte_identicas():
    """PROMPT 17: um banco já em schema v2 (m001+m002 aplicadas, sem m003)
    precisa migrar até a versão mais recente aplicando SOMENTE as
    migrations novas (``batch_engine_tables``, ``circuit_breaker_state``
    -- PROMPT 20, ``job_retry_state`` -- PROMPT 21,
    ``publication_idempotency`` -- PROMPT 22, ``source_asset_declarations``
    -- PROMPT 24b, ``source_asset_context`` -- PROMPT 25,
    ``video_declarations`` -- PROMPT 27.5), sem jamais reaplicar ou
    alterar m001/m002."""
    from _sistema.storage.migrations.m002_audit_append_only import MIGRATION as MIGRATION_002

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        database_path = paths.database / "upgrade-v2.db"
        database_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(sqlite3.connect(str(database_path))) as conn:
            with conn:
                conn.executescript(MIGRATION_001.sql)
                conn.executescript(MIGRATION_002.sql)
                conn.execute(
                    "CREATE TABLE migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (1, MIGRATION_001.name, MIGRATION_001.checksum, utc_now_iso()),
                )
                conn.execute(
                    "INSERT INTO migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (2, MIGRATION_002.name, MIGRATION_002.checksum, utc_now_iso()),
                )
                conn.execute("PRAGMA user_version = 2")

        db = LocalDatabase(path=database_path, paths=paths)
        assert db.schema_version() == 2
        assert db.initialize() == 9

        with db.connection() as conn:
            rows = conn.execute("SELECT version, name, checksum FROM migrations ORDER BY version").fetchall()
            assert [(row["version"], row["name"]) for row in rows] == [
                (1, "initial_local_schema"),
                (2, "audit_append_only"),
                (3, "batch_engine_tables"),
                (4, "circuit_breaker_state"),
                (5, "job_retry_state"),
                (6, "publication_idempotency"),
                (7, "source_asset_declarations"),
                (8, "source_asset_context"),
                (9, "video_declarations"),
            ]
            # m001/m002 byte-idênticas: o checksum persistido continua batendo
            # com o checksum atual do código (nenhuma reescrita ocorreu).
            assert rows[0]["checksum"] == MIGRATION_001.checksum
            assert rows[1]["checksum"] == MIGRATION_002.checksum
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        # Idempotente: reaplicar não duplica nem altera nada.
        assert db.initialize() == 9
        with db.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 9


def test_migration_checksum_adulterado_e_rejeitado(local_db):
    with local_db.transaction() as conn:
        conn.execute("UPDATE migrations SET checksum='adulterado' WHERE version=1")
    with pytest.raises(MigrationIntegrityError):
        local_db.initialize()


def test_wal_e_foreign_keys_estao_ativos(local_db):
    assert local_db.journal_mode() == "wal"
    assert local_db.foreign_keys_enabled() is True



def test_indices_essenciais_existem(local_db):
    expected = {
        "idx_videos_source_asset_id",
        "idx_projects_video_id",
        "idx_jobs_status",
        "idx_jobs_updated_at",
        "idx_publications_account_id",
        "idx_publications_status",
        "idx_schedules_publication_id",
        "idx_schedules_scheduled_utc",
        "idx_artifacts_job_id",
        "idx_errors_job_id",
        "idx_audit_events_entity",
    }
    with local_db.connection() as conn:
        found = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert expected <= found

def test_crud_roundtrip_dos_dez_modelos_de_dominio(local_db):
    entities = persist_complete_graph(local_db)
    for original in entities:
        loaded = local_db.get(type(original), original.id)
        assert loaded is not None
        assert loaded.to_dict() == original.to_dict()

    video = entities[1]
    video.name = "Vídeo renomeado"
    video.touch()
    local_db.save(video)
    assert local_db.get(Video, video.id).name == "Vídeo renomeado"

    error = entities[-1]
    assert local_db.delete(ErrorRecord, error.id) is True
    assert local_db.get(ErrorRecord, error.id) is None
    assert local_db.delete(ErrorRecord, error.id) is False


def test_list_entidades_preserva_tipo_e_ordem(local_db):
    first = SourceAsset(source_uri="file:///a.mp4")
    second = SourceAsset(source_uri="file:///b.mp4")
    local_db.insert(first)
    local_db.insert(second)
    found = local_db.list(SourceAsset)
    assert {item.id for item in found} == {first.id, second.id}
    assert all(isinstance(item, SourceAsset) for item in found)


def test_fk_rejeita_referencia_inexistente(local_db):
    bad_video = Video(source_asset_id=str(uuid4()), name="órfão")
    with pytest.raises(sqlite3.IntegrityError):
        local_db.insert(bad_video)
    assert local_db.get(Video, bad_video.id) is None


def test_fk_impede_apagar_pai_referenciado(local_db):
    source = SourceAsset(source_uri="file:///a.mp4")
    video = Video(source_asset_id=source.id, name="filho")
    local_db.insert(source)
    local_db.insert(video)
    with pytest.raises(sqlite3.IntegrityError):
        local_db.delete(SourceAsset, source.id)
    assert local_db.get(SourceAsset, source.id) is not None


def test_transaction_rollback_nao_deixa_estado_parcial(local_db):
    account = Account(platform="youtube", name="Rollback")
    invalid_video = Video(source_asset_id=str(uuid4()), name="inválido")
    with pytest.raises(sqlite3.IntegrityError):
        with local_db.transaction() as conn:
            local_db.insert(account, connection=conn)
            local_db.insert(invalid_video, connection=conn)
    assert local_db.get(Account, account.id) is None
    assert local_db.get(Video, invalid_video.id) is None


def test_settings_crud_aceita_json_estruturado(local_db):
    value = {"theme": "system", "limits": {"workers": 2}, "items": [1, 2, 3]}
    assert local_db.get_setting("ui.preferences") is None
    local_db.set_setting("ui.preferences", value)
    assert local_db.get_setting("ui.preferences") == value
    local_db.set_setting("ui.preferences", {"theme": "dark"})
    assert local_db.get_setting("ui.preferences") == {"theme": "dark"}
    assert local_db.delete_setting("ui.preferences") is True
    assert local_db.get_setting("ui.preferences", "fallback") == "fallback"


def test_audit_events_sao_append_only_por_api(local_db):
    entity_id = str(uuid4())
    event_id = local_db.append_audit_event(
        "JOB_CREATED",
        entity_type="Job",
        entity_id=entity_id,
        data={"source": "test"},
    )
    events = local_db.list_audit_events()
    assert len(events) == 1
    assert events[0]["id"] == event_id
    assert events[0]["event_type"] == "JOB_CREATED"
    assert events[0]["entity_id"] == entity_id
    assert events[0]["data"] == {"source": "test"}


def test_concorrencia_basica_com_multiplos_writers(local_db):
    workers = 6
    writes_per_worker = 15

    def writer(worker_index: int) -> int:
        for item in range(writes_per_worker):
            local_db.set_setting(
                f"concurrency.{worker_index}.{item}",
                {"worker": worker_index, "item": item},
            )
            local_db.append_audit_event(
                "CONCURRENT_WRITE",
                data={"worker": worker_index, "item": item},
            )
        return writes_per_worker

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(writer, range(workers)))

    assert sum(results) == workers * writes_per_worker
    with local_db.connection() as conn:
        settings_count = conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key LIKE 'concurrency.%'"
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='CONCURRENT_WRITE'"
        ).fetchone()[0]
    assert settings_count == workers * writes_per_worker
    assert audit_count == workers * writes_per_worker


def test_database_default_fica_na_pasta_database_do_app_paths():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(paths=paths)
        assert db.path == paths.database / "painel.db"
        assert paths.install_root not in db.path.parents



def test_initialize_concorrente_mesmo_banco_e_idempotente_entre_instancias():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        database_path = paths.database / "shared-init.db"
        workers = 10
        barrier = threading.Barrier(workers)

        def initializer(_worker_index: int) -> int:
            db = LocalDatabase(path=database_path, paths=paths, busy_timeout_ms=10_000)
            barrier.wait(timeout=5)
            return db.initialize()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            versions = list(pool.map(initializer, range(workers)))

        assert versions == [LATEST_SCHEMA_VERSION] * workers
        inspector = LocalDatabase(path=database_path, paths=paths)
        with inspector.connection() as conn:
            migration_rows = conn.execute(
                "SELECT version, name, checksum FROM migrations ORDER BY version"
            ).fetchall()
            assert len(migration_rows) == LATEST_SCHEMA_VERSION
            assert [row["version"] for row in migration_rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
            assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            found_tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        assert found_tables == EXPECTED_TABLES


def test_historico_de_migrations_precisa_ser_prefixo_continuo(monkeypatch):
    migrations = (
        Migration(1, "one", "CREATE TABLE one(id INTEGER PRIMARY KEY);"),
        Migration(2, "two", "CREATE TABLE two(id INTEGER PRIMARY KEY);"),
        Migration(3, "three", "CREATE TABLE three(id INTEGER PRIMARY KEY);"),
    )
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations)
    monkeypatch.setattr(database_module, "LATEST_SCHEMA_VERSION", 3)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(path=root / "gap.db", paths=paths)
        with db.connection() as conn:
            conn.execute(
                "CREATE TABLE migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO migrations(version, name, checksum, applied_at) VALUES (1, ?, ?, 'now')",
                (migrations[0].name, migrations[0].checksum),
            )
            conn.execute(
                "INSERT INTO migrations(version, name, checksum, applied_at) VALUES (3, ?, ?, 'now')",
                (migrations[2].name, migrations[2].checksum),
            )
            conn.execute("PRAGMA user_version = 3")

        with pytest.raises(MigrationIntegrityError, match="prefixo contínuo"):
            db.initialize()


def test_schema_mais_novo_continua_sendo_recusado_sem_aplicar_migration():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(path=root / "future.db", paths=paths)
        with db.connection() as conn:
            conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")

        with pytest.raises(UnsupportedSchemaVersionError):
            db.initialize()

        with db.connection() as conn:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        assert "migrations" not in tables


def test_publication_aceita_no_maximo_um_schedule_associado(local_db):
    first_publication = Publication(title="Primeira")
    second_publication = Publication(title="Segunda")
    local_db.insert(first_publication)
    local_db.insert(second_publication)

    first_schedule = Schedule(publication_id=first_publication.id)
    local_db.insert(first_schedule)

    duplicate_schedule = Schedule(publication_id=first_publication.id)
    with pytest.raises(sqlite3.IntegrityError):
        local_db.insert(duplicate_schedule)
    assert local_db.get(Schedule, duplicate_schedule.id) is None

    other_schedule = Schedule(publication_id=second_publication.id)
    local_db.insert(other_schedule)
    assert local_db.get(Schedule, other_schedule.id) is not None

    first_schedule.extra["updated"] = True
    first_schedule.touch()
    local_db.save(first_schedule)
    assert local_db.get(Schedule, first_schedule.id).extra["updated"] is True


def test_multiplos_schedules_sem_publication_id_continuam_permitidos(local_db):
    first = Schedule()
    second = Schedule()
    local_db.insert(first)
    local_db.insert(second)
    assert local_db.get(Schedule, first.id) is not None
    assert local_db.get(Schedule, second.id) is not None


def test_initialize_repetido_nao_deixa_handle_sqlite_aberto_para_rename_e_cleanup(tmp_path):
    import shutil

    root = tmp_path / "sqlite-handle-lifecycle"
    paths = build_app_paths(install_root=root / "install", data_root=root / "data")
    db = LocalDatabase(paths=paths)

    for index in range(5):
        assert db.initialize() == LATEST_SCHEMA_VERSION
        db.set_setting("windows.handle.probe", index)
        assert db.get_setting("windows.handle.probe") == index

    database_path = db.path
    renamed_path = database_path.with_name("painel-renamed.db")
    database_path.rename(renamed_path)
    renamed_path.rename(database_path)

    # Remove toda a árvore sem retries/GC/sleeps. No Windows, qualquer handle
    # SQLite ainda vivo faria shutil.rmtree levantar PermissionError/WinError 32.
    shutil.rmtree(root)
    assert not root.exists()
