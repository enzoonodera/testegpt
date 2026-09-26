# -*- coding: utf-8 -*-
"""PROMPT 25 -- Source Context Resolver: testes.

Cobre: classificação de LocalFileImporter/FolderImporter/UrlImporter;
correspondência heurística de conta conectada (exatamente uma, nunca
ambígua); reclassificação (substitui, nunca acumula); isolamento de falha
do resolver; garantias estruturais (AST) de ausência de rede, ausência de
import de módulos protegidos, ausência de criação de outras entidades e
ausência de qualquer fluxo de confirmação de propriedade.
"""
from __future__ import annotations

import ast
import http.server
import inspect
import threading
from pathlib import Path

import pytest

import _sistema.source_context as source_context
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Account, Job, Project, Publication, Schedule, SourceAsset, Video
from _sistema.source_context import (
    CONTEXT_CONNECTED_CHANNEL_CONTENT,
    CONTEXT_EXTERNAL_CONTENT,
    CONTEXT_LOCAL_CONTENT,
    CONTEXT_UNKNOWN_SOURCE,
    SourceContextResolver,
)
from _sistema.source_import import FolderImporter, LocalFileImporter, SourceImportManager, UrlImporter
from _sistema.storage.database import LocalDatabase


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_source_import.py/test_source_import_options.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_paths(tmp_path):
    paths = build_app_paths(data_root=tmp_path / "data")
    ensure_app_directories(paths)
    return paths


@pytest.fixture
def database(app_paths):
    db = LocalDatabase(app_paths.database / "painel.db")
    db.initialize()
    return db


@pytest.fixture
def manager(database, app_paths):
    return SourceImportManager(database, app_paths)


@pytest.fixture
def resolver(database):
    return SourceContextResolver(database)


def _write_video(path: Path, content: bytes = b"conteudo-ficticio-de-video") -> Path:
    path.write_bytes(content)
    return path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture
def http_server(tmp_path):
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()

    def handler_factory(*args, **kwargs):
        return _QuietHandler(*args, directory=str(serve_dir), **kwargs)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_factory)
    port = httpd.server_port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield serve_dir, f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _account(platform: str, name: str, *, local_key: str | None = None, enabled: bool = True) -> Account:
    return Account(platform=platform, name=name, local_key=local_key, enabled=enabled)


# ---------------------------------------------------------------------------
# 1. LocalFileImporter/FolderImporter -> sempre LOCAL_CONTENT
# ---------------------------------------------------------------------------


def test_local_file_importer_classifica_local_content(manager, database, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video)
    assert result.success is True
    context = manager.get_context(result.source_asset.id)
    assert context is not None
    assert context.context == CONTEXT_LOCAL_CONTENT


def test_folder_importer_classifica_todos_como_local_content(manager, database, tmp_path):
    folder = tmp_path / "pasta"
    folder.mkdir()
    _write_video(folder / "a.mp4")
    _write_video(folder / "b.mov", b"outro-conteudo")
    results = manager.import_folder(folder)
    successes = [r for r in results if r.success]
    assert len(successes) == 2
    for r in successes:
        context = manager.get_context(r.source_asset.id)
        assert context is not None
        assert context.context == CONTEXT_LOCAL_CONTENT


# ---------------------------------------------------------------------------
# 2. UrlImporter -- domínio reconhecido + conta batendo -> CONNECTED_CHANNEL_CONTENT
# ---------------------------------------------------------------------------


def test_url_importer_dominio_reconhecido_uma_conta_batendo_connected_channel_content(
    manager, database, http_server, monkeypatch
):
    serve_dir, base_url = http_server
    (serve_dir / "clip.mp4").write_bytes(b"conteudo-de-url")

    account = _account("youtube", "MeuCanal")
    database.insert(account)

    # A URL real servida é o servidor HTTP local efêmero (nunca a internet
    # real); simulamos o host reconhecido reescrevendo RECOGNIZED_PLATFORM_DOMAINS
    # do módulo para apontar o host efêmero de teste para "youtube" -- isto
    # mantém o teste hermético (sem rede real) enquanto exercita a regra de
    # correspondência de domínio real do resolver.
    from urllib.parse import urlparse as _urlparse

    fake_host = _urlparse(base_url).hostname
    monkeypatch.setitem(source_context.RECOGNIZED_PLATFORM_DOMAINS, fake_host, "youtube")

    url = f"{base_url}/@MeuCanal/clip.mp4"
    # UrlImporter exige extensão reconhecida no nome do arquivo extraído do
    # path -- usamos um path cujo último segmento tem extensão válida.
    result = manager.import_url(f"{base_url}/clip.mp4")
    assert result.success is True

    # Classificação foi feita a partir do source_uri real (sem handle no
    # path de download) -- para testar a extração de handle, classificamos
    # diretamente via resolver com um SourceAsset cujo source_uri contém o
    # handle, mantendo a parte de rede isolada ao download em si.
    handle_asset = SourceAsset(source_uri=url)
    database.insert(handle_asset)
    resolver = SourceContextResolver(database)
    context = resolver.resolve_and_persist(handle_asset.id)
    assert context == CONTEXT_CONNECTED_CHANNEL_CONTENT


