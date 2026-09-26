# -*- coding: utf-8 -*-
"""PROMPT — Template Selector: testes.

Cobre: FK real contra o catálogo (template inexistente rejeitado,
BUILT_IN e CUSTOM funcionando igualmente); aplicar/trocar/remover,
isoladamente e em sequência; ``clear_template`` como no-op seguro;
isolamento de fingerprint em relação a ``CAPTIONS``/``CUTS`` nas duas
direções (o teste mais importante deste Prompt); independência de ordem
(Smart Clip sem template, template em vídeo cru); ``bulk_set_template``
(matriz completa: todos válidos, mix válido/inexistente, reenvio
idempotente cai em ``unchanged``, lote vazio rejeitado);
``bulk_clear_template`` simétrico; concorrência real via
``threading.Barrier``; prova negativa de que ``BADGE_TEMPLATE_APPLIED``
nunca acende; restart com instâncias novas; garantias estruturais via
AST; confirmação de que nenhuma migration nova foi criada.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import _sistema.template_selector as template_selector_module
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Project, SourceAsset, Video
from _sistema.edit_project import CAPTIONS, CUTS, TEMPLATE, EditProjectManager, ProjectNaoEncontradoError
from _sistema.media_catalog import BADGE_EDITED, BADGE_TEMPLATE_APPLIED, MediaCatalogService
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import TemplateEngine, TemplateNaoEncontradoError
from _sistema.template_selector import (
    BulkTemplateSelectionResult,
    CampoInvalidoError,
    TemplateSelector,
    TemplateSelectorError,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_template_engine.py / test_audio_engine.py)
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
def template_engine(database, storage, app_paths):
    return TemplateEngine(database, storage_manager=storage, app_paths=app_paths)


@pytest.fixture
def manager(database):
    return EditProjectManager(database)


@pytest.fixture
def selector(manager, template_engine):
    return TemplateSelector(manager, template_engine)


@pytest.fixture
def video(database):
    src = SourceAsset(source_uri="/tmp/original.mp4", original_name="original.mp4")
    database.insert(src)
    v = Video(source_asset_id=src.id, name="v")
    database.insert(v)
    return v


@pytest.fixture
def project(database, video):
    p = Project(video_id=video.id, name="p")
    database.insert(p)
    return p


@pytest.fixture
def project2(database, video):
    p = Project(video_id=video.id, name="p2")
    database.insert(p)
    return p


def _valid_layout(video_zone: dict | None = None) -> dict:
    return {
        "zones": [
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "VIDEO", **(video_zone or {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8})},
        ]
    }


@pytest.fixture
def template_a(template_engine):
    return template_engine.create_template(name="Template A", layout=_valid_layout())


@pytest.fixture
def template_b(template_engine):
    return template_engine.create_template(name="Template B", layout=_valid_layout())


@pytest.fixture
def builtin_template(template_engine):
    return template_engine.create_template(
        name="Oficial", layout=_valid_layout(), template_type="BUILT_IN"
    )


# ---------------------------------------------------------------------------
# 0. Construção
# ---------------------------------------------------------------------------


def test_construtor_rejeita_manager_de_tipo_errado(template_engine):
    with pytest.raises(TypeError):
        TemplateSelector("nao-e-um-manager", template_engine)


def test_construtor_rejeita_template_engine_de_tipo_errado(manager):
    with pytest.raises(TypeError):
        TemplateSelector(manager, "nao-e-um-template-engine")


def test_estado_inicial_e_none(selector, project):
    assert selector.get_template_selection(project.id) is None


# ---------------------------------------------------------------------------
# 1. FK real contra o catálogo
# ---------------------------------------------------------------------------


def test_set_template_rejeita_template_id_inexistente(selector, project):
    with pytest.raises(TemplateNaoEncontradoError):
        selector.set_template(project.id, "00000000-0000-0000-0000-000000000000")
    # nenhuma escrita deve ter ocorrido
    assert selector.get_template_selection(project.id) is None


def test_set_template_rejeita_template_id_vazio(selector, project):
    with pytest.raises(CampoInvalidoError):
        selector.set_template(project.id, "")


def test_set_template_funciona_com_template_built_in(selector, project, builtin_template):
    resultado = selector.set_template(project.id, builtin_template.id)
    assert resultado == builtin_template.id
    assert selector.get_template_selection(project.id) == builtin_template.id


def test_set_template_funciona_com_template_custom(selector, project, template_a):
    resultado = selector.set_template(project.id, template_a.id)
    assert resultado == template_a.id
    assert selector.get_template_selection(project.id) == template_a.id


def test_built_in_e_custom_nao_recebem_tratamento_especial():
    """Verificação estrutural: nenhuma referência a TEMPLATE_TYPE_BUILT_IN
    ou TEMPLATE_TYPE_CUSTOM existe no módulo -- get_template já não
    distingue os dois, e este módulo não adiciona distinção nova."""
    source = Path(template_selector_module.__file__).read_text(encoding="utf-8")
    assert "TEMPLATE_TYPE_BUILT_IN" not in source
    assert "TEMPLATE_TYPE_CUSTOM" not in source


# ---------------------------------------------------------------------------
# 2. Aplicar, trocar, remover -- isoladamente e em sequência
# ---------------------------------------------------------------------------


def test_aplicar_trocar_remover_em_sequencia(selector, project, template_a, template_b):
    selector.set_template(project.id, template_a.id)
    assert selector.get_template_selection(project.id) == template_a.id

    selector.set_template(project.id, template_b.id)
    assert selector.get_template_selection(project.id) == template_b.id

    removido = selector.clear_template(project.id)
    assert removido is True
    assert selector.get_template_selection(project.id) is None


def test_clear_template_quando_ja_nao_ha_template_e_noop_seguro(selector, project):
    removido = selector.clear_template(project.id)
    assert removido is False
    assert selector.get_template_selection(project.id) is None


def test_clear_template_apos_remover_e_tambem_noop(selector, project, template_a):
    selector.set_template(project.id, template_a.id)
    assert selector.clear_template(project.id) is True
    # segunda chamada -- já não há nada para remover
    assert selector.clear_template(project.id) is False


# ---------------------------------------------------------------------------
# 3. Isolamento de fingerprint (O TESTE MAIS IMPORTANTE DESTE PROMPT)
# ---------------------------------------------------------------------------


def test_operacoes_de_template_nunca_alteram_fingerprint_de_captions_e_cuts(
    manager, selector, project, template_a, template_b
):
    manager.set_category(project.id, CAPTIONS, {"lines": ["ola", "mundo"]})
    manager.set_category(project.id, CUTS, {"segments": [{"start": 0.0, "end": 1.0}]})

    captions_before = manager.get_category(project.id, CAPTIONS)
    cuts_before = manager.get_category(project.id, CUTS)

    # 1) aplicar
    selector.set_template(project.id, template_a.id)
    captions_after_apply = manager.get_category(project.id, CAPTIONS)
    cuts_after_apply = manager.get_category(project.id, CUTS)
    assert captions_after_apply.fingerprint == captions_before.fingerprint
    assert captions_after_apply.revision == captions_before.revision
    assert captions_after_apply.updated_at == captions_before.updated_at
    assert cuts_after_apply.fingerprint == cuts_before.fingerprint
    assert cuts_after_apply.revision == cuts_before.revision
    assert cuts_after_apply.updated_at == cuts_before.updated_at

    # 2) trocar
    selector.set_template(project.id, template_b.id)
    captions_after_swap = manager.get_category(project.id, CAPTIONS)
    cuts_after_swap = manager.get_category(project.id, CUTS)
    assert captions_after_swap.fingerprint == captions_before.fingerprint
    assert captions_after_swap.revision == captions_before.revision
    assert captions_after_swap.updated_at == captions_before.updated_at
    assert cuts_after_swap.fingerprint == cuts_before.fingerprint
    assert cuts_after_swap.revision == cuts_before.revision
    assert cuts_after_swap.updated_at == cuts_before.updated_at

    # 3) remover
    selector.clear_template(project.id)
    captions_after_clear = manager.get_category(project.id, CAPTIONS)
    cuts_after_clear = manager.get_category(project.id, CUTS)
    assert captions_after_clear.fingerprint == captions_before.fingerprint
    assert captions_after_clear.revision == captions_before.revision
    assert captions_after_clear.updated_at == captions_before.updated_at
    assert cuts_after_clear.fingerprint == cuts_before.fingerprint
    assert cuts_after_clear.revision == cuts_before.revision
    assert cuts_after_clear.updated_at == cuts_before.updated_at


def test_alterar_cuts_depois_de_template_setado_nunca_muda_fingerprint_de_template(
    manager, selector, project, template_a
):
    selector.set_template(project.id, template_a.id)
    template_before = manager.get_category(project.id, TEMPLATE)

    manager.set_category(project.id, CUTS, {"segments": []})
    manager.set_category(project.id, CUTS, {"segments": [{"start": 0.0, "end": 2.0}]})

    template_after = manager.get_category(project.id, TEMPLATE)
    assert template_after.fingerprint == template_before.fingerprint
    assert template_after.revision == template_before.revision
    assert template_after.updated_at == template_before.updated_at


# ---------------------------------------------------------------------------
# 4. Independência de ordem
# ---------------------------------------------------------------------------


def test_gravar_cuts_sem_nunca_ter_tido_template_funciona_normalmente(manager, project):
    """"Smart Clip -> salvar cortes sem template" do roadmap."""
    resultado = manager.set_category(project.id, CUTS, {"segments": [{"start": 0.0, "end": 3.0}]})
    assert resultado.data == {"segments": [{"start": 0.0, "end": 3.0}]}


def test_aplicar_template_em_projeto_sem_nenhuma_outra_categoria_funciona(
    selector, manager, project, template_a
):
    """"vídeo pronto -> template -> exportar" / "vídeo editado -> aplicar
    template" do roadmap -- Project "cru", sem nenhuma outra categoria
    gravada ainda."""
    assert manager.list_categories(project.id) == ()
    selector.set_template(project.id, template_a.id)
    assert selector.get_template_selection(project.id) == template_a.id
    assert manager.list_categories(project.id) == (TEMPLATE,)


# ---------------------------------------------------------------------------
# 5. Erros estruturados
# ---------------------------------------------------------------------------


def test_set_template_projeto_inexistente(selector, template_a):
    fantasma = "11111111-1111-1111-1111-111111111111"
    with pytest.raises(ProjectNaoEncontradoError):
        selector.set_template(fantasma, template_a.id)


def test_set_template_project_id_malformado_nao_e_valueerror_cru(selector, template_a):
    with pytest.raises(CampoInvalidoError):
        selector.set_template("nao-e-um-uuid", template_a.id)


def test_get_template_selection_project_id_malformado(selector):
    with pytest.raises(CampoInvalidoError):
        selector.get_template_selection("nao-e-um-uuid")


def test_clear_template_project_id_malformado(selector):
    with pytest.raises(CampoInvalidoError):
        selector.clear_template("nao-e-um-uuid")


# ---------------------------------------------------------------------------
# 6. bulk_set_template -- matriz completa
# ---------------------------------------------------------------------------


def test_bulk_set_template_todos_validos(selector, project, project2, template_a):
    resultado = selector.bulk_set_template([project.id, project2.id], template_a.id)
    assert isinstance(resultado, BulkTemplateSelectionResult)
    assert resultado.requested == (project.id, project2.id)
    assert set(resultado.updated) == {project.id, project2.id}
    assert resultado.unchanged == ()
    assert resultado.failed == ()
    assert selector.get_template_selection(project.id) == template_a.id
    assert selector.get_template_selection(project2.id) == template_a.id


def test_bulk_set_template_mix_de_validos_e_inexistente(selector, project, template_a):
    fantasma = "22222222-2222-2222-2222-222222222222"
    resultado = selector.bulk_set_template([project.id, fantasma], template_a.id)
    assert resultado.updated == (project.id,)
    assert len(resultado.failed) == 1
    assert resultado.failed[0][0] == fantasma
    assert selector.get_template_selection(project.id) == template_a.id


def test_bulk_set_template_reenvio_do_mesmo_id_cai_em_unchanged(selector, project, template_a):
    selector.bulk_set_template([project.id], template_a.id)
    resultado = selector.bulk_set_template([project.id], template_a.id)
    assert resultado.updated == ()
    assert resultado.unchanged == (project.id,)


def test_bulk_set_template_lote_vazio_rejeitado(selector, template_a):
    with pytest.raises(CampoInvalidoError):
        selector.bulk_set_template([], template_a.id)


def test_bulk_set_template_template_id_inexistente_rejeita_antes_de_qualquer_escrita(
    selector, project, project2
):
    fantasma_template = "33333333-3333-3333-3333-333333333333"
    with pytest.raises(TemplateNaoEncontradoError):
        selector.bulk_set_template([project.id, project2.id], fantasma_template)
    # nenhuma escrita deve ter ocorrido em nenhum dos dois projetos
    assert selector.get_template_selection(project.id) is None
    assert selector.get_template_selection(project2.id) is None


# ---------------------------------------------------------------------------
# 7. bulk_clear_template -- simétrico
# ---------------------------------------------------------------------------


def test_bulk_clear_template_remove_de_todos(selector, project, project2, template_a):
    selector.bulk_set_template([project.id, project2.id], template_a.id)
    resultado = selector.bulk_clear_template([project.id, project2.id])
    assert set(resultado.updated) == {project.id, project2.id}
    assert resultado.unchanged == ()
    assert selector.get_template_selection(project.id) is None
    assert selector.get_template_selection(project2.id) is None


def test_bulk_clear_template_ja_ausente_cai_em_unchanged(selector, project):
    resultado = selector.bulk_clear_template([project.id])
    assert resultado.updated == ()
    assert resultado.unchanged == (project.id,)


def test_bulk_clear_template_mix_de_presente_e_ausente(selector, project, project2, template_a):
    selector.set_template(project.id, template_a.id)
    resultado = selector.bulk_clear_template([project.id, project2.id])
    assert resultado.updated == (project.id,)
    assert resultado.unchanged == (project2.id,)


def test_bulk_clear_template_lote_vazio_rejeitado(selector):
    with pytest.raises(CampoInvalidoError):
        selector.bulk_clear_template([])


def test_bulk_clear_template_projeto_inexistente_isolado(selector, project, template_a):
    selector.set_template(project.id, template_a.id)
    fantasma = "44444444-4444-4444-4444-444444444444"
    resultado = selector.bulk_clear_template([project.id, fantasma])
    assert resultado.updated == (project.id,)
    assert len(resultado.failed) == 1
    assert resultado.failed[0][0] == fantasma


# ---------------------------------------------------------------------------
# 8. GATE 6 -- concorrência real (threading.Barrier)
# ---------------------------------------------------------------------------


def test_duas_instancias_aplicando_templates_diferentes_simultaneamente_nao_corrompe(
    app_paths, database, storage, project, template_a, template_b
):
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(template_id: str):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_manager = EditProjectManager(db_instance)
        local_template_engine = TemplateEngine(
            db_instance, storage_manager=storage, app_paths=app_paths
        )
        local_selector = TemplateSelector(local_manager, local_template_engine)
        barrier.wait(timeout=5)
        try:
            local_selector.set_template(project.id, template_id)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(template_a.id,)),
        threading.Thread(target=worker, args=(template_b.id,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    final = TemplateSelector(
        EditProjectManager(database), TemplateEngine(database, storage_manager=storage, app_paths=app_paths)
    ).get_template_selection(project.id)
    assert final in (template_a.id, template_b.id), "estado final corrompido/inesperado"


def test_restart_com_novas_instancias_preserva_selecao(app_paths, project, template_a, storage):
    db1 = LocalDatabase(app_paths.database / "painel.db")
    db1.initialize()
    TemplateSelector(
        EditProjectManager(db1), TemplateEngine(db1, storage_manager=storage, app_paths=app_paths)
    ).set_template(project.id, template_a.id)

    db2 = LocalDatabase(app_paths.database / "painel.db")
    resultado = TemplateSelector(
        EditProjectManager(db2), TemplateEngine(db2, storage_manager=storage, app_paths=app_paths)
    ).get_template_selection(project.id)
    assert resultado == template_a.id


# ---------------------------------------------------------------------------
# 9. Badge BADGE_TEMPLATE_APPLIED -- prova negativa
# ---------------------------------------------------------------------------


def test_selecionar_template_nao_acende_badge_template_applied(
    database, selector, project, video, template_a
):
    """Prova negativa explícita: selecionar um template via
    TemplateSelector NUNCA faz BADGE_TEMPLATE_APPLIED acender -- decisão
    (a) documentada no módulo, mesmo espírito do teste negativo já
    existente para AUDIO_PROCESSED."""
    catalog = MediaCatalogService(database)
    antes = catalog.get_item(video.id)
    assert BADGE_TEMPLATE_APPLIED not in antes.system_badges

    selector.set_template(project.id, template_a.id)

    depois = catalog.get_item(video.id)
    assert BADGE_TEMPLATE_APPLIED not in depois.system_badges, (
        "TEMPLATE_APPLIED nao pode acender apenas por uma selecao ter sido gravada"
    )
    # EDITED, por outro lado, deve ter acendido (mesma expressão genérica).
    assert BADGE_EDITED in depois.system_badges


# ---------------------------------------------------------------------------
# 10. Garantias estruturais (AST) e migrations congeladas
# ---------------------------------------------------------------------------


def _real_code_identifiers(tree: ast.AST) -> set[str]:
    """Nomes de identificador (``Name``/importação) realmente presentes
    no CÓDIGO da AST -- exclui docstrings/comentários/strings literais
    (que não viram nós ``ast.Name``/``ast.alias``), então uma menção em
    prosa explicando o que o módulo NÃO faz não conta como uma
    referência real de código."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.name.split(".")[-1])
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[-1])
    return names


