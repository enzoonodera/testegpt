from pathlib import Path

from _sistema import app_paths


def test_default_data_root_usa_localappdata_no_windows_style(tmp_path):
    local = tmp_path / "LocalAppData"
    paths = app_paths.build_app_paths(
        install_root=tmp_path / "programa",
        env={"LOCALAPPDATA": str(local)},
    )
    assert paths.data_root == (local / "PainelOficial").resolve()
    assert paths.accounts == paths.data_root / "accounts"


def test_data_root_override_tem_precedencia(tmp_path):
    override = tmp_path / "dados-teste"
    paths = app_paths.build_app_paths(
        install_root=tmp_path / "programa",
        env={"PAINEL_DATA_ROOT": str(override), "LOCALAPPDATA": str(tmp_path / "ignorar")},
    )
    assert paths.data_root == override.resolve()


def test_layout_mutavel_fica_separado_do_codigo_instalado(tmp_path):
    install = tmp_path / "Program Files" / "PainelOficial"
    data = tmp_path / "LocalAppData" / "PainelOficial"
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    assert paths.install_root == install.resolve()
    assert paths.data_root == data.resolve()
    assert paths.install_root != paths.data_root
    assert paths.system == paths.install_root / "_sistema"

    expected = {
        "database",
        "accounts",
        "projects",
        "cache",
        "logs",
        "temp",
        "templates",
        "backups",
        "models",
        "support",
    }
    assert expected <= {p.name for p in paths.mutable_dirs()}


def test_ensure_app_directories_cria_layout_sem_tocar_no_install_root(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    app_paths.ensure_app_directories(paths)

    assert not install.exists()
    assert data.is_dir()
    for directory in paths.mutable_dirs():
        assert directory.is_dir(), directory


def test_account_paths_preserva_layout_interno_existente(tmp_path):
    account = tmp_path / "accounts" / "youtube" / "Canal_A"
    ap = app_paths.account_paths(account)

    assert ap.root == account.resolve()
    assert ap.data == ap.root / "dados"
    assert ap.videos == ap.root / "videos"
    assert ap.logs == ap.root / "logs"
    assert ap.config == ap.root / "config_canal.json"
    assert ap.profile_youtube == ap.root / "perfil_youtube"
    assert ap.profile_tiktok == ap.root / "perfil_tiktok"
    assert ap.blocked == ap.root / "bloqueados"


def test_migracao_move_layout_legado_para_data_root_e_preserva_conteudo(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "localappdata" / "PainelOficial"
    legacy_account = install / "contas" / "youtube" / "Canal_A"
    legacy_removed = install / "_removidas" / "tiktok" / "Conta_Antiga"
    (legacy_account / "dados").mkdir(parents=True)
    (legacy_account / "dados" / "estado_youtube.json").write_text('{"version":8}', encoding="utf-8")
    (legacy_account / "videos").mkdir()
    (legacy_account / "videos" / "001.mp4").write_bytes(b"video")
    legacy_removed.mkdir(parents=True)
    (legacy_removed / "config_canal.json").write_text('{"plataforma":"tiktok"}', encoding="utf-8")

    paths = app_paths.build_app_paths(install_root=install, data_root=data)
    report = app_paths.initialize_app_storage(paths)

    migrated = paths.accounts / "youtube" / "Canal_A"
    removed = paths.removed_accounts / "tiktok" / "Conta_Antiga"
    assert report.ok
    assert report.changed
    assert (migrated / "dados" / "estado_youtube.json").read_text(encoding="utf-8") == '{"version":8}'
    assert (migrated / "videos" / "001.mp4").read_bytes() == b"video"
    assert (removed / "config_canal.json").exists()
    assert not paths.legacy_accounts.exists()
    assert not paths.legacy_removed_accounts.exists()
    assert paths.migration_state.exists()


def test_migracao_e_idempotente(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    legacy = install / "contas" / "youtube" / "Canal"
    legacy.mkdir(parents=True)
    (legacy / "config_canal.json").write_text("abc", encoding="utf-8")
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    first = app_paths.initialize_app_storage(paths)
    second = app_paths.initialize_app_storage(paths)

    assert first.ok and first.changed
    assert second.ok and not second.changed
    assert (paths.accounts / "youtube" / "Canal" / "config_canal.json").read_text(encoding="utf-8") == "abc"


def test_migracao_nao_sobrescreve_conflito(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    legacy_file = install / "contas" / "youtube" / "Canal" / "config_canal.json"
    target_file = data / "accounts" / "youtube" / "Canal" / "config_canal.json"
    legacy_file.parent.mkdir(parents=True)
    target_file.parent.mkdir(parents=True)
    legacy_file.write_text("LEGADO", encoding="utf-8")
    target_file.write_text("NOVO", encoding="utf-8")
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    report = app_paths.initialize_app_storage(paths)

    assert not report.ok
    assert report.conflicts
    assert target_file.read_text(encoding="utf-8") == "NOVO"
    assert legacy_file.read_text(encoding="utf-8") == "LEGADO"


def test_account_dir_from_env_respeita_account_dir(monkeypatch, tmp_path):
    account = tmp_path / "conta"
    monkeypatch.setenv("ACCOUNT_DIR", str(account))
    assert app_paths.account_dir_from_env() == account.resolve()


def test_initialize_rejeita_data_root_dentro_da_pasta_instalada(tmp_path):
    install = tmp_path / "programa"
    data = install / "dados-mutaveis"
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    import pytest
    with pytest.raises(RuntimeError, match="separada da pasta de instalação"):
        app_paths.initialize_app_storage(paths)


def test_move_file_safely_fallback_cross_volume_valida_antes_de_remover_origem(monkeypatch, tmp_path):
    source = tmp_path / "legacy" / "estado.json"
    destination = tmp_path / "novo" / "estado.json"
    source.parent.mkdir()
    source.write_bytes(b"estado-importante")
    original_replace = Path.replace

    def replace_cross_volume(self, target):
        if self == source:
            raise OSError("cross-device")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace_cross_volume)

    assert app_paths._move_file_safely(source, destination)
    assert not source.exists()
    assert destination.read_bytes() == b"estado-importante"
    assert not destination.with_name(destination.name + ".migration_tmp").exists()


def test_move_file_safely_falha_de_copia_preserva_origem(monkeypatch, tmp_path):
    source = tmp_path / "legacy" / "estado.json"
    destination = tmp_path / "novo" / "estado.json"
    source.parent.mkdir()
    source.write_bytes(b"estado-importante")
    original_replace = Path.replace

    def replace_cross_volume(self, target):
        if self == source:
            raise OSError("cross-device")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace_cross_volume)
    monkeypatch.setattr(app_paths.shutil, "copy2", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("sem espaço")))

    assert not app_paths._move_file_safely(source, destination)
    assert source.read_bytes() == b"estado-importante"
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".migration_tmp").exists()


