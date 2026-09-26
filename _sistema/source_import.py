# -*- coding: utf-8 -*-
"""PROMPT 24 -- Source Import Manager: importadores que populam ``SourceAsset``.

ESCOPO E CONTEXTO (ver investigação da seção 0 do Prompt):

``SourceAsset`` (`_sistema/domain/models.py`, tabela ``sources`` em
``m001_initial.py``) já existe desde antes deste Prompt, com exatamente os
campos que um importador precisa preencher: ``source_uri``, ``local_path``,
``original_name``, ``fingerprint``, ``media_type``, ``size_bytes``. Este
módulo NÃO cria a entidade -- constrói os TRÊS importadores que a populam
corretamente (``LocalFileImporter``, ``FolderImporter``, ``UrlImporter``) e
a fachada ``SourceImportManager`` que os expõe.

ORTOGONALIDADE: este módulo nunca importa nem chama
``circuit_breaker``/``retry_policy``/``publication_idempotency``/
``secrets_manager``/``domain.job_state_machine`` -- fila/retry/idempotência
de publicação/segredos são camadas completamente diferentes. Também nunca
cria ``Job``/``Publication``/``Schedule`` -- um importador termina
exatamente em "devolver um ``SourceAsset`` válido" (persistido no SQLite
local), nunca decide nada sobre fila, edição ou publicação.

DECISÃO: ``Video`` NÃO é criado automaticamente aqui, embora
``Video.source_asset_id`` exista para isso. A seção 0 do Prompt descreve o
trabalho deste módulo como "popular ``SourceAsset``" -- criar também um
``Video`` por importação levantaria perguntas de ciclo de vida fora de
escopo (o que acontece ao reimportar o mesmo arquivo? um ``SourceAsset``
sempre gera exatamente um ``Video``? nunca mais de um?) que este Prompt não
define. Manter o importador restrito a ``SourceAsset`` é o comportamento
mínimo e reversível -- um Prompt futuro que definir essas regras de ciclo
de vida pode criar o ``Video`` de forma consciente, sem que este módulo
precise ser desfeito.

FINGERPRINT: SHA-256 de ``tamanho + primeiro 1 MiB + último 1 MiB`` do
arquivo -- NÃO o arquivo inteiro. Geração 1 (``agendar_youtube.py``,
função ``fingerprint()``) já usa exatamente esta estratégia; ela é
REPRODUZIDA aqui de forma independente (nunca importada -- regra de zero
cross-import já estabelecida desde o Prompt 20) como referência de projeto
já validada. Trade-off explícito: hashear um vídeo de vários GB por
inteiro, para CADA importação (inclusive ``FolderImporter`` processando
centenas de arquivos), custaria I/O proporcional ao tamanho total da
biblioteca do usuário a cada operação -- caro demais para um sinal cuja
única finalidade hoje é identidade/dedup informal (nunca integridade
criptográfica: não há verificação de assinatura nem detecção de adulteração
byte-a-byte dependendo deste valor). O trade-off aceito: dois arquivos
diferentes com o mesmo tamanho E os mesmos primeiro/último MiB produziriam
uma colisão de fingerprint -- teoricamente possível, praticamente
irrelevante para vídeos reais, e already aceito pela Geração 1 em produção.

ARMAZENAMENTO -- referenciar vs. copiar (decisão por importador,
documentada):

- ``LocalFileImporter``/``FolderImporter``: REFERENCIAM o arquivo onde ele
  já está (``local_path`` aponta para o caminho original, nunca copiado).
  Mais rápido, não duplica espaço em disco (vídeos podem ser gigantescos) --
  o trade-off aceito é que mover/apagar o arquivo original depois quebra a
  referência; validação PROFUNDA de "o arquivo referenciado ainda existe e
  é válido" é responsabilidade de uma camada futura (ex.: antes de
  processar um Job que o usa), não deste importador.
- ``UrlImporter``: OBRIGATORIAMENTE copia/persiste -- não existe "arquivo
  original local" quando a origem é uma URL. O conteúdo baixado é gravado
  em ``AppPaths.sources`` (diretório gerenciado novo, adicionado a
  ``app_paths.py`` nesta rodada, com a mesma disciplina dos demais
  diretórios mutáveis do produto).

``UrlImporter`` -- LIMITE RÍGIDO (não uma preferência de implementação):
um GET HTTP(S) simples e direto via ``urllib.request.urlopen(url, ...)``,
SEM NENHUM objeto ``Request`` com cabeçalhos customizados, SEM
``http.cookiejar``, SEM sessão, SEM header ``Authorization``/``Cookie`` em
lugar nenhum do código -- estruturalmente impossível de compor um desses,
porque o código nunca constrói um dicionário de headers para anexar a uma
requisição. Nenhuma extração específica de site, nenhuma tentativa de
contornar login/paywall/DRM: se a URL não servir o conteúdo diretamente com
um GET simples, o resultado é uma falha estruturada
(``SourceImportError``), nunca um problema a contornar.

VALIDAÇÃO: só de nível de ARQUIVO nesta camada (existe, é legível, tamanho
> 0, extensão permitida) -- validação PROFUNDA de mídia (codec, duração,
corrupção, FPS, resolução via ffprobe/equivalente) é explicitamente o
Prompt 26 (MediaProbe), fora de escopo aqui. Um arquivo que passa nesta
validação superficial mas está corrompido por dentro ainda é aceito.

ISOLAMENTO POR ITEM (``FolderImporter``): um arquivo problemático nunca
interrompe o processamento dos demais -- mesmo princípio já usado em toda
a Geração 2 (Prompt 20, ``JobEngine.run_pending()``). ``FolderImporter``
captura ``SourceImportError``/``OSError`` por arquivo (o universo exato de
falhas de nível-arquivo que a validação desta camada define) e continua;
uma falha de banco de dados (``sqlite3.Error``) NÃO é capturada por item --
é sistêmica (o mesmo banco é usado para todos os arquivos da pasta), então
se falhar uma vez provavelmente falharia para todos os demais também;
mascará-la produziria um lote inteiro de resultados "falhou" enganosos em
vez de sinalizar claramente um problema de infraestrutura. Esta é uma
decisão consciente, documentada aqui e no relatório de entrega.

===========================================================================
PROMPT 24b -- IMPORT OPTIONS / USER ASSERTIONS (segunda metade do roadmap)
===========================================================================

``ImportOptions`` é um pacote OPCIONAL de declarações que o chamador pode
anexar a uma importação (``LocalFileImporter``/``FolderImporter``/
``UrlImporter``/``SourceImportManager`` continuam funcionando exatamente
como antes quando ``options`` é omitido -- nenhuma assinatura pública
existente se tornou obrigatória). Quando fornecido, as declarações são
persistidas associadas ao(s) ``SourceAsset`` resultante(s) -- para
``FolderImporter``, o MESMO ``ImportOptions`` se aplica a TODOS os
arquivos importados com sucesso da chamada inteira.

ALVO REAL -- ``SourceAsset``, não ``Video`` (decisão da seção 0.1):
``Video`` não é criado pelos importadores (decisão já tomada e mantida
acima). O roadmap fala em aplicar rótulos/presets "a um vídeo", mas no
momento da importação só existe ``SourceAsset`` -- é a ele que toda
declaração se associa (``source_asset_id``, identidade estável, nunca
caminho de arquivo/URL). Quando um Prompt futuro definir o ciclo de vida
``SourceAsset -> Video``, migrar essas declarações para o ``Video``
correspondente é trabalho desse Prompt futuro, não deste.

``target_project_id``/``target_account_id`` são só REFERÊNCIAS
declarativas: validadas como UUID textual quando presentes (mesmo padrão
``_validate_uuid`` já usado em ``domain/models.py``), mas NUNCA
verificadas contra o banco (não existe lookup nem FK para
``projects``/``accounts``) -- o roadmap não exige isso, e exigir aqui
acoplaria import a projeto/conta antes da hora (uma importação pode
acontecer antes de qualquer projeto/conta existir). Um UUID malformado é
sempre rejeitado (falha estruturada); um UUID válido mas inexistente no
banco é aceito silenciosamente -- decisão consciente, documentada aqui e
no relatório.

``readiness_profile_id`` é só um identificador OPACO opcional nesta
rodada -- armazenado e devolvido, nenhuma lógica de "perfil de prontidão"
associada (não existe ainda em nenhum outro Prompt já lido).

DESENHO DE DADOS -- tabela única append-only (``source_asset_declarations``,
``m007_source_asset_declarations.py``): ver a migration para a
justificativa completa (opção "b" da seção 0.1 do Prompt, preferida a uma
tabela de estado + uma tabela de histórico separadas). Cada linha é um
evento ``ADD``/``REMOVE`` para uma chave lógica (``source_asset_id``,
``kind``, ``value``); o estado atual dessa chave é o ``action`` do evento
mais recente. "Remover" uma declaração é sempre um novo evento ``REMOVE``
-- nunca um ``UPDATE``/``DELETE`` do evento original (a tabela em si é
append-only por trigger SQLite, mesmo padrão de ``audit_events``/m002).

ORIGEM (``SYSTEM``/``USER``/``IMPORT_PRESET``) -- ponto central do
roadmap: é uma coluna OBRIGATÓRIA em toda escrita, nunca inferida na
leitura. Este Prompt NUNCA grava ``origin=SYSTEM`` -- não existe hoje
nenhuma evidência operacional automática do produto sobre um
``SourceAsset`` (isso viria de um Prompt futuro, ex. MediaProbe/Prompt
26). ``SYSTEM`` existe no vocabulário/schema porque o roadmap pede a
distinção estrutural, mas nenhum caminho de código deste módulo o
escreve -- só ``USER`` (declaração explícita do chamador, seja durante a
importação ou via ``apply_assertions``/``remove_assertion`` depois) e
``IMPORT_PRESET`` (declaração derivada automaticamente ao expandir um
preset conhecido).

PRESETS -- mapeamento exato (decisão de produto, documentada e testada):

- ``ORIGINAL``: label ``ORIGINAL`` (nenhuma assertion -- não afirma
  nenhum preparo externo).
- ``ALREADY_EDITED``: assertion ``EXTERNALLY_EDITED``.
- ``READY_FOR_AI``: label ``READY_FOR_AI`` (nenhuma assertion externa --
  o preset só sinaliza "processar com IA", não que algo já foi feito fora
  do produto).
- ``USER_MARKED_READY``: assertion ``USER_MARKED_READY``.
- ``CUSTOM``: nenhum mapeamento automático -- o chamador fornece
  ``user_assertions``/``user_flags``/``user_labels`` explicitamente.

Um preset expandido é sempre gravado com ``origin=IMPORT_PRESET``.
Qualquer valor que o chamador tenha colocado explicitamente em
``user_assertions``/``user_flags``/``user_labels`` (com ou sem preset
junto) é sempre gravado com ``origin=USER`` -- são dois vocabulários
independentes que podem coexistir na mesma importação.

REGRA DE CONTEÚDO (item 1.7, garantia estrutural por AUSÊNCIA):
``EXTERNALLY_TITLE_READY``/``EXTERNALLY_DESCRIPTION_READY``/
``EXTERNALLY_HASHTAGS_READY`` são só metadados declarativos -- este
módulo NÃO lê essas assertions para preencher
``Publication.title``/``description``/hashtags automaticamente. Não
existe nenhum código aqui que faça essa leitura; a garantia é a ausência
do caminho, não uma checagem em runtime. Uma camada futura de publicação
deve tratar essas assertions como "o usuário afirma que está pronto",
nunca como o conteúdo em si -- nunca inventar conteúdo inexistente nem
transformar um campo vazio em conteúdo válido a partir delas.

Nenhuma escrita neste módulo persiste segredo, conteúdo bruto de arquivo
ou caminho de sistema de arquivos desnecessário no histórico de
declarações -- só ``source_asset_id``, ``kind``, ``value``, ``origin``,
``action`` e ``created_at`` (item 1.6), usando sempre ``utc_now_iso()``
(``time_utils.py``) para o timestamp.

===========================================================================
PROMPT 25 -- SOURCE CONTEXT RESOLVER (classificação técnica automática)
===========================================================================

``LocalFileImporter``/``UrlImporter`` chamam automaticamente
``SourceContextResolver.resolve_and_persist`` (``_sistema/source_context.py``
-- contrato completo, regras de classificação e decisões de desenho
documentadas lá) logo após ``assertion_store.apply_options``, ao final de
cada importação bem-sucedida -- "invisível ao usuário" (item 1.2 do
Prompt 25): nenhum parâmetro novo, nenhuma escolha do chamador, sempre
acontece. ``FolderImporter`` delega a ``LocalFileImporter.import_source``
por arquivo, então a classificação automática já cobre todos os arquivos
de um lote sem nenhuma mudança adicional neste módulo.

ISOLAMENTO OBRIGATÓRIO: a chamada ao resolver é envolvida em
``try/except Exception`` explícito em cada ponto de integração -- o
``SourceAsset`` já foi persistido com sucesso ANTES dessa chamada
acontecer, então uma falha inesperada de classificação NUNCA pode
transformar um ``ImportResult`` de sucesso em falha, nem impedir o
retorno do resultado já construído. Esta é a única exceção genérica
(``except Exception``) deste módulo -- documentada aqui e no próprio
ponto de integração porque é deliberada: qualquer outra falha
inesperada nunca capturada por ``except SourceImportError`` continua
propagando normalmente (comportamento inalterado).

Nenhuma assinatura pública existente mudou de contrato: ``ImportResult``
continua exatamente com os mesmos campos; a classificação em si nunca é
devolvida por ``import_source``/``import_local_file``/``import_folder``/
``import_url`` -- é consultada separadamente via
``SourceContextResolver.get_current``/``SourceImportManager.get_context``.
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from .app_paths import AppPaths
from .domain import SourceAsset, new_uuid
from .source_context import SourceContextResolver
from .storage.database import LocalDatabase
from .time_utils import utc_now_iso

__all__ = [
    "VIDEO_EXTS",
    "SourceImportError",
    "ImportResult",
    "compute_fingerprint",
    "LocalFileImporter",
    "FolderImporter",
    "UrlImporter",
    "SourceImportManager",
    # PROMPT 24b -- Import Options / User Assertions
    "DECLARATION_KIND_ASSERTION",
    "DECLARATION_KIND_FLAG",
    "DECLARATION_KIND_LABEL",
    "DECLARATION_KINDS",
    "ORIGIN_SYSTEM",
    "ORIGIN_USER",
    "ORIGIN_IMPORT_PRESET",
    "DECLARATION_ORIGINS",
    "PRESET_ORIGINAL",
    "PRESET_ALREADY_EDITED",
    "PRESET_READY_FOR_AI",
    "PRESET_USER_MARKED_READY",
    "PRESET_CUSTOM",
    "PRESETS",
    "ASSERTION_EXTERNALLY_EDITED",
    "ASSERTION_EXTERNALLY_CAPTIONED",
    "ASSERTION_EXTERNALLY_METADATA_PREPARED",
    "ASSERTION_EXTERNALLY_REFRAMED",
    "ASSERTION_EXTERNALLY_TITLE_READY",
    "ASSERTION_EXTERNALLY_DESCRIPTION_READY",
    "ASSERTION_EXTERNALLY_HASHTAGS_READY",
    "ASSERTION_USER_MARKED_READY",
    "ImportOptions",
    "Declaration",
    "AssertionStore",
]


# Mesma lista de extensões de vídeo já usada em Geração 1
# (``agendar_youtube.py``, ``VIDEO_EXTS``) -- reproduzida aqui por
# referência de projeto (nunca importada: zero cross-import). Manter as
# duas listas em sincronia manual é uma decisão consciente, documentada
# aqui e em ``_sistema/source_import.py``'s docstring de módulo -- as duas
# camadas (Geração 1 e Geração 2) são deliberadamente independentes.
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"})

_FINGERPRINT_CHUNK_SIZE = 1024 * 1024  # 1 MiB, mesmo tamanho de Geração 1.
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_URL_TIMEOUT_SECONDS = 30.0

# Códigos de erro estruturados -- nunca uma exceção crua não tratada
# escapa de um importador; sempre um destes, em ``SourceImportError.code``.
ERROR_ARQUIVO_INEXISTENTE = "ARQUIVO_INEXISTENTE"
ERROR_NAO_E_ARQUIVO_REGULAR = "NAO_E_ARQUIVO_REGULAR"
ERROR_ARQUIVO_VAZIO = "ARQUIVO_VAZIO"
ERROR_EXTENSAO_INVALIDA = "EXTENSAO_INVALIDA"
ERROR_ARQUIVO_ILEGIVEL = "ARQUIVO_ILEGIVEL"
ERROR_PASTA_INEXISTENTE = "PASTA_INEXISTENTE"
ERROR_URL_INVALIDA = "URL_INVALIDA"
ERROR_NOME_ARQUIVO_NAO_RECONHECIVEL = "NOME_ARQUIVO_NAO_RECONHECIVEL"
ERROR_DOWNLOAD_FALHOU = "DOWNLOAD_FALHOU"
ERROR_RESPOSTA_HTTP_INVALIDA = "RESPOSTA_HTTP_INVALIDA"
ERROR_CONTEUDO_VAZIO = "CONTEUDO_VAZIO"


class SourceImportError(RuntimeError):
    """Falha estruturada de importação -- nunca uma exceção Python crua.

    ``code`` é sempre uma das constantes ``ERROR_*`` deste módulo,
    permitindo que o chamador (em particular ``FolderImporter``) distinga
    programaticamente o tipo de falha sem fazer parsing de mensagem.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def compute_fingerprint(path: Path) -> str:
    """SHA-256 de ``tamanho + primeiro 1 MiB + último 1 MiB`` do arquivo.

    Determinístico: o mesmo arquivo produz sempre o mesmo valor (base para
    dedup em rodadas futuras -- este Prompt não implementa dedup
    automático). Ver docstring do módulo para o trade-off vs. hashear o
    arquivo inteiro.
    """
    h = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode("ascii"))
    with path.open("rb") as f:
        h.update(f.read(_FINGERPRINT_CHUNK_SIZE))
        if size > _FINGERPRINT_CHUNK_SIZE:
            f.seek(max(0, size - _FINGERPRINT_CHUNK_SIZE))
            h.update(f.read(_FINGERPRINT_CHUNK_SIZE))
    return h.hexdigest()


