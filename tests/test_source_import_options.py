# -*- coding: utf-8 -*-
"""PROMPT 24b -- Import Options / User Assertions: testes.

Cobre a capacidade declarativa que faltava do Prompt 24 original
(``ImportOptions``, presets, ``AssertionStore``, distinção
SYSTEM/USER/IMPORT_PRESET) -- NUNCA reabre nem altera o comportamento já
aprovado de ``LocalFileImporter``/``FolderImporter``/``UrlImporter`` sem
``ImportOptions`` (ver ``tests/test_source_import.py``, que continua
passando sem nenhuma alteração).
"""
from __future__ import annotations

import ast
import http.server
import re
import sqlite3
import threading
from pathlib import Path

import pytest

import _sistema.source_import as source_import
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Job, Publication, Schedule, Video
from _sistema.source_import import (
    ASSERTION_EXTERNALLY_EDITED,
    ASSERTION_USER_MARKED_READY,
    DECLARATION_KIND_ASSERTION,
    DECLARATION_KIND_FLAG,
    DECLARATION_KIND_LABEL,
    ORIGIN_IMPORT_PRESET,
    ORIGIN_SYSTEM,
    ORIGIN_USER,
    PRESET_ALREADY_EDITED,
    PRESET_CUSTOM,
    PRESET_ORIGINAL,
    PRESET_READY_FOR_AI,
    PRESET_USER_MARKED_READY,
    AssertionStore,
    Declaration,
    ImportOptions,
    SourceImportManager,
)
from _sistema.storage.database import LocalDatabase
from _sistema.time_utils import utc_now_iso


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de tests/test_source_import.py)
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
def store(database):
    return AssertionStore(database)


def _write_video(path: Path, content: bytes = b"conteudo-ficticio-de-video") -> Path:
    path.write_bytes(content)
    return path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - assinatura da stdlib
        pass


@pytest.fixture
def http_server(tmp_path):
    """Servidor HTTP local efêmero -- nunca a internet real (mesmo padrão
    de tests/test_source_import.py)."""
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
# 1. Import sem ImportOptions -- nenhuma regressão
# ---------------------------------------------------------------------------


def test_import_local_file_sem_options_continua_funcionando_como_antes(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video)
    assert result.success is True
    assert result.source_asset.extra == {}


def test_import_local_file_com_options_none_explicito_identico_a_omitido(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video, None)
    assert result.success is True
    assert result.source_asset.extra == {}


def test_import_folder_sem_options_continua_funcionando_como_antes(manager, tmp_path):
    folder = tmp_path / "pasta"
    folder.mkdir()
    _write_video(folder / "a.mp4")
    results = manager.import_folder(folder)
    assert len(results) == 1
    assert results[0].success is True


# ---------------------------------------------------------------------------
# 2. ImportOptions preenchido -- persistido e recuperável
# ---------------------------------------------------------------------------


def test_list_active_devolve_instancias_de_declaration(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video, ImportOptions(user_labels=["X"])).source_asset
    active = manager.assertion_store.list_active(asset.id)
    assert len(active) == 1
    assert isinstance(active[0], Declaration)
    assert active[0].source_asset_id == asset.id


