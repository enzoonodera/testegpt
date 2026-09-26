# -*- coding: utf-8 -*-
"""SmartClipEngine -- Prompt 44 (Fase 7: Smart Clip -- primeiro prompt).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie base do SmartClipEngine.

    Receber vídeo longo. Transcrever. Dividir semanticamente em
    assuntos/segmentos. Detectar: começo da ideia; desenvolvimento;
    conclusão; mudanças de assunto. Não escolher corte somente pelo
    timestamp da frase mais forte.

===========================================================================
0. VISÃO GERAL -- O QUE ESTE MÓDULO É E O QUE ELE NÃO É
===========================================================================

``SmartClipEngine`` é o PRIMEIRO módulo da Fase 7 (Smart Clip). Ele produz
uma segmentação ESTRUTURAL do conteúdo de um vídeo -- onde cada trecho
falado provavelmente começa/termina, e qual o papel de cada trecho dentro
do todo (começo/desenvolvimento/conclusão) -- usando SOMENTE sinais locais
já presentes na transcrição (nenhuma API externa, nenhum modelo de
linguagem, nenhuma pontuação de "quão bom é o corte"). A frase final do
roadmap ("não escolher corte somente pelo timestamp da frase mais forte")
é uma restrição do que este módulo explicitamente NÃO faz ainda -- pontuar/
ranquear candidatos a clipe é o Prompt 45; expandir/ajustar bordas de um
clipe já escolhido é o Prompt 47; detectar o TIPO de conteúdo (tutorial,
vlog, podcast, etc.) é o Prompt 46. Este módulo entrega só a BASE: a
segmentação em si, com papéis estruturais e marcação de mudança de
assunto -- nenhuma decisão de "qual pedaço vale a pena virar clipe" é
tomada aqui.

===========================================================================
0.1 -- TRANSCRIÇÃO: NUNCA REIMPLEMENTADA, SEMPRE REAPROVEITADA
===========================================================================

A transcrição real já existe -- ``captions_engine.py`` (Prompt 31) já
transcreve via ``faster-whisper`` e persiste um ``Artifact``
``kind="transcript_internal"`` (JSON com segmentos ``start``/``end``/
``text``), com cache-hit/cache-miss por ``(video_id, kind, fingerprint)``
já resolvido por aquele módulo. ``SmartClipEngine`` NUNCA chama um backend
de transcrição diretamente -- isso duplicaria a autoridade de
``CaptionsEngine`` sobre transcrição (GATE item 7). Em vez disso:

- Localiza o ``Artifact`` ``transcript_internal`` MAIS RECENTE para este
  ``video_id`` (mesma convenção "mais recente é o atual" já usada por
  ``RenderEngine``/``FinalMediaValidator`` -- nunca recomputa o
  ``cache_key`` de ``CaptionsEngine``, que dependeria de
  ``model_size``/``language`` que este módulo não tem motivo pra conhecer).
- Se não existir NENHUM ``transcript_internal`` válido (artifact ausente,
  ou apontando pra um arquivo que não existe mais no disco), este módulo
  **aciona o handler já existente de ``CaptionsEngine``** para produzir a
  transcrição -- nunca reimplementa a lógica de transcrição, só invoca o
  caminho de produção já testado. O acionamento é feito através de um
  ``JobEngine`` INTERNO e privado (``self._transcription_job_engine``),
  construído e usado exclusivamente para este propósito -- ver seção 0.2
  para a justificativa completa dessa decisão de design.

===========================================================================
0.2 -- POR QUE UM ``JobEngine`` INTERNO, NÃO UM PARÂMETRO EXTERNO
===========================================================================

Nenhum outro módulo de ``_sistema/`` recebe ``JobEngine`` como dependência
de construtor (``BatchEngine`` é a única exceção, e é uma camada de
orquestração de Jobs por natureza -- não um "engine de conteúdo" como
este). Fazer ``SmartClipEngine`` exigir um ``JobEngine`` externo seria
introduzir uma forma de dependência nova e sem precedente só pra este
módulo, e criaria uma ambiguidade real: qual ``JobEngine`` deveria ser
usado -- o mesmo que está avançando o Job DESTE módulo (o chamador), ou um
dedicado? Reaproveitar o do chamador arriscaria efeitos colaterais
inesperados no registro de handlers dele (``register_handler`` é
compartilhado por operação -- duas responsabilidades diferentes
registrando handlers no mesmo objeto por motivos diferentes).

A decisão adotada -- mesmo espírito de ``RenderEngine`` construindo um
``CaptionsEngine``/``AutoReframeEngine`` internos (seção 0 daquele
módulo) -- é ``SmartClipEngine`` construir tanto o ``CaptionsEngine``
quanto um ``JobEngine`` PRÓPRIOS e privados, este último com um único
propósito: reivindicar e executar (``advance``) o Job de transcrição
através do MESMO mecanismo real de produção (``JobEngine.advance`` +
``CaptionsEngine.handle_transcription_job``), nunca uma casca simplificada
que só chama o handler "por fora" do ciclo de vida real de ``Job`` (isso
deixaria o ``Job`` de transcrição órfão, sem transição final persistida --
inaceitável: todo ``Job`` que existe tem que terminar em um estado
persistente coerente, GATE item 13). É seguro chamar
``JobEngine.advance`` recursivamente a partir de dentro de outro handler:
``advance`` nunca mantém uma transação SQLite aberta enquanto o handler
roda -- o ``_claim`` (que É transacional) já commitou e fechou a conexão
antes do handler ser invocado (confirmado por leitura direta de
``job_engine.py``) -- então não há risco de deadlock/reentrância no mesmo
``LocalDatabase``.

===========================================================================
0.3 -- SEGMENTAÇÃO SEMÂNTICA -- OS SINAIS ESCOLHIDOS (E POR QUÊ)
===========================================================================

Sem API externa, sem modelo de linguagem: a segmentação usa EXATAMENTE
dois sinais locais, ambos calculados só a partir da própria sequência de
segmentos de fala já transcritos (``start``/``end``/``text``):

1. **Gap de silêncio entre dois segmentos consecutivos**
   (``proximo.start - atual.end >= min_topic_gap_seconds``). Uma pausa
   comum entre frases dentro do mesmo assunto costuma ser curta (< 1s);
   uma pausa mais longa tipicamente acompanha uma transição real de
   assunto ou um momento de "deixa eu pensar"/"próximo ponto". Valor
   padrão: ``MIN_TOPIC_GAP_SECONDS_PADRAO = 1.5`` segundos -- um valor
   conservador (nem tão curto que quebre uma frase só porque o locutor
   respirou, nem tão longo que nunca dispare em fala natural), exposto
   como parâmetro do construtor (``min_topic_gap_seconds``) para
   calibração futura sem mudança de código -- mesmo espírito de
   ``MIN_OUTPUT_SIZE_BYTES`` em ``final_media_validator.py`` (Prompt 43).
2. **Marcador de discurso no início do texto do próximo segmento**
   (``proximo.text`` normalizado -- ``strip().lower()`` -- começa com uma
   frase de um léxico curado, ex. "agora,", "voltando", "próximo
   assunto", "concluindo", "resumindo", "por fim"). Técnica clássica e
   bem documentada de segmentação de discurso baseada em marcadores
   lexicais -- não é IA, é casamento de prefixo de string contra uma
   lista fixa. Léxico padrão em ``DEFAULT_DISCOURSE_MARKERS``, exposto
   como parâmetro do construtor (``discourse_markers``) pelo mesmo motivo
   do item acima.

Qualquer um dos dois sinais, isoladamente, já é suficiente pra abrir um
novo segmento semântico -- nunca é preciso os dois ao mesmo tempo. O sinal
que efetivamente disparou cada corte é registrado no Artifact
(``split_gap_seconds``/``split_discourse_marker``, ambos ``None`` só no
primeiro segmento) -- nunca um score opaco único, e sim os sinais brutos,
auditáveis, que motivaram a decisão. Isso é, na prática, a aplicação
concreta da restrição final do roadmap: a decisão de ONDE cortar nunca se
resume a "o timestamp da frase mais forte" -- é sempre a combinação
transparente de sinais estruturais reais.

**LIMITAÇÃO CONHECIDA E DOCUMENTADA (exigida pelo Prompt)**: se um
transcript não tem NENHUM gap perceptível (fala contínua do início ao fim,
sem pausas >= ``min_topic_gap_seconds``) E nenhum trecho começa com um dos
marcadores do léxico, o resultado é um ÚNICO segmento semântico cobrindo o
vídeo inteiro -- não há um terceiro sinal de "força bruta" (ex. um teto de
duração) que force uma quebra artificial só para produzir mais de um
segmento. Inventar esse terceiro sinal seria precisamente o tipo de corte
"só por timestamp" que o roadmap pede pra evitar aqui -- a base
(Prompt 44) prefere devolver um segmento único e honesto a fingir uma
granularidade semântica que os sinais disponíveis não sustentam. Prompts
futuros (45+) podem introduzir sinais adicionais (prosódicos, léxicos mais
ricos, ou multimodais) com evidência própria, quando existir.

===========================================================================
0.4 -- PAPEL ESTRUTURAL (BEGINNING/DEVELOPMENT/CONCLUSION)
===========================================================================

Atribuído por POSIÇÃO no resultado da segmentação -- o único sinal
honesto e determinístico disponível sem análise semântica real de
conteúdo (que exigiria IA/API, fora de escopo aqui): o PRIMEIRO segmento
semântico é ``BEGINNING``, o ÚLTIMO é ``CONCLUSION``, todos os do meio são
``DEVELOPMENT``. Caso degenerado (exatamente 1 segmento -- vídeo curto ou
sem nenhum sinal de quebra, seção 0.3): por convenção documentada, esse
único segmento recebe ``BEGINNING`` -- não existe desenvolvimento/conclusão
para distinguir quando há apenas uma unidade, e inventar um papel
composto sem evidência real seria fabricar uma distinção que os dados não
sustentam.

===========================================================================
0.5 -- MUDANÇA DE ASSUNTO (``topic_change``)
===========================================================================

Cada segmento semântico carrega ``topic_change: bool`` -- ``True`` para
todo segmento EXCETO o primeiro (que não tem um "anterior" para mudar
de assunto em relação a ele). Por construção, toda fronteira entre dois
segmentos semânticos JÁ representa um ponto onde um dos sinais da seção
0.3 disparou -- é exatamente por isso que ali existe uma fronteira. O
valor de ``topic_change`` é honesto e não redundante: ele existe para que
consumidores futuros (Prompt 47+) não precisem recalcular fronteiras a
partir de ``index``, e os campos ``split_gap_seconds``/
``split_discourse_marker`` registram COM QUE FORÇA/tipo de evidência
aquela mudança foi detectada.

===========================================================================
0.6 -- COBERTURA -- SEM BURACO, SEM SOBREPOSIÇÃO
===========================================================================

Cada segmento semântico é um agrupamento CONTÍGUO de segmentos brutos da
transcrição original, em ordem -- nunca reordena, nunca descarta, nunca
duplica um segmento bruto. Consequência estrutural (nunca um ajuste
manual): os segmentos semânticos resultantes são estritamente ordenados
e não-sobrepostos (``segmento[i+1].start >= segmento[i].end`` sempre --
na prática, geralmente igual ao gap real do transcript original, que pode
ser ``0`` quando não houve pausa perceptível), e a união deles cobre
exatamente o intervalo ``[primeiro_segmento_bruto.start,
último_segmento_bruto.end]`` -- nenhum trecho de fala original fica de
fora, nenhum é contado duas vezes. "Buraco" aqui significa um segmento
bruto do transcript original desaparecer da segmentação semântica -- isso
nunca acontece, é garantido pela própria construção (particiona a
sequência, nunca filtra). Não significa "zero silêncio entre segmentos" --
silêncio real entre frases faladas é esperado e preservado, não é uma
falha de cobertura.

===========================================================================
0.7 -- CACHE / IDEMPOTÊNCIA
===========================================================================

``compute_cache_key(transcript_cache_key, min_topic_gap_seconds,
discourse_markers)`` -- inclui o ``fingerprint`` (cache_key) do
``Artifact`` de transcrição de origem (se a transcrição mudar -- novo
vídeo, novo modelo Whisper, novo idioma --, a análise semântica também
precisa ser refeita) MAIS os parâmetros de calibração deste módulo
(``min_topic_gap_seconds``/``discourse_markers``, ordenados de forma
canônica) -- mudar qualquer um dos dois invalida o cache automaticamente,
nunca reaproveita silenciosamente uma análise feita com parâmetros
diferentes. ``ALGORITHM_VERSION`` também entra no hash -- uma mudança
futura na LÓGICA de segmentação (não só nos parâmetros) também precisa
invalidar caches antigos.

===========================================================================
0.8 -- ARTIFACT -- ``kind="content_segments_track"``
===========================================================================

Mesmo espírito de ``ARTIFACT_KIND_REFRAME_TRACK``/
``ARTIFACT_KIND_SILENCE_TRACK`` -- um novo ``kind`` de Artifact (JSON),
escrito via ``StorageManager.allocate_temp``/``promote_to_final`` (mesmo
padrão atômico), registrado com ``fingerprint=cache_key``. Escopado só
por ``video_id`` (não por ``project_id`` -- a segmentação semântica é uma
propriedade do CONTEÚDO do vídeo, não de nenhuma decisão de edição de um
Project específico -- mesmo raciocínio já aplicado a
``ARTIFACT_KIND_TRANSCRIPT``/``ARTIFACT_KIND_REFRAME_TRACK``).
``CHECKPOINT_ANALYZED`` (reservado desde o início do vocabulário, nunca
gravado até este Prompt) é gravado SOMENTE no sucesso total -- nunca em
caminho de falha.

===========================================================================
0.9 -- FORA DE ESCOPO (declarado explicitamente, nunca implementado aqui)
===========================================================================

Score/ranking de candidatos a clipe (Prompt 45); geração de um clipe final
com start/end prontos pra render (Prompt 47); detecção de tipo de
conteúdo/formato (Prompt 46); qualquer integração com ``RenderEngine``;
qualquer chamada a API externa/modelo de linguagem; qualquer mudança em
``captions_engine.py`` além de reaproveitar por leitura (o handler de
transcrição é chamado, nunca modificado).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

from .app_paths import AppPaths
from .captions_engine import (
    ARTIFACT_KIND_TRANSCRIPT,
    CaptionsEngine,
    TranscriptionSegment,
)
from .control_manager import ControlManager
from .domain import (
    Artifact,
    CHECKPOINT_ANALYZED,
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_READY,
)
from .edit_project import EditProjectManager
from .job_engine import JobEngine, JobHandlerError, JobStepResult
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager

__all__ = [
    "OPERATION_ANALYZE_CONTENT",
    "ARTIFACT_KIND_CONTENT_SEGMENTS",
    "ALGORITHM_VERSION",
    "MIN_TOPIC_GAP_SECONDS_PADRAO",
    "DEFAULT_DISCOURSE_MARKERS",
    "SEGMENT_ROLE_BEGINNING",
    "SEGMENT_ROLE_DEVELOPMENT",
    "SEGMENT_ROLE_CONCLUSION",
    "SEGMENT_ROLES",
    "SemanticSegment",
    "SmartClipEngineError",
    "InvalidTranscriptArtifactError",
    "compute_cache_key",
    "segment_transcript",
    "SmartClipEngine",
]

OPERATION_ANALYZE_CONTENT = "ANALYZE_CONTENT"

ARTIFACT_KIND_CONTENT_SEGMENTS = "content_segments_track"

# Muda quando a LÓGICA de segmentação muda (não os parâmetros -- esses já
# entram no cache_key por si só). Ver seção 0.7 da docstring do módulo.
ALGORITHM_VERSION = "1"

MIN_TOPIC_GAP_SECONDS_PADRAO = 1.5

DEFAULT_DISCOURSE_MARKERS: "frozenset[str]" = frozenset({
    "agora,", "agora vamos", "bom,", "beleza,", "então,", "voltando",
    "mudando de assunto", "próximo assunto", "próximo ponto", "primeiro,",
    "segundo,", "terceiro,", "por fim", "para finalizar", "concluindo",
    "resumindo", "enfim,", "vamos começar", "vamos falar sobre",
})

SEGMENT_ROLE_BEGINNING = "BEGINNING"
SEGMENT_ROLE_DEVELOPMENT = "DEVELOPMENT"
SEGMENT_ROLE_CONCLUSION = "CONCLUSION"
SEGMENT_ROLES = frozenset({SEGMENT_ROLE_BEGINNING, SEGMENT_ROLE_DEVELOPMENT, SEGMENT_ROLE_CONCLUSION})


class SmartClipEngineError(RuntimeError):
    """Classe base de todos os erros deste módulo."""


class InvalidTranscriptArtifactError(SmartClipEngineError):
    """O Artifact ``transcript_internal`` localizado existe, mas seu
    conteúdo não pôde ser interpretado como uma transcrição válida
    (arquivo corrompido/schema inesperado) -- nunca propaga como uma
    exceção genérica não tratada até o chamador do handler."""


@dataclass(frozen=True)
class SemanticSegment:
    """Um segmento semântico -- agrupamento contíguo de segmentos brutos
    de transcrição. Ver seção 0.3-0.6 da docstring do módulo."""

    index: int
    start: float
    end: float
    text: str
    role: str
    topic_change: bool
    split_gap_seconds: "float | None"
    split_discourse_marker: "str | None"

    def __post_init__(self) -> None:
        if self.role not in SEGMENT_ROLES:
            raise ValueError(f"role desconhecido: {self.role!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "role": self.role,
            "topic_change": self.topic_change,
            "split_gap_seconds": self.split_gap_seconds,
            "split_discourse_marker": self.split_discourse_marker,
        }


# ---------------------------------------------------------------------
# cache_key -- determinístico, inclui transcrição de origem + parâmetros
# ---------------------------------------------------------------------

def _canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_cache_key(
    transcript_cache_key: str,
    *,
    min_topic_gap_seconds: float,
    discourse_markers: "frozenset[str] | Sequence[str]",
) -> str:
    """Ver seção 0.7 da docstring do módulo."""
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "transcript_cache_key": str(transcript_cache_key),
        "min_topic_gap_seconds": float(min_topic_gap_seconds),
        "discourse_markers": sorted(str(m) for m in discourse_markers),
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Segmentação -- função pura, testável sem banco/FFmpeg/Whisper
# ---------------------------------------------------------------------

def _normalize_for_marker_match(text: str) -> str:
    return text.strip().lower()


def _match_discourse_marker(text: str, discourse_markers: "frozenset[str]") -> "str | None":
    normalized = _normalize_for_marker_match(text)
    for marker in discourse_markers:
        if normalized.startswith(marker):
            return marker
    return None


def segment_transcript(
    raw_segments: "Sequence[TranscriptionSegment]",
    *,
    min_topic_gap_seconds: float = MIN_TOPIC_GAP_SECONDS_PADRAO,
    discourse_markers: "frozenset[str]" = DEFAULT_DISCOURSE_MARKERS,
) -> "tuple[SemanticSegment, ...]":
    """Divide uma sequência de segmentos brutos de transcrição (já
    ordenada por ``start``, contrato garantido por
    ``captions_engine._validate_and_normalize_segments``) em segmentos
    semânticos maiores. Ver seções 0.3-0.6 da docstring do módulo para o
    algoritmo completo e suas garantias."""
    if not raw_segments:
        raise ValueError("raw_segments não pode ser vazio")
    if min_topic_gap_seconds <= 0:
        raise ValueError("min_topic_gap_seconds deve ser > 0")

    groups: "list[list[TranscriptionSegment]]" = [[raw_segments[0]]]
    split_signals: "list[tuple[float | None, str | None]]" = [(None, None)]

    for prev, cur in zip(raw_segments, raw_segments[1:]):
        gap = cur.start - prev.end
        gap_signal = gap if gap >= min_topic_gap_seconds else None
        marker_signal = _match_discourse_marker(cur.text, discourse_markers)
        if gap_signal is not None or marker_signal is not None:
            groups.append([cur])
            split_signals.append((gap_signal, marker_signal))
        else:
            groups[-1].append(cur)

    total = len(groups)
    result: "list[SemanticSegment]" = []
    for index, group in enumerate(groups):
        if total == 1 or index == 0:
            role = SEGMENT_ROLE_BEGINNING
        elif index == total - 1:
            role = SEGMENT_ROLE_CONCLUSION
        else:
            role = SEGMENT_ROLE_DEVELOPMENT
        gap_signal, marker_signal = split_signals[index]
        result.append(
            SemanticSegment(
                index=index,
                start=group[0].start,
                end=group[-1].end,
                text=" ".join(seg.text for seg in group),
                role=role,
                topic_change=(index > 0),
                split_gap_seconds=gap_signal,
                split_discourse_marker=marker_signal,
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------
# SmartClipEngine
# ---------------------------------------------------------------------


class SmartClipEngine:
    """Job handler de segmentação semântica de base -- ver docstring do
    módulo para o contrato completo."""

    OPERATION: "ClassVar[str]" = OPERATION_ANALYZE_CONTENT

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        storage_manager: StorageManager,
        app_paths: AppPaths,
        audit_log: "OperationalAuditLog | None" = None,
        control_manager: "ControlManager | None" = None,
        captions_engine: "CaptionsEngine | None" = None,
        min_topic_gap_seconds: float = MIN_TOPIC_GAP_SECONDS_PADRAO,
        discourse_markers: "frozenset[str]" = DEFAULT_DISCOURSE_MARKERS,
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
        if captions_engine is not None and not isinstance(captions_engine, CaptionsEngine):
            raise TypeError(
                f"captions_engine deve ser CaptionsEngine (recebido: {type(captions_engine)!r})"
            )
        if min_topic_gap_seconds <= 0:
            raise ValueError("min_topic_gap_seconds deve ser > 0")
        discourse_markers = frozenset(str(m) for m in discourse_markers)

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._control_manager = control_manager
        self._min_topic_gap_seconds = float(min_topic_gap_seconds)
        self._discourse_markers = discourse_markers

        self._captions_engine = captions_engine if captions_engine is not None else CaptionsEngine(
            manager, database=database, storage_manager=storage_manager,
            app_paths=app_paths, audit_log=self._audit_log, control_manager=control_manager,
        )
        # JobEngine interno e privado -- só existe pra acionar o handler
        # real de transcrição quando necessário (seção 0.2 da docstring).
        # Nunca exposto, nunca usado para nenhuma outra operation.
        self._transcription_job_engine = JobEngine(
            database, audit_log=self._audit_log, control_manager=control_manager,
        )
        self._transcription_job_engine.register_handler(
            CaptionsEngine.OPERATION, self._captions_engine.handle_transcription_job,
            claims_status=JOB_PROCESSING,
        )

    # -- cancelamento --------------------------------------------------

    def _cancel_requested(self, job_id: str) -> bool:
        if self._control_manager is None:
            return False
        return self._control_manager.is_job_cancel_requested(job_id)

    # -- transcrição: localizar ou acionar ------------------------------

    def _find_latest_transcript_artifact(self, video_id: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (video_id, ARTIFACT_KIND_TRANSCRIPT),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _valid_transcript_artifact(self, video_id: str) -> "Artifact | None":
        artifact = self._find_latest_transcript_artifact(video_id)
        if artifact is None or not Path(artifact.path).is_file():
            return None
        return artifact

    def _trigger_transcription(self, video_id: str) -> "tuple[Artifact | None, JobStepResult | None]":
        """Aciona o handler real de ``CaptionsEngine`` através do
        ``JobEngine`` interno (seção 0.2). Devolve o Artifact resultante
        (ou ``None`` se a transcrição não terminou em sucesso) e,
        quando aplicável, um ``JobStepResult`` de falha já pronto para o
        chamador devolver diretamente."""
        transcription_job = self._audit_log.create_job(
            Job(video_id=video_id, operation=CaptionsEngine.OPERATION)
        )
        try:
            result_job = self._transcription_job_engine.advance(transcription_job.id)
        except JobHandlerError as exc:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "transcription_trigger_failed",
                    "transcription_job_id": transcription_job.id,
                    "detail": exc.__class__.__name__,
                },
            )
        if result_job.status != JOB_READY:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "transcription_not_ready",
                    "transcription_job_id": transcription_job.id,
                    "transcription_status": result_job.status,
                },
            )
        artifact = self._valid_transcript_artifact(video_id)
        if artifact is None:
            return None, JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "transcription_succeeded_but_artifact_missing",
                    "transcription_job_id": transcription_job.id,
                },
            )
        return artifact, None

    # -- leitura do transcript -------------------------------------------

    def _read_transcript_segments(self, artifact: Artifact) -> "tuple[TranscriptionSegment, ...]":
        try:
            raw = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidTranscriptArtifactError(
                f"não foi possível ler/decodificar o artifact de transcrição {artifact.id!r}"
            ) from exc
        if not isinstance(raw, Mapping) or not isinstance(raw.get("segments"), list):
            raise InvalidTranscriptArtifactError(
                f"schema inesperado no artifact de transcrição {artifact.id!r}"
            )
        segments: "list[TranscriptionSegment]" = []
        for item in raw["segments"]:
            if not isinstance(item, Mapping):
                raise InvalidTranscriptArtifactError(
                    f"segmento com formato inesperado no artifact {artifact.id!r}"
                )
            try:
                segments.append(
                    TranscriptionSegment(
                        start=float(item["start"]), end=float(item["end"]), text=str(item["text"])
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidTranscriptArtifactError(
                    f"segmento inválido no artifact de transcrição {artifact.id!r}"
                ) from exc
        if not segments:
            raise InvalidTranscriptArtifactError(
                f"artifact de transcrição {artifact.id!r} não contém nenhum segmento"
            )
        return tuple(segments)

    # -- cache/Artifact desta análise -------------------------------------

    def _find_cached_artifact(self, video_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (video_id, ARTIFACT_KIND_CONTENT_SEGMENTS, cache_key),
            ).fetchone()
        if row is None:
            return None
        artifact = self._database.get(Artifact, row[0])
        if artifact is None or not Path(artifact.path).is_file():
            return None
        return artifact

    def _final_path_for(self, video_id: str, cache_key: str) -> Path:
        return Path(self._app_paths.projects) / "content_segments" / video_id / f"{cache_key}.json"

    def _write_and_register_artifact(
        self,
        job: Job,
        cache_key: str,
        transcript_artifact: Artifact,
        semantic_segments: "tuple[SemanticSegment, ...]",
    ) -> Artifact:
        payload = {
            "version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "transcript_artifact_id": transcript_artifact.id,
            "transcript_cache_key": transcript_artifact.fingerprint,
            "min_topic_gap_seconds": self._min_topic_gap_seconds,
            "discourse_markers": sorted(self._discourse_markers),
            "segments": [seg.to_dict() for seg in semantic_segments],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)

        temp_path = self._storage.allocate_temp(suffix="_content_segments.json", create=True)
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
            kind=ARTIFACT_KIND_CONTENT_SEGMENTS,
            path=str(promoted),
            fingerprint=cache_key,
            size_bytes=len(content.encode("utf-8")),
        )
        self._database.insert(artifact)
        return artifact

    # ===================================================================
    # Job handler
    # ===================================================================

    def handle_analyze_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})

        # Nenhuma validação de Video/SourceAsset aqui de propósito: este
        # handler nunca lê ``video.source_asset_id``/``source.local_path``
        # (não toca o arquivo de mídia original, só a transcrição já
        # produzida por CaptionsEngine) -- validar essas entidades por
        # validar seria acoplamento sem uso real e, pior, quebraria
        # espuriamente o caminho de cache-hit (artifact de análise já
        # válido) caso o Video/SourceAsset associado fosse alterado depois
        # -- nada aqui depende deles continuarem existindo. A validação
        # real de Video/SourceAsset acontece dentro do handler de
        # transcrição (``CaptionsEngine.handle_transcription_job``),
        # acionado só quando de fato é preciso transcrever (seção 0.1/0.2
        # da docstring do módulo).

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_before_transcript"})

        transcript_artifact = self._valid_transcript_artifact(job.video_id)
        if transcript_artifact is None:
            transcript_artifact, failure = self._trigger_transcription(job.video_id)
            if failure is not None:
                return failure

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_before_segmentation"})

        cache_key = compute_cache_key(
            transcript_artifact.fingerprint,
            min_topic_gap_seconds=self._min_topic_gap_seconds,
            discourse_markers=self._discourse_markers,
        )

        cached = self._find_cached_artifact(job.video_id, cache_key)
        if cached is not None:
            self._audit_log.record_checkpoint(
                job.id, CHECKPOINT_ANALYZED, data={"cache_hit": True, "cache_key": cache_key}
            )
            return JobStepResult(
                target_status=JOB_READY,
                semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
                data={"cache_hit": True, "cache_key": cache_key, "artifact_id": cached.id},
            )

        try:
            raw_segments = self._read_transcript_segments(transcript_artifact)
        except InvalidTranscriptArtifactError as exc:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "invalid_transcript_artifact", "detail": exc.__class__.__name__},
            )

        semantic_segments = segment_transcript(
            raw_segments,
            min_topic_gap_seconds=self._min_topic_gap_seconds,
            discourse_markers=self._discourse_markers,
        )

        if self._cancel_requested(job.id):
            return JobStepResult(target_status=JOB_CANCELLED, data={"reason": "cancelled_after_segmentation"})

        artifact = self._write_and_register_artifact(job, cache_key, transcript_artifact, semantic_segments)

        self._audit_log.record_checkpoint(
            job.id, CHECKPOINT_ANALYZED, data={"cache_hit": False, "cache_key": cache_key}
        )
        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={
                "cache_hit": False,
                "cache_key": cache_key,
                "artifact_id": artifact.id,
                "segment_count": len(semantic_segments),
            },
        )