def _validate_file_for_import(path: Path) -> None:
    """Validação de nível-ARQUIVO (item 1.5) -- nunca de mídia."""
    if not path.exists():
        raise SourceImportError(
            ERROR_ARQUIVO_INEXISTENTE, f"Arquivo nao encontrado: {path}"
        )
    if not path.is_file():
        raise SourceImportError(
            ERROR_NAO_E_ARQUIVO_REGULAR, f"Caminho nao e um arquivo regular: {path}"
        )
    if path.suffix.lower() not in VIDEO_EXTS:
        raise SourceImportError(
            ERROR_EXTENSAO_INVALIDA,
            f"Extensao nao permitida ({path.suffix!r}): {path}",
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SourceImportError(
            ERROR_ARQUIVO_ILEGIVEL, f"Nao foi possivel ler o arquivo: {path} ({exc})"
        ) from exc
    if size <= 0:
        raise SourceImportError(ERROR_ARQUIVO_VAZIO, f"Arquivo vazio: {path}")
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        raise SourceImportError(
            ERROR_ARQUIVO_ILEGIVEL, f"Nao foi possivel ler o arquivo: {path} ({exc})"
        ) from exc


# ---------------------------------------------------------------------------
# PROMPT 24b -- Import Options / User Assertions
# ---------------------------------------------------------------------------

# ``kind`` de uma declaração -- ver ``m007_source_asset_declarations.py``.
DECLARATION_KIND_ASSERTION = "ASSERTION"
DECLARATION_KIND_FLAG = "FLAG"
DECLARATION_KIND_LABEL = "LABEL"
DECLARATION_KINDS = frozenset(
    {DECLARATION_KIND_ASSERTION, DECLARATION_KIND_FLAG, DECLARATION_KIND_LABEL}
)

# Origem de uma declaração -- coluna obrigatória em toda escrita, nunca
# inferida na leitura (ver docstring do módulo). Este Prompt nunca grava
# ORIGIN_SYSTEM -- nenhuma evidência operacional automática existe ainda.
ORIGIN_SYSTEM = "SYSTEM"
ORIGIN_USER = "USER"
ORIGIN_IMPORT_PRESET = "IMPORT_PRESET"
DECLARATION_ORIGINS = frozenset({ORIGIN_SYSTEM, ORIGIN_USER, ORIGIN_IMPORT_PRESET})

# Vocabulário conhecido de EXTERNALLY_*/USER_MARKED_READY do roadmap --
# valores de exemplo para ``user_assertions``, nunca uma lista fechada
# imposta pelo módulo (``user_labels``/``user_flags`` são livres por
# natureza; ver docstring do módulo).
ASSERTION_EXTERNALLY_EDITED = "EXTERNALLY_EDITED"
ASSERTION_EXTERNALLY_CAPTIONED = "EXTERNALLY_CAPTIONED"
ASSERTION_EXTERNALLY_METADATA_PREPARED = "EXTERNALLY_METADATA_PREPARED"
ASSERTION_EXTERNALLY_REFRAMED = "EXTERNALLY_REFRAMED"
ASSERTION_EXTERNALLY_TITLE_READY = "EXTERNALLY_TITLE_READY"
ASSERTION_EXTERNALLY_DESCRIPTION_READY = "EXTERNALLY_DESCRIPTION_READY"
ASSERTION_EXTERNALLY_HASHTAGS_READY = "EXTERNALLY_HASHTAGS_READY"
ASSERTION_USER_MARKED_READY = "USER_MARKED_READY"

# Presets conceituais (roadmap) -- ver docstring do módulo para o
# mapeamento exato preset -> assertions/labels.
PRESET_ORIGINAL = "ORIGINAL"
PRESET_ALREADY_EDITED = "ALREADY_EDITED"
PRESET_READY_FOR_AI = "READY_FOR_AI"
PRESET_USER_MARKED_READY = "USER_MARKED_READY"
PRESET_CUSTOM = "CUSTOM"
PRESETS = frozenset(
    {
        PRESET_ORIGINAL,
        PRESET_ALREADY_EDITED,
        PRESET_READY_FOR_AI,
        PRESET_USER_MARKED_READY,
        PRESET_CUSTOM,
    }
)

# preset -> (assertions, labels) aplicadas com origin=IMPORT_PRESET.
# CUSTOM deliberadamente ausente daqui: "nenhum mapeamento automático" é o
# próprio comportamento (ver ``_expand_preset``).
_PRESET_ASSERTIONS: dict[str, frozenset[str]] = {
    PRESET_ORIGINAL: frozenset(),
    PRESET_ALREADY_EDITED: frozenset({ASSERTION_EXTERNALLY_EDITED}),
    PRESET_READY_FOR_AI: frozenset(),
    PRESET_USER_MARKED_READY: frozenset({ASSERTION_USER_MARKED_READY}),
}
_PRESET_LABELS: dict[str, frozenset[str]] = {
    PRESET_ORIGINAL: frozenset({PRESET_ORIGINAL}),
    PRESET_ALREADY_EDITED: frozenset(),
    PRESET_READY_FOR_AI: frozenset({PRESET_READY_FOR_AI}),
    PRESET_USER_MARKED_READY: frozenset(),
}


def _validate_declaration_uuid(value: str, field_name: str) -> None:
    """Mesma validação de UUID textual já usada em ``domain/models.py``
    (``_validate_uuid``) -- reproduzida aqui (não importada: é uma função
    privada de outro módulo) para ``target_project_id``/
    ``target_account_id``. Um UUID malformado é sempre rejeitado -- nunca
    aceito silenciosamente (item exigido pelo Prompt)."""
    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field_name} deve conter um UUID válido") from exc


