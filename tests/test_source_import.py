# -*- coding: utf-8 -*-
"""PROMPT 24 -- Source Import Manager: testes.

Testes de ``UrlImporter`` usam um servidor HTTP LOCAL efêmero
(``http.server``/``http.client`` em ``tmp_path``, porta 0 -- o SO escolhe
uma porta livre) -- nunca a internet real, para garantir testes
herméticos e determinísticos.
"""
from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

import _sistema.source_import as source_import
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import SourceAsset
from _sistema.source_import import (
    ERROR_ARQUIVO_INEXISTENTE,
    ERROR_ARQUIVO_VAZIO,
    ERROR_CONTEUDO_VAZIO,
    ERROR_EXTENSAO_INVALIDA,
    ERROR_NOME_ARQUIVO_NAO_RECONHECIVEL,
    ERROR_PASTA_INEXISTENTE,
    ERROR_RESPOSTA_HTTP_INVALIDA,
    ERROR_URL_INVALIDA,
    FolderImporter,
    ImportResult,
    LocalFileImporter,
    SourceImportError,
    SourceImportManager,
    UrlImporter,
    compute_fingerprint,
)
from _sistema.storage.database import LocalDatabase


# ---------------------------------------------------------------------------
# Fixtures
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


def _write_video(path: Path, content: bytes = b"conteudo-ficticio-de-video") -> Path:
    path.write_bytes(content)
    return path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - assinatura da stdlib
        pass


@pytest.fixture
def http_server(tmp_path):
    """Servidor HTTP local efêmero servindo ``tmp_path/serve`` -- nunca a
    internet real."""
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


# ---------------------------------------------------------------------------
# 1. Consistência de formato entre os três importadores
# ---------------------------------------------------------------------------


def test_local_e_url_importer_devolvem_import_result(manager, tmp_path, http_server):
    serve_dir, base_url = http_server
    local_file = _write_video(tmp_path / "a.mp4")
    _write_video(serve_dir / "b.mp4")

    r_local = manager.import_local_file(local_file)
    r_url = manager.import_url(f"{base_url}/b.mp4")

    assert isinstance(r_local, ImportResult)
    assert isinstance(r_url, ImportResult)
    assert r_local.success is True
    assert r_url.success is True
    assert isinstance(r_local.source_asset, SourceAsset)
    assert isinstance(r_url.source_asset, SourceAsset)


def test_folder_importer_devolve_lista_de_import_result_mesmo_formato(manager, tmp_path):
    folder = tmp_path / "pasta"
    folder.mkdir()
    _write_video(folder / "a.mp4")
    _write_video(folder / "b.mkv")

    results = manager.import_folder(folder)
    assert isinstance(results, list)
    assert len(results) == 2
    for item in results:
        assert isinstance(item, ImportResult)
        assert item.success is True
        assert isinstance(item.source_asset, SourceAsset)


# ---------------------------------------------------------------------------
# 2. LocalFileImporter
# ---------------------------------------------------------------------------


