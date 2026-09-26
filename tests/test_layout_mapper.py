# -*- coding: utf-8 -*-
"""PROMPT 39 -- Layout Mapper (operações atômicas de zona, sem frontend):
testes.

Arquivo de testes DEDICADO (separado de ``test_template_engine.py``) para
a funcionalidade nova deste Prompt -- as operações vivem como métodos
novos de ``TemplateEngine`` (decisão documentada na seção 8.2 da
docstring de ``_sistema/template_engine.py``), mas os testes ficam
isolados por feature para não inflar ainda mais o arquivo de testes já
existente (62 testes) nem misturar as duas preocupações.

Cobre: as 5 operações de zona (``add_zone``/``move_zone``/
``resize_zone``/``remove_zone``/``reorder_zone``) isoladamente,
incluindo todos os caminhos de rejeição (geometria inválida,
cardinalidade, zona/`template_id` inexistente, zona BACKGROUND
protegida); endereçamento correto entre múltiplas zonas do MESMO
``zone_type`` (2 zonas LOGO, mover uma nunca afeta a outra);
retrocompatibilidade de ``validate_layout``/``zone_id`` (geração
automática, preservação em revalidação, rejeição de duplicata/tipo
inválido); a correção de atomicidade de ``update_template`` (TOCTOU
pré-existente, ver seção 8.3 da docstring do módulo) provada com duas
instâncias concorrentes via ``threading.Barrier`` (nunca
``time.sleep``); restart com novas instâncias; AST estrutural (nenhum
import de módulo irmão/Geração 1 -- delegado ao módulo que já cobre
isso, este arquivo só confirma que nenhum import novo foi introduzido
no próprio ``template_engine.py``, reaproveitando o teste AST existente
em ``test_template_engine.py`` sem duplicá-lo aqui).
"""
from __future__ import annotations

import threading

import pytest

from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import (
    CampoInvalidoError,
    GeometriaInvalidaError,
    LayoutInvalidoError,
    TemplateEngine,
    TemplateNaoEncontradoError,
    ZONE_TYPE_AI_TEXT,
    ZONE_TYPE_BACKGROUND,
    ZONE_TYPE_CAPTION,
    ZONE_TYPE_LOGO,
    ZONE_TYPE_VIDEO,
    ZonaNaoEncontradaError,
    ZonaProtegidaError,
    validate_layout,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_template_engine.py / test_metadata_manager.py)
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


def _valid_layout() -> dict:
    return {
        "zones": [
            {"zone_type": ZONE_TYPE_BACKGROUND, "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": ZONE_TYPE_VIDEO, "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
        ]
    }


def _create(engine):
    return engine.create_template(name="Layout Mapper", layout=_valid_layout())


def _zone_by_id(template, zone_id: str) -> dict:
    for z in template.layout["zones"]:
        if z["zone_id"] == zone_id:
            return z
    raise AssertionError(f"zone_id {zone_id!r} não encontrado em {template.layout['zones']!r}")


def _zone_by_type(template, zone_type: str) -> dict:
    matches = [z for z in template.layout["zones"] if z["zone_type"] == zone_type]
    assert len(matches) == 1, f"esperado exatamente 1 zona {zone_type!r}, encontradas: {matches!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# validate_layout / zone_id -- retrocompatibilidade e novas garantias
# ---------------------------------------------------------------------------


def test_validate_layout_gera_zone_id_quando_ausente():
    layout = validate_layout(_valid_layout())
    for zone in layout["zones"]:
        assert isinstance(zone["zone_id"], str) and zone["zone_id"]
    ids = [z["zone_id"] for z in layout["zones"]]
    assert len(ids) == len(set(ids))


def test_validate_layout_preserva_zone_id_ja_presente():
    layout = validate_layout(_valid_layout())
    zone_ids_originais = [z["zone_id"] for z in layout["zones"]]
    revalidado = validate_layout(layout)
    assert [z["zone_id"] for z in revalidado["zones"]] == zone_ids_originais


def test_validate_layout_rejeita_zone_id_duplicado():
    payload = _valid_layout()
    payload["zones"][0]["zone_id"] = "11111111-1111-1111-1111-111111111111"
    payload["zones"][1]["zone_id"] = "11111111-1111-1111-1111-111111111111"
    with pytest.raises(LayoutInvalidoError):
        validate_layout(payload)


def test_validate_layout_rejeita_zone_id_nao_uuid():
    payload = _valid_layout()
    payload["zones"][1]["zone_id"] = "nao-e-um-uuid"
    with pytest.raises(CampoInvalidoError):
        validate_layout(payload)


def test_create_template_caller_pre_existente_continua_funcionando_sem_zone_id(engine):
    """Prova de retrocompatibilidade: nenhum caller pré-existente (Prompt
    36/38) jamais passava zone_id -- confirma que continuam funcionando
    idênticos, só ganhando um zone_id novo de forma transparente."""
    tpl = engine.create_template(name="Sem zone_id explícito", layout=_valid_layout())
    assert len(tpl.layout["zones"]) == 2
    assert all("zone_id" in z for z in tpl.layout["zones"])


# ---------------------------------------------------------------------------
# add_zone
# ---------------------------------------------------------------------------


def test_add_zone_sucesso_no_fim_por_padrao(engine):
    tpl = _create(engine)
    atualizado = engine.add_zone(
        tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.2, "height": 0.2}
    )
    tipos = [z["zone_type"] for z in atualizado.layout["zones"]]
    assert tipos == [ZONE_TYPE_BACKGROUND, ZONE_TYPE_VIDEO, ZONE_TYPE_LOGO]
    nova = atualizado.layout["zones"][-1]
    assert isinstance(nova["zone_id"], str) and nova["zone_id"]


