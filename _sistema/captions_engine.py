"""CaptionsEngine -- Prompt 31 (Fase 5: Editor Modular Não Destrutivo).

=======================================================================
0. VISÃO GERAL -- ESTE MÓDULO TEM DUAS METADES, DELIBERADAMENTE DISTINTAS
=======================================================================

Diferente de ``timeline_editor.py``/``visual_editor.py``/``audio_engine.py``
(Prompts 28-30), que só armazenam DECISÕES e nunca processam mídia real,
este módulo tem duas partes com contratos diferentes:

--- PARTE A -- DECISÃO (mesma disciplina dos Prompts 28-30) ---
``set_mode``/``get_mode``: o modo de exibição de legenda (``OFF``/
``SEPARATE``/``BURNED``) é uma decisão pura, persistida via
``EditProjectManager`` na categoria ``CAPTIONS`` (já reservada desde o
Prompt 27, valor ``"captions"`` -- reaproveitada aqui, nenhuma string nova
inventada). ``BURNED`` aqui é SÓ a decisão de que a legenda deve
futuramente ser queimada nos pixels -- a queima real (composição/render)
é trabalho de um Render Engine futuro, inteiramente fora do escopo deste
Prompt. Esta parte nunca chama FFmpeg, nunca compõe vídeo, nunca lê
``SourceAsset.local_path``.

--- PARTE B -- PROCESSAMENTO REAL (novo neste Prompt) ---
Transcrição de verdade via Whisper (biblioteca ``faster-whisper``, já
listada em ``requirements.txt``), gerando 3 artefatos reais em disco
(SRT, VTT, representação interna JSON) e registrando evidência
estruturada (``Artifact`` reais + checkpoint ``TRANSCRIBED``) para que um
Prompt FUTURO de "wiring" possa acender o badge ``CAPTIONS`` no Media
Catalog. ``_sistema/media_catalog.py`` NÃO é tocado por este Prompt --
``BADGE_CAPTIONS`` continua hardcoded ``"0"``, mesma disciplina já
aplicada a ``AUDIO_PROCESSED`` no Prompt 30.

Esta parte TEM permissão explícita de ler ``SourceAsset.local_path`` --
exceção documentada à regra dos módulos 27-30 (que nunca tocam o arquivo
original). Ela nunca chama ``subprocess``/FFmpeg (SRT/VTT são texto
puro); a única dependência externa real é ``faster-whisper``, chamada
através de uma única função fina e isolável (ver seção 0.7).

=======================================================================
0.1 INFRAESTRUTURA REAPROVEITADA -- NADA REINVENTADO
=======================================================================

- ``_sistema/job_engine.py`` (``JobEngine``): este módulo REGISTRA um
  handler (``CaptionsEngine.handle_transcription_job``, contrato
  ``JobHandler = Callable[[Job], JobStepResult]``) para a nova
  ``operation`` ``TRANSCRIBE_CAPTIONS`` (``claims_status=JOB_PROCESSING``
  -- trabalho local, sem efeito remoto observável). Nenhum loop de
  execução próprio é criado; ``JobEngine.advance()``/``run_pending()``
  continuam sendo a única forma de rodar o handler.
- ``_sistema/domain/models.py`` (``Job``/``Artifact``): os 3 artefatos
  gerados são ``Artifact`` reais, com ``kind`` em
  (``"transcript_internal"``, ``"captions_srt"``, ``"captions_vtt"``),
  ``fingerprint`` preenchido com a chave de cache (seção 0.4) e ``path``
  apontando para o arquivo físico promovido.
- ``_sistema/domain/checkpoints.py`` (``CHECKPOINT_TRANSCRIBED``) +
  ``_sistema/storage/audit.py`` (``OperationalAuditLog.record_checkpoint``):
  vocabulário/mecanismo já existentes, reaproveitados sem alteração.
- ``_sistema/storage_manager.py`` (``StorageManager.allocate_temp`` +
  ``promote_to_final``): os 3 arquivos são escritos com
  ``allocate_temp(create=True)`` e só se tornam ``FINAL_ARTIFACT`` via
  ``promote_to_final`` -- nunca ``open()`` direto num caminho inventado.

=======================================================================
0.2 ONDE OS ARTEFATOS FINAIS MORAM (decisão sobre AppPaths)
=======================================================================

``AppPaths`` (``app_paths.py``) não tem hoje um campo dedicado a
artefatos gerados. Decisão: NÃO adicionar um campo novo -- os artefatos
finais vivem sob ``paths.projects`` (já um managed root mutável de
``StorageManager``, categoria ``CATEGORY_USER_FILE``, não-limpável), no
subdiretório determinístico::

    <paths.projects>/captions/<video_id>/<cache_key><.srt|.vtt|.json>

Escolhido por VIDEO_ID (não por ``project_id``): a transcrição é uma
propriedade do CONTEÚDO DE ÁUDIO do vídeo, não de um Project de edição
específico -- vários Projects do mesmo vídeo devem poder reaproveitar a
mesma transcrição (mesmo espírito de cache do Prompt 28: "trocar
legenda... não deve obrigar a repetir transcrição", Princípio B do
CLAUDE.md). ``promote_to_final`` cria o diretório de destino
automaticamente (``final_resolved.parent.mkdir(parents=True,
exist_ok=True)``) -- nenhuma criação manual de diretório é necessária
aqui. ``paths.models`` (já existente) é usado como ``download_root`` do
``WhisperModel`` real (seção 0.7) -- não o cache padrão do sistema.

=======================================================================
0.3 FORMATO DA CHAVE DE CACHE
=======================================================================

A chave de cache (usada tanto para nomear os arquivos finais quanto para
localizar evidência reaproveitável) é um SHA-256 sobre uma serialização
canônica (chaves ordenadas, sem espaços -- mesmo padrão de
``edit_project.py:_canonical_json``) de::

    {"source_fingerprint": <SourceAsset.fingerprint>,
     "model_size": <tamanho do modelo Whisper>,
     "language": <idioma ou null>}

DECISÃO: ``language`` ENTRA na chave de cache. Motivo: o parâmetro
``language`` passado ao Whisper materialmente muda o resultado da
transcrição (força/sugere um idioma diferente do detectado
automaticamente) -- tratá-lo como "não faz parte da configuração" seria
esconder uma transcrição potencialmente diferente atrás do mesmo cache
key. ``model_size`` também entra (modelos diferentes produzem qualidade/
resultado diferentes) -- ambos exigidos pelo próprio Prompt como mínimo.
Serialização com ``sort_keys=True`` garante que um dict de configuração
com ordem de chaves diferente NUNCA produza uma chave de cache diferente
por acidente (ver GATE ADVERSARIAL, testado explicitamente).

=======================================================================
0.4 CACHE -- COMO A REUTILIZAÇÃO FUNCIONA
=======================================================================

Antes de chamar o backend de transcrição, o handler procura, por SQL
direto (``video_id + kind + fingerprint``, ``fingerprint`` = cache key),
um ``Artifact`` existente de ``kind="transcript_internal"``. Se
encontrado, TAMBÉM confirma a existência dos 2 ``Artifact`` irmãos
(``captions_srt``/``captions_vtt``) com o mesmo ``fingerprint`` -- só os
3 juntos contam como evidência reaproveitável válida (criados sempre
atomicamente na mesma execução bem-sucedida, seção 0.6). Se qualquer um
faltar (situação defensiva, não esperada em operação normal), o cache é
tratado como AUSENTE e uma nova transcrição é feita -- nunca uma reutilização
parcial. Quando o cache é reaproveitado, o backend de transcrição NUNCA é
chamado (provado por teste com um mock que falha se chamado mais de uma
vez).

A localização de evidência é 100% por ``Artifact`` NA BASE -- nunca por
"o arquivo existe no disco". Isso é o que torna a janela de crash entre
``promote_to_final`` (arquivo físico já publicado) e o INSERT dos 3
``Artifact`` (ainda não commitado) segura: se o processo morrer nesse
meio-tempo, os arquivos físicos ficam órfãos (sem ``Artifact``
correspondente) mas NUNCA são considerados cache válido por uma execução
futura -- que recalcula do zero e simplesmente grava em novos nomes
(``allocate_temp`` usa UUID4, o cache key só entra no nome do ARQUIVO
FINAL; ver seção 0.6 sobre a decisão de ``overwrite=True`` na promoção).

=======================================================================
0.5 EVIDÊNCIA -- Job.output_artifact_ids NÃO É USADO (decisão documentada)
=======================================================================

O Prompt permite escolher "``output_artifact_ids`` do Job, ou o mecanismo
que você decidir". Decisão: os 3 ``Artifact`` referenciam o Job que os
produziu via ``Artifact.job_id`` (campo que já existe exatamente para
isso) -- e a localização de evidência reaproveitável é sempre feita por
``(video_id, kind, fingerprint)``, nunca por ``Job.output_artifact_ids``.

Motivo de NÃO escrever em ``Job.output_artifact_ids``: o contrato
central do ``JobEngine`` (ver docstring de ``job_engine.py``,
``JobStepResult``) é que "o handler nunca escreve diretamente no
storage: ele apenas descreve o resultado e o JobEngine persiste a
transição atomicamente" -- e ``OperationalAuditLog.transition_job``
RECARREGA o ``Job`` do zero a partir do SQLite antes de persistir (só
altera ``status``); qualquer mutação feita pelo handler no objeto
``Job`` em memória que ele recebeu seria silenciosamente descartada. Uma
chamada extra de ``database.save(job)`` feita pelo próprio handler para
persistir ``output_artifact_ids`` fugiria dessa autoridade única e
abriria uma segunda via de escrita para o mesmo Job fora do controle do
``JobEngine`` -- exatamente o tipo de "segunda fonte de verdade" que o
GATE ADVERSARIAL (item 7, AUTORIDADE ÚNICA) probe. Os ids dos artefatos
(criados ou reaproveitados) são devolvidos em ``JobStepResult.data``
(auditável, visível no evento ``JOB_PROCESSING_COMPLETED``), nunca
escritos de volta no próprio ``Job``. Se um Prompt futuro genuinamente
precisar de ``output_artifact_ids`` preenchido, isso deveria ser um
recurso do próprio ``JobEngine``/``JobStepResult`` (ex.: um novo campo
que o Engine persiste atomicamente junto da transição), não um desvio
feito por este handler.

=======================================================================
0.6 SUCESSO, FALHA E CANCELAMENTO -- CONTRATO EXATO DO HANDLER
=======================================================================

O handler NUNCA usa o caminho de "exceção não tratada" para falhas
CONHECIDAS e antecipadas -- ele DECLARA essas explicitamente devolvendo
``JobStepResult(target_status=JOB_FAILED, data={"reason": ...})``:
- Job sem ``video_id``, Video sem ``source_asset_id``, SourceAsset
  ausente/sem ``local_path``/sem ``fingerprint``: falha estrutural
  imediata, sem tocar o backend.
- Arquivo de origem ausente no disco no momento da execução (checado
  explicitamente antes de chamar o backend -- não só confiado ao que o
  SQLite registrou).
- Saída do backend inválida: lista de segmentos vazia (depois de
  descartar segmentos com texto vazio -- ver seção 0.8) ou qualquer
  segmento com timestamp não-finito/negativo/``start >= end``.

Qualquer OUTRA exceção (o backend real lançando algo inesperado, um erro
de I/O ao promover um arquivo, etc.) NÃO é capturada por este handler --
ela propaga para o ``JobEngine``, que já garante (contrato existente,
não reimplementado aqui) que o Job pousa em ``FAILED`` sozinho
(``claims_status=PROCESSING``), de forma auditável, sem o handler
precisar (nem poder) fingir sucesso.

CANCELAMENTO: se um ``control_manager`` foi fornecido na construção, o
handler consulta ``is_job_cancel_requested`` em dois pontos -- ANTES de
chamar o backend (evita trabalho pesado desperdiçado se já cancelado) e
DEPOIS da transcrição mas ANTES de promover qualquer arquivo a final
(evita publicar evidência de um trabalho que será descartado). Em ambos
os casos o handler devolve explicitamente
``JobStepResult(target_status=JOB_CANCELLED, ...)`` -- transição
diretamente permitida a partir de ``PROCESSING`` pela State Machine
central. Isto é reforço, não substituição: mesmo sem essa checagem
proativa, ``JobEngine._finalize_processing_result`` já sobrescreveria
qualquer resultado do handler para ``CANCELLED`` se um cancelamento foi
pedido enquanto o handler rodava (contrato pré-existente do PROMPT 15) --
a checagem aqui só evita transcrever/promover à toa.

CRASH/PROMOÇÃO PARCIAL: os 3 arquivos são escritos e promovidos ANTES de
qualquer INSERT de ``Artifact`` (uma única transaction cobre os 3
INSERTs juntos -- todos os 3 existem na base, ou nenhum). Se o processo
morrer entre a promoção física e essa transaction, os arquivos ficam
órfãos no disco (nunca lidos de volta como cache válido, seção 0.4) --
vazamento de armazenamento conhecido, não corrupção de dados; aceito
como limitação documentada nesta etapa (um GC futuro de artefatos
órfãos, fora de escopo aqui, poderia varrer ``FINAL_ARTIFACT`` sem
``Artifact`` correspondente).

PROMOÇÃO COM ``overwrite=True``: o caminho final é determinístico
(nomeado pela cache key). Decisão: promover com ``overwrite=True`` --
dois Jobs concorrentes transcrevendo o MESMO vídeo com a MESMA
configuração (nenhum via cache ainda, ambos vencendo a corrida de
checagem de cache) podem colidir no mesmo caminho final; como o conteúdo
é determinístico a partir da mesma entrada (mesmo ``fingerprint``+
config), sobrescrever é seguro -- o conteúdo publicado seria idêntico
(byte-a-byte, modulo eventual não-determinismo do próprio modelo, ver
seção 0.9). Cada Job concorrente ainda insere sua PRÓPRIA linha de
``Artifact`` (não há corrida na escrita da tabela ``artifacts`` --
INSERTs distintos, cada um com seu próprio ``id``/``job_id``); duas
linhas apontando para o mesmo ``path``/``fingerprint`` é inofensivo (uma
consulta de cache futura só precisa achar UMA, ``LIMIT 1``).

=======================================================================
0.7 CHAMADA REAL AO WHISPER -- ISOLADA ATRÁS DE UMA FUNÇÃO FINA
=======================================================================

``faster-whisper`` está listado em ``requirements.txt`` mas NÃO está
necessariamente instalado no ambiente onde os TESTES rodam (confirmado
nesta sessão: ``import faster_whisper`` falha no sandbox de
desenvolvimento). Por isso o import da lib é LOCAL a
``_default_transcribe`` (nunca no topo do módulo) -- importar
``captions_engine`` em si nunca falha por causa disso. Testes NUNCA
exercitam ``_default_transcribe`` -- sempre injetam
``transcription_backend`` (um callable) no construtor de
``CaptionsEngine``; a assinatura do backend é
``Callable[[str, str, str | None], TranscriptionResult]`` (``local_path,
model_size, language``).

``gerar_textos.py`` (Geração 1) já chama ``faster-whisper`` hoje -- é
citado aqui só como PRECEDENTE/referência de uso da API real
(``WhisperModel(size, device="cpu", compute_type="int8")``,
``model.transcribe(path, task="transcribe", language=..., vad_filter=True,
beam_size=1, condition_on_previous_text=False)``). A regra de zero
cross-import entre gerações continua valendo integralmente -- este
módulo reimplementa a chamada de forma independente, nunca importa
``gerar_textos``.

=======================================================================
0.8 NORMALIZAÇÃO DE SEGMENTOS
=======================================================================

Segmentos com texto vazio/só espaço (depois de ``strip()``) são
DESCARTADOS silenciosamente (comum em saídas reais de VAD -- não é uma
falha). Se TODOS os segmentos forem descartados dessa forma (ou o
backend já devolveu uma lista vazia), o resultado inteiro é tratado como
"saída inválida" -> falha estruturada (seção 0.6) -- nenhum decisão de
produto ainda existe sobre vídeos legitimamente silenciosos (documentado
como pendência, seção de riscos do relatório). Qualquer segmento com
``start``/``end`` não-finito, negativo, ou ``start >= end`` invalida a
transcrição INTEIRA (não é descartado individualmente) -- um backend que
devolve timestamps estruturalmente quebrados não é confiável o
suficiente para aceitar parcialmente.

=======================================================================
0.9 FORMATOS SRT/VTT
=======================================================================

Gerados a partir da MESMA lista de segmentos normalizada (nunca duas
transcrições independentes) -- SRT usa ``HH:MM:SS,mmm`` (vírgula) e
numeração de cue 1-based; VTT usa cabeçalho ``WEBVTT`` e
``HH:MM:SS.mmm`` (ponto). Nenhuma variação própria inventada.

=======================================================================
1. PARTE A -- USO
=======================================================================

    engine = CaptionsEngine(manager, database=db, storage_manager=sm,
                             app_paths=paths)
    engine.set_mode(project_id, MODE_SEPARATE)
    state = engine.get_mode(project_id)  # CaptionsState(mode="SEPARATE")

=======================================================================
2. PARTE B -- USO
=======================================================================

    job_engine.register_handler(
        CaptionsEngine.OPERATION,
        engine.handle_transcription_job,
        claims_status=JOB_PROCESSING,
    )
    job = audit_log.create_job(Job(video_id=video.id, operation=CaptionsEngine.OPERATION))
    job_engine.advance(job.id)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

from .app_paths import AppPaths
from .control_manager import ControlManager
from .domain import (
    Artifact,
    CHECKPOINT_TRANSCRIBED,
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    SourceAsset,
    Video,
)
from .edit_project import CAPTIONS, EditProjectManager, ProjectNaoEncontradoError
from .job_engine import JobStepResult
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager

# ---------------------------------------------------------------------
# Vocabulário -- Parte A
# ---------------------------------------------------------------------

MODE_OFF = "OFF"
MODE_SEPARATE = "SEPARATE"
MODE_BURNED = "BURNED"
CAPTION_MODES = frozenset({MODE_OFF, MODE_SEPARATE, MODE_BURNED})

# ---------------------------------------------------------------------
# Vocabulário -- Parte B
# ---------------------------------------------------------------------

OPERATION_TRANSCRIBE_CAPTIONS = "TRANSCRIBE_CAPTIONS"

ARTIFACT_KIND_TRANSCRIPT = "transcript_internal"
ARTIFACT_KIND_SRT = "captions_srt"
ARTIFACT_KIND_VTT = "captions_vtt"
ARTIFACT_KINDS = (ARTIFACT_KIND_TRANSCRIPT, ARTIFACT_KIND_SRT, ARTIFACT_KIND_VTT)

DEFAULT_MODEL_SIZE = "small"


# ---------------------------------------------------------------------
# Erros estruturados -- Parte A
# ---------------------------------------------------------------------

class CaptionsEngineError(RuntimeError):
    """Classe base de todos os erros deste módulo."""

    code: ClassVar[str] = "CAPTIONS_ENGINE_ERRO"


class ModoInvalidoError(CaptionsEngineError):
    """``mode`` fora do vocabulário fechado (``CAPTION_MODES``)."""

    code: ClassVar[str] = "MODO_INVALIDO"


class _InvalidTranscriptionOutput(Exception):
    """Marcador INTERNO (nunca escapa de um método público) usado para
    unificar a validação de saída do backend antes de traduzi-la para um
    ``JobStepResult(target_status=JOB_FAILED, ...)`` estruturado."""


# ---------------------------------------------------------------------
# Dataclasses -- Parte A
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class CaptionsState:
    """Snapshot somente-leitura da categoria ``CAPTIONS`` de um Project."""

    mode: "str | None" = None


# ---------------------------------------------------------------------
# Dataclasses -- Parte B
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class TranscriptionSegment:
    """Um segmento cru devolvido pelo backend de transcrição, ANTES da
    normalização/validação (``start``/``end`` em segundos, relativos ao
    início do vídeo original)."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    """Resultado bruto devolvido por um ``TranscriptionBackend``."""

    segments: "tuple[TranscriptionSegment, ...]"
    language: "str | None" = None


# Backend de transcrição injetável: (local_path, model_size, language) ->
# TranscriptionResult. Nunca chamado diretamente pelos testes deste
# módulo com o backend real -- ver seção 0.7 da docstring do módulo.
TranscriptionBackend = Callable[[str, str, "str | None"], TranscriptionResult]


# ---------------------------------------------------------------------
# Helpers -- cache key (canônico, mesmo padrão de edit_project.py)
# ---------------------------------------------------------------------

def _canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_cache_key(source_fingerprint: str, model_size: str, language: "str | None") -> str:
    """Chave de cache determinística -- ver seção 0.3 da docstring do
    módulo. ``sort_keys=True`` garante que a ordem de construção do dict
    de configuração nunca influencia o resultado (GATE ADVERSARIAL)."""

    payload = {
        "source_fingerprint": str(source_fingerprint),
        "model_size": str(model_size),
        "language": language if language is None else str(language),
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Helpers -- normalização/validação de segmentos (seção 0.8)
# ---------------------------------------------------------------------

def _validate_and_normalize_segments(
    segments: Sequence[TranscriptionSegment],
) -> "tuple[TranscriptionSegment, ...]":
    normalized: "list[TranscriptionSegment]" = []
    for raw in segments:
        start = raw.start
        end = raw.end
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
        ):
            raise _InvalidTranscriptionOutput(
                f"timestamp não-finito ou de tipo inválido: start={start!r} end={end!r}"
            )
        start = float(start)
        end = float(end)
        if start < 0.0 or start >= end:
            raise _InvalidTranscriptionOutput(
                f"timestamps fora de ordem ou negativos: start={start!r} end={end!r}"
            )
        text = str(raw.text or "").strip()
        if not text:
            # Segmento vazio/só espaço: descartado silenciosamente (seção 0.8),
            # não invalida o restante da transcrição.
            continue
        normalized.append(TranscriptionSegment(start=start, end=end, text=text))

    if not normalized:
        raise _InvalidTranscriptionOutput(
            "transcrição vazia após normalização (nenhum segmento com texto)"
        )
    return tuple(normalized)


# ---------------------------------------------------------------------
# Helpers -- representação interna / SRT / VTT (seção 0.9)
# ---------------------------------------------------------------------

def _segments_to_internal_json(
    segments: "tuple[TranscriptionSegment, ...]", language: "str | None"
) -> str:
    payload = {
        "version": 1,
        "language": language,
        "segments": [
            {"start": seg.start, "end": seg.end, "text": seg.text} for seg in segments
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _format_vtt_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _segments_to_srt(segments: "tuple[TranscriptionSegment, ...]") -> str:
    blocks = []
    for index, seg in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n"
            f"{_format_srt_timestamp(seg.start)} --> {_format_srt_timestamp(seg.end)}\n"
            f"{seg.text}\n"
        )
    return "\n".join(blocks) + "\n"


def _segments_to_vtt(segments: "tuple[TranscriptionSegment, ...]") -> str:
    blocks = ["WEBVTT\n"]
    for seg in segments:
        blocks.append(
            f"{_format_vtt_timestamp(seg.start)} --> {_format_vtt_timestamp(seg.end)}\n"
            f"{seg.text}\n"
        )
    return "\n".join(blocks) + "\n"


# ---------------------------------------------------------------------
# Backend real (produção) -- isolado, importado só quando chamado
# ---------------------------------------------------------------------

def _default_transcribe(
    local_path: str, model_size: str, language: "str | None", *, download_root: str
) -> TranscriptionResult:
    """Chamada real ao ``faster-whisper`` -- ver seção 0.7. Import LOCAL
    (nunca no topo do módulo): a lib pode não estar instalada no
    ambiente onde os testes rodam, e nenhum teste deste módulo exercita
    esta função (sempre injetam um ``transcription_backend`` fake)."""

    from faster_whisper import WhisperModel  # import local -- ver seção 0.7

    model = WhisperModel(model_size, device="cpu", compute_type="int8", download_root=download_root)
    raw_segments, info = model.transcribe(
        str(local_path),
        task="transcribe",
        language=language,
        vad_filter=True,
        beam_size=1,
        condition_on_previous_text=False,
    )
    segments = tuple(
        TranscriptionSegment(start=float(seg.start), end=float(seg.end), text=str(seg.text or ""))
        for seg in raw_segments
    )
    return TranscriptionResult(segments=segments, language=getattr(info, "language", None))


# ---------------------------------------------------------------------
# CaptionsEngine
# ---------------------------------------------------------------------

class CaptionsEngine:
    """Parte A (decisão de modo) + Parte B (transcrição real) -- ver
    seção 0 da docstring do módulo para o contrato completo."""

    OPERATION: ClassVar[str] = OPERATION_TRANSCRIBE_CAPTIONS

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        storage_manager: StorageManager,
        app_paths: AppPaths,
        audit_log: "OperationalAuditLog | None" = None,
        transcription_backend: "TranscriptionBackend | None" = None,
        control_manager: "ControlManager | None" = None,
        default_model_size: str = DEFAULT_MODEL_SIZE,
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
        if transcription_backend is not None and not callable(transcription_backend):
            raise TypeError("transcription_backend deve ser chamável")
        if control_manager is not None and not isinstance(control_manager, ControlManager):
            raise TypeError(
                f"control_manager deve ser ControlManager (recebido: {type(control_manager)!r})"
            )
        model_size = str(default_model_size).strip()
        if not model_size:
            raise ValueError("default_model_size não pode ser vazio")

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._backend: TranscriptionBackend = transcription_backend or self._transcribe_with_default_backend
        self._control_manager = control_manager
        self._default_model_size = model_size

    def _transcribe_with_default_backend(
        self, local_path: str, model_size: str, language: "str | None"
    ) -> TranscriptionResult:
        return _default_transcribe(
            local_path, model_size, language, download_root=str(self._app_paths.models)
        )

    # ===================================================================
    # PARTE A -- decisão de modo
    # ===================================================================

    def set_mode(self, project_id: str, mode: Any) -> CaptionsState:
        if not isinstance(mode, str) or mode not in CAPTION_MODES:
            raise ModoInvalidoError(
                f"mode deve ser um de {sorted(CAPTION_MODES)} (recebido: {mode!r})"
            )

        def mutator(current_data: Any) -> dict:
            merged = dict(current_data) if isinstance(current_data, Mapping) else {}
            merged["mode"] = mode
            return merged

        try:
            self._manager.update_category(project_id, CAPTIONS, mutator)
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            raise ModoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        return self.get_mode(project_id)

    def get_mode(self, project_id: str) -> CaptionsState:
        try:
            state = self._manager.get_category(project_id, CAPTIONS)
        except ValueError as exc:
            raise ModoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        if state is None:
            return CaptionsState()
        data = state.data if isinstance(state.data, Mapping) else {}
        mode = data.get("mode")
        if mode is not None and (not isinstance(mode, str) or mode not in CAPTION_MODES):
            raise ModoInvalidoError(f"mode persistido é inválido (recebido: {mode!r})")
        return CaptionsState(mode=mode)

    # ===================================================================
    # PARTE B -- handler de Job (transcrição real)
    # ===================================================================

    def handle_transcription_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})

        video = self._database.get(Video, job.video_id)
        if video is None or video.source_asset_id is None:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "video_or_source_asset_id_missing", "video_id": job.video_id},
            )

        source = self._database.get(SourceAsset, video.source_asset_id)
        if source is None or not source.local_path:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "source_asset_missing_or_no_local_path"},
            )
        if not source.fingerprint:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "source_asset_missing_fingerprint"}
            )

        local_path = str(source.local_path)
        if not os.path.isfile(local_path):
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "source_file_missing_on_disk", "local_path": local_path},
            )

        model_size = self._resolve_model_size(job)
        language = self._resolve_language(job)
        cache_key = compute_cache_key(source.fingerprint, model_size, language)

        if self._cancel_requested(job.id):
            return JobStepResult(
                target_status=JOB_CANCELLED, data={"reason": "cancelled_before_transcription"}
            )

        cached_artifacts = self._find_cached_artifacts(job.video_id, cache_key)
        if cached_artifacts is not None:
            self._audit_log.record_checkpoint(
                job.id, CHECKPOINT_TRANSCRIBED, data={"cache_hit": True, "cache_key": cache_key}
            )
            return JobStepResult(
                target_status=JOB_READY,
                semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
                data={
                    "cache_hit": True,
                    "cache_key": cache_key,
                    "reused_artifact_ids": [a.id for a in cached_artifacts],
                },
            )

        # Trabalho pesado real -- exceções daqui propagam sem serem
        # capturadas (ver seção 0.6): não é uma falha conhecida deste
        # handler, o JobEngine já pousa o Job em FAILED sozinho.
        result = self._backend(local_path, model_size, language)

        if self._cancel_requested(job.id):
            return JobStepResult(
                target_status=JOB_CANCELLED, data={"reason": "cancelled_after_transcription"}
            )

        try:
            segments = _validate_and_normalize_segments(result.segments)
        except _InvalidTranscriptionOutput as exc:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "invalid_transcription_output", "detail": str(exc)},
            )

        artifacts = self._write_and_register_artifacts(
            job, cache_key, segments, result.language
        )

        self._audit_log.record_checkpoint(
            job.id, CHECKPOINT_TRANSCRIBED, data={"cache_hit": False, "cache_key": cache_key}
        )
        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={
                "cache_hit": False,
                "cache_key": cache_key,
                "artifact_ids": [a.id for a in artifacts],
            },
        )

    # -- helpers internos de Parte B ------------------------------------

    def _resolve_model_size(self, job: Job) -> str:
        extra = job.extra
        if isinstance(extra, dict):
            value = extra.get("whisper_model_size")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return self._default_model_size

    def _resolve_language(self, job: Job) -> "str | None":
        extra = job.extra
        if isinstance(extra, dict):
            value = extra.get("language")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _cancel_requested(self, job_id: str) -> bool:
        if self._control_manager is None:
            return False
        return self._control_manager.is_job_cancel_requested(job_id)

    def _find_cached_artifacts(
        self, video_id: str, cache_key: str
    ) -> "tuple[Artifact, ...] | None":
        found: "dict[str, Artifact]" = {}
        with self._database.connection() as conn:
            for kind in ARTIFACT_KINDS:
                row = conn.execute(
                    "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                    "ORDER BY created_at LIMIT 1",
                    (video_id, kind, cache_key),
                ).fetchone()
                if row is None:
                    return None
                found[kind] = row[0]
        artifacts = tuple(
            artifact
            for artifact in (self._database.get(Artifact, found[kind]) for kind in ARTIFACT_KINDS)
            if artifact is not None
        )
        if len(artifacts) != len(ARTIFACT_KINDS):
            # Defensivo: uma linha existia no SELECT mas sumiu antes do
            # GET (não esperado em operação normal -- nunca tratado como
            # cache válido pela metade).
            return None
        return artifacts

    def _final_path_for(self, video_id: str, cache_key: str, extension: str) -> Path:
        return Path(self._app_paths.projects) / "captions" / video_id / f"{cache_key}{extension}"

    def _write_and_register_artifacts(
        self,
        job: Job,
        cache_key: str,
        segments: "tuple[TranscriptionSegment, ...]",
        language: "str | None",
    ) -> "tuple[Artifact, ...]":
        contents = {
            ARTIFACT_KIND_TRANSCRIPT: (".json", _segments_to_internal_json(segments, language)),
            ARTIFACT_KIND_SRT: (".srt", _segments_to_srt(segments)),
            ARTIFACT_KIND_VTT: (".vtt", _segments_to_vtt(segments)),
        }

        temp_paths: "dict[str, Path]" = {}
        try:
            for kind, (extension, content) in contents.items():
                temp_path = self._storage.allocate_temp(suffix=f"_captions{extension}", create=True)
                temp_path.write_text(content, encoding="utf-8")
                temp_paths[kind] = temp_path
        except BaseException:
            for temp_path in temp_paths.values():
                temp_path.unlink(missing_ok=True)
            raise

        final_paths: "dict[str, Path]" = {}
        try:
            for kind, temp_path in temp_paths.items():
                extension = contents[kind][0]
                final_path = self._final_path_for(job.video_id, cache_key, extension)
                promoted = self._storage.promote_to_final(temp_path, final_path, overwrite=True)
                final_paths[kind] = promoted
        except BaseException:
            for kind, temp_path in temp_paths.items():
                if kind not in final_paths:
                    temp_path.unlink(missing_ok=True)
            raise

        artifacts: "list[Artifact]" = []
        with self._database.transaction() as conn:
            for kind, final_path in final_paths.items():
                content = contents[kind][1]
                artifact = Artifact(
                    video_id=job.video_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    kind=kind,
                    path=str(final_path),
                    fingerprint=cache_key,
                    size_bytes=len(content.encode("utf-8")),
                )
                self._database.insert(artifact, connection=conn)
                artifacts.append(artifact)
        return tuple(artifacts)


__all__ = [
    "MODE_OFF",
    "MODE_SEPARATE",
    "MODE_BURNED",
    "CAPTION_MODES",
    "OPERATION_TRANSCRIBE_CAPTIONS",
    "ARTIFACT_KIND_TRANSCRIPT",
    "ARTIFACT_KIND_SRT",
    "ARTIFACT_KIND_VTT",
    "ARTIFACT_KINDS",
    "DEFAULT_MODEL_SIZE",
    "CaptionsEngineError",
    "ModoInvalidoError",
    "CaptionsState",
    "TranscriptionSegment",
    "TranscriptionResult",
    "TranscriptionBackend",
    "compute_cache_key",
    "CaptionsEngine",
]
