# -*- coding: utf-8 -*-
"""PROMPT 36 -- TemplateEngine + 2 templates oficiais built-in: testes.

Cobre: CRUD validado de ``Template`` (create/read/update/list); matriz
adversarial de geometria normalizada (dentro/fora de [0,1], overflow de
borda, width/height <= 0, BACKGROUND fora do frame inteiro); vocabulário
fechado de ``template_type``/``zone_type``; ingestão idempotente dos 2
templates oficiais (cópia nunca in-place, bytes/mtime do original
nunca alterados, idempotência sequencial/concorrente, auto-cura de
arquivo gerenciado apagado); prova de que o código não depende de
"exatamente 2" (um 3º template CUSTOM coexiste normalmente); preview
metadata-only; restart com novas instâncias; AST estrutural (zero
import de módulo irmão/Geração 1, ``media_catalog.py`` não tocado,
nenhuma migration nova).
"""
from __future__ import annotations

import ast
import hashlib
import threading
from pathlib import Path

import pytest

import _sistema.template_engine as template_engine_module
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Template
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import (
    ArquivoOficialAusenteError,
    BUILTIN_TEMPLATE_SPECS,
    CampoInvalidoError,
    DEFAULT_BUILTIN_ASSETS_DIR,
    GeometriaInvalidaError,
    LayoutInvalidoError,
    TEMPLATE_TYPE_BUILT_IN,
    TEMPLATE_TYPE_CUSTOM,
    TemplateEngine,
    TemplateNaoEncontradoError,
    VocabularioInvalidoError,
    validate_layout,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_metadata_manager.py / test_auto_reframe.py)
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
def storage(app_paths):
    return StorageManager(app_paths, database_path=app_paths.database / "painel.db")


@pytest.fixture
def engine(database, storage, app_paths):
    return TemplateEngine(database, storage_manager=storage, app_paths=app_paths)


def _valid_layout(video_zone: dict | None = None) -> dict:
    return {
        "zones": [
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "VIDEO", **(video_zone or {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8})},
        ]
    }


# ---------------------------------------------------------------------------
# CRUD básico
# ---------------------------------------------------------------------------


def test_create_template_valido(engine):
    tpl = engine.create_template(name="Meu Template", layout=_valid_layout())
    assert tpl.id
    assert tpl.template_type == TEMPLATE_TYPE_CUSTOM
    assert tpl.enabled is True
    assert tpl.layout["zones"][0]["zone_type"] == "BACKGROUND"


def test_create_template_com_template_type_explicito(engine):
    tpl = engine.create_template(name="X", layout=_valid_layout(), template_type=TEMPLATE_TYPE_CUSTOM)
    assert tpl.template_type == TEMPLATE_TYPE_CUSTOM


def test_create_template_nome_vazio_falha(engine):
    with pytest.raises(CampoInvalidoError):
        engine.create_template(name="   ", layout=_valid_layout())


def test_create_template_type_invalido_falha(engine):
    with pytest.raises(VocabularioInvalidoError):
        engine.create_template(name="X", layout=_valid_layout(), template_type="ALIEN")


def test_get_template_existente(engine):
    tpl = engine.create_template(name="X", layout=_valid_layout())
    fetched = engine.get_template(tpl.id)
    assert fetched.id == tpl.id
    assert fetched.name == "X"


def test_get_template_inexistente_falha_estruturado(engine):
    with pytest.raises(TemplateNaoEncontradoError):
        engine.get_template("00000000-0000-0000-0000-000000000000")


def test_update_template_nome_e_layout(engine):
    tpl = engine.create_template(name="X", layout=_valid_layout())
    novo_layout = _valid_layout({"x": 0.2, "y": 0.2, "width": 0.5, "height": 0.5})
    updated = engine.update_template(tpl.id, name="Y", layout=novo_layout)
    assert updated.name == "Y"
    assert updated.layout["zones"][1]["x"] == 0.2
    reloaded = engine.get_template(tpl.id)
    assert reloaded.name == "Y"