def test_add_zone_ignora_zone_id_do_chamador_e_gera_um_novo(engine):
    tpl = _create(engine)
    atualizado = engine.add_zone(
        tpl.id,
        {
            "zone_type": ZONE_TYPE_LOGO,
            "x": 0.0,
            "y": 0.0,
            "width": 0.2,
            "height": 0.2,
            "zone_id": "11111111-1111-1111-1111-111111111111",
        },
    )
    nova = atualizado.layout["zones"][-1]
    assert nova["zone_id"] != "11111111-1111-1111-1111-111111111111"


def test_add_zone_com_index_explicito_insere_na_posicao(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    atualizado = engine.add_zone(
        tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1}, index=1
    )
    tipos = [z["zone_type"] for z in atualizado.layout["zones"]]
    assert tipos == [ZONE_TYPE_BACKGROUND, ZONE_TYPE_CAPTION, ZONE_TYPE_VIDEO, ZONE_TYPE_LOGO]


def test_add_zone_index_0_e_ajustado_para_nunca_sobrepor_background(engine):
    tpl = _create(engine)
    atualizado = engine.add_zone(
        tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1}, index=0
    )
    assert atualizado.layout["zones"][0]["zone_type"] == ZONE_TYPE_BACKGROUND
    assert atualizado.layout["zones"][1]["zone_type"] == ZONE_TYPE_LOGO


def test_add_zone_index_alem_do_fim_e_clampado(engine):
    tpl = _create(engine)
    atualizado = engine.add_zone(
        tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1}, index=999
    )
    assert atualizado.layout["zones"][-1]["zone_type"] == ZONE_TYPE_LOGO


def test_add_zone_rejeita_segunda_zona_background(engine):
    tpl = _create(engine)
    with pytest.raises(ZonaProtegidaError):
        engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_BACKGROUND})


def test_add_zone_rejeita_segunda_zona_video_via_cardinalidade(engine):
    tpl = _create(engine)
    with pytest.raises(LayoutInvalidoError):
        engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_VIDEO, "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5})


def test_add_zone_rejeita_geometria_invalida(engine):
    tpl = _create(engine)
    with pytest.raises(GeometriaInvalidaError):
        engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.9, "y": 0.0, "width": 0.5, "height": 0.1})


