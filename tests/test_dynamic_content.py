# -*- coding: utf-8 -*-
"""PROMPT 40 -- DynamicContent (texto dinâmico em zona CAPTION/AI_TEXT,
SEM geração de IA real): testes.

Arquivo de testes DEDICADO (mesmo padrão de ``test_layout_mapper.py`` no
Prompt 39) -- a funcionalidade vive como extensão de
``_sistema/template_engine.py`` (novo sub-schema ``content`` + nova
operação ``TemplateEngine.set_zone_content`` + função pura
``apply_auto_fit``), mas os testes ficam isolados por feature.

Cobre: validação de cada ``content_type`` isoladamente (campos
obrigatórios/proibidos, vocabulário, faixas); ``font_size_min >
font_size`` rejeitado; ``content`` rejeitado em zona BACKGROUND/VIDEO/
LOGO; ``apply_auto_fit`` -- texto que já cabe não muda, texto gigante é
reduzido/truncado deterministicamente e NUNCA excede
``max_lines``/``max_chars``, idempotência (ponto fixo) e determinismo;
``set_zone_content`` isolado (sucesso, zona inexistente, zona de tipo
errado, limpar content); ``add_zone`` aceitando ``content`` já na
criação; retrocompatibilidade (zonas sem content continuam válidas).
"""
from __future__ import annotations

import pytest

from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import (
    CONTENT_FONT_SIZE_RANGE,
    CampoInvalidoError,
    GeometriaInvalidaError,
    MAX_LINES_RANGE,
    TemplateEngine,
    TemplateNaoEncontradoError,
    VocabularioInvalidoError,
    ZONE_TYPE_AI_TEXT,
    ZONE_TYPE_BACKGROUND,
    ZONE_TYPE_CAPTION,
    ZONE_TYPE_LOGO,
    ZONE_TYPE_VIDEO,
    ZonaNaoEncontradaError,
    ZonaProtegidaError,
    apply_auto_fit,
    validate_layout,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_layout_mapper.py)
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
            {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
        ]
    }


def _create(engine):
    return engine.create_template(name="Dynamic Content", layout=_valid_layout())


def _zone_by_type(template, zone_type: str) -> dict:
    matches = [z for z in template.layout["zones"] if z["zone_type"] == zone_type]
    assert len(matches) == 1, f"esperado exatamente 1 zona {zone_type!r}, encontradas: {matches!r}"
    return matches[0]


def _zone_by_id(template, zone_id: str) -> dict:
    for z in template.layout["zones"]:
        if z["zone_id"] == zone_id:
            return z
    raise AssertionError(f"zone_id {zone_id!r} não encontrado")


def _fixed_text_content(**overrides) -> dict:
    base = {
        "content_type": "FIXED_TEXT",
        "text": "Texto fixo de teste",
        "max_lines": 2,
        "max_chars": 50,
        "font_size": 32.0,
        "font_size_min": 16.0,
    }
    base.update(overrides)
    return base


def _custom_ai_content(**overrides) -> dict:
    base = {
        "content_type": "CUSTOM_AI",
        "instruction": "gere um CTA engraçado sobre o vídeo",
        "max_lines": 1,
        "max_chars": 80,
        "font_size": 28.0,
        "font_size_min": 14.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Validação de cada content_type isoladamente
# ---------------------------------------------------------------------------


def test_fixed_text_valido(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    atualizado = engine.set_zone_content(tpl.id, caption_id, _fixed_text_content())
    content = _zone_by_id(atualizado, caption_id)["content"]
    assert content["content_type"] == "FIXED_TEXT"
    assert content["text"] == "Texto fixo de teste"
    assert content["alignment"] == "CENTER"  # default


def test_fixed_text_rejeita_ausencia_de_text(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    content = _fixed_text_content()
    del content["text"]
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, content)


def test_fixed_text_rejeita_instruction_presente(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    content = _fixed_text_content()
    content["instruction"] = "não deveria existir aqui"
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, content)


def test_fixed_text_rejeita_texto_vazio(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(text="   "))


def test_custom_ai_valido(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.1})
    ai_id = _zone_by_type(tpl, ZONE_TYPE_AI_TEXT)["zone_id"]
    atualizado = engine.set_zone_content(tpl.id, ai_id, _custom_ai_content())
    content = _zone_by_id(atualizado, ai_id)["content"]
    assert content["content_type"] == "CUSTOM_AI"
    assert content["instruction"] == "gere um CTA engraçado sobre o vídeo"


def test_custom_ai_rejeita_ausencia_de_instruction(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.1})
    ai_id = _zone_by_type(tpl, ZONE_TYPE_AI_TEXT)["zone_id"]
    content = _custom_ai_content()
    del content["instruction"]
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, ai_id, content)


