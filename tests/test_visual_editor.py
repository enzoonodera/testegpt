# -*- coding: utf-8 -*-
"""PROMPT 29 -- Editor: vídeo e imagem: testes.

Cobre a lista de TESTES do Prompt: cada transformação isolada (sozinha,
item 1.d); cada uma com valor inválido; geometria de crop/resize
inválida; múltiplas transformações persistindo juntas; operação em lote
(sucesso total, sucesso parcial, lote vazio); isolamento de fingerprint
entre categorias; reaproveitamento atômico de ``update_category``;
concorrência real; integração read-only com o badge EDITED; restart;
idempotência; garantias estruturais via AST; e confirmação de que
nenhuma migration nova foi criada.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import _sistema.visual_editor as visual_editor
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Project, SourceAsset, Video
from _sistema.edit_project import CROP, EditProjectManager, TEMPLATE
from _sistema.media_catalog import BADGE_EDITED, MediaCatalogService
from _sistema.storage.database import LocalDatabase
from _sistema.visual_editor import (
    VISUAL_ADJUSTMENTS,
    AdjustmentsState,
    BulkVisualEditResult,
    CampoInvalidoError,
    FrameState,
    GeometriaInvalidaError,
    ModoInvalidoError,
    VisualEditor,
    VisualEditorError,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão dos Prompts 27/27.5/28)
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
def editor(database):
    return VisualEditor(database)


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


# ---------------------------------------------------------------------------
# 1. Cada transformação de ENQUADRAMENTO funcionando SOZINHA (item 1.d)
# ---------------------------------------------------------------------------


def test_estado_inicial_de_enquadramento_e_todo_none(editor, project):
    frame = editor.get_frame(project.id)
    assert frame == FrameState()


def test_set_crop_sozinho_nao_exige_as_demais(editor, project):
    frame = editor.set_crop(project.id, 0.1, 0.2, 0.5, 0.4)
    assert frame.crop == {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.4}
    assert frame.resize is None
    assert frame.fit_mode is None
    assert frame.zoom is None
    assert frame.position is None
    assert frame.rotation is None


def test_set_resize_sozinho(editor, project):
    frame = editor.set_resize(project.id, 1080, 1920)
    assert frame.resize == {"width": 1080, "height": 1920}
    assert frame.crop is None


def test_set_fit_mode_sozinho(editor, project):
    frame = editor.set_fit_mode(project.id, "FIT")
    assert frame.fit_mode == "FIT"
    assert frame.crop is None


def test_set_zoom_sozinho(editor, project):
    frame = editor.set_zoom(project.id, 2.0)
    assert frame.zoom == 2.0
    assert frame.crop is None


def test_set_position_sozinho(editor, project):
    frame = editor.set_position(project.id, -0.2, 0.3)
    assert frame.position == {"x": -0.2, "y": 0.3}
    assert frame.crop is None


def test_set_rotation_sozinho(editor, project):
    frame = editor.set_rotation(project.id, 90)
    assert frame.rotation == 90
    assert frame.crop is None


# ---------------------------------------------------------------------------
# 2. Cada AJUSTE DE IMAGEM/COR funcionando SOZINHO (item 1.d)
# ---------------------------------------------------------------------------


def test_estado_inicial_de_ajustes_e_todo_none(editor, project):
    assert editor.get_adjustments(project.id) == AdjustmentsState()


def test_set_brightness_sozinho(editor, project):
    st = editor.set_brightness(project.id, 0.5)
    assert st.brightness == 0.5
    assert st.contrast is None and st.saturation is None


def test_set_contrast_sozinho(editor, project):
    st = editor.set_contrast(project.id, 1.5)
    assert st.contrast == 1.5
    assert st.brightness is None


def test_set_saturation_sozinho(editor, project):
    st = editor.set_saturation(project.id, 2.0)
    assert st.saturation == 2.0
    assert st.contrast is None


def test_set_gamma_sozinho(editor, project):
    st = editor.set_gamma(project.id, 1.2)
    assert st.gamma == 1.2
    assert st.saturation is None


def test_set_sharpen_sozinho(editor, project):
    st = editor.set_sharpen(project.id, 3.0)
    assert st.sharpen == 3.0
    assert st.gamma is None


def test_set_noise_sozinho(editor, project):
    st = editor.set_noise(project.id, 0.4)
    assert st.noise == 0.4
    assert st.sharpen is None


# ---------------------------------------------------------------------------
# 3. Cada transformação com valor INVÁLIDO (tipo errado, fora de faixa,
#    não-finito) -- erro estruturado correto
# ---------------------------------------------------------------------------


def test_set_crop_tipo_errado_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.set_crop(project.id, "0.1", 0.1, 0.5, 0.5)
    with pytest.raises(CampoInvalidoError):
        editor.set_crop(project.id, True, 0.1, 0.5, 0.5)  # bool nao e' aceito onde se espera float


def test_set_crop_fora_de_faixa_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.set_crop(project.id, -0.1, 0.1, 0.5, 0.5)
    with pytest.raises(CampoInvalidoError):
        editor.set_crop(project.id, 1.5, 0.1, 0.5, 0.5)


def test_set_crop_nao_finito_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.set_crop(project.id, float("inf"), 0.1, 0.5, 0.5)
    with pytest.raises(CampoInvalidoError):
        editor.set_crop(project.id, float("nan"), 0.1, 0.5, 0.5)


def test_set_resize_tipo_errado_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.set_resize(project.id, 1080.5, 1920)  # float onde se espera int
    with pytest.raises(CampoInvalidoError):
        editor.set_resize(project.id, True, 1920)  # bool nao e' aceito onde se espera int


def test_set_resize_fora_de_faixa_e_erro(editor, project):
    with pytest.raises(GeometriaInvalidaError):
        editor.set_resize(project.id, 0, 1920)
    with pytest.raises(GeometriaInvalidaError):
        editor.set_resize(project.id, 1080, 999999)


def test_set_fit_mode_fora_do_vocabulario_e_erro(editor, project):
    with pytest.raises(ModoInvalidoError):
        editor.set_fit_mode(project.id, "CONTAIN")
    with pytest.raises(ModoInvalidoError):
        editor.set_fit_mode(project.id, "fit")  # case-sensitive, vocabulario fechado exato


def test_set_zoom_fora_de_faixa_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.set_zoom(project.id, 0.5)  # abaixo de 1.0
    with pytest.raises(CampoInvalidoError):
        editor.set_zoom(project.id, 5.0)  # acima de 4.0


def test_set_zoom_nao_finito_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.set_zoom(project.id, float("inf"))


def test_set_position_fora_de_faixa_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.set_position(project.id, -2.0, 0.0)
    with pytest.raises(CampoInvalidoError):
        editor.set_position(project.id, 0.0, 2.0)


def test_set_rotation_fora_do_vocabulario_e_erro(editor, project):
    with pytest.raises(ModoInvalidoError):
        editor.set_rotation(project.id, 45)
    with pytest.raises(ModoInvalidoError):
        editor.set_rotation(project.id, -90)
    with pytest.raises(ModoInvalidoError):
        editor.set_rotation(project.id, True)  # bool nao e' aceito onde se espera int


@pytest.mark.parametrize(
    "setter_name,invalid_value",
    [
        ("set_brightness", -2.0),
        ("set_brightness", 2.0),
        ("set_contrast", -1.0),
        ("set_contrast", 4.0),
        ("set_saturation", -1.0),
        ("set_saturation", 4.0),
        ("set_gamma", 0.0),
        ("set_gamma", 4.0),
        ("set_sharpen", -1.0),
        ("set_sharpen", 6.0),
        ("set_noise", -0.1),
        ("set_noise", 1.1),
    ],
)
def test_ajustes_fora_de_faixa_documentada_sao_erro(editor, project, setter_name, invalid_value):
    setter = getattr(editor, setter_name)
    with pytest.raises(CampoInvalidoError):
        setter(project.id, invalid_value)


@pytest.mark.parametrize("setter_name", ["set_brightness", "set_contrast", "set_saturation", "set_gamma", "set_sharpen", "set_noise"])
def test_ajustes_com_valor_nao_finito_sao_erro(editor, project, setter_name):
    setter = getattr(editor, setter_name)
    for valor in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(CampoInvalidoError):
            setter(project.id, valor)


@pytest.mark.parametrize("setter_name", ["set_brightness", "set_contrast", "set_saturation", "set_gamma", "set_sharpen", "set_noise"])
def test_ajustes_com_tipo_errado_bool_nao_e_aceito(editor, project, setter_name):
    """``isinstance(True, int)`` é ``True`` em Python -- armadilha já
    corrigida no Prompt 28, confirmada de novo aqui."""
    setter = getattr(editor, setter_name)
    with pytest.raises(CampoInvalidoError):
        setter(project.id, True)


# ---------------------------------------------------------------------------
# 4. Geometria inválida de crop/resize (largura/altura/fator <= 0)
# ---------------------------------------------------------------------------


def test_crop_largura_zero_ou_negativa_e_erro_geometrico(editor, project):
    with pytest.raises(GeometriaInvalidaError):
        editor.set_crop(project.id, 0.1, 0.1, 0.0, 0.5)
    with pytest.raises(GeometriaInvalidaError):
        editor.set_crop(project.id, 0.1, 0.1, -0.1, 0.5)


def test_crop_altura_zero_ou_negativa_e_erro_geometrico(editor, project):
    with pytest.raises(GeometriaInvalidaError):
        editor.set_crop(project.id, 0.1, 0.1, 0.5, 0.0)


def test_crop_ultrapassa_borda_direita_e_erro_geometrico(editor, project):
    with pytest.raises(GeometriaInvalidaError):
        editor.set_crop(project.id, 0.7, 0.1, 0.5, 0.2)


def test_crop_ultrapassa_borda_inferior_e_erro_geometrico(editor, project):
    with pytest.raises(GeometriaInvalidaError):
        editor.set_crop(project.id, 0.1, 0.7, 0.2, 0.5)


def test_crop_exatamente_no_limite_e_aceito(editor, project):
    """x+width == 1.0 e y+height == 1.0 são válidos (borda, não além dela)."""
    frame = editor.set_crop(project.id, 0.5, 0.5, 0.5, 0.5)
    assert frame.crop == {"x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}


def test_resize_largura_ou_altura_nao_positiva_e_erro_geometrico(editor, project):
    with pytest.raises(GeometriaInvalidaError):
        editor.set_resize(project.id, 0, 1080)
    with pytest.raises(GeometriaInvalidaError):
        editor.set_resize(project.id, 1080, -10)


# ---------------------------------------------------------------------------
# 5. Múltiplas transformações no mesmo projeto persistem e são lidas juntas
# ---------------------------------------------------------------------------


def test_multiplas_transformacoes_de_enquadramento_persistem_juntas(editor, project):
    editor.set_crop(project.id, 0.1, 0.1, 0.5, 0.5)
    editor.set_resize(project.id, 1080, 1920)
    editor.set_fit_mode(project.id, "FILL")
    editor.set_zoom(project.id, 1.5)
    editor.set_position(project.id, 0.1, -0.1)
    editor.set_rotation(project.id, 180)

    frame = editor.get_frame(project.id)
    assert frame.crop == {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}
    assert frame.resize == {"width": 1080, "height": 1920}
    assert frame.fit_mode == "FILL"
    assert frame.zoom == 1.5
    assert frame.position == {"x": 0.1, "y": -0.1}
    assert frame.rotation == 180


def test_multiplos_ajustes_de_imagem_persistem_juntos(editor, project):
    editor.set_brightness(project.id, 0.2)
    editor.set_contrast(project.id, 1.1)
    editor.set_saturation(project.id, 1.3)
    editor.set_gamma(project.id, 0.9)
    editor.set_sharpen(project.id, 2.0)
    editor.set_noise(project.id, 0.1)

    adj = editor.get_adjustments(project.id)
    assert adj == AdjustmentsState(brightness=0.2, contrast=1.1, saturation=1.3, gamma=0.9, sharpen=2.0, noise=0.1)


def test_alterar_um_campo_nao_afeta_os_demais_ja_definidos(editor, project):
    editor.set_crop(project.id, 0.1, 0.1, 0.5, 0.5)
    editor.set_zoom(project.id, 2.0)
    editor.set_zoom(project.id, 3.0)  # sobrescreve só o zoom
    frame = editor.get_frame(project.id)
    assert frame.zoom == 3.0
    assert frame.crop == {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}


# ---------------------------------------------------------------------------
# 6. Operação em lote (item 1.d/seção 2 da docstring do módulo)
# ---------------------------------------------------------------------------


def test_bulk_set_adjustments_sucesso_total(editor, project, project2):
    result = editor.bulk_set_adjustments([project.id, project2.id], brightness=0.3, contrast=1.2)
    assert isinstance(result, BulkVisualEditResult)
    assert result.requested == (project.id, project2.id)
    assert set(result.updated) == {project.id, project2.id}
    assert result.unchanged == ()
    assert result.failed == ()
    assert editor.get_adjustments(project.id).brightness == 0.3
    assert editor.get_adjustments(project2.id).contrast == 1.2


def test_bulk_set_frame_sucesso_total(editor, project, project2):
    result = editor.bulk_set_frame([project.id, project2.id], zoom=2.0, rotation=90)
    assert set(result.updated) == {project.id, project2.id}
    assert editor.get_frame(project.id).zoom == 2.0
    assert editor.get_frame(project2.id).rotation == 90


def test_bulk_sucesso_parcial_projeto_invalido_nao_aborta_os_demais(editor, project, project2):
    result = editor.bulk_set_adjustments([project.id, "nao-existe-mesmo-assim-um-uuid-invalido", project2.id], brightness=0.1)
    assert result.requested == (project.id, "nao-existe-mesmo-assim-um-uuid-invalido", project2.id)
    assert set(result.updated) == {project.id, project2.id}
    assert len(result.failed) == 1
    assert result.failed[0][0] == "nao-existe-mesmo-assim-um-uuid-invalido"
    # nao vazou texto de excecao cru (GATE 10) -- so um codigo curto
    assert " " not in result.failed[0][1]


def test_bulk_com_projeto_inexistente_mas_uuid_valido_falha_isolada(editor, project):
    import uuid

    fake_id = str(uuid.uuid4())
    result = editor.bulk_set_adjustments([project.id, fake_id], brightness=0.2)
    assert result.updated == (project.id,)
    assert len(result.failed) == 1
    assert result.failed[0][0] == fake_id


def test_bulk_project_ids_vazio_devolve_resultado_vazio(editor):
    """Lote vazio de PROJETOS (``project_ids=[]``) é aceito -- devolve um
    resultado vazio, nunca erro (é uma operação legítima sem efeito). Já
    um lote de CAMPOS vazio (nenhum campo a aplicar) É rejeitado -- ver
    ``test_bulk_sem_nenhum_campo_e_erro`` logo abaixo -- não haveria nada
    de fato para fazer, sinal de uso incorreto da API."""
    result = editor.bulk_set_adjustments([], brightness=0.1)
    assert result == BulkVisualEditResult(requested=(), updated=(), unchanged=(), failed=())


def test_bulk_sem_nenhum_campo_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.bulk_set_adjustments([project.id])
    with pytest.raises(CampoInvalidoError):
        editor.bulk_set_frame([project.id])


def test_bulk_com_campo_desconhecido_e_erro(editor, project):
    with pytest.raises(CampoInvalidoError):
        editor.bulk_set_adjustments([project.id], contrastX=1.0)
    with pytest.raises(CampoInvalidoError):
        editor.bulk_set_frame([project.id], cropX={"x": 0, "y": 0, "width": 1, "height": 1})


def test_bulk_valida_uma_vez_antes_de_tocar_qualquer_projeto(editor, project, project2):
    """Um valor inválido no payload do lote falha ANTES de escrever em
    QUALQUER projeto -- nunca aplica parcialmente um valor inválido."""
    with pytest.raises(CampoInvalidoError):
        editor.bulk_set_adjustments([project.id, project2.id], brightness=99.0)
    assert editor.get_adjustments(project.id) == AdjustmentsState()
    assert editor.get_adjustments(project2.id) == AdjustmentsState()


def test_bulk_set_adjustments_valor_repetido_e_unchanged(editor, project):
    editor.set_brightness(project.id, 0.5)
    result = editor.bulk_set_adjustments([project.id], brightness=0.5)
    assert result.updated == ()
    assert result.unchanged == (project.id,)


# ---------------------------------------------------------------------------
# 7. Isolamento de fingerprint entre categorias
# ---------------------------------------------------------------------------


def test_alterar_ajustes_visuais_nunca_muda_fingerprint_de_outra_categoria(database, editor, project):
    manager = EditProjectManager(database)
    manager.set_category(project.id, TEMPLATE, {"template_id": "modelo-1"})
    before = manager.get_category(project.id, TEMPLATE)

    editor.set_brightness(project.id, 0.4)
    editor.set_contrast(project.id, 1.2)

    after = manager.get_category(project.id, TEMPLATE)
    assert before.fingerprint == after.fingerprint
    assert before == after


def test_alterar_enquadramento_nunca_muda_fingerprint_de_ajustes_visuais(database, editor, project):
    editor.set_brightness(project.id, 0.4)
    manager = EditProjectManager(database)
    before = manager.get_category(project.id, VISUAL_ADJUSTMENTS)

    editor.set_crop(project.id, 0.1, 0.1, 0.5, 0.5)
    editor.set_zoom(project.id, 2.0)

    after = manager.get_category(project.id, VISUAL_ADJUSTMENTS)
    assert before.fingerprint == after.fingerprint


def test_categorias_crop_e_visual_adjustments_sao_distintas(database, editor, project):
    editor.set_zoom(project.id, 1.5)
    editor.set_brightness(project.id, 0.2)
    manager = EditProjectManager(database)
    categorias = manager.list_categories(project.id)
    assert CROP in categorias
    assert VISUAL_ADJUSTMENTS in categorias


# ---------------------------------------------------------------------------
# 8. Integração read-only com o badge EDITED do Media Catalog
# ---------------------------------------------------------------------------


def test_definir_crop_faz_o_video_aparecer_como_edited(database, editor, project, video):
    catalog = MediaCatalogService(database)
    antes = catalog.get_item(video.id)
    assert BADGE_EDITED not in antes.system_badges

    editor.set_crop(project.id, 0.1, 0.1, 0.5, 0.5)

    depois = catalog.get_item(video.id)
    assert BADGE_EDITED in depois.system_badges


def test_definir_apenas_ajuste_de_imagem_tambem_faz_o_video_aparecer_como_edited(database, editor, project, video):
    catalog = MediaCatalogService(database)
    antes = catalog.get_item(video.id)
    assert BADGE_EDITED not in antes.system_badges

    editor.set_brightness(project.id, 0.1)

    depois = catalog.get_item(video.id)
    assert BADGE_EDITED in depois.system_badges


# ---------------------------------------------------------------------------
# 9. GATE 4 -- RESTART (instâncias totalmente novas)
# ---------------------------------------------------------------------------


def test_estado_visual_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    editor_a = VisualEditor(db_a)
    editor_a.set_crop(project.id, 0.1, 0.1, 0.5, 0.5)
    editor_a.set_brightness(project.id, 0.3)

    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    editor_b = VisualEditor(db_b)

    frame = editor_b.get_frame(project.id)
    adj = editor_b.get_adjustments(project.id)
    assert frame.crop == {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}
    assert adj.brightness == 0.3


# ---------------------------------------------------------------------------
# 10. GATE 5 -- IDEMPOTÊNCIA
# ---------------------------------------------------------------------------


def test_repetir_o_mesmo_set_brightness_sequencialmente_e_deterministico(editor, project):
    r1 = editor.set_brightness(project.id, 0.4)
    r2 = editor.set_brightness(project.id, 0.4)
    assert r1 == r2 == editor.get_adjustments(project.id)


def test_repetir_o_mesmo_set_crop_sequencialmente_nao_acumula(editor, project):
    r1 = editor.set_crop(project.id, 0.1, 0.1, 0.5, 0.5)
    r2 = editor.set_crop(project.id, 0.1, 0.1, 0.5, 0.5)
    assert r1 == r2
    assert r2.crop == {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}


# ---------------------------------------------------------------------------
# 11. GATE 6 -- concorrência real (threading.Barrier)
# ---------------------------------------------------------------------------


def test_duas_operacoes_concorrentes_em_campos_diferentes_nao_se_perdem(app_paths, database, project):
    """Duas threads reais, sincronizadas por Barrier, cada uma alterando
    um CAMPO DIFERENTE de enquadramento no MESMO projeto simultaneamente.
    Sem ``update_category`` fechando a janela de leitura+decisão+escrita,
    uma das duas mudanças seria silenciosamente apagada."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(fn_name: str, args: tuple):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_editor = VisualEditor(db_instance)
        barrier.wait(timeout=5)
        try:
            getattr(local_editor, fn_name)(project.id, *args)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("set_zoom", (2.0,))),
        threading.Thread(target=worker, args=("set_rotation", (90,))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    final = VisualEditor(database).get_frame(project.id)
    assert final.zoom == 2.0, "mudanca concorrente perdida"
    assert final.rotation == 90, "mudanca concorrente perdida"


def test_duas_operacoes_concorrentes_em_categorias_diferentes_nao_se_perdem(app_paths, database, project):
    """Uma thread altera CROP, outra altera VISUAL_ADJUSTMENTS no MESMO
    projeto simultaneamente -- categorias distintas, cada write atômica
    isolada, nenhuma deveria interferir na outra."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(fn_name: str, args: tuple):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_editor = VisualEditor(db_instance)
        barrier.wait(timeout=5)
        try:
            getattr(local_editor, fn_name)(project.id, *args)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("set_zoom", (1.8,))),
        threading.Thread(target=worker, args=("set_saturation", (1.4,))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    reread = VisualEditor(database)
    assert reread.get_frame(project.id).zoom == 1.8
    assert reread.get_adjustments(project.id).saturation == 1.4


# ---------------------------------------------------------------------------
# 12. Garantias estruturais (AST) e schema
# ---------------------------------------------------------------------------


def _parse_visual_editor_module() -> ast.AST:
    return ast.parse(Path(visual_editor.__file__).read_text(encoding="utf-8"))


def test_visual_editor_nao_importa_modulos_protegidos():
    tree = _parse_visual_editor_module()
    proibidos = (
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "job_state_machine",
        "source_import",
        "source_context",
        "media_probe",
        "video_promotion",
        "media_catalog",
        "timeline_editor",
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
            assert proibido not in names, f"visual_editor.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_visual_editor_nao_chama_subprocess_ffmpeg_ffprobe():
    """AST, nunca grep textual ingênuo -- a própria docstring do módulo
    cita 'ffmpeg'/'ffprobe'/'subprocess' para explicar por que não são
    usados (mesmo problema já enfrentado nos Prompts anteriores)."""
    tree = _parse_visual_editor_module()
    proibidos = ("subprocess", "ffmpeg", "ffprobe")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id.lower() not in proibidos
        elif isinstance(node, ast.Attribute):
            assert node.attr.lower() not in proibidos
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] + [alias.name for alias in node.names]
            )
            for name in names:
                assert not any(p in name.lower() for p in proibidos)


def test_visual_editor_nunca_cria_job_artifact_publication_schedule_video():
    source = Path(visual_editor.__file__).read_text(encoding="utf-8")
    for proibido in ("Job(", "Artifact(", "Publication(", "Schedule(", "Video("):
        assert proibido not in source


def test_visual_editor_nao_abre_transacao_propria_nem_toca_storage_diretamente():
    """AST, não grep textual ingênuo -- verificação real é sobre CHAMADAS
    (nós ast.Attribute/ast.Call), nunca sobre a string aparecer em algum
    lugar do arquivo (que incluiria comentários/docstrings)."""
    tree = _parse_visual_editor_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "transaction"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("get", "save", "insert", "delete"):
                assert not (
                    isinstance(node.func.value, ast.Name) and node.func.value.id == "database"
                ), "visual_editor.py não pode chamar métodos de LocalDatabase diretamente"


def test_leitura_rejeita_sub_objeto_corrompido_persistido_diretamente(database, project):
    """Achado no GATE ADVERSARIAL: ``dict("string")`` tenta iterar a
    string como pares chave/valor e levanta um ``ValueError`` cru --
    ``get_frame`` não pode deixar isso escapar caso o ``data`` persistido
    (por qualquer via, mesmo fora deste módulo) tenha um sub-objeto
    corrompido (ex.: ``crop`` gravado como string em vez de objeto)."""
    manager = EditProjectManager(database)
    manager.set_category(project.id, CROP, {"crop": "nao-e-um-objeto"})
    editor = VisualEditor(database)
    with pytest.raises(CampoInvalidoError):
        editor.get_frame(project.id)


def test_nenhuma_migration_nova_criada_por_este_prompt():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9