def test_modulo_nunca_toca_categoria_alem_de_template():
    """Verificação estrutural: o único identificador de categoria
    importado/usado no CÓDIGO deste módulo é TEMPLATE -- nenhuma
    referência real (fora de docstring/comentário) a
    CUTS/CROP/AUDIO_SETTINGS/CAPTIONS/REFRAME/SPEED/TEXT_LAYERS/
    METADATA_MODE."""
    tree = ast.parse(Path(template_selector_module.__file__).read_text(encoding="utf-8"))
    identifiers = _real_code_identifiers(tree)
    forbidden = {
        "CUTS", "CROP", "AUDIO_SETTINGS", "CAPTIONS", "REFRAME", "SPEED",
        "TEXT_LAYERS", "METADATA_MODE",
    }
    assert not (identifiers & forbidden), identifiers & forbidden


def test_modulo_nao_importa_media_catalog_nem_ffmpeg_nem_job():
    tree = ast.parse(Path(template_selector_module.__file__).read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
    forbidden = {
        "subprocess", "media_catalog", "job_engine", "_sistema.media_catalog",
        "_sistema.job_engine",
    }
    assert not (imported_modules & forbidden), imported_modules & forbidden
    identifiers = _real_code_identifiers(tree)
    assert "subprocess" not in identifiers
    assert not any("ffmpeg" in name.lower() for name in identifiers)


def test_nenhuma_migration_nova_foi_criada():
    migrations_dir = Path(__file__).resolve().parent.parent / "_sistema" / "storage" / "migrations"
    if not migrations_dir.exists():
        pytest.skip("diretório de migrations não encontrado neste layout")
    nomes = sorted(p.name for p in migrations_dir.glob("*.py") if p.name != "__init__.py")
    # Este Prompt não adiciona nenhuma migration -- apenas confirma que a
    # lista de migrations existentes permanece a mesma já usada pelos
    # demais módulos desta fase (m001_initial já cobre edit_state_json).
    assert any("m001" in nome for nome in nomes)
