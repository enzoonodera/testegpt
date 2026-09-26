from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import os
import subprocess
import sys

import pytest

from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Account, Artifact, ErrorRecord, Job, Publication, Schedule, SourceAsset, Video, SCHEDULE_UNKNOWN
from _sistema.storage import LocalDatabase
from _sistema.storage import legacy_migration as lm


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_fixture(tmp_path: Path):
    install = tmp_path / "install"
    data = tmp_path / "data"
    install.mkdir()
    paths = build_app_paths(install_root=install, data_root=data)
    ensure_app_directories(paths)
    account = paths.accounts / "youtube" / "Canal A"
    data_dir = account / "dados"
    videos = account / "videos"
    videos.mkdir(parents=True)
    (videos / "001.mp4").write_bytes(b"video-bytes")

    _write_json(account / "config_canal.json", {
        "plataforma": "youtube",
        "nome_conta": "Canal A",
        "pais_alvo": "Brasil",
        "timezone_iana": "America/Sao_Paulo",
        "horarios": ["12:00", "18:00"],
    })
    _write_json(data_dir / "limpeza_estado.json", {
        "version": 1,
        "items": {
            "fp-001": {
                "fingerprint": "fp-001",
                "source_name": "original.mp4",
                "source_path": str(tmp_path / "originais" / "original.mp4"),
                "source_size": 1234,
                "output": "001.mp4",
                "status": "done",
                "reserved_at": "2026-09-17T12:00:00+00:00",
                "completed_at": "2026-09-17T12:01:00+00:00",
                "last_error": "",
            }
        },
        "updated_at": "2026-09-17T12:01:00+00:00",
    })
    _write_json(data_dir / "textos_postagem.json", {
        "fp-001": {
            "arquivo": "001.mp4",
            "plataforma": "youtube",
            "status": "done",
            "titulo": "Título",
            "descricao": "Descrição",
            "hashtags": ["#Shorts"],
        }
    })
    _write_json(data_dir / "estado_youtube.json", {
        "version": 8,
        "scheduled": [{
            "file": "001.mp4",
            "fingerprint": "fp-001",
            "title": "Título",
            "description": "Descrição",
            "scheduled_local": "2026-09-18T12:00",
            "timezone_iana": "America/Sao_Paulo",
            "scheduled_utc": "2026-09-18T15:00:00+00:00",
            "time_origin": "MANUAL",
            "registered_at": "2026-09-17T12:02:00+00:00",
        }],
    })
    return paths, account


def _legacy_counts(db: LocalDatabase) -> dict[str, int]:
    with db.connection() as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "sources", "videos", "accounts", "jobs", "artifacts",
                "publications", "schedules", "errors", "audit_events",
            )
        } | {
            "legacy_settings": int(conn.execute(
                "SELECT COUNT(*) FROM settings WHERE key LIKE 'legacy_%'"
            ).fetchone()[0])
        }


def _db_dump(db: LocalDatabase) -> tuple[str, ...]:
    with db.connection() as conn:
        return tuple(conn.iterdump())


def test_migracao_cria_backup_checksum_mapeia_estado_e_relatorio(tmp_path):
    paths, account = _build_fixture(tmp_path)
    db = LocalDatabase(paths=paths)

    report = lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    assert report.status == "MIGRATED"
    assert report.files_discovered == 4
    assert report.files_backed_up == 4
    assert report.raw_records_imported == 4
    assert report.accounts_imported == 1
    assert report.cleanup_items_imported == 1
    assert report.publications_imported == 1
    assert report.schedules_imported == 1

    backup_dir = Path(report.backup_path)
    assert backup_dir.is_dir()
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_manifest_sha256"] == report.source_manifest_sha256
    assert manifest["videos_copied"] is False
    assert not (backup_dir / "accounts" / "youtube" / "Canal A" / "videos" / "001.mp4").exists()
    assert (backup_dir / "accounts" / "youtube" / "Canal A" / "dados" / "estado_youtube.json").exists()

    raw = db.get_setting("legacy_json/v1/youtube/Canal A/dados/textos_postagem.json")
    assert raw["payload"]["fp-001"]["titulo"] == "Título"
    assert len(raw["sha256"]) == 64

    assert len(db.list(Account)) == 1
    assert len(db.list(SourceAsset)) == 1
    assert len(db.list(Video)) == 1
    assert len(db.list(Job)) == 1
    assert len(db.list(Artifact)) == 1
    pubs = db.list(Publication)
    schedules = db.list(Schedule)
    assert len(pubs) == 1
    assert pubs[0].status == "UNKNOWN"
    assert len(schedules) == 1
    assert schedules[0].publication_id == pubs[0].id
    assert schedules[0].delivery_state == SCHEDULE_UNKNOWN

    report_path = paths.support / lm.LEGACY_REPORT_FILENAME
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved_report["status"] == "MIGRATED"
    assert saved_report["source_manifest_sha256"] == report.source_manifest_sha256

    # A migration não altera nem remove os JSONs antigos.
    assert (account / "config_canal.json").is_file()
    assert (account / "dados" / "limpeza_estado.json").is_file()