def test_custom_ai_rejeita_text_presente(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.1})
    ai_id = _zone_by_type(tpl, ZONE_TYPE_AI_TEXT)["zone_id"]
    content = _custom_ai_content()
    content["text"] = "não deveria existir aqui"
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, ai_id, content)


@pytest.mark.parametrize(
    "content_type", ["AI_HOOK", "AI_TITLE", "AI_SUMMARY", "AI_QUESTION", "AI_CTA"]
)
def test_ai_types_sem_texto_validos_sem_nenhum_campo_de_texto(engine, content_type):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.1})
    ai_id = _zone_by_type(tpl, ZONE_TYPE_AI_TEXT)["zone_id"]
    content = {
        "content_type": content_type,
        "max_lines": 2,
        "max_chars": 100,
        "font_size": 24.0,
        "font_size_min": 12.0,
    }
    atualizado = engine.set_zone_content(tpl.id, ai_id, content)
    stored = _zone_by_id(atualizado, ai_id)["content"]
    assert "text" not in stored
    assert "instruction" not in stored
    assert stored["content_type"] == content_type


@pytest.mark.parametrize("content_type", ["AI_HOOK", "AI_TITLE", "AI_SUMMARY", "AI_QUESTION", "AI_CTA"])
def test_ai_types_sem_texto_rejeitam_text(engine, content_type):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.1})
    ai_id = _zone_by_type(tpl, ZONE_TYPE_AI_TEXT)["zone_id"]
    content = {
        "content_type": content_type,
        "text": "não deveria existir",
        "max_lines": 2,
        "max_chars": 100,
        "font_size": 24.0,
        "font_size_min": 12.0,
    }
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, ai_id, content)


@pytest.mark.parametrize("content_type", ["AI_HOOK", "AI_TITLE", "AI_SUMMARY", "AI_QUESTION", "AI_CTA"])
def test_ai_types_sem_texto_rejeitam_instruction(engine, content_type):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.1})
    ai_id = _zone_by_type(tpl, ZONE_TYPE_AI_TEXT)["zone_id"]
    content = {
        "content_type": content_type,
        "instruction": "não deveria existir",
        "max_lines": 2,
        "max_chars": 100,
        "font_size": 24.0,
        "font_size_min": 12.0,
    }
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, ai_id, content)