def _normalize_str_tuple(values: "tuple[str, ...] | list[str] | None") -> tuple[str, ...]:
    if not values:
        return ()
    normalized: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            raise ValueError("declaração não pode ser uma string vazia")
        normalized.append(text)
    return tuple(normalized)


@dataclass(frozen=True)
class ImportOptions:
    """Pacote OPCIONAL de declarações que o chamador pode anexar a uma
    importação -- ver a seção "PROMPT 24b" da docstring do módulo para o
    contrato completo (alvo real ``SourceAsset``, distinção
    SYSTEM/USER/IMPORT_PRESET, mapeamento de presets, regra de conteúdo).

    Nunca obrigatório: todo importador continua funcionando exatamente
    como antes quando ``options`` é omitido/``None``.
    """

    preset: str | None = None
    user_assertions: tuple[str, ...] = ()
    user_flags: tuple[str, ...] = ()
    user_labels: tuple[str, ...] = ()
    target_project_id: str | None = None
    target_account_id: str | None = None
    readiness_profile_id: str | None = None

    def __post_init__(self) -> None:
        # GATE 8 (construção dos objetos): tudo é validado ANTES de
        # qualquer uso -- um ``ImportOptions`` que falha ao construir
        # nunca fica parcialmente válido.
        if self.preset is not None and self.preset not in PRESETS:
            raise ValueError(
                f"preset desconhecido: {self.preset!r} (esperado um de {sorted(PRESETS)})"
            )
        object.__setattr__(self, "user_assertions", _normalize_str_tuple(self.user_assertions))
        object.__setattr__(self, "user_flags", _normalize_str_tuple(self.user_flags))
        object.__setattr__(self, "user_labels", _normalize_str_tuple(self.user_labels))
        if self.target_project_id is not None:
            _validate_declaration_uuid(self.target_project_id, "target_project_id")
        if self.target_account_id is not None:
            _validate_declaration_uuid(self.target_account_id, "target_account_id")

    def _preset_assertions(self) -> frozenset[str]:
        if self.preset is None or self.preset == PRESET_CUSTOM:
            return frozenset()
        return _PRESET_ASSERTIONS.get(self.preset, frozenset())

    def _preset_labels(self) -> frozenset[str]:
        if self.preset is None or self.preset == PRESET_CUSTOM:
            return frozenset()
        return _PRESET_LABELS.get(self.preset, frozenset())


