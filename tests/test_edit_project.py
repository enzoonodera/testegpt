# -*- coding: utf-8 -*-
"""PROMPT 27 -- Modelo de Projeto de Edição (EditProjectManager): testes.

Cobre: granularidade por categoria (fingerprint/revisão independentes);
garantia central de não-invalidação cruzada entre categorias; concorrência
real (duas threads, categorias diferentes, mesmo Project, mesmo SQLite,
via Barrier -- GATE 6); restart com novas instâncias (GATE 4); validação
estruturada (nome de categoria/serialização); fingerprint agregado
determinístico; e garantias estruturais (AST) de ausência de
subprocess/ffmpeg/ffprobe, ausência de import de módulos protegidos/
Geração 1, ausência de outras entidades, e ausência de escrita em
``sources``.
"""
from __future__ import annotations

import ast
import dataclasses
import threading
from pathlib import Path

import pytest

import _sistema.edit_project as edit_project
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Job, Project, Publication, Schedule, SourceAsset, Video
from _sistema.edit_project import (
    AUDIO_SETTINGS,
    CAPTIONS,
    CROP,
    CUTS,
    METADATA_MODE,
    REFRAME,
    SPEED,
    TEMPLATE,
    TEXT_LAYERS,
    CategoriaInvalidaError,
    CategoryState,
    DadosNaoSerializaveisError,
    EditProjectManager,
    ProjectNaoEncontradoError,
)
from _sistema.storage.database import LocalDatabase


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_source_context.py/test_media_probe.py)
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
def project(database):
    proj = Project(name="Projeto de teste")
    database.insert(proj)
    return proj


# ---------------------------------------------------------------------------
# 1. Vocabulário -- nove constantes literais do roadmap, extensível
# ---------------------------------------------------------------------------


def test_nove_constantes_literais_do_roadmap():
    assert CUTS == "cuts"
    assert SPEED == "speed"
    assert CROP == "crop"
    assert REFRAME == "reframe"
    assert AUDIO_SETTINGS == "audio_settings"
    assert CAPTIONS == "captions"
    assert TEMPLATE == "template"
    assert TEXT_LAYERS == "text_layers"
    assert METADATA_MODE == "metadata_mode"


def test_categoria_nao_prevista_tambem_e_aceita(manager, project):
    """O conjunto de categorias é extensível (item 1.2) -- qualquer string
    não vazia, inclusive um nome nunca citado no roadmap, deve funcionar
    exatamente como as nove constantes."""
    resultado = manager.set_category(project.id, "watermark_customizada_futura", {"opacidade": 0.5})
    assert resultado.category == "watermark_customizada_futura"
    assert manager.list_categories(project.id) == ("watermark_customizada_futura",)


# ---------------------------------------------------------------------------
# 2. Definir categoria nova -- fingerprint/revisão corretos, sobrevive a restart
# ---------------------------------------------------------------------------


def test_definir_categoria_nova_calcula_fingerprint_e_revisao(manager, project):
    resultado = manager.set_category(project.id, CUTS, {"in": 0, "out": 10})
    assert isinstance(resultado, CategoryState)
    assert resultado.category == CUTS
    assert resultado.revision == 1
    assert isinstance(resultado.fingerprint, str) and len(resultado.fingerprint) == 64  # sha256 hex
    lido = manager.get_category(project.id, CUTS)
    assert lido == resultado


def test_restart_com_novas_instancias_preserva_categoria(app_paths):
    """GATE 4: reabre com NOVAS instâncias de LocalDatabase/EditProjectManager
    -- nunca reutiliza objetos em memória."""
    db1 = LocalDatabase(app_paths.database / "painel.db")
    db1.initialize()
    proj = Project(name="restart")
    db1.insert(proj)
    mgr1 = EditProjectManager(db1)
    gravado = mgr1.set_category(proj.id, CAPTIONS, {"idioma": "pt-BR", "texto": "Olá"})

    db2 = LocalDatabase(app_paths.database / "painel.db")
    mgr2 = EditProjectManager(db2)
    lido = mgr2.get_category(proj.id, CAPTIONS)
    assert lido == gravado