def test_add_zone_rejeita_template_inexistente(engine):
    with pytest.raises(TemplateNaoEncontradoError):
        engine.add_zone("00000000-0000-0000-0000-000000000000", {"zone_type": ZONE_TYPE_LOGO, "x": 0, "y": 0, "width": 0.1, "height": 0.1})


def test_add_zone_permite_duas_zonas_logo(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    atualizado = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.8, "y": 0.8, "width": 0.1, "height": 0.1})
    logos = [z for z in atualizado.layout["zones"] if z["zone_type"] == ZONE_TYPE_LOGO]
    assert len(logos) == 2
    assert logos[0]["zone_id"] != logos[1]["zone_id"]


# ---------------------------------------------------------------------------
# move_zone
# ---------------------------------------------------------------------------


def test_move_zone_sucesso_preserva_demais_campos(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(
        tpl.id,
        {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.2, "height": 0.3, "style": {"opacity": 0.5}},
    )
    logo_id = _zone_by_type(tpl, ZONE_TYPE_LOGO)["zone_id"]
    atualizado = engine.move_zone(tpl.id, logo_id, x=0.4, y=0.5)
    logo = _zone_by_id(atualizado, logo_id)
    assert (logo["x"], logo["y"]) == (0.4, 0.5)
    assert (logo["width"], logo["height"]) == (0.2, 0.3)
    assert logo["style"] == {"opacity": 0.5}


def test_move_zone_endereca_a_zona_correta_entre_duas_do_mesmo_tipo(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.8, "y": 0.8, "width": 0.1, "height": 0.1})
    logos = [z for z in tpl.layout["zones"] if z["zone_type"] == ZONE_TYPE_LOGO]
    primeiro_id, segundo_id = logos[0]["zone_id"], logos[1]["zone_id"]

    atualizado = engine.move_zone(tpl.id, primeiro_id, x=0.45, y=0.45)

    movido = _zone_by_id(atualizado, primeiro_id)
    intocado = _zone_by_id(atualizado, segundo_id)
    assert (movido["x"], movido["y"]) == (0.45, 0.45)
    assert (intocado["x"], intocado["y"]) == (0.8, 0.8)


def test_move_zone_rejeita_background(engine):
    tpl = _create(engine)
    bg_id = _zone_by_type(tpl, ZONE_TYPE_BACKGROUND)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.move_zone(tpl.id, bg_id, x=0.1, y=0.1)


def test_move_zone_rejeita_zone_id_inexistente(engine):
    tpl = _create(engine)
    with pytest.raises(ZonaNaoEncontradaError):
        engine.move_zone(tpl.id, "zone-inexistente", x=0.0, y=0.0)


def test_move_zone_rejeita_template_inexistente(engine):
    with pytest.raises(TemplateNaoEncontradoError):
        engine.move_zone("00000000-0000-0000-0000-000000000000", "qualquer", x=0.0, y=0.0)


def test_move_zone_rejeita_overflow_de_borda(engine):
    tpl = _create(engine)
    video_id = _zone_by_type(tpl, ZONE_TYPE_VIDEO)["zone_id"]
    with pytest.raises(GeometriaInvalidaError):
        engine.move_zone(tpl.id, video_id, x=0.95, y=0.95)


# ---------------------------------------------------------------------------
# resize_zone
# ---------------------------------------------------------------------------


def test_resize_zone_sucesso_preserva_demais_campos(engine):
    tpl = _create(engine)
    video_id = _zone_by_type(tpl, ZONE_TYPE_VIDEO)["zone_id"]
    atualizado = engine.resize_zone(tpl.id, video_id, width=0.5, height=0.4)
    video = _zone_by_id(atualizado, video_id)
    assert (video["width"], video["height"]) == (0.5, 0.4)
    assert (video["x"], video["y"]) == (0.1, 0.1)


def test_resize_zone_rejeita_background(engine):
    tpl = _create(engine)
    bg_id = _zone_by_type(tpl, ZONE_TYPE_BACKGROUND)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.resize_zone(tpl.id, bg_id, width=0.5, height=0.5)