def test_content_type_invalido_rejeitado(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    content = _fixed_text_content(content_type="NAO_EXISTE")
    with pytest.raises(VocabularioInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, content)


def test_alignment_invalido_rejeitado(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    with pytest.raises(VocabularioInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(alignment="DIAGONAL"))


@pytest.mark.parametrize("alignment", ["LEFT", "CENTER", "RIGHT"])
def test_alignment_valido_aceito(engine, alignment):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    atualizado = engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(alignment=alignment))
    assert _zone_by_id(atualizado, caption_id)["content"]["alignment"] == alignment


def test_max_lines_fora_da_faixa_rejeitado(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(max_lines=MAX_LINES_RANGE[1] + 1))


def test_max_chars_fora_da_faixa_rejeitado(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(max_chars=0))


def test_font_size_fora_da_faixa_rejeitado(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(font_size=CONTENT_FONT_SIZE_RANGE[1] + 1))


def test_font_size_min_maior_que_font_size_rejeitado(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(font_size=16.0, font_size_min=32.0))


def test_font_size_min_igual_a_font_size_aceito(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    atualizado = engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(font_size=20.0, font_size_min=20.0))
    content = _zone_by_id(atualizado, caption_id)["content"]
    assert content["font_size"] == content["font_size_min"] == 20.0


def test_content_campo_desconhecido_rejeitado(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    content = _fixed_text_content()
    content["campo_inventado"] = "x"
    with pytest.raises(CampoInvalidoError):
        engine.set_zone_content(tpl.id, caption_id, content)


# ---------------------------------------------------------------------------
# content rejeitado em zona BACKGROUND/VIDEO/LOGO
# ---------------------------------------------------------------------------


def test_content_rejeitado_em_background_via_validate_layout():
    payload = _valid_layout()
    payload["zones"][0]["content"] = _fixed_text_content()
    with pytest.raises(CampoInvalidoError):
        validate_layout(payload)


def test_content_rejeitado_em_video_via_validate_layout():
    payload = _valid_layout()
    payload["zones"][1]["content"] = _fixed_text_content()
    with pytest.raises(CampoInvalidoError):
        validate_layout(payload)


def test_content_rejeitado_em_logo_via_set_zone_content(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1})
    logo_id = _zone_by_type(tpl, ZONE_TYPE_LOGO)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.set_zone_content(tpl.id, logo_id, _fixed_text_content())


def test_content_rejeitado_em_background_via_set_zone_content(engine):
    tpl = _create(engine)
    bg_id = _zone_by_type(tpl, ZONE_TYPE_BACKGROUND)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.set_zone_content(tpl.id, bg_id, _fixed_text_content())


def test_content_rejeitado_em_video_via_set_zone_content(engine):
    tpl = _create(engine)
    video_id = _zone_by_type(tpl, ZONE_TYPE_VIDEO)["zone_id"]
    with pytest.raises(ZonaProtegidaError):
        engine.set_zone_content(tpl.id, video_id, _fixed_text_content())


def test_content_rejeitado_em_logo_via_add_zone(engine):
    tpl = _create(engine)
    with pytest.raises(CampoInvalidoError):
        engine.add_zone(
            tpl.id,
            {
                "zone_type": ZONE_TYPE_LOGO,
                "x": 0.0,
                "y": 0.0,
                "width": 0.1,
                "height": 0.1,
                "content": _fixed_text_content(),
            },
        )


# ---------------------------------------------------------------------------
# set_zone_content -- operação isolada
# ---------------------------------------------------------------------------


def test_set_zone_content_zona_inexistente(engine):
    tpl = _create(engine)
    with pytest.raises(ZonaNaoEncontradaError):
        engine.set_zone_content(tpl.id, "zone-inexistente", _fixed_text_content())


def test_set_zone_content_template_inexistente(engine):
    with pytest.raises(TemplateNaoEncontradoError):
        engine.set_zone_content(
            "00000000-0000-0000-0000-000000000000", "qualquer", _fixed_text_content()
        )


def test_set_zone_content_none_limpa_content_existente(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    tpl = engine.set_zone_content(tpl.id, caption_id, _fixed_text_content())
    assert "content" in _zone_by_id(tpl, caption_id)

    limpo = engine.set_zone_content(tpl.id, caption_id, None)
    assert "content" not in _zone_by_id(limpo, caption_id)


def test_set_zone_content_substitui_content_anterior(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    tpl = engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(text="Primeiro"))
    atualizado = engine.set_zone_content(tpl.id, caption_id, _fixed_text_content(text="Segundo"))
    assert _zone_by_id(atualizado, caption_id)["content"]["text"] == "Segundo"


def test_set_zone_content_endereca_a_zona_correta_entre_duas_do_mesmo_tipo(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.0, "y": 0.0, "width": 0.3, "height": 0.1})
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_AI_TEXT, "x": 0.5, "y": 0.0, "width": 0.3, "height": 0.1})
    ai_zones = [z for z in tpl.layout["zones"] if z["zone_type"] == ZONE_TYPE_AI_TEXT]
    primeiro_id, segundo_id = ai_zones[0]["zone_id"], ai_zones[1]["zone_id"]

    atualizado = engine.set_zone_content(tpl.id, primeiro_id, _fixed_text_content(text="Só o primeiro"))

    com_content = _zone_by_id(atualizado, primeiro_id)
    sem_content = _zone_by_id(atualizado, segundo_id)
    assert com_content["content"]["text"] == "Só o primeiro"
    assert "content" not in sem_content