@dataclass(frozen=True)
class Declaration:
    """Estado ATIVO (derivado) de uma declaração para um ``SourceAsset``
    -- nunca um evento bruto da tabela append-only. Ver
    ``AssertionStore.list_active``/``list_by_origin``."""

    source_asset_id: str
    kind: str
    value: str
    origin: str
    created_at: str


class AssertionStore:
    """Persiste e consulta declarações (``ImportOptions``/presets/API
    manual) associadas a um ``SourceAsset`` -- tabela append-only
    ``source_asset_declarations`` (``m007_source_asset_declarations.py``).

    Ortogonal ao resto do módulo: nunca cria ``Job``/``Publication``/
    ``Schedule``/``Project``/``Video`` -- só grava/lê eventos de
    declaração associados a um ``source_asset_id`` já existente.
    """

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    # -- escrita -----------------------------------------------------

    def remove(
        self,
        source_asset_ids: "list[str] | tuple[str, ...] | str",
        *,
        kind: str,
        values: "list[str] | tuple[str, ...]",
        origin: str = ORIGIN_USER,
    ) -> None:
        """Grava um evento ``REMOVE`` -- NUNCA um ``UPDATE``/``DELETE`` do
        evento ``ADD`` original (tabela append-only). O histórico
        completo (quando foi declarada, quando foi removida) permanece
        reconstruível para sempre. ``origin`` aqui identifica quem pediu
        a remoção -- não precisa coincidir com a origem da declaração
        original."""
        if kind not in DECLARATION_KINDS:
            raise ValueError(f"kind desconhecido: {kind!r} (esperado um de {sorted(DECLARATION_KINDS)})")
        if origin not in DECLARATION_ORIGINS:
            raise ValueError(f"origin desconhecido: {origin!r} (esperado um de {sorted(DECLARATION_ORIGINS)})")
        ids = (source_asset_ids,) if isinstance(source_asset_ids, str) else tuple(source_asset_ids)
        vals = _normalize_str_tuple(tuple(values))
        entries = [(kind, value, origin) for value in vals]
        self._write_batch(ids, entries, action="REMOVE")

    def apply_assertions(
        self,
        source_asset_ids: "list[str] | tuple[str, ...] | str",
        assertions: "list[str] | tuple[str, ...]",
        *,
        origin: str = ORIGIN_USER,
    ) -> None:
        ids = (source_asset_ids,) if isinstance(source_asset_ids, str) else tuple(source_asset_ids)
        self.apply(ids, kind=DECLARATION_KIND_ASSERTION, values=assertions, origin=origin)

    def apply_flags(
        self,
        source_asset_ids: "list[str] | tuple[str, ...] | str",
        flags: "list[str] | tuple[str, ...]",
        *,
        origin: str = ORIGIN_USER,
    ) -> None:
        ids = (source_asset_ids,) if isinstance(source_asset_ids, str) else tuple(source_asset_ids)
        self.apply(ids, kind=DECLARATION_KIND_FLAG, values=flags, origin=origin)

    def apply_labels(
        self,
        source_asset_ids: "list[str] | tuple[str, ...] | str",
        labels: "list[str] | tuple[str, ...]",
        *,
        origin: str = ORIGIN_USER,
    ) -> None:
        ids = (source_asset_ids,) if isinstance(source_asset_ids, str) else tuple(source_asset_ids)
        self.apply(ids, kind=DECLARATION_KIND_LABEL, values=labels, origin=origin)

    def remove_assertion(
        self,
        source_asset_id: str,
        assertion: str,
        *,
        origin: str = ORIGIN_USER,
    ) -> None:
        self.remove((source_asset_id,), kind=DECLARATION_KIND_ASSERTION, values=(assertion,), origin=origin)

    def apply_options(self, source_asset_ids: "list[str] | tuple[str, ...]", options: "ImportOptions | None") -> None:
        """Aplica um ``ImportOptions`` inteiro a um lote de
        ``SourceAsset``s -- usado pelos três importadores quando
        ``options`` é fornecido (item 1.2: "aplicar a todos durante
        importação"), e reutilizável fora do fluxo de import (item 1.2:
        "somente aos selecionados").

        GATE ADVERSARIAL 3 (crash windows): as até cinco categorias de
        declaração de um ``ImportOptions`` (preset-assertions,
        preset-labels, user_assertions, user_flags, user_labels) são
        gravadas em UMA ÚNICA transaction (``_write_batch``), nunca uma
        chamada separada por categoria -- um processo morto no meio da
        aplicação de um ``ImportOptions`` nunca deixa um subconjunto
        parcial das categorias persistido enquanto outras ficam de fora;
        ou tudo o que ``options`` descreve é gravado, ou nada é.
        """
        if options is None or not source_asset_ids:
            return
        entries: list[tuple[str, str, str]] = []
        entries.extend(
            (DECLARATION_KIND_ASSERTION, value, ORIGIN_IMPORT_PRESET)
            for value in options._preset_assertions()
        )
        entries.extend(
            (DECLARATION_KIND_LABEL, value, ORIGIN_IMPORT_PRESET)
            for value in options._preset_labels()
        )
        entries.extend((DECLARATION_KIND_ASSERTION, value, ORIGIN_USER) for value in options.user_assertions)
        entries.extend((DECLARATION_KIND_FLAG, value, ORIGIN_USER) for value in options.user_flags)
        entries.extend((DECLARATION_KIND_LABEL, value, ORIGIN_USER) for value in options.user_labels)
        self._write_batch(source_asset_ids, entries, action="ADD")

    def apply(
        self,
        source_asset_ids: "list[str] | tuple[str, ...]",
        *,
        kind: str,
        values: "list[str] | tuple[str, ...]",
        origin: str,
    ) -> None:
        """Grava um evento ``ADD`` para cada combinação
        (``source_asset_id``, ``value``) -- "aplicar a um vídeo",
        "aplicar a múltiplos vídeos" e "aplicar a todos durante
        importação" do roadmap são todos este mesmo método, variando
        apenas o tamanho de ``source_asset_ids``. Toda a combinação é
        gravada em uma única transaction."""
        if kind not in DECLARATION_KINDS:
            raise ValueError(f"kind desconhecido: {kind!r} (esperado um de {sorted(DECLARATION_KINDS)})")
        if origin not in DECLARATION_ORIGINS:
            raise ValueError(f"origin desconhecido: {origin!r} (esperado um de {sorted(DECLARATION_ORIGINS)})")
        vals = _normalize_str_tuple(tuple(values))
        entries = [(kind, value, origin) for value in vals]
        self._write_batch(source_asset_ids, entries, action="ADD")

    def _write_batch(
        self,
        source_asset_ids: "list[str] | tuple[str, ...]",
        entries: "list[tuple[str, str, str]]",
        *,
        action: str,
    ) -> None:
        """Grava QUALQUER combinação de (kind, value, origin) -- possivelmente
        de várias categorias diferentes -- para todos os
        ``source_asset_ids``, em UMA ÚNICA transaction SQLite. É o único
        ponto de escrita real da tabela append-only; ``apply``/
        ``apply_options``/``remove`` são todos, no fim, chamadas a este
        método."""
        ids = tuple(source_asset_ids)
        if not ids or not entries:
            return
        for source_asset_id in ids:
            _validate_declaration_uuid(source_asset_id, "source_asset_id")
        for kind, _value, origin in entries:
            if kind not in DECLARATION_KINDS:
                raise ValueError(f"kind desconhecido: {kind!r} (esperado um de {sorted(DECLARATION_KINDS)})")
            if origin not in DECLARATION_ORIGINS:
                raise ValueError(f"origin desconhecido: {origin!r} (esperado um de {sorted(DECLARATION_ORIGINS)})")
        timestamp = utc_now_iso()
        rows = [
            (new_uuid(), source_asset_id, kind, value, origin, action, timestamp)
            for source_asset_id in ids
            for kind, value, origin in entries
        ]
        with self.database.transaction() as conn:
            conn.executemany(
                "INSERT INTO source_asset_declarations "
                "(id, source_asset_id, kind, value, origin, action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    # -- leitura -------------------------------------------------------

    def list_active(self, source_asset_id: str, *, kind: str | None = None) -> tuple[Declaration, ...]:
        """Estado atual DERIVADO (nunca eventos brutos): para cada chave
        lógica (``kind``, ``value``), o evento mais recente decide se a
        declaração está ativa (``action='ADD'``) ou não
        (``action='REMOVE'``)."""
        _validate_declaration_uuid(source_asset_id, "source_asset_id")
        sql = (
            "SELECT kind, value, origin, created_at FROM ("
            "  SELECT kind, value, origin, action, created_at,"
            "         ROW_NUMBER() OVER ("
            "             PARTITION BY source_asset_id, kind, value"
            "             ORDER BY rowid DESC"
            "         ) AS rn"
            "  FROM source_asset_declarations"
            "  WHERE source_asset_id = ?"
            ") WHERE rn = 1 AND action = 'ADD'"
        )
        params: list[Any] = [source_asset_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY kind, value"
        with self.database.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return tuple(
            Declaration(
                source_asset_id=source_asset_id,
                kind=row["kind"],
                value=row["value"],
                origin=row["origin"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def list_by_origin(self, origin: str, *, source_asset_id: str | None = None) -> tuple[Declaration, ...]:
        """Estado atual (derivado) filtrado por origem -- prova, junto com
        ``list_active``, que uma declaração ``USER``/``IMPORT_PRESET``
        nunca aparece numa consulta de origem ``SYSTEM``, e vice-versa
        (a origem é uma coluna gravada, nunca inferida)."""
        if origin not in DECLARATION_ORIGINS:
            raise ValueError(f"origin desconhecido: {origin!r} (esperado um de {sorted(DECLARATION_ORIGINS)})")
        sql = (
            "SELECT source_asset_id, kind, value, origin, created_at FROM ("
            "  SELECT source_asset_id, kind, value, origin, action, created_at,"
            "         ROW_NUMBER() OVER ("
            "             PARTITION BY source_asset_id, kind, value"
            "             ORDER BY rowid DESC"
            "         ) AS rn"
            "  FROM source_asset_declarations"
        )
        params: list[Any] = []
        if source_asset_id is not None:
            sql += " WHERE source_asset_id = ?"
            params.append(source_asset_id)
        sql += ") WHERE rn = 1 AND action = 'ADD' AND origin = ?"
        params.append(origin)
        sql += " ORDER BY source_asset_id, kind, value"
        with self.database.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return tuple(
            Declaration(
                source_asset_id=row["source_asset_id"],
                kind=row["kind"],
                value=row["value"],
                origin=row["origin"],
                created_at=row["created_at"],
            )
            for row in rows
        )


def _apply_reference_fields(asset: SourceAsset, options: "ImportOptions | None") -> None:
    """Grava ``target_project_id``/``target_account_id``/
    ``readiness_profile_id`` (quando presentes) em ``SourceAsset.extra``.

    Estes três campos são REFERÊNCIAS OPACAS/escalares (um valor único,
    sem origem/histórico) -- diferente de ``user_assertions``/
    ``user_flags``/``user_labels``, que são declarações repetíveis com
    origem rastreável e vão para ``source_asset_declarations``
    (``AssertionStore``). Não existe coluna dedicada em ``sources`` para
    eles (não reabrimos o schema já aprovado de ``SourceAsset`` para
    isto); ``extra`` já é o mecanismo genérico do projeto para campos
    associados a uma entidade sem exigir migration nova, e permanece
    ``{}`` (comportamento idêntico ao já aprovado) quando nenhuma
    referência é fornecida.
    """
    if options is None:
        return
    references: dict[str, str] = {}
    if options.target_project_id is not None:
        references["target_project_id"] = options.target_project_id
    if options.target_account_id is not None:
        references["target_account_id"] = options.target_account_id
    if options.readiness_profile_id is not None:
        references["readiness_profile_id"] = options.readiness_profile_id
    if references:
        asset.extra["import_options"] = references


@dataclass(frozen=True)
class ImportResult:
    """Resultado de UMA importação -- o mesmo formato para os três
    importadores (``LocalFileImporter``/``UrlImporter`` devolvem um único
    ``ImportResult``; ``FolderImporter`` devolve ``list[ImportResult]``,
    um por arquivo descoberto)."""

    success: bool
    source_uri: str
    source_asset: SourceAsset | None = None
    error_code: str | None = None
    error_message: str | None = None


class LocalFileImporter:
    """Importa um arquivo já presente no disco do usuário.

    REFERENCIA o arquivo (``local_path`` = caminho original, nunca
    copiado) -- ver docstring do módulo, seção de armazenamento.
    """

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database
        self.assertion_store = AssertionStore(database)
        self.context_resolver = SourceContextResolver(database)

    def import_source(self, file_path: "Path | str", options: "ImportOptions | None" = None) -> ImportResult:
        path = Path(file_path)
        source_uri = str(path)
        try:
            _validate_file_for_import(path)
            resolved = path.resolve()
            fingerprint = compute_fingerprint(resolved)
            size_bytes = resolved.stat().st_size
            asset = SourceAsset(
                source_uri=str(resolved),
                local_path=str(resolved),
                original_name=resolved.name,
                fingerprint=fingerprint,
                media_type="video",
                size_bytes=size_bytes,
            )
            _apply_reference_fields(asset, options)
            self.database.insert(asset)
            self.assertion_store.apply_options((asset.id,), options)
            # PROMPT 25 -- classificação automática, invisível ao usuário
            # (ver docstring do módulo, seção "SOURCE CONTEXT RESOLVER").
            # O SourceAsset ja foi persistido com sucesso acima; uma falha
            # inesperada aqui nunca pode transformar este resultado em
            # falha nem impedir o retorno do ImportResult ja construido --
            # unica excecao generica deliberada deste modulo.
            try:
                self.context_resolver.resolve_and_persist(asset.id)
            except Exception:
                pass
            return ImportResult(success=True, source_uri=source_uri, source_asset=asset)
        except SourceImportError as exc:
            return ImportResult(
                success=False,
                source_uri=source_uri,
                error_code=exc.code,
                error_message=str(exc),
            )


class FolderImporter:
    """Descobre e importa todos os arquivos elegíveis de uma pasta.

    Isolamento por item (item 1.6): um arquivo problemático nunca
    interrompe os demais -- cada arquivo produz seu próprio
    ``ImportResult`` (sucesso ou falha), nunca uma exceção que aborta o
    lote inteiro. A falha de nível-pasta (pasta inexistente) é a única
    exceção que propaga: não há nada para iterar.
    """

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database
        self._file_importer = LocalFileImporter(database)

    def import_source(self, folder_path: "Path | str", options: "ImportOptions | None" = None) -> list[ImportResult]:
        folder = Path(folder_path)
        if not folder.exists() or not folder.is_dir():
            raise SourceImportError(
                ERROR_PASTA_INEXISTENTE, f"Pasta nao encontrada: {folder}"
            )
        results: list[ImportResult] = []
        for entry in sorted(folder.iterdir(), key=lambda p: p.name):
            if not entry.is_file():
                continue
            try:
                # ``options`` (se fornecido) se aplica a TODOS os arquivos
                # importados com sucesso desta pasta (item 1.2) -- a
                # associação em si acontece dentro de
                # ``LocalFileImporter.import_source``, então cada arquivo
                # recebe exatamente as mesmas declarações.
                results.append(self._file_importer.import_source(entry, options))
            except OSError as exc:
                # Falha de I/O inesperada neste arquivo especificamente
                # (ex.: permissão negada ao enumerar) -- isolada, nunca
                # interrompe os demais arquivos da pasta.
                results.append(
                    ImportResult(
                        success=False,
                        source_uri=str(entry),
                        error_code=ERROR_ARQUIVO_ILEGIVEL,
                        error_message=f"Falha de I/O ao processar {entry}: {exc}",
                    )
                )
        return results


class UrlImporter:
    """Baixa o conteúdo de uma URL http(s) via GET simples e direto.

    Ver docstring do módulo: NUNCA autenticação, cookies, sessão ou
    extração específica de site -- estruturalmente impossível, não apenas
    "não testado". O conteúdo baixado é persistido em
    ``AppPaths.sources`` (obrigatório: não existe "arquivo original
    local" para uma URL).
    """

    def __init__(self, database: LocalDatabase, app_paths: AppPaths) -> None:
        self.database = database
        self.app_paths = app_paths
        self.assertion_store = AssertionStore(database)
        self.context_resolver = SourceContextResolver(database)

    def import_source(self, url: str, options: "ImportOptions | None" = None) -> ImportResult:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise SourceImportError(
                    ERROR_URL_INVALIDA,
                    f"URL invalida (somente http/https sao aceitos): {url}",
                )
            original_name = Path(parsed.path).name
            extension = Path(original_name).suffix.lower()
            if not original_name or extension not in VIDEO_EXTS:
                raise SourceImportError(
                    ERROR_NOME_ARQUIVO_NAO_RECONHECIVEL,
                    f"Nao foi possivel determinar um nome de arquivo com "
                    f"extensao permitida a partir da URL: {url}",
                )

            self.app_paths.sources.mkdir(parents=True, exist_ok=True)
            destination = self.app_paths.sources / f"{uuid4()}_{original_name}"

            # GET simples e direto -- ``urlopen`` recebe a URL como string
            # pura, nunca um objeto ``Request`` com headers customizados.
            # Nenhum cookiejar, nenhuma sessão, nenhum header de
            # autenticação: não há caminho de código para nenhum deles.
            try:
                with urllib.request.urlopen(url, timeout=_URL_TIMEOUT_SECONDS) as response:
                    status = getattr(response, "status", 200)
                    if status is not None and not (200 <= int(status) < 300):
                        raise SourceImportError(
                            ERROR_RESPOSTA_HTTP_INVALIDA,
                            f"Resposta HTTP nao-2xx ({status}) para {url}",
                        )
                    total_bytes = 0
                    with destination.open("wb") as out:
                        while True:
                            chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                            if not chunk:
                                break
                            out.write(chunk)
                            total_bytes += len(chunk)
            except urllib.error.HTTPError as exc:
                destination.unlink(missing_ok=True)
                raise SourceImportError(
                    ERROR_RESPOSTA_HTTP_INVALIDA,
                    f"Resposta HTTP {exc.code} para {url}",
                ) from exc
            except urllib.error.URLError as exc:
                destination.unlink(missing_ok=True)
                raise SourceImportError(
                    ERROR_DOWNLOAD_FALHOU, f"Falha ao baixar {url}: {exc}"
                ) from exc
            except OSError as exc:
                destination.unlink(missing_ok=True)
                raise SourceImportError(
                    ERROR_DOWNLOAD_FALHOU, f"Falha de I/O ao baixar {url}: {exc}"
                ) from exc

            if total_bytes <= 0:
                destination.unlink(missing_ok=True)
                raise SourceImportError(
                    ERROR_CONTEUDO_VAZIO, f"Conteudo vazio recebido de {url}"
                )

            fingerprint = compute_fingerprint(destination)
            asset = SourceAsset(
                source_uri=url,
                local_path=str(destination),
                original_name=original_name,
                fingerprint=fingerprint,
                media_type="video",
                size_bytes=total_bytes,
            )
            _apply_reference_fields(asset, options)
            self.database.insert(asset)
            self.assertion_store.apply_options((asset.id,), options)
            # PROMPT 25 -- ver LocalFileImporter.import_source: mesmo
            # isolamento deliberado (excecao generica documentada).
            try:
                self.context_resolver.resolve_and_persist(asset.id)
            except Exception:
                pass
            return ImportResult(success=True, source_uri=url, source_asset=asset)
        except SourceImportError as exc:
            return ImportResult(
                success=False,
                source_uri=url,
                error_code=exc.code,
                error_message=str(exc),
            )


class SourceImportManager:
    """Fachada que expõe os três importadores por um único ponto de
    entrada -- escolhe/despacha para o importador certo conforme o tipo
    de origem."""

    def __init__(self, database: LocalDatabase, app_paths: AppPaths) -> None:
        self.database = database
        self.app_paths = app_paths
        self.local_file_importer = LocalFileImporter(database)
        self.folder_importer = FolderImporter(database)
        self.url_importer = UrlImporter(database, app_paths)
        # Mesmo ``AssertionStore`` (mesmo ``LocalDatabase``) usado
        # internamente pelos três importadores -- exposto aqui para
        # aplicar/remover declarações FORA do fluxo de import (item 1.4:
        # "somente aos selecionados", "remover depois", "editar em lote
        # posteriormente").
        self.assertion_store = AssertionStore(database)
        # PROMPT 25 -- mesmo ``SourceContextResolver`` (mesmo
        # ``LocalDatabase``) usado internamente por ``LocalFileImporter``/
        # ``UrlImporter`` -- exposto aqui para consultar/reclassificar
        # manualmente fora do fluxo automático de import.
        self.context_resolver = SourceContextResolver(database)

    def import_local_file(self, file_path: "Path | str", options: "ImportOptions | None" = None) -> ImportResult:
        return self.local_file_importer.import_source(file_path, options)

    def import_folder(self, folder_path: "Path | str", options: "ImportOptions | None" = None) -> list[ImportResult]:
        return self.folder_importer.import_source(folder_path, options)

    def import_url(self, url: str, options: "ImportOptions | None" = None) -> ImportResult:
        return self.url_importer.import_source(url, options)

    def apply_assertions(
        self,
        source_asset_ids: "list[str] | tuple[str, ...] | str",
        assertions: "list[str] | tuple[str, ...]",
        *,
        origin: str = ORIGIN_USER,
    ) -> None:
        """API pública para aplicar declarações a um subconjunto JÁ
        SELECIONADO de ``SourceAsset``s, fora do fluxo de import (item
        1.4)."""
        self.assertion_store.apply_assertions(source_asset_ids, assertions, origin=origin)

    def remove_assertion(self, source_asset_id: str, assertion: str, *, origin: str = ORIGIN_USER) -> None:
        """API pública para remover uma declaração já aplicada (item 1.4:
        "remover depois")."""
        self.assertion_store.remove_assertion(source_asset_id, assertion, origin=origin)

    def get_context(self, source_asset_id: str):
        """API pública (PROMPT 25) para consultar a classificação de
        contexto atual de um ``SourceAsset`` -- ``None`` quando ausente
        (ver ``SourceContextResolver.get_current``)."""
        return self.context_resolver.get_current(source_asset_id)
