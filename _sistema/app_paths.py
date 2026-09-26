# -*- coding: utf-8 -*-
"""Caminhos centrais do aplicativo e migração do storage legado.

O código instalado e os dados mutáveis são deliberadamente separados. Por padrão,
os dados vivem em ``%LOCALAPPDATA%\\PainelOficial`` no Windows. O override
``PAINEL_DATA_ROOT`` existe para testes, builds portáteis controlados e diagnóstico.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import shutil
from typing import Iterable

try:
    from .time_utils import utc_now_iso
except ImportError:
    from time_utils import utc_now_iso

PRODUCT_DATA_DIR_NAME = "PainelOficial"
DATA_ROOT_ENV = "PAINEL_DATA_ROOT"
STORAGE_LAYOUT_VERSION = 1


@dataclass(frozen=True)
class AccountPaths:
    root: Path
    data: Path
    videos: Path
    logs: Path
    config: Path
    profile_youtube: Path
    profile_tiktok: Path
    blocked: Path


@dataclass(frozen=True)
class AppPaths:
    install_root: Path
    system: Path
    data_root: Path
    database: Path
    accounts: Path
    projects: Path
    cache: Path
    logs: Path
    temp: Path
    templates: Path
    backups: Path
    models: Path
    support: Path
    removed_accounts: Path
    migration_state: Path
    legacy_accounts: Path
    legacy_removed_accounts: Path
    # PROMPT 24 (Source Import Manager): diretório gerenciado dedicado a
    # ``SourceAsset`` (tabela ``sources``, ``_sistema/domain/models.py``).
    # Não existia nenhum diretório assim antes deste Prompt -- só é
    # necessário para conteúdo que NÃO tem um "arquivo original local" já
    # existente em algum lugar do disco do usuário: hoje, exclusivamente o
    # destino do que ``UrlImporter`` baixa de uma URL (ver
    # ``_sistema/source_import.py``). ``LocalFileImporter``/
    # ``FolderImporter`` deliberadamente REFERENCIAM o arquivo onde já
    # está (nunca copiam para cá) -- ver justificativa no módulo.
    sources: Path

    def mutable_dirs(self) -> tuple[Path, ...]:
        return (
            self.database,
            self.accounts,
            self.projects,
            self.cache,
            self.logs,
            self.temp,
            self.templates,
            self.backups,
            self.models,
            self.support,
            self.removed_accounts,
            self.sources,
        )


@dataclass(frozen=True)
class MigrationReport:
    moved: tuple[str, ...] = ()
    duplicate_removed: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.duplicate_removed)

    @property
    def ok(self) -> bool:
        return not self.conflicts


def _default_data_root(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = str(env.get(DATA_ROOT_ENV, "")).strip()
    if override:
        return Path(override).expanduser().resolve()

    local_app_data = str(env.get("LOCALAPPDATA", "")).strip()
    if local_app_data:
        return (Path(local_app_data).expanduser() / PRODUCT_DATA_DIR_NAME).resolve()

    # Fallback para desenvolvimento/testes fora do Windows. A release Windows usa
    # LOCALAPPDATA; esse fallback evita gravar dados ao lado do código em outros SOs.
    xdg_data_home = str(env.get("XDG_DATA_HOME", "")).strip()
    if xdg_data_home:
        return (Path(xdg_data_home).expanduser() / PRODUCT_DATA_DIR_NAME).resolve()
    return (Path.home() / ".local" / "share" / PRODUCT_DATA_DIR_NAME).resolve()


def build_app_paths(
    *,
    install_root: Path | str | None = None,
    data_root: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> AppPaths:
    install = Path(install_root).expanduser().resolve() if install_root else Path(__file__).resolve().parent.parent
    mutable = Path(data_root).expanduser().resolve() if data_root else _default_data_root(env)
    backups = mutable / "backups"
    support = mutable / "support"
    return AppPaths(
        install_root=install,
        system=install / "_sistema",
        data_root=mutable,
        database=mutable / "database",
        accounts=mutable / "accounts",
        projects=mutable / "projects",
        cache=mutable / "cache",
        logs=mutable / "logs",
        temp=mutable / "temp",
        templates=mutable / "templates",
        backups=backups,
        models=mutable / "models",
        support=support,
        removed_accounts=backups / "removed_accounts",
        migration_state=support / "storage_migration_v1.json",
        legacy_accounts=install / "contas",
        legacy_removed_accounts=install / "_removidas",
        sources=mutable / "sources",
    )


def account_paths(root: Path | str) -> AccountPaths:
    root = Path(root).expanduser().resolve()
    return AccountPaths(
        root=root,
        data=root / "dados",
        videos=root / "videos",
        logs=root / "logs",
        config=root / "config_canal.json",
        profile_youtube=root / "perfil_youtube",
        profile_tiktok=root / "perfil_tiktok",
        blocked=root / "bloqueados",
    )


def account_dir_from_env(*extra_keys: str, standalone_namespace: str | None = None) -> Path:
    """Resolve o diretório da conta sem compartilhar fallback entre engines.

    O painel normal sempre fornece ``ACCOUNT_DIR``. O fallback existe apenas para
    preservar execução/importação standalone legada; ele é obrigatoriamente
    isolado por ferramenta. Assim, dois engines nunca caem silenciosamente no
    mesmo root mutável.
    """
    for key in ("ACCOUNT_DIR", *extra_keys):
        raw = str(os.environ.get(key, "")).strip()
        if raw:
            return Path(raw).expanduser().resolve()
    namespace = str(standalone_namespace or "").strip()
    if not namespace:
        raise RuntimeError(
            "ACCOUNT_DIR não foi informado. Execução standalone exige "
            "standalone_namespace explícito para impedir compartilhamento acidental de dados."
        )
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in namespace).strip("._")
    if not safe:
        raise RuntimeError("standalone_namespace inválido para isolamento do storage.")
    return (PATHS.accounts / "_standalone" / safe).resolve()



def validate_storage_separation(paths: AppPaths | None = None) -> None:
    paths = paths or PATHS
    install = paths.install_root.resolve()
    data = paths.data_root.resolve()
    if data == install or install in data.parents or data in install.parents:
        raise RuntimeError(
            "A raiz de dados mutáveis deve ficar separada da pasta de instalação. "
            f"install={install} data={data}"
        )

def ensure_app_directories(paths: AppPaths | None = None) -> None:
    paths = paths or PATHS
    paths.data_root.mkdir(parents=True, exist_ok=True)
    for directory in paths.mutable_dirs():
        directory.mkdir(parents=True, exist_ok=True)


def _files_equal(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        ha = hashlib.sha256()
        hb = hashlib.sha256()
        with a.open("rb") as fa, b.open("rb") as fb:
            while True:
                ba = fa.read(1024 * 1024)
                bb = fb.read(1024 * 1024)
                if ba != bb:
                    return False
                if not ba:
                    return True
                ha.update(ba)
                hb.update(bb)
        return ha.digest() == hb.digest()
    except OSError:
        return False


def _rel_label(source_root: Path, path: Path, prefix: str) -> str:
    try:
        rel = path.relative_to(source_root)
    except ValueError:
        rel = Path(path.name)
    return f"{prefix}/{rel.as_posix()}"


def _move_file_safely(source: Path, destination: Path) -> bool:
    """Move um arquivo sem remover a origem antes do commit do destino."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # No mesmo volume, replace/rename é atômico e não duplica o arquivo.
        source.replace(destination)
        return True
    except OSError:
        pass

    # Entre volumes: verifica espaço, copia para temporário, valida e só então publica + remove origem.
    try:
        required = max(0, source.stat().st_size)
        free = shutil.disk_usage(destination.parent).free
        if free < required:
            return False
    except OSError:
        return False

    temp = destination.with_name(destination.name + ".migration_tmp")
    try:
        if temp.exists():
            if temp.is_file() or temp.is_symlink():
                temp.unlink()
            else:
                shutil.rmtree(temp)
        shutil.copy2(source, temp, follow_symlinks=False)
        if source.is_file() and not _files_equal(source, temp):
            raise OSError("cópia de migração não passou na validação")
        temp.replace(destination)
        source.unlink()
        return True
    except Exception:
        try:
            if temp.exists():
                if temp.is_file() or temp.is_symlink():
                    temp.unlink()
                else:
                    shutil.rmtree(temp)
        except OSError:
            pass
        return False


