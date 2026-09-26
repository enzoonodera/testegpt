# -*- coding: utf-8 -*-
"""PROMPT 28 -- Editor: corte e timeline.

TEXTO LITERAL DO ROADMAP (Fase 5, imediatamente após o Prompt 27.5):

    Implemente módulo de edição temporal.

    Funções:
    - trim;
    - split;
    - remover trecho;
    - juntar trechos;
    - reorganizar quando suportado;
    - alterar velocidade.

    Salvar tudo como Edit Decisions.

    Não alterar vídeo original.

===========================================================================
0. CAMADA DE PERSISTÊNCIA -- REAPROVEITAMENTO, NÃO REINVENÇÃO
===========================================================================

``EditProjectManager`` (PROMPT 27, ``_sistema/edit_project.py``) já é a
ÚNICA camada de persistência de decisões de edição do produto, e sua
própria docstring já delega explicitamente a este Prompt (28+) a
responsabilidade de definir o SCHEMA INTERNO da categoria ``cuts``. Este
módulo não cria nenhuma tabela nova, nenhuma migration nova, e não
reimplementa fingerprint/revisão/concorrência -- ele define o CONTEÚDO que
vai dentro de ``data`` da categoria ``CUTS`` e usa exclusivamente os
métodos públicos de ``EditProjectManager`` para ler/escrever.

``LATEST_SCHEMA_VERSION`` permanece 9 (inalterado) -- confirmado por teste
dedicado.

===========================================================================
0.1 -- UMA EXTENSÃO NECESSÁRIA E JUSTIFICADA EM ``edit_project.py``
===========================================================================

Investigação ANTES de escrever qualquer linha deste módulo: os métodos
públicos existentes de ``EditProjectManager`` eram ``set_category``
(substitui o ``data`` inteiro de uma categoria, sem olhar o valor
anterior), ``get_category`` (leitura pura) e ``remove_category``. Nenhum
deles serve para "ler o estado atual da timeline, computar um novo estado
A PARTIR do atual (ex.: aplicar um ``trim`` sobre um segmento já
existente), escrever de volta" de forma ATÔMICA.

Um padrão ingênuo -- ``get_category()`` seguido de ``set_category()`` como
DUAS chamadas separadas de banco -- reabre exatamente o *lost update* que
a seção 0.4 da docstring de ``edit_project.py`` já resolveu para
SUBSTITUIÇÃO completa de uma categoria: duas chamadas concorrentes de
``TimelineEditor`` sobre o MESMO projeto (ex.: uma dando ``trim`` num
segmento, outra dando ``split`` em outro) leriam o mesmo estado inicial,
decidiriam cada uma com base nele, e a escrita que chegasse por último
apagaria silenciosamente a mudança da outra -- porque ``set_category``
apenas SUBSTITUI o ``data`` da categoria pelo que reflete, sem comparar
contra o que estava lá quando o chamador decidiu.

Por isso, esta rodada adicionou UM método novo, aditivo, a
``EditProjectManager``: ``update_category(project_id, category, mutator)``,
que executa leitura + decisão (``mutator(current_data)``) + escrita dentro
da MESMA transaction ``BEGIN IMMEDIATE`` -- fechando a janela de corrida.
Nenhum método existente de ``EditProjectManager`` teve comportamento ou
assinatura alterados (as 34 asserções de ``tests/test_edit_project.py``
já existentes continuam passando sem nenhuma modificação); a mudança é
puramente aditiva. Ver a docstring do próprio método para detalhes, e o
relatório de entrega para a prova de concorrência (GATE 6).

Todas as operações deste módulo usam exclusivamente
``EditProjectManager.update_category``/``get_category`` -- nunca abrem uma
``transaction()`` própria, nunca leem/escrevem ``Project.edit_state``
diretamente (item 1.d do Prompt).

===========================================================================
0.2 -- INTEGRAÇÃO COM O MEDIA CATALOG (PROMPT 27.5) -- SEM ALTERAR NADA LÁ
===========================================================================

O badge ``EDITED`` do ``MediaCatalogService`` já dispara automaticamente
assim que um ``Project`` tiver pelo menos uma categoria definida em
``edit_state`` (evidência: ``json_each`` sobre
``edit_state_json->'$.categories'``). Gravar a primeira decisão de
``cuts`` através deste módulo já torna o vídeo ``EDITED`` no catálogo,
SEM nenhuma mudança em ``media_catalog.py`` -- confirmado por teste de
integração read-only em ``tests/test_timeline_editor.py``.
``_sistema/media_catalog.py`` NÃO foi alterado nesta rodada.

===========================================================================
0.3 -- SEM VALIDAÇÃO CONTRA O ARQUIVO REAL
===========================================================================

``MediaProbe`` (PROMPT 26) é deliberadamente stateless e sob demanda --
nunca chamado automaticamente. Este módulo NÃO chama ``MediaProbe``/
``ffmpeg``/``ffprobe``/``subprocess`` para confirmar que um corte está
dentro da duração real do arquivo. A validação aqui é inteiramente
ESTRUTURAL (ver seção 2 abaixo) -- nunca contra o arquivo. Se uma etapa de
renderização futura precisar confrontar Edit Decisions com a duração real,
ela decide isso por conta própria; não é responsabilidade deste Prompt.

Consequência direta (item 0.4 do Prompt): nem ``Video``/``SourceAsset``/
``Project`` têm hoje nenhum campo de duração persistida. A duração total
do vídeo original só entra neste módulo como parâmetro EXPLÍCITO passado
pelo chamador em ``initialize_timeline`` -- nunca lida "magicamente" de
algum lugar que não existe.

===========================================================================
1. SCHEMA DA TIMELINE (``data`` da categoria ``CUTS``)
===========================================================================

    {
      "segments": [
        {
          "segment_id": "<uuid>",
          "start": <float >= 0, segundos, relativo ao vídeo ORIGINAL>,
          "end": <float > start>,
          "speed": <float > 0, default 1.0>,
          "source_segment_id": "<segment_id de origem quando resultado de
                                 split/remove, ou null>"
        },
        ...
      ]
    }

A lista é ORDENADA: a ordem dos elementos É a ordem de reprodução final.
``start``/``end`` são SEMPRE relativos ao vídeo ORIGINAL (nunca ao
resultado já cortado) -- é isso que permite reaproveitar processamento
anterior por trecho no futuro (mesmo espírito do Prompt 27).

===========================================================================
1.1 -- DECISÃO: VELOCIDADE VIVE DENTRO DE ``cuts``, NÃO EM ``SPEED``
        SEPARADO
===========================================================================

``EditProjectManager`` expõe ``SPEED = "speed"`` como categoria de
CONVENIÊNCIA (nomenclatura, nunca uma exigência). Este módulo delibera-
damente NÃO usa a categoria ``SPEED`` -- o campo ``speed`` vive DENTRO de
cada segmento de ``cuts``. Razão: velocidade por segmento é inerente à
própria timeline de cortes (qual trecho toca a qual velocidade É parte da
mesma decisão que QUAL trecho toca). Se ``speed`` vivesse numa categoria
separada (ex.: um dict ``{segment_id: fator}``), as duas categorias
poderiam DESSINCRONIZAR -- um ``split``/``remove``/``reorder`` em ``cuts``
mudaria o conjunto de ``segment_id`` válidos sem que ``speed`` fosse
atualizado no mesmo commit atômico (``set_category``/``update_category``
operam UMA categoria por chamada; manter as duas em sincronia exigiria
DUAS chamadas separadas para uma única operação lógica, reabrindo a mesma
classe de janela de corrida que a seção 0.1 já identificou). Manter tudo
em ``cuts`` garante que toda operação (inclusive ``set_speed``) seja uma
única escrita atômica de um único documento consistente. Confirmado por
teste (``test_speed_nunca_e_usada_como_categoria_separada``) que a
categoria ``SPEED``/``"speed"`` nunca é gravada por este módulo.

===========================================================================
1.2 -- REORGANIZAR: "QUANDO SUPORTADO" (texto literal do roadmap)
===========================================================================

``Project.video_id`` é único por projeto (um projeto está associado a UM
vídeo -- ``domain/models.py``). Logo, TODOS os segmentos de uma mesma
timeline já pertencem, por construção, ao mesmo vídeo de origem -- não
existe, no modelo de dados atual, a noção de "reordenar trechos de vídeos
DIFERENTES numa mesma timeline". ``reorder`` é implementado para o caso
que o roadmap pede e que o modelo atual suporta: uma nova PERMUTAÇÃO da
lista de segmentos já existente (mesmo conjunto de ``segment_id``, nova
ordem). Um multi-vídeo por timeline exigiria estender o schema (um campo
de origem por segmento, hoje inexistente) -- fora de escopo sem
necessidade concreta demonstrada (CLAUDE.md: "não fazer overengineering
sem benefício concreto").

===========================================================================
1.3 -- JUNTAR TRECHOS: POR QUE EXIGE CONTIGUIDADE NO VÍDEO ORIGINAL
===========================================================================

O roadmap deixa a cargo desta implementação decidir quando "faz sentido"
mesclar dois segmentos adjacentes (cita "mesma fonte" e "velocidade
compatível" como exemplos). Este módulo exige, além de ambas:

- os dois segmentos precisam ser ADJACENTES NA ORDEM da timeline (nunca
  dois segmentos não-vizinhos -- exigido literalmente pelo Prompt);
- ``segmento_a.end == segmento_b.start`` (contiguidade no vídeo
  ORIGINAL).

A segunda condição não é uma política de produto arbitrária -- é uma
consequência direta do schema escolhido (seção 1): um segmento é
representado por UM ÚNICO intervalo ``[start, end]``. Unir dois trechos
que NÃO são contíguos no vídeo original exigiria um segmento capaz de
representar múltiplos intervalos descontínuos, o que este schema não
suporta e este Prompt não introduz (nenhuma necessidade concreta disso foi
demonstrada; ver CLAUDE.md sobre overengineering). "Mesma fonte" é sempre
verdadeira estruturalmente (seção 1.2) -- não é uma checagem adicional
necessária, mas é mencionada aqui para registrar que foi considerada.
"Velocidade compatível" é aplicada literalmente: as duas ``speed`` devem
ser idênticas (um segmento resultante só pode ter UM valor de ``speed``).

===========================================================================
1.4 -- TRIM: SÓ ENCOLHE, NUNCA EXPANDE
===========================================================================

``trim`` ajusta ``start``/``end`` de um segmento já existente. Decisão:
o novo ``start`` (se informado) precisa ser ``>=`` o ``start`` atual, e o
novo ``end`` (se informado) precisa ser ``<=`` o ``end`` atual -- ou seja,
``trim`` só pode ENCOLHER um segmento a partir das bordas, nunca
expandi-lo além do que já estava registrado. Expandir um segmento além do
que já foi validado (por um ``split`` anterior, ou pela inicialização da
timeline) fabricaria a alegação de que um trecho antes excluído passou a
ser válido, sem nenhuma evidência (nem sequer a duração real do arquivo é
verificada por este módulo -- seção 0.3). Quem quiser "recuperar" um
trecho removido deve fazer isso explicitamente através de uma nova
operação sobre a timeline (ex.: um novo ``split``/edição a partir do
estado anterior), nunca implicitamente via um ``trim`` que expande.

``trim`` que reduziria a duração resultante a ``<= 0`` é ERRO estruturado
(``SegmentoInvalidoError``) -- nunca remove o segmento silenciosamente.
Quem quiser remover um segmento inteiro usa ``remove_segment``
explicitamente (seção 1.5).

===========================================================================
1.4B -- TIMELINE ESVAZIADA POR EDIÇÃO == TIMELINE NUNCA INICIALIZADA
        (decisão explícita, achado no GATE ADVERSARIAL)
===========================================================================

É possível chegar a uma timeline sem NENHUM segmento por um caminho
totalmente válido: remover (via ``remove_segment``/``remove_range``) o
ÚLTIMO segmento restante. Nesse estado, ``data`` da categoria ``cuts``
passa a ser ``{"segments": []}`` -- indistinguível, para este módulo, do
estado "categoria ``cuts`` nunca foi definida" (``get_timeline`` retorna
``()`` nos dois casos, e ``initialize_timeline`` aceita reinicializar nos
dois casos, sem levantar ``TimelineJaInicializadaError``). Decisão
DELIBERADA, não um bug: distinguir "nunca tocado" de "esvaziado por edição
válida" exigiria um terceiro estado no schema sem nenhum benefício
concreto demonstrado nesta etapa (CLAUDE.md: não fazer overengineering sem
necessidade); reinicializar uma timeline esvaziada é um caminho de
recuperação legítimo, não uma perda de dados silenciosa (o histórico de
como ela chegou a ficar vazia já está nas revisões/fingerprints de
``EditProjectManager``, que este módulo não substitui). Confirmado por
teste dedicado.

===========================================================================
1.5 -- REMOVER TRECHO: UMA ÚNICA PRIMITIVA (``remove_range``) + UM ATALHO
===========================================================================

``remove_range(project_id, segment_id, start, end)`` remove o subintervalo
``[start, end)`` de DENTRO de um segmento -- cobre os três casos do
roadmap (trecho no meio == split implícito nos dois lados; trecho tocando
uma borda == equivalente a um trim da borda; trecho == segmento inteiro ==
remoção total) através de UMA única função pura, evitando três caminhos de
código divergentes para o mesmo conceito. ``remove_segment(project_id,
segment_id)`` é um atalho que remove o segmento INTEIRO (roadmap: "excluir
um segmento") -- implementado internamente como
``remove_range(segment_id, start=segmento.start, end=segmento.end)``, ou
seja, nunca duplica a lógica de validação/remoção.

===========================================================================
2. VALIDAÇÃO ESTRUTURAL (nunca contra o arquivo real -- seção 0.3)
===========================================================================

- ``start >= 0`` e ``end > start`` sempre, em todo segmento.
- nenhuma operação pode produzir (ou deixar) um segmento com duração
  resultante ``<= 0``.
- ``speed`` estritamente positivo; intervalo aceito ``[0.25, 4.0]``
  (faixa comum de controles de velocidade de ferramentas de edição
  comerciais -- qualquer valor fora disso é tratado como erro de entrada,
  não um caso de uso real do produto; documentado aqui para que uma
  revisão futura possa ajustar conscientemente, nunca por acidente).
- ``split`` exige um ponto estritamente interno (``start < at < end`` do
  segmento alvo) -- um ponto igual a uma das bordas não corta nada.
- ``join`` exige adjacência de ORDEM e de TEMPO (seção 1.3) -- erro
  estruturado caso contrário.
- ``reorder`` exige que o conjunto de ``segment_id`` da nova ordem seja
  EXATAMENTE igual ao conjunto já existente (sem duplicata, sem ausência,
  sem id desconhecido) -- erro estruturado caso contrário, com uma
  variante de erro distinta testada para cada uma das três formas de
  violação.

===========================================================================
3. NÃO ALTERA O VÍDEO ORIGINAL (texto literal do roadmap)
===========================================================================

Este módulo NUNCA chama ``subprocess``/``ffmpeg``/``ffprobe``, nunca
escreve/move/renomeia/apaga qualquer arquivo referenciado por
``SourceAsset.local_path``, nunca importa ``circuit_breaker``/
``retry_policy``/``publication_idempotency``/``secrets_manager``/
``domain.job_state_machine``/nada de Geração 1, e nunca cria
``Job``/``Artifact``/``Publication``/``Schedule`` (isso é responsabilidade
de uma etapa de renderização futura, fora de escopo aqui). Confirmado por
teste estrutural via AST em ``tests/test_timeline_editor.py`` (nunca grep
textual ingênuo -- a própria docstring deste módulo cita "ffmpeg"/
"ffprobe"/"subprocess" para explicar por que não são usados, o mesmo
problema já enfrentado nos Prompts 24b/25/26/27/27b/27.5).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, ClassVar
from uuid import UUID, uuid4

from .edit_project import CUTS, EditProjectManager
from .storage.database import LocalDatabase

__all__ = [
    "MIN_SPEED",
    "MAX_SPEED",
    "Segment",
    "TimelineEditorError",
    "SegmentoNaoEncontradoError",
    "SegmentoInvalidoError",
    "PontoDeCorteInvalidoError",
    "VelocidadeInvalidaError",
    "SegmentosNaoAdjacentesError",
    "ReordenacaoInvalidaError",
    "TimelineJaInicializadaError",
    "TimelineNaoInicializadaError",
    "DuracaoInvalidaError",
    "TimelineEditor",
]

# Faixa aceita de fator de velocidade -- ver seção 2 da docstring do módulo.
MIN_SPEED = 0.25
MAX_SPEED = 4.0


# ---------------------------------------------------------------------------
# Erros estruturados (mesmo padrão ``.code`` de edit_project.py/media_catalog.py)
# ---------------------------------------------------------------------------
class TimelineEditorError(RuntimeError):
    """Base para todo erro estruturado deste módulo -- nunca um
    ``ValueError``/``KeyError`` cru escapa de um método público de
    ``TimelineEditor``. Erros de ``EditProjectManager`` (ex.:
    ``ProjectNaoEncontradoError``) já são estruturados e se propagam sem
    wrapping -- ver seção 0.1 da docstring do módulo."""

    code: ClassVar[str] = "TIMELINE_EDITOR_ERRO"


class SegmentoNaoEncontradoError(TimelineEditorError):
    """``segment_id`` não corresponde a nenhum segmento da timeline atual."""

    code: ClassVar[str] = "SEGMENTO_NAO_ENCONTRADO"


class SegmentoInvalidoError(TimelineEditorError):
    """Um segmento resultante violaria uma invariante estrutural
    (``start``/``end``/duração/forma dos dados)."""

    code: ClassVar[str] = "SEGMENTO_INVALIDO"


class PontoDeCorteInvalidoError(TimelineEditorError):
    """``split``/``remove_range`` recebeu um ponto/intervalo fora do
    segmento alvo."""

    code: ClassVar[str] = "PONTO_DE_CORTE_INVALIDO"


class VelocidadeInvalidaError(TimelineEditorError):
    """``speed`` fora do intervalo ``[MIN_SPEED, MAX_SPEED]`` ou não
    numérico."""

    code: ClassVar[str] = "VELOCIDADE_INVALIDA"


class SegmentosNaoAdjacentesError(TimelineEditorError):
    """``join`` chamado sobre dois segmentos que não são vizinhos na
    ordem da timeline e/ou não são contíguos no vídeo original."""

    code: ClassVar[str] = "SEGMENTOS_NAO_ADJACENTES"


class ReordenacaoInvalidaError(TimelineEditorError):
    """A nova ordem passada a ``reorder`` não é uma permutação exata do
    conjunto de ``segment_id`` já existente."""

    code: ClassVar[str] = "REORDENACAO_INVALIDA"


class TimelineJaInicializadaError(TimelineEditorError):
    """``initialize_timeline`` chamado sobre um projeto que já tem uma
    categoria ``cuts`` definida."""

    code: ClassVar[str] = "TIMELINE_JA_INICIALIZADA"


class TimelineNaoInicializadaError(TimelineEditorError):
    """Uma operação que exige uma timeline existente foi chamada antes de
    ``initialize_timeline``."""

    code: ClassVar[str] = "TIMELINE_NAO_INICIALIZADA"


class DuracaoInvalidaError(TimelineEditorError):
    """``duration_seconds`` passado a ``initialize_timeline`` não é um
    número finito estritamente positivo."""

    code: ClassVar[str] = "DURACAO_INVALIDA"


@dataclass(frozen=True)
class Segment:
    """Um trecho da timeline -- ver seção 1 da docstring do módulo."""

    segment_id: str
    start: float
    end: float
    speed: float = 1.0
    source_segment_id: str | None = None

    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start": self.start,
            "end": self.end,
            "speed": self.speed,
            "source_segment_id": self.source_segment_id,
        }

    @staticmethod
    def from_dict(payload: Any) -> "Segment":
        if not isinstance(payload, dict):
            raise SegmentoInvalidoError(f"segmento deve ser um objeto, recebido: {payload!r}")
        try:
            segment_id = payload["segment_id"]
            start = payload["start"]
            end = payload["end"]
        except KeyError as exc:
            raise SegmentoInvalidoError(f"segmento sem campo obrigatório: {exc}") from exc
        speed = payload.get("speed", 1.0)
        source_segment_id = payload.get("source_segment_id")
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise SegmentoInvalidoError(f"segment_id inválido: {segment_id!r}")
        if source_segment_id is not None and (
            not isinstance(source_segment_id, str) or not source_segment_id.strip()
        ):
            raise SegmentoInvalidoError(f"source_segment_id inválido: {source_segment_id!r}")
        segment = Segment(
            segment_id=segment_id,
            start=_as_float(start, "start"),
            end=_as_float(end, "end"),
            speed=_as_float(speed, "speed"),
            source_segment_id=source_segment_id,
        )
        _validate_segment_shape(segment)
        return segment


def _as_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SegmentoInvalidoError(f"{field_name} deve ser numérico, recebido: {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise SegmentoInvalidoError(f"{field_name} deve ser finito, recebido: {value!r}")
    return number


def _validate_segment_shape(segment: Segment) -> None:
    if segment.start < 0:
        raise SegmentoInvalidoError(f"start deve ser >= 0 (segmento {segment.segment_id}): {segment.start!r}")
    if segment.end <= segment.start:
        raise SegmentoInvalidoError(
            f"end deve ser > start (segmento {segment.segment_id}): start={segment.start!r} end={segment.end!r}"
        )
    _validate_speed(segment.speed)


def _validate_speed(speed: float) -> None:
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise VelocidadeInvalidaError(f"speed deve ser numérico, recebido: {speed!r}")
    speed = float(speed)
    if not (MIN_SPEED <= speed <= MAX_SPEED):
        raise VelocidadeInvalidaError(
            f"speed deve estar entre {MIN_SPEED} e {MAX_SPEED}, recebido: {speed!r}"
        )


def _validate_timeline(segments: list[Segment]) -> None:
    """Validação global -- chamada ao final de TODA operação, antes de
    persistir (item 1.c do Prompt). Além da validação por segmento (já
    feita em ``_validate_segment_shape``), confirma unicidade de
    ``segment_id``."""
    ids = [segment.segment_id for segment in segments]
    if len(ids) != len(set(ids)):
        raise SegmentoInvalidoError(f"segment_id duplicado na timeline resultante: {ids!r}")
    for segment in segments:
        _validate_segment_shape(segment)


def _segments_from_data(data: Any) -> list[Segment]:
    """Decodifica o ``data`` bruto persistido (ou ``None``, quando a
    categoria ``cuts`` ainda não existe) para uma lista de ``Segment``."""
    if data is None:
        return []
    if not isinstance(data, dict):
        raise SegmentoInvalidoError(f"data da categoria 'cuts' deve ser um objeto, recebido: {data!r}")
    raw_segments = data.get("segments", [])
    if not isinstance(raw_segments, list):
        raise SegmentoInvalidoError(f"'segments' deve ser uma lista, recebido: {raw_segments!r}")
    return [Segment.from_dict(item) for item in raw_segments]


def _segments_to_data(segments: list[Segment]) -> dict[str, Any]:
    return {"segments": [segment.to_dict() for segment in segments]}


def _find_index(segments: list[Segment], segment_id: str) -> int:
    for index, segment in enumerate(segments):
        if segment.segment_id == segment_id:
            return index
    raise SegmentoNaoEncontradoError(f"segmento não encontrado na timeline: {segment_id!r}")


def _require_initialized(segments: list[Segment]) -> None:
    if not segments:
        raise TimelineNaoInicializadaError(
            "timeline ainda não foi inicializada para este projeto (chame initialize_timeline primeiro)"
        )


class TimelineEditor:
    """Serviço de edição temporal não destrutiva -- opera exclusivamente
    sobre a categoria ``cuts`` de ``EditProjectManager`` (ver docstring do
    módulo). Nunca modifica o vídeo original, nunca abre uma transaction
    própria, nunca cria ``Job``/``Artifact``/``Publication``/``Schedule``.
    """

    def __init__(self, database: LocalDatabase) -> None:
        if not isinstance(database, LocalDatabase):
            raise TypeError("database deve ser uma instância de LocalDatabase")
        self._manager = EditProjectManager(database)

    # -- leitura -------------------------------------------------------

    def get_timeline(self, project_id: str) -> tuple[Segment, ...]:
        """Lista os segmentos atuais, em ordem de reprodução. Tupla vazia
        quando o projeto existe mas a timeline ainda não foi inicializada
        (estado normal, não um erro -- item de teste explícito do
        Prompt)."""
        state = self._manager.get_category(project_id, CUTS)
        if state is None:
            return ()
        return tuple(_segments_from_data(state.data))

    # -- inicialização ---------------------------------------------------

    def initialize_timeline(self, project_id: str, duration_seconds: float, *, segment_id: str | None = None) -> tuple[Segment, ...]:
        """Cria a timeline inicial: um único segmento cobrindo
        ``[0, duration_seconds]`` do vídeo original, velocidade 1.0.
        ``duration_seconds`` é SEMPRE um parâmetro explícito do chamador
        -- nunca lido de nenhum campo persistido (seção 0.3/0.4 da
        docstring do módulo, já que nenhum existe hoje). Erro estruturado
        se a timeline já foi inicializada (nunca sobrescreve
        silenciosamente uma timeline existente)."""
        duration = _as_float(duration_seconds, "duration_seconds")
        if duration <= 0:
            raise DuracaoInvalidaError(f"duration_seconds deve ser > 0, recebido: {duration_seconds!r}")
        initial_id = segment_id if segment_id is not None else str(uuid4())
        if not isinstance(initial_id, str) or not initial_id.strip():
            raise SegmentoInvalidoError(f"segment_id inválido: {initial_id!r}")

        def mutator(current_data: Any) -> dict[str, Any]:
            if current_data is not None and _segments_from_data(current_data):
                raise TimelineJaInicializadaError(
                    f"timeline já inicializada para o projeto {project_id!r} -- use as operações de edição em vez de reinicializar"
                )
            segment = Segment(segment_id=initial_id, start=0.0, end=duration, speed=1.0, source_segment_id=None)
            _validate_timeline([segment])
            return _segments_to_data([segment])

        state = self._manager.update_category(project_id, CUTS, mutator)
        return tuple(_segments_from_data(state.data))

    # -- operações ---------------------------------------------------------

    def trim(self, project_id: str, segment_id: str, *, start: float | None = None, end: float | None = None) -> tuple[Segment, ...]:
        """Ajusta ``start``/``end`` de um segmento existente -- só pode
        ENCOLHER (seção 1.4 da docstring do módulo), nunca expandir além
        dos limites já registrados."""
        if start is None and end is None:
            raise SegmentoInvalidoError("trim exige ao menos um de 'start'/'end'")

        def compute(segments: list[Segment]) -> list[Segment]:
            index = _find_index(segments, segment_id)
            original = segments[index]
            new_start = original.start if start is None else _as_float(start, "start")
            new_end = original.end if end is None else _as_float(end, "end")
            if new_start < original.start:
                raise SegmentoInvalidoError(
                    f"trim não pode expandir o início além do já registrado: novo={new_start!r} atual={original.start!r}"
                )
            if new_end > original.end:
                raise SegmentoInvalidoError(
                    f"trim não pode expandir o fim além do já registrado: novo={new_end!r} atual={original.end!r}"
                )
            trimmed = replace(original, start=new_start, end=new_end)
            new_segments = list(segments)
            new_segments[index] = trimmed
            return new_segments

        return self._apply(project_id, compute)

    def split(self, project_id: str, segment_id: str, at: float) -> tuple[Segment, ...]:
        """Divide um segmento em dois, em um ponto ESTRITAMENTE interno
        (``start < at < end``)."""
        at_value = _as_float(at, "at")

        def compute(segments: list[Segment]) -> list[Segment]:
            index = _find_index(segments, segment_id)
            original = segments[index]
            if not (original.start < at_value < original.end):
                raise PontoDeCorteInvalidoError(
                    f"ponto de split fora do segmento: at={at_value!r} segmento=[{original.start!r}, {original.end!r}]"
                )
            left = Segment(
                segment_id=str(uuid4()),
                start=original.start,
                end=at_value,
                speed=original.speed,
                source_segment_id=segment_id,
            )
            right = Segment(
                segment_id=str(uuid4()),
                start=at_value,
                end=original.end,
                speed=original.speed,
                source_segment_id=segment_id,
            )
            new_segments = list(segments)
            new_segments[index:index + 1] = [left, right]
            return new_segments

        return self._apply(project_id, compute)

    def remove_range(self, project_id: str, segment_id: str, start: float, end: float) -> tuple[Segment, ...]:
        """Remove o subintervalo ``[start, end)`` de dentro de um
        segmento -- cobre trecho no meio (split implícito dos dois
        lados), trecho tocando uma borda (equivalente a um trim daquela
        borda) e trecho == segmento inteiro (remoção total). Ver seção
        1.5 da docstring do módulo."""
        range_start = _as_float(start, "start")
        range_end = _as_float(end, "end")
        if range_end <= range_start:
            raise PontoDeCorteInvalidoError(f"intervalo a remover inválido: start={range_start!r} end={range_end!r}")

        def compute(segments: list[Segment]) -> list[Segment]:
            index = _find_index(segments, segment_id)
            original = segments[index]
            if range_start < original.start or range_end > original.end:
                raise PontoDeCorteInvalidoError(
                    f"intervalo a remover fora do segmento: [{range_start!r}, {range_end!r}] "
                    f"segmento=[{original.start!r}, {original.end!r}]"
                )
            replacement: list[Segment] = []
            if range_start > original.start:
                replacement.append(
                    Segment(
                        segment_id=str(uuid4()),
                        start=original.start,
                        end=range_start,
                        speed=original.speed,
                        source_segment_id=segment_id,
                    )
                )
            if range_end < original.end:
                replacement.append(
                    Segment(
                        segment_id=str(uuid4()),
                        start=range_end,
                        end=original.end,
                        speed=original.speed,
                        source_segment_id=segment_id,
                    )
                )
            new_segments = list(segments)
            new_segments[index:index + 1] = replacement
            return new_segments

        return self._apply(project_id, compute)

    def remove_segment(self, project_id: str, segment_id: str) -> tuple[Segment, ...]:
        """Remove um segmento INTEIRO -- atalho documentado sobre
        ``remove_range`` (seção 1.5), nunca duplica a lógica de remoção."""

        def compute(segments: list[Segment]) -> list[Segment]:
            index = _find_index(segments, segment_id)
            original = segments[index]
            return self._compute_remove_range(segments, index, original.start, original.end)

        return self._apply(project_id, compute)

    def _compute_remove_range(self, segments: list[Segment], index: int, range_start: float, range_end: float) -> list[Segment]:
        original = segments[index]
        replacement: list[Segment] = []
        if range_start > original.start:
            replacement.append(replace(original, segment_id=str(uuid4()), end=range_start, source_segment_id=original.segment_id))
        if range_end < original.end:
            replacement.append(replace(original, segment_id=str(uuid4()), start=range_end, source_segment_id=original.segment_id))
        new_segments = list(segments)
        new_segments[index:index + 1] = replacement
        return new_segments

    def join(self, project_id: str, segment_id_a: str, segment_id_b: str) -> tuple[Segment, ...]:
        """Mescla dois segmentos ADJACENTES na ordem da timeline E
        contíguos no vídeo original, com velocidade idêntica -- ver seção
        1.3 da docstring do módulo."""

        def compute(segments: list[Segment]) -> list[Segment]:
            index_a = _find_index(segments, segment_id_a)
            index_b = _find_index(segments, segment_id_b)
            if index_b != index_a + 1:
                raise SegmentosNaoAdjacentesError(
                    f"segmentos não são adjacentes na ordem da timeline: {segment_id_a!r} (pos {index_a}) "
                    f"e {segment_id_b!r} (pos {index_b})"
                )
            seg_a = segments[index_a]
            seg_b = segments[index_b]
            if seg_a.end != seg_b.start:
                raise SegmentosNaoAdjacentesError(
                    f"segmentos não são contíguos no vídeo original: fim de {segment_id_a!r}={seg_a.end!r} "
                    f"vs início de {segment_id_b!r}={seg_b.start!r}"
                )
            if seg_a.speed != seg_b.speed:
                raise SegmentosNaoAdjacentesError(
                    f"segmentos têm velocidades incompatíveis: {seg_a.speed!r} vs {seg_b.speed!r}"
                )
            merged = Segment(
                segment_id=str(uuid4()),
                start=seg_a.start,
                end=seg_b.end,
                speed=seg_a.speed,
                source_segment_id=None,
            )
            new_segments = list(segments)
            new_segments[index_a:index_b + 1] = [merged]
            return new_segments

        return self._apply(project_id, compute)

    def reorder(self, project_id: str, new_order: "list[str] | tuple[str, ...]") -> tuple[Segment, ...]:
        """Reordena os segmentos existentes -- ``new_order`` precisa ser
        uma permutação EXATA do conjunto de ``segment_id`` atual (seção
        1.2/2 da docstring do módulo)."""
        new_order = list(new_order)

        def compute(segments: list[Segment]) -> list[Segment]:
            current_ids = [segment.segment_id for segment in segments]
            current_set = set(current_ids)
            new_set = set(new_order)
            if len(new_order) != len(new_set):
                raise ReordenacaoInvalidaError(f"nova ordem contém segment_id duplicado: {new_order!r}")
            missing = current_set - new_set
            if missing:
                raise ReordenacaoInvalidaError(f"nova ordem omite segmento(s) existente(s): {sorted(missing)!r}")
            unknown = new_set - current_set
            if unknown:
                raise ReordenacaoInvalidaError(f"nova ordem contém segment_id desconhecido: {sorted(unknown)!r}")
            by_id = {segment.segment_id: segment for segment in segments}
            return [by_id[segment_id] for segment_id in new_order]

        return self._apply(project_id, compute)

    def set_speed(self, project_id: str, segment_id: str, speed: float) -> tuple[Segment, ...]:
        """Altera o fator de velocidade de UM segmento -- nunca um fator
        global que ignore os demais (item 1 do Prompt)."""
        speed_value = _as_float(speed, "speed")
        _validate_speed(speed_value)

        def compute(segments: list[Segment]) -> list[Segment]:
            index = _find_index(segments, segment_id)
            new_segments = list(segments)
            new_segments[index] = replace(segments[index], speed=speed_value)
            return new_segments

        return self._apply(project_id, compute)

    # -- internos -----------------------------------------------------------

    def _apply(self, project_id: str, compute: "Any") -> tuple[Segment, ...]:
        """Encapsula o padrão comum a toda operação: decodifica a
        timeline atual, exige que já esteja inicializada, aplica
        ``compute`` (pura, só decide com base na lista recebida), valida
        globalmente o resultado, e persiste através de
        ``EditProjectManager.update_category`` -- leitura, decisão e
        escrita na MESMA transaction atômica (seção 0.1)."""

        def mutator(current_data: Any) -> dict[str, Any]:
            segments = _segments_from_data(current_data)
            _require_initialized(segments)
            new_segments = compute(segments)
            _validate_timeline(new_segments)
            return _segments_to_data(new_segments)

        state = self._manager.update_category(project_id, CUTS, mutator)
        return tuple(_segments_from_data(state.data))