def test_update_template_campo_desconhecido_falha(engine):
    tpl = engine.create_template(name="X", layout=_valid_layout())
    with pytest.raises(CampoInvalidoError):
        engine.update_template(tpl.id, campo_fantasma=123)


def test_update_template_enabled(engine):
    tpl = engine.create_template(name="X", layout=_valid_layout())
    updated = engine.update_template(tpl.id, enabled=False)
    assert updated.enabled is False


def test_update_template_layout_invalido_nao_persiste(engine):
    tpl = engine.create_template(name="X", layout=_valid_layout())
    with pytest.raises(LayoutInvalidoError):
        engine.update_template(tpl.id, layout={"zones": []})
    reloaded = engine.get_template(tpl.id)
    assert len(reloaded.layout["zones"]) == 2  # layout original preservado


def test_list_templates_vazio(engine):
    assert engine.list_templates() == []


def test_list_templates_filtra_por_template_type(engine):
    engine.create_template(name="Custom 1", layout=_valid_layout())
    engine.ingest_builtin_templates()
    custom = engine.list_templates(template_type=TEMPLATE_TYPE_CUSTOM)
    builtin = engine.list_templates(template_type=TEMPLATE_TYPE_BUILT_IN)
    assert len(custom) == 1
    assert len(builtin) == 2


def test_list_templates_enabled_only(engine):
    tpl = engine.create_template(name="X", layout=_valid_layout())
    engine.update_template(tpl.id, enabled=False)
    engine.create_template(name="Y", layout=_valid_layout())
    assert len(engine.list_templates(enabled_only=True)) == 1
    assert len(engine.list_templates()) == 2


# ---------------------------------------------------------------------------
# Matriz adversarial de geometria (mesmo espírito de test_visual_editor.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "video_zone",
    [
        {"x": -0.01, "y": 0.1, "width": 0.5, "height": 0.5},
        {"x": 0.1, "y": -0.01, "width": 0.5, "height": 0.5},
        {"x": 1.01, "y": 0.1, "width": 0.5, "height": 0.5},
        {"x": 0.1, "y": 1.01, "width": 0.5, "height": 0.5},
        {"x": 0.6, "y": 0.1, "width": 0.5, "height": 0.5},   # overflow direita
        {"x": 0.1, "y": 0.6, "width": 0.5, "height": 0.5},   # overflow baixo
        {"x": 0.1, "y": 0.1, "width": 0.0, "height": 0.5},   # width == 0
        {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.0},   # height == 0
        {"x": 0.1, "y": 0.1, "width": -0.1, "height": 0.5},  # width negativo
    ],
)
def test_validate_layout_geometria_invalida_rejeitada(video_zone):
    with pytest.raises(GeometriaInvalidaError):
        validate_layout(_valid_layout(video_zone))