def test_resize_zone_rejeita_largura_zero(engine):
    tpl = _create(engine)
    video_id = _zone_by_type(tpl, ZONE_TYPE_VIDEO)["zone_id"]
    with pytest.raises(GeometriaInvalidaError):
        engine.resize_zone(tpl.id, video_id, width=0.0, height=0.5)


def test_resize_zone_rejeita_overflow_de_borda(engine):
    tpl = _create(engine)
    video_id = _zone_by_type(tpl, ZONE_TYPE_VIDEO)["zone_id"]
    with pytest.raises(GeometriaInvalidaError):
        engine.resize_zone(tpl.id, video_id, width=0.95, height=0.95)


def test_resize_zone_rejeita_zone_id_inexistente(engine):
    tpl = _create(engine)
    with pytest.raises(ZonaNaoEncontradaError):
        engine.resize_zone(tpl.id, "zone-inexistente", width=0.5, height=0.5)


# ---------------------------------------------------------------------------
# remove_zone
# ---------------------------------------------------------------------------


def test_remove_zone_sucesso(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    logo_id = _zone_by_type(tpl, ZONE_TYPE_LOGO)["zone_id"]
    atualizado = engine.remove_zone(tpl.id, logo_id)
    tipos = [z["zone_type"] for z in atualizado.layout["zones"]]
    assert ZONE_TYPE_LOGO not in tipos
    assert len(atualizado.layout["zones"]) == 2


def test_remove_zone_endereca_a_zona_correta_entre_duas_do_mesmo_tipo(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.8, "y": 0.8, "width": 0.1, "height": 0.1})
    logos = [z for z in tpl.layout["zones"] if z["zone_type"] == ZONE_TYPE_LOGO]
    primeiro_id, segundo_id = logos[0]["zone_id"], logos[1]["zone_id"]

    atualizado = engine.remove_zone(tpl.id, primeiro_id)

    remanescentes = [z["zone_id"] for z in atualizado.layout["zones"] if z["zone_type"] == ZONE_TYPE_LOGO]
    assert remanescentes == [segundo_id]


def test_remove_zone_rejeita_background(engine):
    tpl = _create(engine)
    bg_id = _zone_by_type(tpl, ZONE_TYPE_BACKGROUND)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.remove_zone(tpl.id, bg_id)


def test_remove_zone_rejeita_video(engine):
    tpl = _create(engine)
    video_id = _zone_by_type(tpl, ZONE_TYPE_VIDEO)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.remove_zone(tpl.id, video_id)


def test_remove_zone_rejeita_zone_id_inexistente(engine):
    tpl = _create(engine)
    with pytest.raises(ZonaNaoEncontradaError):
        engine.remove_zone(tpl.id, "zone-inexistente")


def test_remove_zone_rejeita_template_inexistente(engine):
    with pytest.raises(TemplateNaoEncontradoError):
        engine.remove_zone("00000000-0000-0000-0000-000000000000", "qualquer")


# ---------------------------------------------------------------------------
# reorder_zone
# ---------------------------------------------------------------------------