# ---------------------------------------------------------------------------
# 3. Garantia central -- alterar uma categoria nunca muda outra (bit-a-bit)
# ---------------------------------------------------------------------------


def test_atualizar_uma_categoria_nao_muda_fingerprint_revisao_de_outras(manager, project):
    cuts_v1 = manager.set_category(project.id, CUTS, {"in": 0, "out": 10})
    crop_v1 = manager.set_category(project.id, CROP, {"x": 0, "y": 0, "w": 1080, "h": 1920})
    speed_v1 = manager.set_category(project.id, SPEED, {"fator": 1.0})

    cuts_v2 = manager.set_category(project.id, CUTS, {"in": 0, "out": 25})

    assert cuts_v2.fingerprint != cuts_v1.fingerprint
    assert cuts_v2.revision == cuts_v1.revision + 1

    crop_depois = manager.get_category(project.id, CROP)
    speed_depois = manager.get_category(project.id, SPEED)

    assert crop_depois.fingerprint == crop_v1.fingerprint
    assert crop_depois.revision == crop_v1.revision
    assert crop_depois.updated_at == crop_v1.updated_at
    assert crop_depois.data == crop_v1.data

    assert speed_depois.fingerprint == speed_v1.fingerprint
    assert speed_depois.revision == speed_v1.revision
    assert speed_depois.updated_at == speed_v1.updated_at


# ---------------------------------------------------------------------------
# 4. Remover categoria -- some da listagem, outras inalteradas
# ---------------------------------------------------------------------------


def test_remover_categoria_some_da_listagem_outras_inalteradas(manager, project):
    manager.set_category(project.id, CUTS, {"in": 0, "out": 10})
    crop_antes = manager.set_category(project.id, CROP, {"x": 0})

    removido = manager.remove_category(project.id, CUTS)
    assert removido is True
    assert manager.list_categories(project.id) == (CROP,)
    assert manager.get_category(project.id, CUTS) is None

    crop_depois = manager.get_category(project.id, CROP)
    assert crop_depois == crop_antes


def test_remover_categoria_inexistente_e_idempotente_nao_lanca(manager, project):
    manager.set_category(project.id, CUTS, {"in": 0, "out": 10})
    removido = manager.remove_category(project.id, "categoria_nunca_definida")
    assert removido is False
    assert manager.list_categories(project.id) == (CUTS,)


# ---------------------------------------------------------------------------
# 5. Mesmos dados -> mesmo fingerprint; revisão NÃO avança (decisão documentada)
# ---------------------------------------------------------------------------


def test_mesmos_dados_mesma_categoria_fingerprint_deterministico_revisao_nao_avanca(manager, project):
    primeira = manager.set_category(project.id, TEMPLATE, {"id": "vertical_01"})
    segunda = manager.set_category(project.id, TEMPLATE, {"id": "vertical_01"})

    assert segunda.fingerprint == primeira.fingerprint
    # Decisão documentada na docstring de EditProjectManager.set_category:
    # reenvio idempotente do mesmo estado é um NO-OP -- a revisão não avança,
    # para que a revisão continue sendo um sinal real de mudança.
    assert segunda.revision == primeira.revision
    assert segunda.updated_at == primeira.updated_at


def test_mesmos_dados_em_ordem_de_chaves_diferente_ainda_e_no_op(manager, project):
    """O fingerprint é calculado sobre o JSON CANÔNICO (chaves ordenadas) --
    a mesma estrutura lógica com chaves em outra ordem de construção do dict
    Python produz o mesmo fingerprint e continua sendo tratada como NO-OP."""
    primeira = manager.set_category(project.id, CROP, {"x": 1, "y": 2, "w": 3, "h": 4})
    segunda = manager.set_category(project.id, CROP, {"h": 4, "w": 3, "y": 2, "x": 1})
    assert segunda.fingerprint == primeira.fingerprint
    assert segunda.revision == primeira.revision


# ---------------------------------------------------------------------------
# 6. Validação estruturada -- nunca uma exceção crua de serialização
# ---------------------------------------------------------------------------


