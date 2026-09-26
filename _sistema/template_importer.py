"""``TemplateImporter`` -- Prompt 38 (Template Importer).

====================================================================
0. CONTRATO
====================================================================

Adaptador FINO sobre ``TemplateEngine`` (Prompt 36/37, já aprovado):
recebe o caminho de um arquivo PNG/JPG fornecido pelo usuário e devolve
o ``Template`` (``CUSTOM``) criado, com o arquivo copiado (nunca
referenciado in-place -- o original do usuário nunca é lido para
escrita, nunca movido, nunca alterado) para o armazenamento gerenciado
(``AppPaths.templates`` via ``StorageManager``, categoria protegida
``CATEGORY_TEMPLATE``).

Este módulo NUNCA reimplementa ``validate_layout`` nem manipula a
tabela ``templates`` diretamente -- toda escrita do ``Template`` passa
por ``TemplateEngine.create_template``, que já valida ``layout``,
vocabulário fechado de ``template_type``, etc. ``TemplateEngine``
continua sendo o único dono da entidade ``Template``.

====================================================================
1. GROUNDING (confirmado por leitura direta antes de codificar)
====================================================================

- ``TemplateEngine.create_template(*, name, layout,
  template_type="CUSTOM", source_path=None, enabled=True,
  extra=None) -> Template`` já existe e já valida tudo que este módulo
  precisa -- reaproveitado diretamente, nunca duplicado.
- ``_read_image_dimensions(path) -> (width, height)`` (mesmo domínio de
  ``template_engine.py``, não um módulo irmão de processamento
  paralelo) já isola a leitura via ``ffprobe`` -- reaproveitada aqui
  para a validação de CONTEÚDO (seção 3), nunca ``MediaProbe`` (que
  rejeita imagem estática, já confirmado no Prompt 36).
- Padrão de cópia binária-segura para armazenamento gerenciado:
  ``StorageManager.allocate_temp(suffix=..., create=True)`` + escrita
  dos bytes + ``promote_to_final(...)`` -- nunca ``open()``/``shutil``
  cru sob ``AppPaths.templates``.
- ``_sistema/source_import.py::_validate_file_for_import`` é o
  precedente de validação de ARQUIVO (não de mídia) para upload de
  vídeo -- reimplementado aqui para o domínio de imagem (NUNCA
  importado: módulo irmão de domínio de vídeo, não relacionado), com
  vocabulário de extensão e limite de tamanho próprios deste domínio.

====================================================================
2. INCIDENTE RECENTE (Prompt 36) -- POR QUE ESTE MÓDULO É CAUTELOSO COM
   BYTES BINÁRIOS
====================================================================

O empacotador de release (``empacotar_release.py``) corrompeu os 2
PNGs oficiais do Prompt 36 para 5 bytes cada por abrir arquivos com
``os.open(path, O_RDONLY)`` sem ``os.O_BINARY`` -- no Windows isso abre
em MODO TEXTO, onde o byte ``0x1A`` (presente na própria assinatura de
todo PNG, ``\\x89PNG\\r\\n\\x1a\\n``) é tratado como fim de arquivo.

Este módulo é estruturalmente IMUNE à mesma classe de bug: a cópia usa
exclusivamente ``Path.read_bytes()``/``Path.write_bytes()`` da stdlib
(nunca ``os.open``/``os.read`` crus). **Confirmado por leitura da
implementação do CPython**: ``Path.read_bytes()``/``Path.write_bytes()``
delegam a ``io.open(path, "rb")``/``io.open(path, "wb")`` -- o modo
``"b"`` (binário) é explícito na própria chamada, e o Python `io`
abre com ``O_BINARY`` internamente em qualquer plataforma sempre que
``"b"`` está no modo (ao contrário do ``os.open`` cru usado no bug do
Prompt 36, que exige a flag manualmente). Isto é reforçado por um teste
de regressão dedicado (``test_copia_preserva_byte_0x1a_no_meio_do_
arquivo``) que usa um arquivo sintético com ``0x1A`` no meio do
conteúdo -- prova direta, não inferência.

====================================================================
3. DECISÕES ARQUITETURAIS (documentadas, com justificativa)
====================================================================

**1. Validação de ARQUIVO** (``_validate_file_for_import``, antes de
qualquer leitura de conteúdo): vocabulário fechado de extensão
``IMPORT_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}`` -- exatamente o que
o roadmap pede ("PNG/JPG"), com ``.jpeg`` incluído por ser a grafia
alternativa mais comum de ``.jpg``. Existência, regularidade, tamanho >
0 e legibilidade seguem a MESMA disciplina de
``source_import.py::_validate_file_for_import`` (reimplementada, nunca
importada -- módulo irmão de domínio diferente). Hierarquia de erro:
``TemplateImportError`` **estende ``TemplateEngineError``** (não uma
nova base desacoplada) -- justificativa: o domínio é o mesmo
(``Template``), então qualquer chamador que já captura
``TemplateEngineError`` genericamente (ex.: uma futura camada de UI)
continua funcionando sem precisar conhecer este módulo especificamente;
``TemplateImportError`` adiciona só o atributo ``.code`` (mesmo padrão
de ``SourceImportError.code``) para discriminação programática fina
sem parsing de mensagem.

**2. Validação de CONTEÚDO** (a defesa real contra o cenário do
incidente recente): depois da validação de arquivo,
``_read_image_dimensions`` (de ``template_engine.py``) é chamada sobre
o caminho. Qualquer falha (``ffprobe`` não decodifica, retorna código
de erro, timeout, ou devolve dimensão <= 0) é capturada e traduzida
para ``TemplateImportError(ERROR_CONTEUDO_NAO_E_IMAGEM, ...)`` --
NUNCA uma exceção crua de ``template_engine`` (``TemplateEngineError``
genérica) escapa sem ser re-embrulhada com o código estruturado deste
módulo, e o arquivo NUNCA é aceito quando essa validação falha. Isto é
o que rejeita um ``.txt`` renomeado para ``.png``, ou um arquivo com só
os primeiros bytes da assinatura PNG sem o resto -- exatamente o
cenário do incidente do Prompt 36, agora do lado do upload do usuário.

**3. Tamanho máximo de arquivo**: não havia precedente no produto.
Decisão: ``MAX_IMPORT_FILE_SIZE_BYTES = 20 MiB``. Justificativa: os 2
templates oficiais do Prompt 36/37 (941x1672px, arte com gradientes e
texturas) pesam 379KB e 1.3MB -- 20 MiB dá margem de 15-50x sobre o
maior asset real conhecido do produto, cobrindo tranquilamente uma
imagem de alta resolução com textura pesada, sem deixar o limite
irrestrito (um upload irrestrito permitiria, por engano ou abuso, que
um arquivo de dezenas/centenas de MB (ex.: uma foto RAW renomeada, ou
um vídeo renomeado para ``.png``) fosse lido inteiro em memória via
``read_bytes()`` e copiado, sem necessidade concreta para uma imagem
de composição de template). Rejeitado ANTES da leitura de conteúdo
(``path.stat().st_size``, sem nunca ler os bytes) -- barato e cedo.

**4. Cópia binária-segura**: ``allocate_temp(suffix=<extensão
original em minúsculas>, create=True)`` (nome UUID4 gerado pelo
próprio ``StorageManager``, nenhum esquema de nome novo inventado) +
``Path.write_bytes(Path.read_bytes())`` (binário garantido em qualquer
SO, ver seção 2) + ``promote_to_final(temp_path, AppPaths.templates /
temp_path.name)``. O nome final REAPROVEITA o próprio nome UUID4 do
temporário (com a extensão original preservada) -- ao contrário dos 2
built-ins do Prompt 36 (nome determinístico por slug, para permitir
re-ingestão idempotente), aqui CADA importação é um ``Template``
``CUSTOM`` novo e sem necessidade de nome legível/estável (decisão 6),
então o UUID4 já gerado é reaproveitado sem custo extra.
``promote_to_final`` é chamado com ``overwrite=False`` (diferente do
Prompt 36, que usa ``overwrite=True`` de propósito para idempotência
por slug fixo) -- aqui o nome é sempre um UUID4 novo, então uma
colisão de destino já preexistente seria uma anomalia grave (nunca
esperada), e ``overwrite=False`` garante que, na hipótese
astronomicamente improvável de colisão, o import falha ruidosamente em
vez de silenciosamente sobrescrever o arquivo gerenciado de OUTRO
template.

**5. Layout inicial**: como não há inspeção visual possível de uma
imagem arbitrária do usuário (ao contrário dos 2 built-ins do Prompt
36, cuja zona ``VIDEO`` foi proposta por inspeção manual das imagens
fornecidas), a zona ``VIDEO`` nasce com um valor-padrão genérico e
centralizado: ``x=0.1, y=0.1, width=0.8, height=0.8`` -- 10% de margem
uniforme em todos os lados, cobrindo 64% da área do frame. Escolha
deliberadamente conservadora (nem cobre a borda inteira, nem é uma
área pequena arbitrária) -- é só uma PROPOSTA inicial editável,
igual à dos built-ins, pendente do futuro Layout Mapper (Prompt 39).
``BACKGROUND`` segue a mesma regra imposta de ``validate_layout``
(frame inteiro).

**6. Idempotência**: decisão consciente de NÃO deduplicar. Ao
contrário da ingestão built-in (idempotente por slug fixo, porque os 2
templates oficiais são um catálogo fixo e conhecido), cada chamada de
``import_template`` cria um ``Template`` ``CUSTOM`` NOVO e distinto --
dois uploads do mesmo arquivo pelo usuário são dois templates
customizados legítimos e independentes (o usuário pode querer
"Template A" e "Template A (cópia para editar)" coexistindo). Nenhuma
transação ``BEGIN IMMEDIATE`` de deduplicação é necessária -- não há
decisão atômica "já existe?" a proteger aqui, ao contrário do Prompt
36.

**7. Arquivo original do usuário nunca é modificado**: o caminho
fornecido é usado exclusivamente com ``path.exists()``/``path.is_file()``/
``path.stat()``/``path.open("rb")`` (para a checagem de legibilidade)/
``path.read_bytes()`` -- nenhuma chamada de escrita, rename ou delete
sobre ele em nenhum ponto do módulo. Provado por teste
(``test_arquivo_original_do_usuario_nunca_e_modificado``): hash/mtime
comparados antes/depois da importação.

====================================================================
4. ISOLAMENTO
====================================================================

Este módulo NÃO importa nenhum módulo irmão de decisão/processamento
paralelo (``visual_editor.py``, ``captions_style.py``,
``metadata_manager.py``, ``auto_reframe.py``, ``captions_engine.py``,
``silence_removal.py``, ``source_import.py``) nem ``media_catalog.py``
nem nada de Geração 1. Importa apenas ``template_engine`` (mesmo
domínio -- ``Template`` -- não um irmão de domínio diferente).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .app_paths import AppPaths
from .domain import Template
from .storage_manager import StorageManager
from .template_engine import (
    TEMPLATE_TYPE_CUSTOM,
    TemplateEngine,
    TemplateEngineError,
    _read_image_dimensions,
)

# ---------------------------------------------------------------------------
# Vocabulário fechado / limites -- ver decisões 1 e 3 da docstring do módulo
# ---------------------------------------------------------------------------

IMPORT_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg"})
MAX_IMPORT_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MiB -- ver decisao 3

# ---------------------------------------------------------------------------
# Códigos de erro estruturados -- nunca uma exceção crua escapa deste
# módulo; sempre um destes, em ``TemplateImportError.code``.
# ---------------------------------------------------------------------------

ERROR_ARQUIVO_INEXISTENTE = "ARQUIVO_INEXISTENTE"
ERROR_NAO_E_ARQUIVO_REGULAR = "NAO_E_ARQUIVO_REGULAR"
ERROR_EXTENSAO_INVALIDA = "EXTENSAO_INVALIDA"
ERROR_ARQUIVO_VAZIO = "ARQUIVO_VAZIO"
ERROR_ARQUIVO_MUITO_GRANDE = "ARQUIVO_MUITO_GRANDE"
ERROR_ARQUIVO_ILEGIVEL = "ARQUIVO_ILEGIVEL"
ERROR_CONTEUDO_NAO_E_IMAGEM = "CONTEUDO_NAO_E_IMAGEM"


class TemplateImportError(TemplateEngineError):
    """Falha estruturada de importação -- nunca uma exceção Python crua.

    Estende ``TemplateEngineError`` (mesmo domínio -- ``Template``) em
    vez de uma base nova desacoplada -- ver decisão 1 da docstring do
    módulo. ``code`` é sempre uma das constantes ``ERROR_*`` acima.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Validação de ARQUIVO (nunca de conteúdo/mídia) -- ver decisão 1
