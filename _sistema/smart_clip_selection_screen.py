# -*- coding: utf-8 -*-
"""ClipSelectionScreen -- Prompt 48 (Fase 7: Smart Clip -- quinto e último prompt).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Criar interface conceitual para resultados:
    Corte 1
    título/resumo
    00:12:21 → 00:13:04
    Excelente corte
    Preview
    Usuário pode: aceitar; rejeitar; editar início/fim; selecionar todos.
    Não expor scores técnicos por padrão.
    Cada corte aprovado vira novo Video/Project/Job normal.

===========================================================================
0. VISÃO GERAL -- "INTERFACE CONCEITUAL" = LÓGICA DE TELA, NUNCA GUI
===========================================================================

Mesmo precedente de ``TimelineEditor``/``VisualEditor``/``TemplateSelector``:
este módulo é a camada de lógica de negócio de uma tela -- o que ela
mostra (``list_clips``) e o que o usuário pode fazer nela (``decide``/
``edit_bounds``/``select_all``) -- sem nenhuma renderização. Um frontend
futuro só consome os dicionários devolvidos e chama os métodos.

Entrada única: o Artifact ``ranked_clip_candidates_track`` do Prompt 47.
Este módulo NUNCA recalcula score, borda, ranking ou supressão (autoridade
única continua sendo o ``ClipRankingEngine``), e NUNCA confunde o campo
``accepted`` do Prompt 47 (decisão AUTOMÁTICA de supressão por
sobreposição) com a decisão do USUÁRIO, que aqui se chama
``user_decision`` (``PENDING``/``ACCEPTED``/``REJECTED``).

Candidato com ``accepted=False`` (suprimido no Prompt 47) nunca aparece em
``list_clips`` e qualquer ação sobre ele (``decide``/``edit_bounds``) falha
com ``CorteSuprimidoError`` -- nunca é silenciosamente ignorado.
``select_all`` só considera candidatos ``accepted=True``.

===========================================================================
1. APRESENTAÇÃO PADRÃO vs. AVANÇADA
===========================================================================

Visão padrão (``include_scores=False``) -- o que o roadmap pede, nada de
score técnico: ``label`` ("Corte 1", posição entre os NÃO suprimidos na
ordem de rank do Prompt 47), ``title`` (seção 2), ``time_range``
(``HH:MM:SS → HH:MM:SS``, mesmo formato literal do roadmap), ``quality_label``
(seção 3), os limites vigentes ``start``/``end`` (necessários para o
Preview posicionar o player no vídeo original -- são tempo, não score),
``user_decision``, ``bounds_edited`` e, quando promovido, os ids do
``Video``/``Project`` criados. ``candidate_index``/``ranked_artifact_id``
aparecem porque são o identificador que o frontend precisa devolver nas
ações -- também não são score.

Visão avançada (``include_scores=True``, explícita): acrescenta
``overall_score``, todos os sub-scores, ``reason``, ``expansion_reasons``,
``rank``, ``role`` e os limites ORIGINAIS do Prompt 47.

"Preview": não há player aqui (não é GUI). O dicionário carrega
``preview`` = ``{"source_video_id", "start", "end"}`` -- tudo que um player
precisa para tocar o trecho do vídeo ORIGINAL sem renderizar nada.

===========================================================================
2. TÍTULO/RESUMO -- DERIVAÇÃO DETERMINÍSTICA DO TEXTO REAL, NUNCA IA
===========================================================================

Título por IA é Fase 8 (Prompts 49/50) e NÃO é antecipado aqui.
``derive_title`` usa exclusivamente o ``text`` já transcrito que o Prompt
47 gravou: normaliza espaços, pega a PRIMEIRA frase (até o primeiro ``.``,
``!``, ``?`` ou ``…`` seguido de espaço/fim) e, se ela passar de
``TITLE_MAX_CHARS`` (60), corta na última fronteira de palavra antes do
limite e acrescenta ``…``. Texto vazio -> ``"Trecho sem fala detectada"``.
Nunca inventa palavra, nunca chama modelo -- é um recorte literal da fala.

===========================================================================
3. ETIQUETA QUALITATIVA -- FAIXAS DE ``overall_score``
===========================================================================

``overall_score`` (Prompt 45) é média ponderada renormalizada de sub-scores
em ``[0, 1]``. Faixas (``QUALITY_BANDS``, limite inferior inclusivo):

    >= 0.75  "Excelente corte"
    >= 0.60  "Ótimo corte"
    >= 0.45  "Bom corte"
    <  0.45  "Corte possível"

Escolha documentada: o hook tem peso 0.30 e um candidato sem gancho
nenhum (hook 0) mas com todo o resto perfeito fica em 0.70 -- por isso
"Excelente" exige >= 0.75 (precisa de gancho real); 0.45 é o piso em que
metade dos sinais ponderados está presente. Nenhuma etiqueta afirma
audiência ("mais assistido" é proibido pelo CLAUDE.md item H) -- são
qualificadores de "corte recomendado". Nunca exibe o número na visão
padrão.

===========================================================================
4. PERSISTÊNCIA DAS DECISÕES -- ``audit_events`` (append-only no SQLite)
===========================================================================

Cada ação do usuário vira UMA linha nova em ``audit_events``
(``entity_type="SmartClipSelection"``, ``entity_id=<ranked_artifact_id>``):

    SMART_CLIP_USER_DECISION  {candidate_index, decision, start, end}
    SMART_CLIP_BOUNDS_EDITED  {candidate_index, start, end, previous_start, previous_end}
    SMART_CLIP_PROMOTED       {candidate_index, video_id, project_id, segment_id,
                               start, end, source_video_id}

O estado vigente de cada corte é o REPLAY desses eventos em ordem
(``rowid``) -- "o mais recente vence", mesma convenção do override do
Prompt 46. Nenhuma linha é jamais alterada/apagada (triggers da m002
negam UPDATE/DELETE): mudar de ideia acrescenta um evento, o histórico
inteiro continua em ``history()``. Os limites ORIGINAIS nunca são
tocados -- vêm sempre do JSON imutável do Prompt 47 (``original_bounds``).

Por que ``audit_events`` e não um Artifact-arquivo por decisão (sugestão
"ex." do Prompt, padrão do override do 46): a decisão de ACEITAR precisa
ser gravada NA MESMA transação que cria o ``Video``/``Project`` (GATE 2/3).
Um Artifact exige escrever um arquivo fora do SQLite antes de inserir a
linha -- janela de crash em que existiria decisão sem Video ou Video sem
decisão. ``audit_events`` é append-only imposto pelo próprio banco, entra
em ``BEGIN IMMEDIATE`` junto com os inserts, e já é usado com tipos de
evento próprios por ``circuit_breaker``/``batch_engine``. Nenhuma
migration nova.

===========================================================================
5. PROMOÇÃO -- CAMINHO PRÓPRIO, NUNCA ``VideoPromotionService.promote()``
===========================================================================

``VideoPromotionService.promote()`` (Prompt 27B) é idempotente por
``source_asset_id`` -- no máximo UM ``Video`` por origem. Um vídeo longo
gera N cortes aprovados -> N ``Video``s da mesma origem; chamar
``promote()`` devolveria sempre o mesmo ``Video`` já existente. O contrato
1:1 dele está correto para o propósito dele (promover importação bruta) e
NÃO é alterado nem chamado aqui. O schema permite N ``Video``s por origem
(sem índice único em ``videos.source_asset_id``).

Chave de idempotência PRÓPRIA: ``(ranked_artifact_id, candidate_index)``.
Os ids do ``Video``/``Project`` e do segmento inicial são ``uuid5``
determinísticos dessa chave (``promotion_ids``). Consequências:
- repetir ``decide(ACCEPTED)`` (sequencial, concorrente, pós-crash) nunca
  duplica -- dentro de ``BEGIN IMMEDIATE`` o evento ``SMART_CLIP_PROMOTED``
  já existente é reaproveitado; e mesmo que alguém contornasse essa
  checagem, a PRIMARY KEY do SQLite recusaria um segundo ``Video`` com o
  mesmo id (defesa em profundidade);
- os limites usados são os VIGENTES no momento da aceitação e ficam
  congelados no evento de promoção (histórico nunca reescrito); depois de
  promovido, ``edit_bounds`` falha com ``CorteJaPromovidoError`` -- ajustes
  posteriores são feitos no próprio Project via ``TimelineEditor``.

``Video`` novo: ``source_asset_id`` = o MESMO da origem longa (o recorte é
do mesmo arquivo original -- processamento não destrutivo), ``name`` =
``"<nome do vídeo de origem> - corte HH:MM:SS-HH:MM:SS"``. ``Project`` novo
aponta para ele. O vínculo "este Video veio do corte X do vídeo Y" fica
auditável no evento ``SMART_CLIP_PROMOTED`` (nenhum campo novo em
``Video``, nenhuma migration).

Recorte = ``TimelineEditor`` (Prompt 28), nunca uma segunda representação:
``initialize_timeline(project_id, end, segment_id=<uuid5>)`` cria
``Segment(0, end)`` e ``trim(start=start)`` o reduz a ``Segment(start,
end)`` -- exatamente o ``Segment`` relativo ao vídeo ORIGINAL que o
``RenderEngine`` já consome. Nenhuma escrita direta em ``edit_state``.
Limitação documentada: ``initialize_timeline`` recebe ``end`` como
duração (nenhuma duração do original é persistida hoje -- seção 0.3 de
``timeline_editor.py``), então dentro do Project o corte só pode ser
ENCOLHIDO; alargar é feito aqui com ``edit_bounds`` ANTES de aceitar.

"Job normal": nenhum ``Job`` é criado aqui. Criar um Job de render agora
seria acionar render implicitamente (fora de escopo -- render é ação
explícita e posterior). O ``Video``/``Project`` promovido é idêntico a
qualquer outro, então o fluxo normal de Job (render, template, legenda...)
se aplica a ele sem nada especial.

===========================================================================
6. CRASH / RESTART
===========================================================================

Fase 1 (atômica, ``BEGIN IMMEDIATE``): evento de decisão + ``Video`` +
``Project`` + evento de promoção. Crash antes do COMMIT -> nada existe.
Fase 2 (fora da transação, porque ``EditProjectManager`` abre a própria):
inicializar/aparar a timeline. Crash entre as fases -> Project existe com
timeline vazia (ou só com ``Segment(0, end)``). ``_ensure_timeline`` é
retomável e só age sobre esses dois estados exatos gerados por ele mesmo
(nunca sobrescreve edição feita pelo usuário no Project); é chamado de
novo por ``decide(ACCEPTED)`` repetido e por ``resume_pending_promotions``
(recuperação explícita pós-restart). Nenhum checkpoint novo --
``domain/checkpoints.py`` continua fechado.

===========================================================================
7. FORA DE ESCOPO
===========================================================================

Qualquer mudança em ``smart_clip_engine.py``/``smart_clip_scoring.py``/
``content_type_detector.py``/``smart_clip_ranking_engine.py``/
``domain/checkpoints.py``/``video_promotion.py``; título/resumo por IA;
Render automático; qualquer coisa da Fase 8; rede/API/modelo de linguagem.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping

from .domain import Artifact, Project, Video
from .smart_clip_ranking_engine import ARTIFACT_KIND_RANKED_CANDIDATES
from .storage.database import LocalDatabase
from .timeline_editor import TimelineEditor, TimelineEditorError, TimelineJaInicializadaError

__all__ = [
    "DECISION_ACCEPTED",
    "DECISION_REJECTED",
    "DECISION_PENDING",
    "EVENT_ENTITY_TYPE",
    "EVENT_USER_DECISION",
    "EVENT_BOUNDS_EDITED",
    "EVENT_PROMOTED",
    "QUALITY_BANDS",
    "TITLE_MAX_CHARS",
    "ClipSelectionError",
    "RankingNaoEncontradoError",
    "RankingIlegivelError",
    "CorteNaoEncontradoError",
    "CorteSuprimidoError",
    "DecisaoInvalidaError",
    "LimitesInvalidosError",
    "CorteJaPromovidoError",
    "VideoOrigemNaoEncontradoError",
    "ClipSelectionScreen",
    "derive_title",
    "format_timestamp",
    "format_time_range",
    "quality_label_for",
    "promotion_ids",
]

DECISION_ACCEPTED = "ACCEPTED"
DECISION_REJECTED = "REJECTED"
DECISION_PENDING = "PENDING"
_USER_DECISIONS = frozenset({DECISION_ACCEPTED, DECISION_REJECTED})

EVENT_ENTITY_TYPE = "SmartClipSelection"
EVENT_USER_DECISION = "SMART_CLIP_USER_DECISION"
EVENT_BOUNDS_EDITED = "SMART_CLIP_BOUNDS_EDITED"
EVENT_PROMOTED = "SMART_CLIP_PROMOTED"

# Seção 3 -- (limite inferior inclusivo, etiqueta), do maior para o menor.
QUALITY_BANDS: "tuple[tuple[float, str], ...]" = (
    (0.75, "Excelente corte"),
    (0.60, "Ótimo corte"),
    (0.45, "Bom corte"),
    (float("-inf"), "Corte possível"),
)

TITLE_MAX_CHARS = 60
_EMPTY_TITLE = "Trecho sem fala detectada"

# Namespace fixo dos uuid5 de promoção (seção 5). NUNCA mudar: mudaria os
# ids e quebraria a idempotência de promoções já feitas.
_PROMOTION_NAMESPACE = uuid.UUID("6f3c1a52-8d0e-5b7a-9c41-2e7d4b0f8a13")

# Campos técnicos -- só na visão avançada (seção 1).
_TECHNICAL_FIELDS = (
    "overall_score", "hook_score", "context_score", "speech_score",
    "completion_score", "visual_score", "audio_score",
)


# ---------------------------------------------------------------------
# Erros estruturados
# ---------------------------------------------------------------------


class ClipSelectionError(RuntimeError):
    code: ClassVar[str] = "CLIP_SELECTION_ERRO"


class RankingNaoEncontradoError(ClipSelectionError):
    code: ClassVar[str] = "RANKING_NAO_ENCONTRADO"


class RankingIlegivelError(ClipSelectionError):
    code: ClassVar[str] = "RANKING_ILEGIVEL"


class CorteNaoEncontradoError(ClipSelectionError):
    code: ClassVar[str] = "CORTE_NAO_ENCONTRADO"


class CorteSuprimidoError(ClipSelectionError):
    """O candidato foi suprimido por sobreposição no Prompt 47
    (``accepted=False``) -- nunca é apresentado e não aceita decisão."""

    code: ClassVar[str] = "CORTE_SUPRIMIDO"


class DecisaoInvalidaError(ClipSelectionError):
    code: ClassVar[str] = "DECISAO_INVALIDA"


class LimitesInvalidosError(ClipSelectionError):
    code: ClassVar[str] = "LIMITES_INVALIDOS"


class CorteJaPromovidoError(ClipSelectionError):
    code: ClassVar[str] = "CORTE_JA_PROMOVIDO"


class VideoOrigemNaoEncontradoError(ClipSelectionError):
    code: ClassVar[str] = "VIDEO_ORIGEM_NAO_ENCONTRADO"


# ---------------------------------------------------------------------
# Funções puras de apresentação
# ---------------------------------------------------------------------


_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")


def derive_title(text: Any, *, max_chars: int = TITLE_MAX_CHARS) -> str:
    """Seção 2 -- recorte literal e determinístico do texto transcrito."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return _EMPTY_TITLE
    match = _SENTENCE_END.search(normalized)
    sentence = normalized[: match.end()] if match else normalized
    if len(sentence) <= max_chars:
        return sentence
    cut = sentence[:max_chars]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_time_range(start: float, end: float) -> str:
    """``HH:MM:SS → HH:MM:SS`` -- início arredondado para baixo e fim para
    cima, para que o intervalo exibido sempre contenha o corte real."""
    return f"{format_timestamp(math.floor(start))} → {format_timestamp(math.ceil(end))}"


