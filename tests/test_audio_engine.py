# -*- coding: utf-8 -*-
"""PROMPT 30 -- Audio Engine: testes.

Cobre a lista de TESTES do Prompt: cada campo isolado (sozinho); cada um
com valor inválido (tipo/intervalo/não-finito); múltiplos campos juntos;
um campo mudando sem afetar outros; isolamento de fingerprint em relação
a ``CUTS``/``CROP``/``VISUAL_ADJUSTMENTS``/``TEMPLATE`` nas duas direções;
reaproveitamento atômico de ``update_category``; concorrência real via
``threading.Barrier``; integração read-only com o badge ``EDITED``; a
PROVA NEGATIVA explícita de que ``AUDIO_PROCESSED`` NÃO acende; restart
com instâncias totalmente novas; idempotência; a operação em lote
completa (sucesso total, parcial, lote vazio, campos vazios,
pré-validação antes de qualquer escrita); garantias estruturais via AST;
e confirmação de que nenhuma migration nova foi criada.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import _sistema.audio_engine as audio_engine
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.audio_engine import (
    AUDIO_SETTINGS,
    AudioEngine,
    AudioEngineError,
    AudioSettingsState,
    BulkAudioEditResult,
    CampoInvalidoError,
)
from _sistema.domain import Project, SourceAsset, Video
from _sistema.edit_project import CROP, CUTS, EditProjectManager, TEMPLATE
from _sistema.media_catalog import BADGE_AUDIO_PROCESSED, BADGE_EDITED, MediaCatalogService
from _sistema.storage.database import LocalDatabase


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão dos Prompts 27/27.5/28/29)
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
    return AudioEngine(manager)


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
# 0. Construção
# ---------------------------------------------------------------------------


def test_construtor_rejeita_manager_de_tipo_errado():
    with pytest.raises(TypeError):
        AudioEngine("nao-e-um-manager")


def test_estado_inicial_e_tudo_none(engine, project):
    estado = engine.get_audio_settings(project.id)
    assert estado == AudioSettingsState()


# ---------------------------------------------------------------------------
# 1. Cada campo funcionando SOZINHO
# ---------------------------------------------------------------------------


def test_definir_apenas_volume(engine, project):
    r = engine.set_volume(project.id, 1.5)
    assert r.volume == 1.5
    assert r.mute is None
    assert r.gain is None


def test_definir_apenas_mute(engine, project):
    r = engine.set_mute(project.id, True)
    assert r.mute is True
    assert r.volume is None


def test_definir_apenas_gain(engine, project):
    r = engine.set_gain(project.id, -6.0)
    assert r.gain == -6.0
    assert r.volume is None


def test_definir_apenas_clipping_protection(engine, project):
    r = engine.set_clipping_protection(project.id, True)
    assert r.clipping_protection is True
    assert r.gain is None


def test_definir_apenas_fade_in(engine, project):
    r = engine.set_fade_in(project.id, 2.5)
    assert r.fade_in_seconds == 2.5
    assert r.fade_out_seconds is None


def test_definir_apenas_fade_out(engine, project):
    r = engine.set_fade_out(project.id, 3.5)
    assert r.fade_out_seconds == 3.5
    assert r.fade_in_seconds is None


def test_definir_apenas_normalization(engine, project):
    r = engine.set_normalization(project.id, True, -14.0)
    assert r.normalization_enabled is True
    assert r.normalization_target_lufs == -14.0
    assert r.volume is None


def test_definir_normalization_sem_target_lufs_fica_none(engine, project):
    r = engine.set_normalization(project.id, True)
    assert r.normalization_enabled is True
    assert r.normalization_target_lufs is None


def test_definir_apenas_noise_reduction(engine, project):
    r = engine.set_noise_reduction(project.id, True, 0.6)
    assert r.noise_reduction_enabled is True
    assert r.noise_reduction_intensity == 0.6
    assert r.fade_in_seconds is None


def test_definir_noise_reduction_com_intensity_default(engine, project):
    r = engine.set_noise_reduction(project.id, False)
    assert r.noise_reduction_enabled is False
    assert r.noise_reduction_intensity == 0.0


# ---------------------------------------------------------------------------
# 2. Cada campo com valor INVÁLIDO (tipo / intervalo / não-finito)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valor", [-0.1, 3.1, 10.0, -5.0])
def test_volume_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_volume(project.id, valor)


@pytest.mark.parametrize("valor", [-24.1, 24.1, 100.0, -100.0])
def test_gain_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_gain(project.id, valor)


@pytest.mark.parametrize("valor", [-0.01, 30.1, 999.0, -5.0])
def test_fade_in_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_fade_in(project.id, valor)


@pytest.mark.parametrize("valor", [-0.01, 30.1, 999.0, -5.0])
def test_fade_out_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_fade_out(project.id, valor)


@pytest.mark.parametrize("valor", [-36.1, -5.9, 0.0, 10.0])
def test_target_lufs_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_normalization(project.id, True, valor)


@pytest.mark.parametrize("valor", [-0.01, 1.01, 5.0, -1.0])
def test_noise_reduction_intensity_fora_do_intervalo_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_noise_reduction(project.id, True, valor)


@pytest.mark.parametrize("valor", [float("inf"), float("-inf"), float("nan")])
def test_volume_nao_finito_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_volume(project.id, valor)


@pytest.mark.parametrize("valor", [float("inf"), float("-inf"), float("nan")])
def test_gain_nao_finito_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_gain(project.id, valor)


@pytest.mark.parametrize("valor", [float("inf"), float("-inf"), float("nan")])
def test_fade_in_nao_finito_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_fade_in(project.id, valor)


@pytest.mark.parametrize("valor", [float("inf"), float("-inf"), float("nan")])
def test_target_lufs_nao_finito_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_normalization(project.id, True, valor)


@pytest.mark.parametrize(
    "metodo,args",
    [
        ("set_volume", (True,)),
        ("set_gain", (True,)),
        ("set_fade_in", (True,)),
        ("set_fade_out", (False,)),
    ],
)
def test_bool_e_rejeitado_onde_float_e_esperado(engine, project, metodo, args):
    """Armadilha bool-como-int: em Python ``bool`` é subclasse de
    ``int``/``float``-compatível -- precisa ser explicitamente rejeitado
    antes da checagem numérica genérica."""
    with pytest.raises(CampoInvalidoError):
        getattr(engine, metodo)(project.id, *args)


@pytest.mark.parametrize("valor", ["1.0", None, [], {}, object()])
def test_volume_tipo_errado_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_volume(project.id, valor)


@pytest.mark.parametrize("valor", [1, "sim", None, 0])
def test_mute_tipo_errado_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_mute(project.id, valor)


@pytest.mark.parametrize("valor", [1, "sim", None, 0])
def test_clipping_protection_tipo_errado_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_clipping_protection(project.id, valor)


@pytest.mark.parametrize("valor", [1, "sim", None])
def test_normalization_enabled_tipo_errado_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_normalization(project.id, valor)


@pytest.mark.parametrize("valor", ["-14", [], {}])
def test_target_lufs_tipo_errado_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_normalization(project.id, True, valor)


@pytest.mark.parametrize("valor", [1, "sim", None])
def test_noise_reduction_enabled_tipo_errado_e_erro(engine, project, valor):
    with pytest.raises(CampoInvalidoError):
        engine.set_noise_reduction(project.id, valor)


# ---------------------------------------------------------------------------
# 3. Múltiplos campos juntos / um campo não afeta os outros
# ---------------------------------------------------------------------------


def test_multiplos_campos_persistem_juntos(engine, project):
    engine.set_volume(project.id, 1.2)
    engine.set_mute(project.id, False)
    engine.set_gain(project.id, -2.0)
    engine.set_normalization(project.id, True, -14.0)
    engine.set_clipping_protection(project.id, True)
    engine.set_fade_in(project.id, 1.0)
    engine.set_fade_out(project.id, 1.5)
    engine.set_noise_reduction(project.id, True, 0.3)

    final = engine.get_audio_settings(project.id)
    assert final == AudioSettingsState(
        volume=1.2,
        mute=False,
        gain=-2.0,
        normalization_enabled=True,
        normalization_target_lufs=-14.0,
        clipping_protection=True,
        fade_in_seconds=1.0,
        fade_out_seconds=1.5,
        noise_reduction_enabled=True,
        noise_reduction_intensity=0.3,
    )


def test_alterar_um_campo_nao_afeta_os_demais_ja_definidos(engine, project):
    engine.set_volume(project.id, 1.0)
    engine.set_gain(project.id, 5.0)
    engine.set_fade_in(project.id, 2.0)

    engine.set_volume(project.id, 2.5)

    final = engine.get_audio_settings(project.id)
    assert final.volume == 2.5
    assert final.gain == 5.0
    assert final.fade_in_seconds == 2.0


def test_normalization_e_noise_reduction_nao_interferem_entre_si(engine, project):
    engine.set_normalization(project.id, True, -20.0)
    engine.set_noise_reduction(project.id, True, 0.7)

    engine.set_normalization(project.id, False, None)

    final = engine.get_audio_settings(project.id)
    assert final.normalization_enabled is False
    assert final.normalization_target_lufs is None
    assert final.noise_reduction_enabled is True
    assert final.noise_reduction_intensity == 0.7


# ---------------------------------------------------------------------------
# 4. Isolamento de fingerprint em relação a outras categorias (2 direções)
# ---------------------------------------------------------------------------


def test_alterar_audio_settings_nunca_muda_fingerprint_de_cuts(database, engine, project, manager):
    manager.set_category(project.id, CUTS, {"segments": []})
    before = manager.get_category(project.id, CUTS)

    engine.set_volume(project.id, 1.8)
    engine.set_fade_in(project.id, 2.0)

    after = manager.get_category(project.id, CUTS)
    assert before.fingerprint == after.fingerprint


def test_alterar_audio_settings_nunca_muda_fingerprint_de_crop(database, engine, project, manager):
    manager.set_category(project.id, CROP, {"zoom": 1.5})
    before = manager.get_category(project.id, CROP)

    engine.set_gain(project.id, -3.0)

    after = manager.get_category(project.id, CROP)
    assert before.fingerprint == after.fingerprint


def test_alterar_audio_settings_nunca_muda_fingerprint_de_template(database, engine, project, manager):
    manager.set_category(project.id, TEMPLATE, {"id": "x"})
    before = manager.get_category(project.id, TEMPLATE)

    engine.set_normalization(project.id, True, -16.0)

    after = manager.get_category(project.id, TEMPLATE)
    assert before.fingerprint == after.fingerprint


def test_alterar_cuts_crop_ou_template_nunca_muda_fingerprint_de_audio_settings(database, engine, project, manager):
    engine.set_volume(project.id, 1.3)
    before = manager.get_category(project.id, AUDIO_SETTINGS)

    manager.set_category(project.id, CUTS, {"segments": []})
    manager.set_category(project.id, CROP, {"zoom": 1.2})
    manager.set_category(project.id, TEMPLATE, {"id": "y"})

    after = manager.get_category(project.id, AUDIO_SETTINGS)
    assert before.fingerprint == after.fingerprint


def test_categorias_audio_settings_e_cuts_sao_distintas(database, engine, project, manager):
    engine.set_volume(project.id, 1.0)
    manager.set_category(project.id, CUTS, {"segments": []})
    categorias = manager.list_categories(project.id)
    assert AUDIO_SETTINGS in categorias
    assert CUTS in categorias


# ---------------------------------------------------------------------------
# 5. Reaproveitamento atômico de update_category (sem get+set separados)
# ---------------------------------------------------------------------------


def test_set_field_usa_update_category_nao_get_e_set_separados():
    """Verificação estrutural: nenhum método de escrita chama
    ``get_category`` seguido de ``set_category`` como duas chamadas
    (o que reabriria a janela de TOCTOU que ``update_category`` fecha)."""
    source = Path(audio_engine.__file__).read_text(encoding="utf-8")
    assert "set_category(" not in source
    assert source.count("update_category(") >= 2  # _set_field + bulk


# ---------------------------------------------------------------------------
# 6. GATE 6 -- concorrência real (threading.Barrier)
# ---------------------------------------------------------------------------


def test_duas_operacoes_concorrentes_em_campos_diferentes_nao_se_perdem(app_paths, database, project):
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(fn_name: str, args: tuple):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_manager = EditProjectManager(db_instance)
        local_engine = AudioEngine(local_manager)
        barrier.wait(timeout=5)
        try:
            getattr(local_engine, fn_name)(project.id, *args)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("set_fade_in", (3.0,))),
        threading.Thread(target=worker, args=("set_fade_out", (4.0,))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    final = AudioEngine(EditProjectManager(database)).get_audio_settings(project.id)
    assert final.fade_in_seconds == 3.0, "mudanca concorrente perdida"
    assert final.fade_out_seconds == 4.0, "mudanca concorrente perdida"


def test_duas_operacoes_concorrentes_em_categorias_diferentes_nao_se_perdem(app_paths, database, project):
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker_audio():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_engine = AudioEngine(EditProjectManager(db_instance))
        barrier.wait(timeout=5)
        try:
            local_engine.set_volume(project.id, 1.7)
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

    threads = [threading.Thread(target=worker_audio), threading.Thread(target=worker_manager)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    reread_manager = EditProjectManager(database)
    reread_engine = AudioEngine(reread_manager)
    assert reread_engine.get_audio_settings(project.id).volume == 1.7
    assert reread_manager.get_category(project.id, CROP).data == {"zoom": 2.0}


# ---------------------------------------------------------------------------
# 7. Integração read-only com o badge EDITED -- e PROVA NEGATIVA de AUDIO_PROCESSED
# ---------------------------------------------------------------------------


def test_definir_volume_faz_o_video_aparecer_como_edited(database, engine, project, video):
    catalog = MediaCatalogService(database)
    antes = catalog.get_item(video.id)
    assert BADGE_EDITED not in antes.system_badges

    engine.set_volume(project.id, 1.4)

    depois = catalog.get_item(video.id)
    assert BADGE_EDITED in depois.system_badges


def test_gravar_audio_settings_nao_acende_badge_audio_processed(database, engine, project, video):
    """O TESTE MAIS IMPORTANTE DESTE PROMPT (per a própria especificação):
    prova negativa explícita de que gravar QUALQUER decisão de áudio via
    AudioEngine NÃO faz ``AUDIO_PROCESSED`` acender -- diferente do
    padrão de ``EDITED`` dos Prompts 28/29. Esse badge só deve virar
    verdadeiro quando um processamento REAL (fora do escopo deste
    Prompt) produzir evidência estruturada genuína."""
    catalog = MediaCatalogService(database)
    antes = catalog.get_item(video.id)
    assert BADGE_AUDIO_PROCESSED not in antes.system_badges

    engine.set_volume(project.id, 1.9)
    engine.set_gain(project.id, 6.0)
    engine.set_normalization(project.id, True, -14.0)
    engine.set_clipping_protection(project.id, True)
    engine.set_fade_in(project.id, 2.0)
    engine.set_fade_out(project.id, 2.0)
    engine.set_noise_reduction(project.id, True, 0.9)
    engine.set_mute(project.id, False)

    depois = catalog.get_item(video.id)
    assert BADGE_AUDIO_PROCESSED not in depois.system_badges, (
        "AUDIO_PROCESSED nao pode acender apenas por AUDIO_SETTINGS ter sido gravado"
    )
    # EDITED, por outro lado, deve ter acendido (mesma expressão genérica).
    assert BADGE_EDITED in depois.system_badges


def test_audio_processed_permanece_sempre_falso_na_expressao_sql():
    """Verificação estrutural direta da expressão SQL de
    ``AUDIO_PROCESSED`` em ``media_catalog.py`` -- deve continuar
    hardcoded como sempre-falso (``"0"``), nunca uma expressão real
    derivada de ``edit_state_json``, confirmando que este Prompt não
    tocou ``media_catalog.py``."""
    from _sistema import media_catalog

    assert media_catalog._BADGE_SQL_EXPRESSIONS[BADGE_AUDIO_PROCESSED] == "0"


def test_audio_engine_nao_importa_media_catalog():
    tree = _parse_audio_engine_module()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] + [alias.name for alias in node.names]
            )
            for name in names:
                assert "media_catalog" not in name


# ---------------------------------------------------------------------------
# 8. GATE 4 -- RESTART (instâncias totalmente novas)
# ---------------------------------------------------------------------------


def test_estado_de_audio_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    engine_a = AudioEngine(EditProjectManager(db_a))
    engine_a.set_volume(project.id, 1.6)
    engine_a.set_fade_in(project.id, 1.2)
    engine_a.set_noise_reduction(project.id, True, 0.5)

    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    engine_b = AudioEngine(EditProjectManager(db_b))

    final = engine_b.get_audio_settings(project.id)
    assert final.volume == 1.6
    assert final.fade_in_seconds == 1.2
    assert final.noise_reduction_enabled is True
    assert final.noise_reduction_intensity == 0.5


# ---------------------------------------------------------------------------
# 9. GATE 5 -- IDEMPOTÊNCIA
# ---------------------------------------------------------------------------


def test_repetir_o_mesmo_set_volume_sequencialmente_e_deterministico(engine, project):
    r1 = engine.set_volume(project.id, 1.5)
    r2 = engine.set_volume(project.id, 1.5)
    assert r1 == r2 == engine.get_audio_settings(project.id)


def test_repetir_o_mesmo_set_normalization_sequencialmente_nao_acumula(engine, project, manager):
    engine.set_normalization(project.id, True, -14.0)
    before = manager.get_category(project.id, AUDIO_SETTINGS)
    engine.set_normalization(project.id, True, -14.0)
    after = manager.get_category(project.id, AUDIO_SETTINGS)
    assert before.fingerprint == after.fingerprint
    assert before.revision == after.revision


def test_repetir_bulk_com_mesmo_valor_e_no_op_na_segunda_chamada(engine, project, project2):
    r1 = engine.bulk_set_audio_settings([project.id, project2.id], volume=1.1)
    assert set(r1.updated) == {project.id, project2.id}

    r2 = engine.bulk_set_audio_settings([project.id, project2.id], volume=1.1)
    assert r2.updated == ()
    assert set(r2.unchanged) == {project.id, project2.id}


# ---------------------------------------------------------------------------
# 10. Operação em lote (batch) -- matriz completa
# ---------------------------------------------------------------------------


def test_bulk_sucesso_total(engine, project, project2):
    result = engine.bulk_set_audio_settings([project.id, project2.id], volume=0.8, mute=True)
    assert isinstance(result, BulkAudioEditResult)
    assert set(result.updated) == {project.id, project2.id}
    assert result.unchanged == ()
    assert result.failed == ()
    assert engine.get_audio_settings(project.id).volume == 0.8
    assert engine.get_audio_settings(project2.id).mute is True


def test_bulk_sucesso_parcial_um_projeto_inexistente(engine, project):
    result = engine.bulk_set_audio_settings([project.id, "projeto-que-nao-existe"], gain=2.0)
    assert result.updated == (project.id,)
    assert len(result.failed) == 1
    assert result.failed[0][0] == "projeto-que-nao-existe"


def test_bulk_lote_de_project_ids_vazio_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_audio_settings([], volume=1.0)


def test_bulk_sem_nenhum_campo_e_erro_estruturado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_audio_settings([project.id])


def test_bulk_com_project_ids_duplicados_deduplicados(engine, project):
    result = engine.bulk_set_audio_settings([project.id, project.id, project.id], volume=1.2)
    assert result.requested == (project.id,)
    assert result.updated == (project.id,)


def test_bulk_campo_invalido_nao_escreve_em_nenhum_projeto_antes_de_validar(engine, project, project2):
    """Pré-validação-antes-de-qualquer-escrita: se UM campo do lote for
    inválido, NENHUM projeto deve ser tocado -- mesmo os que viriam antes
    dele na ordem de iteração."""
    engine.set_volume(project.id, 1.0)
    before = engine.get_audio_settings(project.id)

    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_audio_settings([project.id, project2.id], volume=999.0)

    after = engine.get_audio_settings(project.id)
    assert before == after
    assert engine.get_audio_settings(project2.id) == AudioSettingsState()


def test_bulk_campo_desconhecido_e_erro_estruturado(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_audio_settings([project.id], campo_fantasma=True)


def test_bulk_normalization_target_lufs_sem_normalization_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_audio_settings([project.id], normalization_target_lufs=-10.0)


def test_bulk_noise_reduction_intensity_sem_enabled_e_erro(engine, project):
    with pytest.raises(CampoInvalidoError):
        engine.bulk_set_audio_settings([project.id], noise_reduction_intensity=0.5)


def test_bulk_com_campos_aninhados_funciona(engine, project, project2):
    result = engine.bulk_set_audio_settings(
        [project.id, project2.id],
        normalization=True,
        normalization_target_lufs=-16.0,
        noise_reduction_enabled=True,
        noise_reduction_intensity=0.4,
    )
    assert set(result.updated) == {project.id, project2.id}
    final = engine.get_audio_settings(project.id)
    assert final.normalization_enabled is True
    assert final.normalization_target_lufs == -16.0
    assert final.noise_reduction_enabled is True
    assert final.noise_reduction_intensity == 0.4


def test_bulk_valor_repetido_classifica_como_unchanged(engine, project):
    engine.bulk_set_audio_settings([project.id], gain=3.0)
    result = engine.bulk_set_audio_settings([project.id], gain=3.0)
    assert result.updated == ()
    assert result.unchanged == (project.id,)


def test_bulk_nao_afeta_categorias_de_outros_modulos(engine, project, manager):
    manager.set_category(project.id, CUTS, {"segments": []})
    before = manager.get_category(project.id, CUTS)

    engine.bulk_set_audio_settings([project.id], volume=1.3)

    after = manager.get_category(project.id, CUTS)
    assert before.fingerprint == after.fingerprint


# ---------------------------------------------------------------------------
# 11. Garantias estruturais (AST) e schema
# ---------------------------------------------------------------------------


def _parse_audio_engine_module() -> ast.AST:
    return ast.parse(Path(audio_engine.__file__).read_text(encoding="utf-8"))


def test_audio_engine_nao_importa_modulos_protegidos():
    tree = _parse_audio_engine_module()
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
            assert proibido not in names, f"audio_engine.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_audio_engine_nao_chama_subprocess_ffmpeg_ffprobe():
    """AST, nunca grep textual ingênuo -- a própria docstring do módulo
    cita 'ffmpeg'/'atempo'/'subprocess' para explicar o que NÃO é feito
    aqui (mesmo problema já enfrentado nos Prompts 28/29)."""
    tree = _parse_audio_engine_module()
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


def test_audio_engine_nunca_cria_job_artifact_publication_schedule_video():
    source = Path(audio_engine.__file__).read_text(encoding="utf-8")
    for proibido in ("Job(", "Artifact(", "Publication(", "Schedule(", "Video("):
        assert proibido not in source


def test_audio_engine_nao_abre_transacao_propria_nem_toca_storage_diretamente():
    """AST, não grep textual ingênuo -- verificação real é sobre CHAMADAS
    (nós ast.Attribute/ast.Call), nunca sobre a string aparecer em algum
    lugar do arquivo (que incluiria comentários/docstrings)."""
    tree = _parse_audio_engine_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "transaction"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("get", "save", "insert", "delete"):
                assert not (
                    isinstance(node.func.value, ast.Name) and node.func.value.id == "database"
                ), "audio_engine.py não pode chamar métodos de LocalDatabase diretamente"


def test_audio_engine_nao_le_categoria_cuts():
    """Decisão documentada na seção 0.6 do módulo: AudioEngine NÃO lê a
    categoria CUTS para validação cruzada de fade nesta etapa -- garantir
    estruturalmente que essa decisão não foi silenciosamente revertida."""
    source = Path(audio_engine.__file__).read_text(encoding="utf-8")
    # A única menção a "CUTS" no arquivo deve estar dentro da docstring
    # (seção 0.6), nunca em código executável (import ou uso real).
    tree = _parse_audio_engine_module()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Name, ast.Attribute)):
            ident = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "module", None)
            if ident:
                assert ident != "CUTS"


def test_audio_engine_nao_escreve_campo_background_music(engine, project):
    """Decisão documentada: background_music é deliberadamente omitido
    (nenhum setter, nenhum getter, nenhuma chave-fantasma)."""
    assert not hasattr(engine, "set_background_music")
    assert not hasattr(engine, "get_background_music")
    engine.set_volume(project.id, 1.0)
    final_raw = EditProjectManager(engine._manager.database).get_category(project.id, AUDIO_SETTINGS)
    assert "background_music" not in final_raw.data


def test_leitura_rejeita_sub_objeto_corrompido_persistido_diretamente(database, project, manager):
    """Achado no GATE ADVERSARIAL (mesmo padrão de visual_editor.py):
    ``dict("string")`` tenta iterar a string como pares chave/valor e
    levanta um ``ValueError`` cru -- ``get_audio_settings`` não pode
    deixar isso escapar caso ``normalization``/``noise_reduction`` tenham
    sido persistidos como um valor não-objeto."""
    manager.set_category(project.id, AUDIO_SETTINGS, {"normalization": "nao-e-um-objeto"})
    engine = AudioEngine(manager)
    with pytest.raises(CampoInvalidoError):
        engine.get_audio_settings(project.id)


def test_leitura_rejeita_noise_reduction_corrompido(database, project, manager):
    manager.set_category(project.id, AUDIO_SETTINGS, {"noise_reduction": 42})
    engine = AudioEngine(manager)
    with pytest.raises(CampoInvalidoError):
        engine.get_audio_settings(project.id)


# ---------------------------------------------------------------------------
# 12. Regressão -- project_id malformado (não-UUID) nunca escapa como ValueError cru
# ---------------------------------------------------------------------------


def test_set_field_com_project_id_nao_uuid_e_erro_estruturado(engine):
    """Achado no GATE ADVERSARIAL: ``LocalDatabase`` valida o formato de
    ``entity_id`` e levanta um ``ValueError`` cru ("entity_id deve ser
    UUID válido") para um project_id que não é sequer um UUID -- distinto
    de ``ProjectNaoEncontradoError`` (UUID válido mas inexistente). Nenhum
    ``ValueError`` cru pode escapar de um método público deste módulo."""
    with pytest.raises(CampoInvalidoError):
        engine.set_volume("nao-e-um-uuid", 1.0)


def test_get_audio_settings_com_project_id_nao_uuid_e_erro_estruturado(engine):
    with pytest.raises(CampoInvalidoError):
        engine.get_audio_settings("nao-e-um-uuid")


def test_bulk_com_project_id_nao_uuid_e_classificado_como_falha_estruturada(engine, project):
    result = engine.bulk_set_audio_settings([project.id, "nao-e-um-uuid"], volume=1.0)
    assert result.updated == (project.id,)
    assert result.failed == (("nao-e-um-uuid", "PROJETO_ID_INVALIDO"),)


def test_nenhuma_migration_nova_criada_por_este_prompt():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9