def _node_equal(source: Path, destination: Path) -> bool:
    """Compara dois nós sem seguir symlinks; falha fechada em qualquer erro."""
    try:
        if source.is_symlink() or destination.is_symlink():
            return source.is_symlink() and destination.is_symlink() and os.readlink(source) == os.readlink(destination)
        if source.is_file() or destination.is_file():
            return source.is_file() and destination.is_file() and _files_equal(source, destination)
        if not source.is_dir() or not destination.is_dir():
            return False
        src_children = {child.name: child for child in source.iterdir()}
        dst_children = {child.name: child for child in destination.iterdir()}
        if src_children.keys() != dst_children.keys():
            return False
        return all(_node_equal(src_children[name], dst_children[name]) for name in src_children)
    except OSError:
        return False


def _tree_size(root: Path) -> int:
    """Soma bytes de arquivos regulares sem seguir symlinks."""
    total = 0
    try:
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            # Evita contabilizar diretórios que sejam symlinks como subárvores.
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
            for name in files:
                path = current_path / name
                if path.is_symlink():
                    continue
                total += path.stat().st_size
    except OSError:
        return -1
    return total


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _cleanup_staging(path: Path) -> None:
    try:
        _remove_path(path)
    except OSError:
        pass


def _retire_source_unit(source: Path) -> bool:
    """Retira uma unidade legada do caminho ativo antes de apagá-la.

    O rename local torna o desaparecimento da conta atômico. Se o processo cair
    durante o rmtree posterior, sobra apenas um tombstone oculto; o destino já foi
    validado e publicado integralmente.
    """
    tombstone = source.with_name(f".{source.name}.migration_cleanup")
    try:
        if tombstone.exists() or tombstone.is_symlink():
            _remove_path(tombstone)
        source.replace(tombstone)
    except OSError:
        return False
    try:
        _remove_path(tombstone)
    except OSError:
        # O destino completo já é a fonte válida; o tombstone pode ser limpo numa
        # execução posterior sem reaparecer como conta legada.
        pass
    return True