def test_dominio_reconhecido_sem_nenhuma_account_cadastrada_external_content(database):
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https://www.youtube.com/@CanalQualquer")
    database.insert(asset)
    assert resolver.resolve_and_persist(asset.id) == CONTEXT_EXTERNAL_CONTENT


def test_dominio_reconhecido_duas_accounts_batendo_ambiguamente_external_content(database):
    database.insert(_account("youtube", "CanalDuplicado"))
    database.insert(_account("youtube", "CanalDuplicado"))
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https://www.youtube.com/@CanalDuplicado")
    database.insert(asset)
    assert resolver.resolve_and_persist(asset.id) == CONTEXT_EXTERNAL_CONTENT


def test_account_desabilitada_nunca_conta_como_conectada(database):
    database.insert(_account("youtube", "CanalDesabilitado", enabled=False))
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https://www.youtube.com/@CanalDesabilitado")
    database.insert(asset)
    assert resolver.resolve_and_persist(asset.id) == CONTEXT_EXTERNAL_CONTENT


def test_conta_de_outra_plataforma_nao_bate(database):
    database.insert(_account("tiktok", "MeuCanal"))
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https://www.youtube.com/@MeuCanal")
    database.insert(asset)
    assert resolver.resolve_and_persist(asset.id) == CONTEXT_EXTERNAL_CONTENT


def test_match_via_local_key_sufixo(database):
    database.insert(_account("tiktok", "Nome Diferente", local_key="tiktok/handle_real"))
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https://www.tiktok.com/@handle_real")
    database.insert(asset)
    assert resolver.resolve_and_persist(asset.id) == CONTEXT_CONNECTED_CHANNEL_CONTENT


# ---------------------------------------------------------------------------
# 3. Host não reconhecido -> EXTERNAL_CONTENT
# ---------------------------------------------------------------------------


def test_host_nao_reconhecido_external_content(database):
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https://exemplo-generico.com/video.mp4")
    database.insert(asset)
    assert resolver.resolve_and_persist(asset.id) == CONTEXT_EXTERNAL_CONTENT


def test_dominio_reconhecido_sem_handle_extraivel_external_content(database):
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https://www.youtube.com/watch?v=abc123")
    database.insert(asset)
    assert resolver.resolve_and_persist(asset.id) == CONTEXT_EXTERNAL_CONTENT


# ---------------------------------------------------------------------------
# 4. source_uri malformado -> UNKNOWN_SOURCE
# ---------------------------------------------------------------------------


def test_source_uri_malformado_sem_host_unknown_source(database):
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https:///caminho-sem-host")
    database.insert(asset)
    assert resolver.resolve_and_persist(asset.id) == CONTEXT_UNKNOWN_SOURCE


def test_source_asset_inexistente_devolve_none(database):
    resolver = SourceContextResolver(database)
    assert resolver.resolve_and_persist("11111111-1111-1111-1111-111111111111") is None
    assert resolver.get_current("11111111-1111-1111-1111-111111111111") is None


# ---------------------------------------------------------------------------
# 5. Reclassificação substitui, nunca acumula
# ---------------------------------------------------------------------------


def test_reclassificacao_substitui_classificacao_anterior_nunca_acumula(database):
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="https://www.youtube.com/@CanalNovo")
    database.insert(asset)

    primeira = resolver.resolve_and_persist(asset.id)
    assert primeira == CONTEXT_EXTERNAL_CONTENT

    database.insert(_account("youtube", "CanalNovo"))
    segunda = resolver.resolve_and_persist(asset.id)
    assert segunda == CONTEXT_CONNECTED_CHANNEL_CONTENT

    current = resolver.get_current(asset.id)
    assert current is not None
    assert current.context == CONTEXT_CONNECTED_CHANNEL_CONTENT

    with database.connection() as conn:
        rows = conn.execute(
            "SELECT context FROM source_asset_context WHERE source_asset_id = ? ORDER BY rowid",
            (asset.id,),
        ).fetchall()
    # Ambos os eventos permanecem no histórico append-only (nunca apagados),
    # mas o estado ATUAL derivado é só a última classificação -- nunca as
    # duas simultaneamente.
    assert [row["context"] for row in rows] == [CONTEXT_EXTERNAL_CONTENT, CONTEXT_CONNECTED_CHANNEL_CONTENT]
    assert current.context != primeira