def test_validate_layout_geometria_na_borda_exata_aceita():
    # x + width == 1.0 exatamente (sem overflow) deve ser aceito.
    layout = validate_layout(_valid_layout({"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}))
    assert layout["zones"][1]["width"] == 0.5


def test_validate_layout_background_fora_do_frame_inteiro_rejeitado():
    layout = {
        "zones": [
            {"zone_type": "BACKGROUND", "x": 0.1, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
        ]
    }
    with pytest.raises(GeometriaInvalidaError):
        validate_layout(layout)


def test_validate_layout_zones_nao_e_lista_falha():
    with pytest.raises(LayoutInvalidoError):
        validate_layout({"zones": "nao e uma lista"})


def test_validate_layout_zones_vazia_falha():
    with pytest.raises(LayoutInvalidoError):
        validate_layout({"zones": []})


def test_validate_layout_campo_topo_desconhecido_falha():
    layout = _valid_layout()
    layout["campo_fantasma"] = 1
    with pytest.raises(LayoutInvalidoError):
        validate_layout(layout)


def test_validate_layout_sem_background_falha():
    layout = {"zones": [{"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}]}
    with pytest.raises(LayoutInvalidoError):
        validate_layout(layout)


def test_validate_layout_dois_backgrounds_falha():
    layout = {
        "zones": [
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
        ]
    }
    with pytest.raises(LayoutInvalidoError):
        validate_layout(layout)


def test_validate_layout_background_nao_e_primeira_falha():
    layout = {
        "zones": [
            {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
        ]
    }
    with pytest.raises(LayoutInvalidoError):
        validate_layout(layout)


def test_validate_layout_sem_video_falha():
    layout = {"zones": [{"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}]}
    with pytest.raises(LayoutInvalidoError):
        validate_layout(layout)


def test_validate_layout_dois_videos_falha():
    layout = {
        "zones": [
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "VIDEO", "x": 0.0, "y": 0.0, "width": 0.4, "height": 0.4},
            {"zone_type": "VIDEO", "x": 0.5, "y": 0.5, "width": 0.4, "height": 0.4},
        ]
    }
    with pytest.raises(LayoutInvalidoError):
        validate_layout(layout)


def test_validate_layout_multiplas_zonas_logo_permitidas():
    layout = {
        "zones": [
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "VIDEO", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5},
            {"zone_type": "LOGO", "x": 0.6, "y": 0.6, "width": 0.1, "height": 0.1},
            {"zone_type": "LOGO", "x": 0.8, "y": 0.8, "width": 0.1, "height": 0.1},
            {"zone_type": "CAPTION", "x": 0.0, "y": 0.85, "width": 1.0, "height": 0.1},
            {"zone_type": "AI_TEXT", "x": 0.0, "y": 0.0, "width": 0.3, "height": 0.1},
        ]
    }
    validated = validate_layout(layout)
    assert len(validated["zones"]) == 6


def test_validate_layout_zone_type_desconhecido_falha():
    layout = {
        "zones": [
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "ALIENIGENA", "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
        ]
    }
    with pytest.raises(VocabularioInvalidoError):
        validate_layout(layout)


def test_validate_layout_style_valido():
    layout = _valid_layout({"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5, "style": {"opacity": 0.5, "background_color": "#ff00aa"}})
    validated = validate_layout(layout)
    style = validated["zones"][1]["style"]
    assert style["opacity"] == 0.5
    assert style["background_color"] == "#FF00AA"


def test_validate_layout_style_campo_desconhecido_falha():
    layout = _valid_layout({"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5, "style": {"font": "Arial"}})
    with pytest.raises(CampoInvalidoError):
        validate_layout(layout)


def test_validate_layout_style_opacity_fora_do_range_falha():
    layout = _valid_layout({"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5, "style": {"opacity": 1.5}})
    with pytest.raises(GeometriaInvalidaError):
        validate_layout(layout)


@pytest.mark.parametrize("cor", ["not-a-color", "#GGGGGG", "#12345", 123])
def test_validate_layout_style_cor_invalida_falha(cor):
    layout = _valid_layout({"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5, "style": {"background_color": cor}})
    with pytest.raises(CampoInvalidoError):
        validate_layout(layout)


# ---------------------------------------------------------------------------
# Ingestão dos templates oficiais built-in
# ---------------------------------------------------------------------------


def test_ingest_builtin_templates_cria_2_templates(engine):
    result = engine.ingest_builtin_templates()
    assert len(result) == 2
    assert {t.template_type for t in result} == {TEMPLATE_TYPE_BUILT_IN}
    assert all(t.enabled is True for t in result)
    nomes = {t.name for t in result}
    assert nomes == {"Moldura Tech Azul", "Moldura Branca Clássica"}


def test_ingest_builtin_templates_copia_nunca_in_place(engine, app_paths):
    result = engine.ingest_builtin_templates()
    for tpl in result:
        managed_path = Path(tpl.source_path)
        assert managed_path.is_file()
        assert Path(app_paths.templates) in managed_path.parents
        assert DEFAULT_BUILTIN_ASSETS_DIR not in managed_path.parents


def test_ingest_builtin_templates_originais_nunca_alterados(engine):
    hashes_antes = {}
    for spec in BUILTIN_TEMPLATE_SPECS:
        p = DEFAULT_BUILTIN_ASSETS_DIR / spec["source_filename"]
        hashes_antes[spec["slug"]] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)

    engine.ingest_builtin_templates()
    engine.ingest_builtin_templates()  # segunda chamada -- ainda assim nunca toca o original

    for spec in BUILTIN_TEMPLATE_SPECS:
        p = DEFAULT_BUILTIN_ASSETS_DIR / spec["source_filename"]
        hash_depois = hashlib.sha256(p.read_bytes()).hexdigest()
        mtime_depois = p.stat().st_mtime_ns
        hash_antes, mtime_antes = hashes_antes[spec["slug"]]
        assert hash_depois == hash_antes
        assert mtime_depois == mtime_antes


def test_ingest_builtin_templates_e_idempotente_sequencial(engine):
    r1 = engine.ingest_builtin_templates()
    r2 = engine.ingest_builtin_templates()
    assert [t.id for t in r1] == [t.id for t in r2]
    assert len(engine.list_templates(template_type=TEMPLATE_TYPE_BUILT_IN)) == 2


def test_ingest_builtin_templates_preserva_layout_ajustado_apos_reingestao(engine):
    r1 = engine.ingest_builtin_templates()
    tpl = r1[0]
    novo_layout = _valid_layout({"x": 0.2, "y": 0.2, "width": 0.3, "height": 0.3})
    engine.update_template(tpl.id, layout=novo_layout)

    engine.ingest_builtin_templates()  # re-ingestao nao pode reverter o ajuste

    reloaded = engine.get_template(tpl.id)
    assert reloaded.layout["zones"][1]["width"] == 0.3


def test_ingest_builtin_templates_auto_cura_arquivo_apagado(engine):
    r1 = engine.ingest_builtin_templates()
    managed_path = Path(r1[0].source_path)
    assert managed_path.is_file()
    managed_path.unlink()
    assert not managed_path.is_file()

    engine.ingest_builtin_templates()
    assert managed_path.is_file()


def test_ingest_builtin_templates_arquivo_oficial_ausente_falha_sem_substituto_generico(engine, tmp_path):
    vazio = tmp_path / "assets_vazios"
    vazio.mkdir()
    with pytest.raises(ArquivoOficialAusenteError):
        engine.ingest_builtin_templates(assets_dir=vazio)
    assert engine.list_templates() == []


def test_ingest_builtin_templates_concorrente_duas_instancias_nao_duplica(app_paths):
    db_path = app_paths.database / "painel.db"
    db_a = LocalDatabase(db_path)
    db_a.initialize()
    storage_a = StorageManager(app_paths, database_path=db_path)
    engine_a = TemplateEngine(db_a, storage_manager=storage_a, app_paths=app_paths)

    db_b = LocalDatabase(db_path)
    storage_b = StorageManager(app_paths, database_path=db_path)
    engine_b = TemplateEngine(db_b, storage_manager=storage_b, app_paths=app_paths)

    resultados: list = [None, None]
    barreira = threading.Barrier(2)

    def _run(idx, engine_instance):
        barreira.wait(timeout=10)
        resultados[idx] = engine_instance.ingest_builtin_templates()

    t1 = threading.Thread(target=_run, args=(0, engine_a))
    t2 = threading.Thread(target=_run, args=(1, engine_b))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert resultados[0] is not None and resultados[1] is not None
    verificador = TemplateEngine(LocalDatabase(db_path), storage_manager=storage_a, app_paths=app_paths)
    final = verificador.list_templates(template_type=TEMPLATE_TYPE_BUILT_IN)
    assert len(final) == 2  # nunca 4 -- nenhuma duplicata por corrida


# ---------------------------------------------------------------------------
# Prova adversarial central: arquitetura NÃO depende de "exatamente 2"
# ---------------------------------------------------------------------------


def test_terceiro_template_custom_coexiste_sem_hardcode_de_dois(engine):
    engine.ingest_builtin_templates()
    terceiro = engine.create_template(name="Meu Template Extra", layout=_valid_layout(), template_type=TEMPLATE_TYPE_CUSTOM)

    todos = engine.list_templates()
    assert len(todos) == 3
    assert terceiro.id in {t.id for t in todos}
    builtins = engine.list_templates(template_type=TEMPLATE_TYPE_BUILT_IN)
    assert len(builtins) == 2  # os 2 built-ins continuam intactos e distintos do terceiro


def test_builtin_template_specs_e_uma_sequencia_iteravel_nao_um_numero_fixo():
    # Prova estrutural: a lista de specs é uma tupla iterável -- adicionar
    # um 3º/4º built-in no futuro é só adicionar uma entrada, nenhuma
    # lógica de ingestão precisa mudar.
    assert isinstance(BUILTIN_TEMPLATE_SPECS, tuple)
    assert len(BUILTIN_TEMPLATE_SPECS) == 2
    slugs = [spec["slug"] for spec in BUILTIN_TEMPLATE_SPECS]
    assert len(slugs) == len(set(slugs))  # slugs únicos


# ---------------------------------------------------------------------------
# Preview / thumbnail (metadata-only, ver seção 5 da docstring do módulo)
# ---------------------------------------------------------------------------


def test_get_preview_info_builtin(engine):
    result = engine.ingest_builtin_templates()
    tpl = result[0]
    info = engine.get_preview_info(tpl.id)
    assert info.source_path == tpl.source_path
    assert info.width == 941
    assert info.height == 1672


def test_get_preview_info_custom_sem_dimensoes(engine):
    tpl = engine.create_template(name="X", layout=_valid_layout())
    info = engine.get_preview_info(tpl.id)
    assert info.source_path is None
    assert info.width is None
    assert info.height is None


def test_ingest_builtin_templates_nao_gera_arquivo_de_thumbnail_fisico(engine, app_paths):
    result = engine.ingest_builtin_templates()
    arquivos_no_diretorio_templates = list(Path(app_paths.templates).iterdir())
    # Exatamente 2 arquivos (as cópias gerenciadas dos originais) -- nenhum
    # thumbnail redimensionado extra é gerado nesta etapa (decisão
    # metadata-only, ver seção 5 da docstring do módulo).
    assert len(arquivos_no_diretorio_templates) == 2
    for tpl in result:
        assert Path(tpl.source_path) in arquivos_no_diretorio_templates


# ---------------------------------------------------------------------------
# Restart (novas instâncias, nunca reaproveitar objetos em memória)
# ---------------------------------------------------------------------------


def test_restart_preserva_templates_e_arquivos(app_paths):
    db_path = app_paths.database / "painel.db"
    db1 = LocalDatabase(db_path)
    db1.initialize()
    storage1 = StorageManager(app_paths, database_path=db_path)
    engine1 = TemplateEngine(db1, storage_manager=storage1, app_paths=app_paths)
    builtins = engine1.ingest_builtin_templates()
    custom = engine1.create_template(name="Persistente", layout=_valid_layout())

    # Novas instâncias -- nunca reaproveitar objetos em memória.
    db2 = LocalDatabase(db_path)
    storage2 = StorageManager(app_paths, database_path=db_path)
    engine2 = TemplateEngine(db2, storage_manager=storage2, app_paths=app_paths)

    reloaded_all = engine2.list_templates()
    assert len(reloaded_all) == 3
    reloaded_custom = engine2.get_template(custom.id)
    assert reloaded_custom.name == "Persistente"
    for tpl in builtins:
        reloaded_builtin = engine2.get_template(tpl.id)
        assert Path(reloaded_builtin.source_path).is_file()


# ---------------------------------------------------------------------------
# Construtor -- validação de dependências (item 8 do GATE ADVERSARIAL)
# ---------------------------------------------------------------------------


def test_init_rejeita_database_de_tipo_errado(storage, app_paths):
    with pytest.raises(TypeError):
        TemplateEngine("nao e um database", storage_manager=storage, app_paths=app_paths)


def test_init_rejeita_storage_manager_de_tipo_errado(database, app_paths):
    with pytest.raises(TypeError):
        TemplateEngine(database, storage_manager="nao e um storage manager", app_paths=app_paths)


def test_init_rejeita_app_paths_de_tipo_errado(database, storage):
    with pytest.raises(TypeError):
        TemplateEngine(database, storage_manager=storage, app_paths="nao e um app_paths")


# ---------------------------------------------------------------------------
# Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def _parse_module() -> ast.AST:
    return ast.parse(Path(template_engine_module.__file__).read_text(encoding="utf-8"))


def test_modulo_nao_importa_modulos_protegidos_nem_irmaos():
    tree = _parse_module()
    proibidos = (
        "visual_editor",
        "captions_engine",
        "captions_style",
        "media_catalog",
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "source_import",
        "source_context",
        "video_promotion",
        "timeline_editor",
        "audio_engine",
        "auto_reframe",
        "silence_removal",
        "metadata_manager",
        "limpar_metadados_oficial",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for proibido in proibidos:
            assert proibido not in names, f"template_engine.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_media_catalog_nao_e_modificado():
    media_catalog_path = Path(template_engine_module.__file__).resolve().parent / "media_catalog.py"
    conteudo = media_catalog_path.read_text(encoding="utf-8")
    assert 'BADGE_TEMPLATE_APPLIED = "TEMPLATE_APPLIED"' in conteudo
    assert 'BADGE_TEMPLATE_APPLIED: "0"' in conteudo


def test_nenhuma_migration_nova_schema_version_permanece():
    from _sistema.storage.database import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9


# Dimensoes esperadas dos 2 PNGs oficiais fornecidos pelo dono do produto
# (941x1672px, 9:16 -- confirmado por inspecao direta dos arquivos
# anexados ao Prompt). Usado pela correcao abaixo para de fato COMPARAR
# a dimensao lida contra um valor esperado, em vez de so checar
# ``is_file()`` -- gap que permitiu o bug de truncamento (Prompt 36,
# correcao) passar despercebido por este teste antes.
_EXPECTED_DIMENSIONS_BY_SLUG = {
    "moldura_tech_azul": (941, 1672),
    "moldura_branca_classica": (941, 1672),
}


def test_assets_oficiais_existem_e_tem_as_dimensoes_esperadas():
    assert set(_EXPECTED_DIMENSIONS_BY_SLUG) == {spec["slug"] for spec in BUILTIN_TEMPLATE_SPECS}, (
        "_EXPECTED_DIMENSIONS_BY_SLUG deste teste ficou fora de sincronia com "
        "BUILTIN_TEMPLATE_SPECS -- todo spec precisa de uma dimensao esperada aqui."
    )
    for spec in BUILTIN_TEMPLATE_SPECS:
        p = DEFAULT_BUILTIN_ASSETS_DIR / spec["source_filename"]
        assert p.is_file(), f"asset oficial ausente: {p}"

        largura, altura = template_engine_module._read_image_dimensions(p)
        largura_esperada, altura_esperada = _EXPECTED_DIMENSIONS_BY_SLUG[spec["slug"]]
        assert (largura, altura) == (largura_esperada, altura_esperada), (
            f"dimensao inesperada para {spec['slug']!r} ({p}): "
            f"lida={largura}x{altura}, esperada={largura_esperada}x{altura_esperada} -- "
            f"isto teria detectado o bug de truncamento (Windows/0x1A) corrigido "
            f"neste round, onde o arquivo empacotado virava so 5 bytes."
        )
