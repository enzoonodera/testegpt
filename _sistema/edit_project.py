# -*- coding: utf-8 -*-
"""PROMPT 27 -- Modelo de Projeto de Edição (não destrutivo).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie modelo de edição não destrutivo.

    O Project deve armazenar decisões, não alterar source.

    Exemplos:

    cuts
    speed
    crop
    reframe
    audio settings
    captions
    template
    text layers
    metadata mode

    Trocar apenas um elemento não deve invalidar processamento que ainda
    seja reutilizável.

===========================================================================
0.1 -- O QUE JÁ EXISTIA E O QUE ESTE MÓDULO ACRESCENTA
===========================================================================

A entidade ``Project`` e a coluna ``edit_state_json`` (tabela ``projects``)
já existem desde ``m001_initial.py`` (schema congelado -- ver
``EntitySpec(Project, "projects", frozenset({"edit_state"}))`` em
``storage/database.py``). ``Project.edit_state`` já é um ``dict`` livre;
NENHUMA migration nova foi criada nesta rodada -- a coluna já comporta
qualquer JSON, e a estrutura interna definida aqui é inteiramente
responsabilidade deste módulo, sem exigir mudança de schema.

O que faltava (e é o problema real que este Prompt resolve) não era "ter
um dict" -- é dar a esse dict uma ESTRUTURA que torne a frase do roadmap
("trocar apenas um elemento não deve invalidar processamento que ainda
seja reutilizável") verificável em código, nunca apenas uma intenção
documentada. Sem estrutura, qualquer mudança em ``edit_state`` pareceria
"o projeto inteiro mudou" para uma camada futura (Prompt 42+) que precise
decidir se um processamento anterior ainda é reaproveitável -- exatamente
o comportamento que o roadmap pede para evitar.

===========================================================================
0.3 -- CAMADA DE RESPONSABILIDADE
===========================================================================

``domain/models.py`` continua contendo apenas a estrutura de dados
(``Project``), sem lógica de negócio. Toda a lógica deste Prompt
(validação, granularidade por categoria, fingerprints, leitura/escrita
segura sob concorrência) vive aqui, em ``EditProjectManager``, que opera
sobre um ``project_id`` já persistido e usa ``LocalDatabase.get``/``.save``
já existentes (dentro de uma transaction própria) -- não reimplementa CRUD
genérico.

===========================================================================
0.4 -- CONCORRÊNCIA: O RISCO DE *LOST UPDATE* E COMO É RESOLVIDO
===========================================================================

``edit_state`` é persistido como UMA ÚNICA coluna JSON por linha
(``edit_state_json``). Um padrão ingênuo de "ler o Project inteiro,
modificar uma categoria em memória, escrever o Project inteiro de volta"
como DUAS operações separadas (uma leitura fora de transaction, seguida de
uma escrita em outra transaction) tem uma janela de corrida real: se duas
chamadas concorrentes lerem o mesmo estado, cada uma alterar uma categoria
DIFERENTE, e escreverem de volta sem coordenação, a que escrever por
último apaga silenciosamente a mudança da outra -- mesmo as duas mudanças
sendo, em princípio, independentes e ambas válidas.

RESOLUÇÃO (mesma técnica já usada em ``ControlManager._cancel_or_defer_job``
e no espírito de ``SourceContextResolver``): toda escrita deste módulo
executa leitura + decisão + escrita dentro da MESMA transaction
``BEGIN IMMEDIATE`` (``LocalDatabase.transaction()``). ``BEGIN IMMEDIATE``
adquire o write lock do SQLite imediatamente ao abrir a transaction -- uma
segunda chamada concorrente bloqueia em sua própria abertura de transaction
até a primeira commitar, e SÓ ENTÃO lê o estado (já atualizado pela
primeira). Isso elimina a janela de *lost update*: nunca há uma leitura
"stale" seguida de uma escrita que sobrescreve uma mudança alheia, porque
a leitura e a escrita desta chamada compartilham o mesmo lock ininterrupto.
Confirmado por teste com ``threading.Barrier`` forçando sobreposição real
(seção de testes).

===========================================================================
1.5 -- PAPEL DE ``Project.revision`` vs. REVISÃO POR CATEGORIA
===========================================================================

``Project.revision`` (campo já existente, hoje morto -- nunca incrementado
em nenhum lugar do código) passa a ter um papel deliberadamente GROSSO e
SEPARADO: um contador de "algo mudou no projeto como um todo", incrementado
a cada mutação bem-sucedida de QUALQUER categoria (``set_category`` que
realmente alterou dados, ou ``remove_category`` que removeu algo). Ele
serve como um sinal de controle de concorrência otimista no nível do
``Project`` inteiro (útil, por exemplo, para uma UI detectar "este projeto
mudou desde que você abriu a tela" sem precisar comparar categoria por
categoria), mas NUNCA é usado como substituto do mecanismo fino de
reaproveitamento -- essa decisão (se um processamento anterior ainda é
válido) depende exclusivamente do fingerprint/revisão POR CATEGORIA
(``_CategoryState.fingerprint``/``.revision``), nunca de
``Project.revision``. Os dois conceitos são deliberadamente independentes
e não podem ser confundidos: um bump em ``Project.revision`` não diz QUAL
categoria mudou; só o estado por categoria diz isso.

===========================================================================
ESTRUTURA DE ``edit_state`` (contrato deste módulo)
===========================================================================

    {
      "categories": {
        "<nome-da-categoria>": {
          "data": <qualquer JSON-serializável>,
          "fingerprint": "<sha256 hex do JSON canônico de 'data'>",
          "revision": <inteiro >= 1>,
          "updated_at": "<ISO-8601 UTC>"
        },
        ...
      }
    }

O CONJUNTO de categorias é extensível -- qualquer string não vazia é um
nome de categoria válido, inclusive um ainda não previsto por nenhum
Prompt futuro (item 1.2 do Prompt). As nove constantes citadas
literalmente no roadmap (``CUTS``, ``SPEED``, ``CROP``, ``REFRAME``,
``AUDIO_SETTINGS``, ``CAPTIONS``, ``TEMPLATE``, ``TEXT_LAYERS``,
``METADATA_MODE``) são apenas CONVENIÊNCIAS de nomenclatura -- nenhuma
validação restringe o nome de categoria a essa lista (nunca um enum/CHECK
fechado). O SCHEMA INTERNO de cada categoria (o que vai dentro de
"cuts"/"crop"/etc.) é responsabilidade dos Prompts 28+ (Editor) -- este
módulo trata ``data`` como um valor JSON opaco.

===========================================================================
FINGERPRINT POR CATEGORIA E FINGERPRINT AGREGADO
===========================================================================

O fingerprint de uma categoria é o SHA-256 hex do JSON canônico (chaves
ordenadas, sem espaços) de ``data`` -- CALCULADO EXCLUSIVAMENTE a partir
dos dados daquela categoria, nunca de outras categorias nem de campos como
``Project.name``/``Project.video_id``. Isso é o que garante a garantia
central do roadmap: alterar uma categoria nunca muda o fingerprint de
nenhuma outra.

O fingerprint AGREGADO do projeto (``aggregate_fingerprint``) é derivado de
forma determinística dos fingerprints individuais -- SHA-256 hex do JSON
canônico da lista ``[[nome, fingerprint], ...]`` ordenada por nome --,
NUNCA um hash direto do JSON bruto de ``edit_state`` inteiro (isso
reintroduziria o problema que este Prompt existe para resolver: um hash de
blob bruto muda de forma opaca a cada edição, sem permitir isolar QUAL
categoria mudou). É calculado sob demanda a partir do estado persistido
atual -- não é armazenado como um campo próprio, evitando um segundo lugar
que precisaria ser mantido em sincronia com as categorias.

===========================================================================
ORTOGONALIDADE E ESCOPO
===========================================================================

Este módulo nunca importa nem chama ``circuit_breaker``/``retry_policy``/
``publication_idempotency``/``secrets_manager``/``domain.job_state_machine``,
nunca importa nada de Geração 1, nunca chama ``subprocess``/``ffmpeg``/
``ffprobe`` (não processa mídia -- só grava decisões), e nunca cria
``Job``/``Artifact``/``Publication``/``Schedule``/``Video``. Nunca toca
``SourceAsset``/o arquivo original -- este módulo só lê/escreve a tabela
``projects``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, ClassVar

from .domain import Project
from .storage.database import LocalDatabase
from .time_utils import utc_now_iso

__all__ = [
    "CUTS",
    "SPEED",
    "CROP",
    "REFRAME",
    "AUDIO_SETTINGS",
    "CAPTIONS",
    "TEMPLATE",
    "TEXT_LAYERS",
    "METADATA_MODE",
    "EditProjectError",
    "ProjectNaoEncontradoError",
    "CategoriaInvalidaError",
    "DadosNaoSerializaveisError",
    "CategoryState",
    "EditProjectManager",
]

# ---------------------------------------------------------------------------
# Constantes de conveniência para os nove exemplos literais do roadmap.
# Nomenclatura, NUNCA uma lista fechada -- qualquer string não vazia é um
# nome de categoria válido (ver ``_validate_category_name``).
# ---------------------------------------------------------------------------

CUTS = "cuts"
SPEED = "speed"
CROP = "crop"
REFRAME = "reframe"
AUDIO_SETTINGS = "audio_settings"
CAPTIONS = "captions"
TEMPLATE = "template"
TEXT_LAYERS = "text_layers"
METADATA_MODE = "metadata_mode"


class EditProjectError(RuntimeError):
    """Base para todo erro estruturado deste módulo -- nunca uma exceção
    crua (``TypeError``/``ValueError`` de ``json.dumps``, por exemplo)
    escapa de um método público de ``EditProjectManager``. ``code`` é a
    identificação estável e programática do erro (mesmo espírito de
    ``ERRO_*``/``.code`` em ``media_probe.py``/``source_import.py``, aqui
    expresso como classes distintas -- mesmo padrão de erro tipado já
    usado por ``ControlManagerError`` e suas subclasses)."""

    code: ClassVar[str] = "EDIT_PROJECT_ERRO"


class ProjectNaoEncontradoError(EditProjectError):
    """``project_id`` não corresponde a nenhum ``Project`` persistido."""

    code: ClassVar[str] = "PROJETO_NAO_ENCONTRADO"


class CategoriaInvalidaError(EditProjectError):
    """Nome de categoria vazio, não-string, ou de outra forma inválido."""

    code: ClassVar[str] = "CATEGORIA_INVALIDA"


class DadosNaoSerializaveisError(EditProjectError):
    """``data`` de uma categoria não é serializável em JSON (mesmo
    contrato já usado por ``edit_state_json``) -- inclui tipos inesperados
    (ex.: um objeto Python arbitrário, um ``set``) que o ``json`` padrão
    não sabe codificar."""

    code: ClassVar[str] = "DADOS_NAO_SERIALIZAVEIS"


@dataclass(frozen=True)
class CategoryState:
    """Estado estruturado de uma categoria -- devolvido por
    ``set_category``/``get_category``, nunca um dict solto."""

    category: str
    data: Any
    fingerprint: str
    revision: int
    updated_at: str


def _validate_category_name(category: Any) -> str:
    if not isinstance(category, str) or not category.strip():
        raise CategoriaInvalidaError(
            f"nome de categoria deve ser uma string não vazia (recebido: {category!r})"
        )
    return category


def _canonical_json(data: Any) -> str:
    """Serialização canônica -- mesmos parâmetros de ``_json_dumps`` em
    ``storage/database.py`` (chaves ordenadas, sem espaços), garantindo
    que o fingerprint calculado aqui seja consistente com o JSON
    efetivamente persistido em ``edit_state_json``. Qualquer falha de
    serialização (tipo inesperado, referência circular, etc.) é
    convertida em ``DadosNaoSerializaveisError`` -- nunca uma exceção
    crua do módulo ``json`` escapa deste método."""
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise DadosNaoSerializaveisError(
            f"dados da categoria não são serializáveis em JSON: {exc}"
        ) from exc


def _fingerprint_for(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _categories_from_edit_state(edit_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extrai o mapa de categorias de um ``edit_state`` bruto -- tolerante
    a um ``edit_state`` legado/vazio (``{}``) sem a chave ``categories``,
    nunca lança para esse caso (é o estado inicial padrão de todo
    ``Project`` novo)."""
    categories = edit_state.get("categories") if isinstance(edit_state, dict) else None
    if not isinstance(categories, dict):
        return {}
    return dict(categories)