# ---------------------------------------------------------------------------
# 6. Isolamento -- falha do resolver nunca derruba a importação
# ---------------------------------------------------------------------------


def test_falha_inesperada_do_resolver_nao_derruba_importacao(manager, database, tmp_path, monkeypatch):
    def _quebra(self, source_asset_id):
        raise RuntimeError("falha simulada de classificacao")

    monkeypatch.setattr(SourceContextResolver, "resolve_and_persist", _quebra)

    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video)

    assert result.success is True
    assert result.source_asset is not None
    reloaded = database.get(SourceAsset, result.source_asset.id)
    assert reloaded is not None
    # Nenhuma classificação foi gravada (estado "ausente" -- distinto de
    # UNKNOWN_SOURCE explícito).
    assert manager.get_context(result.source_asset.id) is None


def test_folder_importer_falha_de_classificacao_em_um_arquivo_nao_afeta_os_demais(
    manager, database, tmp_path, monkeypatch
):
    folder = tmp_path / "pasta"
    folder.mkdir()
    _write_video(folder / "a.mp4")
    _write_video(folder / "b.mp4", b"outro-conteudo")

    original = SourceContextResolver.resolve_and_persist
    calls = {"count": 0}

    def _quebra_no_primeiro(self, source_asset_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("falha simulada no primeiro arquivo")
        return original(self, source_asset_id)

    monkeypatch.setattr(SourceContextResolver, "resolve_and_persist", _quebra_no_primeiro)

    results = manager.import_folder(folder)
    successes = [r for r in results if r.success]
    assert len(successes) == 2

    contexts = [manager.get_context(r.source_asset.id) for r in successes]
    values = [c.context if c is not None else None for c in contexts]
    # Exatamente um "ausente" (o que quebrou) e um LOCAL_CONTENT (o que
    # classificou normalmente) -- isolamento por item confirmado.
    assert values.count(None) == 1
    assert values.count(CONTEXT_LOCAL_CONTENT) == 1


# ---------------------------------------------------------------------------
# 7. Garantias estruturais (AST) -- rede, módulos protegidos, outras entidades
# ---------------------------------------------------------------------------


def test_nenhuma_importacao_de_biblioteca_de_rede():
    tree = ast.parse(Path(source_context.__file__).read_text(encoding="utf-8"))
    proibidos = ("urllib.request", "http.client", "socket", "requests", "httpx", "ftplib")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for termo in proibidos:
            assert termo not in names.lower(), f"import de rede proibido encontrado: {names!r}"


def test_source_context_nao_importa_modulos_protegidos():
    tree = ast.parse(Path(source_context.__file__).read_text(encoding="utf-8"))
    proibidos = (
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "job_state_machine",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for termo in proibidos:
            assert termo not in names.lower()


def test_nenhum_job_publication_schedule_project_ou_video_criado_por_codigo_novo():
    source = Path(source_context.__file__).read_text(encoding="utf-8")
    for termo in ("Job(", "Publication(", "Schedule(", "Project(", "Video("):
        assert termo not in source


def test_resolver_nao_cria_nenhuma_outra_entidade_no_banco(manager, database, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    manager.import_local_file(video)
    assert database.list(Job) == []
    assert database.list(Publication) == []
    assert database.list(Schedule) == []
    assert database.list(Project) == []
    assert database.list(Video) == []


def test_nenhum_fluxo_de_confirmacao_de_propriedade_no_codigo():
    """Garantia por ausência, verificada em código real (AST), não em texto
    de docstring -- a docstring do módulo CITA propositalmente a pergunta
    proibida ("esse vídeo é seu?") em prosa, na seção que documenta
    justamente esta garantia (mesma lição já aplicada ao teste equivalente
    de ``source_import.py``/Prompt 24b: um grep textual ingênuo colidiria
    com a própria documentação). Aqui a checagem inspeciona: (a) nós AST
    reais de chamada a ``input(...)``/``sys.stdin`` -- nunca a docstring;
    (b) nomes de identificador reais (``def``/atribuição) contendo termos
    de confirmação de propriedade; (c) assinaturas de função reais via
    ``inspect``."""
    tree = ast.parse(Path(source_context.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "input":
                raise AssertionError("chamada a input() encontrada -- fluxo interativo proibido")
            if isinstance(func, ast.Attribute) and func.attr in ("readline", "read"):
                # sys.stdin.readline()/sys.stdin.read() seriam o padrão de
                # leitura interativa via atributo -- nenhuma ocorrência
                # esperada neste módulo.
                if isinstance(func.value, ast.Attribute) and func.value.attr == "stdin":
                    raise AssertionError("leitura de sys.stdin encontrada -- fluxo interativo proibido")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lowered = node.name.lower()
            for termo in ("confirma_propriedade", "e_seu_video", "confirm_ownership"):
                assert termo not in lowered, f"função com nome suspeito de confirmação de propriedade: {node.name}"

    for name, obj in inspect.getmembers(source_context):
        if inspect.isfunction(obj) or inspect.ismethod(obj):
            if obj.__module__ != source_context.__name__:
                continue
            signature = inspect.signature(obj)
            for param in signature.parameters.values():
                lowered = param.name.lower()
                assert "confirm" not in lowered
                assert "ownership" not in lowered
                assert "propriedade" not in lowered


# ---------------------------------------------------------------------------
# 8. Vocabulário -- exatamente as quatro constantes do roadmap
# ---------------------------------------------------------------------------


def test_vocabulario_exato_das_quatro_constantes():
    from _sistema.source_context import CONTEXTS

    assert CONTEXTS == {
        "CONNECTED_CHANNEL_CONTENT",
        "EXTERNAL_CONTENT",
        "LOCAL_CONTENT",
        "UNKNOWN_SOURCE",
    }


def test_persist_rejeita_context_desconhecido(database):
    resolver = SourceContextResolver(database)
    asset = SourceAsset(source_uri="/tmp/qualquer.mp4")
    database.insert(asset)
    with pytest.raises(ValueError):
        resolver._persist(asset.id, "NAO_EXISTE")


def test_resolve_and_persist_rejeita_uuid_invalido(database):
    resolver = SourceContextResolver(database)
    with pytest.raises(ValueError):
        resolver.resolve_and_persist("nao-e-um-uuid")


# ---------------------------------------------------------------------------
# 9. GATE adversarial -- concorrência (item 6) e restart (item 4)
# ---------------------------------------------------------------------------


def test_duas_instancias_concorrentes_classificando_o_mesmo_source_asset(app_paths, database):
    """Duas threads reais, sincronizadas por Barrier (determinístico, nunca
    probabilístico -- GATE 6), chamando ``resolve_and_persist`` para o
    MESMO ``source_asset_id`` ao mesmo tempo contra o mesmo SQLite. Nunca
    deve haver crash/corrupção -- o resultado final é sempre um dos dois
    valores calculados, nunca um estado parcial."""
    asset = SourceAsset(source_uri="/tmp/video_concorrente.mp4")
    database.insert(asset)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    results: list[str | None] = []

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_resolver = SourceContextResolver(db_instance)
        barrier.wait(timeout=5)
        try:
            results.append(local_resolver.resolve_and_persist(asset.id))
        except BaseException as exc:  # pragma: no cover - só se algo quebrar
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert results == [CONTEXT_LOCAL_CONTENT, CONTEXT_LOCAL_CONTENT]

    resolver = SourceContextResolver(database)
    current = resolver.get_current(asset.id)
    assert current is not None
    assert current.context == CONTEXT_LOCAL_CONTENT

    with database.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM source_asset_context WHERE source_asset_id = ?",
            (asset.id,),
        ).fetchone()["n"]
    # As duas chamadas concorrentes produziram duas linhas no histórico
    # append-only -- nenhuma foi perdida, nenhum crash, nenhum conflito.
    assert count == 2


def test_restart_com_novas_instancias_preserva_classificacao(app_paths):
    """GATE 4 (restart): reabre com NOVAS instâncias de LocalDatabase e
    SourceContextResolver -- nunca reutiliza objetos em memória."""
    db1 = LocalDatabase(app_paths.database / "painel.db")
    db1.initialize()
    resolver1 = SourceContextResolver(db1)
    asset = SourceAsset(source_uri="https://www.youtube.com/@CanalRestart")
    db1.insert(asset)
    database_account = Account(platform="youtube", name="CanalRestart")
    db1.insert(database_account)
    context1 = resolver1.resolve_and_persist(asset.id)
    assert context1 == CONTEXT_CONNECTED_CHANNEL_CONTENT

    # Nova instância de LocalDatabase e de SourceContextResolver --
    # simula reabrir o produto após reinicialização/crash.
    db2 = LocalDatabase(app_paths.database / "painel.db")
    resolver2 = SourceContextResolver(db2)
    current = resolver2.get_current(asset.id)
    assert current is not None
    assert current.context == CONTEXT_CONNECTED_CHANNEL_CONTENT
