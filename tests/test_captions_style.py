# -*- coding: utf-8 -*-
"""PROMPT 32 -- Estilos de Legenda: testes.

Cobre a lista de TESTES do Prompt: cada preset aplicado produz
exatamente os valores esperados (campo a campo); os 3+ presets são
genuinamente distintos entre si; cada campo com valor inválido (range,
tipo, bool-como-int/float, enum fora do vocabulário, cor malformada,
unicode em ``font``); isolamento de fingerprint da categoria
``captions_style`` em relação a ``CAPTIONS``/``AUDIO_SETTINGS``/``CROP``
nas duas direções; ``update_category`` atômico (nunca
``get_category``+``set_category`` separados); restart com instâncias
totalmente novas; idempotência; concorrência real via
``threading.Barrier``; operação em lote (sucesso total, parcial,
pré-validação antes de qualquer escrita); ``project_id`` não-UUID nunca
escapa como ``ValueError`` cru; garantias estruturais via AST; e
confirmação de que nenhuma migration nova foi criada.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import _sistema.captions_style as captions_style
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.captions_style import (
    ALIGNMENTS,
    ANIMATIONS,
    BulkCaptionsStyleResult,
    CampoInvalidoError,
    CAPTIONS_STYLE,
    CaptionsStyleEngine,
    CaptionsStyleError,
    CaptionsStyleState,
    CorInvalidaError,
    PRESET_BOLD_SOCIAL,
    PRESET_CLASSIC,
    PRESET_KARAOKE,
    PRESETS,
    PresetInvalidoError,
    VocabularioInvalidoError,
)
from _sistema.domain import Project, SourceAsset, Video
from _sistema.edit_project import AUDIO_SETTINGS, CAPTIONS, CROP, EditProjectManager
from _sistema.storage.database import LocalDatabase


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão dos Prompts 27/27.5/28/29/30)
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
def manager(database):
    return EditProjectManager(database)


@pytest.fixture
def engine(manager):
    return CaptionsStyleEngine(manager)


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


def _parse_module() -> ast.AST:
    source = Path(captions_style.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


# ---------------------------------------------------------------------------
# 0. Construção
# ---------------------------------------------------------------------------


def test_construtor_rejeita_manager_de_tipo_errado():
    with pytest.raises(TypeError):
        CaptionsStyleEngine(manager="nao-e-um-manager")


# ---------------------------------------------------------------------------
# 1. Estado inicial / leitura
# ---------------------------------------------------------------------------


def test_estado_inicial_e_tudo_none(engine, project):
    state = engine.get_style(project.id)
    assert state == CaptionsStyleState()


# ---------------------------------------------------------------------------
# 2. Presets -- valores exatos, distinção par a par
# ---------------------------------------------------------------------------


def test_preset_classic_produz_valores_exatos(engine, project):
    state = engine.apply_preset(project.id, PRESET_CLASSIC)
    assert state.font == "Arial"
    assert state.size == 32.0
    assert state.position == {"x": 0.5, "y": 0.85}
    assert state.alignment == "CENTER"
    assert state.stroke == {"enabled": True, "color": "#000000", "width": 2.0}
    assert state.shadow == {
        "enabled": False,
        "color": "#000000",
        "blur": 0.0,
        "offset_x": 0.0,
        "offset_y": 0.0,
    }
    assert state.background == {"enabled": False, "color": "#000000", "opacity": 0.0}
    assert state.max_words == 6
    assert state.lines == 2
    assert state.animation == "NONE"
    assert state.current_word_highlight == {"enabled": False, "color": "#FFFFFF"}


def test_preset_bold_social_produz_valores_exatos(engine, project):
    state = engine.apply_preset(project.id, PRESET_BOLD_SOCIAL)
    assert state.font == "Montserrat"
    assert state.size == 52.0
    assert state.position == {"x": 0.5, "y": 0.5}
    assert state.stroke == {"enabled": True, "color": "#000000", "width": 5.0}
    assert state.shadow == {
        "enabled": True,
        "color": "#000000",
        "blur": 6.0,
        "offset_x": 2.0,
        "offset_y": 2.0,
    }
    assert state.background == {"enabled": True, "color": "#000000AA", "opacity": 0.6}
    assert state.max_words == 3
    assert state.lines == 1
    assert state.animation == "POP"
    assert state.current_word_highlight == {"enabled": False, "color": "#FFFF00"}


def test_preset_karaoke_produz_valores_exatos(engine, project):
    state = engine.apply_preset(project.id, PRESET_KARAOKE)
    assert state.font == "Poppins"
    assert state.size == 40.0
    assert state.position == {"x": 0.5, "y": 0.9}
    assert state.max_words == 8
    assert state.lines == 1
    assert state.animation == "FADE"
    assert state.current_word_highlight == {"enabled": True, "color": "#FFEE00"}
    assert state.background == {"enabled": False, "color": "#000000", "opacity": 0.0}


def test_pelo_menos_tres_presets_definidos():
    assert len(PRESETS) >= 3
    assert {PRESET_CLASSIC, PRESET_BOLD_SOCIAL, PRESET_KARAOKE} <= PRESETS


def test_presets_sao_genuinamente_distintos_par_a_par(engine, project, project2, database):
    from _sistema.domain import Project as _Project

    ids = []
    for name in sorted(PRESETS):
        pid = _Project(name=f"preset-check-{name}")
        database.insert(pid)
        ids.append((name, pid.id))

    payloads = {}
    for name, pid in ids:
        state = engine.apply_preset(pid, name)
        payloads[name] = state

    names = list(payloads)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            assert payloads[names[i]] != payloads[names[j]], (
                f"presets {names[i]!r} e {names[j]!r} sao identicos"
            )


def test_apply_preset_com_nome_desconhecido_e_erro_estruturado(engine, project):
    with pytest.raises(PresetInvalidoError):
        engine.apply_preset(project.id, "NAO_EXISTE")


def test_apply_preset_e_substituicao_completa_nao_merge(engine, project):
    engine.set_style(project.id, font="CustomFont", max_words=15)
    state = engine.apply_preset(project.id, PRESET_CLASSIC)
    # o preset substitui TODOS os campos, nunca preserva o que já estava.
    assert state.font == "Arial"
    assert state.max_words == 6


# ---------------------------------------------------------------------------
# 3. set_style -- campo a campo, valores válidos
# ---------------------------------------------------------------------------


def test_set_style_define_apenas_font(engine, project):
    state = engine.set_style(project.id, font="Roboto")
    assert state.font == "Roboto"
    assert state.size is None


def test_set_style_define_apenas_size(engine, project):
    state = engine.set_style(project.id, size=48.0)
    assert state.size == 48.0


def test_set_style_define_apenas_position(engine, project):
    state = engine.set_style(project.id, position={"x": 0.2, "y": 0.7})
    assert state.position == {"x": 0.2, "y": 0.7}


def test_set_style_define_apenas_alignment(engine, project):
    state = engine.set_style(project.id, alignment="LEFT")
    assert state.alignment == "LEFT"


def test_set_style_define_apenas_stroke(engine, project):
    state = engine.set_style(
        project.id, stroke={"enabled": True, "color": "#112233", "width": 3.5}
    )
    assert state.stroke == {"enabled": True, "color": "#112233", "width": 3.5}


def test_set_style_define_apenas_shadow(engine, project):
    payload = {"enabled": True, "color": "#445566", "blur": 4.0, "offset_x": 1.0, "offset_y": -1.0}
    state = engine.set_style(project.id, shadow=payload)
    assert state.shadow == payload


def test_set_style_define_apenas_background(engine, project):
    payload = {"enabled": True, "color": "#778899", "opacity": 0.4}
    state = engine.set_style(project.id, background=payload)
    assert state.background == payload


def test_set_style_define_apenas_max_words(engine, project):
    state = engine.set_style(project.id, max_words=10)
    assert state.max_words == 10


def test_set_style_define_apenas_lines(engine, project):
    state = engine.set_style(project.id, lines=3)
    assert state.lines == 3


def test_set_style_define_apenas_animation(engine, project):
    state = engine.set_style(project.id, animation="SLIDE")
    assert state.animation == "SLIDE"


def test_set_style_define_apenas_current_word_highlight(engine, project):
    payload = {"enabled": True, "color": "#00FF00"}
    state = engine.set_style(project.id, current_word_highlight=payload)
    assert state.current_word_highlight == payload


def test_set_style_multiplos_campos_persistem_juntos(engine, project):
    state = engine.set_style(project.id, font="Roboto", size=30.0, lines=3)
    assert state.font == "Roboto"
    assert state.size == 30.0
    assert state.lines == 3


def test_set_style_alterar_um_campo_nao_afeta_os_demais(engine, project):
    engine.set_style(project.id, font="Roboto", size=30.0)
    state = engine.set_style(project.id, size=40.0)
    assert state.font == "Roboto"
    assert state.size == 40.0


def test_set_style_sem_nenhum_campo_e_erro_estruturado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id)


def test_set_style_campo_desconhecido_e_erro_estruturado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, cor_de_fundo_inventada="#FFFFFF")


# ---------------------------------------------------------------------------
# 4. Validação -- range numérico
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valor", [7.9, 200.1, -10.0, 1000.0])
def test_size_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, size=valor)


@pytest.mark.parametrize("valor", [-0.01, 1.01, -5.0, 5.0])
def test_position_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, position={"x": valor, "y": 0.5})
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, position={"x": 0.5, "y": valor})


@pytest.mark.parametrize("valor", [-0.01, 20.01])
def test_stroke_width_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, stroke={"enabled": True, "color": "#000000", "width": valor})


@pytest.mark.parametrize("valor", [-0.01, 20.01])
def test_shadow_blur_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(
            project.id,
            shadow={"enabled": True, "color": "#000000", "blur": valor, "offset_x": 0.0, "offset_y": 0.0},
        )


@pytest.mark.parametrize("valor", [-20.01, 20.01])
def test_shadow_offset_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(
            project.id,
            shadow={"enabled": True, "color": "#000000", "blur": 0.0, "offset_x": valor, "offset_y": 0.0},
        )
    with pytest.raises(CampoInvalidoError):
        engine.set_style(
            project.id,
            shadow={"enabled": True, "color": "#000000", "blur": 0.0, "offset_x": 0.0, "offset_y": valor},
        )


@pytest.mark.parametrize("valor", [-0.01, 1.01])
def test_background_opacity_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, background={"enabled": True, "color": "#000000", "opacity": valor})


@pytest.mark.parametrize("valor", [0, 21, -1])
def test_max_words_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, max_words=valor)


@pytest.mark.parametrize("valor", [0, 6, -1])
def test_lines_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, lines=valor)


@pytest.mark.parametrize("valor", [float("inf"), float("-inf"), float("nan")])
def test_size_nao_finito_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, size=valor)


@pytest.mark.parametrize("valor", [float("inf"), float("-inf"), float("nan")])
def test_position_nao_finito_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, position={"x": valor, "y": 0.5})


# ---------------------------------------------------------------------------
# 5. Validação -- tipo errado, incluindo armadilha bool-como-int/float
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valor", ["40", None, [], {}])
def test_size_tipo_errado_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, size=valor)


def test_size_bool_e_rejeitado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, size=True)
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, size=False)


def test_max_words_bool_e_rejeitado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, max_words=True)


def test_lines_bool_e_rejeitado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, lines=True)


def test_max_words_float_e_rejeitado(engine, project):
    """``max_words``/``lines`` são inteiros -- um float (mesmo que
    numericamente igual a um inteiro, ex.: 5.0) é rejeitado
    estruturalmente, nunca truncado silenciosamente."""
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, max_words=5.0)


def test_stroke_enabled_bool_e_obrigatorio(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, stroke={"enabled": 1, "color": "#000000", "width": 1.0})


def test_stroke_nao_objeto_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, stroke="nao-e-um-objeto")


def test_font_tipo_errado_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, font=123)


def test_font_vazio_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, font="")
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, font="   ")


def test_font_excede_comprimento_maximo_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.set_style(project.id, font="A" * 201)


def test_font_unicode_emoji_e_aceito(engine, project):
    valor = "日本語フォント 😀 Ñoño"
    state = engine.set_style(project.id, font=valor)
    assert state.font == valor


# ---------------------------------------------------------------------------
# 6. Validação -- vocabulário fechado (alignment/animation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valor", ["MIDDLE", "left", "", None, 1, True])
def test_alignment_fora_do_vocabulario_e_erro(engine, project, valor):
    with pytest.raises(VocabularioInvalidoError):
        engine.set_style(project.id, alignment=valor)


@pytest.mark.parametrize("valor", ["BOUNCE", "fade", "", None, 1, True])
def test_animation_fora_do_vocabulario_e_erro(engine, project, valor):
    with pytest.raises(VocabularioInvalidoError):
        engine.set_style(project.id, animation=valor)


@pytest.mark.parametrize("valor", ALIGNMENTS)
def test_cada_alignment_valido_e_aceito(engine, project, valor):
    state = engine.set_style(project.id, alignment=valor)
    assert state.alignment == valor


@pytest.mark.parametrize("valor", ANIMATIONS)
def test_cada_animation_valida_e_aceita(engine, project, valor):
    state = engine.set_style(project.id, animation=valor)
    assert state.animation == valor


# ---------------------------------------------------------------------------
# 7. Validação -- formato de cor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valor",
    [
        "red",
        "#FFF",
        "#GGGGGG",
        "FFFFFF",
        "#FFFFFFF",
        "#FFFFFFFFF",
        "",
        None,
        123456,
        "#12345",
    ],
)
def test_cor_estruturalmente_invalida_e_erro(engine, project, valor):
    with pytest.raises(CorInvalidaError):
        engine.set_style(project.id, stroke={"enabled": True, "color": valor, "width": 1.0})


def test_cor_6_digitos_e_aceita(engine, project):
    state = engine.set_style(project.id, stroke={"enabled": True, "color": "#ABCDEF", "width": 1.0})
    assert state.stroke["color"] == "#ABCDEF"


def test_cor_8_digitos_com_alfa_e_aceita(engine, project):
    state = engine.set_style(project.id, background={"enabled": True, "color": "#AABBCCDD", "opacity": 0.5})
    assert state.background["color"] == "#AABBCCDD"


def test_cor_e_normalizada_para_maiusculas(engine, project):
    state = engine.set_style(project.id, stroke={"enabled": True, "color": "#abcdef", "width": 1.0})
    assert state.stroke["color"] == "#ABCDEF"


def test_cor_minusculas_e_maiusculas_produzem_mesmo_fingerprint(engine, project, manager):
    engine.set_style(project.id, stroke={"enabled": True, "color": "#abcdef", "width": 1.0})
    fp1 = manager.get_category(project.id, CAPTIONS_STYLE).fingerprint
    engine.set_style(project.id, stroke={"enabled": True, "color": "#ABCDEF", "width": 1.0})
    fp2 = manager.get_category(project.id, CAPTIONS_STYLE).fingerprint
    assert fp1 == fp2


# ---------------------------------------------------------------------------
# 8. Isolamento de fingerprint (nas duas direções)
# ---------------------------------------------------------------------------


def test_alterar_captions_style_nunca_muda_fingerprint_de_captions(database, engine, project, manager):
    manager.set_category(project.id, CAPTIONS, {"mode": "SEPARATE"})
    before = manager.get_category(project.id, CAPTIONS)
    engine.set_style(project.id, font="Roboto")
    after = manager.get_category(project.id, CAPTIONS)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_alterar_captions_style_nunca_muda_fingerprint_de_audio_settings(database, engine, project, manager):
    manager.set_category(project.id, AUDIO_SETTINGS, {"volume": 1.2})
    before = manager.get_category(project.id, AUDIO_SETTINGS)
    engine.set_style(project.id, font="Roboto")
    after = manager.get_category(project.id, AUDIO_SETTINGS)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_alterar_captions_style_nunca_muda_fingerprint_de_crop(database, engine, project, manager):
    manager.set_category(project.id, CROP, {"zoom": 2.0})
    before = manager.get_category(project.id, CROP)
    engine.set_style(project.id, font="Roboto")
    after = manager.get_category(project.id, CROP)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_alterar_captions_ou_audio_settings_ou_crop_nunca_muda_fingerprint_de_captions_style(
    database, engine, project, manager
):
    engine.set_style(project.id, font="Roboto")
    before = manager.get_category(project.id, CAPTIONS_STYLE)

    manager.set_category(project.id, CAPTIONS, {"mode": "SEPARATE"})
    manager.set_category(project.id, AUDIO_SETTINGS, {"volume": 1.2})
    manager.set_category(project.id, CROP, {"zoom": 2.0})

    after = manager.get_category(project.id, CAPTIONS_STYLE)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_categorias_captions_style_e_captions_sao_distintas(database, engine, project, manager):
    engine.set_style(project.id, font="Roboto")
    manager.set_category(project.id, CAPTIONS, {"mode": "SEPARATE"})
    style_data = manager.get_category(project.id, CAPTIONS_STYLE).data
    captions_data = manager.get_category(project.id, CAPTIONS).data
    assert "mode" not in style_data
    assert "font" not in captions_data


# ---------------------------------------------------------------------------
# 9. update_category atômico -- nunca get_category+set_category separados
# ---------------------------------------------------------------------------


def test_modulo_usa_update_category_nao_set_category_direto():
    source = Path(captions_style.__file__).read_text(encoding="utf-8")
    assert "self._manager.set_category(" not in source
    assert source.count("update_category(") >= 3  # set_style + apply_preset + bulk


# ---------------------------------------------------------------------------
# 10. Concorrência real (threading.Barrier)
# ---------------------------------------------------------------------------


def test_duas_operacoes_concorrentes_em_campos_diferentes_nao_se_perdem(app_paths, database, project):
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(field_name: str, value):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_engine = CaptionsStyleEngine(EditProjectManager(db_instance))
        barrier.wait(timeout=5)
        try:
            local_engine.set_style(project.id, **{field_name: value})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("max_words", 5)),
        threading.Thread(target=worker, args=("lines", 3)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    final = CaptionsStyleEngine(EditProjectManager(database)).get_style(project.id)
    assert final.max_words == 5, "mudanca concorrente perdida"
    assert final.lines == 3, "mudanca concorrente perdida"


def test_duas_operacoes_concorrentes_em_categorias_diferentes_nao_se_perdem(app_paths, database, project):
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker_style():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_engine = CaptionsStyleEngine(EditProjectManager(db_instance))
        barrier.wait(timeout=5)
        try:
            local_engine.set_style(project.id, font="Roboto")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def worker_manager():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_manager = EditProjectManager(db_instance)
        barrier.wait(timeout=5)
        try:
            local_manager.set_category(project.id, CROP, {"zoom": 2.0})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker_style), threading.Thread(target=worker_manager)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    reread_manager = EditProjectManager(database)
    reread_engine = CaptionsStyleEngine(reread_manager)
    assert reread_engine.get_style(project.id).font == "Roboto"
    assert reread_manager.get_category(project.id, CROP).data == {"zoom": 2.0}


# ---------------------------------------------------------------------------
# 11. Restart -- instâncias totalmente novas
# ---------------------------------------------------------------------------


def test_estado_de_estilo_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    engine_a = CaptionsStyleEngine(EditProjectManager(db_a))
    engine_a.apply_preset(project.id, PRESET_KARAOKE)
    engine_a.set_style(project.id, size=44.0)

    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    engine_b = CaptionsStyleEngine(EditProjectManager(db_b))

    final = engine_b.get_style(project.id)
    assert final.font == "Poppins"
    assert final.size == 44.0
    assert final.current_word_highlight == {"enabled": True, "color": "#FFEE00"}


# ---------------------------------------------------------------------------
# 12. Idempotência
# ---------------------------------------------------------------------------


def test_repetir_o_mesmo_set_style_sequencialmente_e_deterministico(engine, project):
    r1 = engine.set_style(project.id, size=30.0)
    r2 = engine.set_style(project.id, size=30.0)
    assert r1 == r2 == engine.get_style(project.id)


def test_repetir_o_mesmo_set_style_nao_acumula_revisao(engine, project, manager):
    engine.set_style(project.id, size=30.0)
    before = manager.get_category(project.id, CAPTIONS_STYLE)
    engine.set_style(project.id, size=30.0)
    after = manager.get_category(project.id, CAPTIONS_STYLE)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_repetir_o_mesmo_apply_preset_nao_acumula_revisao(engine, project, manager):
    engine.apply_preset(project.id, PRESET_CLASSIC)
    before = manager.get_category(project.id, CAPTIONS_STYLE)
    engine.apply_preset(project.id, PRESET_CLASSIC)
    after = manager.get_category(project.id, CAPTIONS_STYLE)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_repetir_bulk_com_mesmo_valor_e_no_op_na_segunda_chamada(engine, project, project2):
    r1 = engine.bulk_set_style([project.id, project2.id], size=30.0)
    assert set(r1.updated) == {project.id, project2.id}

    r2 = engine.bulk_set_style([project.id, project2.id], size=30.0)
    assert r2.updated == ()
    assert set(r2.unchanged) == {project.id, project2.id}


def test_repetir_bulk_apply_preset_com_mesmo_preset_e_no_op_na_segunda_chamada(engine, project, project2):
    r1 = engine.bulk_apply_preset([project.id, project2.id], PRESET_CLASSIC)
    assert set(r1.updated) == {project.id, project2.id}

    r2 = engine.bulk_apply_preset([project.id, project2.id], PRESET_CLASSIC)
    assert r2.updated == ()
    assert set(r2.unchanged) == {project.id, project2.id}


# ---------------------------------------------------------------------------
# 13. Operação em lote -- matriz completa
# ---------------------------------------------------------------------------


def test_bulk_set_style_sucesso_total(engine, project, project2):
    result = engine.bulk_set_style([project.id, project2.id], max_words=4, lines=1)
    assert isinstance(result, BulkCaptionsStyleResult)
    assert set(result.updated) == {project.id, project2.id}
    assert result.unchanged == ()
    assert result.failed == ()
    assert engine.get_style(project.id).max_words == 4
    assert engine.get_style(project2.id).max_words == 4


def test_bulk_apply_preset_sucesso_total(engine, project, project2):
    result = engine.bulk_apply_preset([project.id, project2.id], PRESET_BOLD_SOCIAL)
    assert set(result.updated) == {project.id, project2.id}
    assert result.failed == ()
    assert engine.get_style(project.id).font == "Montserrat"
    assert engine.get_style(project2.id).font == "Montserrat"


def test_bulk_set_style_sucesso_parcial_um_projeto_inexistente(engine, project):
    fake_id = "11111111-1111-1111-1111-111111111111"
    result = engine.bulk_set_style([project.id, fake_id], max_words=4)
    assert result.updated == (project.id,)
    assert len(result.failed) == 1
    assert result.failed[0][0] == fake_id


def test_bulk_apply_preset_sucesso_parcial_um_projeto_inexistente(engine, project):
    fake_id = "22222222-2222-2222-2222-222222222222"
    result = engine.bulk_apply_preset([project.id, fake_id], PRESET_CLASSIC)
    assert result.updated == (project.id,)
    assert len(result.failed) == 1
    assert result.failed[0][0] == fake_id


def test_bulk_set_style_lote_de_project_ids_vazio_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_style([], max_words=4)


def test_bulk_apply_preset_lote_de_project_ids_vazio_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_apply_preset([], PRESET_CLASSIC)


def test_bulk_set_style_sem_nenhum_campo_e_erro_estruturado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_style([project.id])


def test_bulk_set_style_com_project_ids_duplicados_deduplicados(engine, project):
    result = engine.bulk_set_style([project.id, project.id, project.id], max_words=4)
    assert result.requested == (project.id,)


def test_bulk_set_style_campo_invalido_nao_escreve_em_nenhum_projeto_antes_de_validar(
    engine, project, project2
):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_style([project.id, project2.id], size=99999.0)
    assert engine.get_style(project.id).size is None
    assert engine.get_style(project2.id).size is None


def test_bulk_apply_preset_com_nome_invalido_nao_escreve_em_nenhum_projeto(engine, project, project2):
    with pytest.raises(PresetInvalidoError):
        engine.bulk_apply_preset([project.id, project2.id], "NAO_EXISTE")
    assert engine.get_style(project.id) == CaptionsStyleState()
    assert engine.get_style(project2.id) == CaptionsStyleState()


def test_bulk_set_style_campo_desconhecido_e_erro_estruturado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_style([project.id], campo_fantasma="x")


def test_bulk_set_style_nao_afeta_categorias_de_outros_modulos(engine, project, manager):
    manager.set_category(project.id, AUDIO_SETTINGS, {"volume": 1.5})
    engine.bulk_set_style([project.id], max_words=4)
    assert manager.get_category(project.id, AUDIO_SETTINGS).data == {"volume": 1.5}


# ---------------------------------------------------------------------------
# 14. Estrutural / AST -- escopo, imports proibidos, criação de entidades
# ---------------------------------------------------------------------------


def test_modulo_nao_importa_modulos_protegidos():
    tree = _parse_module()
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
        "visual_editor",
        "audio_engine",
        "captions_engine",
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
            assert proibido not in names, f"captions_style.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_modulo_nao_chama_subprocess_ffmpeg_ffprobe():
    tree = _parse_module()
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


def test_modulo_nunca_cria_job_artifact_video_publication_schedule():
    source = Path(captions_style.__file__).read_text(encoding="utf-8")
    for proibido in ("Job(", "Artifact(", "Video(", "Publication(", "Schedule("):
        assert proibido not in source


def test_modulo_nunca_le_source_asset_local_path():
    """Diferente do Prompt 31 Parte B -- este módulo é só decisão, sem
    exceção para tocar o arquivo original. AST, não grep textual ingênuo
    -- a própria docstring do módulo cita ``local_path`` para explicar
    por que NÃO é usado (mesmo problema já enfrentado em
    ``audio_engine.py``/``visual_editor.py`` com "ffmpeg"/"subprocess")."""
    tree = _parse_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "local_path"
        if isinstance(node, ast.Name):
            assert node.id != "local_path"


def test_modulo_nao_abre_transacao_propria_nem_toca_storage_diretamente():
    tree = _parse_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "transaction"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("get", "save", "insert", "delete"):
                assert not (
                    isinstance(node.func.value, ast.Name) and node.func.value.id == "database"
                ), "captions_style.py não pode chamar métodos de LocalDatabase diretamente"


def test_nenhuma_migration_nova_criada_por_este_prompt():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9


# ---------------------------------------------------------------------------
# 15. Leitura defensiva -- sub-objeto corrompido persistido diretamente
# ---------------------------------------------------------------------------


def test_leitura_rejeita_stroke_corrompido(database, project, manager):
    manager.set_category(project.id, CAPTIONS_STYLE, {"stroke": "nao-e-um-objeto"})
    engine = CaptionsStyleEngine(manager)
    with pytest.raises(CampoInvalidoError):
        engine.get_style(project.id)


def test_leitura_rejeita_background_corrompido(database, project, manager):
    manager.set_category(project.id, CAPTIONS_STYLE, {"background": 42})
    engine = CaptionsStyleEngine(manager)
    with pytest.raises(CampoInvalidoError):
        engine.get_style(project.id)


# ---------------------------------------------------------------------------
# 16. Regressão -- project_id malformado (não-UUID) nunca escapa como ValueError cru
# ---------------------------------------------------------------------------


def test_get_style_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.get_style("nao-e-um-uuid")


def test_set_style_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.set_style("nao-e-um-uuid", size=30.0)


def test_apply_preset_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.apply_preset("nao-e-um-uuid", PRESET_CLASSIC)


def test_bulk_set_style_com_project_id_nao_uuid_e_classificado_como_falha_estruturada(engine, project):
    result = engine.bulk_set_style([project.id, "nao-e-um-uuid"], size=30.0)
    assert result.updated == (project.id,)
    assert result.failed == (("nao-e-um-uuid", "PROJETO_ID_INVALIDO"),)


def test_bulk_apply_preset_com_project_id_nao_uuid_e_classificado_como_falha_estruturada(engine, project):
    result = engine.bulk_apply_preset([project.id, "nao-e-um-uuid"], PRESET_CLASSIC)
    assert result.updated == (project.id,)
    assert result.failed == (("nao-e-um-uuid", "PROJETO_ID_INVALIDO"),)


# ---------------------------------------------------------------------------
# 17. Erros são subclasses de CaptionsStyleError
# ---------------------------------------------------------------------------


def test_todos_os_erros_sao_subclasses_de_captions_style_error():
    assert issubclass(CampoInvalidoError, CaptionsStyleError)
    assert issubclass(VocabularioInvalidoError, CaptionsStyleError)
    assert issubclass(CorInvalidaError, CaptionsStyleError)
    assert issubclass(PresetInvalidoError, CaptionsStyleError)