class EditProjectManager:
    """Serviço de leitura/escrita estruturada de ``Project.edit_state``.

    Sem lógica de mídia/processamento (item 0.3/NÃO FAZER) -- só grava e
    lê decisões. Toda escrita é atômica sob concorrência real (seção
    0.4)."""

    def __init__(self, database: LocalDatabase) -> None:
        if not isinstance(database, LocalDatabase):
            raise TypeError("database deve ser uma instância de LocalDatabase")
        self.database = database

    # -- escrita -----------------------------------------------------

    def set_category(self, project_id: str, category: str, data: Any) -> CategoryState:
        """Define/atualiza uma categoria inteira -- substitui os dados
        daquela categoria e recalcula SÓ o fingerprint/revisão dela.
        Nunca toca o fingerprint/revisão de nenhuma outra categoria já
        presente no mesmo projeto (garantia central deste Prompt).

        Se os dados forem byte-a-byte idênticos (mesmo fingerprint) aos já
        gravados para esta categoria, a chamada é um NO-OP determinístico:
        nem a revisão da categoria nem ``Project.revision`` avançam, e
        nenhuma escrita é feita no banco (decisão documentada na seção
        1.5 do módulo -- evita inflar revisões por reenvios idempotentes
        do mesmo estado, mantendo a revisão como sinal real de mudança)."""
        category = _validate_category_name(category)
        canonical = _canonical_json(data)
        fingerprint = _fingerprint_for(canonical)

        with self.database.transaction() as conn:
            project = self.database.get(Project, project_id, connection=conn)
            if project is None:
                raise ProjectNaoEncontradoError(f"Project não encontrado: {project_id}")

            categories = _categories_from_edit_state(project.edit_state)
            existing = categories.get(category)

            if existing is not None and existing.get("fingerprint") == fingerprint:
                # NO-OP determinístico -- ver docstring do método.
                return CategoryState(
                    category=category,
                    data=existing.get("data"),
                    fingerprint=fingerprint,
                    revision=int(existing.get("revision", 1)),
                    updated_at=str(existing.get("updated_at", "")),
                )

            new_revision = int(existing.get("revision", 0)) + 1 if existing is not None else 1
            timestamp = utc_now_iso()
            categories[category] = {
                "data": data,
                "fingerprint": fingerprint,
                "revision": new_revision,
                "updated_at": timestamp,
            }
            project.edit_state = {"categories": categories}
            project.revision += 1
            project.touch()
            self.database.save(project, connection=conn)

        return CategoryState(
            category=category,
            data=data,
            fingerprint=fingerprint,
            revision=new_revision,
            updated_at=timestamp,
        )

    def update_category(self, project_id: str, category: str, mutator: "Callable[[Any], Any]") -> CategoryState:
        """Leitura + decisão + escrita ATÔMICAS de uma categoria, dentro da
        MESMA transaction ``BEGIN IMMEDIATE`` (mesma técnica de
        ``set_category`` -- ver seção 0.4 da docstring do módulo).

        Acrescentado no PROMPT 28 (Editor: corte e timeline): um consumidor
        que precisa "ler o estado atual da categoria, calcular um novo
        estado A PARTIR do atual, escrever de volta" (ex.: aplicar uma
        operação de corte sobre uma timeline já existente) NÃO pode fazer
        isso com ``get_category()`` seguido de ``set_category()`` como duas
        chamadas separadas -- entre a leitura e a escrita existiria uma
        janela onde outra chamada concorrente escreve por cima, e a
        segunda escrita (que decidiu com base num estado já stale)
        apagaria silenciosamente a mudança alheia (o mesmo *lost update*
        que a seção 0.4 já resolve para SUBSTITUIÇÃO completa de uma
        categoria, mas que reaparece se um "ler para decidir" for feito
        fora de uma única transaction). ``update_category`` fecha essa
        janela: chama ``mutator(current_data)`` (``current_data`` é
        ``None`` se a categoria ainda não existir) DENTRO da mesma
        transaction que já leu o ``Project`` e que fará a escrita --
        nenhuma outra chamada consegue intercalar uma escrita entre a
        leitura e a decisão, porque ``BEGIN IMMEDIATE`` já segura o write
        lock desde a leitura.

        Se ``mutator`` levantar qualquer exceção, a transaction inteira
        sofre ROLLBACK (nenhuma escrita parcial) e a exceção se propaga
        para o chamador -- mesmo contrato de qualquer erro dentro de
        ``LocalDatabase.transaction()``.

        Idêntico a ``set_category`` em tudo o mais (mesmo cálculo de
        fingerprint/revisão, mesmo NO-OP determinístico quando o resultado
        é byte-a-byte idêntico ao já persistido, mesma garantia de nunca
        tocar outras categorias)."""
        category = _validate_category_name(category)

        with self.database.transaction() as conn:
            project = self.database.get(Project, project_id, connection=conn)
            if project is None:
                raise ProjectNaoEncontradoError(f"Project não encontrado: {project_id}")

            categories = _categories_from_edit_state(project.edit_state)
            existing = categories.get(category)
            current_data = existing.get("data") if existing is not None else None

            new_data = mutator(current_data)
            canonical = _canonical_json(new_data)
            fingerprint = _fingerprint_for(canonical)

            if existing is not None and existing.get("fingerprint") == fingerprint:
                return CategoryState(
                    category=category,
                    data=existing.get("data"),
                    fingerprint=fingerprint,
                    revision=int(existing.get("revision", 1)),
                    updated_at=str(existing.get("updated_at", "")),
                )

            new_revision = int(existing.get("revision", 0)) + 1 if existing is not None else 1
            timestamp = utc_now_iso()
            categories[category] = {
                "data": new_data,
                "fingerprint": fingerprint,
                "revision": new_revision,
                "updated_at": timestamp,
            }
            project.edit_state = {"categories": categories}
            project.revision += 1
            project.touch()
            self.database.save(project, connection=conn)

        return CategoryState(
            category=category,
            data=new_data,
            fingerprint=fingerprint,
            revision=new_revision,
            updated_at=timestamp,
        )

    def remove_category(self, project_id: str, category: str) -> bool:
        """Remove uma categoria -- some da listagem, as demais permanecem
        intocadas (mesma garantia central de ``set_category``). Idempotente
        por design: remover uma categoria já ausente devolve ``False`` sem
        lançar (mesmo espírito de ``LocalDatabase.delete``, que devolve
        ``bool`` em vez de exigir que o chamador saiba se a linha existia).
        """
        category = _validate_category_name(category)

        with self.database.transaction() as conn:
            project = self.database.get(Project, project_id, connection=conn)
            if project is None:
                raise ProjectNaoEncontradoError(f"Project não encontrado: {project_id}")

            categories = _categories_from_edit_state(project.edit_state)
            if category not in categories:
                return False

            del categories[category]
            project.edit_state = {"categories": categories}
            project.revision += 1
            project.touch()
            self.database.save(project, connection=conn)

        return True

    # -- leitura -------------------------------------------------------

    def get_category(self, project_id: str, category: str) -> CategoryState | None:
        """Lê os dados/fingerprint/revisão atuais de uma categoria
        específica. ``None`` quando o projeto existe mas a categoria não
        foi definida ainda -- estado "ausente" é um resultado normal, não
        um erro (mesmo espírito de ``SourceContextResolver.get_current``).
        """
        category = _validate_category_name(category)
        project = self.database.get(Project, project_id)
        if project is None:
            raise ProjectNaoEncontradoError(f"Project não encontrado: {project_id}")

        categories = _categories_from_edit_state(project.edit_state)
        entry = categories.get(category)
        if entry is None:
            return None
        return CategoryState(
            category=category,
            data=entry.get("data"),
            fingerprint=str(entry.get("fingerprint", "")),
            revision=int(entry.get("revision", 1)),
            updated_at=str(entry.get("updated_at", "")),
        )

    def list_categories(self, project_id: str) -> tuple[str, ...]:
        """Lista, em ordem determinística (alfabética), todas as
        categorias presentes no projeto."""
        project = self.database.get(Project, project_id)
        if project is None:
            raise ProjectNaoEncontradoError(f"Project não encontrado: {project_id}")
        categories = _categories_from_edit_state(project.edit_state)
        return tuple(sorted(categories.keys()))

    def aggregate_fingerprint(self, project_id: str) -> str:
        """Fingerprint agregado do projeto inteiro -- derivado de forma
        determinística dos fingerprints individuais de cada categoria
        (ver docstring do módulo). Determinístico independente da ordem
        de inserção das categorias: a lista usada no cálculo é sempre
        ordenada por nome antes de serializar."""
        project = self.database.get(Project, project_id)
        if project is None:
            raise ProjectNaoEncontradoError(f"Project não encontrado: {project_id}")
        categories = _categories_from_edit_state(project.edit_state)
        pairs = sorted(
            (name, str(entry.get("fingerprint", ""))) for name, entry in categories.items()
        )
        canonical = _canonical_json(pairs)
        return _fingerprint_for(canonical)
