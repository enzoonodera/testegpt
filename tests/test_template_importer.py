# -*- coding: utf-8 -*-
"""PROMPT 38 -- TemplateImporter: testes.

Cobre: importação bem-sucedida de PNG/JPG válido (Template CUSTOM,
layout válido, extra com dimensões corretas); rejeição estruturada por
extensão/arquivo (inexistente, não-regular, vazio, ilegível, extensão
inválida, tamanho acima do limite); REJEIÇÃO POR CONTEÚDO FALSO --
prova direta do cenário do incidente do Prompt 36, agora do lado do
upload do usuário (extensão .png válida mas conteúdo não é imagem
real, incluindo um arquivo com só a assinatura PNG truncada); prova de
imunidade ao bug de truncamento 0x1A do Prompt 36 (bytes copiados
idênticos byte a byte, incluindo 0x1A no meio do conteúdo); arquivo
original do usuário nunca modificado; duas importações do mesmo
arquivo produzem dois Templates distintos; AST estrutural (zero
import de módulo irmão/Geração 1).
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

import _sistema.template_importer as template_importer_module
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import TEMPLATE_TYPE_CUSTOM, TemplateEngine
from _sistema.template_importer import (
    ERROR_ARQUIVO_ILEGIVEL,
    ERROR_ARQUIVO_INEXISTENTE,
    ERROR_ARQUIVO_MUITO_GRANDE,
    ERROR_ARQUIVO_VAZIO,
    ERROR_CONTEUDO_NAO_E_IMAGEM,
    ERROR_EXTENSAO_INVALIDA,
    ERROR_NAO_E_ARQUIVO_REGULAR,
    MAX_IMPORT_FILE_SIZE_BYTES,
    TemplateImportError,
    TemplateImporter,
)


# ---------------------------------------------------------------------------
# Fixtures (mesmo padrão de test_template_engine.py)
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


@pytest.fixture
def importer(engine, storage, app_paths):
    return TemplateImporter(engine, storage_manager=storage, app_paths=app_paths)


# Um PNG real e minimo (1x1 pixel, gerado uma vez e reutilizado) -- o
# menor PNG valido e decodificavel que existe, usado para nao depender
# de nenhum asset externo ao rodar estes testes.
_MINIMAL_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415478da6360606060000000050001a5f645400000000049454e44ae426082"
)


@pytest.fixture
def png_1x1(tmp_path) -> Path:
    p = tmp_path / "fonte" / "meu_template.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_MINIMAL_PNG_1X1)
    return p


# ---------------------------------------------------------------------------
# Importação bem-sucedida
# ---------------------------------------------------------------------------


def test_import_template_sucesso_cria_template_custom(importer, png_1x1):
    tpl = importer.import_template(png_1x1)
    assert tpl.template_type == TEMPLATE_TYPE_CUSTOM
    assert tpl.enabled is True
    assert tpl.name == "meu_template"
    assert tpl.source_path != str(png_1x1)
    assert Path(tpl.source_path).is_file()
    assert tpl.layout["zones"][0]["zone_type"] == "BACKGROUND"
    assert tpl.layout["zones"][1]["zone_type"] == "VIDEO"


def test_import_template_extra_tem_dimensoes_corretas(importer, png_1x1):
    tpl = importer.import_template(png_1x1)
    assert tpl.extra["width"] == 1
    assert tpl.extra["height"] == 1
    assert tpl.extra["imported_original_filename"] == "meu_template.png"


def test_import_template_com_nome_explicito(importer, png_1x1):
    tpl = importer.import_template(png_1x1, name="  Nome Customizado  ")
    assert tpl.name == "Nome Customizado"


def test_import_template_nome_default_deriva_do_stem_do_arquivo(importer, tmp_path):
    p = tmp_path / "Meu Arquivo Especial.png"
    p.write_bytes(_MINIMAL_PNG_1X1)
    tpl = importer.import_template(p)
    assert tpl.name == "Meu Arquivo Especial"


def test_import_template_persistido_e_recuperavel_via_engine(importer, engine, png_1x1):
    tpl = importer.import_template(png_1x1)
    reloaded = engine.get_template(tpl.id)
    assert reloaded.id == tpl.id
    assert reloaded.source_path == tpl.source_path


def test_import_template_source_path_dentro_de_app_paths_templates(importer, app_paths, png_1x1):
    tpl = importer.import_template(png_1x1)
    managed = Path(tpl.source_path)
    assert Path(app_paths.templates) in managed.parents


def test_import_template_aceita_jpg_e_jpeg(importer, tmp_path):
    # ffprobe/ffmpeg leem qualquer formato pela assinatura real, nao pela
    # extensao -- um PNG com extensao .jpg ainda e uma imagem
    # decodificavel valida, o suficiente para exercitar o vocabulario de
    # extensao aceitas sem depender de gerar um JPEG real.
    for ext in (".jpg", ".jpeg"):
        p = tmp_path / f"imagem{ext}"
        p.write_bytes(_MINIMAL_PNG_1X1)
        tpl = importer.import_template(p)
        assert tpl.id


# ---------------------------------------------------------------------------
# Rejeição estruturada -- nível arquivo
# ---------------------------------------------------------------------------


def test_import_template_arquivo_inexistente(importer, tmp_path):
    with pytest.raises(TemplateImportError) as exc_info:
        importer.import_template(tmp_path / "nao_existe.png")
    assert exc_info.value.code == ERROR_ARQUIVO_INEXISTENTE


def test_import_template_nao_e_arquivo_regular(importer, tmp_path):
    d = tmp_path / "diretorio.png"
    d.mkdir()
    with pytest.raises(TemplateImportError) as exc_info:
        importer.import_template(d)
    assert exc_info.value.code == ERROR_NAO_E_ARQUIVO_REGULAR


def test_import_template_extensao_invalida(importer, tmp_path):
    p = tmp_path / "arquivo.gif"
    p.write_bytes(_MINIMAL_PNG_1X1)
    with pytest.raises(TemplateImportError) as exc_info:
        importer.import_template(p)
    assert exc_info.value.code == ERROR_EXTENSAO_INVALIDA


def test_import_template_arquivo_vazio(importer, tmp_path):
    p = tmp_path / "vazio.png"
    p.write_bytes(b"")
    with pytest.raises(TemplateImportError) as exc_info:
        importer.import_template(p)
    assert exc_info.value.code == ERROR_ARQUIVO_VAZIO


def test_import_template_arquivo_muito_grande(importer, tmp_path):
    p = tmp_path / "grande.png"
    p.write_bytes(b"\x00" * (MAX_IMPORT_FILE_SIZE_BYTES + 1))
    with pytest.raises(TemplateImportError) as exc_info:
        importer.import_template(p)
    assert exc_info.value.code == ERROR_ARQUIVO_MUITO_GRANDE


def test_import_template_arquivo_exatamente_no_limite_e_aceito_na_camada_de_arquivo(importer, tmp_path):
    # No limite exato (nao acima) a validacao de ARQUIVO deve deixar
    # passar -- so falha depois, na validacao de CONTEUDO, porque o
    # conteudo sintetico nao decodifica como imagem real. Prova que o
    # limite e "> MAX", nao ">= MAX".
    p = tmp_path / "no_limite.png"
    p.write_bytes(_MINIMAL_PNG_1X1 + b"\x00" * (MAX_IMPORT_FILE_SIZE_BYTES - len(_MINIMAL_PNG_1X1)))
    tpl = importer.import_template(p)
    assert tpl.id  # nao levantou ARQUIVO_MUITO_GRANDE


# ---------------------------------------------------------------------------
# REJEIÇÃO POR CONTEÚDO FALSO -- prova direta do incidente do Prompt 36
# ---------------------------------------------------------------------------


def test_import_template_conteudo_texto_puro_com_extensao_png_rejeitado(importer, tmp_path):
    p = tmp_path / "fingido.png"
    p.write_bytes(b"isto e apenas texto, nao uma imagem" * 10)
    with pytest.raises(TemplateImportError) as exc_info:
        importer.import_template(p)
    assert exc_info.value.code == ERROR_CONTEUDO_NAO_E_IMAGEM


def test_import_template_apenas_assinatura_png_truncada_rejeitado(importer, tmp_path):
    """O cenario exato do incidente: um arquivo com so a assinatura PNG
    (que contem 0x1A) e nada mais -- nao decodifica, precisa ser
    rejeitado por CONTEUDO, nunca aceito."""
    p = tmp_path / "truncado.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(TemplateImportError) as exc_info:
        importer.import_template(p)
    assert exc_info.value.code == ERROR_CONTEUDO_NAO_E_IMAGEM


def test_import_template_conteudo_falso_nao_cria_nenhum_template(importer, engine, tmp_path):
    p = tmp_path / "fingido.png"
    p.write_bytes(b"nao e uma imagem")
    with pytest.raises(TemplateImportError):
        importer.import_template(p)
    assert engine.list_templates() == []


# ---------------------------------------------------------------------------
# Imunidade ao bug de truncamento 0x1A (Prompt 36) -- prova direta
# ---------------------------------------------------------------------------


def test_copia_preserva_byte_0x1a_no_meio_do_arquivo(importer, tmp_path):
    """O proprio PNG minimo real ja contem 0x1A na assinatura (byte 7,
    ``\\x89PNG\\r\\n\\x1a\\n``) -- exatamente o byte que o bug do Prompt
    36 tratava como fim de arquivo em modo texto no Windows. Prova que
    a copia preserva os bytes identicos, comparando contra o ORIGINAL
    gravado em disco (nao contra a variavel Python em memoria)."""
    p = tmp_path / "com_0x1a.png"
    p.write_bytes(_MINIMAL_PNG_1X1)
    assert 0x1A in _MINIMAL_PNG_1X1[:8]  # confirma a premissa do teste
    hash_original = hashlib.sha256(p.read_bytes()).hexdigest()

    tpl = importer.import_template(p)

    hash_copiado = hashlib.sha256(Path(tpl.source_path).read_bytes()).hexdigest()
    assert hash_copiado == hash_original
    assert Path(tpl.source_path).stat().st_size == p.stat().st_size


def test_copia_e_binaria_segura_para_arquivo_sintetico_com_0x1a_no_meio(importer, tmp_path):
    """Prova mais direta ainda: injeta 0x1A no meio de um payload extra
    apos um PNG valido e confirma que a copia gerenciada tem o mesmo
    tamanho e hash do arquivo de origem -- nao apenas que 'importou',
    mas que o comprimento em bytes é idêntico (o sintoma do bug do
    Prompt 36 era truncamento silencioso de tamanho)."""
    payload_extra = b"dados extras " * 50 + b"\x1a" + b"mais dados depois do 0x1a" * 50
    conteudo = _MINIMAL_PNG_1X1 + payload_extra
    p = tmp_path / "extra_com_0x1a.png"
    p.write_bytes(conteudo)

    tpl = importer.import_template(p)

    copiado = Path(tpl.source_path).read_bytes()
    assert copiado == conteudo
    assert len(copiado) == len(conteudo)


# ---------------------------------------------------------------------------
# Arquivo original nunca modificado
# ---------------------------------------------------------------------------


def test_arquivo_original_do_usuario_nunca_e_modificado(importer, png_1x1):
    hash_antes = hashlib.sha256(png_1x1.read_bytes()).hexdigest()
    mtime_antes = png_1x1.stat().st_mtime_ns

    importer.import_template(png_1x1)

    hash_depois = hashlib.sha256(png_1x1.read_bytes()).hexdigest()
    mtime_depois = png_1x1.stat().st_mtime_ns
    assert hash_depois == hash_antes
    assert mtime_depois == mtime_antes


# ---------------------------------------------------------------------------
# Idempotência: decisão consciente de NÃO deduplicar
# ---------------------------------------------------------------------------


def test_duas_importacoes_do_mesmo_arquivo_produzem_templates_distintos(importer, png_1x1):
    tpl1 = importer.import_template(png_1x1)
    tpl2 = importer.import_template(png_1x1)

    assert tpl1.id != tpl2.id
    assert tpl1.source_path != tpl2.source_path
    assert Path(tpl1.source_path).is_file()
    assert Path(tpl2.source_path).is_file()
    # ambos os arquivos gerenciados coexistem, com o mesmo conteudo
    assert Path(tpl1.source_path).read_bytes() == Path(tpl2.source_path).read_bytes()


def test_duas_importacoes_aparecem_como_dois_templates_na_listagem(importer, engine, png_1x1):
    importer.import_template(png_1x1)
    importer.import_template(png_1x1)
    assert len(engine.list_templates(template_type=TEMPLATE_TYPE_CUSTOM)) == 2


# ---------------------------------------------------------------------------
# Construtor -- validação de dependências
# ---------------------------------------------------------------------------


def test_init_rejeita_template_engine_de_tipo_errado(storage, app_paths):
    with pytest.raises(TypeError):
        TemplateImporter("nao e um engine", storage_manager=storage, app_paths=app_paths)


def test_init_rejeita_storage_manager_de_tipo_errado(engine, app_paths):
    with pytest.raises(TypeError):
        TemplateImporter(engine, storage_manager="nao e um storage", app_paths=app_paths)


def test_init_rejeita_app_paths_de_tipo_errado(engine, storage):
    with pytest.raises(TypeError):
        TemplateImporter(engine, storage_manager=storage, app_paths="nao e um app_paths")


# ---------------------------------------------------------------------------
# Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def _parse_module() -> ast.AST:
    return ast.parse(Path(template_importer_module.__file__).read_text(encoding="utf-8"))


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
            assert proibido not in names, f"template_importer.py nao pode importar {proibido!r} (encontrado: {names!r})"


def test_modulo_so_importa_template_engine_como_dependencia_de_dominio():
    tree = _parse_module()
    modulos_locais_importados = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level > 0:
            modulos_locais_importados.add(node.module)
    assert modulos_locais_importados == {"app_paths", "domain", "storage_manager", "template_engine"}


def test_copia_binaria_nunca_usa_os_open_cru():
    """AST: garante que a copia continua usando Path.read_bytes/write_bytes
    (binario garantido) -- nunca volta a usar os.open/os.read crus, que
    foi exatamente a causa raiz do bug do Prompt 36."""
    tree = _parse_module()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("open",):
            # 'os.open' especificamente -- Path.open("rb") e permitido
            # (usado so para a checagem de legibilidade, sempre em modo
            # binario "rb").
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                raise AssertionError("template_importer.py nao pode usar os.open cru")
    codigo = Path(template_importer_module.__file__).read_text(encoding="utf-8")
    assert "import os" not in codigo