def quality_label_for(overall_score: float) -> str:
    for lower_bound, label in QUALITY_BANDS:
        if overall_score >= lower_bound:
            return label
    return QUALITY_BANDS[-1][1]


def promotion_ids(ranked_artifact_id: str, candidate_index: int) -> "tuple[str, str, str]":
    """Seção 5 -- ``(video_id, project_id, segment_id)`` determinísticos da
    chave de idempotência própria deste módulo."""
    key = f"{ranked_artifact_id}:{int(candidate_index)}"
    return (
        str(uuid.uuid5(_PROMOTION_NAMESPACE, f"video:{key}")),
        str(uuid.uuid5(_PROMOTION_NAMESPACE, f"project:{key}")),
        str(uuid.uuid5(_PROMOTION_NAMESPACE, f"segment:{key}")),
    )


def _validate_bound(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LimitesInvalidosError(f"{name} deve ser numérico, recebido: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise LimitesInvalidosError(f"{name} deve ser finito, recebido: {value!r}")
    if number < 0:
        raise LimitesInvalidosError(f"{name} deve ser >= 0, recebido: {value!r}")
    return number


# ---------------------------------------------------------------------
# Estado interno
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    index: int
    start: float
    end: float
    text: str
    role: str
    rank: int
    accepted: bool
    reason: str
    expansion_reasons: "tuple[str, ...]"
    scores: "Mapping[str, float | None]"

    @staticmethod
    def from_dict(data: "Mapping[str, Any]") -> "_Candidate":
        return _Candidate(
            index=int(data["index"]), start=float(data["start"]), end=float(data["end"]),
            text=str(data["text"]), role=str(data["role"]), rank=int(data["rank"]),
            accepted=bool(data["accepted"]), reason=str(data["reason"]),
            expansion_reasons=tuple(str(r) for r in data.get("expansion_reasons") or ()),
            scores={
                name: (None if data.get(name) is None else float(data[name]))
                for name in _TECHNICAL_FIELDS
            },
        )


@dataclass(frozen=True)
class _Ranking:
    artifact_id: str
    video_id: str
    candidates: "Mapping[int, _Candidate]"

    def visible(self) -> "list[_Candidate]":
        return sorted((c for c in self.candidates.values() if c.accepted), key=lambda c: c.rank)


@dataclass
class _ClipState:
    decision: str = DECISION_PENDING
    bounds: "tuple[float, float] | None" = None
    promotion: "dict[str, Any] | None" = None


def _replay(events: "list[dict[str, Any]]") -> "dict[int, _ClipState]":
    """Seção 4 -- estado vigente = replay dos eventos em ordem de ``rowid``."""
    states: "dict[int, _ClipState]" = {}
    for event in events:
        data = event.get("data") or {}
        if "candidate_index" not in data:
            continue
        state = states.setdefault(int(data["candidate_index"]), _ClipState())
        kind = event.get("event_type")
        if kind == EVENT_USER_DECISION:
            state.decision = str(data["decision"])
        elif kind == EVENT_BOUNDS_EDITED:
            state.bounds = (float(data["start"]), float(data["end"]))
        elif kind == EVENT_PROMOTED and state.promotion is None:
            state.promotion = dict(data)
    return states


# ---------------------------------------------------------------------
# ClipSelectionScreen
# ---------------------------------------------------------------------


class ClipSelectionScreen:
    """Lógica da tela de seleção do Smart Clip -- ver docstring do módulo."""

    def __init__(self, database: LocalDatabase) -> None:
        if not isinstance(database, LocalDatabase):
            raise TypeError(f"database deve ser LocalDatabase (recebido: {type(database)!r})")
        self._database = database
        self._timeline = TimelineEditor(database)

    # -- leitura do ranking (Prompt 47) ----------------------------------

    def latest_ranked_artifact_id(self, video_id: str) -> "str | None":
        """O ranking vigente de um vídeo -- o Artifact
        ``ranked_clip_candidates_track`` mais recente (convenção do projeto)."""
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (video_id, ARTIFACT_KIND_RANKED_CANDIDATES),
            ).fetchone()
        return None if row is None else str(row[0])

    def _load_ranking(self, ranked_artifact_id: str) -> _Ranking:
        try:
            uuid.UUID(str(ranked_artifact_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise RankingNaoEncontradoError(f"ranked_artifact_id inválido: {ranked_artifact_id!r}") from exc
        artifact = self._database.get(Artifact, str(ranked_artifact_id))
        if artifact is None or artifact.kind != ARTIFACT_KIND_RANKED_CANDIDATES:
            raise RankingNaoEncontradoError(f"ranking não encontrado: {ranked_artifact_id!r}")
        if artifact.video_id is None:
            raise RankingIlegivelError(f"ranking {artifact.id!r} sem video_id")
        try:
            raw = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
            items = raw["candidates"]
            if not isinstance(items, list):
                raise TypeError("candidates não é lista")
            candidates = {c.index: c for c in (_Candidate.from_dict(item) for item in items)}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Só o nome da classe -- nunca str(exc) (CLAUDE.md, GATE 10).
            raise RankingIlegivelError(
                f"ranking {artifact.id!r} ilegível ({exc.__class__.__name__})"
            ) from exc
        return _Ranking(artifact_id=artifact.id, video_id=artifact.video_id, candidates=candidates)

    @staticmethod
    def _require_visible(ranking: _Ranking, candidate_index: Any) -> _Candidate:
        if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
            raise CorteNaoEncontradoError(f"candidate_index deve ser int, recebido: {candidate_index!r}")
        candidate = ranking.candidates.get(candidate_index)
        if candidate is None:
            raise CorteNaoEncontradoError(
                f"corte #{candidate_index} não existe no ranking {ranking.artifact_id!r}"
            )
        if not candidate.accepted:
            raise CorteSuprimidoError(
                f"corte #{candidate_index} foi suprimido por sobreposição (Prompt 47) "
                "e não aceita decisão do usuário"
            )
        return candidate

    def _events(self, ranked_artifact_id: str, *, connection=None) -> "list[dict[str, Any]]":
        return self._database.list_audit_events(
            entity_type=EVENT_ENTITY_TYPE, entity_id=ranked_artifact_id, connection=connection,
        )

    # -- apresentação ------------------------------------------------------

    def _view(
        self, ranking: _Ranking, candidate: _Candidate, position: int, state: _ClipState, *,
        include_scores: bool,
    ) -> "dict[str, Any]":
        if state.promotion is not None:
            start, end = float(state.promotion["start"]), float(state.promotion["end"])
        elif state.bounds is not None:
            start, end = state.bounds
        else:
            start, end = candidate.start, candidate.end
        view: "dict[str, Any]" = {
            "ranked_artifact_id": ranking.artifact_id,
            "candidate_index": candidate.index,
            "position": position,
            "label": f"Corte {position}",
            "title": derive_title(candidate.text),
            "time_range": format_time_range(start, end),
            "quality_label": quality_label_for(candidate.scores["overall_score"] or 0.0),
            "start": start,
            "end": end,
            "preview": {"source_video_id": ranking.video_id, "start": start, "end": end},
            "user_decision": state.decision,
            "bounds_edited": (start, end) != (candidate.start, candidate.end),
            "promoted_video_id": None if state.promotion is None else state.promotion["video_id"],
            "promoted_project_id": None if state.promotion is None else state.promotion["project_id"],
        }
        if include_scores:
            view.update(dict(candidate.scores))
            view.update({
                "reason": candidate.reason,
                "expansion_reasons": list(candidate.expansion_reasons),
                "rank": candidate.rank,
                "role": candidate.role,
                "original_start": candidate.start,
                "original_end": candidate.end,
            })
        return view

    def list_clips(self, ranked_artifact_id: str, *, include_scores: bool = False) -> "tuple[dict[str, Any], ...]":
        """Cortes apresentáveis (só ``accepted=True`` do Prompt 47), em
        ordem de rank. ``include_scores=True`` = visão avançada explícita."""
        ranking = self._load_ranking(ranked_artifact_id)
        states = _replay(self._events(ranking.artifact_id))
        return tuple(
            self._view(ranking, c, position, states.get(c.index, _ClipState()), include_scores=include_scores)
            for position, c in enumerate(ranking.visible(), start=1)
        )

    def get_clip(self, ranked_artifact_id: str, candidate_index: int, *, include_scores: bool = False) -> "dict[str, Any]":
        ranking = self._load_ranking(ranked_artifact_id)
        self._require_visible(ranking, candidate_index)
        for clip in self.list_clips(ranked_artifact_id, include_scores=include_scores):
            if clip["candidate_index"] == candidate_index:
                return clip
        raise CorteNaoEncontradoError(f"corte #{candidate_index} não encontrado")  # pragma: no cover

    def original_bounds(self, ranked_artifact_id: str, candidate_index: int) -> "tuple[float, float]":
        """Limites do Prompt 47 -- sempre recuperáveis, nunca alterados."""
        ranking = self._load_ranking(ranked_artifact_id)
        candidate = self._require_visible(ranking, candidate_index)
        return candidate.start, candidate.end

    def history(self, ranked_artifact_id: str, candidate_index: "int | None" = None) -> "list[dict[str, Any]]":
        """Histórico append-only completo (auditoria)."""
        ranking = self._load_ranking(ranked_artifact_id)
        events = self._events(ranking.artifact_id)
        if candidate_index is None:
            return events
        return [e for e in events if (e.get("data") or {}).get("candidate_index") == candidate_index]

    # -- ações do usuário ---------------------------------------------------

    def decide(self, ranked_artifact_id: str, candidate_index: int, decision: str) -> "dict[str, Any]":
        """Aceitar/rejeitar. Aceitar promove a Video+Project (idempotente);
        rejeitar nunca cria nada. Repetir a decisão vigente é no-op."""
        decision = self._validate_decision(decision)
        ranking = self._load_ranking(ranked_artifact_id)
        candidate = self._require_visible(ranking, candidate_index)
        with self._database.transaction() as conn:
            states = _replay(self._events(ranking.artifact_id, connection=conn))
            promotion = self._apply_decision_locked(
                conn, ranking, candidate, states.get(candidate.index, _ClipState()), decision,
            )
        if promotion is not None:
            self._ensure_timeline(promotion)
        return self.get_clip(ranking.artifact_id, candidate.index)

    def select_all(self, ranked_artifact_id: str, decision: str = DECISION_ACCEPTED) -> "tuple[int, ...]":
        """Aplica ``decision`` a todos os cortes apresentáveis ainda
        ``PENDING``, numa única transação. Suprimidos nunca são tocados;
        cortes já decididos pelo usuário mantêm a decisão dele. Devolve os
        ``candidate_index`` afetados."""
        decision = self._validate_decision(decision)
        ranking = self._load_ranking(ranked_artifact_id)
        affected: "list[int]" = []
        promotions: "list[dict[str, Any]]" = []
        with self._database.transaction() as conn:
            states = _replay(self._events(ranking.artifact_id, connection=conn))
            for candidate in ranking.visible():
                state = states.get(candidate.index, _ClipState())
                if state.decision != DECISION_PENDING:
                    continue
                promotion = self._apply_decision_locked(conn, ranking, candidate, state, decision)
                affected.append(candidate.index)
                if promotion is not None:
                    promotions.append(promotion)
        for promotion in promotions:
            self._ensure_timeline(promotion)
        return tuple(affected)

    def edit_bounds(self, ranked_artifact_id: str, candidate_index: int, *, start: float, end: float) -> "dict[str, Any]":
        """Ajusta início/fim antes da promoção. Os limites originais do
        Prompt 47 continuam em ``original_bounds``/visão avançada."""
        new_start = _validate_bound(start, "start")
        new_end = _validate_bound(end, "end")
        if new_start >= new_end:
            raise LimitesInvalidosError(f"start deve ser < end (start={new_start!r}, end={new_end!r})")
        ranking = self._load_ranking(ranked_artifact_id)
        candidate = self._require_visible(ranking, candidate_index)
        with self._database.transaction() as conn:
            state = _replay(self._events(ranking.artifact_id, connection=conn)).get(candidate.index, _ClipState())
            if state.promotion is not None:
                raise CorteJaPromovidoError(
                    f"corte #{candidate.index} já virou o Project {state.promotion['project_id']} -- "
                    "ajuste o trecho no editor do próprio projeto"
                )
            previous = state.bounds or (candidate.start, candidate.end)
            if previous != (new_start, new_end):
                self._database.append_audit_event(
                    EVENT_BOUNDS_EDITED, entity_type=EVENT_ENTITY_TYPE, entity_id=ranking.artifact_id,
                    data={
                        "candidate_index": candidate.index, "start": new_start, "end": new_end,
                        "previous_start": previous[0], "previous_end": previous[1],
                    },
                    connection=conn,
                )
        return self.get_clip(ranking.artifact_id, candidate.index)

    def resume_pending_promotions(self, ranked_artifact_id: str) -> "tuple[int, ...]":
        """Recuperação pós-crash (seção 6): garante a timeline de todo corte
        já promovido. Idempotente; devolve os índices verificados."""
        ranking = self._load_ranking(ranked_artifact_id)
        states = _replay(self._events(ranking.artifact_id))
        done: "list[int]" = []
        for index in sorted(states):
            if states[index].promotion is not None:
                self._ensure_timeline(states[index].promotion)
                done.append(index)
        return tuple(done)

    # -- interno --------------------------------------------------------------

    @staticmethod
    def _validate_decision(decision: Any) -> str:
        if decision not in _USER_DECISIONS:
            raise DecisaoInvalidaError(
                f"decision deve ser {DECISION_ACCEPTED!r} ou {DECISION_REJECTED!r}, recebido: {decision!r}"
            )
        return str(decision)

    def _apply_decision_locked(
        self, conn, ranking: _Ranking, candidate: _Candidate, state: _ClipState, decision: str,
    ) -> "dict[str, Any] | None":
        """Dentro de ``BEGIN IMMEDIATE`` já aberta: leitura + decisão +
        escrita atômicas (GATE 2). Devolve a promoção quando aceito."""
        start, end = state.bounds or (candidate.start, candidate.end)
        if state.decision != decision:
            self._database.append_audit_event(
                EVENT_USER_DECISION, entity_type=EVENT_ENTITY_TYPE, entity_id=ranking.artifact_id,
                data={"candidate_index": candidate.index, "decision": decision, "start": start, "end": end},
                connection=conn,
            )
        if decision != DECISION_ACCEPTED:
            return None
        if state.promotion is not None:
            return state.promotion
        return self._promote_locked(conn, ranking, candidate.index, start, end)

    def _promote_locked(self, conn, ranking: _Ranking, candidate_index: int, start: float, end: float) -> "dict[str, Any]":
        source_video = self._database.get(Video, ranking.video_id, connection=conn)
        if source_video is None:
            raise VideoOrigemNaoEncontradoError(f"vídeo de origem não encontrado: {ranking.video_id!r}")
        video_id, project_id, segment_id = promotion_ids(ranking.artifact_id, candidate_index)
        name = (
            f"{source_video.name} - corte "
            f"{format_timestamp(math.floor(start))}-{format_timestamp(math.ceil(end))}"
        )
        # PRIMARY KEY determinística: um segundo insert do mesmo corte
        # falharia no SQLite mesmo se a checagem por evento fosse contornada.
        self._database.insert(
            Video(id=video_id, source_asset_id=source_video.source_asset_id, name=name, status="ACTIVE"),
            connection=conn,
        )
        self._database.insert(Project(id=project_id, video_id=video_id, name=name), connection=conn)
        promotion = {
            "candidate_index": candidate_index, "video_id": video_id, "project_id": project_id,
            "segment_id": segment_id, "start": start, "end": end, "source_video_id": ranking.video_id,
        }
        self._database.append_audit_event(
            EVENT_PROMOTED, entity_type=EVENT_ENTITY_TYPE, entity_id=ranking.artifact_id,
            data=promotion, connection=conn,
        )
        return promotion

    def _ensure_timeline(self, promotion: "Mapping[str, Any]") -> None:
        """Fase 2 retomável (seção 6). Só age nos dois estados que ela mesma
        produz: timeline vazia, ou exatamente ``Segment(0, end)`` com o
        segment_id determinístico. Qualquer outro estado = editado pelo
        usuário, nunca tocado."""
        project_id = str(promotion["project_id"])
        segment_id = str(promotion["segment_id"])
        start, end = float(promotion["start"]), float(promotion["end"])

        if not self._timeline.get_timeline(project_id):
            try:
                self._timeline.initialize_timeline(project_id, end, segment_id=segment_id)
            except TimelineJaInicializadaError:
                pass  # outra instância inicializou primeiro -- segue para o trim

        segments = self._timeline.get_timeline(project_id)
        if (
            start > 0
            and len(segments) == 1
            and segments[0].segment_id == segment_id
            and segments[0].start == 0.0
            and segments[0].end == end
        ):
            try:
                self._timeline.trim(project_id, segment_id, start=start)
            except TimelineEditorError:
                # Corrida: estado mudou entre a leitura e o trim (outra
                # instância aparou/usuário editou). Só é aceitável se o
                # resultado não for mais o estado inicial.
                current = self._timeline.get_timeline(project_id)
                if len(current) == 1 and current[0].segment_id == segment_id and current[0].start == 0.0:
                    raise