def test_dados_nao_serializaveis_gera_erro_estruturado(manager, project):
    class ObjetoArbitrario:
        pass

    with pytest.raises(DadosNaoSerializaveisError) as excinfo:
        manager.set_category(project.id, CUTS, {"obj": ObjetoArbitrario()})
    assert excinfo.value.code == "DADOS_NAO_SERIALIZAVEIS"


def test_dados_com_set_python_nao_serializavel_gera_erro_estruturado(manager, project):
    with pytest.raises(DadosNaoSerializaveisError):
        manager.set_category(project.id, CUTS, {"valores": {1, 2, 3}})


@pytest.mark.parametrize("nome_invalido", ["", "   ", None, 123, [], {}])
def test_nome_categoria_invalido_gera_erro_estruturado(manager, project, nome_invalido):
    with pytest.raises(CategoriaInvalidaError) as excinfo:
        manager.set_category(project.id, nome_invalido, {"a": 1})
    assert excinfo.value.code == "CATEGORIA_INVALIDA"


def test_project_inexistente_gera_erro_estruturado_em_todas_as_operacoes(manager):
    projeto_fantasma = Project(name="nunca inserido").id
    with pytest.raises(ProjectNaoEncontradoError):
        manager.set_category(projeto_fantasma, CUTS, {"in": 0})
    with pytest.raises(ProjectNaoEncontradoError):
        manager.get_category(projeto_fantasma, CUTS)
    with pytest.raises(ProjectNaoEncontradoError):
        manager.list_categories(projeto_fantasma)
    with pytest.raises(ProjectNaoEncontradoError):
        manager.remove_category(projeto_fantasma, CUTS)
    with pytest.raises(ProjectNaoEncontradoError):
        manager.aggregate_fingerprint(projeto_fantasma)


def test_construcao_rejeita_database_invalido():
    with pytest.raises(TypeError):
        EditProjectManager(database="nao-e-um-LocalDatabase")


def test_category_state_e_dataclass_imutavel(manager, project):
    resultado = manager.set_category(project.id, CUTS, {"in": 0, "out": 10})
    assert dataclasses.is_dataclass(resultado)
    with pytest.raises(dataclasses.FrozenInstanceError):
        resultado.revision = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 7. Fingerprint agregado -- muda com qualquer categoria, determinístico
# ---------------------------------------------------------------------------


def test_fingerprint_agregado_muda_quando_qualquer_categoria_muda(manager, project):
    agregado_0 = manager.aggregate_fingerprint(project.id)
    manager.set_category(project.id, CUTS, {"in": 0, "out": 10})
    agregado_1 = manager.aggregate_fingerprint(project.id)
    manager.set_category(project.id, CROP, {"x": 0})
    agregado_2 = manager.aggregate_fingerprint(project.id)
    manager.set_category(project.id, CUTS, {"in": 0, "out": 99})
    agregado_3 = manager.aggregate_fingerprint(project.id)

    assert len({agregado_0, agregado_1, agregado_2, agregado_3}) == 4


def test_fingerprint_agregado_independente_da_ordem_de_insercao(database):
    proj_a = Project(name="ordem A")
    proj_b = Project(name="ordem B")
    database.insert(proj_a)
    database.insert(proj_b)
    mgr = EditProjectManager(database)

    mgr.set_category(proj_a.id, CUTS, {"in": 0, "out": 10})
    mgr.set_category(proj_a.id, CROP, {"x": 1})
    mgr.set_category(proj_a.id, SPEED, {"fator": 1.5})

    mgr.set_category(proj_b.id, SPEED, {"fator": 1.5})
    mgr.set_category(proj_b.id, CUTS, {"in": 0, "out": 10})
    mgr.set_category(proj_b.id, CROP, {"x": 1})

    assert mgr.aggregate_fingerprint(proj_a.id) == mgr.aggregate_fingerprint(proj_b.id)


def test_fingerprint_agregado_projeto_sem_categorias_e_deterministico(database):
    proj_a = Project(name="vazio A")
    proj_b = Project(name="vazio B")
    database.insert(proj_a)
    database.insert(proj_b)
    mgr = EditProjectManager(database)
    assert mgr.aggregate_fingerprint(proj_a.id) == mgr.aggregate_fingerprint(proj_b.id)


