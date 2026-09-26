# -*- coding: utf-8 -*-
"""PROMPT 27B -- Promoção de SourceAsset para Video: testes.

Cobre: promoção correta de um SourceAsset válido; idempotência sob
chamadas sequenciais e sob concorrência real (Barrier); leitura de
vídeos promovidos; validação estruturada (SourceAsset inexistente,
source_asset_id inválido); garantias estruturais (AST) de escopo
(nenhuma outra entidade, nenhum import de módulos protegidos/Geração 1,
promoção nunca automática nos importadores); e confirmação de que o
SourceAsset original nunca é modificado.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

import _sistema.video_promotion as video_promotion
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.domain import Job, Project, Publication, Schedule, SourceAsset, Video
from _sistema.source_import import FolderImporter, LocalFileImporter, UrlImporter
from _sistema.storage.database import LocalDatabase
from _sistema.video_promotion import (
    SourceAssetIdInvalidoError,
    SourceAssetNaoEncontradoError,
    VideoPromotionService,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão dos Prompts 25/26/27)
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
def service(database):
    return VideoPromotionService(database)


@pytest.fixture
def source_asset(database):
    asset = SourceAsset(source_uri="/tmp/original.mp4", original_name="original.mp4", size_bytes=1024)
    database.insert(asset)
    return asset


# ---------------------------------------------------------------------------
# 0.1 -- confirmação de que só este módulo e legacy_migration.py criam Video
# ---------------------------------------------------------------------------


def test_apenas_video_promotion_e_legacy_migration_criam_video_no_projeto():
    """Confirmação viva (não só documentada em prosa) do achado da seção
    0.1 -- nenhum outro módulo de ``_sistema/`` cria ``Video(``, exceto
    ``domain/models.py`` (definição), ``video_promotion.py`` (este módulo)
    e ``storage/legacy_migration.py`` (migração de dados antigos).

    PROMPT 48 acrescentou conscientemente ``smart_clip_selection_screen.py``:
    cada corte aprovado vira um Video NOVO da mesma origem, o que o
    contrato 1:1 de ``promote()`` não permite (e não deve permitir). O
    contrato deste módulo continua intacto -- ver seção 5 da docstring de
    ``smart_clip_selection_screen.py``."""
    sistema_dir = Path(video_promotion.__file__).parent
    permitidos = {
        "models.py", "video_promotion.py", "legacy_migration.py", "test_video_promotion.py",
        "smart_clip_selection_screen.py",
    }
    for py_file in sistema_dir.rglob("*.py"):
        if py_file.name in permitidos:
            continue
        if "__pycache__" in py_file.parts:
            continue
        source = py_file.read_text(encoding="utf-8")
        assert "Video(" not in source, f"Video( encontrado fora do esperado em {py_file}"


# ---------------------------------------------------------------------------
# 1. Promoção básica
# ---------------------------------------------------------------------------


def test_promover_source_asset_valido_cria_video_corretamente(service, source_asset):
    video = service.promote(source_asset.id)
    assert isinstance(video, Video)
    assert video.source_asset_id == source_asset.id
    assert video.name == "original.mp4"
    assert video.status == "ACTIVE"


def test_promover_deriva_nome_de_local_path_quando_sem_original_name(service, database):
    asset = SourceAsset(source_uri="/tmp/x.mp4", local_path="/pasta/qualquer/clipe_bruto.mp4")
    database.insert(asset)
    video = service.promote(asset.id)
    assert video.name == "clipe_bruto.mp4"


def test_promover_deriva_nome_de_source_uri_quando_sem_nome_nem_local_path(service, database):
    asset = SourceAsset(source_uri="https://www.youtube.com/watch?v=abc123")
    database.insert(asset)
    video = service.promote(asset.id)
    assert video.name == "https://www.youtube.com/watch?v=abc123"


def test_promover_usa_fallback_generico_quando_nada_disponivel(service, database):
    asset = SourceAsset(source_uri="")
    database.insert(asset)
    video = service.promote(asset.id)
    assert video.name.startswith("video-sem-nome-")
    assert asset.id[:8] in video.name


def test_promocao_sobrevive_a_restart(app_paths):
    """GATE 4: reabre com NOVAS instâncias de LocalDatabase/
    VideoPromotionService -- nunca reutiliza objetos em memória."""
    db1 = LocalDatabase(app_paths.database / "painel.db")
    db1.initialize()
    asset = SourceAsset(source_uri="/tmp/restart.mp4", original_name="restart.mp4")
    db1.insert(asset)
    svc1 = VideoPromotionService(db1)
    video1 = svc1.promote(asset.id)

    db2 = LocalDatabase(app_paths.database / "painel.db")
    svc2 = VideoPromotionService(db2)
    videos = svc2.list_videos_for_source(asset.id)
    assert len(videos) == 1
    assert videos[0].id == video1.id
    assert videos[0].name == video1.name


# ---------------------------------------------------------------------------
# 2. Erros estruturados
# ---------------------------------------------------------------------------


def test_promover_source_asset_inexistente_gera_erro_estruturado(service):
    fantasma = SourceAsset(source_uri="/tmp/nao_inserido.mp4").id
    with pytest.raises(SourceAssetNaoEncontradoError) as excinfo:
        service.promote(fantasma)
    assert excinfo.value.code == "SOURCE_ASSET_NAO_ENCONTRADO"


@pytest.mark.parametrize("valor_invalido", ["", "nao-e-um-uuid", "123", None, 42, []])
def test_promover_source_asset_id_invalido_gera_erro_estruturado(service, valor_invalido):
    with pytest.raises(SourceAssetIdInvalidoError) as excinfo:
        service.promote(valor_invalido)
    assert excinfo.value.code == "SOURCE_ASSET_ID_INVALIDO"


def test_list_videos_for_source_id_invalido_gera_erro_estruturado(service):
    with pytest.raises(SourceAssetIdInvalidoError):
        service.list_videos_for_source("nao-e-um-uuid")


def test_construcao_rejeita_database_invalido():
    with pytest.raises(TypeError):
        VideoPromotionService(database="nao-e-um-LocalDatabase")


# ---------------------------------------------------------------------------
# 3. Idempotência sequencial (decisão da seção 1.3)
# ---------------------------------------------------------------------------


def test_promover_o_mesmo_source_asset_duas_vezes_sequencialmente_e_idempotente(service, source_asset):
    v1 = service.promote(source_asset.id)
    v2 = service.promote(source_asset.id)
    assert v1.id == v2.id
    assert v1 == v2


def test_promover_duas_vezes_nao_cria_segundo_video_no_banco(service, database, source_asset):
    service.promote(source_asset.id)
    service.promote(source_asset.id)
    videos = database.list(Video)
    assert len(videos) == 1


def test_promover_origens_diferentes_cria_videos_distintos(service, database):
    asset_a = SourceAsset(source_uri="/tmp/a.mp4", original_name="a.mp4")
    asset_b = SourceAsset(source_uri="/tmp/b.mp4", original_name="b.mp4")
    database.insert(asset_a)
    database.insert(asset_b)
    video_a = service.promote(asset_a.id)
    video_b = service.promote(asset_b.id)
    assert video_a.id != video_b.id
    assert {v.id for v in database.list(Video)} == {video_a.id, video_b.id}


# ---------------------------------------------------------------------------
# 4. list_videos_for_source
# ---------------------------------------------------------------------------


def test_list_videos_for_source_vazio_antes_de_promover(service, source_asset):
    assert service.list_videos_for_source(source_asset.id) == ()


def test_list_videos_for_source_um_video_apos_promover(service, source_asset):
    video = service.promote(source_asset.id)
    videos = service.list_videos_for_source(source_asset.id)
    assert videos == (video,)


def test_list_videos_for_source_reflete_multiplos_videos_criados_fora_do_servico(service, database, source_asset):
    """Vídeos criados por outro caminho (ex.: legacy_migration.py, fora
    deste serviço) devem continuar aparecendo em list_videos_for_source --
    a idempotência é uma garantia de ``promote()``, não uma restrição de
    leitura. ``promote()`` chamado depois deve devolver o mais antigo,
    nunca criar um terceiro."""
    video_legado_1 = Video(source_asset_id=source_asset.id, name="legado_1.mp4")
    video_legado_2 = Video(source_asset_id=source_asset.id, name="legado_2.mp4")
    database.insert(video_legado_1)
    database.insert(video_legado_2)

    videos = service.list_videos_for_source(source_asset.id)
    assert {v.id for v in videos} == {video_legado_1.id, video_legado_2.id}

    promovido = service.promote(source_asset.id)
    assert promovido.id in {video_legado_1.id, video_legado_2.id}
    # Nenhum terceiro Video foi criado.
    assert len(database.list(Video)) == 2


# ---------------------------------------------------------------------------
# 5. GATE 6 -- concorrência real, determinística via Barrier
# ---------------------------------------------------------------------------


def test_duas_chamadas_concorrentes_promovendo_o_mesmo_source_asset_nunca_duplicam(app_paths, database, source_asset):
    """Duas threads reais, sincronizadas por Barrier (nunca
    probabilístico), chamando ``promote()`` para o MESMO
    ``source_asset_id`` ao mesmo tempo contra o mesmo SQLite. Este teste
    genuinamente FALHARIA se a idempotência dependesse de um
    read-modify-write fora de uma transação atômica -- a resolução via
    BEGIN IMMEDIATE (seção 1.3 do módulo) impede a criação de um segundo
    Video por corrida."""
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    results: list[Video] = []

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_service = VideoPromotionService(db_instance)
        barrier.wait(timeout=5)
        try:
            results.append(local_service.promote(source_asset.id))
        except BaseException as exc:  # pragma: no cover - só se algo quebrar
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert results[0].id == results[1].id

    service = VideoPromotionService(database)
    videos = service.list_videos_for_source(source_asset.id)
    assert len(videos) == 1


def test_muitas_chamadas_concorrentes_mesmo_source_asset_apenas_um_video(app_paths, database, source_asset):
    """Extensão adversarial: 8 threads simultâneas promovendo a MESMA
    origem -- ainda assim apenas um Video deve existir ao final."""
    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    results: list[Video] = []
    lock = threading.Lock()

    def worker():
        db_instance = LocalDatabase(app_paths.database / "painel.db")
        local_service = VideoPromotionService(db_instance)
        barrier.wait(timeout=5)
        try:
            video = local_service.promote(source_asset.id)
            with lock:
                results.append(video)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(results) == n
    assert len({video.id for video in results}) == 1

    service = VideoPromotionService(database)
    assert len(service.list_videos_for_source(source_asset.id)) == 1


# ---------------------------------------------------------------------------
# 6. SourceAsset original nunca é modificado
# ---------------------------------------------------------------------------


def test_promocao_nunca_modifica_o_source_asset_original(service, database, source_asset):
    antes = database.get(SourceAsset, source_asset.id)
    service.promote(source_asset.id)
    service.promote(source_asset.id)  # segunda chamada idempotente também
    depois = database.get(SourceAsset, source_asset.id)
    assert depois == antes
    assert database.list(SourceAsset) == [antes]


# ---------------------------------------------------------------------------
# 7. Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def _parse_video_promotion_module() -> ast.AST:
    return ast.parse(Path(video_promotion.__file__).read_text(encoding="utf-8"))


def test_video_promotion_nao_importa_modulos_protegidos():
    tree = _parse_video_promotion_module()
    proibidos = (
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "job_state_machine",
        "source_import",
        "source_context",
        "media_probe",
        "edit_project",
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


def test_video_promotion_nao_importa_nada_de_geracao_1():
    tree = _parse_video_promotion_module()
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


def test_nenhum_job_artifact_publication_schedule_project_criado():
    source = Path(video_promotion.__file__).read_text(encoding="utf-8")
    for termo in ("Job(", "Artifact(", "Publication(", "Schedule(", "Project("):
        assert termo not in source


def test_nenhuma_outra_entidade_criada_no_banco(service, database, source_asset):
    service.promote(source_asset.id)
    assert database.list(Job) == []
    assert database.list(Publication) == []
    assert database.list(Schedule) == []
    assert database.list(Project) == []


def test_video_promotion_nao_cria_migration_nova():
    """Valor esperado acompanha ``LATEST_SCHEMA_VERSION`` de quando este
    teste foi escrito (8); PROMPT 27.5 adicionou legitimamente
    ``m009_video_declarations`` depois, bumping para 9 -- atualizado
    conscientemente nesta mesma rodada."""
    from _sistema.storage.migrations import LATEST_SCHEMA_VERSION

    assert LATEST_SCHEMA_VERSION == 9


# ---------------------------------------------------------------------------
# 8. Promoção continua explícita -- nunca automática dentro dos importadores
# ---------------------------------------------------------------------------


def test_promocao_nao_e_chamada_automaticamente_pelos_importadores():
    """Mesma lição já aplicada nos Prompts 25/26/27: verificação via AST,
    nunca grep textual ingênuo (a própria docstring de video_promotion.py
    referencia os importadores em prosa, o que colidiria com um grep
    textual)."""
    from _sistema import source_import

    tree = ast.parse(Path(source_import.__file__).read_text(encoding="utf-8"))
    termos_proibidos = ("videopromotionservice", "video_promotion", "promote(")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            names = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
        else:
            continue
        lowered = names.lower()
        for termo in termos_proibidos:
            assert termo not in lowered, f"referência proibida a promoção automática: {names!r}"

    # Confirma também que os três importadores em si não citam o serviço.
    assert "VideoPromotionService" not in Path(source_import.__file__).read_text(encoding="utf-8")
    for classe in (LocalFileImporter, FolderImporter, UrlImporter):
        assert classe is not None  # os três existem e seguem definidos em source_import.py