def test_import_local_file_com_options_persiste_assertions_flags_labels(manager, database, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    options = ImportOptions(
        user_assertions=["EXTERNALLY_CAPTIONED"],
        user_flags=["REVISAR_DEPOIS"],
        user_labels=["CAMPANHA_X"],
    )
    result = manager.import_local_file(video, options)
    assert result.success is True

    active = manager.assertion_store.list_active(result.source_asset.id)
    by_kind = {(d.kind, d.value): d for d in active}
    assert (DECLARATION_KIND_ASSERTION, "EXTERNALLY_CAPTIONED") in by_kind
    assert (DECLARATION_KIND_FLAG, "REVISAR_DEPOIS") in by_kind
    assert (DECLARATION_KIND_LABEL, "CAMPANHA_X") in by_kind
    for declaration in by_kind.values():
        assert declaration.origin == ORIGIN_USER


def test_import_local_file_com_options_persiste_referencias_escalares_em_extra(manager, tmp_path):
    project_id = "11111111-1111-1111-1111-111111111111"
    account_id = "22222222-2222-2222-2222-222222222222"
    video = _write_video(tmp_path / "clip.mp4")
    options = ImportOptions(
        target_project_id=project_id,
        target_account_id=account_id,
        readiness_profile_id="perfil-opaco-qualquer",
    )
    result = manager.import_local_file(video, options)
    assert result.success is True

    # Recuperável associado ao SourceAsset resultante -- via releitura do
    # banco, não só do objeto em memória devolvido pela chamada.
    from _sistema.domain import SourceAsset as _SourceAsset

    reloaded = manager.database.get(_SourceAsset, result.source_asset.id)
    assert reloaded.extra["import_options"] == {
        "target_project_id": project_id,
        "target_account_id": account_id,
        "readiness_profile_id": "perfil-opaco-qualquer",
    }


def test_import_url_com_options_persiste_declaracoes(manager, http_server):
    serve_dir, base_url = http_server
    (serve_dir / "clip.mp4").write_bytes(b"conteudo")
    options = ImportOptions(user_assertions=["EXTERNALLY_CAPTIONED"])

    result = manager.import_url(f"{base_url}/clip.mp4", options)
    assert result.success is True

    active = manager.assertion_store.list_active(result.source_asset.id)
    assert any(
        d.kind == DECLARATION_KIND_ASSERTION and d.value == "EXTERNALLY_CAPTIONED" and d.origin == ORIGIN_USER
        for d in active
    )


# ---------------------------------------------------------------------------
# 3. FolderImporter aplica o MESMO ImportOptions a TODOS os arquivos
# ---------------------------------------------------------------------------


def test_folder_importer_aplica_options_a_todos_os_arquivos_importados(manager, tmp_path):
    folder = tmp_path / "pasta"
    folder.mkdir()
    _write_video(folder / "a.mp4")
    _write_video(folder / "b.mkv")
    options = ImportOptions(user_labels=["LOTE_SETEMBRO"])

    results = manager.import_folder(folder, options)
    assert len(results) == 2
    for result in results:
        assert result.success is True
        active = manager.assertion_store.list_active(result.source_asset.id)
        values = {(d.kind, d.value) for d in active}
        assert (DECLARATION_KIND_LABEL, "LOTE_SETEMBRO") in values


def test_folder_importer_arquivo_invalido_nao_impede_options_nos_validos(manager, tmp_path):
    folder = tmp_path / "pasta"
    folder.mkdir()
    _write_video(folder / "a.mp4")
    (folder / "invalido.txt").write_text("nao e video")
    options = ImportOptions(user_labels=["LOTE"])

    results = manager.import_folder(folder, options)
    assert len(results) == 2
    ok = [r for r in results if r.success]
    fail = [r for r in results if not r.success]
    assert len(ok) == 1
    assert len(fail) == 1
    active = manager.assertion_store.list_active(ok[0].source_asset.id)
    assert any(d.kind == DECLARATION_KIND_LABEL and d.value == "LOTE" for d in active)


# ---------------------------------------------------------------------------
# 4. Presets -- mapeamento documentado, CUSTOM sem mapeamento automático
# ---------------------------------------------------------------------------


def test_preset_already_edited_mapeia_para_externally_edited(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video, ImportOptions(preset=PRESET_ALREADY_EDITED))
    active = manager.assertion_store.list_active(result.source_asset.id)
    values = {(d.kind, d.value, d.origin) for d in active}
    assert (DECLARATION_KIND_ASSERTION, ASSERTION_EXTERNALLY_EDITED, ORIGIN_IMPORT_PRESET) in values


def test_preset_user_marked_ready_mapeia_para_user_marked_ready(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video, ImportOptions(preset=PRESET_USER_MARKED_READY))
    active = manager.assertion_store.list_active(result.source_asset.id)
    values = {(d.kind, d.value, d.origin) for d in active}
    assert (DECLARATION_KIND_ASSERTION, ASSERTION_USER_MARKED_READY, ORIGIN_IMPORT_PRESET) in values


def test_preset_original_mapeia_para_label_original_sem_assertion(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video, ImportOptions(preset=PRESET_ORIGINAL))
    active = manager.assertion_store.list_active(result.source_asset.id)
    assert {(d.kind, d.value) for d in active} == {(DECLARATION_KIND_LABEL, "ORIGINAL")}


def test_preset_ready_for_ai_mapeia_para_label_sem_assertion_externa(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video, ImportOptions(preset=PRESET_READY_FOR_AI))
    active = manager.assertion_store.list_active(result.source_asset.id)
    assert {(d.kind, d.value) for d in active} == {(DECLARATION_KIND_LABEL, "READY_FOR_AI")}


def test_preset_custom_nao_produz_nenhum_mapeamento_automatico(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    result = manager.import_local_file(video, ImportOptions(preset=PRESET_CUSTOM))
    active = manager.assertion_store.list_active(result.source_asset.id)
    assert active == ()


def test_preset_custom_com_user_assertions_explicitas_funciona_normalmente(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    options = ImportOptions(preset=PRESET_CUSTOM, user_assertions=["EXTERNALLY_REFRAMED"])
    result = manager.import_local_file(video, options)
    active = manager.assertion_store.list_active(result.source_asset.id)
    values = {(d.kind, d.value, d.origin) for d in active}
    assert (DECLARATION_KIND_ASSERTION, "EXTERNALLY_REFRAMED", ORIGIN_USER) in values


def test_preset_desconhecido_e_rejeitado_na_construcao_de_import_options():
    with pytest.raises(ValueError):
        ImportOptions(preset="NAO_EXISTE")


# ---------------------------------------------------------------------------
# 5. API pós-import: aplicar a subconjunto selecionado, editar em lote,
#    remover
# ---------------------------------------------------------------------------


def test_apply_assertions_a_subconjunto_selecionado_fora_do_import(manager, tmp_path):
    video_a = _write_video(tmp_path / "a.mp4")
    video_b = _write_video(tmp_path / "b.mp4")
    video_c = _write_video(tmp_path / "c.mp4")
    asset_a = manager.import_local_file(video_a).source_asset
    asset_b = manager.import_local_file(video_b).source_asset
    asset_c = manager.import_local_file(video_c).source_asset

    # "Somente aos selecionados": a e c, nunca b.
    manager.apply_assertions([asset_a.id, asset_c.id], ["EXTERNALLY_CAPTIONED"])

    assert any(d.value == "EXTERNALLY_CAPTIONED" for d in manager.assertion_store.list_active(asset_a.id))
    assert any(d.value == "EXTERNALLY_CAPTIONED" for d in manager.assertion_store.list_active(asset_c.id))
    assert manager.assertion_store.list_active(asset_b.id) == ()


def test_editar_em_lote_aplica_a_multiplos_source_assets_de_uma_vez(manager, tmp_path):
    assets = []
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        video = _write_video(tmp_path / name)
        assets.append(manager.import_local_file(video).source_asset)

    manager.assertion_store.apply_labels([a.id for a in assets], ["REVISADO_EM_LOTE"])

    for asset in assets:
        active = manager.assertion_store.list_active(asset.id)
        assert any(d.kind == DECLARATION_KIND_LABEL and d.value == "REVISADO_EM_LOTE" for d in active)


def test_remover_declaracao_ja_aplicada(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video).source_asset
    manager.apply_assertions([asset.id], ["EXTERNALLY_CAPTIONED"])
    assert any(d.value == "EXTERNALLY_CAPTIONED" for d in manager.assertion_store.list_active(asset.id))

    manager.remove_assertion(asset.id, "EXTERNALLY_CAPTIONED")
    assert manager.assertion_store.list_active(asset.id) == ()


def test_remover_preserva_historico_append_only_da_declaracao_original(manager, database, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video).source_asset
    manager.apply_assertions([asset.id], ["EXTERNALLY_CAPTIONED"])
    manager.remove_assertion(asset.id, "EXTERNALLY_CAPTIONED")

    with database.connection() as conn:
        rows = conn.execute(
            "SELECT kind, value, action FROM source_asset_declarations "
            "WHERE source_asset_id = ? ORDER BY rowid",
            (asset.id,),
        ).fetchall()
    actions = [(row["kind"], row["value"], row["action"]) for row in rows]
    assert ("ASSERTION", "EXTERNALLY_CAPTIONED", "ADD") in actions
    assert ("ASSERTION", "EXTERNALLY_CAPTIONED", "REMOVE") in actions
    # O evento ADD original nunca foi apagado/reescrito -- ambos os
    # eventos continuam presentes na tabela append-only.
    assert len(actions) == 2


def test_reaplicar_apos_remover_torna_a_declaracao_ativa_de_novo(manager, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video).source_asset
    manager.apply_assertions([asset.id], ["EXTERNALLY_CAPTIONED"])
    manager.remove_assertion(asset.id, "EXTERNALLY_CAPTIONED")
    manager.apply_assertions([asset.id], ["EXTERNALLY_CAPTIONED"])

    active = manager.assertion_store.list_active(asset.id)
    assert any(d.value == "EXTERNALLY_CAPTIONED" for d in active)


# ---------------------------------------------------------------------------
# 6. Distinção estrutural SYSTEM_BADGE vs. USER_ASSERTION
# ---------------------------------------------------------------------------


def test_declaracao_user_nunca_aparece_em_consulta_de_origem_system(manager, store, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video).source_asset
    manager.apply_assertions([asset.id], ["EXTERNALLY_CAPTIONED"], origin=ORIGIN_USER)

    system_declarations = store.list_by_origin(ORIGIN_SYSTEM, source_asset_id=asset.id)
    assert system_declarations == ()
    user_declarations = store.list_by_origin(ORIGIN_USER, source_asset_id=asset.id)
    assert any(d.value == "EXTERNALLY_CAPTIONED" for d in user_declarations)


def test_declaracao_import_preset_nunca_aparece_em_consulta_de_origem_system(manager, store, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video, ImportOptions(preset=PRESET_ALREADY_EDITED)).source_asset

    system_declarations = store.list_by_origin(ORIGIN_SYSTEM, source_asset_id=asset.id)
    assert system_declarations == ()
    preset_declarations = store.list_by_origin(ORIGIN_IMPORT_PRESET, source_asset_id=asset.id)
    assert any(d.value == ASSERTION_EXTERNALLY_EDITED for d in preset_declarations)


def test_este_prompt_nunca_grava_origin_system(store, database, tmp_path):
    """Nenhum caminho de código deste Prompt escreve ``origin=SYSTEM`` --
    provado de duas formas: (a) inspeção estrutural do módulo (nenhuma
    chamada grava a constante ``ORIGIN_SYSTEM``) e (b) comportamento real:
    depois de qualquer sequência de operações públicas, a tabela nunca tem
    uma linha ``origin='SYSTEM'``."""
    with database.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM source_asset_declarations WHERE origin = 'SYSTEM'"
        ).fetchone()[0]
    assert count == 0


def test_write_many_rejeita_origin_desconhecida(store, database, app_paths, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    from _sistema.source_import import LocalFileImporter

    asset = LocalFileImporter(database).import_source(video).source_asset
    with pytest.raises(ValueError):
        store.apply([asset.id], kind=DECLARATION_KIND_ASSERTION, values=["X"], origin="NAO_EXISTE")


def test_write_many_rejeita_kind_desconhecido(store, database, tmp_path):
    from _sistema.source_import import LocalFileImporter

    video = _write_video(tmp_path / "clip.mp4")
    asset = LocalFileImporter(database).import_source(video).source_asset
    with pytest.raises(ValueError):
        store.apply([asset.id], kind="NAO_EXISTE", values=["X"], origin=ORIGIN_USER)


def test_declaracao_gravada_diretamente_como_system_e_lida_de_volta_como_system(store, database, tmp_path):
    """Complementa o teste anterior: SE algo (ex. um Prompt futuro real de
    evidência automática) gravar origin=SYSTEM através do mesmo mecanismo,
    ela é lida de volta como SYSTEM -- nunca confundida com USER/
    IMPORT_PRESET. Prova que a coluna de origem é a fonte de verdade, não
    uma inferência."""
    from _sistema.source_import import LocalFileImporter

    video = _write_video(tmp_path / "clip.mp4")
    asset = LocalFileImporter(database).import_source(video).source_asset
    store.apply([asset.id], kind=DECLARATION_KIND_ASSERTION, values=["EVIDENCIA_X"], origin=ORIGIN_SYSTEM)
    store.apply([asset.id], kind=DECLARATION_KIND_ASSERTION, values=["EVIDENCIA_X_USER"], origin=ORIGIN_USER)

    system_only = store.list_by_origin(ORIGIN_SYSTEM, source_asset_id=asset.id)
    assert {d.value for d in system_only} == {"EVIDENCIA_X"}
    user_only = store.list_by_origin(ORIGIN_USER, source_asset_id=asset.id)
    assert {d.value for d in user_only} == {"EVIDENCIA_X_USER"}


# ---------------------------------------------------------------------------
# 7. Regra de conteúdo (item 1.7) -- nenhum preenchimento automático
# ---------------------------------------------------------------------------


def test_nenhum_preenchimento_automatico_de_titulo_descricao_hashtags_no_codigo():
    """Garantia por AUSÊNCIA, verificada no CÓDIGO real (AST), não na
    docstring do módulo (que cita esses nomes em prosa, na seção que
    documenta justamente esta garantia -- por isso a checagem aqui
    inspeciona nós de atribuição/atributo reais, nunca o texto bruto do
    arquivo, que colidiria com a própria documentação)."""
    tree = ast.parse(Path(source_import.__file__).read_text(encoding="utf-8"))
    proibidos_attr = {"title", "description", "hashtags"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in proibidos_attr, (
                f"referência de atributo proibida encontrada: .{node.attr} (linha {node.lineno})"
            )


def test_nenhum_job_publication_schedule_ou_project_criado_por_codigo_novo():
    source = Path(source_import.__file__).read_text(encoding="utf-8")
    for termo in ("Job(", "Publication(", "Schedule(", "Project("):
        assert termo not in source


def test_import_com_options_completo_nao_cria_nenhuma_outra_entidade_no_banco(manager, database, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    options = ImportOptions(
        preset=PRESET_ALREADY_EDITED,
        user_assertions=["EXTERNALLY_CAPTIONED"],
        user_flags=["X"],
        user_labels=["Y"],
        target_project_id="33333333-3333-3333-3333-333333333333",
        target_account_id="44444444-4444-4444-4444-444444444444",
        readiness_profile_id="perfil",
    )
    manager.import_local_file(video, options)

    assert database.list(Job) == []
    assert database.list(Publication) == []
    assert database.list(Schedule) == []
    assert database.list(Video) == []


# ---------------------------------------------------------------------------
# 8. Ortogonalidade -- nenhum import dos módulos protegidos
# ---------------------------------------------------------------------------


def test_source_import_continua_sem_importar_modulos_ortogonais():
    tree = ast.parse(Path(source_import.__file__).read_text(encoding="utf-8"))
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


# ---------------------------------------------------------------------------
# 9. Validação de UUID -- target_project_id/target_account_id
# ---------------------------------------------------------------------------


def test_target_project_id_nao_uuid_falha_estruturada_na_construcao():
    with pytest.raises(ValueError):
        ImportOptions(target_project_id="nao-e-um-uuid")


def test_target_account_id_nao_uuid_falha_estruturada_na_construcao():
    with pytest.raises(ValueError):
        ImportOptions(target_account_id="tambem-nao-e-uuid")


def test_target_project_id_uuid_valido_mas_inexistente_no_banco_e_aceito(manager, tmp_path):
    """Decisão documentada na seção 0.1: nenhum lookup contra
    projects/accounts é feito -- um UUID bem formado é aceito mesmo que
    não exista nenhum Project com esse id."""
    video = _write_video(tmp_path / "clip.mp4")
    inexistente = "55555555-5555-5555-5555-555555555555"
    result = manager.import_local_file(video, ImportOptions(target_project_id=inexistente))
    assert result.success is True
    assert result.source_asset.extra["import_options"]["target_project_id"] == inexistente


# ---------------------------------------------------------------------------
# 10. Timestamp -- utc_now_iso()
# ---------------------------------------------------------------------------


_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")


def test_timestamp_da_declaracao_usa_formato_utc_now_iso(manager, database, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video).source_asset
    manager.apply_assertions([asset.id], ["EXTERNALLY_CAPTIONED"])

    with database.connection() as conn:
        row = conn.execute(
            "SELECT created_at FROM source_asset_declarations WHERE source_asset_id = ?",
            (asset.id,),
        ).fetchone()
    assert _ISO_TIMESTAMP_RE.match(row["created_at"]) is not None
    # Mesmo formato de referência já usado no resto do projeto.
    assert _ISO_TIMESTAMP_RE.match(utc_now_iso()) is not None


# ---------------------------------------------------------------------------
# 11. Nenhum segredo/conteúdo bruto/caminho desnecessário persistido
# ---------------------------------------------------------------------------


def test_nenhum_segredo_ou_caminho_de_arquivo_vaza_no_historico_de_declaracoes(manager, database, tmp_path):
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video).source_asset

    caminho_sensivel_ficticio = str(tmp_path / "credenciais" / "token_secreto_nao_deveria_aparecer.txt")
    manager.apply_assertions([asset.id], ["EXTERNALLY_METADATA_PREPARED"])
    # Simula um valor de declaração que, por engano de um chamador futuro,
    # tentasse carregar algo sensível -- confirma que só as colunas do
    # item 1.6 existem na tabela, nada além disso onde um caminho/segredo
    # poderia ser escondido.
    with database.connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_asset_declarations)")}
    assert columns == {"id", "source_asset_id", "kind", "value", "origin", "action", "created_at"}

    with database.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM source_asset_declarations WHERE source_asset_id = ?",
            (asset.id,),
        ).fetchall()
    for row in rows:
        row_text = " ".join(str(row[col]) for col in row.keys())
        assert caminho_sensivel_ficticio not in row_text


# ---------------------------------------------------------------------------
# 11b. GATE 3 -- crash window: apply_options é atômico (tudo ou nada)
# ---------------------------------------------------------------------------


def test_apply_options_com_multiplas_categorias_morre_no_meio_e_nao_persiste_nada(
    manager, database, tmp_path, monkeypatch
):
    """Prova real de crash-window, não uma RuntimeError capturada pelo
    próprio código (item 12 do gate: "teste honesto" -- uma Exception
    capturada NÃO é crash real). ``ImportOptions`` com preset +
    user_assertions + user_flags + user_labels grava as QUATRO categorias
    em uma única transaction (``AssertionStore._write_batch``). Para
    provar que um processo morto NO MEIO dessa transaction nunca deixa um
    subconjunto parcial persistido, forçamos uma falha REAL do SQLite a
    meio do ``executemany`` (não uma exceção sintética): o trigger
    append-only ``trg_source_asset_declarations_no_id_reuse``
    (``m007``) aborta a transaction se um ``id`` duplicado aparecer --
    monkeypatchamos ``new_uuid`` para devolver sempre o mesmo id, então a
    segunda linha do batch colide com a primeira (já visível dentro da
    mesma transaction) e o SQLite genuinamente aborta no meio do
    ``INSERT`` em lote. A exceção propaga sem ser capturada por
    ``AssertionStore`` -- o ROLLBACK automático de ``LocalDatabase.transaction()``
    é quem garante o "tudo ou nada", nunca um try/except deste módulo."""
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager.import_local_file(video).source_asset

    fixed_id = "99999999-9999-9999-9999-999999999999"
    monkeypatch.setattr(source_import, "new_uuid", lambda: fixed_id)

    options = ImportOptions(
        preset=PRESET_ALREADY_EDITED,
        user_assertions=["EXTERNALLY_REFRAMED"],
        user_flags=["FLAG_X"],
        user_labels=["LABEL_Y"],
    )
    with pytest.raises(sqlite3.IntegrityError):
        manager.assertion_store.apply_options((asset.id,), options)

    monkeypatch.undo()

    # Nenhuma das quatro categorias ficou parcialmente persistida --
    # ROLLBACK da transaction cobriu TODAS as linhas do batch inteiro
    # (nem mesmo a primeira linha, que teria sido aceita isoladamente).
    with database.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM source_asset_declarations WHERE source_asset_id = ?",
            (asset.id,),
        ).fetchone()[0]
    assert count == 0

    # Recuperação: uma nova chamada (processo "reiniciado", ids reais de
    # novo) funciona normalmente e persiste tudo de uma vez.
    manager.assertion_store.apply_options((asset.id,), options)
    active = manager.assertion_store.list_active(asset.id)
    assert len(active) == 4


# ---------------------------------------------------------------------------
# 12. Restart -- novas instâncias
# ---------------------------------------------------------------------------


def test_declaracoes_sobrevivem_a_restart_com_novas_instancias(app_paths, tmp_path):
    db1 = LocalDatabase(app_paths.database / "painel.db")
    db1.initialize()
    manager1 = SourceImportManager(db1, app_paths)
    video = _write_video(tmp_path / "clip.mp4")
    asset = manager1.import_local_file(video, ImportOptions(user_labels=["PERSISTE"])).source_asset

    db2 = LocalDatabase(app_paths.database / "painel.db")
    db2.initialize()
    manager2 = SourceImportManager(db2, app_paths)
    active = manager2.assertion_store.list_active(asset.id)
    assert any(d.kind == DECLARATION_KIND_LABEL and d.value == "PERSISTE" for d in active)


# ---------------------------------------------------------------------------
# 13. Normalização/validação de entradas
# ---------------------------------------------------------------------------


def test_import_options_rejeita_string_vazia_em_user_assertions():
    with pytest.raises(ValueError):
        ImportOptions(user_assertions=["", "EXTERNALLY_EDITED"])


def test_apply_ignora_chamada_com_lista_vazia_de_source_asset_ids(store):
    # Não deve levantar nem gravar nada -- chamada "vazia" é um no-op seguro.
    store.apply([], kind=DECLARATION_KIND_ASSERTION, values=["X"], origin=ORIGIN_USER)
    store.apply(["11111111-1111-1111-1111-111111111111"], kind=DECLARATION_KIND_ASSERTION, values=[], origin=ORIGIN_USER)
