# -*- coding: utf-8 -*-
"""SmartClipScoringEngine -- Prompt 45 (Fase 7: Smart Clip -- segundo prompt).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Smart Clip deve funcionar sem Google Analytics e sem API externa
    obrigatória. Usar sinais locais: transcrição, contexto, ritmo de
    fala, perguntas, respostas, hooks, histórias, emoção, frases fortes,
    mudanças visuais, áudio, completude do raciocínio. Gerar candidatos
    com scores internos. Não chamar score de "visualizações" ou
    "retenção real".

===========================================================================
0. VISÃO GERAL -- O QUE ESTE MÓDULO É E O QUE ELE NÃO É
===========================================================================

``SmartClipScoringEngine`` consome os ``SemanticSegment`` que
``SmartClipEngine`` (Prompt 44) já produziu -- NUNCA relê o transcript
bruto diretamente, NUNCA duplica a segmentação semântica. Para cada
segmento, calcula sub-scores auditáveis a partir de sinais locais (nenhuma
API externa, nenhuma chamada de rede) e combina tudo num ``overall_score``
interno, gerando exatamente UM candidato por segmento -- nem descarta, nem
duplica, nem funde segmentos. Este módulo explicitamente NÃO expande
bordas de um candidato nem deduplica candidatos entre si (Prompt 47) e não
detecta tipo de conteúdo (Prompt 46).

**Vocabulário proibido, de propósito**: em nenhum lugar deste módulo --
código, docstring, nome de campo, mensagem de log -- o resultado é
chamado de "visualizações" ou "retenção real". O roadmap pede
explicitamente que isso nunca aconteça, porque o produto não tem acesso a
métricas reais de audiência (CLAUDE.md, princípio H, "Smart Clip"): os
nomes usados são sempre ``score``/``overall_score``/sub-scores nomeados
pelo SINAL que os origina (``hook_score``, ``context_score``,
``speech_score``, ``completion_score``, ``visual_score``,
``audio_score``), nunca uma promessa de resultado real de audiência.
Testado estruturalmente (seção 0.6).

===========================================================================
0.1 -- OS DOZE SINAIS DO ROADMAP -> SEIS SUB-SCORES COMPUTÁVEIS HOJE
===========================================================================

O roadmap lista doze sinais: transcrição, contexto, ritmo de fala,
perguntas, respostas, hooks, histórias, emoção, frases fortes, mudanças
visuais, áudio, completude do raciocínio. Investigação real do código
(Prompt 44 e módulos irmãos) mostra que nem todos têm um backend real
disponível hoje -- este módulo nunca finge o contrário:

- **transcrição**: já é a matéria-prima de tudo aqui (o texto de cada
  ``SemanticSegment`` vem da transcrição real do Prompt 31/44) -- não é
  um sub-score isolado, é a base de todos os outros.
- **contexto** -> ``context_score`` (seção 0.2): usa ``role``/
  ``topic_change`` que o Prompt 44 já calculou -- nenhuma reimplementação.
- **ritmo de fala** -> ``speech_score`` (seção 0.3): palavras / duração do
  segmento -- diretamente computável, sem backend.
- **perguntas, respostas, hooks, histórias, emoção, frases fortes** ->
  agrupados em ``hook_score`` (seção 0.4): todos são, na prática, a mesma
  categoria de sinal -- "o quanto este trecho, isolado, prende atenção" --
  cada um coberto por um léxico/heurística PRÓPRIA e reportado
  separadamente em ``reason`` (nunca colapsados num número só sem
  explicação).
- **completude do raciocínio** -> ``completion_score`` (seção 0.5):
  aproximação honesta via ``role == CONCLUSION`` + pontuação final --
  nunca uma análise semântica real (isso exigiria IA, fora de escopo).
- **mudanças visuais** -> ``visual_score`` (seção 0.6): backend
  injetável, SEM implementação real hoje (mesma decisão de
  ``auto_reframe.py``) -- default sempre devolve "sem sinal".
- **áudio** -> ``audio_score`` (seção 0.6): backend injetável, SEM
  implementação real hoje (mesma decisão de ``silence_removal.py``) --
  default sempre devolve "sem sinal".

===========================================================================
0.2 -- ``context_score`` -- papel estrutural + mudança de assunto
===========================================================================

Reaproveita EXATAMENTE os campos que ``SmartClipEngine`` já calculou
(``role``/``topic_change``), nunca reimplementa segmentação. Heurística:
um segmento ``BEGINNING`` ou ``CONCLUSION`` tende a ser mais autossuficiente
(faz sentido sem contexto anterior) que um ``DEVELOPMENT`` (que
tipicamente pressupõe algo já dito antes) -- e um segmento que É uma
mudança de assunto (``topic_change=True``) tende a introduzir sua própria
ideia do zero, também mais autossuficiente que continuar um raciocínio já
em andamento. Nem toda combinação é possível na prática (o primeiro
segmento nunca tem ``topic_change=True``, por definição do Prompt 44),
mas a fórmula é honesta com qualquer entrada válida.

===========================================================================
0.3 -- ``speech_score`` -- ritmo de fala
===========================================================================

``palavras / duração_segundos``. Fora de uma faixa "confortável" de fala
(``SPEECH_RATE_MIN_WPS``/``SPEECH_RATE_MAX_WPS``, ambos parâmetros do
construtor), o score cai linearmente até 0 -- nem fala muito lenta
(arrastada, pouco energética) nem muito rápida (difícil de acompanhar em
formato curto) pontua bem. Valores padrão (``1.5``-``3.5`` palavras/s) são
uma faixa conservadora de fala falada em português, não validada
empiricamente contra vídeos reais -- documentado como heurística v1,
ajustável sem mudança de código.

===========================================================================
0.4 -- ``hook_score`` -- perguntas, respostas, hooks, histórias, emoção,
        frases fortes
===========================================================================

Seis heurísticas locais independentes, cada uma um léxico/padrão curado
(mesmo espírito de ``DEFAULT_DISCOURSE_MARKERS`` do Prompt 44), cada uma
tunável via parâmetro do construtor:

- ``has_question``: o texto contém ``"?"``.
- ``has_answer_cue``: o texto contém uma expressão de resposta/conclusão
  local ("a resposta é", "por isso", "no final", "é assim que").
- ``hook_lexicon``: abertura de gancho de atenção ("você sabia", "ninguém
  te conta", "o erro que", "isso vai mudar").
- ``story_lexicon``: sinalização de narrativa pessoal ("um dia", "aconteceu
  comigo", "certa vez", "eu lembro").
- ``emotion_lexicon``: expressão de emoção forte ("incrível", "chocante",
  "não acreditei", "emocionante").
- ``strong_phrase_lexicon``: afirmação categórica/superlativa ("nunca",
  "sempre", "a verdade é", "ninguém fala sobre isso").

``hook_score`` é a fração de categorias que dispararam (0 a 6 categorias
-> 0.0 a 1.0) -- nunca um único número opaco: cada categoria disparada
entra, nomeada, na lista ``hook_signals`` do candidato e no texto de
``reason``. Um segmento pode disparar zero categorias (hook_score=0.0,
sem inventar sinal onde não há).

===========================================================================
0.5 -- ``completion_score`` -- completude do raciocínio
===========================================================================

Aproximação honesta, documentada como tal (nunca uma análise semântica
real de "a ideia terminou aqui"): ``0.7`` se o texto termina em pontuação
terminal (``.``/``!``/``?``), ``0.3`` caso contrário (frase claramente
cortada no meio), mais ``+0.3`` se ``role == CONCLUSION`` (papel
estrutural que o Prompt 44 já atribuiu ao último segmento) -- somado e
limitado a ``1.0``. Um ``DEVELOPMENT``/``BEGINNING`` que termina em
pontuação terminal ainda pontua razoavelmente (``0.7``) porque a FRASE
está completa, mesmo que o RACIOCÍNIO da seção inteira ainda continue no
próximo segmento -- essa é exatamente a limitação documentada: este é um
proxy sintático, não uma compreensão real do argumento.

===========================================================================
0.6 -- ``visual_score``/``audio_score`` -- SEM BACKEND REAL HOJE
===========================================================================

Achado crítico herdado da investigação dos módulos irmãos: nenhuma
biblioteca real de visão computacional (``auto_reframe.py``,
``_default_detect``) ou análise de áudio (``silence_removal.py``,
``_default_detect_silences``) está integrada nesta etapa -- ambas
documentadas como stubs deliberados. Este módulo HERDA a mesma decisão
honesta, com backends PRÓPRIOS (não reaproveita os tipos de
``auto_reframe``/``silence_removal`` -- aqueles respondem perguntas
diferentes: geometria de enquadramento e intervalos de silêncio pra corte,
não "o quanto este trecho é visual/sonoramente dinâmico"; reaproveitar o
tipo errado por conveniência violaria autoridade única, GATE item 7):

- ``VisualActivityBackend``/``AudioActivityBackend``: ``Callable[[str,
  float, float], ActivitySample]`` -- ``(local_path, start_seconds,
  end_seconds) -> ActivitySample``. ``ActivitySample.available`` é
  ``False`` por padrão (backend padrão -- seção "Backends padrão" abaixo)
  -- NUNCA fabrica um valor.
- Quando ``available=False``, o sub-score correspondente é persistido
  como ``None`` no candidato (nunca ``0.0`` -- ``0.0`` pareceria "sinal
  real, e ele é ruim", quando na verdade é "não sabemos"). O
  ``overall_score`` (seção 0.7) exclui sub-scores ``None`` do cálculo e
  RENORMALIZA os pesos restantes -- a ausência de visual/áudio nunca
  penaliza silenciosamente um candidato, nem finge que o sinal existe.

===========================================================================
0.7 -- ``overall_score`` -- combinação, nunca "visualizações"/"retenção"
===========================================================================

Média ponderada dos sub-scores DISPONÍVEIS (pesos padrão em
``DEFAULT_SCORE_WEIGHTS``, tunáveis via construtor), renormalizada sobre
os pesos dos sub-scores que não são ``None``. ``reason`` é uma string
legível listando os sinais que mais contribuíram (nunca só o número) --
ver ``_build_reason``.

===========================================================================
0.8 -- ARTIFACT / CACHE / IDEMPOTÊNCIA -- SEM CHECKPOINT NOVO
===========================================================================

Novo Artifact ``kind="clip_candidates_track"`` (vocabulário novo,
primeira vez introduzido -- nenhum candidato existia antes deste Prompt),
um candidato por ``SemanticSegment`` de origem (mesmos ``start``/``end``,
sem expansão nem deduplicação -- Prompt 47). ``cache_key`` determinístico
inclui o ``fingerprint`` do Artifact ``content_segments_track`` de origem
(se a segmentação mudar, os candidatos precisam ser recalculados) MAIS
todos os parâmetros de scoring (pesos, faixas de ritmo de fala, léxicos)
MAIS ``ALGORITHM_VERSION`` -- mesmo padrão de ``compute_cache_key`` do
Prompt 44.

``domain/checkpoints.py`` é vocabulário fechado por design (documentado
na própria docstring daquele módulo) -- ``CHECKPOINT_ANALYZED`` já foi
consumido pelo Prompt 44 para a segmentação; inventar ou reutilizar um
checkpoint aqui seria dishonesto (marcaria uma etapa que não é esta) ou
uma mudança arquitetural fora de escopo (adicionar vocabulário novo a um
módulo fechado). Este handler NUNCA chama ``record_checkpoint`` --
idempotência é resolvida inteiramente por ``(video_id, kind, fingerprint)``
sobre o Artifact de candidatos, exatamente como ``FinalMediaValidator``
resolve idempotência sobre validação sem precisar de um checkpoint
dedicado para cada checagem.

===========================================================================
0.9 -- TRANSCRIÇÃO/SEGMENTAÇÃO NUNCA REIMPLEMENTADAS
===========================================================================

Mesmo padrão de "JobEngine interno e privado" do Prompt 44 (lá,
justificado em detalhe): este módulo localiza o Artifact
``content_segments_track`` mais recente e válido para o ``video_id``; se
ausente, aciona o Job real de ``SmartClipEngine`` (``ANALYZE_CONTENT``)
através de um ``JobEngine`` interno e privado -- nunca duplica a lógica de
segmentação. Encadeamento total possível: Scoring aciona Segmentação, que
por sua vez aciona Transcrição, cada uma através do MESMO mecanismo real
(Job/JobEngine/checkpoint/Artifact), nunca uma casca simplificada.

===========================================================================
0.10 -- FORA DE ESCOPO (declarado explicitamente, nunca implementado aqui)
===========================================================================

Expansão de bordas de um candidato e deduplicação entre candidatos
(Prompt 47); detecção de tipo de conteúdo (Prompt 46); qualquer
integração com ``RenderEngine``; qualquer chamada a API externa; qualquer
mudança em ``domain/checkpoints.py``; qualquer mudança de comportamento em
``auto_reframe.py``/``silence_removal.py`` (backends próprios, nunca
tocados); qualquer novo checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

from .app_paths import AppPaths
from .control_manager import ControlManager
from .domain import Artifact, Job, JOB_CANCELLED, JOB_FAILED, JOB_PROCESSING, JOB_READY
from .edit_project import EditProjectManager
from .job_engine import JobEngine, JobHandlerError, JobStepResult
from .smart_clip_engine import (
    ARTIFACT_KIND_CONTENT_SEGMENTS,
    OPERATION_ANALYZE_CONTENT,
    SEGMENT_ROLE_CONCLUSION,
    SmartClipEngine,
)
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager

__all__ = [
    "OPERATION_SCORE_CANDIDATES",
    "ARTIFACT_KIND_CLIP_CANDIDATES",
    "ALGORITHM_VERSION",
    "SPEECH_RATE_MIN_WPS_PADRAO",
    "SPEECH_RATE_MAX_WPS_PADRAO",
    "DEFAULT_SCORE_WEIGHTS",
    "DEFAULT_QUESTION_MARKERS",
    "DEFAULT_ANSWER_CUE_LEXICON",
    "DEFAULT_HOOK_LEXICON",
    "DEFAULT_STORY_LEXICON",
    "DEFAULT_EMOTION_LEXICON",
    "DEFAULT_STRONG_PHRASE_LEXICON",
    "ActivitySample",
    "ClipCandidate",
    "SmartClipScoringEngine",
    "compute_cache_key",
    "score_context",
    "score_speech_rate",
    "score_hook",
    "score_completion",
    "combine_overall",
]

OPERATION_SCORE_CANDIDATES = "SCORE_CLIP_CANDIDATES"

ARTIFACT_KIND_CLIP_CANDIDATES = "clip_candidates_track"

# Muda quando a LÓGICA de scoring muda (não os parâmetros -- esses já
# entram no cache_key por si só). Ver seção 0.8 da docstring do módulo.
ALGORITHM_VERSION = "1"

SPEECH_RATE_MIN_WPS_PADRAO = 1.5
SPEECH_RATE_MAX_WPS_PADRAO = 3.5

# Nomes de sub-score reaproveitados do vocabulário já reservado pelo
# Prompt 47 (evita retrabalho -- ver seção 6 do Prompt) -- "audio_score"
# é uma adição própria deste Prompt (o roadmap lista "áudio" como sinal
# distinto de "mudanças visuais"; Prompt 47 ainda não definiu esse nome,
# então nenhuma colisão é possível).
DEFAULT_SCORE_WEIGHTS: "Mapping[str, float]" = {
    "hook_score": 0.30,
    "context_score": 0.20,
    "completion_score": 0.20,
    "speech_score": 0.15,
    "visual_score": 0.10,
    "audio_score": 0.05,
}

DEFAULT_QUESTION_MARKERS: "frozenset[str]" = frozenset({"?"})

DEFAULT_ANSWER_CUE_LEXICON: "frozenset[str]" = frozenset({
    "a resposta é", "a resposta e", "por isso", "no final", "é assim que",
    "e assim que", "a solução é", "a solucao e", "então a resposta",
    "entao a resposta",
})

DEFAULT_HOOK_LEXICON: "frozenset[str]" = frozenset({
    "você sabia", "voce sabia", "ninguém te conta", "ninguem te conta",
    "o erro que", "isso vai mudar", "poucas pessoas sabem", "presta atenção",
    "presta atencao", "isso muda tudo",
})

DEFAULT_STORY_LEXICON: "frozenset[str]" = frozenset({
    "um dia", "aconteceu comigo", "certa vez", "eu lembro", "outro dia",
    "uma vez", "isso me aconteceu",
})

DEFAULT_EMOTION_LEXICON: "frozenset[str]" = frozenset({
    "incrível", "incrivel", "chocante", "não acreditei", "nao acreditei",
    "emocionante", "impressionante", "revoltante", "surreal",
})

DEFAULT_STRONG_PHRASE_LEXICON: "frozenset[str]" = frozenset({
    "nunca", "sempre", "a verdade é", "a verdade e",
    "ninguém fala sobre isso", "ninguem fala sobre isso", "o maior erro",
    "a maior mentira",
})


@dataclass(frozen=True)
class ActivitySample:
    """Resultado de UMA chamada a um backend de atividade visual/sonora,
    para UM intervalo de tempo. ``activity`` (0.0-1.0) só é significativo
    quando ``available=True`` -- ver seção 0.6 da docstring do módulo."""

    available: bool
    activity: "float | None" = None


# Backends injetáveis -- própria seção 0.6: nunca reaproveita os tipos de
# auto_reframe.py/silence_removal.py (perguntas diferentes).
VisualActivityBackend = Callable[[str, float, float], ActivitySample]
AudioActivityBackend = Callable[[str, float, float], ActivitySample]


def _default_visual_activity(local_path: str, start_seconds: float, end_seconds: float) -> ActivitySample:
    """Backend de produção padrão -- nenhuma biblioteca de visão
    computacional real está disponível/instalada nesta etapa (mesma
    decisão de ``auto_reframe._default_detect``). Sempre devolve
    ``available=False`` -- nunca finge detectar atividade visual."""
    return ActivitySample(available=False)


def _default_audio_activity(local_path: str, start_seconds: float, end_seconds: float) -> ActivitySample:
    """Backend de produção padrão -- nenhuma análise real de áudio está
    disponível/integrada nesta etapa (mesma decisão de
    ``silence_removal._default_detect_silences``). Sempre devolve
    ``available=False`` -- nunca finge detectar atividade sonora."""
    return ActivitySample(available=False)


@dataclass(frozen=True)
class ClipCandidate:
    """Um candidato a clipe -- exatamente um por ``SemanticSegment`` de
    origem, sem expansão de bordas nem deduplicação (Prompt 47)."""

    index: int
    start: float
    end: float
    text: str
    role: str
    topic_change: bool
    hook_score: float
    hook_signals: "tuple[str, ...]"
    context_score: float
    speech_score: float
    completion_score: float
    visual_score: "float | None"
    audio_score: "float | None"
    overall_score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "role": self.role,
            "topic_change": self.topic_change,
            "hook_score": self.hook_score,
            "hook_signals": list(self.hook_signals),
            "context_score": self.context_score,
            "speech_score": self.speech_score,
            "completion_score": self.completion_score,
            "visual_score": self.visual_score,
            "audio_score": self.audio_score,
            "overall_score": self.overall_score,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------


def _canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_cache_key(
    content_segments_cache_key: str,
    *,
    weights: "Mapping[str, float]",
    speech_rate_min_wps: float,
    speech_rate_max_wps: float,
    question_markers: "frozenset[str] | Sequence[str]",
    answer_cue_lexicon: "frozenset[str] | Sequence[str]",
    hook_lexicon: "frozenset[str] | Sequence[str]",
    story_lexicon: "frozenset[str] | Sequence[str]",
    emotion_lexicon: "frozenset[str] | Sequence[str]",
    strong_phrase_lexicon: "frozenset[str] | Sequence[str]",
) -> str:
    """Ver seção 0.8 da docstring do módulo."""
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "content_segments_cache_key": str(content_segments_cache_key),
        "weights": {str(k): float(v) for k, v in sorted(weights.items())},
        "speech_rate_min_wps": float(speech_rate_min_wps),
        "speech_rate_max_wps": float(speech_rate_max_wps),
        "question_markers": sorted(str(m) for m in question_markers),
        "answer_cue_lexicon": sorted(str(m) for m in answer_cue_lexicon),
        "hook_lexicon": sorted(str(m) for m in hook_lexicon),
        "story_lexicon": sorted(str(m) for m in story_lexicon),
        "emotion_lexicon": sorted(str(m) for m in emotion_lexicon),
        "strong_phrase_lexicon": sorted(str(m) for m in strong_phrase_lexicon),
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Sub-scores -- funções puras, testáveis sem DB/FFmpeg
# ---------------------------------------------------------------------

_TERMINAL_PUNCTUATION = (".", "!", "?")

_WORD_PATTERN = re.compile(r"\S+")


def _word_count(text: str) -> int:
    return len(_WORD_PATTERN.findall(text))


def _normalize(text: str) -> str:
    return text.strip().lower()


def score_context(role: str, topic_change: bool) -> float:
    """Ver seção 0.2 da docstring do módulo."""
    role_component = 1.0 if role != "DEVELOPMENT" else 0.4
    topic_component = 1.0 if topic_change else 0.5
    return round((role_component + topic_component) / 2.0, 6)


def score_speech_rate(
    word_count: int,
    duration_seconds: float,
    *,
    min_wps: float = SPEECH_RATE_MIN_WPS_PADRAO,
    max_wps: float = SPEECH_RATE_MAX_WPS_PADRAO,
) -> float:
    """Ver seção 0.3 da docstring do módulo."""
    if duration_seconds <= 0:
        return 0.0
    if word_count <= 0:
        return 0.0
    rate = word_count / duration_seconds
    if min_wps <= rate <= max_wps:
        return 1.0
    if rate < min_wps:
        if min_wps <= 0:
            return 0.0
        return max(0.0, round(rate / min_wps, 6))
    # rate > max_wps -- decai linearmente até 0 quando rate dobra o teto.
    overflow = (rate - max_wps) / max_wps
    return max(0.0, round(1.0 - overflow, 6))


def score_hook(
    text: str,
    *,
    question_markers: "frozenset[str]" = DEFAULT_QUESTION_MARKERS,
    answer_cue_lexicon: "frozenset[str]" = DEFAULT_ANSWER_CUE_LEXICON,
    hook_lexicon: "frozenset[str]" = DEFAULT_HOOK_LEXICON,
    story_lexicon: "frozenset[str]" = DEFAULT_STORY_LEXICON,
    emotion_lexicon: "frozenset[str]" = DEFAULT_EMOTION_LEXICON,
    strong_phrase_lexicon: "frozenset[str]" = DEFAULT_STRONG_PHRASE_LEXICON,
) -> "tuple[float, tuple[str, ...]]":
    """Ver seção 0.4 da docstring do módulo. Devolve ``(hook_score,
    hook_signals)`` -- nunca só o número, sempre a lista auditável de
    categorias que dispararam."""
    normalized = _normalize(text)
    categories: "dict[str, frozenset[str] | None]" = {
        "question": question_markers,
        "answer_cue": answer_cue_lexicon,
        "hook": hook_lexicon,
        "story": story_lexicon,
        "emotion": emotion_lexicon,
        "strong_phrase": strong_phrase_lexicon,
    }
    fired: "list[str]" = []
    for name, lexicon in categories.items():
        if any(term in normalized for term in lexicon):
            fired.append(name)
    total = len(categories)
    score = round(len(fired) / total, 6) if total else 0.0
    return score, tuple(fired)


def score_completion(text: str, role: str) -> float:
    """Ver seção 0.5 da docstring do módulo."""
    normalized = text.strip()
    base = 0.7 if normalized.endswith(_TERMINAL_PUNCTUATION) else 0.3
    if role == SEGMENT_ROLE_CONCLUSION:
        base += 0.3
    return round(min(base, 1.0), 6)


def combine_overall(
    sub_scores: "Mapping[str, float | None]",
    *,
    weights: "Mapping[str, float]" = DEFAULT_SCORE_WEIGHTS,
) -> float:
    """Média ponderada dos sub-scores DISPONÍVEIS (não ``None``),
    renormalizada sobre os pesos restantes. Ver seção 0.7 da docstring do
    módulo. Nunca fabrica peso extra para um sinal ausente."""
    available = {k: v for k, v in sub_scores.items() if v is not None}
    if not available:
        return 0.0
    weight_sum = sum(weights.get(k, 0.0) for k in available)
    if weight_sum <= 0:
        # Nenhum peso configurado para os sinais disponíveis -- média
        # simples honesta, nunca zero por acidente de configuração.
        return round(sum(available.values()) / len(available), 6)
    weighted = sum(available[k] * weights.get(k, 0.0) for k in available)
    return round(weighted / weight_sum, 6)


def _build_reason(
    *,
    hook_signals: "tuple[str, ...]",
    context_score: float,
    speech_score: float,
    completion_score: float,
    visual_score: "float | None",
    audio_score: "float | None",
) -> str:
    parts: "list[str]" = []
    if hook_signals:
        parts.append(f"hook: {', '.join(hook_signals)}")
    else:
        parts.append("hook: nenhum sinal lexical detectado")
    parts.append(f"contexto: score {context_score:.2f}")
    parts.append(f"ritmo de fala: score {speech_score:.2f}")
    parts.append(f"completude do raciocínio: score {completion_score:.2f}")
    if visual_score is None:
        parts.append("visual: sem sinal (backend não integrado)")
    else:
        parts.append(f"visual: score {visual_score:.2f}")
    if audio_score is None:
        parts.append("áudio: sem sinal (backend não integrado)")
    else:
        parts.append(f"áudio: score {audio_score:.2f}")
    return "; ".join(parts)


# ---------------------------------------------------------------------
# SmartClipScoringEngine
# ---------------------------------------------------------------------


class SmartClipScoringEngine:
    """Job handler de scoring local de candidatos -- ver docstring do
    módulo para o contrato completo."""

    OPERATION: "ClassVar[str]" = OPERATION_SCORE_CANDIDATES

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        storage_manager: StorageManager,
        app_paths: AppPaths,
        audit_log: "OperationalAuditLog | None" = None,
        control_manager: "ControlManager | None" = None,
        smart_clip_engine: "SmartClipEngine | None" = None,
        visual_activity_backend: "VisualActivityBackend | None" = None,
        audio_activity_backend: "AudioActivityBackend | None" = None,
        weights: "Mapping[str, float]" = DEFAULT_SCORE_WEIGHTS,
        speech_rate_min_wps: float = SPEECH_RATE_MIN_WPS_PADRAO,
        speech_rate_max_wps: float = SPEECH_RATE_MAX_WPS_PADRAO,
        question_markers: "frozenset[str]" = DEFAULT_QUESTION_MARKERS,
        answer_cue_lexicon: "frozenset[str]" = DEFAULT_ANSWER_CUE_LEXICON,
        hook_lexicon: "frozenset[str]" = DEFAULT_HOOK_LEXICON,
        story_lexicon: "frozenset[str]" = DEFAULT_STORY_LEXICON,
        emotion_lexicon: "frozenset[str]" = DEFAULT_EMOTION_LEXICON,
        strong_phrase_lexicon: "frozenset[str]" = DEFAULT_STRONG_PHRASE_LEXICON,
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
        if smart_clip_engine is not None and not isinstance(smart_clip_engine, SmartClipEngine):
            raise TypeError(
                f"smart_clip_engine deve ser SmartClipEngine (recebido: {type(smart_clip_engine)!r})"
            )
        if visual_activity_backend is not None and not callable(visual_activity_backend):
            raise TypeError("visual_activity_backend deve ser chamável")
        if audio_activity_backend is not None and not callable(audio_activity_backend):
            raise TypeError("audio_activity_backend deve ser chamável")
        if speech_rate_min_wps <= 0 or speech_rate_max_wps <= 0:
            raise ValueError("speech_rate_min_wps/speech_rate_max_wps devem ser > 0")
        if speech_rate_min_wps >= speech_rate_max_wps:
            raise ValueError("speech_rate_min_wps deve ser < speech_rate_max_wps")
        if not weights:
            raise ValueError("weights não pode ser vazio")

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._control_manager = control_manager
        self._weights = dict(weights)
        self._speech_rate_min_wps = float(speech_rate_min_wps)
        self._speech_rate_max_wps = float(speech_rate_max_wps)
        self._question_markers = frozenset(str(m) for m in question_markers)
        self._answer_cue_lexicon = frozenset(str(m) for m in answer_cue_lexicon)
        self._hook_lexicon = frozenset(str(m) for m in hook_lexicon)
        self._story_lexicon = frozenset(str(m) for m in story_lexicon)
        self._emotion_lexicon = frozenset(str(m) for m in emotion_lexicon)
        self._strong_phrase_lexicon = frozenset(str(m) for m in strong_phrase_lexicon)

        self._visual_backend: VisualActivityBackend = visual_activity_backend or _default_visual_activity
        self._audio_backend: AudioActivityBackend = audio_activity_backend or _default_audio_activity

        self._smart_clip_engine = smart_clip_engine if smart_clip_engine is not None else SmartClipEngine(
            manager, database=database, storage_manager=storage_manager, app_paths=app_paths,
            audit_log=self._audit_log, control_manager=control_manager,
        )
        # JobEngine interno e privado -- só existe pra acionar o handler
        # real de segmentação semântica (Prompt 44) quando necessário
        # (seção 0.9 da docstring do módulo, mesmo padrão do Prompt 44).
        self._segmentation_job_engine = JobEngine(
            database, audit_log=self._audit_log, control_manager=control_manager,
        )
        self._segmentation_job_engine.register_handler(
            OPERATION_ANALYZE_CONTENT, self._smart_clip_engine.handle_analyze_job,
            claims_status=JOB_PROCESSING,
        )

    # -- cancelamento --------------------------------------------------

    def _cancel_requested(self, job_id: str) -> bool:
        if self._control_manager is None:
            return False
        return self._control_manager.is_job_cancel_requested(job_id)

    # -- content_segments_track: localizar ou acionar -------------------

    def _find_latest_segments_artifact(self, video_id: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (video_id, ARTIFACT_KIND_CONTENT_SEGMENTS),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _valid_segments_artifact(self, video_id: str) -> "Artifact | None":
        artifact = self._find_latest_segments_artifact(video_id)
        if artifact is None or not Path(artifact.path).is_file():
            return None
        return artifact

    def _trigger_segmentation(self, video_id: str) -> "tuple[Artifact | None, JobStepResult | None]":
        """Aciona o handler real de ``SmartClipEngine`` através do
        ``JobEngine`` interno (seção 0.9). Ver ``SmartClipEngine.
        _trigger_transcription`` para o precedente exato deste padrão."""
        segmentation_job = self._audit_log.create_job(
            Job(video_id=video_id, operation=OPERATION_ANALYZE_CONTENT)
        )
        try:
            result_job = self._segmentation_job_engine.advance(segmentation_job.id)
        except JobHandlerError as exc:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "segmentation_trigger_failed",
                    "segmentation_job_id": segmentation_job.id,
                    "detail": exc.__class__.__name__,
                },
            )
        if result_job.status != JOB_READY:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "segmentation_not_ready",
                    "segmentation_job_id": segmentation_job.id,
                    "segmentation_status": result_job.status,
                },
            )
        artifact = self._valid_segments_artifact(video_id)
        if artifact is None:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "segmentation_succeeded_but_artifact_missing",
                    "segmentation_job_id": segmentation_job.id,
                },
            )
        return artifact, None

    # -- leitura dos segmentos --------------------------------------------

    def _read_segments(self, artifact: Artifact) -> "list[dict[str, Any]]":
        raw = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        segments = raw.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError(f"artifact de segmentação {artifact.id!r} sem segmentos válidos")
        return segments

    # -- cache/Artifact desta análise -------------------------------------

    def _find_cached_artifact(self, video_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (video_id, ARTIFACT_KIND_CLIP_CANDIDATES, cache_key),
            ).fetchone()
        if row is None:
            return None
        artifact = self._database.get(Artifact, row[0])
        if artifact is None or not Path(artifact.path).is_file():
            return None
        return artifact

    def _final_path_for(self, video_id: str, cache_key: str) -> Path:
        return Path(self._app_paths.projects) / "clip_candidates" / video_id / f"{cache_key}.json"

    def _score_segment(self, local_path: "str | None", segment: "Mapping[str, Any]") -> ClipCandidate:
        start = float(segment["start"])
        end = float(segment["end"])
        text = str(segment["text"])
        role = str(segment["role"])
        topic_change = bool(segment["topic_change"])
        duration = max(0.0, end - start)

        context = score_context(role, topic_change)
        speech = score_speech_rate(
            _word_count(text), duration,
            min_wps=self._speech_rate_min_wps, max_wps=self._speech_rate_max_wps,
        )
        hook, hook_signals = score_hook(
            text,
            question_markers=self._question_markers,
            answer_cue_lexicon=self._answer_cue_lexicon,
            hook_lexicon=self._hook_lexicon,
            story_lexicon=self._story_lexicon,
            emotion_lexicon=self._emotion_lexicon,
            strong_phrase_lexicon=self._strong_phrase_lexicon,
        )
        completion = score_completion(text, role)

        visual_sample = self._visual_backend(local_path or "", start, end) if local_path else ActivitySample(False)
        audio_sample = self._audio_backend(local_path or "", start, end) if local_path else ActivitySample(False)
        visual_score = visual_sample.activity if visual_sample.available else None
        audio_score = audio_sample.activity if audio_sample.available else None

        overall = combine_overall(
            {
                "hook_score": hook,
                "context_score": context,
                "speech_score": speech,
                "completion_score": completion,
                "visual_score": visual_score,
                "audio_score": audio_score,
            },
            weights=self._weights,
        )
        reason = _build_reason(
            hook_signals=hook_signals, context_score=context, speech_score=speech,
            completion_score=completion, visual_score=visual_score, audio_score=audio_score,
        )
        return ClipCandidate(
            index=int(segment["index"]), start=start, end=end, text=text, role=role,
            topic_change=topic_change, hook_score=hook, hook_signals=hook_signals,
            context_score=context, speech_score=speech, completion_score=completion,
            visual_score=visual_score, audio_score=audio_score, overall_score=overall, reason=reason,
        )

    def _write_and_register_artifact(
        self, job: Job, cache_key: str, segments_artifact: Artifact, candidates: "tuple[ClipCandidate, ...]",
    ) -> Artifact:
        payload = {
            "version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "content_segments_artifact_id": segments_artifact.id,
            "content_segments_cache_key": segments_artifact.fingerprint,
            "weights": self._weights,
            "speech_rate_min_wps": self._speech_rate_min_wps,
            "speech_rate_max_wps": self._speech_rate_max_wps,
            "candidates": [c.to_dict() for c in candidates],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        temp_path = self._storage.allocate_temp(suffix="_clip_candidates.json", create=True)
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
            kind=ARTIFACT_KIND_CLIP_CANDIDATES, path=str(promoted), fingerprint=cache_key,
            size_bytes=len(content.encode("utf-8")),
        )
        self._database.insert(artifact)
        return artifact

    # ===================================================================
    # Job handler
    # ===================================================================

    def handle_score_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})

        segments_artifact = self._valid_segments_artifact(job.video_id)
        if segments_artifact is None:
            segments_artifact, failure = self._trigger_segmentation(job.video_id)
            if failure is not None:
                return failure

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_before_scoring"})

        cache_key = compute_cache_key(
            segments_artifact.fingerprint,
            weights=self._weights,
            speech_rate_min_wps=self._speech_rate_min_wps,
            speech_rate_max_wps=self._speech_rate_max_wps,
            question_markers=self._question_markers,
            answer_cue_lexicon=self._answer_cue_lexicon,
            hook_lexicon=self._hook_lexicon,
            story_lexicon=self._story_lexicon,
            emotion_lexicon=self._emotion_lexicon,
            strong_phrase_lexicon=self._strong_phrase_lexicon,
        )

        cached = self._find_cached_artifact(job.video_id, cache_key)
        if cached is not None:
            return JobStepResult(
                target_status=JOB_READY,
                semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
                data={"cache_hit": True, "cache_key": cache_key, "artifact_id": cached.id},
            )

        try:
            raw_segments = self._read_segments(segments_artifact)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "invalid_segments_artifact", "detail": exc.__class__.__name__},
            )

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_after_read"})

        local_path = job.extra.get("local_path") if isinstance(job.extra, dict) else None
        candidates = tuple(self._score_segment(local_path, seg) for seg in raw_segments)

        artifact = self._write_and_register_artifact(job, cache_key, segments_artifact, candidates)

        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={
                "cache_hit": False,
                "cache_key": cache_key,
                "artifact_id": artifact.id,
                "candidate_count": len(candidates),
            },
        )