def test_move_file_safely_sem_espaco_preserva_origem(monkeypatch, tmp_path):
    source = tmp_path / "legacy" / "video.mp4"
    destination = tmp_path / "novo" / "video.mp4"
    source.parent.mkdir()
    source.write_bytes(b"x" * 1024)
    original_replace = Path.replace

    def replace_cross_volume(self, target):
        if self == source:
            raise OSError("cross-device")
        return original_replace(self, target)

    class Usage:
        total = 2048
        used = 2048
        free = 0

    monkeypatch.setattr(Path, "replace", replace_cross_volume)
    monkeypatch.setattr(app_paths.shutil, "disk_usage", lambda path: Usage())

    assert not app_paths._move_file_safely(source, destination)
    assert source.exists()
    assert not destination.exists()


def _write_account_tree(root: Path, *, config: str = "CFG", video: bytes = b"video", profile: bytes = b"profile") -> None:
    (root / "videos").mkdir(parents=True, exist_ok=True)
    (root / "perfil_youtube" / "Default").mkdir(parents=True, exist_ok=True)
    (root / "config_canal.json").write_text(config, encoding="utf-8")
    (root / "videos" / "001.mp4").write_bytes(video)
    (root / "perfil_youtube" / "Default" / "Cookies").write_bytes(profile)


