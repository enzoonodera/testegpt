# -*- coding: utf-8 -*-
"""AutoReframe -- Prompt 33 (Fase 5: Editor Modular Não Destrutivo).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie AutoReframe opcional.
    Converter horizontal/quadrado para vertical.
    Detectar: rosto; pessoa; região relevante.
    Suavizar tracking.
    Se detector falhar: usar fallback seguro.
    Nunca bloquear Job por falha de tracking.

=======================================================================
0. VISÃO GERAL -- MESMA ESTRUTURA EM DUAS PARTES DO PROMPT 31
=======================================================================

Segundo módulo da Fase 5 com processamento real (mesma exceção já aberta
pelo Prompt 31/``captions_engine.py`` -- nunca o padrão puro de decisão
dos Prompts 27-30/32).

--- PARTE A -- DECISÃO ---
Configuração do AutoReframe (habilitado, aspect ratio alvo, força de
suavização, estratégia de fallback), persistida via ``EditProjectManager``
na categoria ``REFRAME`` (``"reframe"``, já reservada desde o Prompt 27 --
ver ``edit_project.py``, confirmado por leitura que ``visual_editor.py``
deliberadamente NÃO usa essa categoria: lá são decisões MANUAIS de
enquadramento do usuário; aqui é o RESULTADO de um detector automático).
Nunca importa ``visual_editor.py`` (zero acoplamento entre módulos irmãos
da Fase 5).

--- PARTE B -- PROCESSAMENTO REAL ---
Detecção real (via um backend injetável, seção 0.2) sobre amostras do
vídeo, geração de uma curva de tracking suavizada, e persistência como
``Artifact`` real (``kind="reframe_track"``) + checkpoint
``MEDIA_PROCESSED`` (seção 0.4). ``_sistema/media_catalog.py`` NÃO é
tocado por este Prompt -- ``BADGE_REFRAMED_9_16`` continua hardcoded
``"0"``, mesma disciplina já aplicada a ``AUDIO_PROCESSED`` (Prompt 30) e
antes disso a ``CAPTIONS``/``TRANSCRIBED`` (Prompt 31, até o Prompt
dedicado de "Catalog Wiring" ligar o badge depois).

=======================================================================
0.1 INFRAESTRUTURA REAPROVEITADA -- NADA REINVENTADO
=======================================================================

- ``_sistema/job_engine.py`` (``JobEngine``): registra um handler
  (``AutoReframeEngine.handle_reframe_job``, contrato ``JobHandler =
  Callable[[Job], JobStepResult]``) para a nova ``operation``
  ``AUTO_REFRAME`` (``claims_status=JOB_PROCESSING`` -- trabalho local,
  sem efeito remoto observável). Mesmo mecanismo do Prompt 31, nenhum
  loop de execução próprio.
- ``_sistema/media_probe.py`` (``MediaProbe.probe``): usado DENTRO do
  handler (nunca fora, nunca antecipado -- ``MediaProbe`` é stateless e
  sob demanda por design, seção 0.3 da própria docstring do módulo) para
  obter ``width``/``height`` reais do arquivo antes de calcular geometria
  de reenquadramento. ``probe`` (rasa) é suficiente -- não ``probe_deep``:
  a geometria só precisa de resolução, não de decodificação de frames
  completa (decisão documentada, seção 0.3 abaixo).
- ``_sistema/domain/models.py`` (``Job``/``Artifact``): a curva final
  (real ou fallback) é um ``Artifact`` real, ``kind="reframe_track"``,
  ``fingerprint`` = chave de cache (seção 0.5), ``path`` apontando para o
  arquivo físico promovido.
- ``_sistema/domain/checkpoints.py`` + ``storage/audit.py``
  (``OperationalAuditLog.record_checkpoint``): reaproveita
  ``CHECKPOINT_MEDIA_PROCESSED`` já existente no vocabulário FECHADO --
  ver seção 0.4 para a justificativa de por que este (e não um checkpoint
  novo).
- ``_sistema/storage_manager.py`` (``allocate_temp``/``promote_to_final``):
  mesmo padrão de escrita atômica do Prompt 31 -- escreve em temp, só
  promove a final depois.
- ``_sistema/control_manager.py`` (``ControlManager.is_job_cancel_requested``):
  mesmo padrão de dois pontos de checagem do Prompt 31 -- seção 0.7.

=======================================================================
0.2 BACKEND DE DETECÇÃO -- PLUGÁVEL, INFRAESTRUTURA PRIMEIRO
=======================================================================

Investigação confirmada nesta rodada: ``requirements.txt`` não lista
NENHUMA biblioteca de visão computacional (nem opencv-python, nem
mediapipe, nem face_recognition/dlib), e o ambiente de sandbox não tem
acesso de rede para instalar uma agora. Estruturalmente idêntico à
situação do Prompt 31 com ``faster-whisper`` no início daquele Prompt.

DECISÃO (uma das duas alternativas aceitas pelo próprio Prompt): este
módulo entrega a INFRAESTRUTURA COMPLETA -- Job/cache/Artifact/fallback/
curva/checkpoint -- com um backend de detecção PLUGÁVEL
(``detection_backend: DetectionBackend | None`` no construtor, mesmo
padrão de ``transcription_backend`` do Prompt 31). Nenhuma biblioteca de
visão computacional real é adicionada a ``requirements.txt`` nesta rodada
-- decisão explícita de NÃO escolher/instalar uma sem informação real
suficiente hoje sobre qual (mesmo espírito de adiar decisões sem dados
já documentado em ``audio_engine.py``/``captions_style.py``). Quando um
Prompt futuro escolher e integrar uma biblioteca real (o candidato óbvio
citado no roadmap é algo como MediaPipe Face/Pose Detection, mas isso
fica para essa rodada futura decidir com informação real), ela entra
IGUAL a ``_default_transcribe``: import local dentro da função de
backend "de verdade" (``_default_detect``, nunca no topo do módulo),
nunca quebrando a importação deste módulo nem a suíte de testes.

Contrato do backend injetável (``DetectionBackend``):

    Callable[[str, float, int, int], DetectionSample]
    (local_path, timestamp_seconds, width, height) -> DetectionSample

Chamado UMA VEZ por amostra de tempo (seção 0.6 -- estratégia de
amostragem), nunca uma vez por frame decodificado bruto (processar todo
frame de um vídeo longo seria proibitivo -- CLAUDE.md Princípio C, "alto
volume": custo por vídeo precisa ser prevísivel em lote). Testes NUNCA
exercitam ``_default_detect`` -- sempre injetam ``detection_backend`` (um
callable fake determinístico), mesmo espírito do Prompt 31.

``_default_detect`` (produção, sem biblioteca real disponível hoje):
implementado como um STUB EXPLÍCITO que sempre devolve
``DetectionSample(found=False, ...)`` -- nunca finge detectar algo que
não pode detectar hoje. Isso significa que, em produção, SEM um
``detection_backend`` real injetado, o AutoReframe sempre cai no
fallback seguro (crop central) -- comportamento correto e seguro por
construção (nunca bloqueia o Job, nunca inventa uma curva de tracking
falsa), documentado explicitamente como conhecido/esperado até que um
backend real seja integrado.

=======================================================================
0.3 GEOMETRIA -- POR QUE ``probe`` RASO BASTA (NÃO ``probe_deep``)
=======================================================================

A curva de tracking só precisa saber a RESOLUÇÃO real (``width``/
``height``) para normalizar as coordenadas do detector (que trabalha em
pixels do frame amostrado) para o espaço ``0.0``-``1.0`` já usado em todo
o produto (``visual_editor.py``/``captions_style.py``). ``probe_deep``
decodifica frames para validar integridade de dados -- útil para
detectar corrupção, irrelevante para geometria. Usar ``probe`` (rasa,
só ``ffprobe``) é suficiente e mais barato; se ``probe(...).valid`` for
``False`` (contêiner ilegível/sem stream de vídeo/resolução inválida),
isso é tratado como falha ESTRUTURAL do Job (não uma falha de detecção --
sem resolução real não há como calcular geometria alguma, mesma
disciplina de "arquivo de origem ausente" do Prompt 31).

=======================================================================
0.4 CHECKPOINT -- POR QUE ``MEDIA_PROCESSED`` (NÃO UM NOVO)
=======================================================================

``domain/checkpoints.py`` é vocabulário FECHADO (``JOB_CHECKPOINTS``);
qualquer adição exigiria justificativa extraordinária (mesma disciplina
de "pare e justifique" já aplicada a migrations em todos os Prompts).
Confirmado por grep nesta rodada: nenhum módulo além de
``captions_engine.py`` usa ``record_checkpoint`` hoje (só
``CHECKPOINT_TRANSCRIBED``) -- ``CHECKPOINT_MEDIA_PROCESSED`` está livre,
nunca usado por nenhum outro Prompt. Semanticamente é o encaixe direto:
o próprio módulo ``domain/checkpoints.py`` não impõe significado além do
nome (deliberado, ver sua docstring -- "cada operação decide, na
prática, quais etapas percorre"), e "a curva de tracking foi calculada e
persistida" é literalmente uma etapa de PROCESSAMENTO DE MÍDIA
concluída -- não é uma transcrição (``TRANSCRIBED``), não é análise de
conteúdo (``ANALYZED``), não é plano de edição (``EDIT_PLANNED``), não é
composição/render (``COMPOSED``/``RENDERED``). Nenhum checkpoint novo foi
necessário.

=======================================================================
0.5 FORMATO DA CHAVE DE CACHE
=======================================================================

Mesmo mecanismo do Prompt 31 (SHA-256 sobre JSON canônico,
``sort_keys=True``, mesma função ``_canonical_json`` reimplementada aqui
-- nunca importada de ``captions_engine.py``, zero acoplamento entre
módulos irmãos) sobre::

    {"source_fingerprint": <SourceAsset.fingerprint>,
     "target_aspect_ratio": <ex.: "9:16">,
     "smoothing_strength": <float>,
     "fallback_strategy": <ex.: "CENTER_CROP">,
     "detector_backend_version": <constante deste módulo>}

DECISÃO: ``detector_backend_version`` ENTRA na chave de cache --
diferente do Prompt 31 (onde não existe equivalente), aqui um backend de
detecção DIFERENTE (ou uma versão diferente do mesmo backend) pode
produzir uma curva de tracking materialmente diferente para a MESMA
configuração -- tratar isso como "não faz parte da configuração"
esconderia uma curva potencialmente diferente atrás da mesma chave.
``DETECTOR_BACKEND_VERSION`` é uma constante fixa deste módulo (``"v1"``
hoje), a ser incrementada manualmente sempre que o backend de produção
(``_default_detect``) mudar de forma que altere o resultado. A
PRIORIDADE de detecção (rosto > pessoa > região) é uma constante FIXA
(seção 0.6), não configurável -- por isso NÃO entra na chave de cache
hoje (nenhuma variação possível); se um Prompt futuro tornar isso
configurável, precisará entrar na chave nesse momento (documentado como
pendência).

=======================================================================
0.6 ESTRATÉGIA DE AMOSTRAGEM, PRIORIDADE DE DETECÇÃO E SUAVIZAÇÃO
=======================================================================

AMOSTRAGEM: ``DETECTION_SAMPLE_COUNT = 30`` amostras, distribuídas
UNIFORMEMENTE entre ``t=0`` e ``t=duration`` (inclusive nas pontas;
``duration`` vem do ``MediaProbeResult`` real, seção 0.3). DECISÃO: um
número FIXO de amostras (não "uma amostra a cada N segundos") -- garante
custo PREVISÍVEL por vídeo independente da duração (Princípio C do
CLAUDE.md, "alto volume": processar 500+ vídeos em lote exige que cada
um tenha custo previsível, não proporcional à duração). Vídeo de 10s e
vídeo de 10min custam o mesmo número de chamadas ao backend aqui --
trade-off documentado: vídeos muito longos têm cobertura temporal mais
esparsa por amostra: aceito nesta etapa, não um SLA de produto.

PRIORIDADE DE DETECÇÃO: ``DETECTION_PRIORITY = ("FACE", "PERSON",
"REGION")`` -- constante FIXA do módulo, NÃO um campo configurável por
projeto. DECISÃO: o roadmap lista os três em ordem ("detectar: rosto;
pessoa; região relevante") como uma cascata de fallback DENTRO do
detector (se não achar rosto, tenta pessoa; se não achar pessoa, tenta
região relevante) -- não como um menu de escolha independente por
projeto. Tornar isso configurável seria expandir escopo além do que o
roadmap pede (nenhum campo de projeto exige isso). O backend injetável
(seção 0.2) é responsável por essa cascata internamente e devolve, em
``DetectionSample.kind``, qual estratégia efetivamente encontrou algo
(``"FACE"``/``"PERSON"``/``"REGION"``/``None``) -- só para fins de
diagnóstico/auditoria (``JobStepResult.data``), nunca usado para decidir
o fallback (isso é decidido só por ``found``, seção 0.7).

SUAVIZAÇÃO: EMA (média móvel exponencial) sobre as coordenadas
normalizadas de centro (``cx``/``cy``) das amostras ENCONTRADAS, na
ordem temporal -- amostras ``not found`` são puladas na suavização (não
"interpoladas" nem tratadas como ``(0.5, 0.5)``, para não puxar a curva
real na direção do fallback). ``alpha = 1.0 - (smoothing_strength * 0.9)``
-- ``smoothing_strength=0.0`` → ``alpha=1.0`` (curva crua, sem
suavização); ``smoothing_strength=1.0`` → ``alpha=0.1`` (suavização
forte, nunca ``alpha=0`` exatamente -- um ``alpha`` zero congelaria a
curva no primeiro valor para sempre, o que não é "suavizar tracking", é
"ignorar tracking"). Fórmula ``smoothed[0] = raw[0]``;
``smoothed[i] = alpha*raw[i] + (1-alpha)*smoothed[i-1]`` para ``i>0``.

=======================================================================
0.7 FALLBACK SEGURO -- QUANDO E O QUE
=======================================================================

``FALLBACK_MIN_FOUND_RATIO = 0.5`` -- se a fração de amostras com
``found=True`` for MENOR que este limiar, o fallback é engajado. Isto
cobre, com o MESMO mecanismo e o MESMO resultado (``JOB_READY``, nunca
``JOB_FAILED``), os dois cenários que o Prompt pede para distinguir por
teste mas que produzem o mesmo comportamento:
(a) detector funcionou mas não achou nada em quantidade suficiente
    (``found=False`` retornado explicitamente pelo backend);
(b) o backend de detecção lançou uma exceção inesperada por amostra --
    capturada explicitamente pelo handler (``try/except Exception`` POR
    AMOSTRA, nunca deixando propagar) e tratada EXATAMENTE como
    ``found=False`` para aquela amostra. Um backend que lança em TODA
    amostra produz ``found_ratio=0.0`` → mesmo caminho de fallback.

Fallback = crop central estático (``FALLBACK_CENTER_CROP``, único valor
de ``FALLBACK_STRATEGIES`` hoje -- roadmap não pede outra estratégia
segura): curva com 2 keyframes (``t=0`` e ``t=duration``), ambos
``(cx=0.5, cy=0.5)`` -- nenhuma suavização aplicada a um fallback (já é
estático por definição). O ``Artifact`` resultante marca
``fallback=true`` no JSON (seção 0.9) -- nunca disfarçado de curva real.

ISTO É DISTINTO de falha ESTRUTURAL (``SourceAsset``/``Video`` ausente,
arquivo ilegível, ``probe`` inválido) -- essas continuam falhando o Job
normalmente (``JOB_FAILED``), verificadas ANTES de qualquer amostra ser
processada, exatamente como ``captions_engine.py`` faz para suas
próprias falhas estruturais. A diferença central de design deste Prompt
em relação ao Prompt 31: lá, QUALQUER falha do backend de transcrição
falha o Job (a transcrição SENDO o produto, uma transcrição que falhou
não tem substituto seguro); aqui, uma falha do DETECTOR especificamente
tem um substituto seguro documentado (crop central) -- o roadmap pede
isso explicitamente ("nunca bloquear Job por falha de tracking"), então
o handler NUNCA deixa uma exceção de amostra do backend de detecção
propagar, nunca falha o Job só por causa disso.

=======================================================================
0.8 CANCELAMENTO
=======================================================================

Mesmo padrão do Prompt 31: se ``control_manager`` foi fornecido, checa
``is_job_cancel_requested`` em dois pontos -- ANTES de iniciar o loop de
amostragem (evita trabalho pesado desperdiçado) e DEPOIS de calcular a
curva mas ANTES de promover o Artifact a final (evita publicar evidência
de um trabalho que será descartado). Reforço, não substituição -- mesmo
sem essa checagem proativa, ``JobEngine`` já garante o mesmo resultado
via seu próprio contrato pré-existente.

=======================================================================
0.9 FORMATO DO ARQUIVO PERSISTIDO
=======================================================================

JSON de keyframes normalizados (reaproveitando a convenção
``0.0``-``1.0`` já usada em ``visual_editor.py``/``captions_style.py``,
decisão deliberada de consistência, não uma coincidência)::

    {"version": 1, "target_aspect_ratio": "9:16", "fallback": false,
     "detector_backend_version": "v1",
     "keyframes": [{"t": 0.0, "x": 0.5, "y": 0.42}, ...]}

Onde vive (mesmo padrão do Prompt 31, seção 0.2 daquele módulo, POR
VIDEO_ID -- a curva de tracking é uma propriedade do CONTEÚDO VISUAL do
vídeo, não de um Project de edição específico, mesmo raciocínio de cache
de "trocar decisão não deve obrigar reprocessamento", Princípio B do
CLAUDE.md)::

    <paths.projects>/reframe/<video_id>/<cache_key>.json

=======================================================================
0.10 EVIDÊNCIA -- MESMA DECISÃO DO PROMPT 31 (``Job.output_artifact_ids`` não usado)
=======================================================================

Mesmo raciocínio da seção 0.5 de ``captions_engine.py`` -- reaproveitado,
não reinventado: o ``Artifact`` referencia o Job produtor via
``Artifact.job_id``; localização de evidência reaproveitável é sempre
por ``(video_id, kind, fingerprint)``; o handler nunca escreve de volta
no próprio ``Job`` (autoridade única do ``JobEngine`` sobre a persistência
da transição).

=======================================================================
0.11 ``media_catalog.py`` -- NÃO TOCADO
=======================================================================

Confirmado: este Prompt NÃO modifica ``_sistema/media_catalog.py``.
``BADGE_REFRAMED_9_16`` permanece ``"0"`` (hardcoded, estruturalmente
impossível) após este Prompt -- destravar esse badge é trabalho de uma
rodada FUTURA de "Catalog Wiring" dedicada (mesmo padrão já usado para
``CAPTIONS``/``TRANSCRIBED``: Prompt 31 implementou o processamento real,
um Prompt SEPARADO "Catalog Wiring 27.5+30+31" ligou o badge depois, com
sua própria suíte de testes adversariais dedicada).

=======================================================================
1. PARTE A -- USO
=======================================================================

    engine = AutoReframeEngine(manager, database=db, storage_manager=sm,
                                app_paths=paths)
    engine.set_config(project_id, enabled=True, target_aspect_ratio="9:16",
                       smoothing_strength=0.5, fallback_strategy="CENTER_CROP")
    state = engine.get_config(project_id)

=======================================================================
2. PARTE B -- USO
=======================================================================

    job_engine.register_handler(
        AutoReframeEngine.OPERATION,
        engine.handle_reframe_job,
        claims_status=JOB_PROCESSING,
    )
    job = audit_log.create_job(Job(video_id=video.id, project_id=project.id,
                                    operation=AutoReframeEngine.OPERATION))
    job_engine.advance(job.id)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

from .app_paths import AppPaths
from .control_manager import ControlManager
from .domain import (
    Artifact,
    CHECKPOINT_MEDIA_PROCESSED,
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    SourceAsset,
    Video,
)
from .edit_project import EditProjectManager, ProjectNaoEncontradoError, REFRAME
from .job_engine import JobStepResult
from .media_probe import MediaProbe
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager

# ---------------------------------------------------------------------
# Vocabulário -- Parte A
# ---------------------------------------------------------------------

ASPECT_RATIO_9_16 = "9:16"
# Vocabulário fechado com um único valor hoje -- estruturalmente pronto
# para extensão futura (ex.: "1:1") sem exigir nova migration/campo,
# mas SEM expandir escopo além do que o roadmap pede ("converter
# horizontal/quadrado para vertical").
TARGET_ASPECT_RATIOS = frozenset({ASPECT_RATIO_9_16})

FALLBACK_CENTER_CROP = "CENTER_CROP"
FALLBACK_STRATEGIES = frozenset({FALLBACK_CENTER_CROP})

SMOOTHING_STRENGTH_RANGE = (0.0, 1.0)

OPERATION_AUTO_REFRAME = "AUTO_REFRAME"

ARTIFACT_KIND_REFRAME_TRACK = "reframe_track"

# Cascata FIXA de prioridade de detecção (seção 0.6) -- não configurável.
DETECTION_PRIORITY: "tuple[str, ...]" = ("FACE", "PERSON", "REGION")

DETECTION_SAMPLE_COUNT = 30
FALLBACK_MIN_FOUND_RATIO = 0.5
DETECTOR_BACKEND_VERSION = "v1"


# ---------------------------------------------------------------------
# Erros estruturados -- Parte A
# ---------------------------------------------------------------------

class AutoReframeError(RuntimeError):
    """Classe base de todos os erros deste módulo."""

    code: "ClassVar[str]" = "AUTO_REFRAME_ERRO"


class CampoInvalidoError(AutoReframeError):
    """Um campo de configuração está fora do formato/range aceito."""

    code: "ClassVar[str]" = "CAMPO_INVALIDO"


class VocabularioInvalidoError(AutoReframeError):
    """Um campo de vocabulário fechado recebeu um valor fora dele."""

    code: "ClassVar[str]" = "VOCABULARIO_INVALIDO"


class _InvalidDetectionOutput(Exception):
    """Marcador INTERNO (nunca escapa de método público) -- reservado
    para saída ESTRUTURALMENTE inválida de um ``DetectionSample`` (ex.:
    tipo errado), distinto de "não encontrou nada" (``found=False``, que
    NUNCA é um erro, é um resultado válido -- ver seção 0.7)."""


# ---------------------------------------------------------------------
# Dataclasses -- Parte A
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ReframeConfigState:
    """Snapshot somente-leitura da categoria ``REFRAME`` de um Project.
    Todo campo ``None`` até que o usuário decida explicitamente --
    nenhum default "mágico" é presumido para nenhum campo (mesmo espírito
    de ``CaptionsStyleState``, Prompt 32)."""

    enabled: "bool | None" = None
    target_aspect_ratio: "str | None" = None
    smoothing_strength: "float | None" = None
    fallback_strategy: "str | None" = None


# ---------------------------------------------------------------------
# Dataclasses -- Parte B
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class DetectionSample:
    """Resultado de UMA chamada ao backend de detecção, para UMA amostra
    de tempo. ``cx``/``cy`` são coordenadas normalizadas (``0.0``-``1.0``)
    do CENTRO da região detectada, relativas ao frame inteiro -- só
    presentes quando ``found=True``."""

    found: bool
    cx: "float | None" = None
    cy: "float | None" = None
    kind: "str | None" = None  # "FACE"/"PERSON"/"REGION" -- só diagnóstico


# Backend de detecção injetável: (local_path, timestamp_seconds, width,
# height) -> DetectionSample. Nunca chamado diretamente pelos testes
# deste módulo com o backend real -- ver seção 0.2 da docstring.
DetectionBackend = Callable[[str, float, int, int], DetectionSample]


# ---------------------------------------------------------------------
# Helpers -- cache key (canônico, reimplementado -- nunca importado de
# captions_engine.py, zero acoplamento entre módulos irmãos)
# ---------------------------------------------------------------------

def _canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_cache_key(
    source_fingerprint: str,
    target_aspect_ratio: str,
    smoothing_strength: float,
    fallback_strategy: str,
) -> str:
    """Chave de cache determinística -- ver seção 0.5 da docstring do
    módulo. ``sort_keys=True`` garante que a ordem de construção do dict
    de configuração nunca influencia o resultado."""
    payload = {
        "source_fingerprint": str(source_fingerprint),
        "target_aspect_ratio": str(target_aspect_ratio),
        "smoothing_strength": float(smoothing_strength),
        "fallback_strategy": str(fallback_strategy),
        "detector_backend_version": DETECTOR_BACKEND_VERSION,
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Helpers -- validação de campos (Parte A)
# ---------------------------------------------------------------------

def _validate_enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise CampoInvalidoError(f"enabled deve ser bool (recebido: {value!r})")
    return value


def _validate_target_aspect_ratio(value: Any) -> str:
    if not isinstance(value, str) or value not in TARGET_ASPECT_RATIOS:
        raise VocabularioInvalidoError(
            f"target_aspect_ratio deve ser um de {sorted(TARGET_ASPECT_RATIOS)} (recebido: {value!r})"
        )
    return value


def _validate_smoothing_strength(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampoInvalidoError(f"smoothing_strength deve ser numérico (recebido: {value!r})")
    result = float(value)
    lo, hi = SMOOTHING_STRENGTH_RANGE
    if not (lo <= result <= hi) or result != result:  # NaN nunca é <= hi
        raise CampoInvalidoError(
            f"smoothing_strength deve estar em [{lo}, {hi}] (recebido: {value!r})"
        )
    return result


def _validate_fallback_strategy(value: Any) -> str:
    if not isinstance(value, str) or value not in FALLBACK_STRATEGIES:
        raise VocabularioInvalidoError(
            f"fallback_strategy deve ser um de {sorted(FALLBACK_STRATEGIES)} (recebido: {value!r})"
        )
    return value


_FIELD_VALIDATORS: "dict[str, Any]" = {
    "enabled": _validate_enabled,
    "target_aspect_ratio": _validate_target_aspect_ratio,
    "smoothing_strength": _validate_smoothing_strength,
    "fallback_strategy": _validate_fallback_strategy,
}


def _validate_config_fields(fields: Mapping[str, Any]) -> "dict[str, Any]":
    unknown = set(fields) - set(_FIELD_VALIDATORS)
    if unknown:
        raise CampoInvalidoError(f"campo(s) desconhecido(s): {sorted(unknown)}")
    validated: "dict[str, Any]" = {}
    for name, value in fields.items():
        validated[name] = _FIELD_VALIDATORS[name](value)
    return validated


# ---------------------------------------------------------------------
# Helpers -- amostragem/suavização/fallback (Parte B)
# ---------------------------------------------------------------------

def _sample_timestamps(duration: float, count: int) -> "tuple[float, ...]":
    if count <= 1:
        return (0.0,)
    step = duration / (count - 1)
    return tuple(min(duration, i * step) for i in range(count))


def _smooth_curve(
    found_samples: "Sequence[tuple[float, float, float]]", *, smoothing_strength: float
) -> "tuple[dict[str, float], ...]":
    """``found_samples`` = tuplas ``(t, cx, cy)`` já filtradas por
    ``found=True``, em ordem temporal. Ver seção 0.6 da docstring para a
    fórmula EMA."""
    if not found_samples:
        return ()
    alpha = 1.0 - (smoothing_strength * 0.9)
    smoothed_x = found_samples[0][1]
    smoothed_y = found_samples[0][2]
    curve = [{"t": found_samples[0][0], "x": smoothed_x, "y": smoothed_y}]
    for t, cx, cy in found_samples[1:]:
        smoothed_x = alpha * cx + (1.0 - alpha) * smoothed_x
        smoothed_y = alpha * cy + (1.0 - alpha) * smoothed_y
        curve.append({"t": t, "x": smoothed_x, "y": smoothed_y})
    return tuple(curve)


def _fallback_curve(duration: float) -> "tuple[dict[str, float], ...]":
    return (
        {"t": 0.0, "x": 0.5, "y": 0.5},
        {"t": duration, "x": 0.5, "y": 0.5},
    )


# ---------------------------------------------------------------------
# Backend real (produção) -- stub explícito, ver seção 0.2
# ---------------------------------------------------------------------

def _default_detect(local_path: str, timestamp_seconds: float, width: int, height: int) -> DetectionSample:
    """Backend de produção padrão -- NENHUMA biblioteca de visão
    computacional real está disponível/instalada nesta etapa (ver seção
    0.2). Sempre devolve ``found=False`` -- nunca finge detectar algo.
    Import local reservado para quando uma biblioteca real for integrada
    (mesmo padrão de ``_default_transcribe`` no Prompt 31) -- hoje não há
    nenhum import a isolar."""
    return DetectionSample(found=False)


# ---------------------------------------------------------------------
# AutoReframeEngine
# ---------------------------------------------------------------------

class AutoReframeEngine:
    """Parte A (configuração) + Parte B (detecção/tracking real) -- ver
    seção 0 da docstring do módulo para o contrato completo."""

    OPERATION: "ClassVar[str]" = OPERATION_AUTO_REFRAME

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        storage_manager: StorageManager,
        app_paths: AppPaths,
        audit_log: "OperationalAuditLog | None" = None,
        detection_backend: "DetectionBackend | None" = None,
        control_manager: "ControlManager | None" = None,
        media_probe: "MediaProbe | None" = None,
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
        if detection_backend is not None and not callable(detection_backend):
            raise TypeError("detection_backend deve ser chamável")
        if control_manager is not None and not isinstance(control_manager, ControlManager):
            raise TypeError(
                f"control_manager deve ser ControlManager (recebido: {type(control_manager)!r})"
            )
        if media_probe is not None and not isinstance(media_probe, MediaProbe):
            raise TypeError(f"media_probe deve ser MediaProbe (recebido: {type(media_probe)!r})")

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._backend: DetectionBackend = detection_backend or _default_detect
        self._control_manager = control_manager
        self._probe = media_probe if media_probe is not None else MediaProbe()

    # ===================================================================
    # PARTE A -- configuração
    # ===================================================================

    def set_config(self, project_id: str, **fields: Any) -> ReframeConfigState:
        validated = _validate_config_fields(fields)

        def mutator(current_data: Any) -> dict:
            merged = dict(current_data) if isinstance(current_data, Mapping) else {}
            merged.update(validated)
            return merged

        try:
            self._manager.update_category(project_id, REFRAME, mutator)
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        return self.get_config(project_id)

    def get_config(self, project_id: str) -> ReframeConfigState:
        try:
            state = self._manager.get_category(project_id, REFRAME)
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        if state is None:
            return ReframeConfigState()
        data = state.data if isinstance(state.data, Mapping) else {}

        def _read(name: str, validator) -> Any:
            value = data.get(name)
            if value is None:
                return None
            try:
                return validator(value)
            except AutoReframeError as exc:
                raise CampoInvalidoError(
                    f"{name} persistido é inválido (recebido: {value!r})"
                ) from exc

        return ReframeConfigState(
            enabled=_read("enabled", _validate_enabled),
            target_aspect_ratio=_read("target_aspect_ratio", _validate_target_aspect_ratio),
            smoothing_strength=_read("smoothing_strength", _validate_smoothing_strength),
            fallback_strategy=_read("fallback_strategy", _validate_fallback_strategy),
        )

    # ===================================================================
    # PARTE B -- handler de Job (detecção/tracking real)
    # ===================================================================

    def handle_reframe_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})
        if job.project_id is None:
            # Diferente do Prompt 31 (Parte B independe de Project): aqui a
            # decisão "habilitado"/config vive NO Project -- sem project_id
            # não há de onde ler essa decisão (ver seção 0 da docstring).
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_project_id"})

        try:
            config = self.get_config(job.project_id)
        except ProjectNaoEncontradoError:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "project_not_found", "project_id": job.project_id},
            )
        if config.enabled is not True:
            # Decisão documentada (seção 0 acima): um Job criado para uma
            # operação explicitamente desabilitada/nunca configurada é um
            # erro estrutural de orquestração -- não é "o detector falhou"
            # (a regra "nunca bloquear Job por falha de tracking" é sobre
            # o DETECTOR, não sobre pular a precondição de configuração).
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "reframe_not_enabled"})

        target_aspect_ratio = config.target_aspect_ratio or ASPECT_RATIO_9_16
        smoothing_strength = config.smoothing_strength if config.smoothing_strength is not None else 0.0
        fallback_strategy = config.fallback_strategy or FALLBACK_CENTER_CROP

        video = self._database.get(Video, job.video_id)
        if video is None or video.source_asset_id is None:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "video_or_source_asset_id_missing", "video_id": job.video_id},
            )
        source = self._database.get(SourceAsset, video.source_asset_id)
        if source is None or not source.local_path:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "source_asset_missing_or_no_local_path"}
            )
        if not source.fingerprint:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "source_asset_missing_fingerprint"}
            )

        local_path = str(source.local_path)
        cache_key = compute_cache_key(
            source.fingerprint, target_aspect_ratio, smoothing_strength, fallback_strategy
        )

        if self._cancel_requested(job.id):
            return JobStepResult(
                target_status=JOB_CANCELLED, data={"reason": "cancelled_before_detection"}
            )

        cached_artifact = self._find_cached_artifact(job.video_id, cache_key)
        if cached_artifact is not None:
            self._audit_log.record_checkpoint(
                job.id, CHECKPOINT_MEDIA_PROCESSED, data={"cache_hit": True, "cache_key": cache_key}
            )
            return JobStepResult(
                target_status=JOB_READY,
                semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
                data={"cache_hit": True, "cache_key": cache_key, "reused_artifact_id": cached_artifact.id},
            )

        probe_result = self._probe.probe(local_path)
        if not probe_result.valid or not probe_result.width or not probe_result.height:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "probe_invalid", "error_code": probe_result.error_code},
            )
        width, height = probe_result.width, probe_result.height
        duration = probe_result.duration or 0.0

        # Trabalho pesado real -- amostra a amostra. Exceção POR AMOSTRA do
        # backend de detecção NUNCA propaga (seção 0.7): vira found=False.
        timestamps = _sample_timestamps(duration, DETECTION_SAMPLE_COUNT)
        found_samples: "list[tuple[float, float, float]]" = []
        found_count = 0
        for t in timestamps:
            try:
                sample = self._backend(local_path, t, width, height)
            except Exception:
                continue
            if not isinstance(sample, DetectionSample):
                continue
            if sample.found and sample.cx is not None and sample.cy is not None:
                found_count += 1
                cx = min(1.0, max(0.0, float(sample.cx)))
                cy = min(1.0, max(0.0, float(sample.cy)))
                found_samples.append((t, cx, cy))

        found_ratio = (found_count / len(timestamps)) if timestamps else 0.0
        used_fallback = found_ratio < FALLBACK_MIN_FOUND_RATIO
        if used_fallback:
            keyframes = _fallback_curve(duration)
        else:
            keyframes = _smooth_curve(found_samples, smoothing_strength=smoothing_strength)

        if self._cancel_requested(job.id):
            return JobStepResult(
                target_status=JOB_CANCELLED, data={"reason": "cancelled_after_detection"}
            )

        artifact = self._write_and_register_artifact(
            job, cache_key, target_aspect_ratio, used_fallback, keyframes
        )

        self._audit_log.record_checkpoint(
            job.id,
            CHECKPOINT_MEDIA_PROCESSED,
            data={"cache_hit": False, "cache_key": cache_key, "fallback": used_fallback},
        )
        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={
                "cache_hit": False,
                "cache_key": cache_key,
                "artifact_id": artifact.id,
                "fallback": used_fallback,
                "found_ratio": found_ratio,
            },
        )

    # -- helpers internos de Parte B ------------------------------------

    def _cancel_requested(self, job_id: str) -> bool:
        if self._control_manager is None:
            return False
        return self._control_manager.is_job_cancel_requested(job_id)

    def _find_cached_artifact(self, video_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (video_id, ARTIFACT_KIND_REFRAME_TRACK, cache_key),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _final_path_for(self, video_id: str, cache_key: str) -> Path:
        return Path(self._app_paths.projects) / "reframe" / video_id / f"{cache_key}.json"

    def _write_and_register_artifact(
        self,
        job: Job,
        cache_key: str,
        target_aspect_ratio: str,
        used_fallback: bool,
        keyframes: "tuple[dict[str, float], ...]",
    ) -> Artifact:
        payload = {
            "version": 1,
            "target_aspect_ratio": target_aspect_ratio,
            "fallback": used_fallback,
            "detector_backend_version": DETECTOR_BACKEND_VERSION,
            "keyframes": list(keyframes),
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        temp_path = self._storage.allocate_temp(suffix="_reframe.json", create=True)
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
            video_id=job.video_id,
            project_id=job.project_id,
            job_id=job.id,
            kind=ARTIFACT_KIND_REFRAME_TRACK,
            path=str(promoted),
            fingerprint=cache_key,
            size_bytes=len(content.encode("utf-8")),
        )
        self._database.insert(artifact)
        return artifact


__all__ = [
    "ASPECT_RATIO_9_16",
    "TARGET_ASPECT_RATIOS",
    "FALLBACK_CENTER_CROP",
    "FALLBACK_STRATEGIES",
    "SMOOTHING_STRENGTH_RANGE",
    "OPERATION_AUTO_REFRAME",
    "ARTIFACT_KIND_REFRAME_TRACK",
    "DETECTION_PRIORITY",
    "DETECTION_SAMPLE_COUNT",
    "FALLBACK_MIN_FOUND_RATIO",
    "DETECTOR_BACKEND_VERSION",
    "AutoReframeError",
    "CampoInvalidoError",
    "VocabularioInvalidoError",
    "ReframeConfigState",
    "DetectionSample",
    "DetectionBackend",
    "compute_cache_key",
    "AutoReframeEngine",
]