def test_migracao_duas_vezes_e_idempotente_sem_duplicar(tmp_path):
    paths, _ = _build_fixture(tmp_path)
    db = LocalDatabase(paths=paths)

    first = lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)
    counts_before = _legacy_counts(db)
    dump_before = _db_dump(db)
    backups_before = sorted((paths.backups / lm.LEGACY_BACKUP_DIRNAME).glob("[!.]*"))

    second = lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)
    counts_after = _legacy_counts(db)
    dump_after = _db_dump(db)
    backups_after = sorted((paths.backups / lm.LEGACY_BACKUP_DIRNAME).glob("[!.]*"))

    assert first.status == "MIGRATED"
    assert second.status == "NOOP"
    assert first.source_manifest_sha256 == second.source_manifest_sha256
    assert counts_after == counts_before
    assert dump_after == dump_before
    assert backups_after == backups_before
    assert counts_after["audit_events"] == 1


def test_checksum_do_backup_detecta_adulteracao_antes_de_reusar(tmp_path):
    paths, _ = _build_fixture(tmp_path)
    db = LocalDatabase(paths=paths)
    first = lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)
    target = Path(first.backup_path) / "accounts" / "youtube" / "Canal A" / "dados" / "textos_postagem.json"
    target.write_text("{}", encoding="utf-8")

    with pytest.raises(lm.LegacyJsonBackupError):
        lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)


def test_json_corrompido_falha_estritamente_e_nao_publica_estado_sqlite(tmp_path):
    paths, account = _build_fixture(tmp_path)
    (account / "dados" / "estado_youtube.json").write_text("{quebrado", encoding="utf-8")
    db = LocalDatabase(paths=paths)

    with pytest.raises(lm.LegacyJsonValidationError):
        lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    db.initialize()
    assert db.get_setting(lm.LEGACY_STATE_SETTING) is None
    assert db.list(Account) == []
    # O backup byte-a-byte do arquivo problemático fica disponível para diagnóstico.
    backup_root = paths.backups / lm.LEGACY_BACKUP_DIRNAME
    dirs = [p for p in backup_root.iterdir() if p.is_dir() and p.name != ".staging"]
    assert len(dirs) == 1
    assert (dirs[0] / "accounts" / "youtube" / "Canal A" / "dados" / "estado_youtube.json").is_file()


def test_falha_de_validacao_de_contagens_faz_rollback_total(tmp_path, monkeypatch):
    paths, _ = _build_fixture(tmp_path)
    db = LocalDatabase(paths=paths)

    def explode(*args, **kwargs):
        raise lm.LegacyJsonMigrationError("falha simulada após writes")

    monkeypatch.setattr(lm, "_validate_transaction_counts", explode)
    with pytest.raises(lm.LegacyJsonMigrationError, match="falha simulada"):
        lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    assert db.get_setting(lm.LEGACY_STATE_SETTING) is None
    assert db.list(Account) == []
    assert db.list(SourceAsset) == []
    assert db.list(Video) == []
    assert db.list(Job) == []
    assert db.list(Publication) == []
    assert db.list(Schedule) == []
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'legacy_json/v1/%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