def test_migracao_transacional_move_conta_inteira_como_unidade(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "contas" / "youtube" / "Canal"
    _write_account_tree(source)
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    report = app_paths.initialize_app_storage(paths)

    destination = paths.accounts / "youtube" / "Canal"
    assert report.ok and report.changed
    assert not source.exists()
    assert (destination / "config_canal.json").read_text(encoding="utf-8") == "CFG"
    assert (destination / "videos" / "001.mp4").read_bytes() == b"video"
    assert (destination / "perfil_youtube" / "Default" / "Cookies").read_bytes() == b"profile"


def test_conflito_em_config_nao_move_video_nem_perfil_da_conta(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "contas" / "youtube" / "Canal"
    destination = data / "accounts" / "youtube" / "Canal"
    _write_account_tree(source, config="LEGADO", video=b"legacy-video", profile=b"legacy-profile")
    destination.mkdir(parents=True)
    (destination / "config_canal.json").write_text("NOVO", encoding="utf-8")
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    report = app_paths.initialize_app_storage(paths)

    assert not report.ok
    assert source.exists()
    assert (source / "videos" / "001.mp4").read_bytes() == b"legacy-video"
    assert (source / "perfil_youtube" / "Default" / "Cookies").read_bytes() == b"legacy-profile"
    assert (destination / "config_canal.json").read_text(encoding="utf-8") == "NOVO"
    assert not (destination / "videos").exists()
    assert not (destination / "perfil_youtube").exists()


def test_conflito_no_perfil_chrome_nao_move_outros_arquivos_da_conta(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "contas" / "youtube" / "Canal"
    destination = data / "accounts" / "youtube" / "Canal"
    _write_account_tree(source, config="IGUAL", video=b"legacy-video", profile=b"COOKIE-LEGADO")
    _write_account_tree(destination, config="IGUAL", video=b"dest-video", profile=b"COOKIE-NOVO")
    # Remove o vídeo do destino para provar que a migração não completa a árvore parcialmente.
    (destination / "videos" / "001.mp4").unlink()
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    report = app_paths.initialize_app_storage(paths)

    assert not report.ok
    assert source.exists()
    assert (source / "videos" / "001.mp4").read_bytes() == b"legacy-video"
    assert (source / "perfil_youtube" / "Default" / "Cookies").read_bytes() == b"COOKIE-LEGADO"
    assert not (destination / "videos" / "001.mp4").exists()
    assert (destination / "perfil_youtube" / "Default" / "Cookies").read_bytes() == b"COOKIE-NOVO"


def test_arvores_de_conta_identicas_sao_duplicata_segura(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "contas" / "youtube" / "Canal"
    destination = data / "accounts" / "youtube" / "Canal"
    _write_account_tree(source, config="IGUAL", video=b"same", profile=b"same-profile")
    _write_account_tree(destination, config="IGUAL", video=b"same", profile=b"same-profile")
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    report = app_paths.initialize_app_storage(paths)

    assert report.ok
    assert report.duplicate_removed == ("contas/youtube/Canal",)
    assert not source.exists()
    assert (destination / "videos" / "001.mp4").read_bytes() == b"same"
    assert (destination / "perfil_youtube" / "Default" / "Cookies").read_bytes() == b"same-profile"


def test_falha_no_meio_da_copia_transacional_preserva_origem_integral(monkeypatch, tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "contas" / "youtube" / "Canal"
    destination = data / "accounts" / "youtube" / "Canal"
    _write_account_tree(source, config="CFG", video=b"video-original", profile=b"profile-original")
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    original_replace = Path.replace
    original_copy2 = app_paths.shutil.copy2
    calls = {"count": 0}

    def cross_volume_for_source(self, target):
        if self == source:
            raise OSError("cross-device")
        return original_replace(self, target)

    def flaky_copy2(src, dst, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] >= 2:
            raise OSError("falha simulada no meio da cópia")
        return original_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", cross_volume_for_source)
    monkeypatch.setattr(app_paths.shutil, "copy2", flaky_copy2)

    report = app_paths.initialize_app_storage(paths)

    assert not report.ok
    assert source.exists()
    assert (source / "config_canal.json").read_text(encoding="utf-8") == "CFG"
    assert (source / "videos" / "001.mp4").read_bytes() == b"video-original"
    assert (source / "perfil_youtube" / "Default" / "Cookies").read_bytes() == b"profile-original"
    assert not destination.exists()
    assert not (paths.accounts / ".migration_staging" / "youtube" / "Canal").exists()


def test_staging_incompleto_de_crash_nunca_vira_conta_valida(monkeypatch, tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "contas" / "youtube" / "Canal"
    _write_account_tree(source)
    paths = app_paths.build_app_paths(install_root=install, data_root=data)
    stale = paths.accounts / ".migration_staging" / "youtube" / "Canal"
    stale.mkdir(parents=True)
    (stale / "config_canal.json").write_text("PARCIAL", encoding="utf-8")
    destination = paths.accounts / "youtube" / "Canal"

    original_replace = Path.replace

    def cross_volume_for_source(self, target):
        if self == source:
            raise OSError("cross-device")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", cross_volume_for_source)
    monkeypatch.setattr(app_paths.shutil, "copytree", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash")))

    report = app_paths.initialize_app_storage(paths)

    assert not report.ok
    assert source.exists()
    assert not destination.exists()
    assert not stale.exists()
    assert not any(p.name == "Canal" for p in (paths.accounts / "youtube").iterdir())


def test_segunda_execucao_apos_migracao_transacional_e_idempotente(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "contas" / "youtube" / "Canal"
    _write_account_tree(source)
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    first = app_paths.initialize_app_storage(paths)
    snapshot = {
        p.relative_to(paths.accounts).as_posix(): p.read_bytes()
        for p in paths.accounts.rglob("*")
        if p.is_file()
    }
    second = app_paths.initialize_app_storage(paths)
    snapshot_after = {
        p.relative_to(paths.accounts).as_posix(): p.read_bytes()
        for p in paths.accounts.rglob("*")
        if p.is_file()
    }

    assert first.ok and first.changed
    assert second.ok and not second.changed
    assert snapshot_after == snapshot


def test_removidas_com_conflito_tambem_nao_mescla_unidade(tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "_removidas" / "youtube" / "Canal_20260101"
    destination = data / "backups" / "removed_accounts" / "youtube" / "Canal_20260101"
    _write_account_tree(source, config="LEGADO", video=b"legacy", profile=b"legacy-profile")
    destination.mkdir(parents=True)
    (destination / "config_canal.json").write_text("OUTRO", encoding="utf-8")
    paths = app_paths.build_app_paths(install_root=install, data_root=data)

    report = app_paths.initialize_app_storage(paths)

    assert not report.ok
    assert source.exists()
    assert (source / "videos" / "001.mp4").exists()
    assert not (destination / "videos").exists()


def test_account_dir_from_env_sem_contexto_exige_namespace(monkeypatch):
    monkeypatch.delenv("ACCOUNT_DIR", raising=False)
    monkeypatch.delenv("YT_CHANNEL_DIR", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="standalone_namespace"):
        app_paths.account_dir_from_env()


def test_fallback_standalone_e_isolado_por_engine(monkeypatch):
    monkeypatch.delenv("ACCOUNT_DIR", raising=False)
    monkeypatch.delenv("YT_CHANNEL_DIR", raising=False)

    youtube = app_paths.account_dir_from_env(standalone_namespace="agendar_youtube")
    tiktok = app_paths.account_dir_from_env(standalone_namespace="agendar_tiktok")

    assert youtube != tiktok
    assert youtube == (app_paths.PATHS.accounts / "_standalone" / "agendar_youtube").resolve()
    assert tiktok == (app_paths.PATHS.accounts / "_standalone" / "agendar_tiktok").resolve()


def test_account_dir_explicito_continua_tendo_precedencia_sobre_namespace(monkeypatch, tmp_path):
    account = tmp_path / "conta-real"
    monkeypatch.setenv("ACCOUNT_DIR", str(account))
    assert app_paths.account_dir_from_env(standalone_namespace="agendar_tiktok") == account.resolve()


def test_copia_cross_volume_publica_conta_so_apos_validacao(monkeypatch, tmp_path):
    install = tmp_path / "instalado"
    data = tmp_path / "dados"
    source = install / "contas" / "youtube" / "Canal"
    destination = data / "accounts" / "youtube" / "Canal"
    _write_account_tree(source, config="CFG", video=b"video-cross", profile=b"profile-cross")
    paths = app_paths.build_app_paths(install_root=install, data_root=data)
    original_replace = Path.replace

    def cross_volume_for_source(self, target):
        if self == source and Path(target) == destination:
            raise OSError("cross-device")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", cross_volume_for_source)

    report = app_paths.initialize_app_storage(paths)

    assert report.ok and report.changed
    assert not source.exists()
    assert (destination / "config_canal.json").read_text(encoding="utf-8") == "CFG"
    assert (destination / "videos" / "001.mp4").read_bytes() == b"video-cross"
    assert (destination / "perfil_youtube" / "Default" / "Cookies").read_bytes() == b"profile-cross"
    assert not (paths.accounts / ".migration_staging").exists()


def test_engines_youtube_e_tiktok_nao_compartilham_fallback_standalone(tmp_path):
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env.pop("ACCOUNT_DIR", None)
    env.pop("YT_CHANNEL_DIR", None)
    env["PAINEL_DATA_ROOT"] = str(tmp_path / "dados")
    code = (
        "from _sistema import agendar_youtube as y, agendar_tiktok as t; "
        "print(y.BASE); print(t.BASE); assert y.BASE != t.BASE"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    youtube_path = Path(lines[0])
    tiktok_path = Path(lines[1])
    assert youtube_path.parts[-3:] == ("accounts", "_standalone", "agendar_youtube")
    assert tiktok_path.parts[-3:] == ("accounts", "_standalone", "agendar_tiktok")
    assert youtube_path != tiktok_path
