# -*- coding: utf-8 -*-
"""PROMPT 25 -- Source Context Resolver: classificação técnica automática
e invisível de contexto para ``SourceAsset``.

TEXTO LITERAL DO ROADMAP (implementado exatamente, ver seção 0/1 do
Prompt):

    Crie SourceContextResolver invisível ao usuário.

    Para conteúdo de plataforma:
    - tentar identificar canal/origem;
    - verificar se corresponde a conta já conectada;
    - classificar contexto técnico automaticamente.

    Exemplos internos:
    CONNECTED_CHANNEL_CONTENT
    EXTERNAL_CONTENT
    LOCAL_CONTENT
    UNKNOWN_SOURCE

    Não perguntar: "esse vídeo é seu?"

    Essa classificação não é determinação jurídica de propriedade.

    Ela serve apenas para selecionar automaticamente o melhor pipeline
    técnico.

GARANTIA CENTRAL -- NUNCA DETERMINAÇÃO JURÍDICA DE PROPRIEDADE: as quatro
constantes deste módulo (``CONTEXT_*``) são um sinal TÉCNICO/HEURÍSTICO
usado apenas para escolher pipeline, nunca uma alegação factual/legal
sobre quem é dono do conteúdo -- mesmo espírito da distinção já
estabelecida no Prompt 24b entre ``SYSTEM_BADGE`` (evidência automática do
produto) e ``USER_ASSERTION`` (declaração do usuário): nenhuma das duas é
prova jurídica, e esta classificação também não é.

GARANTIA ESTRUTURAL -- NENHUMA PERGUNTA AO USUÁRIO: este módulo não tem
UI, não é invocado por nada interativo, não aceita nenhum input do
usuário e não grava nenhum campo de "confirmação de propriedade". A
garantia é por AUSÊNCIA de qualquer código de interação (mesmo princípio
já usado para ``UrlImporter`` nunca ter caminho de código para
autenticação) -- verificada em teste (``tests/test_source_context.py``).

GARANTIA ESTRUTURAL -- NENHUMA CHAMADA DE REDE: toda a classificação usa
exclusivamente dados já locais (``SourceAsset.source_uri``, ``Account``s
já persistidas no SQLite local). Este módulo nunca importa
``urllib.request``/``http.client``/``socket``/bibliotecas de rede
equivalentes -- apenas ``urllib.parse.urlparse`` para inspecionar a
STRING da URL (nunca abrir uma conexão). Garantido por teste AST
(``tests/test_source_context.py``).

===========================================================================
0. INVESTIGAÇÃO -- POR QUE ``CONNECTED_CHANNEL_CONTENT`` É SEMPRE
   HEURÍSTICO (nunca uma correspondência verificada/autoritativa)
===========================================================================

Confirmado por leitura de ``_sistema/storage/legacy_migration.py``
(``_account_entity``) e por grep em todo o projeto: ``Account.
external_account_id`` NUNCA é populado -- é sempre ``None``, tanto na
migração de contas legadas quanto em qualquer outro caminho de código.
``Account.name``/``local_key`` vêm de ``nome_conta``/``nome_canal`` do
``config_canal.json`` de Geração 1: um NOME ESCOLHIDO PELO USUÁRIO ao
cadastrar a conta (o login é feito abrindo o Chrome de verdade com perfil
persistente; o produto nunca chama a API do YouTube/TikTok para confirmar
identidade de canal). CONSEQUÊNCIA OBRIGATÓRIA, aplicada em todo este
módulo: ``CONNECTED_CHANNEL_CONTENT`` é sempre uma correspondência
HEURÍSTICA (comparação textual entre o handle/slug extraído da URL e
``Account.name``/``local_key`` de contas habilitadas daquela plataforma),
nunca verificada por uma API real. Qualquer ambiguidade (zero ou mais de
uma conta batendo) NUNCA classifica como ``CONNECTED_CHANNEL_CONTENT`` --
cai em ``EXTERNAL_CONTENT``. Errar para o lado conservador é obrigatório:
uma classificação ``CONNECTED_CHANNEL_CONTENT`` incorreta tem custo maior
(escolhe o pipeline errado presumindo canal próprio) do que uma
``UNKNOWN_SOURCE``/``EXTERNAL_CONTENT`` correta.

Confirmado por grep: não existe hoje nenhuma lista reutilizável de
domínios de plataforma no projeto (``youtube.com``/``tiktok.com``
aparecem só como URLs de upload/login hardcoded em
``agendar_youtube.py``/``login_conta.py``/``painel_oficial.py``, nunca
como uma constante compartilhada). ``RECOGNIZED_PLATFORM_DOMAINS`` abaixo
é a lista própria deste módulo -- mesmo espírito de ``VIDEO_EXTS`` em
``source_import.py``: uma constante documentada, reproduzida aqui, nunca
importada de Geração 1 (zero cross-import).

``SourceAsset.source_uri`` (Prompt 24) é o dado de entrada natural:
``LocalFileImporter``/``FolderImporter`` gravam o CAMINHO LOCAL resolvido
(nunca uma URL); ``UrlImporter`` grava a URL http(s) original. A
distinção LOCAL_CONTENT vs. conteúdo-de-URL já está implícita nesse dado
existente -- nenhum campo novo foi adicionado a ``SourceAsset`` para
isto (confirmado por leitura de ``_sistema/source_import.py`` antes de
decidir).

===========================================================================
1. ONDE PERSISTIR A CLASSIFICAÇÃO
===========================================================================

Ver ``_sistema/storage/migrations/m008_source_asset_context.py`` para a
decisão de desenho completa (tabela nova dedicada ``source_asset_context``,
opção "b" das duas descritas pelo Prompt, com justificativa detalhada de
por que estender ``source_asset_declarations``/m007 -- opção "a" -- foi
avaliada e rejeitada).

===========================================================================
2. REGRAS DE CLASSIFICAÇÃO (item 1.3 do Prompt, implementadas exatamente)
===========================================================================

- ``source_uri`` SEM esquema http(s) (caminho de arquivo local --
  ``LocalFileImporter``/``FolderImporter``) -> sempre ``LOCAL_CONTENT``.
  Nunca depende de conta conectada, nunca falha por caminho "malformado"
  (qualquer string sem esquema http(s) é, por definição desta regra, um
  caminho local).
- ``source_uri`` COM esquema http(s) mas SEM host parseável (``netloc``
  vazio -- ex. ``https:///caminho``) -> ``UNKNOWN_SOURCE``. Única forma
  legítima de cair aqui, junto de qualquer erro inesperado do próprio
  resolver (ver seção 3 abaixo).
- Host presente: se bater com um domínio de plataforma reconhecido
  (``RECOGNIZED_PLATFORM_DOMAINS``) E for possível extrair um handle/slug
  de canal da URL (``_extract_platform_handle``) E existir EXATAMENTE UMA
  ``Account`` habilitada (``enabled=True``) daquela plataforma cujo
  ``name``/``local_key`` bate heuristicamente (``_normalize_for_comparison``
  -- normalização determinística: remove espaços nas pontas, remove um
  ``@`` inicial, converte para minúsculas) -> ``CONNECTED_CHANNEL_CONTENT``.
- Host de plataforma reconhecido, mas ZERO ou MAIS DE UMA conta batendo
  (ambíguo), OU handle/slug não extraível da URL -> ``EXTERNAL_CONTENT``.
  Decisão consciente: em ambos os casos ainda sabemos que é conteúdo de
  uma plataforma reconhecida, só não de uma conta conectada de forma
  inequívoca -- tratado como o mesmo caso ("não é canal próprio
  confirmável"), nunca como ambiguidade adicional a distinguir.
- Host que NÃO bate com nenhum domínio de plataforma reconhecido (URL
  genérica qualquer) -> ``EXTERNAL_CONTENT`` também -- é conteúdo externo
  de qualquer forma; não há necessidade de uma quinta categoria para
  "externo mas de plataforma desconhecida" vs. "externo mas de plataforma
  conhecida sem conta batendo": ambos resultam no mesmo pipeline técnico
  (não é conteúdo local, não é de conta conectada).

===========================================================================
3. ISOLAMENTO -- FALHA DO RESOLVER NUNCA DERRUBA A IMPORTAÇÃO
===========================================================================

O ``SourceAsset`` já foi importado e persistido com sucesso ANTES do
resolver rodar (ver ``_sistema/source_import.py``: a chamada acontece
depois de ``database.insert(asset)``/``assertion_store.apply_options``).
O ponto de integração em ``LocalFileImporter``/``UrlImporter`` envolve a
chamada a ``SourceContextResolver.resolve_and_persist`` num
``try/except Exception`` explícito e documentado -- uma exceção
inesperada durante a classificação (erro de leitura do banco, bug de
parsing, o que for) NUNCA propaga para o chamador do importador:
``ImportResult.success`` continua ``True``, o ``SourceAsset`` continua
persistido, e simplesmente NENHUMA linha de classificação é gravada para
aquele ``source_asset_id`` (estado "ausente" -- distinto de
``UNKNOWN_SOURCE`` explícito, que é gravado quando a classificação RODOU
e concluiu que o resultado é ``UNKNOWN_SOURCE``; "ausente" significa que a
classificação nem chegou a concluir). ``get_current`` devolve ``None``
para "ausente", nunca confundido com a string ``"UNKNOWN_SOURCE"``. Este é
o mesmo princípio de isolamento por item já usado em toda a Geração 2 (ex.
``FolderImporter`` -- um arquivo problemático nunca compromete os demais).

``FolderImporter`` delega inteiramente a ``LocalFileImporter.import_source``
por arquivo (ver ``source_import.py``) -- o isolamento acima já cobre
``FolderImporter`` automaticamente, sem nenhuma integração adicional
neste módulo ou naquele: uma falha de classificação no arquivo 1 de 50
não afeta a classificação nem o resultado dos demais 49.

===========================================================================
3b. CONCORRÊNCIA (GATE adversarial item 6, considerado e dispensado com
    justificativa -- não silenciosamente ignorado)
===========================================================================

``resolve_and_persist`` NÃO precisa de uma transação única cobrindo
"ler o SourceAsset/Accounts, decidir, gravar" (ao contrário de operações
que impõem uma invariante de unicidade, ex. "claim" de um Job): cada
chamada apenas ANEXA um novo evento de classificação (INSERT puro na
tabela append-only ``source_asset_context``), nunca lê o estado anterior
para decidir SE deve escrever -- sempre escreve. Duas instâncias
chamando ``resolve_and_persist`` para o mesmo ``source_asset_id`` ao
mesmo tempo produzem duas linhas (nunca um conflito/erro): cada uma
reflete fielmente a classificação calculada a partir do estado que aquela
chamada específica observou, e ``get_current`` (maior ``rowid``) resolve
deterministicamente para uma das duas -- nunca um estado corrompido ou
parcialmente escrito, mesmo padrão commutativo já usado por
``AssertionStore._write_batch``/m007. Testado explicitamente em
``tests/test_source_context.py`` com duas threads reais contra o mesmo
SQLite, sincronizadas por ``threading.Barrier`` (determinístico, nunca
probabilístico).

===========================================================================
4. ORTOGONALIDADE
===========================================================================

Este módulo nunca importa nem chama
``circuit_breaker``/``retry_policy``/``publication_idempotency``/
``secrets_manager``/``domain.job_state_machine`` -- camadas
completamente diferentes. Nunca cria ``Job``/``Publication``/
``Schedule``/``Project``/``Video`` -- termina exatamente em "classificar e
persistir uma classificação para um ``SourceAsset`` já existente".
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import ParseResult, urlparse
from uuid import UUID

from .domain import Account, SourceAsset, new_uuid
from .storage.database import LocalDatabase
from .time_utils import utc_now_iso

__all__ = [
    "CONTEXT_CONNECTED_CHANNEL_CONTENT",
    "CONTEXT_EXTERNAL_CONTENT",
    "CONTEXT_LOCAL_CONTENT",
    "CONTEXT_UNKNOWN_SOURCE",
    "CONTEXTS",
    "RECOGNIZED_PLATFORM_DOMAINS",
    "SourceContext",
    "SourceContextResolver",
]

# ---------------------------------------------------------------------------
# Vocabulário -- exatamente as quatro constantes do roadmap, sem variação.
# ---------------------------------------------------------------------------

CONTEXT_CONNECTED_CHANNEL_CONTENT = "CONNECTED_CHANNEL_CONTENT"
CONTEXT_EXTERNAL_CONTENT = "EXTERNAL_CONTENT"
CONTEXT_LOCAL_CONTENT = "LOCAL_CONTENT"
CONTEXT_UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
CONTEXTS = frozenset(
    {
        CONTEXT_CONNECTED_CHANNEL_CONTENT,
        CONTEXT_EXTERNAL_CONTENT,
        CONTEXT_LOCAL_CONTENT,
        CONTEXT_UNKNOWN_SOURCE,
    }
)

# Lista PRÓPRIA deste módulo (nunca importada de Geração 1 -- ver seção 0
# da docstring do módulo). Hosts exatos, em minúsculas -- comparação
# determinística por igualdade de string, sem heurística adicional de
# subdomínio (um subdomínio não listado explicitamente não é reconhecido;
# adicionar um novo é uma decisão consciente, feita aqui).
RECOGNIZED_PLATFORM_DOMAINS: dict[str, str] = {
    "youtube.com": "youtube",
    "www.youtube.com": "youtube",
    "m.youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
}


def _validate_source_asset_id(value: str) -> None:
    """Mesma validação de UUID textual reproduzida (nunca importada) em
    ``source_import.py``/``domain/models.py`` -- consistente com o
    restante do projeto, sem criar dependência entre os dois módulos."""
    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("source_asset_id deve conter um UUID válido") from exc


def _normalize_for_comparison(text: str) -> str:
    """Normalização determinística usada para comparar um handle extraído
    de URL com ``Account.name``/``local_key``: remove espaços nas pontas,
    remove um ``@`` inicial (se houver) e converte para minúsculas. Não é
    sofisticada de propósito (item 1.3 do Prompt: "não precisa ser
    sofisticada, mas precisa ser determinística e testada")."""
    return text.strip().lstrip("@").lower()


def _extract_platform_handle(platform: str, parsed: ParseResult) -> str | None:
    """Extrai um handle/slug de canal da URL, quando reconhecível --
    ``None`` quando a URL não segue nenhum dos formatos conhecidos desta
    plataforma. Regra deliberadamente simples e determinística, não uma
    tentativa exaustiva de cobrir toda a superfície de URLs de cada
    plataforma."""
    segments = [segment for segment in (parsed.path or "").split("/") if segment]
    if not segments:
        return None
    first = segments[0]
    if platform == "youtube":
        if first.startswith("@") and len(first) > 1:
            return first[1:]
        if first in ("c", "user", "channel") and len(segments) >= 2:
            # "/channel/UC..." carrega um ID opaco, não um handle legível
            # por humanos -- ainda assim devolvido tal qual: uma
            # correspondência exata contra Account.local_key é
            # tecnicamente possível, mas na prática não vai bater e cai em
            # EXTERNAL_CONTENT (comportamento documentado, não um bug).
            return segments[1]
        return None
    if platform == "tiktok":
        if first.startswith("@") and len(first) > 1:
            return first[1:]
        return None
    return None


@dataclass(frozen=True)
class SourceContext:
    """Estado ATUAL (derivado) da classificação de um ``SourceAsset`` --
    nunca um evento bruto da tabela append-only."""

    source_asset_id: str
    context: str
    created_at: str


class SourceContextResolver:
    """Classifica automaticamente um ``SourceAsset`` em exatamente um de
    ``CONTEXT_CONNECTED_CHANNEL_CONTENT``/``CONTEXT_EXTERNAL_CONTENT``/
    ``CONTEXT_LOCAL_CONTENT``/``CONTEXT_UNKNOWN_SOURCE`` e persiste o
    resultado -- ver docstring do módulo para o contrato completo.

    Ortogonal ao resto do projeto: nunca cria ``Job``/``Publication``/
    ``Schedule``/``Project``/``Video`` -- só lê um ``SourceAsset``/
    ``Account``s já existentes e grava um evento de classificação
    associado a um ``source_asset_id`` já existente.
    """

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    # -- classificação ---------------------------------------------------

    def classify(self, asset: SourceAsset) -> str:
        """Classifica (sem persistir) um ``SourceAsset`` já carregado --
        exposto separadamente de ``resolve_and_persist`` para permitir
        testar a lógica de classificação pura, e para reclassificação
        manual sem depender de uma nova leitura do banco."""
        source_uri = asset.source_uri or ""
        parsed = urlparse(source_uri)
        if parsed.scheme not in ("http", "https"):
            return CONTEXT_LOCAL_CONTENT
        host = (parsed.hostname or "").lower()
        if not host:
            return CONTEXT_UNKNOWN_SOURCE
        platform = RECOGNIZED_PLATFORM_DOMAINS.get(host)
        if platform is None:
            return CONTEXT_EXTERNAL_CONTENT
        handle = _extract_platform_handle(platform, parsed)
        if handle is None:
            return CONTEXT_EXTERNAL_CONTENT
        matches = self._matching_accounts(platform, handle)
        if len(matches) == 1:
            return CONTEXT_CONNECTED_CHANNEL_CONTENT
        return CONTEXT_EXTERNAL_CONTENT

    def _matching_accounts(self, platform: str, handle: str) -> tuple[Account, ...]:
        normalized_handle = _normalize_for_comparison(handle)
        matches: list[Account] = []
        for account in self.database.list(Account):
            if not account.enabled:
                continue
            if account.platform != platform:
                continue
            candidates = {_normalize_for_comparison(account.name)}
            if account.local_key:
                candidates.add(_normalize_for_comparison(account.local_key))
                _prefix, _sep, suffix = account.local_key.partition("/")
                if suffix:
                    candidates.add(_normalize_for_comparison(suffix))
            if normalized_handle in candidates:
                matches.append(account)
        return tuple(matches)

    # -- resolução automática (item 1.2 -- "invisível ao usuário") ------

    def resolve_and_persist(self, source_asset_id: str) -> str | None:
        """Ponto de integração automático chamado pelos importadores (ver
        seção 3 da docstring do módulo). Carrega o ``SourceAsset``,
        classifica e persiste -- devolve a classificação gravada, ou
        ``None`` quando nada pôde ser classificado (``SourceAsset`` não
        encontrado). NUNCA lança: qualquer falha inesperada durante a
        classificação em si é responsabilidade do CHAMADOR isolar (ver
        ``_sistema/source_import.py`` -- a chamada a este método é
        envolvida em ``try/except Exception`` no ponto de integração,
        para que uma falha aqui nunca derrube a importação)."""
        _validate_source_asset_id(source_asset_id)
        asset = self.database.get(SourceAsset, source_asset_id)
        if asset is None:
            return None
        context = self.classify(asset)
        self._persist(source_asset_id, context)
        return context

    def _persist(self, source_asset_id: str, context: str) -> None:
        if context not in CONTEXTS:
            raise ValueError(f"context desconhecido: {context!r} (esperado um de {sorted(CONTEXTS)})")
        timestamp = utc_now_iso()
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO source_asset_context (id, source_asset_id, context, created_at) "
                "VALUES (?, ?, ?, ?)",
                (new_uuid(), source_asset_id, context, timestamp),
            )

    # -- leitura -----------------------------------------------------

    def get_current(self, source_asset_id: str) -> SourceContext | None:
        """Estado ATUAL (derivado): o evento de MAIOR ``rowid`` para este
        ``source_asset_id`` -- sem particionar por valor de ``context``
        (diferença deliberada em relação a
        ``AssertionStore.list_active``/m007: aqui exatamente UM valor
        vence, nunca vários simultaneamente). ``None`` quando nenhuma
        classificação foi gravada ainda (estado "ausente" -- ver seção 3
        da docstring do módulo)."""
        _validate_source_asset_id(source_asset_id)
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT context, created_at FROM source_asset_context "
                "WHERE source_asset_id = ? ORDER BY rowid DESC LIMIT 1",
                (source_asset_id,),
            ).fetchone()
        if row is None:
            return None
        return SourceContext(
            source_asset_id=source_asset_id,
            context=row["context"],
            created_at=row["created_at"],
        )
