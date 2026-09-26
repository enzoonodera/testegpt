# -*- coding: utf-8 -*-
"""ClipRankingEngine -- Prompt 47 (Fase 7: Smart Clip -- quarto prompt).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Cada candidato deve possuir internamente: start_time, end_time,
    duration, hook_score, context_score, speech_score, visual_score,
    completion_score, overall_score, reason.

    Expandir automaticamente início/fim para evitar:
    frases cortadas; história sem contexto; conclusão faltando.

    Evitar sobreposição excessiva entre cortes.

===========================================================================
0. VISÃO GERAL -- O QUE ESTE MÓDULO É E O QUE ELE NÃO É
===========================================================================

``ClipRankingEngine`` consome os ``ClipCandidate`` que
``SmartClipScoringEngine`` (Prompt 45) já produziu, um por
``SemanticSegment`` -- NUNCA recalcula nenhum sub-score
(``hook_score``/``context_score``/``speech_score``/``completion_score``/
``visual_score``/``audio_score``), NUNCA relê o transcript bruto, NUNCA
reimplementa segmentação. A responsabilidade NOVA deste módulo, não
coberta por nenhum outro, é: (1) decidir se as bordas (``start``/``end``)
de um candidato precisam ser expandidas com base em EVIDÊNCIA LOCAL já
calculada por Prompts anteriores; (2) ordenar os candidatos por
``overall_score``; (3) suprimir sobreposição excessiva entre cortes
aceitos, de forma auditável (GATE item 7 -- autoridade única: este é o
único módulo que decide bordas/ranking/overlap; nenhum outro módulo toma
essa decisão).

Este módulo explicitamente NÃO detecta tipo de conteúdo (Prompt 46, cujo
resultado nem é lido aqui) e NÃO implementa a tela de seleção do usuário
(Prompt 48).

===========================================================================
0.1 -- NOMES DE CAMPO: ``start``/``end`` (não ``start_time``/``end_time``)
===========================================================================

O roadmap descreve o CONCEITO ("start_time, end_time, duration, ...") --
não uma assinatura Python literal. ``ClipCandidate`` (Prompt 45, já
aprovado) já usa ``start``/``end`` -- os MESMOS nomes que
``SemanticSegment`` (Prompt 44) usa. Renomear o Artifact já aprovado do
Prompt 45 está fora de escopo e quebraria compatibilidade sem necessidade
(CLAUDE.md, item 5: "preserve tudo que já funciona"). Decisão: este
módulo também usa ``start``/``end`` -- mantendo UM ÚNICO par de nomes para
o mesmo conceito em toda a pipeline (``SemanticSegment`` ->
``ClipCandidate`` -> ``RankedClipCandidate``), em vez de introduzir um
segundo par de nomes (``start_time``/``end_time``) que representaria a
MESMA informação com um nome diferente -- fonte de confusão, não de
clareza. O único campo genuinamente novo exigido pelo roadmap e ausente
até aqui é ``duration`` (``end - start``), adicionado em
``RankedClipCandidate``. Testado em
``test_todos_os_campos_do_roadmap_estao_presentes``.

===========================================================================
0.2 -- EXPANSÃO DE BORDAS -- SEMPRE POR EVIDÊNCIA LOCAL JÁ CALCULADA
===========================================================================

Três motivos de expansão, cada um auditável (reason explícito) e cada um
consultando apenas campos que Prompts anteriores JÁ calcularam (nunca uma
nova análise de texto):

(a) **Frase cortada** (estende o FIM): ``completion_score`` do candidato
    original está abaixo de ``completion_expansion_threshold``
    (parâmetro do construtor, padrão
    ``COMPLETION_EXPANSION_THRESHOLD_PADRAO = 0.7`` -- ver seção 0.5 da
    docstring de ``smart_clip_scoring.py``: ``score_completion`` devolve
    exatamente ``0.7``/``1.0`` quando o texto termina em pontuação
    terminal, e ``0.3``/``0.6`` quando não termina -- ``0.7`` é portanto o
    limiar EXATO que separa "termina em pontuação terminal" de "não
    termina", nunca um valor arbitrário por tentativa e erro). Estende o
    fim, segmento a segmento (usando os candidatos vizinhos JÁ pontuados
    pelo Prompt 45, nunca uma nova pontuação), até incluir um segmento cujo
    próprio ``completion_score`` já atinja o limiar, ou até não haver mais
    segmentos seguintes, ou até o teto de expansão ser alcançado.

(b) **História sem contexto** (estende o INÍCIO): ``role ==
    "DEVELOPMENT"`` e ``topic_change is False`` no candidato original --
    exatamente o par de campos que o roadmap deste Prompt já indicou como
    evidência ("tipicamente DEVELOPMENT sem topic_change"), herdados do
    Prompt 44 sem reinterpretação. Estende o início por exatamente UM
    segmento anterior (o vizinho imediato), nunca mais que isso nesta
    categoria.

(c) **Conclusão faltando** (estende o FIM): ``role != "CONCLUSION"`` e o
    PRÓXIMO segmento tem ``topic_change is False`` (ou seja, o próximo
    segmento não inicia um assunto novo -- continua a mesma história/
    raciocínio). Estende o fim por exatamente UM segmento seguinte.

Todas as três expansões respeitam um ÚNICO teto combinado,
``max_expansion_seconds`` (padrão ``MAX_EXPANSION_SECONDS_PADRAO = 20.0``
segundos -- escolha documentada como um valor conservador para conteúdo
curto: expandir mais que isso arriscaria transformar um "corte
recomendado" curto em um trecho longo demais para o formato, contrariando
o propósito do Smart Clip; parametrizável sem mudança de código): a cada
tentativa de expansão, o tempo TOTAL já adicionado (início + fim,
somados) é recomputado e a expansão só é aplicada se o total permanecer
dentro do teto -- nunca por reason isoladamente, exatamente como o Prompt
exige ("teto nunca é ultrapassado, mesmo com múltiplos motivos
simultâneos"). Um candidato já bem formado (nenhum dos três motivos
dispara) nunca é expandido -- nenhuma expansão "porque pode".

Texto final = concatenação literal dos textos dos segmentos efetivamente
incluídos (``" ".join(...)``, na ordem original) -- NUNCA inventa texto
novo; os índices dos segmentos incluídos ficam auditáveis em
``source_candidate_indices``.

===========================================================================
0.3 -- ``split_gap_seconds``/``split_discourse_marker`` -- REAPROVEITADOS
===========================================================================

Quando uma expansão cruza a fronteira de um segmento, este módulo lê (via
o Artifact ``content_segments_track`` do Prompt 44, leitura best-effort,
nunca recalculada) os campos ``split_gap_seconds``/
``split_discourse_marker`` do segmento na fronteira e os inclui no texto
de auditoria (``expansion_reasons``) -- é a evidência REAL e já medida
(silêncio cronometrado ou marcador de discurso) de por que aquele corte
de segmento aconteceu, nunca um número recalculado por este módulo. Se o
Artifact de segmentos não estiver mais disponível no disco (ex.: limpeza
manual), a expansão continua funcionando normalmente usando apenas os
``ClipCandidate`` (que já carregam ``start``/``end``/``text``/``role``/
``topic_change``/``completion_score`` -- tudo que o algoritmo de expansão
realmente precisa) -- a leitura dos segmentos é estritamente um
enriquecimento de auditoria, nunca uma dependência dura.

===========================================================================
0.4 -- RANKING -- ``overall_score`` DESCENDENTE, DESEMPATE POR ``index``
===========================================================================

Ordenação determinística: chave ``(-overall_score, index)`` -- nunca
depende de ordem de inserção no banco ou de qualquer valor não
determinístico (timestamp, hash de objeto). Dois candidatos com o mesmo
``overall_score`` sempre ficam na mesma ordem relativa entre execuções
(``index`` menor primeiro).

===========================================================================
0.5 -- SUPRESSÃO DE SOBREPOSIÇÃO -- ``max_overlap_ratio``
===========================================================================

Em ordem de ranking (do melhor para o pior), cada candidato é comparado
apenas contra os candidatos JÁ ACEITOS de rank mais alto (nunca contra
candidatos já suprimidos -- um corte que não será mostrado ao usuário não
deveria impedir outro de aparecer). A métrica de sobreposição escolhida
-- ``coverage_ratio`` -- é a fração da PRÓPRIA duração do candidato sob
avaliação (o de rank mais baixo) que já está coberta por um candidato
aceito de rank mais alto:

    coverage_ratio = segundos_de_intersecao / duration(candidato_em_avaliacao)

Escolha deliberada (documentada, não Jaccard/união simétrica): um
candidato curto inteiramente contido dentro de um candidato aceito muito
mais longo deve ser suprimido (``coverage_ratio = 1.0``, quase certamente
redundante) mesmo que, do ponto de vista do candidato longo, aquela
sobreposição seja uma fração pequena de sua própria duração; o inverso
(um candidato longo que apenas tangencia um candidato aceito curto) não
deveria ser suprimido só por essa tangência pequena. Padrão
``MAX_OVERLAP_RATIO_PADRAO = 0.5``: um candidato só é suprimido quando
METADE ou mais de sua própria duração já está coberta por um corte melhor
-- sobreposição leve (abaixo do limiar) nunca suprime, ambos aparecem.
Candidatos suprimidos NUNCA desaparecem silenciosamente: ficam no Artifact
com ``accepted=False`` e ``suppressed_reason`` explicando qual candidato
de rank mais alto causou a supressão e qual foi a razão de sobreposição
calculada.

===========================================================================
0.6 -- ARTIFACT / CACHE / IDEMPOTÊNCIA -- SEM CHECKPOINT NOVO
===========================================================================

Novo Artifact ``kind="ranked_clip_candidates_track"`` (vocabulário novo).
``cache_key`` determinístico inclui o ``fingerprint`` do Artifact
``clip_candidates_track`` de origem (Prompt 45) MAIS todos os parâmetros
deste Prompt (``completion_expansion_threshold``,
``max_expansion_seconds``, ``max_overlap_ratio``) MAIS
``ALGORITHM_VERSION`` -- muda a origem OU qualquer parâmetro, o cache_key
muda; nada mudando, reaproveita. ``domain/checkpoints.py`` permanece
vocabulário fechado -- este handler NUNCA chama ``record_checkpoint`` nem
referencia nenhum ``CHECKPOINT_*``; idempotência resolvida inteiramente
por ``(video_id, kind, fingerprint)`` sobre o Artifact deste Prompt.

===========================================================================
0.7 -- ENCADEAMENTO -- ``JobEngine`` INTERNO E PRIVADO (mesmo padrão 44/45/46)
===========================================================================

Se o Artifact ``clip_candidates_track`` não existir/estiver ilegível, este
módulo aciona o handler REAL de ``SmartClipScoringEngine``
(``SCORE_CLIP_CANDIDATES``) através de um ``JobEngine`` interno e
privado -- nunca um shortcut simplificado. Como ``SmartClipScoringEngine``
já aciona, em cadeia, a segmentação (e esta, a transcrição) quando
necessário, uma única chamada a este mecanismo é suficiente para acionar
a cadeia completa: Ranking -> Scoring -> Segmentação -> Transcrição, cada
etapa através do seu próprio Job/JobEngine/Artifact reais.

===========================================================================
0.8 -- FORA DE ESCOPO (declarado explicitamente, nunca implementado aqui)
===========================================================================

Qualquer mudança em ``smart_clip_engine.py``, ``smart_clip_scoring.py``,
``content_type_detector.py`` ou ``domain/checkpoints.py``; leitura do
resultado do Prompt 46 (tipo de conteúdo); a tela de seleção do usuário
(Prompt 48); qualquer chamada a API externa, rede ou modelo de linguagem;
qualquer novo checkpoint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

from .app_paths import AppPaths
from .control_manager import ControlManager
from .domain import Artifact, Job, JOB_CANCELLED, JOB_FAILED, JOB_PROCESSING, JOB_READY
from .edit_project import EditProjectManager
from .job_engine import JobEngine, JobHandlerError, JobStepResult
from .smart_clip_engine import (
    ARTIFACT_KIND_CONTENT_SEGMENTS,
    SEGMENT_ROLE_CONCLUSION,
    SEGMENT_ROLE_DEVELOPMENT,
)
from .smart_clip_scoring import (
    ARTIFACT_KIND_CLIP_CANDIDATES,
    OPERATION_SCORE_CANDIDATES,
    SmartClipScoringEngine,
)
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager

__all__ = [
    "OPERATION_RANK_CANDIDATES",
    "ARTIFACT_KIND_RANKED_CANDIDATES",
    "ALGORITHM_VERSION",
    "COMPLETION_EXPANSION_THRESHOLD_PADRAO",
    "MAX_EXPANSION_SECONDS_PADRAO",
    "MAX_OVERLAP_RATIO_PADRAO",
    "RankedClipCandidate",
    "ClipRankingEngine",
    "compute_cache_key",
    "plan_expansion",
    "coverage_ratio",
    "rank_and_suppress",
]

OPERATION_RANK_CANDIDATES = "RANK_CLIP_CANDIDATES"

ARTIFACT_KIND_RANKED_CANDIDATES = "ranked_clip_candidates_track"

# Muda quando a LÓGICA de expansão/ranking/supressão muda (não os
# parâmetros -- esses já entram no cache_key por si só). Ver seção 0.6.
ALGORITHM_VERSION = "1"

# Ver seção 0.2(a) -- valor EXATO que separa "termina em pontuação
# terminal" (score_completion >= 0.7) de "não termina" (< 0.7).
COMPLETION_EXPANSION_THRESHOLD_PADRAO = 0.7

# Ver seção 0.2 -- teto combinado (início + fim) de segundos adicionados.
MAX_EXPANSION_SECONDS_PADRAO = 20.0

# Ver seção 0.5 -- fração da própria duração do candidato avaliado que já
# precisa estar coberta por um aceito de rank mais alto para ser suprimido.
MAX_OVERLAP_RATIO_PADRAO = 0.5


@dataclass(frozen=True)
class RankedClipCandidate:
    """Candidato final, após expansão de bordas, ranking e decisão de
    supressão por sobreposição. Ver seções 0.1-0.5 da docstring do
    módulo."""

    index: int
    start: float
    end: float
    duration: float
    text: str
    role: str
    topic_change: bool
    hook_score: float
    context_score: float
    speech_score: float
    completion_score: float
    visual_score: "float | None"
    audio_score: "float | None"
    overall_score: float
    reason: str
    expansion_reasons: "tuple[str, ...]"
    source_candidate_indices: "tuple[int, ...]"
    rank: int
    accepted: bool
    suppressed_reason: "str | None"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "duration": self.duration,
            "text": self.text,
            "role": self.role,
            "topic_change": self.topic_change,
            "hook_score": self.hook_score,
            "context_score": self.context_score,
            "speech_score": self.speech_score,
            "completion_score": self.completion_score,
            "visual_score": self.visual_score,
            "audio_score": self.audio_score,
            "overall_score": self.overall_score,
            "reason": self.reason,
            "expansion_reasons": list(self.expansion_reasons),
            "source_candidate_indices": list(self.source_candidate_indices),
            "rank": self.rank,
            "accepted": self.accepted,
            "suppressed_reason": self.suppressed_reason,
        }


# ---------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------


def _canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_cache_key(
    clip_candidates_cache_key: str,
    *,
    completion_expansion_threshold: float,
    max_expansion_seconds: float,
    max_overlap_ratio: float,
) -> str:
    """Ver seção 0.6 da docstring do módulo."""
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "clip_candidates_cache_key": str(clip_candidates_cache_key),
        "completion_expansion_threshold": float(completion_expansion_threshold),
        "max_expansion_seconds": float(max_expansion_seconds),
        "max_overlap_ratio": float(max_overlap_ratio),
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Expansão de bordas -- função pura, testável sem DB
# ---------------------------------------------------------------------


class _CandidateLike:
    """Protocolo mínimo esperado de cada item em ``candidates_by_index``:
    ``index``/``start``/``end``/``text``/``role``/``topic_change``/
    ``completion_score``. ``ClipCandidate`` (Prompt 45) já satisfaz este
    protocolo -- nenhuma conversão é necessária."""


def plan_expansion(
    candidates_by_index: "Mapping[int, Any]",
    original_index: int,
    *,
    completion_expansion_threshold: float = COMPLETION_EXPANSION_THRESHOLD_PADRAO,
    max_expansion_seconds: float = MAX_EXPANSION_SECONDS_PADRAO,
    segments_by_index: "Mapping[int, Mapping[str, Any]] | None" = None,
) -> "tuple[int, int, tuple[str, ...]]":
    """Decide as bordas finais (``start_index``, ``end_index``, inclusive
    em ambos os lados) para o candidato em ``original_index``, e a lista
    auditável de motivos. Ver seções 0.2-0.3 da docstring do módulo.
    Nunca modifica ``candidates_by_index`` -- função pura."""
    original = candidates_by_index[original_index]
    start_index = original_index
    end_index = original_index
    reasons: "list[str]" = []

    def _total_added_seconds(candidate_start_index: int, candidate_end_index: int) -> float:
        start_c = candidates_by_index[candidate_start_index]
        end_c = candidates_by_index[candidate_end_index]
        return (original.start - start_c.start) + (end_c.end - original.end)

    def _segment_hint(idx: int) -> str:
        if not segments_by_index:
            return ""
        seg = segments_by_index.get(idx)
        if not seg:
            return ""
        gap = seg.get("split_gap_seconds")
        marker = seg.get("split_discourse_marker")
        if gap is not None:
            return f" (evidência original: silêncio de {float(gap):.2f}s medido no Prompt 44)"
        if marker:
            return f" (evidência original: marcador de discurso {marker!r} detectado no Prompt 44)"
        return ""

    # (b) história sem contexto -- estende o início por exatamente 1 segmento
    if original.role == SEGMENT_ROLE_DEVELOPMENT and not original.topic_change:
        prev = candidates_by_index.get(original_index - 1)
        if prev is not None:
            trial = _total_added_seconds(prev.index, end_index)
            if trial <= max_expansion_seconds:
                start_index = prev.index
                reasons.append(
                    f"contexto: incluído segmento anterior #{prev.index} "
                    f"(candidato é DEVELOPMENT sem topic_change, sem contexto próprio)"
                    f"{_segment_hint(original_index)}"
                )

    # (c) conclusão faltando -- estende o fim por exatamente 1 segmento
    next_seg = candidates_by_index.get(original_index + 1)
    if (
        original.role != SEGMENT_ROLE_CONCLUSION
        and next_seg is not None
        and not next_seg.topic_change
    ):
        trial = _total_added_seconds(start_index, next_seg.index)
        if trial <= max_expansion_seconds:
            end_index = next_seg.index
            reasons.append(
                f"conclusão: incluído segmento seguinte #{next_seg.index} "
                f"(candidato não é CONCLUSION e o próximo continua a mesma história)"
                f"{_segment_hint(next_seg.index)}"
            )

    # (a) frase cortada -- estende o fim, segmento a segmento, até achar
    # pontuação terminal (completion_score do PRÓPRIO segmento incluído).
    if original.completion_score < completion_expansion_threshold:
        cursor = end_index + 1
        extended_to: "int | None" = None
        reached_terminal_text = ""
        while True:
            seg = candidates_by_index.get(cursor)
            if seg is None:
                break
            trial = _total_added_seconds(start_index, seg.index)
            if trial > max_expansion_seconds:
                break
            end_index = seg.index
            extended_to = seg.index
            if seg.completion_score >= completion_expansion_threshold:
                reached_terminal_text = " -- pontuação terminal encontrada"
                break
            cursor += 1
        if extended_to is not None:
            reasons.append(
                f"frase cortada: estendido até segmento #{extended_to} "
                f"(completion_score original {original.completion_score:.2f} < "
                f"{completion_expansion_threshold:.2f}){reached_terminal_text}"
                f"{_segment_hint(extended_to)}"
            )

    return start_index, end_index, tuple(reasons)


# ---------------------------------------------------------------------
# Ranking + supressão de sobreposição -- funções puras
# ---------------------------------------------------------------------


def coverage_ratio(candidate_start: float, candidate_end: float, other_start: float, other_end: float) -> float:
    """Ver seção 0.5 da docstring do módulo -- fração da duração de
    ``candidate`` que está coberta por ``other``. Assimétrico por
    design."""
    duration = candidate_end - candidate_start
    if duration <= 0:
        return 0.0
    intersection = min(candidate_end, other_end) - max(candidate_start, other_start)
    if intersection <= 0:
        return 0.0
    return min(1.0, intersection / duration)


def rank_and_suppress(
    expanded: "Sequence[RankedClipCandidate]",
    *,
    max_overlap_ratio: float = MAX_OVERLAP_RATIO_PADRAO,
) -> "tuple[RankedClipCandidate, ...]":
    """Ver seções 0.4-0.5 da docstring do módulo. Recebe candidatos JÁ
    expandidos (com ``rank``/``accepted``/``suppressed_reason`` ainda
    provisórios) e devolve a lista final, ordenada por rank."""
    ordered = sorted(expanded, key=lambda c: (-c.overall_score, c.index))
    accepted: "list[RankedClipCandidate]" = []
    results: "list[RankedClipCandidate]" = []

    for position, candidate in enumerate(ordered, start=1):
        suppressed_reason = None
        for higher in accepted:
            ratio = coverage_ratio(candidate.start, candidate.end, higher.start, higher.end)
            if ratio > max_overlap_ratio:
                suppressed_reason = (
                    f"suprimido: {ratio:.2f} da própria duração já coberta pelo candidato "
                    f"de rank mais alto #{higher.index} (rank {higher.rank}, "
                    f"overall_score {higher.overall_score:.2f}); "
                    f"limiar max_overlap_ratio={max_overlap_ratio:.2f}"
                )
                break
        final = replace(
            candidate, rank=position, accepted=suppressed_reason is None,
            suppressed_reason=suppressed_reason,
        )
        if final.accepted:
            accepted.append(final)
        results.append(final)

    return tuple(results)


# ---------------------------------------------------------------------
# ClipRankingEngine
# ---------------------------------------------------------------------


class ClipRankingEngine:
    """Job handler de ranking/expansão/supressão -- ver docstring do
    módulo para o contrato completo."""

    OPERATION: "ClassVar[str]" = OPERATION_RANK_CANDIDATES

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        storage_manager: StorageManager,
        app_paths: AppPaths,
        audit_log: "OperationalAuditLog | None" = None,
        control_manager: "ControlManager | None" = None,
        scoring_engine: "SmartClipScoringEngine | None" = None,
        completion_expansion_threshold: float = COMPLETION_EXPANSION_THRESHOLD_PADRAO,
        max_expansion_seconds: float = MAX_EXPANSION_SECONDS_PADRAO,
        max_overlap_ratio: float = MAX_OVERLAP_RATIO_PADRAO,
    ) -> None:
        if not isinstance(manager, EditProjectManager):
            raise TypeError(f"manager deve ser EditProjectManager (recebido: {type(manager)!r})")
        if not isinstance(database, LocalDatabase):
            raise TypeError(f"database deve ser LocalDatabase (recebido: {type(database)!r})")
        if not isinstance(storage_manager, StorageManager):
            raise TypeError(
                f"storage_manager deve ser StorageManager (recebido: {type(storage_manager)!r})"
            )
        if not isinstance(app_paths, AppPaths):
            raise TypeError(f"app_paths deve ser AppPaths (recebido: {type(app_paths)!r})")
        if control_manager is not None and not isinstance(control_manager, ControlManager):
            raise TypeError(
                f"control_manager deve ser ControlManager (recebido: {type(control_manager)!r})"
            )
        if scoring_engine is not None and not isinstance(scoring_engine, SmartClipScoringEngine):
            raise TypeError(
                f"scoring_engine deve ser SmartClipScoringEngine (recebido: {type(scoring_engine)!r})"
            )
        if completion_expansion_threshold < 0 or completion_expansion_threshold > 1:
            raise ValueError("completion_expansion_threshold deve estar entre 0 e 1")
        if max_expansion_seconds < 0:
            raise ValueError("max_expansion_seconds deve ser >= 0")
        if max_overlap_ratio < 0 or max_overlap_ratio > 1:
            raise ValueError("max_overlap_ratio deve estar entre 0 e 1")

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._control_manager = control_manager
        self._completion_expansion_threshold = float(completion_expansion_threshold)
        self._max_expansion_seconds = float(max_expansion_seconds)
        self._max_overlap_ratio = float(max_overlap_ratio)

        self._scoring_engine = scoring_engine if scoring_engine is not None else SmartClipScoringEngine(
            manager, database=database, storage_manager=storage_manager, app_paths=app_paths,
            audit_log=self._audit_log, control_manager=control_manager,
        )
        # JobEngine interno e privado -- só existe pra acionar o handler
        # real de scoring (Prompt 45) quando necessário (seção 0.7 da
        # docstring do módulo, mesmo padrão dos Prompts 44/45/46).
        self._scoring_job_engine = JobEngine(
            database, audit_log=self._audit_log, control_manager=control_manager,
        )
        self._scoring_job_engine.register_handler(
            OPERATION_SCORE_CANDIDATES, self._scoring_engine.handle_score_job,
            claims_status=JOB_PROCESSING,
        )

    # -- cancelamento --------------------------------------------------

    def _cancel_requested(self, job_id: str) -> bool:
        if self._control_manager is None:
            return False
        return self._control_manager.is_job_cancel_requested(job_id)

    # -- clip_candidates_track: localizar ou acionar --------------------

    def _find_latest_candidates_artifact(self, video_id: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (video_id, ARTIFACT_KIND_CLIP_CANDIDATES),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _valid_candidates_artifact(self, video_id: str) -> "Artifact | None":
        artifact = self._find_latest_candidates_artifact(video_id)
        if artifact is None or not Path(artifact.path).is_file():
            return None
        return artifact

    def _trigger_scoring(self, video_id: str) -> "tuple[Artifact | None, JobStepResult | None]":
        """Aciona o handler real de ``SmartClipScoringEngine`` através do
        ``JobEngine`` interno (seção 0.7). Mesmo precedente exato de
        ``SmartClipScoringEngine._trigger_segmentation``."""
        scoring_job = self._audit_log.create_job(
            Job(video_id=video_id, operation=OPERATION_SCORE_CANDIDATES)
        )
        try:
            result_job = self._scoring_job_engine.advance(scoring_job.id)
        except JobHandlerError as exc:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "scoring_trigger_failed",
                    "scoring_job_id": scoring_job.id,
                    "detail": exc.__class__.__name__,
                },
            )
        if result_job.status != JOB_READY:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "scoring_not_ready",
                    "scoring_job_id": scoring_job.id,
                    "scoring_status": result_job.status,
                },
            )
        artifact = self._valid_candidates_artifact(video_id)
        if artifact is None:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "scoring_succeeded_but_artifact_missing",
                    "scoring_job_id": scoring_job.id,
                },
            )
        return artifact, None

    # -- content_segments_track: leitura best-effort para auditoria ------

    def _read_segments_by_index(self, video_id: str) -> "dict[int, dict[str, Any]]":
        """Best-effort -- ver seção 0.3 da docstring do módulo. Nunca
        levanta exceção, nunca aciona segmentação (se não existir, o
        enriquecimento é simplesmente omitido)."""
        try:
            with self._database.connection() as conn:
                row = conn.execute(
                    "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (video_id, ARTIFACT_KIND_CONTENT_SEGMENTS),
                ).fetchone()
            if row is None:
                return {}
            artifact = self._database.get(Artifact, row[0])
            if artifact is None or not Path(artifact.path).is_file():
                return {}
            raw = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
            segments = raw.get("segments")
            if not isinstance(segments, list):
                return {}
            return {int(seg["index"]): seg for seg in segments if isinstance(seg, dict) and "index" in seg}
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            return {}

    # -- leitura dos candidatos --------------------------------------------

    def _read_candidates(self, artifact: Artifact) -> "list[dict[str, Any]]":
        raw = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        candidates = raw.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError(f"artifact de candidatos {artifact.id!r} sem candidatos válidos")
        return candidates

    # -- cache/Artifact deste ranking -------------------------------------

    def _find_cached_artifact(self, video_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (video_id, ARTIFACT_KIND_RANKED_CANDIDATES, cache_key),
            ).fetchone()
        if row is None:
            return None
        artifact = self._database.get(Artifact, row[0])
        if artifact is None or not Path(artifact.path).is_file():
            return None
        return artifact

    def _final_path_for(self, video_id: str, cache_key: str) -> Path:
        return Path(self._app_paths.projects) / "ranked_clip_candidates" / video_id / f"{cache_key}.json"

    # -- construção dos candidatos expandidos -----------------------------

    def _build_expanded(
        self, raw_candidates: "list[dict[str, Any]]", segments_by_index: "dict[int, dict[str, Any]]",
    ) -> "tuple[RankedClipCandidate, ...]":
        parsed = [_ParsedCandidate.from_dict(c) for c in raw_candidates]
        by_index = {c.index: c for c in parsed}

        expanded: "list[RankedClipCandidate]" = []
        for candidate in parsed:
            start_index, end_index, expansion_reasons = plan_expansion(
                by_index, candidate.index,
                completion_expansion_threshold=self._completion_expansion_threshold,
                max_expansion_seconds=self._max_expansion_seconds,
                segments_by_index=segments_by_index,
            )
            included_indices = tuple(range(start_index, end_index + 1))
            included = [by_index[i] for i in included_indices if i in by_index]
            text = " ".join(c.text for c in included)
            new_start = by_index[start_index].start
            new_end = by_index[end_index].end
            expanded.append(
                RankedClipCandidate(
                    index=candidate.index, start=new_start, end=new_end,
                    duration=max(0.0, new_end - new_start), text=text,
                    role=candidate.role, topic_change=candidate.topic_change,
                    hook_score=candidate.hook_score, context_score=candidate.context_score,
                    speech_score=candidate.speech_score, completion_score=candidate.completion_score,
                    visual_score=candidate.visual_score, audio_score=candidate.audio_score,
                    overall_score=candidate.overall_score, reason=candidate.reason,
                    expansion_reasons=expansion_reasons,
                    source_candidate_indices=included_indices,
                    rank=0, accepted=True, suppressed_reason=None,
                )
            )
        return tuple(expanded)

    def _write_and_register_artifact(
        self, job: Job, cache_key: str, candidates_artifact: Artifact, ranked: "tuple[RankedClipCandidate, ...]",
    ) -> Artifact:
        payload = {
            "version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "clip_candidates_artifact_id": candidates_artifact.id,
            "clip_candidates_cache_key": candidates_artifact.fingerprint,
            "completion_expansion_threshold": self._completion_expansion_threshold,
            "max_expansion_seconds": self._max_expansion_seconds,
            "max_overlap_ratio": self._max_overlap_ratio,
            "candidates": [c.to_dict() for c in ranked],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        temp_path = self._storage.allocate_temp(suffix="_ranked_clip_candidates.json", create=True)
        try:
            temp_path.write_text(content, encoding="utf-8")
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        final_path = self._final_path_for(job.video_id, cache_key)
        try:
            promoted = self._storage.promote_to_final(temp_path, final_path, overwrite=True)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        artifact = Artifact(
            video_id=job.video_id, project_id=job.project_id, job_id=job.id,
            kind=ARTIFACT_KIND_RANKED_CANDIDATES, path=str(promoted), fingerprint=cache_key,
            size_bytes=len(content.encode("utf-8")),
        )
        self._database.insert(artifact)
        return artifact

    # ===================================================================
    # Job handler
    # ===================================================================

    def handle_rank_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})

        candidates_artifact = self._valid_candidates_artifact(job.video_id)
        if candidates_artifact is None:
            candidates_artifact, failure = self._trigger_scoring(job.video_id)
            if failure is not None:
                return failure

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_before_ranking"})

        cache_key = compute_cache_key(
            candidates_artifact.fingerprint,
            completion_expansion_threshold=self._completion_expansion_threshold,
            max_expansion_seconds=self._max_expansion_seconds,
            max_overlap_ratio=self._max_overlap_ratio,
        )

        cached = self._find_cached_artifact(job.video_id, cache_key)
        if cached is not None:
            return JobStepResult(
                target_status=JOB_READY,
                semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
                data={"cache_hit": True, "cache_key": cache_key, "artifact_id": cached.id},
            )

        try:
            raw_candidates = self._read_candidates(candidates_artifact)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "invalid_candidates_artifact", "detail": exc.__class__.__name__},
            )

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_after_read"})

        segments_by_index = self._read_segments_by_index(job.video_id)
        try:
            expanded = self._build_expanded(raw_candidates, segments_by_index)
        except (KeyError, ValueError, TypeError) as exc:
            # Payload de candidatos existe e é uma lista (checado em
            # ``_read_candidates``), mas algum item individual tem schema
            # inválido (campo ausente/tipo errado) -- falha honesta, nunca
            # deixa o Job em estado intermediário (GATE item 13).
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "invalid_candidate_schema", "detail": exc.__class__.__name__},
            )
        ranked = rank_and_suppress(expanded, max_overlap_ratio=self._max_overlap_ratio)

        artifact = self._write_and_register_artifact(job, cache_key, candidates_artifact, ranked)

        accepted_count = sum(1 for c in ranked if c.accepted)
        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={
                "cache_hit": False,
                "cache_key": cache_key,
                "artifact_id": artifact.id,
                "candidate_count": len(ranked),
                "accepted_count": accepted_count,
                "suppressed_count": len(ranked) - accepted_count,
            },
        )


@dataclass(frozen=True)
class _ParsedCandidate:
    """Representação mínima de um ``ClipCandidate`` (Prompt 45) lido de
    volta do JSON persistido -- nunca recalcula nenhum sub-score, apenas
    desserializa exatamente o que já foi calculado."""

    index: int
    start: float
    end: float
    text: str
    role: str
    topic_change: bool
    hook_score: float
    context_score: float
    speech_score: float
    completion_score: float
    visual_score: "float | None"
    audio_score: "float | None"
    overall_score: float
    reason: str

    @staticmethod
    def from_dict(data: "Mapping[str, Any]") -> "_ParsedCandidate":
        return _ParsedCandidate(
            index=int(data["index"]), start=float(data["start"]), end=float(data["end"]),
            text=str(data["text"]), role=str(data["role"]), topic_change=bool(data["topic_change"]),
            hook_score=float(data["hook_score"]), context_score=float(data["context_score"]),
            speech_score=float(data["speech_score"]), completion_score=float(data["completion_score"]),
            visual_score=(None if data.get("visual_score") is None else float(data["visual_score"])),
            audio_score=(None if data.get("audio_score") is None else float(data["audio_score"])),
            overall_score=float(data["overall_score"]), reason=str(data["reason"]),
        )