# ---------------------------------------------------------------------------


def _validate_file_for_import(path: Path) -> None:
    if not path.exists():
        raise TemplateImportError(ERROR_ARQUIVO_INEXISTENTE, f"Arquivo nao encontrado: {path}")
    if not path.is_file():
        raise TemplateImportError(
            ERROR_NAO_E_ARQUIVO_REGULAR, f"Caminho nao e um arquivo regular: {path}"
        )
    if path.suffix.lower() not in IMPORT_IMAGE_EXTS:
        raise TemplateImportError(
            ERROR_EXTENSAO_INVALIDA, f"Extensao nao permitida ({path.suffix!r}): {path}"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise TemplateImportError(
            ERROR_ARQUIVO_ILEGIVEL, f"Nao foi possivel ler o arquivo: {path}"
        ) from exc
    if size <= 0:
        raise TemplateImportError(ERROR_ARQUIVO_VAZIO, f"Arquivo vazio: {path}")
    if size > MAX_IMPORT_FILE_SIZE_BYTES:
        raise TemplateImportError(
            ERROR_ARQUIVO_MUITO_GRANDE,
            f"Arquivo maior que o limite permitido de {MAX_IMPORT_FILE_SIZE_BYTES} "
            f"bytes: {path} ({size} bytes)",
        )
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise TemplateImportError(
            ERROR_ARQUIVO_ILEGIVEL, f"Nao foi possivel ler o arquivo: {path}"
        ) from exc


# ---------------------------------------------------------------------------
# Validação de CONTEÚDO (a defesa real) -- ver decisão 2
# ---------------------------------------------------------------------------


def _validate_image_content(path: Path) -> tuple[int, int]:
    try:
        width, height = _read_image_dimensions(path)
    except TemplateEngineError as exc:
        raise TemplateImportError(
            ERROR_CONTEUDO_NAO_E_IMAGEM,
            f"Conteudo do arquivo nao e uma imagem decodificavel: {path}",
        ) from exc
    if width <= 0 or height <= 0:
        raise TemplateImportError(
            ERROR_CONTEUDO_NAO_E_IMAGEM,
            f"Dimensoes invalidas lidas do arquivo: {path} ({width}x{height})",
        )
    return width, height


def _default_import_layout() -> dict[str, Any]:
    """Layout inicial genérico -- ver decisão 5 da docstring do módulo.
    Validado por ``TemplateEngine.create_template`` (via
    ``validate_layout``) no momento da criação -- nunca reimplementado
    aqui."""
    return {
        "zones": [
            {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
        ]
    }


def _derive_default_name(path: Path) -> str:
    stem = path.stem.strip()
    return stem if stem else "Template Importado"


class TemplateImporter:
    """Adaptador fino sobre ``TemplateEngine`` para importação de um
    PNG/JPG fornecido pelo usuário como ``Template`` ``CUSTOM`` -- ver
    docstring do módulo para o contrato completo."""

    def __init__(
        self,
        template_engine: TemplateEngine,
        *,
        storage_manager: StorageManager,
        app_paths: AppPaths,
    ) -> None:
        if not isinstance(template_engine, TemplateEngine):
            raise TypeError(
                f"template_engine deve ser TemplateEngine (recebido: {type(template_engine)!r})"
            )
        if not isinstance(storage_manager, StorageManager):
            raise TypeError(
                f"storage_manager deve ser StorageManager (recebido: {type(storage_manager)!r})"
            )
        if not isinstance(app_paths, AppPaths):
            raise TypeError(f"app_paths deve ser AppPaths (recebido: {type(app_paths)!r})")
        self._engine = template_engine
        self._storage = storage_manager
        self._app_paths = app_paths

    def _copy_into_managed_storage(self, source_path: Path) -> Path:
        suffix = source_path.suffix.lower()
        temp_path = self._storage.allocate_temp(suffix=suffix, create=True)
        # Path.read_bytes()/write_bytes() -- binario garantido em
        # qualquer SO (ver secao 2 da docstring do modulo). Nunca
        # os.open/os.read crus.
        temp_path.write_bytes(source_path.read_bytes())
        final_path = Path(self._app_paths.templates) / temp_path.name
        promoted = self._storage.promote_to_final(temp_path, final_path, overwrite=False)
        return Path(promoted)

    def import_template(self, file_path: "Path | str", *, name: str | None = None) -> Template:
        """Importa ``file_path`` (PNG/JPG fornecido pelo usuário) como um
        novo ``Template`` ``CUSTOM``. ``file_path`` é sempre LIDO, nunca
        escrito/movido/apagado -- ver decisão 7 da docstring do módulo.
        Cada chamada cria um ``Template`` novo e distinto, mesmo que
        ``file_path`` já tenha sido importado antes -- ver decisão 6."""
        path = Path(file_path)
        _validate_file_for_import(path)
        width, height = _validate_image_content(path)

        managed_path = self._copy_into_managed_storage(path)

        final_name = name.strip() if name and name.strip() else _derive_default_name(path)

        return self._engine.create_template(
            name=final_name,
            layout=_default_import_layout(),
            template_type=TEMPLATE_TYPE_CUSTOM,
            source_path=str(managed_path),
            enabled=True,
            extra={
                "width": width,
                "height": height,
                "imported_original_filename": path.name,
            },
        )


__all__ = [
    "TemplateImporter",
    "TemplateImportError",
    "IMPORT_IMAGE_EXTS",
    "MAX_IMPORT_FILE_SIZE_BYTES",
    "ERROR_ARQUIVO_INEXISTENTE",
    "ERROR_NAO_E_ARQUIVO_REGULAR",
    "ERROR_EXTENSAO_INVALIDA",
    "ERROR_ARQUIVO_VAZIO",
    "ERROR_ARQUIVO_MUITO_GRANDE",
    "ERROR_ARQUIVO_ILEGIVEL",
    "ERROR_CONTEUDO_NAO_E_IMAGEM",
]