def test_jsons_mudaram_depois_da_migracao_nao_sao_mesclados_silenciosamente(tmp_path):
    paths, account = _build_fixture(tmp_path)
    db = LocalDatabase(paths=paths)
    lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)
    before = _legacy_counts(db)

    textos = account / "dados" / "textos_postagem.json"
    payload = json.loads(textos.read_text(encoding="utf-8"))
    payload["fp-001"]["titulo"] = "Mudou depois"
    _write_json(textos, payload)

    with pytest.raises(lm.LegacyJsonChangedAfterMigrationError):
        lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    assert _legacy_counts(db) == before


def test_publicacao_legada_sem_timezone_nao_inventa_timezone_e_preserva_raw(tmp_path):
    paths, account = _build_fixture(tmp_path)
    cfg = json.loads((account / "config_canal.json").read_text(encoding="utf-8"))
    cfg.pop("timezone_iana")
    _write_json(account / "config_canal.json", cfg)
    state_path = account / "dados" / "estado_youtube.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for item in state["scheduled"]:
        item.pop("timezone_iana", None)
        item.pop("scheduled_utc", None)
    _write_json(state_path, state)

    db = LocalDatabase(paths=paths)
    report = lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    assert report.publications_imported == 1
    assert report.schedules_imported == 0
    assert report.warnings
    assert len(db.list(Publication)) == 1
    assert db.list(Schedule) == []
    raw = db.get_setting("legacy_json/v1/youtube/Canal A/dados/estado_youtube.json")
    assert raw["payload"]["scheduled"][0]["datetime"] if "datetime" in raw["payload"]["scheduled"][0] else True


def test_descoberta_nunca_varre_json_de_perfil_chrome_ou_transcricao_cache(tmp_path):
    paths, account = _build_fixture(tmp_path)
    _write_json(account / "perfil_youtube" / "Default" / "Preferences", {"token": "nao importar"})
    _write_json(account / "dados" / "transcricoes" / "fp-001.json", {"text": "conteudo pessoal"})

    found = {path.resolve() for path in lm.discover_legacy_state_files(paths)}
    assert (account / "config_canal.json").resolve() in found
    assert (account / "dados" / "transcricoes" / "fp-001.json").resolve() not in found
    assert (account / "perfil_youtube" / "Default" / "Preferences").resolve() not in found


def test_sourceasset_original_nao_e_sobrescrito_por_publication_output(tmp_path):
    paths, _ = _build_fixture(tmp_path)
    db = LocalDatabase(paths=paths)
    lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    source = db.list(SourceAsset)[0]
    video = db.list(Video)[0]
    artifact = db.list(Artifact)[0]
    publication = db.list(Publication)[0]

    assert source.local_path == str(tmp_path / "originais" / "original.mp4")
    assert source.original_name == "original.mp4"
    assert source.size_bytes == 1234
    assert Path(artifact.path).name == "001.mp4"
    assert publication.video_id == video.id
    assert video.source_asset_id == source.id


def _set_cleanup_item(account: Path, *, status: str, output: str = "001.mp4") -> None:
    path = account / "dados" / "limpeza_estado.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    item = payload["items"]["fp-001"]
    item["status"] = status
    item["output"] = output
    _write_json(path, payload)


def test_pending_com_output_reservado_nao_cria_artifact(tmp_path):
    paths, account = _build_fixture(tmp_path)
    _set_cleanup_item(account, status="pending")
    (account / "videos" / "001.mp4").unlink()
    db = LocalDatabase(paths=paths)
    lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    job = db.list(Job)[0]
    assert job.status == "PENDING"
    assert job.output_artifact_ids == []
    assert db.list(Artifact) == []


def test_done_com_output_inexistente_vira_pending_sem_artifact_e_com_erro(tmp_path):
    paths, account = _build_fixture(tmp_path)
    _set_cleanup_item(account, status="done")
    (account / "videos" / "001.mp4").unlink()
    db = LocalDatabase(paths=paths)
    report = lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    job = db.list(Job)[0]
    assert job.status == "PENDING"
    assert job.output_artifact_ids == []
    assert db.list(Artifact) == []
    errors = db.list(ErrorRecord)
    assert any(error.code == "LEGACY_OUTPUT_MISSING_OR_EMPTY" for error in errors)
    assert any("status done sem output válido" in warning for warning in report.warnings)