def test_local_file_importer_arquivo_valido_produz_source_asset_completo(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4", b"x" * 4096)
    result = manager.import_local_file(video)

    assert result.success is True
    asset = result.source_asset
    assert asset.local_path == str(video.resolve())
    assert asset.original_name == "clip.mp4"
    assert asset.fingerprint is not None and len(asset.fingerprint) == 64
    assert asset.size_bytes == 4096
    assert asset.media_type == "video"


def test_local_file_importer_persiste_no_banco(manager, database, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video)
    fetched = database.get(SourceAsset, result.source_asset.id)
    assert fetched is not None
    assert fetched.fingerprint == result.source_asset.fingerprint


def test_local_file_importer_arquivo_inexistente_falha_estruturada_nunca_excecao_crua(
    manager, tmp_path
):
    result = manager.import_local_file(tmp_path / "nao_existe.mp4")
    assert result.success is False
    assert result.error_code == ERROR_ARQUIVO_INEXISTENTE
    assert result.source_asset is None


def test_local_file_importer_arquivo_vazio_falha_estruturada(manager, tmp_path):
    empty = tmp_path / "vazio.mp4"
    empty.write_bytes(b"")
    result = manager.import_local_file(empty)
    assert result.success is False
    assert result.error_code == ERROR_ARQUIVO_VAZIO


def test_local_file_importer_extensao_invalida_falha_estruturada(manager, tmp_path):
    bad = tmp_path / "documento.txt"
    bad.write_bytes(b"nao e um video")
    result = manager.import_local_file(bad)
    assert result.success is False
    assert result.error_code == ERROR_EXTENSAO_INVALIDA


def test_local_file_importer_nunca_levanta_excecao_para_entradas_invalidas(manager, tmp_path):
    # Confirma que import_source() nunca deixa uma exceção crua escapar --
    # sempre devolve um ImportResult, mesmo para entradas problemáticas.
    try:
        result = manager.import_local_file(tmp_path / "inexistente" / "tambem_inexistente.mp4")
    except Exception as exc:  # pragma: no cover - o teste falha se isso ocorrer
        pytest.fail(f"import_local_file levantou excecao crua: {exc!r}")
    assert result.success is False


# ---------------------------------------------------------------------------
# 3. FolderImporter -- isolamento por item (item 1.6)
# ---------------------------------------------------------------------------


def test_folder_importer_isola_arquivos_invalidos_sem_interromper_os_demais(manager, tmp_path):
    folder = tmp_path / "mix"
    folder.mkdir()
    _write_video(folder / "valido1.mp4")
    _write_video(folder / "valido2.mkv")
    (folder / "invalido_extensao.txt").write_bytes(b"nao e video")
    (folder / "invalido_vazio.mp4").write_bytes(b"")

    results = manager.import_folder(folder)
    by_name = {Path(r.source_uri).name: r for r in results}

    assert len(results) == 4
    assert by_name["valido1.mp4"].success is True
    assert by_name["valido2.mkv"].success is True
    assert by_name["invalido_extensao.txt"].success is False
    assert by_name["invalido_extensao.txt"].error_code == ERROR_EXTENSAO_INVALIDA
    assert by_name["invalido_vazio.mp4"].success is False
    assert by_name["invalido_vazio.mp4"].error_code == ERROR_ARQUIVO_VAZIO

    # Os dois válidos foram de fato persistidos, apesar dos dois inválidos.
    valid_assets = [r.source_asset for r in results if r.success]
    assert len(valid_assets) == 2


def test_folder_importer_pasta_vazia_devolve_lista_vazia(manager, tmp_path):
    folder = tmp_path / "vazia"
    folder.mkdir()
    assert manager.import_folder(folder) == []


def test_folder_importer_pasta_inexistente_falha_estruturada(manager, tmp_path):
    with pytest.raises(SourceImportError) as exc_info:
        manager.import_folder(tmp_path / "nao_existe")
    assert exc_info.value.code == ERROR_PASTA_INEXISTENTE


def test_folder_importer_ignora_subdiretorios(manager, tmp_path):
    folder = tmp_path / "com_subdir"
    folder.mkdir()
    _write_video(folder / "video.mp4")
    (folder / "subpasta").mkdir()
    (folder / "subpasta" / "outro.mp4").write_bytes(b"nao deve ser varrido")

    results = manager.import_folder(folder)
    assert len(results) == 1
    assert results[0].success is True


# ---------------------------------------------------------------------------
# 4. UrlImporter -- servidor HTTP local, hermético
# ---------------------------------------------------------------------------


def test_url_importer_download_correto_de_servidor_local(manager, http_server):
    serve_dir, base_url = http_server
    content = b"bytes-do-video-de-teste" * 100
    (serve_dir / "video.mp4").write_bytes(content)

    result = manager.import_url(f"{base_url}/video.mp4")

    assert result.success is True
    asset = result.source_asset
    assert asset.size_bytes == len(content)
    assert asset.source_uri == f"{base_url}/video.mp4"
    assert asset.fingerprint == compute_fingerprint(Path(asset.local_path))
    assert Path(asset.local_path).read_bytes() == content


def test_url_importer_persiste_conteudo_no_diretorio_gerenciado(manager, app_paths, http_server):
    serve_dir, base_url = http_server
    (serve_dir / "video.mp4").write_bytes(b"conteudo")
    result = manager.import_url(f"{base_url}/video.mp4")
    local_path = Path(result.source_asset.local_path)
    assert local_path.is_relative_to(app_paths.sources)


def test_url_importer_servidor_devolve_404_falha_estruturada(manager, http_server):
    _serve_dir, base_url = http_server
    result = manager.import_url(f"{base_url}/nao_existe.mp4")
    assert result.success is False
    assert result.error_code == ERROR_RESPOSTA_HTTP_INVALIDA


def test_url_importer_conteudo_vazio_falha_estruturada(manager, http_server):
    serve_dir, base_url = http_server
    (serve_dir / "vazio.mp4").write_bytes(b"")
    result = manager.import_url(f"{base_url}/vazio.mp4")
    assert result.success is False
    assert result.error_code == ERROR_CONTEUDO_VAZIO


def test_url_importer_esquema_nao_http_e_rejeitado(manager, tmp_path):
    alvo = tmp_path / "secreto.mp4"
    alvo.write_bytes(b"nao deveria ser lido")
    result = manager.import_url(f"file://{alvo}")
    assert result.success is False
    assert result.error_code == ERROR_URL_INVALIDA


def test_url_importer_url_sem_extensao_reconhecivel_falha_estruturada(manager, http_server):
    _serve_dir, base_url = http_server
    result = manager.import_url(f"{base_url}/sem_extensao")
    assert result.success is False
    assert result.error_code == ERROR_NOME_ARQUIVO_NAO_RECONHECIVEL


def test_url_importer_nao_deixa_arquivo_parcial_apos_falha(manager, app_paths, http_server):
    serve_dir, base_url = http_server
    (serve_dir / "vazio.mp4").write_bytes(b"")
    manager.import_url(f"{base_url}/vazio.mp4")
    # Nenhum arquivo deveria ter sobrado no diretório gerenciado.
    assert list(app_paths.sources.iterdir()) == []


# ---------------------------------------------------------------------------
# 5. UrlImporter -- nunca autenticação/cookies/sessão (estrutural, não só teste)
# ---------------------------------------------------------------------------


def test_url_importer_nunca_constroi_objeto_request_com_headers():
    """Inspeciona o código-fonte real: ``UrlImporter`` chama
    ``urllib.request.urlopen`` com a URL como STRING pura -- nunca um
    objeto ``Request`` (que permitiria anexar headers de autenticação).
    """
    import ast

    source = Path(source_import.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_request_call = (
                (isinstance(func, ast.Attribute) and func.attr == "Request")
                or (isinstance(func, ast.Name) and func.id == "Request")
            )
            assert not is_request_call, (
                "source_import.py constroi um objeto Request -- isso "
                "abriria caminho para headers de autenticacao"
            )


def test_url_importer_nunca_importa_ou_referencia_cookiejar_ou_sessao():
    import ast

    source = Path(source_import.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    proibidos = ("cookiejar", "cookie", "session", "authorization", "auth_header")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = " ".join(
                filter(
                    None,
                    [getattr(node, "module", None)]
                    + [alias.name for alias in node.names],
                )
            )
            for termo in proibidos:
                assert termo not in names.lower()
        if isinstance(node, ast.Attribute):
            assert node.attr.lower() not in proibidos
        if isinstance(node, ast.Name):
            assert node.id.lower() not in proibidos


def test_url_importer_nunca_passa_argumento_headers_para_urlopen():
    import ast

    source = Path(source_import.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                assert keyword.arg != "headers", (
                    "uma chamada passa headers= explicitamente -- "
                    "UrlImporter nunca deve fazer isso"
                )


# ---------------------------------------------------------------------------
# 6. Fingerprint determinístico
# ---------------------------------------------------------------------------


def test_fingerprint_e_deterministico_para_o_mesmo_arquivo(tmp_path):
    video = _write_video(tmp_path / "clip.mp4", b"conteudo-estavel" * 50)
    fp1 = compute_fingerprint(video)
    fp2 = compute_fingerprint(video)
    assert fp1 == fp2


def test_fingerprint_e_deterministico_entre_duas_importacoes_do_mesmo_arquivo(
    manager, tmp_path
):
    video = _write_video(tmp_path / "clip.mp4", b"conteudo-estavel" * 50)
    r1 = manager.import_local_file(video)
    r2 = manager.import_local_file(video)
    assert r1.source_asset.fingerprint == r2.source_asset.fingerprint


def test_fingerprint_difere_para_arquivos_com_conteudo_diferente(tmp_path):
    a = _write_video(tmp_path / "a.mp4", b"conteudo-A" * 1000)
    b = _write_video(tmp_path / "b.mp4", b"conteudo-B" * 1000)
    assert compute_fingerprint(a) != compute_fingerprint(b)


# ---------------------------------------------------------------------------
# 7. Nenhum Job/Publication/Schedule criado; nenhum import proibido
# ---------------------------------------------------------------------------


def test_source_import_nunca_importa_modulos_ortogonais():
    import ast

    source = Path(source_import.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
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
            for termo in proibidos:
                assert termo not in names.lower()
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
            for termo in proibidos:
                assert termo not in names.lower()


def test_source_import_nunca_referencia_job_publication_ou_schedule_no_codigo():
    """Confirma, por inspeção do código-fonte, que nenhuma classe
    ``Job``/``Publication``/``Schedule`` é sequer referenciada -- não é
    só "não testado ser criado", é "não existe caminho para isso"."""
    source = Path(source_import.__file__).read_text(encoding="utf-8")
    for termo in ("Job(", "Publication(", "Schedule(", "Project("):
        assert termo not in source


def test_import_local_file_nao_cria_nenhuma_outra_entidade_no_banco(manager, database, tmp_path):
    from _sistema.domain import Job, Publication, Schedule, Video

    video = _write_video(tmp_path / "clip.mp4")
    manager.import_local_file(video)

    assert database.list(Job) == []
    assert database.list(Publication) == []
    assert database.list(Schedule) == []
    assert database.list(Video) == []


def test_import_url_nao_cria_nenhuma_outra_entidade_no_banco(manager, database, http_server):
    from _sistema.domain import Job, Publication, Schedule, Video

    serve_dir, base_url = http_server
    (serve_dir / "clip.mp4").write_bytes(b"conteudo")
    manager.import_url(f"{base_url}/clip.mp4")

    assert database.list(Job) == []
    assert database.list(Publication) == []
    assert database.list(Schedule) == []
    assert database.list(Video) == []


# ---------------------------------------------------------------------------
# 8. Restart -- novas instâncias
# ---------------------------------------------------------------------------


def test_source_asset_sobrevive_a_restart_com_novas_instancias(app_paths, tmp_path):
    db1 = LocalDatabase(app_paths.database / "painel.db")
    db1.initialize()
    manager1 = SourceImportManager(db1, app_paths)
    video = _write_video(tmp_path / "clip.mp4")
    result = manager1.import_local_file(video)

    db2 = LocalDatabase(app_paths.database / "painel.db")
    manager2 = SourceImportManager(db2, app_paths)
    fetched = manager2.database.get(SourceAsset, result.source_asset.id)
    assert fetched is not None
    assert fetched.fingerprint == result.source_asset.fingerprint


# ---------------------------------------------------------------------------
# 9. AppPaths.sources -- diretório gerenciado novo
# ---------------------------------------------------------------------------


def test_app_paths_tem_diretorio_sources_gerenciado(app_paths):
    assert app_paths.sources.is_dir()
    assert app_paths.sources in app_paths.mutable_dirs()