# ---------------------------------------------------------------------------
# 8. Project.revision -- contador grosso, separado das revisões por categoria
# ---------------------------------------------------------------------------


def test_project_revision_avanca_a_cada_mutacao_real_mas_nao_em_no_op(database):
    proj = Project(name="revisao")
    database.insert(proj)
    mgr = EditProjectManager(database)

    assert database.get(Project, proj.id).revision == 1

    mgr.set_category(proj.id, CUTS, {"in": 0, "out": 10})
    assert database.get(Project, proj.id).revision == 2

    mgr.set_category(proj.id, CROP, {"x": 0})
    assert database.get(Project, proj.id).revision == 3

    # NO-OP (mesmos dados) -- Project.revision não avança.
    mgr.set_category(proj.id, CROP, {"x": 0})
    assert database.get(Project, proj.id).revision == 3

    mgr.remove_category(proj.id, CUTS)
    assert database.get(Project, proj.id).revision == 4

    # Remover categoria já ausente -- idempotente, não avança.
    mgr.remove_category(proj.id, CUTS)
    assert database.get(Project, proj.id).revision == 4


# ---------------------------------------------------------------------------
# 9. GATE 6 -- concorrência real, determinística via Barrier
# ---------------------------------------------------------------------------


def test_duas_threads_atualizando_categorias_diferentes_do_mesmo_project_nenhuma_perdida(app_paths, database):
    """Duas threads reais, sincronizadas por Barrier (nunca probabilístico),
    cada uma chamando ``set_category`` para uma categoria DIFERENTE do MESMO
    Project ao mesmo tempo, contra o mesmo arquivo SQLite. Este teste
    genuinamente FALHARIA com um read-modify-write ingênuo (ler o Project
    inteiro fora de uma transaction, decidir em memória, escrever de volta
    depois) -- a resolução via BEGIN IMMEDIATE (seção 0.4 do módulo) impede
    a corrida de 'lost update'."""
    proj = Project(name="concorrencia")
    database.insert(proj)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(categoria: str, dados: dict) -> None:
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_manager = EditProjectManager(db_instance)
        barrier.wait(timeout=5)
        try:
            local_manager.set_category(proj.id, categoria, dados)
        except BaseException as exc:  # pragma: no cover - só se algo quebrar
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(CUTS, {"in": 0, "out": 10}))
    t2 = threading.Thread(target=worker, args=(CROP, {"x": 0, "y": 0}))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert errors == []

    mgr = EditProjectManager(database)
    assert set(mgr.list_categories(proj.id)) == {CUTS, CROP}
    cuts_final = mgr.get_category(proj.id, CUTS)
    crop_final = mgr.get_category(proj.id, CROP)
    assert cuts_final is not None and cuts_final.data == {"in": 0, "out": 10}
    assert crop_final is not None and crop_final.data == {"x": 0, "y": 0}


def test_muitas_threads_categorias_distintas_nenhuma_perdida(app_paths, database):
    """Extensão adversarial: mais de duas threads (8), cada uma com sua
    própria categoria, todas sincronizadas para colidir na mesma janela --
    todas as 8 categorias devem sobreviver."""
    proj = Project(name="concorrencia_8")
    database.insert(proj)

    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(indice: int) -> None:
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_manager = EditProjectManager(db_instance)
        barrier.wait(timeout=5)
        try:
            local_manager.set_category(proj.id, f"categoria_{indice}", {"valor": indice})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    mgr = EditProjectManager(database)
    categorias = set(mgr.list_categories(proj.id))
    assert categorias == {f"categoria_{i}" for i in range(n)}
    for i in range(n):
        estado = mgr.get_category(proj.id, f"categoria_{i}")
        assert estado is not None
        assert estado.data == {"valor": i}


# ---------------------------------------------------------------------------
# 10. Garantias estruturais (AST) -- escopo, isolamento, ausência de outras entidades
# ---------------------------------------------------------------------------


def _parse_edit_project_module() -> ast.AST:
    return ast.parse(Path(edit_project.__file__).read_text(encoding="utf-8"))