def test_done_com_output_zero_bytes_vira_pending_sem_artifact(tmp_path):
    paths, account = _build_fixture(tmp_path)
    _set_cleanup_item(account, status="done")
    (account / "videos" / "001.mp4").write_bytes(b"")
    db = LocalDatabase(paths=paths)
    lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    job = db.list(Job)[0]
    assert job.status == "PENDING"
    assert job.output_artifact_ids == []
    assert db.list(Artifact) == []
    assert any(error.code == "LEGACY_OUTPUT_MISSING_OR_EMPTY" for error in db.list(ErrorRecord))


def test_done_com_output_existente_materializa_artifact_ready_e_tamanho(tmp_path):
    paths, account = _build_fixture(tmp_path)
    payload = b"artifact-real-data"
    (account / "videos" / "001.mp4").write_bytes(payload)
    _set_cleanup_item(account, status="done")
    db = LocalDatabase(paths=paths)
    lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    job = db.list(Job)[0]
    artifacts = db.list(Artifact)
    assert job.status == "READY"
    assert len(artifacts) == 1
    assert job.output_artifact_ids == [artifacts[0].id]
    assert artifacts[0].size_bytes == len(payload)


def test_failed_nao_cria_artifact_mesmo_se_output_reservado_existe(tmp_path):
    paths, account = _build_fixture(tmp_path)
    _set_cleanup_item(account, status="failed")
    db = LocalDatabase(paths=paths)
    lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)

    job = db.list(Job)[0]
    assert job.status == "FAILED"
    assert job.output_artifact_ids == []
    assert db.list(Artifact) == []


def test_zero_json_nao_marca_concluida_e_dados_posteriores_migram(tmp_path):
    install = tmp_path / "install"
    data = tmp_path / "data"
    install.mkdir()
    paths = build_app_paths(install_root=install, data_root=data)
    ensure_app_directories(paths)
    db = LocalDatabase(paths=paths)

    first = lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)
    assert first.status == "NO_DATA"
    assert db.get_setting(lm.LEGACY_STATE_SETTING) is None
    assert db.list_audit_events() == []

    account = paths.accounts / "youtube" / "Canal Depois"
    _write_json(account / "config_canal.json", {
        "plataforma": "youtube",
        "nome_conta": "Canal Depois",
        "timezone_iana": "America/Sao_Paulo",
    })
    second = lm.migrate_legacy_json_to_sqlite(paths=paths, database=db)
    assert second.status == "MIGRATED"
    assert db.get_setting(lm.LEGACY_STATE_SETTING) is not None
    assert len(db.list_audit_events()) == 1


def test_migration_concorrente_em_subprocessos_resulta_migrated_e_noop(tmp_path):
    paths, _ = _build_fixture(tmp_path)
    project_root = Path(__file__).resolve().parents[1]
    code = """
import json, sys
from pathlib import Path
from _sistema.app_paths import build_app_paths
from _sistema.storage.legacy_migration import migrate_legacy_json_to_sqlite
paths = build_app_paths(install_root=Path(sys.argv[1]), data_root=Path(sys.argv[2]))
report = migrate_legacy_json_to_sqlite(paths=paths, lock_timeout_seconds=20.0)
print(json.dumps({\"status\": report.status}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-c", code, str(paths.install_root), str(paths.data_root)]
    procs = [
        subprocess.Popen(
            cmd,
            cwd=project_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [proc.communicate(timeout=30) for proc in procs]
    assert [proc.returncode for proc in procs] == [0, 0], outputs
    statuses = sorted(json.loads(stdout.strip().splitlines()[-1])["status"] for stdout, _ in outputs)
    assert statuses == ["MIGRATED", "NOOP"]

    db = LocalDatabase(paths=paths)
    counts = _legacy_counts(db)
    assert counts["audit_events"] == 1
    assert counts["accounts"] == 1
    assert counts["sources"] == 1
    assert counts["videos"] == 1
    assert counts["jobs"] == 1
    assert counts["artifacts"] == 1
    assert counts["publications"] == 1
    assert counts["schedules"] == 1