def _copy_unit_transactionally(source: Path, destination: Path, staging: Path) -> bool:
    """Copia uma conta inteira para staging, valida e só então publica o destino."""
    _cleanup_staging(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    required = _tree_size(source)
    if required < 0:
        return False
    try:
        if shutil.disk_usage(staging.parent).free < required:
            return False
    except OSError:
        return False

    try:
        shutil.copytree(source, staging, symlinks=True, copy_function=shutil.copy2)
        if not _node_equal(source, staging):
            raise OSError("cópia transacional não passou na validação integral")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(str(destination))
        staging.replace(destination)
    except Exception:
        _cleanup_staging(staging)
        return False

    # A origem só é retirada depois que o diretório final completo foi publicado.
    if _node_equal(source, destination):
        _retire_source_unit(source)
    return True


def _migrate_unit(source: Path, destination: Path, staging: Path, label: str) -> MigrationReport:
    """Migra uma unidade de conta sem nunca mesclar árvores parciais."""
    if destination.exists() or destination.is_symlink():
        if _node_equal(source, destination):
            if _retire_source_unit(source):
                return MigrationReport(duplicate_removed=(label,))
            return MigrationReport(conflicts=(label,))
        return MigrationReport(conflicts=(label,))

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Mesmo volume: rename da conta inteira como unidade.
        source.replace(destination)
        return MigrationReport(moved=(label,))
    except OSError:
        pass

    # Outro volume (ou rename indisponível): staging integral no filesystem destino.
    if _copy_unit_transactionally(source, destination, staging):
        return MigrationReport(moved=(label,))
    return MigrationReport(conflicts=(label,))


def _migrate_tree(source: Path, destination: Path, prefix: str) -> MigrationReport:
    """Migra storage legado de forma transacional por conta/unidade.

    A árvore esperada é ``<raiz>/<plataforma>/<conta>``. O diretório final de uma
    conta nunca é criado parcialmente: quando ausente, a unidade inteira é movida
    por rename ou copiada para staging + validada antes do commit. Quando o destino
    já existe, árvores diferentes são preservadas integralmente dos dois lados.
    """
    if not source.exists():
        return MigrationReport()

    destination.mkdir(parents=True, exist_ok=True)
    staging_root = destination / ".migration_staging"
    # Staging é privado da migração e nunca fica sob accounts/<plataforma>/.
    # Restos de crash não representam conta válida e podem ser descartados antes
    # de uma nova tentativa, pois a origem ainda não foi removida antes do commit.
    _cleanup_staging(staging_root)

    reports: list[MigrationReport] = []
    for platform_source in sorted(source.iterdir(), key=lambda p: p.name.casefold()):
        # Tombstones são resíduos seguros de uma origem que já tem destino válido.
        if platform_source.name.startswith(".") and platform_source.name.endswith(".migration_cleanup"):
            _cleanup_staging(platform_source)
            continue
        if not platform_source.is_dir() or platform_source.is_symlink():
            reports.append(MigrationReport(conflicts=(_rel_label(source, platform_source, prefix),)))
            continue

        platform_destination = destination / platform_source.name
        platform_destination.mkdir(parents=True, exist_ok=True)
        platform_staging = staging_root / platform_source.name

        for unit_source in sorted(platform_source.iterdir(), key=lambda p: p.name.casefold()):
            if unit_source.name.startswith(".") and unit_source.name.endswith(".migration_cleanup"):
                _cleanup_staging(unit_source)
                continue
            label = _rel_label(source, unit_source, prefix)
            if not unit_source.is_dir() or unit_source.is_symlink():
                reports.append(MigrationReport(conflicts=(label,)))
                continue
            reports.append(
                _migrate_unit(
                    unit_source,
                    platform_destination / unit_source.name,
                    platform_staging / unit_source.name,
                    label,
                )
            )

        try:
            platform_source.rmdir()
        except OSError:
            pass

    _cleanup_staging(staging_root)
    try:
        source.rmdir()
    except OSError:
        pass
    return _merge_reports(reports)

def _merge_reports(reports: Iterable[MigrationReport]) -> MigrationReport:
    moved: list[str] = []
    duplicates: list[str] = []
    conflicts: list[str] = []
    for report in reports:
        moved.extend(report.moved)
        duplicates.extend(report.duplicate_removed)
        conflicts.extend(report.conflicts)
    return MigrationReport(tuple(moved), tuple(duplicates), tuple(conflicts))


def _write_migration_state(paths: AppPaths, report: MigrationReport) -> None:
    paths.support.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STORAGE_LAYOUT_VERSION,
        "updated_at": utc_now_iso(),
        "status": "ok" if report.ok else "conflicts",
        "moved_count": len(report.moved),
        "duplicate_removed_count": len(report.duplicate_removed),
        "conflicts": list(report.conflicts),
    }
    tmp = paths.migration_state.with_suffix(paths.migration_state.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(paths.migration_state)


def migrate_legacy_storage(paths: AppPaths | None = None) -> MigrationReport:
    paths = paths or PATHS
    ensure_app_directories(paths)
    report = _merge_reports(
        (
            _migrate_tree(paths.legacy_accounts, paths.accounts, "contas"),
            _migrate_tree(paths.legacy_removed_accounts, paths.removed_accounts, "_removidas"),
        )
    )
    _write_migration_state(paths, report)
    return report


def initialize_app_storage(paths: AppPaths | None = None) -> MigrationReport:
    """Cria layout mutável e executa migração idempotente do layout legado."""
    paths = paths or PATHS
    validate_storage_separation(paths)
    ensure_app_directories(paths)
    return migrate_legacy_storage(paths)


PATHS = build_app_paths()
