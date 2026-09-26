# -*- coding: utf-8 -*-
"""PROMPT 28 -- Editor: corte e timeline: testes.

Cobre a lista de TESTES do Prompt: timeline vazia/inicial; trim (simples,
os dois lados, e o caso de reduzir a <=0); split (válido e fora do
segmento); remover trecho (no meio e o segmento inteiro); juntar trechos
(adjacentes válidos e a rejeição de não-adjacentes); reorganizar (válido e
as três formas de violação, testadas separadamente); alterar velocidade
(válida e fora do limite); uma sequência de operações em cadeia;
isolamento de fingerprint entre categorias; integração read-only com o
badge EDITED do Media Catalog; garantias estruturais via AST; e
confirmação de que nenhuma migration nova foi criada.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import _sistema.timeline_editor as timeline_editor
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Project, SourceAsset, Video
from _sistema.edit_project import CUTS, SPEED, EditProjectManager, TEMPLATE
from _sistema.media_catalog import BADGE_EDITED, MediaCatalogService
from _sistema.storage.database import LocalDatabase
from _sistema.timeline_editor import (
    MAX_SPEED,
    MIN_SPEED,
    DuracaoInvalidaError,
    PontoDeCorteInvalidoError,
    ReordenacaoInvalidaError,
    Segment,
    SegmentoInvalidoError,
    SegmentoNaoEncontradoError,
    SegmentosNaoAdjacentesError,
    TimelineEditor,
    TimelineJaInicializadaError,
    TimelineNaoInicializadaError,
    VelocidadeInvalidaError,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão dos Prompts 25/26/27/27b/27.5)
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
    return TimelineEditor(database)


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


# ---------------------------------------------------------------------------
# 1. Timeline inicial vazia / inicialização
# ---------------------------------------------------------------------------


def test_timeline_vazia_para_projeto_recem_criado(editor, project):
    assert editor.get_timeline(project.id) == ()


def test_operacao_antes_de_inicializar_levanta_erro(editor, project):
    with pytest.raises(TimelineNaoInicializadaError):
        editor.trim(project.id, "qualquer", start=1.0)
    with pytest.raises(TimelineNaoInicializadaError):
        editor.split(project.id, "qualquer", 1.0)


def test_initialize_timeline_cria_um_unico_segmento_cobrindo_tudo(editor, project):
    segments = editor.initialize_timeline(project.id, 120.0)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.start == 0.0
    assert seg.end == 120.0
    assert seg.speed == 1.0
    assert seg.source_segment_id is None


def test_initialize_timeline_duas_vezes_levanta_erro(editor, project):
    editor.initialize_timeline(project.id, 60.0)
    with pytest.raises(TimelineJaInicializadaError):
        editor.initialize_timeline(project.id, 30.0)


def test_initialize_timeline_duracao_invalida_levanta_erro(editor, project):
    with pytest.raises(DuracaoInvalidaError):
        editor.initialize_timeline(project.id, 0.0)
    with pytest.raises(DuracaoInvalidaError):
        editor.initialize_timeline(project.id, -5.0)


# ---------------------------------------------------------------------------
# 2. trim
# ---------------------------------------------------------------------------


def test_trim_inicio(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.trim(project.id, seg.segment_id, start=10.0)
    assert result[0].start == 10.0
    assert result[0].end == 100.0


def test_trim_fim(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.trim(project.id, seg.segment_id, end=90.0)
    assert result[0].start == 0.0
    assert result[0].end == 90.0


def test_trim_inicio_e_fim(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.trim(project.id, seg.segment_id, start=10.0, end=90.0)
    assert result[0].start == 10.0
    assert result[0].end == 90.0


def test_trim_que_reduziria_a_duracao_a_zero_ou_menos_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    with pytest.raises(SegmentoInvalidoError):
        editor.trim(project.id, seg.segment_id, start=100.0)
    with pytest.raises(SegmentoInvalidoError):
        editor.trim(project.id, seg.segment_id, start=50.0, end=50.0)
    with pytest.raises(SegmentoInvalidoError):
        editor.trim(project.id, seg.segment_id, start=60.0, end=50.0)


def test_trim_nao_pode_expandir_alem_do_ja_registrado(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    editor.trim(project.id, seg.segment_id, start=10.0, end=90.0)
    with pytest.raises(SegmentoInvalidoError):
        editor.trim(project.id, seg.segment_id, start=5.0)
    with pytest.raises(SegmentoInvalidoError):
        editor.trim(project.id, seg.segment_id, end=95.0)


def test_trim_segmento_inexistente_levanta_erro(editor, project):
    editor.initialize_timeline(project.id, 100.0)
    with pytest.raises(SegmentoNaoEncontradoError):
        editor.trim(project.id, "nao-existe", start=1.0)


def test_trim_sem_start_nem_end_levanta_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    with pytest.raises(SegmentoInvalidoError):
        editor.trim(project.id, seg.segment_id)


# ---------------------------------------------------------------------------
# 3. split
# ---------------------------------------------------------------------------


def test_split_em_ponto_valido_soma_das_duracoes_preservada(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.split(project.id, seg.segment_id, 40.0)
    assert len(result) == 2
    left, right = result
    assert left.start == 0.0 and left.end == 40.0
    assert right.start == 40.0 and right.end == 100.0
    assert abs((left.duration() + right.duration()) - 100.0) < 1e-9
    assert left.source_segment_id == seg.segment_id
    assert right.source_segment_id == seg.segment_id
    assert left.segment_id != right.segment_id


def test_split_em_ponto_fora_do_segmento_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    with pytest.raises(PontoDeCorteInvalidoError):
        editor.split(project.id, seg.segment_id, 150.0)
    with pytest.raises(PontoDeCorteInvalidoError):
        editor.split(project.id, seg.segment_id, -10.0)


def test_split_na_borda_exata_e_erro(editor, project):
    """Um ponto igual a uma das bordas não corta nada -- deve ser
    rejeitado, nunca produzir um segmento de duração zero."""
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    with pytest.raises(PontoDeCorteInvalidoError):
        editor.split(project.id, seg.segment_id, 0.0)
    with pytest.raises(PontoDeCorteInvalidoError):
        editor.split(project.id, seg.segment_id, 100.0)


# ---------------------------------------------------------------------------
# 4. remover trecho
# ---------------------------------------------------------------------------


def test_remover_trecho_no_meio_de_um_segmento(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.remove_range(project.id, seg.segment_id, 40.0, 60.0)
    assert len(result) == 2
    left, right = result
    assert left.start == 0.0 and left.end == 40.0
    assert right.start == 60.0 and right.end == 100.0
    assert left.source_segment_id == seg.segment_id
    assert right.source_segment_id == seg.segment_id


def test_remover_trecho_tocando_uma_borda_equivale_a_trim(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.remove_range(project.id, seg.segment_id, 0.0, 20.0)
    assert len(result) == 1
    assert result[0].start == 20.0 and result[0].end == 100.0


def test_remover_trecho_que_e_o_segmento_inteiro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.remove_range(project.id, seg.segment_id, 0.0, 100.0)
    assert result == ()


def test_remover_o_ultimo_segmento_restante_esvazia_a_timeline_e_permite_reinicializar(editor, project):
    """Achado no GATE ADVERSARIAL, decisão documentada na seção 1.4B do
    módulo: esvaziar a timeline por edição válida (remover o último
    segmento) é indistinguível de "nunca inicializada" -- get_timeline
    volta a retornar tupla vazia, e initialize_timeline aceita
    reinicializar sem levantar TimelineJaInicializadaError."""
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.remove_segment(project.id, seg.segment_id)
    assert result == ()
    assert editor.get_timeline(project.id) == ()

    reinicializada = editor.initialize_timeline(project.id, 555.0)
    assert len(reinicializada) == 1
    assert reinicializada[0].end == 555.0


def test_remove_segment_atalho_remove_o_segmento_inteiro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    left, right = editor.split(project.id, seg.segment_id, 50.0)
    result = editor.remove_segment(project.id, left.segment_id)
    assert len(result) == 1
    assert result[0].segment_id == right.segment_id


def test_remover_trecho_fora_do_segmento_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    with pytest.raises(PontoDeCorteInvalidoError):
        editor.remove_range(project.id, seg.segment_id, 90.0, 200.0)


def test_remover_trecho_intervalo_invertido_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    with pytest.raises(PontoDeCorteInvalidoError):
        editor.remove_range(project.id, seg.segment_id, 60.0, 50.0)


# ---------------------------------------------------------------------------
# 5. juntar trechos
# ---------------------------------------------------------------------------


def test_juntar_dois_segmentos_adjacentes_contiguos(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    left, right = editor.split(project.id, seg.segment_id, 40.0)
    joined = editor.join(project.id, left.segment_id, right.segment_id)
    assert len(joined) == 1
    assert joined[0].start == 0.0
    assert joined[0].end == 100.0


def test_juntar_dois_segmentos_nao_adjacentes_na_ordem_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    a, bc = editor.split(project.id, seg.segment_id, 30.0)
    _, b, c = editor.split(project.id, bc.segment_id, 60.0)
    with pytest.raises(SegmentosNaoAdjacentesError):
        editor.join(project.id, a.segment_id, c.segment_id)


def test_juntar_segmentos_nao_contiguos_no_video_original_e_erro(editor, project):
    """Mesmo lado a lado na ORDEM da timeline, dois segmentos que não são
    contíguos no vídeo original (ex.: depois de um remove_range no meio)
    não podem ser mesclados -- o schema não representa um segmento com
    dois intervalos descontínuos (seção 1.3 da docstring do módulo)."""
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    left, right = editor.remove_range(project.id, seg.segment_id, 40.0, 60.0)
    with pytest.raises(SegmentosNaoAdjacentesError):
        editor.join(project.id, left.segment_id, right.segment_id)


def test_juntar_segmentos_com_velocidades_incompativeis_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    left, right = editor.split(project.id, seg.segment_id, 40.0)
    editor.set_speed(project.id, left.segment_id, 2.0)
    with pytest.raises(SegmentosNaoAdjacentesError):
        editor.join(project.id, left.segment_id, right.segment_id)


def test_juntar_segmento_com_ele_mesmo_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    with pytest.raises(SegmentosNaoAdjacentesError):
        editor.join(project.id, seg.segment_id, seg.segment_id)


# ---------------------------------------------------------------------------
# 6. reorganizar
# ---------------------------------------------------------------------------


def test_reorganizar_com_permutacao_valida(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    a, bc = editor.split(project.id, seg.segment_id, 30.0)
    _, b, c = editor.split(project.id, bc.segment_id, 60.0)
    ids = [s.segment_id for s in (a, b, c)]
    nova_ordem = [ids[2], ids[0], ids[1]]
    result = editor.reorder(project.id, nova_ordem)
    assert [s.segment_id for s in result] == nova_ordem


def test_reorganizar_com_id_inexistente_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    a, b = editor.split(project.id, seg.segment_id, 50.0)
    with pytest.raises(ReordenacaoInvalidaError):
        editor.reorder(project.id, [a.segment_id, "id-que-nao-existe"])


def test_reorganizar_com_id_duplicado_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    a, b = editor.split(project.id, seg.segment_id, 50.0)
    with pytest.raises(ReordenacaoInvalidaError):
        editor.reorder(project.id, [a.segment_id, a.segment_id])


def test_reorganizar_com_id_faltando_e_erro(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    a, b = editor.split(project.id, seg.segment_id, 50.0)
    with pytest.raises(ReordenacaoInvalidaError):
        editor.reorder(project.id, [a.segment_id])


# ---------------------------------------------------------------------------
# 7. alterar velocidade
# ---------------------------------------------------------------------------


def test_alterar_velocidade_valor_valido_persiste(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    result = editor.set_speed(project.id, seg.segment_id, 2.0)
    assert result[0].speed == 2.0
    reread = editor.get_timeline(project.id)
    assert reread[0].speed == 2.0


def test_alterar_velocidade_por_segmento_nao_afeta_os_demais(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    a, b = editor.split(project.id, seg.segment_id, 50.0)
    editor.set_speed(project.id, a.segment_id, 2.0)
    current = editor.get_timeline(project.id)
    by_id = {s.segment_id: s for s in current}
    assert by_id[a.segment_id].speed == 2.0
    assert by_id[b.segment_id].speed == 1.0


@pytest.mark.parametrize("valor", [0.0, -1.0, MIN_SPEED - 0.01, MAX_SPEED + 0.01])
def test_alterar_velocidade_fora_do_limite_documentado_e_erro(editor, project, valor):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    with pytest.raises(VelocidadeInvalidaError):
        editor.set_speed(project.id, seg.segment_id, valor)


def test_alterar_velocidade_nao_finita_e_erro_estruturado(editor, project):
    """inf/nan falham a validação numérica básica (não são um valor de
    velocidade "fora do limite", são um valor que nem é um número
    utilizável) -- ainda assim devem sempre levantar um erro ESTRUTURADO
    da hierarquia do módulo (nunca um ValueError/OverflowError cru
    escapando de um método público)."""
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    for valor in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(timeline_editor.TimelineEditorError):
            editor.set_speed(project.id, seg.segment_id, valor)


def test_alterar_velocidade_nos_limites_exatos_e_aceita(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    r1 = editor.set_speed(project.id, seg.segment_id, MIN_SPEED)
    assert r1[0].speed == MIN_SPEED
    r2 = editor.set_speed(project.id, seg.segment_id, MAX_SPEED)
    assert r2[0].speed == MAX_SPEED


# ---------------------------------------------------------------------------
# 8. sequência de operações em cadeia
# ---------------------------------------------------------------------------


def test_sequencia_de_operacoes_em_cadeia_preserva_consistencia(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    left, right = editor.split(project.id, seg.segment_id, 40.0)
    trimmed = editor.trim(project.id, right.segment_id, start=50.0)
    current = editor.get_timeline(project.id)
    right_now = current[1]
    reordered = editor.reorder(project.id, [right_now.segment_id, left.segment_id])
    assert [s.segment_id for s in reordered] == [right_now.segment_id, left.segment_id]
    final = editor.set_speed(project.id, left.segment_id, 1.5)
    by_id = {s.segment_id: s for s in final}
    assert by_id[left.segment_id].speed == 1.5
    assert by_id[right_now.segment_id].start == 50.0
    assert by_id[right_now.segment_id].end == 100.0
    # nenhum id perdido/duplicado ao longo da cadeia
    assert len(final) == 2
    assert len({s.segment_id for s in final}) == 2


# ---------------------------------------------------------------------------
# 9. isolamento de fingerprint entre categorias
# ---------------------------------------------------------------------------


def test_alterar_cuts_nunca_muda_fingerprint_de_outra_categoria_ja_definida(database, editor, project):
    manager = EditProjectManager(database)
    manager.set_category(project.id, TEMPLATE, {"template_id": "modelo-1"})
    before = manager.get_category(project.id, TEMPLATE)

    seg = editor.initialize_timeline(project.id, 100.0)[0]
    left, _right = editor.split(project.id, seg.segment_id, 40.0)
    editor.set_speed(project.id, left.segment_id, 2.0)

    after = manager.get_category(project.id, TEMPLATE)
    assert before.fingerprint == after.fingerprint
    assert before == after


def test_speed_nunca_e_usada_como_categoria_separada(database, editor, project):
    """Decisão documentada na seção 1.1 do módulo: velocidade vive DENTRO
    de 'cuts', nunca numa categoria 'speed' separada."""
    manager = EditProjectManager(database)
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    editor.set_speed(project.id, seg.segment_id, 2.0)
    assert manager.get_category(project.id, SPEED) is None
    assert CUTS in manager.list_categories(project.id)
    assert SPEED not in manager.list_categories(project.id)


# ---------------------------------------------------------------------------
# 10. integração read-only com o badge EDITED do Media Catalog
# ---------------------------------------------------------------------------


def test_gravar_cuts_faz_o_video_aparecer_como_edited_no_catalogo(database, editor, project, video):
    catalog = MediaCatalogService(database)
    antes = catalog.get_item(video.id)
    assert BADGE_EDITED not in antes.system_badges

    editor.initialize_timeline(project.id, 100.0)

    depois = catalog.get_item(video.id)
    assert BADGE_EDITED in depois.system_badges


# ---------------------------------------------------------------------------
# 11a. GATE 4 -- RESTART (reabrir com instâncias NOVAS, nunca reaproveitar
# objetos em memória)
# ---------------------------------------------------------------------------


def test_timeline_sobrevive_reabertura_com_instancias_totalmente_novas(app_paths, project):
    db_a = LocalDatabase(app_paths.database / "painel.db")
    db_a.initialize()
    editor_a = TimelineEditor(db_a)
    seg = editor_a.initialize_timeline(project.id, 100.0)[0]
    left, right = editor_a.split(project.id, seg.segment_id, 40.0)
    editor_a.set_speed(project.id, left.segment_id, 2.0)

    # instâncias NOVAS de LocalDatabase e TimelineEditor -- nunca os
    # objetos db_a/editor_a já usados (isso não provaria nada sobre
    # persistência real em disco).
    db_b = LocalDatabase(app_paths.database / "painel.db")
    db_b.initialize()
    editor_b = TimelineEditor(db_b)

    reread = editor_b.get_timeline(project.id)
    assert len(reread) == 2
    by_id = {s.segment_id: s for s in reread}
    assert by_id[left.segment_id].speed == 2.0
    assert by_id[left.segment_id].end == 40.0
    assert by_id[right.segment_id].start == 40.0
    assert by_id[right.segment_id].end == 100.0


# ---------------------------------------------------------------------------
# 11b. GATE 5 -- IDEMPOTÊNCIA (repetir a mesma operação não corrompe nem
# acumula efeito)
# ---------------------------------------------------------------------------


def test_repetir_o_mesmo_trim_sequencialmente_e_deterministico_e_nao_acumula(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    r1 = editor.trim(project.id, seg.segment_id, start=10.0, end=90.0)
    r2 = editor.trim(project.id, seg.segment_id, start=10.0, end=90.0)
    assert r1 == r2
    assert r2[0].start == 10.0
    assert r2[0].end == 90.0


def test_repetir_o_mesmo_set_speed_sequencialmente_e_deterministico(editor, project):
    seg = editor.initialize_timeline(project.id, 100.0)[0]
    r1 = editor.set_speed(project.id, seg.segment_id, 1.5)
    r2 = editor.set_speed(project.id, seg.segment_id, 1.5)
    assert r1 == r2 == editor.get_timeline(project.id)


def test_initialize_timeline_repetido_nunca_apaga_a_timeline_ja_existente(editor, project):
    """Chamar initialize_timeline duas vezes é rejeitado (já coberto
    acima) -- aqui confirmamos explicitamente que a REJEIÇÃO não corrompe
    nem apaga o estado que já existia (histórico original preservado,
    GATE 9/5)."""
    original = editor.initialize_timeline(project.id, 100.0)
    with pytest.raises(TimelineJaInicializadaError):
        editor.initialize_timeline(project.id, 999.0)
    assert editor.get_timeline(project.id) == original


# ---------------------------------------------------------------------------
# 11. GATE 6 -- concorrência real numa cadeia de operações via TimelineEditor
# ---------------------------------------------------------------------------


def test_duas_operacoes_concorrentes_em_segmentos_diferentes_nao_se_perdem(app_paths, database, project):
    """Duas threads reais, sincronizadas por Barrier, cada uma dando
    ``set_speed`` num segmento DIFERENTE do MESMO projeto, ao mesmo tempo.
    Sem ``update_category`` fechando a janela de leitura+decisão+escrita,
    uma das duas mudanças seria silenciosamente apagada."""
    editor_setup = TimelineEditor(database)
    seg = editor_setup.initialize_timeline(project.id, 100.0)[0]
    a, b = editor_setup.split(project.id, seg.segment_id, 50.0)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(segment_id: str, speed: float):
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_editor = TimelineEditor(db_instance)
        barrier.wait(timeout=5)
        try:
            local_editor.set_speed(project.id, segment_id, speed)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(a.segment_id, 2.0)),
        threading.Thread(target=worker, args=(b.segment_id, 0.5)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    final = TimelineEditor(database).get_timeline(project.id)
    by_id = {s.segment_id: s for s in final}
    assert by_id[a.segment_id].speed == 2.0, "mudanca concorrente perdida"
    assert by_id[b.segment_id].speed == 0.5, "mudanca concorrente perdida"


# ---------------------------------------------------------------------------
# 12. Garantias estruturais (AST) e schema
# ---------------------------------------------------------------------------


def _parse_timeline_editor_module() -> ast.AST:
    return ast.parse(Path(timeline_editor.__file__).read_text(encoding="utf-8"))


def test_timeline_editor_nao_importa_modulos_protegidos():
    tree = _parse_timeline_editor_module()
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
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for proibido in proibidos:
            assert proibido not in names, f"timeline_editor.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_timeline_editor_nao_chama_subprocess_ffmpeg_ffprobe():
    """AST, nunca grep textual ingenuo -- a propria docstring do modulo
    cita 'ffmpeg'/'ffprobe'/'subprocess' para explicar por que nao sao
    usados (mesmo problema ja enfrentado nos Prompts 24b/25/26/27/27b/27.5)."""
    tree = _parse_timeline_editor_module()
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


def test_timeline_editor_nunca_cria_job_artifact_publication_schedule_video():
    source = Path(timeline_editor.__file__).read_text(encoding="utf-8")
    for proibido in ("Job(", "Artifact(", "Publication(", "Schedule(", "Video("):
        assert proibido not in source


def test_timeline_editor_nao_abre_transacao_propria_nem_toca_storage_diretamente():
    """AST, não grep textual ingênuo: a docstring do módulo cita
    ``edit_state_json`` legitimamente para explicar o schema herdado que
    NÃO é tocado diretamente por este módulo (mesma armadilha já vista em
    Prompts anteriores) -- por isso a verificação real é sobre CHAMADAS
    (nós ast.Call/ast.Attribute), nunca sobre a string aparecer em
    algum lugar do arquivo (que incluiria comentários/docstrings)."""
    tree = _parse_timeline_editor_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            # nunca `<algo>.transaction()` chamado a partir deste módulo
            assert node.attr != "transaction"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # nunca `database.get(Project, ...)` / `database.save(...)`
            # chamados por este módulo -- toda leitura/escrita passa por
            # EditProjectManager (get_category/set_category/update_category)
            if node.func.attr in ("get", "save", "insert", "delete"):
                assert not (
                    isinstance(node.func.value, ast.Name) and node.func.value.id == "database"
                ), "timeline_editor.py não pode chamar métodos de LocalDatabase diretamente"


def test_segment_from_dict_rejeita_forma_invalida():
    with pytest.raises(SegmentoInvalidoError):
        Segment.from_dict("nao e um dict")
    with pytest.raises(SegmentoInvalidoError):
        Segment.from_dict({"segment_id": "x"})  # falta start/end
    with pytest.raises(SegmentoInvalidoError):
        Segment.from_dict({"segment_id": "", "start": 0, "end": 1})


def test_segment_from_dict_rejeita_source_segment_id_de_tipo_errado():
    """Achado no GATE ADVERSARIAL: source_segment_id não era validado --
    um valor não-string (ex.: vindo de dado corrompido/externo) era
    silenciosamente aceito, quebrando a garantia de que este campo é
    sempre str|None."""
    with pytest.raises(SegmentoInvalidoError):
        Segment.from_dict({"segment_id": "x", "start": 0, "end": 1, "source_segment_id": 12345})
    with pytest.raises(SegmentoInvalidoError):
        Segment.from_dict({"segment_id": "x", "start": 0, "end": 1, "source_segment_id": ""})
    # None e string não-vazia continuam aceitos normalmente
    ok1 = Segment.from_dict({"segment_id": "x", "start": 0, "end": 1, "source_segment_id": None})
    assert ok1.source_segment_id is None
    ok2 = Segment.from_dict({"segment_id": "x", "start": 0, "end": 1, "source_segment_id": "y"})
    assert ok2.source_segment_id == "y"


def test_nenhuma_migration_nova_criada_por_este_prompt():
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9