def test_edit_project_nao_importa_modulos_protegidos():
    tree = _parse_edit_project_module()
    proibidos = (
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "job_state_machine",
        "source_import",
        "source_context",
        "media_probe",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for termo in proibidos:
            assert termo not in names.lower(), f"import proibido encontrado: {names!r}"


def test_edit_project_nao_importa_nada_de_geracao_1():
    tree = _parse_edit_project_module()
    proibidos_geracao_1 = (
        "agendar_youtube",
        "agendar_tiktok",
        "limpar_metadados_oficial",
        "gerar_textos",
        "painel_oficial",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for termo in proibidos_geracao_1:
            assert termo not in names.lower()


def test_edit_project_nunca_chama_subprocess_ffmpeg_ffprobe():
    """Este módulo só grava decisões -- nunca processa mídia (item NÃO
    FAZER). Verificado via AST: nenhuma chamada de função/atributo cujo
    nome referencie subprocess/ffmpeg/ffprobe, e nenhum import de
    ``subprocess``."""
    tree = _parse_edit_project_module()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [module] + [alias.name for alias in node.names]
            for name in names:
                assert "subprocess" not in name.lower()
        if isinstance(node, ast.Call):
            func = node.func
            func_name = ""
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            lowered = func_name.lower()
            for termo in ("subprocess", "ffmpeg", "ffprobe", "popen", "run"):
                if termo == "run":
                    # "run" isolado é comum demais (ex. algum futuro helper) --
                    # só reprovamos combinações claramente ligadas a processo
                    # externo, cobertas pelos outros termos.
                    continue
                assert termo not in lowered, f"chamada suspeita de processo externo: {func_name!r}"


def test_nenhum_job_artifact_publication_schedule_video_criado():
    source = Path(edit_project.__file__).read_text(encoding="utf-8")
    for termo in ("Job(", "Artifact(", "Publication(", "Schedule(", "Video("):
        assert termo not in source


def test_edit_project_nunca_referencia_source_asset_no_codigo():
    """``SourceAsset``/o arquivo original nunca é tocado por este módulo
    (item NÃO FAZER) -- confirmado por ausência de referência no código-fonte
    (não na docstring, que só o cita em prosa)."""
    tree = _parse_edit_project_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "SourceAsset":
            raise AssertionError("referência a SourceAsset encontrada no código de edit_project.py")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [module] + [alias.name for alias in node.names]
            for name in names:
                assert "sourceasset" not in name.lower().replace("_", "")


def test_edit_project_so_toca_tabela_projects_nunca_sources(database):
    """Confirmação funcional (não só AST): inserir um SourceAsset, operar
    livremente via EditProjectManager sobre um Project, e confirmar que a
    linha de ``sources`` nunca foi tocada (mesmo espírito do teste
    equivalente de ``media_probe.py`` para ``storage.database``, adaptado
    aqui para confirmar que só ``projects`` é gravada)."""
    asset = SourceAsset(source_uri="/tmp/original_intocado.mp4")
    database.insert(asset)
    asset_antes = database.get(SourceAsset, asset.id)

    proj = Project(name="isolamento")
    database.insert(proj)
    mgr = EditProjectManager(database)
    mgr.set_category(proj.id, CUTS, {"in": 0, "out": 5})
    mgr.set_category(proj.id, CROP, {"x": 1})
    mgr.remove_category(proj.id, CUTS)

    asset_depois = database.get(SourceAsset, asset.id)
    assert asset_depois == asset_antes
    assert database.list(SourceAsset) == [asset_antes]


def test_nenhuma_outra_entidade_criada_no_banco(manager, project, database):
    manager.set_category(project.id, CUTS, {"in": 0, "out": 10})
    manager.set_category(project.id, CROP, {"x": 0})
    manager.remove_category(project.id, CUTS)
    assert database.list(Job) == []
    assert database.list(Publication) == []
    assert database.list(Schedule) == []
    assert database.list(Video) == []
    assert database.list(SourceAsset) == []


# ---------------------------------------------------------------------------
# update_category (PROMPT 28) -- leitura+decisão+escrita atômicas
# ---------------------------------------------------------------------------


def test_update_category_cria_categoria_nova_quando_ausente(manager, project):
    state = manager.update_category(project.id, CUTS, lambda current: {"visto": current, "novo": True})
    assert state.data == {"visto": None, "novo": True}
    assert state.revision == 1
    assert manager.get_category(project.id, CUTS).data == {"visto": None, "novo": True}


def test_update_category_recebe_o_data_atual_e_pode_derivar_dele(manager, project):
    manager.set_category(project.id, CUTS, {"contador": 1})
    state = manager.update_category(project.id, CUTS, lambda current: {"contador": current["contador"] + 1})
    assert state.data == {"contador": 2}
    state2 = manager.update_category(project.id, CUTS, lambda current: {"contador": current["contador"] + 1})
    assert state2.data == {"contador": 3}


def test_update_category_no_op_deterministico_quando_resultado_identico(manager, project):
    manager.set_category(project.id, CUTS, {"x": 1})
    before = manager.get_category(project.id, CUTS)
    state = manager.update_category(project.id, CUTS, lambda current: {"x": 1})
    assert state.revision == before.revision
    assert state.fingerprint == before.fingerprint


def test_update_category_nunca_toca_outras_categorias(manager, project):
    manager.set_category(project.id, CROP, {"x": 0})
    before = manager.get_category(project.id, CROP)
    manager.update_category(project.id, CUTS, lambda current: {"novo": True})
    after = manager.get_category(project.id, CROP)
    assert before == after


def test_update_category_projeto_inexistente_levanta_erro(manager):
    import uuid

    with pytest.raises(ProjectNaoEncontradoError):
        manager.update_category(str(uuid.uuid4()), CUTS, lambda current: {})


def test_update_category_mutator_que_levanta_excecao_nao_escreve_nada(manager, project):
    manager.set_category(project.id, CUTS, {"estavel": True})
    before = manager.get_category(project.id, CUTS)

    class Sinalizador(RuntimeError):
        pass

    def mutator_com_erro(current):
        raise Sinalizador("erro deliberado do chamador")

    with pytest.raises(Sinalizador):
        manager.update_category(project.id, CUTS, mutator_com_erro)

    after = manager.get_category(project.id, CUTS)
    assert after == before


def test_update_category_duas_threads_concorrentes_nenhuma_perdida(app_paths, database, project):
    """Prova direta da razão de existir de ``update_category`` (ver sua
    docstring): duas threads fazem 'ler o valor atual de um contador em
    ``cuts`` e incrementar' -- exatamente o padrão que, feito com
    ``get_category()``+``set_category()`` como duas chamadas separadas,
    perderia um incremento sob corrida real. Sincronizado por
    ``threading.Barrier`` (determinístico, não probabilístico -- GATE 6)."""
    mgr = EditProjectManager(database)
    mgr.set_category(project.id, CUTS, {"contador": 0})

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_mgr = EditProjectManager(db_instance)
        barrier.wait(timeout=5)
        try:
            local_mgr.update_category(project.id, CUTS, lambda current: {"contador": current["contador"] + 1})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    final = mgr.get_category(project.id, CUTS)
    assert final.data == {"contador": 2}, "incremento perdido -- update_category nao fechou a corrida"


def test_update_category_muitas_threads_concorrentes_nenhuma_perdida(app_paths, database, project):
    mgr = EditProjectManager(database)
    mgr.set_category(project.id, CUTS, {"contador": 0})

    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_mgr = EditProjectManager(db_instance)
        barrier.wait(timeout=5)
        try:
            local_mgr.update_category(project.id, CUTS, lambda current: {"contador": current["contador"] + 1})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    final = mgr.get_category(project.id, CUTS)
    assert final.data == {"contador": n}


def test_edit_project_nao_cria_migration_nova():
    """Confirma (por inspeção do módulo de migrations, congelado) que
    nenhuma migration nova foi adicionada para este Prompt -- a coluna
    ``edit_state_json`` já existia desde m001. O valor esperado aqui
    acompanha ``LATEST_SCHEMA_VERSION`` de quando este teste foi escrito
    (8); PROMPT 27.5 (Media Catalog Backend) adicionou legitimamente
    ``m009_video_declarations`` depois, bumping para 9 -- atualizado
    conscientemente nesta mesma rodada (nunca uma alteração silenciosa)."""
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9
