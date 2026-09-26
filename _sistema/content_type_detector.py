# -*- coding: utf-8 -*-
"""ContentCategoryDetector -- Prompt 46 (Fase 7: Smart Clip -- terceiro prompt).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Detectar automaticamente o tipo provável de conteúdo:
    - podcast/interview
    - tutorial
    - vlog
    - gameplay
    - talking-head
    - general

    A estratégia do Smart Clip pode mudar internamente.
    Usuário não precisa escolher obrigatoriamente.
    Permitir override avançado caso queira.

===========================================================================
0. VISÃO GERAL
===========================================================================

Este módulo classifica o VÍDEO INTEIRO (não segmento a segmento) numa
categoria dentro de um vocabulário FECHADO de seis valores (seção 0.1),
usando só sinais locais já disponíveis nos ``SemanticSegment`` que
``SmartClipEngine`` (Prompt 44) já produziu -- nunca relê o transcript
bruto, nunca duplica segmentação. Sempre que o Artifact de segmentação
ainda não existe, este módulo aciona a cadeia real (Segmentação ->
Transcrição) pelo MESMO padrão de "JobEngine interno e privado" já
estabelecido pelo Prompt 45 (``SmartClipScoringEngine``) -- nunca um
atalho.

A classificação em si não decide NADA sobre a estratégia futura do Smart
Clip (isso é possibilidade descrita no roadmap, não obrigação deste
Prompt -- explicitamente fora de escopo: qualquer ajuste de peso em
``smart_clip_scoring.py`` por tipo de conteúdo). Este módulo só produz e
persiste o rótulo, auditável, com um override manual opcional que nunca
apaga a detecção automática original.

===========================================================================
0.1 -- NOME ESCOLHIDO -- POR QUE NÃO COLIDE COM ``template_engine.py``
===========================================================================

Achado documentado na evidência deste Prompt: ``_sistema/template_engine.py``
já usa o identificador ``content_type`` (e a constante ``CONTENT_TYPES``,
``_validate_content_type``) para um conceito COMPLETAMENTE diferente -- o
tipo de ZONA de conteúdo de um template visual (``FIXED_TEXT``,
``AI_HOOK``, ``AI_TITLE``, ``AI_SUMMARY``, ``AI_QUESTION``, ``AI_CTA``,
``CUSTOM_AI``). Confundir os dois seria um erro de autoridade sério (GATE
item 7 -- duas fontes de verdade para "o que significa content_type").

Decisão: este módulo NUNCA usa o identificador ``content_type`` em nada
que ele expõe -- nem nome de módulo público, nem classe, nem campo, nem
constante de vocabulário. Em vez disso:

- O CONCEITO se chama **categoria de conteúdo do vídeo**
  (``content category`` em inglês solto na prosa, nunca como
  identificador Python).
- A classe se chama ``ContentCategoryDetector`` (não
  ``ContentTypeDetector`` -- "Detector" no nome do arquivo é só um rótulo
  de arquivo, o identificador Python real que importa é o da classe/
  constantes, e esse usa "Category").
- O vocabulário fechado é ``SMART_CLIP_CONTENT_CATEGORIES`` (não
  ``CONTENT_TYPES``).
- O campo persistido é ``category`` dentro do JSON do Artifact (nunca
  ``content_type``), e o Artifact tem ``kind="content_category_classification"``
  (nunca ``"content_type"`` em nenhuma forma).
- O arquivo se chama ``content_type_detector.py`` só porque é o nome
  sugerido no Prompt e mantém a convenção de nomenclatura de arquivo em
  inglês do restante do projeto -- nomes de ARQUIVO nunca colidem em
  Python (não são identificadores importáveis), só nomes de símbolo
  colidiriam, e nenhum símbolo aqui se chama ``content_type``/
  ``CONTENT_TYPES``. Testado estruturalmente (seção 0.7): os dois
  vocabulários (``SMART_CLIP_CONTENT_CATEGORIES`` vs
  ``template_engine.CONTENT_TYPES``) são conjuntos disjuntos, e este
  módulo nunca importa ``template_engine`` em absoluto.

===========================================================================
0.2 -- VOCABULÁRIO FECHADO -> CONSTANTES PYTHON
===========================================================================

Mesma filosofia de ``domain/checkpoints.py`` (vocabulário fechado,
estender exige um Prompt novo, não decisão unilateral do código):

    "podcast/interview" -> CATEGORY_PODCAST_INTERVIEW = "PODCAST_INTERVIEW"
    "tutorial"          -> CATEGORY_TUTORIAL           = "TUTORIAL"
    "vlog"               -> CATEGORY_VLOG               = "VLOG"
    "gameplay"           -> CATEGORY_GAMEPLAY           = "GAMEPLAY"
    "talking-head"       -> CATEGORY_TALKING_HEAD       = "TALKING_HEAD"
    "general"            -> CATEGORY_GENERAL            = "GENERAL"

===========================================================================
0.3 -- SEM DIARIZAÇÃO -- A CLASSIFICAÇÃO NUNCA ASSUME SABER QUEM FALA
===========================================================================

Confirmado por grep (evidência do Prompt): nenhuma diarização de locutor
existe em lugar nenhum do projeto. Toda heurística abaixo trabalha só com
o TEXTO de cada ``SemanticSegment`` (nunca "quando o locutor troca") --
"densidade de pergunta/resposta alternada" (seção 0.4) é medida pela
ALTERNÂNCIA DE PADRÃO TEXTUAL entre segmentos consecutivos (um segmento
com "?" seguido de um sem "?", ou vice-versa), nunca por saber se são
duas pessoas diferentes falando.

===========================================================================
0.4 -- CINCO HEURÍSTICAS + ``GENERAL`` COMO ESPELHO HONESTO
===========================================================================

Cada categoria (exceto ``GENERAL``) tem sua própria função de score
isolada e nomeada, cada sinal auditável reportado em ``signals``:

- **PODCAST_INTERVIEW** (``score_podcast_interview``): densidade de
  segmentos com pergunta (reaproveita
  ``smart_clip_scoring.DEFAULT_QUESTION_MARKERS`` -- import direto, nunca
  duplicado) + densidade de segmentos com marcador de resposta
  (reaproveita ``smart_clip_scoring.DEFAULT_ANSWER_CUE_LEXICON``, mesmo
  motivo) + bônus de ALTERNÂNCIA real entre pergunta/não-pergunta entre
  segmentos consecutivos (ver seção 0.3).
- **TUTORIAL** (``score_tutorial``): densidade de segmentos com
  marcador de instrução sequencial/imperativo (léxico próprio,
  ``DEFAULT_TUTORIAL_LEXICON`` -- "primeiro", "depois", "em seguida",
  "passo a passo", "clique em", "abra o", "digite", "configure",
  "selecione", "vá até").
- **VLOG** (``score_vlog``): densidade de segmentos com narrativa em
  primeira pessoa/vocabulário de rotina pessoal (léxico próprio,
  ``DEFAULT_VLOG_LEXICON`` -- "hoje eu", "meu dia", "minha rotina",
  "eu decidi", "cheguei em casa", "no meu dia a dia", "vou te mostrar
  como é").
- **GAMEPLAY** (``score_gameplay``): léxico de jogos (``DEFAULT_
  GAMEPLAY_LEXICON`` -- "gameplay", "esse jogo", "fase", "boss",
  "combo", "respawn", "loot", "partida") como SINAL FRACO, deliberadamente
  limitado por ``GAMEPLAY_WEAK_SIGNAL_CAP`` (padrão ``0.5``) quando
  nenhum backend visual/sonoro real está disponível -- texto sobre jogos
  não é prova de que o vídeo É gameplay (alguém pode estar CONVERSANDO
  sobre um jogo em um talking-head). Quando um backend real É injetado e
  reporta ``available=True`` (mesmo padrão de stub honesto -- seção 0.5),
  o sinal léxico se combina com o sinal real SEM o teto, porque agora há
  evidência de fato observada, não só vocabulário.
- **TALKING_HEAD** (``score_talking_head``): densidade de segmentos com
  endereçamento direto de opinião (léxico próprio, ``DEFAULT_
  TALKING_HEAD_LEXICON`` -- "eu acho que", "na minha opinião", "eu
  acredito", "deixa eu te falar", "olha", "cara").
- **GENERAL**: nunca tem léxico próprio -- seu score é
  ``1.0 - max(scores das outras cinco)``. Isso é deliberado: ``GENERAL``
  só vence quando NENHUMA outra categoria tem sinal real, nunca por um
  threshold arbitrário separado que poderia ser ajustado pra "forçar"
  uma categoria. Se todas as cinco heurísticas derem ``0.0`` (nenhum
  sinal), ``GENERAL`` fica em ``1.0`` e vence honestamente. Isso satisfaz
  diretamente a exigência do Prompt: "general é o destino honesto quando
  nenhuma categoria tem sinal suficiente -- nunca forçar uma categoria só
  pra evitar general".

Desempate determinístico quando dois scores empatam exatamente: ordem de
prioridade fixa ``(PODCAST_INTERVIEW, TUTORIAL, VLOG, GAMEPLAY,
TALKING_HEAD, GENERAL)`` -- a primeira da lista com o score máximo vence.
``GENERAL`` por último nessa ordem significa que só vence empate se as
outras cinco também estiverem zeradas (caso raro, mas coberto).

===========================================================================
0.5 -- SINAIS VISUAIS/SONOROS -- MESMO PADRÃO DE STUB HONESTO
===========================================================================

Nenhum backend real de visão computacional/análise de áudio está
integrado hoje (confirmado nos três módulos irmãos). Este módulo herda
exatamente o mesmo padrão: backends injetáveis próprios
(``GameplayVisualCueBackend``/``GameplayAudioCueBackend``, mesma forma de
``Callable[[str, float, float], ActivitySample]``), com defaults que
sempre devolvem ``ActivitySample(available=False)`` -- nunca fabricam um
valor. ``ActivitySample`` é reaproveitado DIRETAMENTE de
``smart_clip_scoring.py`` (import direto -- é só um tipo de dado genérico
"disponível ou não", sem lógica/autoridade própria, reaproveitá-lo não
viola autoridade única).

===========================================================================
0.6 -- ARTIFACT / CACHE / OVERRIDE -- SEM CHECKPOINT NOVO
===========================================================================

Novo Artifact ``kind=ARTIFACT_KIND_CONTENT_CATEGORY`` (
``"content_category_classification"``), um JSON por classificação (auto
OU override), nunca reaproveitando/sobrescrevendo o arquivo anterior.

**Detecção automática**: ``fingerprint`` do Artifact = ``cache_key``
determinístico (segmentação de origem + todos os parâmetros/léxicos +
``ALGORITHM_VERSION``, mesmo padrão exato dos Prompts 44/45). Uma
detecção automática já cacheada é reaproveitada via ``(video_id, kind,
fingerprint)``, nunca duplicada.

**Override manual**: ``register_override`` grava um NOVO Artifact
(mesmo ``kind``, ``fingerprint=None`` -- overrides não participam do
cache por ``cache_key``, cada chamada é uma decisão explícita nova) com
``source="manual_override"`` no JSON. A detecção automática original
NUNCA é apagada nem sobrescrita -- ela continua sendo uma linha própria
no banco, sempre recuperável via ``get_latest_auto_classification``
(varre as linhas mais recentes primeiro e devolve a primeira cujo JSON
tem ``source="auto"``). O valor EFETIVO atual (o que qualquer consumidor
futuro like Prompt 48 leria) é sempre "o Artifact mais recente desse
``kind``, seja ele auto ou override" -- mesma convenção "mais recente
vence" já usada em todo o projeto (Prompts 42-45).

``domain/checkpoints.py`` continua vocabulário fechado -- nenhum
checkpoint novo é gravado. Idempotência (da detecção automática) resolvida
inteiramente pelo par ``(video_id, kind, fingerprint)``. Testado
estruturalmente (nenhuma chamada a ``record_checkpoint``, nenhuma
constante ``CHECKPOINT_*`` nova).

===========================================================================
0.7 -- FORA DE ESCOPO (declarado explicitamente, nunca implementado aqui)
===========================================================================

Qualquer ajuste de peso em ``smart_clip_scoring.py`` por tipo de
conteúdo; a tela de seleção/override do usuário (Prompt 48); qualquer
chamada de rede/API externa/modelo de linguagem; qualquer mudança em
``smart_clip_engine.py``, ``smart_clip_scoring.py``,
``domain/checkpoints.py`` ou ``template_engine.py`` (todos confirmados
byte-idênticos antes/depois desta entrega).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Callable, ClassVar, Mapping, Sequence

from .app_paths import AppPaths
from .control_manager import ControlManager
from .domain import Artifact, Job, JOB_CANCELLED, JOB_FAILED, JOB_PROCESSING, JOB_READY
from .edit_project import EditProjectManager
from .job_engine import JobEngine, JobHandlerError, JobStepResult
from .smart_clip_engine import ARTIFACT_KIND_CONTENT_SEGMENTS, OPERATION_ANALYZE_CONTENT, SmartClipEngine
from .smart_clip_scoring import (
    ActivitySample,
    DEFAULT_ANSWER_CUE_LEXICON,
    DEFAULT_QUESTION_MARKERS,
)
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager

__all__ = [
    "OPERATION_DETECT_CONTENT_CATEGORY",
    "ARTIFACT_KIND_CONTENT_CATEGORY",
    "ALGORITHM_VERSION",
    "CATEGORY_PODCAST_INTERVIEW",
    "CATEGORY_TUTORIAL",
    "CATEGORY_VLOG",
    "CATEGORY_GAMEPLAY",
    "CATEGORY_TALKING_HEAD",
    "CATEGORY_GENERAL",
    "SMART_CLIP_CONTENT_CATEGORIES",
    "CATEGORY_PRIORITY_ORDER",
    "SOURCE_AUTO",
    "SOURCE_MANUAL_OVERRIDE",
    "GAMEPLAY_WEAK_SIGNAL_CAP_PADRAO",
    "DEFAULT_TUTORIAL_LEXICON",
    "DEFAULT_VLOG_LEXICON",
    "DEFAULT_GAMEPLAY_LEXICON",
    "DEFAULT_TALKING_HEAD_LEXICON",
    "InvalidContentCategoryError",
    "validate_content_category",
    "ClassificationResult",
    "ContentCategoryDetector",
    "compute_cache_key",
    "score_podcast_interview",
    "score_tutorial",
    "score_vlog",
    "score_gameplay",
    "score_talking_head",
    "classify_segments",
]

OPERATION_DETECT_CONTENT_CATEGORY = "DETECT_CONTENT_CATEGORY"

ARTIFACT_KIND_CONTENT_CATEGORY = "content_category_classification"

# Muda quando a LÓGICA de classificação muda (não os parâmetros -- esses
# já entram no cache_key por si só). Ver seção 0.6 da docstring do módulo.
ALGORITHM_VERSION = "1"

CATEGORY_PODCAST_INTERVIEW = "PODCAST_INTERVIEW"
CATEGORY_TUTORIAL = "TUTORIAL"
CATEGORY_VLOG = "VLOG"
CATEGORY_GAMEPLAY = "GAMEPLAY"
CATEGORY_TALKING_HEAD = "TALKING_HEAD"
CATEGORY_GENERAL = "GENERAL"

SMART_CLIP_CONTENT_CATEGORIES: "frozenset[str]" = frozenset({
    CATEGORY_PODCAST_INTERVIEW, CATEGORY_TUTORIAL, CATEGORY_VLOG,
    CATEGORY_GAMEPLAY, CATEGORY_TALKING_HEAD, CATEGORY_GENERAL,
})

# Ordem de desempate determinístico -- ver seção 0.4. GENERAL por último
# de propósito (só vence empate se as outras cinco também zerarem).
CATEGORY_PRIORITY_ORDER: "tuple[str, ...]" = (
    CATEGORY_PODCAST_INTERVIEW, CATEGORY_TUTORIAL, CATEGORY_VLOG,
    CATEGORY_GAMEPLAY, CATEGORY_TALKING_HEAD, CATEGORY_GENERAL,
)

SOURCE_AUTO = "auto"
SOURCE_MANUAL_OVERRIDE = "manual_override"

GAMEPLAY_WEAK_SIGNAL_CAP_PADRAO = 0.5

DEFAULT_TUTORIAL_LEXICON: "frozenset[str]" = frozenset({
    "primeiro,", "depois,", "em seguida", "passo a passo", "o próximo passo",
    "o proximo passo", "clique em", "abra o", "abra a", "digite", "configure",
    "selecione", "vá até", "va até", "va ate", "agora vá em", "agora va em",
})

DEFAULT_VLOG_LEXICON: "frozenset[str]" = frozenset({
    "hoje eu", "meu dia", "minha rotina", "eu decidi", "cheguei em casa",
    "no meu dia a dia", "vou te mostrar como é", "vou te mostrar como e",
    "esse foi o meu dia", "bora comigo",
})

DEFAULT_GAMEPLAY_LEXICON: "frozenset[str]" = frozenset({
    "gameplay", "esse jogo", "nesse jogo", "fase", "boss", "combo",
    "respawn", "loot", "partida", "meu personagem", "level up",
})

DEFAULT_TALKING_HEAD_LEXICON: "frozenset[str]" = frozenset({
    "eu acho que", "na minha opinião", "na minha opiniao", "eu acredito",
    "deixa eu te falar", "olha", "cara,", "o que eu penso",
})


class InvalidContentCategoryError(ValueError):
    """Categoria fora do vocabulário fechado (seção 0.2 da docstring)."""


def validate_content_category(category: str) -> str:
    if category not in SMART_CLIP_CONTENT_CATEGORIES:
        raise InvalidContentCategoryError(f"categoria de conteúdo inválida: {category!r}")
    return category


# Backends injetáveis -- própria seção 0.5: reaproveita ActivitySample
# (tipo de dado genérico) mas define seus próprios backends, escopados à
# pergunta específica "há sinal visual/sonoro de jogo neste trecho?".
GameplayVisualCueBackend = Callable[[str, float, float], ActivitySample]
GameplayAudioCueBackend = Callable[[str, float, float], ActivitySample]


def _default_gameplay_visual_cue(local_path: str, start_seconds: float, end_seconds: float) -> ActivitySample:
    """Backend de produção padrão -- nenhuma biblioteca de visão
    computacional real está disponível/instalada nesta etapa (mesma
    decisão de ``auto_reframe._default_detect``/
    ``smart_clip_scoring._default_visual_activity``). Sempre devolve
    ``available=False`` -- nunca finge detectar um sinal de jogo."""
    return ActivitySample(available=False)


def _default_gameplay_audio_cue(local_path: str, start_seconds: float, end_seconds: float) -> ActivitySample:
    """Backend de produção padrão -- nenhuma análise real de áudio está
    disponível/integrada nesta etapa (mesma decisão de
    ``silence_removal._default_detect_silences``/
    ``smart_clip_scoring._default_audio_activity``). Sempre devolve
    ``available=False`` -- nunca finge detectar um sinal de jogo."""
    return ActivitySample(available=False)


@dataclass(frozen=True)
class ClassificationResult:
    """Resultado de UMA classificação de vídeo inteiro. Ver seção 0.4 da
    docstring do módulo."""

    category: str
    confidence: float
    category_scores: "Mapping[str, float]"
    signals: "Mapping[str, tuple[str, ...]]"
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "confidence": self.confidence,
            "category_scores": dict(self.category_scores),
            "signals": {k: list(v) for k, v in self.signals.items()},
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
    gameplay_weak_signal_cap: float,
    question_markers: "frozenset[str] | Sequence[str]",
    answer_cue_lexicon: "frozenset[str] | Sequence[str]",
    tutorial_lexicon: "frozenset[str] | Sequence[str]",
    vlog_lexicon: "frozenset[str] | Sequence[str]",
    gameplay_lexicon: "frozenset[str] | Sequence[str]",
    talking_head_lexicon: "frozenset[str] | Sequence[str]",
) -> str:
    """Ver seção 0.6 da docstring do módulo."""
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "content_segments_cache_key": str(content_segments_cache_key),
        "gameplay_weak_signal_cap": float(gameplay_weak_signal_cap),
        "question_markers": sorted(str(m) for m in question_markers),
        "answer_cue_lexicon": sorted(str(m) for m in answer_cue_lexicon),
        "tutorial_lexicon": sorted(str(m) for m in tutorial_lexicon),
        "vlog_lexicon": sorted(str(m) for m in vlog_lexicon),
        "gameplay_lexicon": sorted(str(m) for m in gameplay_lexicon),
        "talking_head_lexicon": sorted(str(m) for m in talking_head_lexicon),
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Sub-scores por categoria -- funções puras, testáveis sem DB
# ---------------------------------------------------------------------


def _normalize(text: str) -> str:
    return text.strip().lower()


def _lexicon_density(texts: "Sequence[str]", lexicon: "frozenset[str]") -> "tuple[float, tuple[bool, ...]]":
    flags = tuple(any(term in _normalize(t) for term in lexicon) for t in texts)
    density = sum(1 for f in flags if f) / len(flags) if flags else 0.0
    return density, flags


def score_podcast_interview(
    texts: "Sequence[str]",
    *,
    question_markers: "frozenset[str]" = DEFAULT_QUESTION_MARKERS,
    answer_cue_lexicon: "frozenset[str]" = DEFAULT_ANSWER_CUE_LEXICON,
) -> "tuple[float, tuple[str, ...]]":
    """Ver seção 0.4 da docstring do módulo. Reaproveita os léxicos de
    ``smart_clip_scoring.py`` via import direto -- nunca duplicados."""
    if not texts:
        return 0.0, ("sem segmentos",)
    question_density, question_flags = _lexicon_density(texts, question_markers)
    answer_density, _ = _lexicon_density(texts, answer_cue_lexicon)
    alternation = 0.0
    if len(question_flags) > 1:
        changes = sum(1 for a, b in zip(question_flags, question_flags[1:]) if a != b)
        alternation = changes / (len(question_flags) - 1)
    score = min(1.0, (question_density + answer_density) / 2.0 + 0.2 * alternation)
    signals = (
        f"question_density={question_density:.2f}",
        f"answer_density={answer_density:.2f}",
        f"alternation={alternation:.2f}",
    )
    return round(score, 6), signals


def score_tutorial(
    texts: "Sequence[str]", *, tutorial_lexicon: "frozenset[str]" = DEFAULT_TUTORIAL_LEXICON,
) -> "tuple[float, tuple[str, ...]]":
    """Ver seção 0.4 da docstring do módulo."""
    if not texts:
        return 0.0, ("sem segmentos",)
    density, _ = _lexicon_density(texts, tutorial_lexicon)
    return round(density, 6), (f"tutorial_lexicon_density={density:.2f}",)


def score_vlog(
    texts: "Sequence[str]", *, vlog_lexicon: "frozenset[str]" = DEFAULT_VLOG_LEXICON,
) -> "tuple[float, tuple[str, ...]]":
    """Ver seção 0.4 da docstring do módulo."""
    if not texts:
        return 0.0, ("sem segmentos",)
    density, _ = _lexicon_density(texts, vlog_lexicon)
    return round(density, 6), (f"vlog_lexicon_density={density:.2f}",)


def score_gameplay(
    texts: "Sequence[str]",
    *,
    gameplay_lexicon: "frozenset[str]" = DEFAULT_GAMEPLAY_LEXICON,
    weak_signal_cap: float = GAMEPLAY_WEAK_SIGNAL_CAP_PADRAO,
    visual_sample: "ActivitySample | None" = None,
    audio_sample: "ActivitySample | None" = None,
) -> "tuple[float, tuple[str, ...]]":
    """Ver seções 0.4/0.5 da docstring do módulo -- léxico é SEMPRE um
    sinal fraco (nunca prova sozinho que é gameplay); só perde o teto
    quando um backend real reporta ``available=True``."""
    if not texts:
        return 0.0, ("sem segmentos",)
    lexicon_density, _ = _lexicon_density(texts, gameplay_lexicon)

    real_values = [
        s.activity for s in (visual_sample, audio_sample)
        if s is not None and s.available and s.activity is not None
    ]
    if real_values:
        real_component = mean(real_values)
        score = min(1.0, (lexicon_density + real_component) / 2.0)
        signals = (
            f"gameplay_lexicon_density={lexicon_density:.2f}",
            f"real_signal_component={real_component:.2f}",
        )
    else:
        score = min(1.0, lexicon_density) * weak_signal_cap
        signals = (
            f"gameplay_lexicon_density={lexicon_density:.2f}",
            "visual_signal=unavailable", "audio_signal=unavailable",
            f"weak_signal_cap={weak_signal_cap:.2f}",
        )
    return round(score, 6), signals


def score_talking_head(
    texts: "Sequence[str]", *, talking_head_lexicon: "frozenset[str]" = DEFAULT_TALKING_HEAD_LEXICON,
) -> "tuple[float, tuple[str, ...]]":
    """Ver seção 0.4 da docstring do módulo."""
    if not texts:
        return 0.0, ("sem segmentos",)
    density, _ = _lexicon_density(texts, talking_head_lexicon)
    return round(density, 6), (f"talking_head_lexicon_density={density:.2f}",)


def classify_segments(
    texts: "Sequence[str]",
    *,
    question_markers: "frozenset[str]" = DEFAULT_QUESTION_MARKERS,
    answer_cue_lexicon: "frozenset[str]" = DEFAULT_ANSWER_CUE_LEXICON,
    tutorial_lexicon: "frozenset[str]" = DEFAULT_TUTORIAL_LEXICON,
    vlog_lexicon: "frozenset[str]" = DEFAULT_VLOG_LEXICON,
    gameplay_lexicon: "frozenset[str]" = DEFAULT_GAMEPLAY_LEXICON,
    talking_head_lexicon: "frozenset[str]" = DEFAULT_TALKING_HEAD_LEXICON,
    gameplay_weak_signal_cap: float = GAMEPLAY_WEAK_SIGNAL_CAP_PADRAO,
    gameplay_visual_sample: "ActivitySample | None" = None,
    gameplay_audio_sample: "ActivitySample | None" = None,
) -> ClassificationResult:
    """Função pura de classificação -- testável sem DB/FFmpeg. Ver seção
    0.4 da docstring do módulo para o algoritmo completo."""
    podcast_score, podcast_signals = score_podcast_interview(
        texts, question_markers=question_markers, answer_cue_lexicon=answer_cue_lexicon,
    )
    tutorial_score, tutorial_signals = score_tutorial(texts, tutorial_lexicon=tutorial_lexicon)
    vlog_score, vlog_signals = score_vlog(texts, vlog_lexicon=vlog_lexicon)
    gameplay_score, gameplay_signals = score_gameplay(
        texts, gameplay_lexicon=gameplay_lexicon, weak_signal_cap=gameplay_weak_signal_cap,
        visual_sample=gameplay_visual_sample, audio_sample=gameplay_audio_sample,
    )
    talking_head_score, talking_head_signals = score_talking_head(
        texts, talking_head_lexicon=talking_head_lexicon,
    )

    non_general_scores = {
        CATEGORY_PODCAST_INTERVIEW: podcast_score,
        CATEGORY_TUTORIAL: tutorial_score,
        CATEGORY_VLOG: vlog_score,
        CATEGORY_GAMEPLAY: gameplay_score,
        CATEGORY_TALKING_HEAD: talking_head_score,
    }
    general_score = round(max(0.0, 1.0 - max(non_general_scores.values(), default=0.0)), 6)

    category_scores = dict(non_general_scores)
    category_scores[CATEGORY_GENERAL] = general_score

    signals = {
        CATEGORY_PODCAST_INTERVIEW: podcast_signals,
        CATEGORY_TUTORIAL: tutorial_signals,
        CATEGORY_VLOG: vlog_signals,
        CATEGORY_GAMEPLAY: gameplay_signals,
        CATEGORY_TALKING_HEAD: talking_head_signals,
        CATEGORY_GENERAL: (f"general = 1.0 - max(outras) = {general_score:.2f}",),
    }

    winner = CATEGORY_GENERAL
    best = -1.0
    for candidate in CATEGORY_PRIORITY_ORDER:
        if category_scores[candidate] > best:
            best = category_scores[candidate]
            winner = candidate

    reason_parts = [f"{cat}={category_scores[cat]:.2f}" for cat in CATEGORY_PRIORITY_ORDER]
    reason = f"categoria escolhida: {winner} (scores: {', '.join(reason_parts)}; sinais: {'; '.join(signals[winner])})"

    return ClassificationResult(
        category=winner, confidence=category_scores[winner],
        category_scores=category_scores, signals=signals, reason=reason,
    )


# ---------------------------------------------------------------------
# ContentCategoryDetector
# ---------------------------------------------------------------------


class ContentCategoryDetector:
    """Job handler de classificação de categoria de conteúdo -- ver
    docstring do módulo para o contrato completo."""

    OPERATION: "ClassVar[str]" = OPERATION_DETECT_CONTENT_CATEGORY

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
        gameplay_visual_cue_backend: "GameplayVisualCueBackend | None" = None,
        gameplay_audio_cue_backend: "GameplayAudioCueBackend | None" = None,
        gameplay_weak_signal_cap: float = GAMEPLAY_WEAK_SIGNAL_CAP_PADRAO,
        question_markers: "frozenset[str]" = DEFAULT_QUESTION_MARKERS,
        answer_cue_lexicon: "frozenset[str]" = DEFAULT_ANSWER_CUE_LEXICON,
        tutorial_lexicon: "frozenset[str]" = DEFAULT_TUTORIAL_LEXICON,
        vlog_lexicon: "frozenset[str]" = DEFAULT_VLOG_LEXICON,
        gameplay_lexicon: "frozenset[str]" = DEFAULT_GAMEPLAY_LEXICON,
        talking_head_lexicon: "frozenset[str]" = DEFAULT_TALKING_HEAD_LEXICON,
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
        if gameplay_visual_cue_backend is not None and not callable(gameplay_visual_cue_backend):
            raise TypeError("gameplay_visual_cue_backend deve ser chamável")
        if gameplay_audio_cue_backend is not None and not callable(gameplay_audio_cue_backend):
            raise TypeError("gameplay_audio_cue_backend deve ser chamável")
        if not (0.0 <= gameplay_weak_signal_cap <= 1.0):
            raise ValueError("gameplay_weak_signal_cap deve estar entre 0.0 e 1.0")

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._control_manager = control_manager
        self._gameplay_weak_signal_cap = float(gameplay_weak_signal_cap)
        self._question_markers = frozenset(str(m) for m in question_markers)
        self._answer_cue_lexicon = frozenset(str(m) for m in answer_cue_lexicon)
        self._tutorial_lexicon = frozenset(str(m) for m in tutorial_lexicon)
        self._vlog_lexicon = frozenset(str(m) for m in vlog_lexicon)
        self._gameplay_lexicon = frozenset(str(m) for m in gameplay_lexicon)
        self._talking_head_lexicon = frozenset(str(m) for m in talking_head_lexicon)

        self._gameplay_visual_backend = gameplay_visual_cue_backend or _default_gameplay_visual_cue
        self._gameplay_audio_backend = gameplay_audio_cue_backend or _default_gameplay_audio_cue

        self._smart_clip_engine = smart_clip_engine if smart_clip_engine is not None else SmartClipEngine(
            manager, database=database, storage_manager=storage_manager, app_paths=app_paths,
            audit_log=self._audit_log, control_manager=control_manager,
        )
        # JobEngine interno e privado -- mesmo padrão do Prompt 45, só pra
        # acionar a segmentação real quando ausente (seção 0 da docstring).
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
        """Mesmo padrão exato de ``SmartClipScoringEngine._trigger_segmentation``
        (Prompt 45)."""
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

    def _read_segment_texts(self, artifact: Artifact) -> "list[str]":
        raw = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        segments = raw.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError(f"artifact de segmentação {artifact.id!r} sem segmentos válidos")
        return [str(seg["text"]) for seg in segments]

    # -- cache/Artifact desta classificação -------------------------------

    def _find_cached_auto_artifact(self, video_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (video_id, ARTIFACT_KIND_CONTENT_CATEGORY, cache_key),
            ).fetchone()
        if row is None:
            return None
        artifact = self._database.get(Artifact, row[0])
        if artifact is None or not Path(artifact.path).is_file():
            return None
        return artifact

    def _final_path_for(self, video_id: str, token: str) -> Path:
        return Path(self._app_paths.projects) / "content_category" / video_id / f"{token}.json"

    def _read_classification_payload(self, artifact: Artifact) -> "dict[str, Any] | None":
        try:
            return json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_artifact(
        self, *, video_id: str, project_id: "str | None", job_id: "str | None",
        payload: "dict[str, Any]", fingerprint: "str | None", token: str,
    ) -> Artifact:
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        temp_path = self._storage.allocate_temp(suffix="_content_category.json", create=True)
        try:
            temp_path.write_text(content, encoding="utf-8")
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        final_path = self._final_path_for(video_id, token)
        try:
            promoted = self._storage.promote_to_final(temp_path, final_path, overwrite=True)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        artifact = Artifact(
            video_id=video_id, project_id=project_id, job_id=job_id,
            kind=ARTIFACT_KIND_CONTENT_CATEGORY, path=str(promoted), fingerprint=fingerprint,
            size_bytes=len(content.encode("utf-8")),
        )
        self._database.insert(artifact)
        return artifact

    # ===================================================================
    # Job handler -- detecção automática
    # ===================================================================

    def handle_detect_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})

        segments_artifact = self._valid_segments_artifact(job.video_id)
        if segments_artifact is None:
            segments_artifact, failure = self._trigger_segmentation(job.video_id)
            if failure is not None:
                return failure

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_before_classification"})

        cache_key = compute_cache_key(
            segments_artifact.fingerprint,
            gameplay_weak_signal_cap=self._gameplay_weak_signal_cap,
            question_markers=self._question_markers,
            answer_cue_lexicon=self._answer_cue_lexicon,
            tutorial_lexicon=self._tutorial_lexicon,
            vlog_lexicon=self._vlog_lexicon,
            gameplay_lexicon=self._gameplay_lexicon,
            talking_head_lexicon=self._talking_head_lexicon,
        )

        cached = self._find_cached_auto_artifact(job.video_id, cache_key)
        if cached is not None:
            return JobStepResult(
                target_status=JOB_READY,
                semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
                data={"cache_hit": True, "cache_key": cache_key, "artifact_id": cached.id},
            )

        try:
            texts = self._read_segment_texts(segments_artifact)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "invalid_segments_artifact", "detail": exc.__class__.__name__},
            )

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_after_read"})

        local_path = job.extra.get("local_path") if isinstance(job.extra, dict) else None
        visual_sample = (
            self._gameplay_visual_backend(local_path, 0.0, 0.0) if local_path else ActivitySample(False)
        )
        audio_sample = (
            self._gameplay_audio_backend(local_path, 0.0, 0.0) if local_path else ActivitySample(False)
        )

        result = classify_segments(
            texts,
            question_markers=self._question_markers, answer_cue_lexicon=self._answer_cue_lexicon,
            tutorial_lexicon=self._tutorial_lexicon, vlog_lexicon=self._vlog_lexicon,
            gameplay_lexicon=self._gameplay_lexicon, talking_head_lexicon=self._talking_head_lexicon,
            gameplay_weak_signal_cap=self._gameplay_weak_signal_cap,
            gameplay_visual_sample=visual_sample, gameplay_audio_sample=audio_sample,
        )

        payload = {
            "version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "source": SOURCE_AUTO,
            "content_segments_artifact_id": segments_artifact.id,
            "content_segments_cache_key": segments_artifact.fingerprint,
            **result.to_dict(),
        }
        artifact = self._write_artifact(
            video_id=job.video_id, project_id=job.project_id, job_id=job.id,
            payload=payload, fingerprint=cache_key, token=cache_key,
        )

        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={
                "cache_hit": False, "cache_key": cache_key, "artifact_id": artifact.id,
                "category": result.category, "confidence": result.confidence,
            },
        )

    # ===================================================================
    # Override manual -- nunca apaga a detecção automática
    # ===================================================================

    def register_override(
        self, video_id: str, category: str, *, project_id: "str | None" = None, reason: "str | None" = None,
    ) -> Artifact:
        """Grava um NOVO Artifact de override -- a detecção automática
        original permanece intacta e recuperável via
        ``get_latest_auto_classification``. Ver seção 0.6 da docstring do
        módulo."""
        if not video_id:
            raise ValueError("video_id não pode ser vazio")
        validate_content_category(category)
        payload = {
            "version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "source": SOURCE_MANUAL_OVERRIDE,
            "category": category,
            "confidence": 1.0,
            "category_scores": {},
            "signals": {},
            "reason": reason or "override manual registrado pelo usuário",
        }
        import uuid
        token = f"override-{uuid.uuid4().hex}"
        return self._write_artifact(
            video_id=video_id, project_id=project_id, job_id=None,
            payload=payload, fingerprint=None, token=token,
        )

    def get_latest_classification(self, video_id: str) -> "dict[str, Any] | None":
        """O valor EFETIVO atual -- o Artifact mais recente desse
        ``kind`` para o vídeo, seja ele automático ou override (mesma
        convenção "mais recente vence" de todo o projeto)."""
        artifact = self._find_latest_any_classification(video_id)
        if artifact is None:
            return None
        return self._read_classification_payload(artifact)

    def get_latest_auto_classification(self, video_id: str) -> "dict[str, Any] | None":
        """A detecção AUTOMÁTICA mais recente -- sempre recuperável e
        auditável mesmo depois de um ou mais overrides manuais, porque
        nenhum override jamais apaga/sobrescreve uma linha existente."""
        with self._database.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC",
                (video_id, ARTIFACT_KIND_CONTENT_CATEGORY),
            ).fetchall()
        for row in rows:
            artifact = self._database.get(Artifact, row[0])
            if artifact is None:
                continue
            payload = self._read_classification_payload(artifact)
            if payload is not None and payload.get("source") == SOURCE_AUTO:
                return payload
        return None

    def _find_latest_any_classification(self, video_id: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (video_id, ARTIFACT_KIND_CONTENT_CATEGORY),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])