def test_reorder_zone_sucesso_move_para_nova_posicao(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    logo_id = _zone_by_type(tpl, ZONE_TYPE_LOGO)["zone_id"]

    # ordem atual: BACKGROUND, VIDEO, LOGO, CAPTION -- mover LOGO para o fim (index 3)
    atualizado = engine.reorder_zone(tpl.id, logo_id, new_index=3)
    tipos = [z["zone_type"] for z in atualizado.layout["zones"]]
    assert tipos == [ZONE_TYPE_BACKGROUND, ZONE_TYPE_VIDEO, ZONE_TYPE_CAPTION, ZONE_TYPE_LOGO]


def test_reorder_zone_index_alem_do_fim_e_clampado(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    video_id = _zone_by_type(tpl, ZONE_TYPE_VIDEO)["zone_id"]
    atualizado = engine.reorder_zone(tpl.id, video_id, new_index=999)
    assert atualizado.layout["zones"][-1]["zone_type"] == ZONE_TYPE_VIDEO


def test_reorder_zone_rejeita_background(engine):
    tpl = _create(engine)
    bg_id = _zone_by_type(tpl, ZONE_TYPE_BACKGROUND)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.reorder_zone(tpl.id, bg_id, new_index=1)


def test_reorder_zone_rejeita_index_0_para_zona_diferente_de_background(engine):
    tpl = _create(engine)
    video_id = _zone_by_type(tpl, ZONE_TYPE_VIDEO)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.reorder_zone(tpl.id, video_id, new_index=0)


def test_reorder_zone_rejeita_zone_id_inexistente(engine):
    tpl = _create(engine)
    with pytest.raises(ZonaNaoEncontradaError):
        engine.reorder_zone(tpl.id, "zone-inexistente", new_index=1)


# ---------------------------------------------------------------------------
# zone_id estável através de update_template (round-trip)
# ---------------------------------------------------------------------------


def test_zone_id_estavel_apos_update_template_com_layout_relido(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    ids_antes = [z["zone_id"] for z in tpl.layout["zones"]]

    relido = engine.get_template(tpl.id)
    atualizado = engine.update_template(tpl.id, layout=relido.layout)

    ids_depois = [z["zone_id"] for z in atualizado.layout["zones"]]
    assert ids_depois == ids_antes


# ---------------------------------------------------------------------------
# Concorrência real (item 2/6 do GATE ADVERSARIAL -- Barrier, nunca sleep)
# ---------------------------------------------------------------------------


def test_update_template_concorrente_duas_instancias_nao_perde_mudanca(app_paths):
    """Prova direta da correção de TOCTOU descrita na seção 8.3 da
    docstring do módulo: duas instâncias concorrentes de TemplateEngine,
    cada uma editando um campo DIFERENTE do MESMO template via
    update_template, sob a MESMA BEGIN IMMEDIATE -- nenhuma das duas
    mudanças pode ser silenciosamente perdida (last-write-wins ficando
    só com uma delas)."""
    db_path = app_paths.database / "painel.db"
    db_a = LocalDatabase(db_path)
    db_a.initialize()
    storage_a = StorageManager(app_paths, database_path=db_path)
    engine_a = TemplateEngine(db_a, storage_manager=storage_a, app_paths=app_paths)

    db_b = LocalDatabase(db_path)
    storage_b = StorageManager(app_paths, database_path=db_path)
    engine_b = TemplateEngine(db_b, storage_manager=storage_b, app_paths=app_paths)

    tpl = engine_a.create_template(name="Original", layout=_valid_layout(), enabled=True)

    resultados: list = [None, None]
    barreira = threading.Barrier(2)

    def _run_name(idx):
        barreira.wait(timeout=10)
        resultados[idx] = engine_a.update_template(tpl.id, name="Nome Alterado Por A")

    def _run_enabled(idx):
        barreira.wait(timeout=10)
        resultados[idx] = engine_b.update_template(tpl.id, enabled=False)

    t1 = threading.Thread(target=_run_name, args=(0,))
    t2 = threading.Thread(target=_run_enabled, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert resultados[0] is not None and resultados[1] is not None

    verificador = TemplateEngine(LocalDatabase(db_path), storage_manager=storage_a, app_paths=app_paths)
    final = verificador.get_template(tpl.id)
    # nenhuma das duas mudanças concorrentes pode ter sido perdida --
    # sob a transação atômica corrigida, a segunda escrita a de fato
    # commitar sempre relê o estado já persistido pela primeira dentro
    # da MESMA transação, então ambos os campos convergem.
    assert final.name == "Nome Alterado Por A"
    assert final.enabled is False


def test_add_zone_concorrente_duas_instancias_nao_perde_nenhuma_zona(app_paths):
    """Mesma prova que a de cima, mas contra as operações de zona novas
    especificamente (mais realista do cenário citado pelo Prompt: uma
    futura UI de arrastar-e-soltar disparando chamadas pequenas e
    frequentes contra o mesmo template)."""
    db_path = app_paths.database / "painel.db"
    db_a = LocalDatabase(db_path)
    db_a.initialize()
    storage_a = StorageManager(app_paths, database_path=db_path)
    engine_a = TemplateEngine(db_a, storage_manager=storage_a, app_paths=app_paths)

    db_b = LocalDatabase(db_path)
    storage_b = StorageManager(app_paths, database_path=db_path)
    engine_b = TemplateEngine(db_b, storage_manager=storage_b, app_paths=app_paths)

    tpl = engine_a.create_template(name="Concorrência de Zonas", layout=_valid_layout())

    resultados: list = [None, None]
    barreira = threading.Barrier(2)

    def _run(idx, engine_instance, zone_type):
        barreira.wait(timeout=10)
        resultados[idx] = engine_instance.add_zone(
            tpl.id, {"zone_type": zone_type, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1}
        )

    t1 = threading.Thread(target=_run, args=(0, engine_a, ZONE_TYPE_LOGO))
    t2 = threading.Thread(target=_run, args=(1, engine_b, ZONE_TYPE_CAPTION))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert resultados[0] is not None and resultados[1] is not None

    verificador = TemplateEngine(LocalDatabase(db_path), storage_manager=storage_a, app_paths=app_paths)
    final = verificador.get_template(tpl.id)
    tipos = [z["zone_type"] for z in final.layout["zones"]]
    assert tipos.count(ZONE_TYPE_BACKGROUND) == 1
    assert tipos.count(ZONE_TYPE_VIDEO) == 1
    assert tipos.count(ZONE_TYPE_LOGO) == 1
    assert tipos.count(ZONE_TYPE_CAPTION) == 1
    assert len(final.layout["zones"]) == 4


# ---------------------------------------------------------------------------
# Restart (item 4 do GATE ADVERSARIAL -- novas instâncias, nunca reutilizadas)
# ---------------------------------------------------------------------------


def test_operacoes_de_zona_sobrevivem_a_restart_com_novas_instancias(app_paths):
    db_path = app_paths.database / "painel.db"
    db1 = LocalDatabase(db_path)
    db1.initialize()
    storage1 = StorageManager(app_paths, database_path=db_path)
    engine1 = TemplateEngine(db1, storage_manager=storage1, app_paths=app_paths)

    tpl = engine1.create_template(name="Restart", layout=_valid_layout())
    tpl = engine1.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    logo_id = _zone_by_type(tpl, ZONE_TYPE_LOGO)["zone_id"]

    # "restart": instâncias novas de LocalDatabase/StorageManager/TemplateEngine,
    # nunca os objetos em memória acima.
    db2 = LocalDatabase(db_path)
    storage2 = StorageManager(app_paths, database_path=db_path)
    engine2 = TemplateEngine(db2, storage_manager=storage2, app_paths=app_paths)

    movido = engine2.move_zone(tpl.id, logo_id, x=0.5, y=0.5)
    logo = _zone_by_id(movido, logo_id)
    assert (logo["x"], logo["y"]) == (0.5, 0.5)

    db3 = LocalDatabase(db_path)
    storage3 = StorageManager(app_paths, database_path=db_path)
    engine3 = TemplateEngine(db3, storage_manager=storage3, app_paths=app_paths)
    relido = engine3.get_template(tpl.id)
    logo_relido = _zone_by_id(relido, logo_id)
    assert (logo_relido["x"], logo_relido["y"]) == (0.5, 0.5)


# ---------------------------------------------------------------------------
# Regras pré-existentes continuam valendo (não regrediram com as mudanças)
# ---------------------------------------------------------------------------


def test_zones_ai_text_e_caption_continuam_cardinalidade_0_a_n(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.2, "y": 0.0, "width": 0.1, "height": 0.1})
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.4, "y": 0.0, "width": 0.1, "height": 0.1})
    tipos = [z["zone_type"] for z in tpl.layout["zones"]]
    assert tipos.count(ZONE_TYPE_AI_TEXT) == 2
    assert tipos.count(ZONE_TYPE_CAPTION) == 1


def test_background_permanece_travada_ao_frame_inteiro_apos_operacoes(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    bg = _zone_by_type(tpl, ZONE_TYPE_BACKGROUND)
    assert (bg["x"], bg["y"], bg["width"], bg["height"]) == (0.0, 0.0, 1.0, 1.0)