# ---------------------------------------------------------------------------
# add_zone aceita content já na criação
# ---------------------------------------------------------------------------


def test_add_zone_com_content_na_criacao(engine):
    tpl = _create(engine)
    atualizado = engine.add_zone(
        tpl.id,
        {
            "zone_type": ZONE_TYPE_CAPTION,
            "x": 0.0,
            "y": 0.8,
            "width": 1.0,
            "height": 0.2,
            "content": _fixed_text_content(text="Já nasce com texto"),
        },
    )
    caption = _zone_by_type(atualizado, ZONE_TYPE_CAPTION)
    assert caption["content"]["text"] == "Já nasce com texto"


def test_add_zone_sem_content_continua_funcionando(engine):
    tpl = _create(engine)
    atualizado = engine.add_zone(
        tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2}
    )
    caption = _zone_by_type(atualizado, ZONE_TYPE_CAPTION)
    assert "content" not in caption


# ---------------------------------------------------------------------------
# Retrocompatibilidade
# ---------------------------------------------------------------------------


def test_zonas_pre_existentes_sem_content_continuam_validas(engine):
    tpl = engine.create_template(
        name="Sem content",
        layout={
            "zones": [
                {"zone_type": ZONE_TYPE_BACKGROUND, "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
                {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2},
            ]
        },
    )
    assert "content" not in _zone_by_type(tpl, ZONE_TYPE_CAPTION)
    relido = engine.get_template(tpl.id)
    assert "content" not in _zone_by_type(relido, ZONE_TYPE_CAPTION)


# ---------------------------------------------------------------------------
# apply_auto_fit -- função pura
# ---------------------------------------------------------------------------


def test_auto_fit_texto_que_ja_cabe_nao_muda():
    r = apply_auto_fit("texto curto", max_lines=3, max_chars=100, font_size=32.0, font_size_min=16.0)
    assert r.text == "texto curto"
    assert r.font_size == 32.0
    assert not r.truncated
    assert not r.reduced
    assert r.lines <= 3


def test_auto_fit_texto_vazio():
    r = apply_auto_fit("", max_lines=2, max_chars=50, font_size=32.0, font_size_min=16.0)
    assert r.text == ""
    assert r.lines == 1
    assert not r.truncated


@pytest.mark.parametrize("tamanho", [200, 1000, 10000, 50000])
def test_auto_fit_nunca_excede_max_lines_e_max_chars_para_texto_gigante(tamanho):
    texto = "palavra " * tamanho
    r = apply_auto_fit(texto, max_lines=3, max_chars=120, font_size=48.0, font_size_min=12.0)
    assert len(r.text) <= 120
    assert r.lines <= 3


def test_auto_fit_reduz_font_size_antes_de_truncar_pesadamente():
    # texto moderado -- deve caber reduzindo a fonte, sem precisar do
    # corte adicional do passo 3 (max_chars generoso o bastante).
    texto = "Uma frase um pouco mais longa para testar a redução de fonte"
    r = apply_auto_fit(texto, max_lines=2, max_chars=400, font_size=48.0, font_size_min=12.0)
    assert len(r.text) <= 400
    assert r.lines <= 2
    assert r.font_size <= 48.0


def test_auto_fit_trunca_por_max_chars_independente_de_font_size():
    texto = "x" * 300
    r = apply_auto_fit(texto, max_lines=10, max_chars=50, font_size=8.0, font_size_min=8.0)
    assert len(r.text) <= 50
    assert r.truncated


def test_auto_fit_marcador_de_truncamento_presente_quando_corta():
    texto = "y" * 300
    r = apply_auto_fit(texto, max_lines=10, max_chars=50, font_size=8.0, font_size_min=8.0)
    assert r.truncated
    assert r.text.endswith("…")


def test_auto_fit_determinismo_mesma_entrada_mesma_saida():
    texto = "Texto de teste para determinismo " * 5
    r1 = apply_auto_fit(texto, max_lines=3, max_chars=150, font_size=40.0, font_size_min=14.0)
    r2 = apply_auto_fit(texto, max_lines=3, max_chars=150, font_size=40.0, font_size_min=14.0)
    assert r1 == r2


def test_auto_fit_idempotente_ponto_fixo():
    texto = "Texto grande o bastante para precisar de redução " * 20
    r1 = apply_auto_fit(texto, max_lines=2, max_chars=80, font_size=40.0, font_size_min=14.0)
    # rodar de novo com o TEXTO e FONT_SIZE já resultantes não deve mudar
    # nem o texto, nem o font_size, nem o número de linhas -- ponto fixo.
    r2 = apply_auto_fit(r1.text, max_lines=2, max_chars=80, font_size=r1.font_size, font_size_min=14.0)
    assert r2.text == r1.text
    assert r2.font_size == r1.font_size
    assert r2.lines == r1.lines
    assert not r2.truncated
    assert not r2.reduced


def test_auto_fit_font_size_min_maior_que_font_size_rejeitado():
    with pytest.raises(CampoInvalidoError):
        apply_auto_fit("texto", max_lines=2, max_chars=50, font_size=10.0, font_size_min=20.0)


def test_auto_fit_max_lines_fora_da_faixa_rejeitado():
    with pytest.raises(CampoInvalidoError):
        apply_auto_fit("texto", max_lines=MAX_LINES_RANGE[1] + 1, max_chars=50, font_size=32.0, font_size_min=16.0)


def test_auto_fit_max_chars_fora_da_faixa_rejeitado():
    with pytest.raises(CampoInvalidoError):
        apply_auto_fit("texto", max_lines=2, max_chars=0, font_size=32.0, font_size_min=16.0)


def test_auto_fit_nunca_reduz_abaixo_de_font_size_min():
    texto = "texto enorme " * 500
    r = apply_auto_fit(texto, max_lines=1, max_chars=500, font_size=100.0, font_size_min=50.0)
    assert r.font_size >= 50.0


# ---------------------------------------------------------------------------
# Regras pré-existentes (Prompts 36-39) continuam valendo
# ---------------------------------------------------------------------------


def test_zone_id_continua_estavel_apos_set_zone_content(engine):
    tpl = _create(engine)
    tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2})
    caption_id = _zone_by_type(tpl, ZONE_TYPE_CAPTION)["zone_id"]
    atualizado = engine.set_zone_content(tpl.id, caption_id, _fixed_text_content())
    assert _zone_by_type(atualizado, ZONE_TYPE_CAPTION)["zone_id"] == caption_id


def test_geometria_de_zona_com_content_continua_validada(engine):
    tpl = _create(engine)
    with pytest.raises(GeometriaInvalidaError):
        engine.add_zone(
            tpl.id,
            {
                "zone_type": ZONE_TYPE_CAPTION,
                "x": 0.9,
                "y": 0.9,
                "width": 0.5,
                "height": 0.5,
                "content": _fixed_text_content(),
            },
        )
