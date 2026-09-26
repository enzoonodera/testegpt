# -*- coding: utf-8 -*-
"""FinalMediaValidator -- Prompt 43 (Fase 5: Editor Modular Não Destrutivo).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie FinalMediaValidator.

    Antes de marcar READY verificar:
    arquivo existe
    duration válida
    resolução
    codec
    áudio
    tamanho > mínimo
    sem output truncado
    captions dentro do canvas quando verificável
    texto dentro das zonas
    artifact íntegro

    Tentar correção automática somente quando segura.
    Caso contrário: USER_ACTION_REQUIRED ou FAILED apropriado.

===========================================================================
0. VISÃO GERAL -- O PORTÃO DE QUALIDADE ANTES DE READY
===========================================================================

``FinalMediaValidator`` é o SEGUNDO produtor de evidência sobre um
``render_output`` (o primeiro é ``RenderEngine``, Prompt 42) e o PRIMEIRO
produtor real de ``CHECKPOINT_VALIDATED``/``BADGE_VALIDATED`` -- ambos
existiam desde antes (``domain/checkpoints.py``/``media_catalog.py``),
deliberadamente reservados e nunca gravados por nenhum módulo até agora
(``metadata_manager.py`` e ``render_engine.py`` documentam explicitamente
essa reserva). Segue a MESMA estrutura de handler de ``JobEngine`` já
usada por ``RenderEngine``/``AutoReframeEngine``/``CaptionsEngine``:
registra um handler (``FinalMediaValidator.handle_validate_job``) para a
``operation`` ``VALIDATE_MEDIA`` (``claims_status=JOB_PROCESSING`` --
trabalho local, sem efeito remoto observável).

NUNCA reprocessa/reenquadra/reencoda mídia -- só LÊ o que já existe
(``Artifact`` ``render_output`` do Prompt 42, ``SourceAsset`` original
para o cross-check de áudio, ``Template``/``CaptionsStyleState`` para os
dois itens condicionais do checklist) e, no máximo, corrige uma lacuna de
METADADO no próprio banco (seção 0.4) -- nunca toca no arquivo de mídia
em si. "Correção automática" aqui NUNCA significa "re-render": essa
possibilidade é explicitamente vetada pelo texto do Prompt.

===========================================================================
0.1 -- DEPENDÊNCIAS -- DELIBERADAMENTE MAIS LEVES QUE RenderEngine
===========================================================================

``FinalMediaValidator`` não escreve nenhum arquivo de mídia -- só lê
arquivos já existentes (via ``Path``/``os.stat``) e, no máximo, atualiza
um campo de metadado de uma linha ``Artifact`` já existente via
``LocalDatabase.save`` (nunca via ``StorageManager.allocate_temp``/
``promote_to_final``, que são para ESCREVER conteúdo novo). Por isso,
diferente de ``RenderEngine``, este módulo **não recebe nem usa
``StorageManager``/``AppPaths``** -- dependência deliberadamente omitida,
não esquecida (confirmado por teste estrutural/AST). Recebe
``TemplateEngine`` já construído (mesmo padrão de injeção de
``RenderEngine``) apenas para resolver o ``Template`` selecionado, quando
houver, e ler a geometria da zona ``CAPTION``.

``CAPTIONS``/``METADATA_MODE`` são lidos por leitura DIRETA de categoria
(``EditProjectManager.get_category``) em vez de construir um
``CaptionsEngine`` completo só para usar seu getter Parte A -- evita
puxar a dependência pesada de ``CaptionsEngine`` (que exige
``StorageManager``/``AppPaths`` no construtor) só para ler
``{"mode": ...}``, um dado que a própria categoria já expõe sem
indireção. ``CaptionsStyleEngine``/``TemplateSelector`` são construídos
internamente (ambos leves -- só precisam de ``EditProjectManager``/
``TemplateEngine``, nenhum I/O pesado no construtor), mesmo espírito do
``RenderEngine``.

===========================================================================
0.2 -- LOCALIZAÇÃO DO ARTIFACT A VALIDAR
===========================================================================

``RenderEngine`` escopa cache por ``project_id`` (um mesmo vídeo pode ter
vários Projects, cada um com seu próprio ``render_output``). Este módulo
segue a MESMA convenção: localiza o ``Artifact`` ``kind="render_output"``
MAIS RECENTE (``created_at`` DESC) para o ``(video_id, project_id)`` do
Job -- mesma convenção "o mais recente é o atual" já usada por
``render_engine.py`` para localizar o Artifact de legendas mais recente
quando ``CAPTIONS.mode == BURNED`` (seção 0.3 daquele módulo). DECISÃO
DELIBERADA: não reimporta/reexecuta o cálculo de ``cache_key`` de
``RenderEngine`` (evitaria acoplamento desnecessário a detalhes internos
de outro módulo, e o Prompt marca mudanças em ``render_engine.py`` como
fora de escopo -- importar suas funções puras não seria uma mudança, mas
a convenção "mais recente" já é suficiente e mais simples).

===========================================================================
0.3 -- OS 10 ITENS DO CHECKLIST -- MAPEAMENTO PARA DADOS REAIS
===========================================================================

Cada item é uma função pura de módulo (``_check_*``), testável
isoladamente com um ``MediaProbeResult`` fabricado à mão -- mesmo
espírito dos ``build_*_command`` de ``render_engine.py``. Cada uma
devolve um ``ChecklistItemResult`` com um de 4 status:
``CHECK_PASS``/``CHECK_FAIL``/``CHECK_SKIP``/``CHECK_USER_ACTION_REQUIRED``
(mais ``CHECK_CORRECTED``, só para o item 10 -- seção 0.4).

1.  **arquivo existe** -- ``Path(artifact.path).is_file()``. Ausência é
    FAIL incondicional (nunca ambíguo).
2.  **duration válida** -- ``probe_deep(...).duration`` presente e > 0.
3.  **resolução** -- presente (``width``/``height`` > 0, já garantido por
    ``probe()`` quando ``valid=True`` -- reafirmado aqui explicitamente
    como item do checklist) E, quando um Template está selecionado e sua
    ``extra["width"]``/``extra["height"]`` são conhecidas, a resolução do
    render deve bater EXATAMENTE com o canvas do template (a etapa de
    composição de ``render_engine.py`` sempre produz exatamente essas
    dimensões -- um desvio é um bug estrutural do pipeline, nunca uma
    escolha do usuário, portanto FAIL, nunca USER_ACTION_REQUIRED). Sem
    template selecionado, não há "resolução esperada" para comparar --
    só a presença é verificada.
4.  **codec** -- ``probe_deep(...).codec in ALLOWED_VIDEO_CODECS``
    (``{"h264"}`` -- ``render_engine.py`` sempre codifica com
    ``-c:v libx264``/ffprobe reporta ``codec_name="h264"`` para esse
    encoder, não outro valor; um valor diferente indica um pipeline fora
    de controle, nunca uma decisão do usuário -- FAIL).
5.  **áudio** -- cross-check contra o ``SourceAsset`` original (probe
    RASO, ``probe()``, nunca ``probe_deep()`` -- só precisamos de
    ``has_audio``, não de decodificar o original de novo): se o original
    tem áudio e o render não tem, é uma perda estrutural real (nenhuma
    etapa de ``render_engine.py`` remove o STREAM de áudio -- ``mute``
    silencia via filtro ``volume=0``, o stream continua existindo) --
    FAIL. Se o original não é probável (removido/movido) ou a checagem
    rasa falha, o item é SKIP (não fabrica um resultado sem dado real).
    Demais combinações (original sem áudio, com ou sem áudio no render)
    são PASS -- nunca um problema.
6.  **tamanho > mínimo** -- ``file_size > MIN_OUTPUT_SIZE_BYTES`` (seção
    0.5 para a justificativa do valor escolhido).
7.  **sem output truncado** -- usa ``probe_deep()`` (decodifica o
    arquivo INTEIRO via ``ffmpeg -f null -``), nunca ``probe()`` raso
    (exigência explícita já confirmada nesta investigação: ``probe()``
    só lê metadados do container e não detecta um arquivo truncado cujo
    container ainda feche de forma válida). PASS quando
    ``result.valid and result.deep_checked``.
8.  **captions dentro do canvas quando verificável** -- só avaliável
    quando TODAS as condições abaixo são verdadeiras (caso contrário,
    SKIP, nunca fabrica um resultado): ``CAPTIONS.mode == BURNED``; um
    Template está selecionado; esse Template tem uma zona ``CAPTION``;
    ``CaptionsStyleState.position`` foi explicitamente configurado (não
    ``None`` -- o padrão do sistema nunca é avaliado contra uma zona
    específica, só uma escolha explícita do usuário). Quando todas
    presentes: o ponto de ancoragem (``position.x``/``position.y``,
    já validado em ``[0,1]`` por ``captions_style.py``) precisa cair
    DENTRO do retângulo da zona ``CAPTION`` (coordenadas normalizadas do
    template). Dentro: PASS. Fora: USER_ACTION_REQUIRED -- o render é
    tecnicamente válido (a legenda foi queimada exatamente onde
    configurada), mas o resultado visual provavelmente não é o
    pretendido -- decisão do usuário (ajustar a posição ou aceitar),
    nunca um FAIL técnico nem uma correção automática (mexer na posição
    da legenda é uma decisão de conteúdo, não uma correção se15gura).
9.  **texto dentro das zonas** -- ``TEXT_LAYERS`` confirmado SEM NENHUM
    produtor real até hoje (mesmo achado documentado em
    ``render_engine.py`` seção 0.3). SEMPRE ``SKIP`` -- mesmo quando a
    categoria não está vazia, não existe um schema validado para
    interpretar "zona" de um texto dinâmico ainda, e fabricar uma
    validação sem um produtor real violaria a exigência do Prompt
    ("nunca fabricando dado que nenhum módulo produz"). Documentado, não
    escondido.
10. **artifact íntegro** -- ver seção 0.4 (é o único item com um caminho
    de correção automática segura).

===========================================================================
0.4 -- ITEM 10 (ARTIFACT ÍNTEGRO) E A ÚNICA CORREÇÃO AUTOMÁTICA SEGURA
===========================================================================

``Artifact.size_bytes`` é preenchido por ``RenderEngine`` no momento da
promoção (``validation.file_size`` do ``probe_deep`` que RODOU antes de
promover -- Prompt 42, seção 0.5). Comparando esse valor gravado com o
tamanho REAL do arquivo agora (``path.stat().st_size``), há 3 casos:

- **Iguais**: ``CHECK_PASS`` -- o arquivo não mudou desde a promoção.
- **``Artifact.size_bytes is None``**: um Artifact mais antigo (de antes
  deste campo existir, ou qualquer gravação que por algum motivo não o
  preencheu) não tem essa evidência -- MAS todos os outros 9 itens deste
  MESMO checklist, incluindo o item 7 (decode profundo completo, item
  1 (existência) e item 6 (tamanho mínimo), já comprovam que o arquivo
  ATUAL é válido e íntegro. Backfillar ``size_bytes`` com o tamanho real
  agora é seguro porque não reescreve nem reinterpreta o CONTEÚDO do
  arquivo -- só preenche uma lacuna de metadado usando um dado que a
  PRÓPRIA validação já mediu de forma confiável nesta mesma execução.
  ``CHECK_CORRECTED`` -- grava ``database.save(artifact)`` com o novo
  ``size_bytes`` e conta como sucesso para fins de ``CHECKPOINT_VALIDATED``
  (mas é reportado separadamente em ``data["corrected_items"]``, nunca
  escondido dentro de um "PASS" silencioso).
- **Diferentes (ambos não-``None``)**: o arquivo mudou depois da
  promoção -- pode ser corrupção, uma cópia externa incompleta, ou
  qualquer outra interferência fora do controle deste produto.
  ``CHECK_FAIL`` -- NUNCA corrigido automaticamente (reescrever o
  metadado para "concordar" com um arquivo que mudou por um motivo
  desconhecido esconderia um problema real, o oposto do propósito deste
  módulo) e NUNCA re-renderizado (vetado pelo texto do Prompt). O
  caminho de recuperação é o normal de ``JOB_FAILED`` -> ``JOB_RETRY``
  -> um novo render (fora do escopo deste módulo).

===========================================================================
0.5 -- TAMANHO MÍNIMO -- VALOR ESCOLHIDO E JUSTIFICATIVA
===========================================================================

``MIN_OUTPUT_SIZE_BYTES = 2048`` (2 KiB). Não é um piso de "qualidade"
(o Prompt não fornece nenhuma tabela de bitrate/resolução/duração-alvo
para derivar um número por combinação, e inventar uma tabela de política
sem essa evidência seria uma decisão de produto que este Prompt não
autoriza) -- é um piso ESTRUTURAL contra arquivos vazios/quase-vazios
(um MP4 sintaticamente válido mas sem conteúdo real de frame/áudio, ex.
um container com só ``ftyp``/``moov`` mínimos, tipicamente fica na casa
de poucas centenas de bytes). 2 KiB é folgado o bastante para nunca
gerar falso positivo contra os menores clipes sintéticos realistas deste
projeto (vídeos de teste de 1s em 64x64 já produzem alguns KB mesmo em
``libx264``/``preset veryfast``) e apertado o bastante para pegar
qualquer coisa próxima de "arquivo vazio" que o item 1 (existência) e o
item 7 (decode completo) porventura não peguem sozinhos (defesa em
profundidade -- ``RenderEngine`` já valida antes de promover, então este
item cobre o caso de o arquivo ter sido corrompido/truncado DEPOIS da
promoção, sem nenhuma outra evidência). Exposto como parâmetro do
construtor (``min_output_size_bytes``) para calibração futura sem exigir
mudança de código -- não uma constante de módulo hardcoded e inacessível.

===========================================================================
0.6 -- MATRIZ DE DECISÃO FINAL
===========================================================================

- Qualquer item ``CHECK_FAIL`` presente -> ``JOB_FAILED`` (falha técnica
  inequívoca -- nenhuma checagem FAIL definida acima é ambígua; todas
  são fatos verificáveis sobre o arquivo, nunca uma escolha de conteúdo
  do usuário). Nenhum ``CHECKPOINT_VALIDATED`` é gravado.
- Nenhum FAIL, mas algum item ``CHECK_USER_ACTION_REQUIRED`` presente ->
  ``JOB_USER_ACTION_REQUIRED`` (hoje, só o item 8 pode produzir este
  status -- um render tecnicamente correto cujo resultado visual
  provavelmente não é o pretendido). Nenhum ``CHECKPOINT_VALIDATED`` é
  gravado.
- Nenhum FAIL e nenhum USER_ACTION_REQUIRED (todo item é PASS, SKIP ou
  CORRECTED) -> sucesso total: grava ``CHECKPOINT_VALIDATED`` e devolve
  ``JOB_READY``.

===========================================================================
0.7 -- BADGE_VALIDATED / BADGE_READY (media_catalog.py)
===========================================================================

``BADGE_VALIDATED`` ganha a MESMA arquitetura de dupla evidência de
``BADGE_RENDERED`` (Prompt 42)/``BADGE_REFRAMED_9_16``/``BADGE_TRANSCRIBED``:
checkpoint ``VALIDATED`` gravado para QUALQUER Job deste vídeo E o
``Artifact`` ``render_output`` (o MESMO artifact -- não um novo ``kind``
inventado; ``FinalMediaValidator`` não produz nenhum artifact próprio,
só valida e opcionalmente corrige o existente, seção 0.4) com arquivo
real no disco. Reaproveitar ``kind="render_output"`` em vez de inventar
um novo tipo de Artifact evita um vocabulário duplicado sem necessidade
(nenhuma abstração nova sem benefício concreto -- CLAUDE.md,
"REVISÃO CRÍTICA"). ``BADGE_READY`` é a conjunção EXATA pedida pelo
Prompt: ``(expressão de RENDERED) AND (expressão de VALIDATED)``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping

from .domain import (
    Artifact,
    CHECKPOINT_VALIDATED,
    Job,
    JOB_FAILED,
    JOB_READY,
    JOB_USER_ACTION_REQUIRED,
    SourceAsset,
    Template,
    Video,
)
from .edit_project import CAPTIONS, EditProjectManager, TEXT_LAYERS
from .job_engine import JobStepResult
from .media_probe import MediaProbe, MediaProbeResult
from .captions_engine import MODE_BURNED
from .captions_style import CaptionsStyleEngine, CaptionsStyleState
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .template_engine import TemplateEngine, TemplateNaoEncontradoError, ZONE_TYPE_CAPTION
from .template_selector import TemplateSelector

__all__ = [
    "OPERATION_VALIDATE_MEDIA",
    "ARTIFACT_KIND_RENDER_OUTPUT",
    "ALLOWED_VIDEO_CODECS",
    "MIN_OUTPUT_SIZE_BYTES_PADRAO",
    "CHECK_PASS",
    "CHECK_FAIL",
    "CHECK_SKIP",
    "CHECK_USER_ACTION_REQUIRED",
    "CHECK_CORRECTED",
    "CHECKLIST_ITEM_NAMES",
    "ChecklistItemResult",
    "FinalMediaValidatorError",
    "check_file_exists",
    "check_duration_valid",
    "check_resolution_valid",
    "check_codec_valid",
    "check_audio_present",
    "check_size_above_minimum",
    "check_not_truncated",
    "check_captions_within_canvas",
    "check_text_within_zones",
    "check_artifact_integrity",
    "FinalMediaValidator",
]

OPERATION_VALIDATE_MEDIA = "VALIDATE_MEDIA"

# Mesmo kind já registrado por render_engine.py (Prompt 42) -- nunca
# reinventado aqui (ver seção 0.7 da docstring do módulo).
ARTIFACT_KIND_RENDER_OUTPUT = "render_output"

# render_engine.py sempre codifica com "-c:v libx264" -- ver seção 0.3
# item 4 da docstring do módulo.
ALLOWED_VIDEO_CODECS = frozenset({"h264"})

# Ver seção 0.5 da docstring do módulo para a justificativa completa.
MIN_OUTPUT_SIZE_BYTES_PADRAO = 2048

CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_SKIP = "SKIP"
CHECK_USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
CHECK_CORRECTED = "CORRECTED"

_CHECK_STATUSES = frozenset(
    {CHECK_PASS, CHECK_FAIL, CHECK_SKIP, CHECK_USER_ACTION_REQUIRED, CHECK_CORRECTED}
)

# Ordem EXATA do checklist literal do Prompt -- usada para nomear cada
# item nos resultados (``ChecklistItemResult.name``) e na ordem em que
# aparecem em ``data["checklist"]``.
CHECK_FILE_EXISTS = "file_exists"
CHECK_DURATION_VALID = "duration_valid"
CHECK_RESOLUTION_VALID = "resolution_valid"
CHECK_CODEC_VALID = "codec_valid"
CHECK_AUDIO_PRESENT = "audio_present"
CHECK_SIZE_ABOVE_MINIMUM = "size_above_minimum"
CHECK_NOT_TRUNCATED = "not_truncated"
CHECK_CAPTIONS_WITHIN_CANVAS = "captions_within_canvas"
CHECK_TEXT_WITHIN_ZONES = "text_within_zones"
CHECK_ARTIFACT_INTEGRITY = "artifact_integrity"

CHECKLIST_ITEM_NAMES = (
    CHECK_FILE_EXISTS,
    CHECK_DURATION_VALID,
    CHECK_RESOLUTION_VALID,
    CHECK_CODEC_VALID,
    CHECK_AUDIO_PRESENT,
    CHECK_SIZE_ABOVE_MINIMUM,
    CHECK_NOT_TRUNCATED,
    CHECK_CAPTIONS_WITHIN_CANVAS,
    CHECK_TEXT_WITHIN_ZONES,
    CHECK_ARTIFACT_INTEGRITY,
)


class FinalMediaValidatorError(RuntimeError):
    """Classe base de todos os erros deste módulo."""

    code: ClassVar[str] = "FINAL_MEDIA_VALIDATOR_ERRO"


@dataclass(frozen=True)
class ChecklistItemResult:
    """Resultado de UM item do checklist -- ``status`` é sempre um de
    ``_CHECK_STATUSES``, ``detail`` é uma string curta e não-sensível
    (nunca ``str(exc)``/traceback -- GATE item 10) explicando o motivo."""

    name: str
    status: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.name not in CHECKLIST_ITEM_NAMES:
            raise ValueError(f"nome de item de checklist desconhecido: {self.name!r}")
        if self.status not in _CHECK_STATUSES:
            raise ValueError(f"status de checklist desconhecido: {self.status!r}")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


# ---------------------------------------------------------------------
# Itens do checklist -- funções puras de módulo, testáveis isoladamente
# com um MediaProbeResult fabricado à mão (nunca precisam de FFmpeg real
# para testar a LÓGICA de decisão -- mesmo espírito dos build_*_command
# de render_engine.py).
# ---------------------------------------------------------------------


def check_file_exists(artifact_path: str) -> ChecklistItemResult:
    if artifact_path and os.path.isfile(artifact_path):
        return ChecklistItemResult(CHECK_FILE_EXISTS, CHECK_PASS)
    return ChecklistItemResult(CHECK_FILE_EXISTS, CHECK_FAIL, "arquivo do artifact nao existe no disco")


def check_duration_valid(result: MediaProbeResult) -> ChecklistItemResult:
    if result.duration is not None and result.duration > 0:
        return ChecklistItemResult(CHECK_DURATION_VALID, CHECK_PASS)
    return ChecklistItemResult(CHECK_DURATION_VALID, CHECK_FAIL, f"duracao invalida: {result.duration!r}")


def check_resolution_valid(
    result: MediaProbeResult, *, expected_width: "int | None" = None, expected_height: "int | None" = None
) -> ChecklistItemResult:
    if not result.width or not result.height or result.width <= 0 or result.height <= 0:
        return ChecklistItemResult(
            CHECK_RESOLUTION_VALID, CHECK_FAIL, f"resolucao invalida: {result.width}x{result.height}"
        )
    if expected_width is not None and expected_height is not None:
        if result.width != expected_width or result.height != expected_height:
            return ChecklistItemResult(
                CHECK_RESOLUTION_VALID, CHECK_FAIL,
                f"resolucao {result.width}x{result.height} nao bate com o canvas do template "
                f"({expected_width}x{expected_height})",
            )
    return ChecklistItemResult(CHECK_RESOLUTION_VALID, CHECK_PASS)


def check_codec_valid(result: MediaProbeResult) -> ChecklistItemResult:
    if result.codec in ALLOWED_VIDEO_CODECS:
        return ChecklistItemResult(CHECK_CODEC_VALID, CHECK_PASS)
    return ChecklistItemResult(CHECK_CODEC_VALID, CHECK_FAIL, f"codec inesperado: {result.codec!r}")


def check_audio_present(
    result: MediaProbeResult, *, source_probe: "MediaProbeResult | None"
) -> ChecklistItemResult:
    if source_probe is None or not source_probe.valid:
        return ChecklistItemResult(
            CHECK_AUDIO_PRESENT, CHECK_SKIP, "source original indisponivel para comparar presenca de audio"
        )
    if source_probe.has_audio and not result.has_audio:
        return ChecklistItemResult(
            CHECK_AUDIO_PRESENT, CHECK_FAIL, "source original tem audio, mas o render nao tem"
        )
    return ChecklistItemResult(CHECK_AUDIO_PRESENT, CHECK_PASS)


def check_size_above_minimum(result: MediaProbeResult, *, min_output_size_bytes: int) -> ChecklistItemResult:
    if result.file_size is not None and result.file_size > min_output_size_bytes:
        return ChecklistItemResult(CHECK_SIZE_ABOVE_MINIMUM, CHECK_PASS)
    return ChecklistItemResult(
        CHECK_SIZE_ABOVE_MINIMUM, CHECK_FAIL,
        f"tamanho {result.file_size!r} bytes nao ultrapassa o minimo de {min_output_size_bytes} bytes",
    )


def check_not_truncated(result: MediaProbeResult) -> ChecklistItemResult:
    if result.valid and result.deep_checked:
        return ChecklistItemResult(CHECK_NOT_TRUNCATED, CHECK_PASS)
    return ChecklistItemResult(
        CHECK_NOT_TRUNCATED, CHECK_FAIL,
        f"decode profundo nao confirmou integridade: valid={result.valid} error_code={result.error_message!r}",
    )


def check_captions_within_canvas(
    *,
    captions_mode: "str | None",
    caption_zone: "Mapping[str, float] | None",
    style: "CaptionsStyleState | None",
) -> ChecklistItemResult:
    if captions_mode != MODE_BURNED:
        return ChecklistItemResult(CHECK_CAPTIONS_WITHIN_CANVAS, CHECK_SKIP, "captions nao estao sendo queimadas")
    if caption_zone is None:
        return ChecklistItemResult(
            CHECK_CAPTIONS_WITHIN_CANVAS, CHECK_SKIP, "sem template selecionado ou sem zona CAPTION definida"
        )
    if style is None or style.position is None:
        return ChecklistItemResult(
            CHECK_CAPTIONS_WITHIN_CANVAS, CHECK_SKIP, "nenhuma posicao explicita configurada para a legenda"
        )
    x = style.position.get("x")
    y = style.position.get("y")
    if x is None or y is None:
        return ChecklistItemResult(CHECK_CAPTIONS_WITHIN_CANVAS, CHECK_SKIP, "posicao da legenda incompleta")
    zone_x, zone_y = caption_zone["x"], caption_zone["y"]
    zone_w, zone_h = caption_zone["width"], caption_zone["height"]
    if zone_x <= x <= zone_x + zone_w and zone_y <= y <= zone_y + zone_h:
        return ChecklistItemResult(CHECK_CAPTIONS_WITHIN_CANVAS, CHECK_PASS)
    return ChecklistItemResult(
        CHECK_CAPTIONS_WITHIN_CANVAS, CHECK_USER_ACTION_REQUIRED,
        f"ancora da legenda ({x:.3f}, {y:.3f}) fora da zona CAPTION do template",
    )


def check_text_within_zones(*, text_layers_data: Any) -> ChecklistItemResult:
    # TEXT_LAYERS confirmado sem nenhum produtor real hoje -- SEMPRE
    # SKIP, mesmo quando a categoria não está vazia (sem schema validado
    # para interpretar, nunca fabricado -- ver seção 0.3 item 9).
    return ChecklistItemResult(
        CHECK_TEXT_WITHIN_ZONES, CHECK_SKIP, "TEXT_LAYERS sem produtor real hoje -- nada a verificar"
    )


def check_artifact_integrity(*, recorded_size_bytes: "int | None", real_size_bytes: int) -> ChecklistItemResult:
    if recorded_size_bytes is None:
        return ChecklistItemResult(
            CHECK_ARTIFACT_INTEGRITY, CHECK_CORRECTED,
            f"size_bytes nunca foi gravado -- corrigido para {real_size_bytes} (arquivo ja validado nesta execucao)",
        )
    if recorded_size_bytes == real_size_bytes:
        return ChecklistItemResult(CHECK_ARTIFACT_INTEGRITY, CHECK_PASS)
    return ChecklistItemResult(
        CHECK_ARTIFACT_INTEGRITY, CHECK_FAIL,
        f"tamanho gravado ({recorded_size_bytes}) diverge do tamanho real ({real_size_bytes})",
    )


# ---------------------------------------------------------------------
# FinalMediaValidator
# ---------------------------------------------------------------------


class FinalMediaValidator:
    """Job handler de validação de qualidade final -- ver docstring do
    módulo para o contrato completo (checklist de 10 itens, matriz de
    decisão, correção automática segura)."""

    OPERATION: "ClassVar[str]" = OPERATION_VALIDATE_MEDIA

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        template_engine: TemplateEngine,
        audit_log: "OperationalAuditLog | None" = None,
        media_probe: "MediaProbe | None" = None,
        min_output_size_bytes: int = MIN_OUTPUT_SIZE_BYTES_PADRAO,
    ) -> None:
        if not isinstance(manager, EditProjectManager):
            raise TypeError(f"manager deve ser EditProjectManager (recebido: {type(manager)!r})")
        if not isinstance(database, LocalDatabase):
            raise TypeError(f"database deve ser LocalDatabase (recebido: {type(database)!r})")
        if not isinstance(template_engine, TemplateEngine):
            raise TypeError(
                f"template_engine deve ser TemplateEngine (recebido: {type(template_engine)!r})"
            )
        if media_probe is not None and not isinstance(media_probe, MediaProbe):
            raise TypeError(f"media_probe deve ser MediaProbe (recebido: {type(media_probe)!r})")
        if not isinstance(min_output_size_bytes, int) or isinstance(min_output_size_bytes, bool):
            raise TypeError("min_output_size_bytes deve ser int")
        if min_output_size_bytes < 0:
            raise ValueError("min_output_size_bytes deve ser >= 0")

        self._manager = manager
        self._database = database
        self._template_engine = template_engine
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._probe = media_probe if media_probe is not None else MediaProbe()
        self._min_output_size_bytes = min_output_size_bytes

        self._template_selector = TemplateSelector(manager, template_engine)
        self._captions_style_engine = CaptionsStyleEngine(manager)

    # -- localização do artifact a validar (ver seção 0.2) --------------

    def _find_latest_render_artifact(self, video_id: str, project_id: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND project_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (video_id, project_id, ARTIFACT_KIND_RENDER_OUTPUT),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    # -- resolução de contexto para os itens condicionais (seção 0.3) ---

    def _resolve_caption_zone(self, project_id: str) -> "dict[str, float] | None":
        try:
            template_id = self._template_selector.get_template_selection(project_id)
        except Exception:
            return None
        if template_id is None:
            return None
        try:
            template = self._template_engine.get_template(template_id)
        except TemplateNaoEncontradoError:
            return None
        zones = template.layout.get("zones", []) if isinstance(template.layout, Mapping) else []
        for zone in zones:
            if zone.get("zone_type") == ZONE_TYPE_CAPTION:
                return {
                    "x": float(zone["x"]), "y": float(zone["y"]),
                    "width": float(zone["width"]), "height": float(zone["height"]),
                }
        return None

    def _resolve_expected_resolution(self, project_id: str) -> "tuple[int | None, int | None]":
        try:
            template_id = self._template_selector.get_template_selection(project_id)
        except Exception:
            return None, None
        if template_id is None:
            return None, None
        try:
            template = self._template_engine.get_template(template_id)
        except TemplateNaoEncontradoError:
            return None, None
        extra = template.extra if isinstance(template.extra, Mapping) else {}
        width, height = extra.get("width"), extra.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
        return None, None

    def _resolve_captions_mode(self, project_id: str) -> "str | None":
        state = self._manager.get_category(project_id, CAPTIONS)
        if state is None or not isinstance(state.data, Mapping):
            return None
        mode = state.data.get("mode")
        return mode if isinstance(mode, str) else None

    def _resolve_text_layers_data(self, project_id: str) -> Any:
        state = self._manager.get_category(project_id, TEXT_LAYERS)
        return state.data if state is not None else None

    # ===================================================================
    # Job handler
    # ===================================================================

    def handle_validate_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})
        if job.project_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_project_id"})

        video_id = job.video_id
        project_id = job.project_id

        video = self._database.get(Video, video_id)
        if video is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "video_not_found"})

        artifact = self._find_latest_render_artifact(video_id, project_id)
        if artifact is None:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "no_render_output_artifact_found", "project_id": project_id},
            )

        items: "list[ChecklistItemResult]" = []

        items.append(check_file_exists(artifact.path))
        if items[-1].status == CHECK_FAIL:
            # Sem arquivo, nenhuma outra checagem baseada em probe faz
            # sentido -- registra os demais itens como SKIP (nunca
            # fabrica um PASS/FAIL sobre um arquivo inexistente) e
            # encerra cedo.
            for name in CHECKLIST_ITEM_NAMES[1:]:
                items.append(ChecklistItemResult(name, CHECK_SKIP, "arquivo nao existe -- checagem nao aplicavel"))
            return self._finalize(job, artifact, items)

        result = self._probe.probe_deep(artifact.path)

        items.append(check_duration_valid(result))

        expected_width, expected_height = self._resolve_expected_resolution(project_id)
        items.append(check_resolution_valid(result, expected_width=expected_width, expected_height=expected_height))

        items.append(check_codec_valid(result))

        source_probe = self._probe_source_asset(video)
        items.append(check_audio_present(result, source_probe=source_probe))

        items.append(check_size_above_minimum(result, min_output_size_bytes=self._min_output_size_bytes))

        items.append(check_not_truncated(result))

        caption_zone = self._resolve_caption_zone(project_id)
        captions_mode = self._resolve_captions_mode(project_id)
        style = self._captions_style_engine.get_style(project_id)
        items.append(
            check_captions_within_canvas(captions_mode=captions_mode, caption_zone=caption_zone, style=style)
        )

        text_layers_data = self._resolve_text_layers_data(project_id)
        items.append(check_text_within_zones(text_layers_data=text_layers_data))

        real_size_bytes = result.file_size if result.file_size is not None else self._safe_file_size(artifact.path)
        integrity_item = check_artifact_integrity(
            recorded_size_bytes=artifact.size_bytes, real_size_bytes=real_size_bytes
        )
        if integrity_item.status == CHECK_CORRECTED:
            artifact.size_bytes = real_size_bytes
            self._database.save(artifact)
        items.append(integrity_item)

        return self._finalize(job, artifact, items)

    # -- helpers ----------------------------------------------------------

    def _probe_source_asset(self, video: Video) -> "MediaProbeResult | None":
        if video.source_asset_id is None:
            return None
        source = self._database.get(SourceAsset, video.source_asset_id)
        if source is None or not source.local_path:
            return None
        return self._probe.probe(source.local_path)

    @staticmethod
    def _safe_file_size(path: str) -> int:
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0

    def _finalize(self, job: Job, artifact: Artifact, items: "list[ChecklistItemResult]") -> JobStepResult:
        checklist_data = [item.to_dict() for item in items]
        failed = [item.name for item in items if item.status == CHECK_FAIL]
        needs_user_action = [item.name for item in items if item.status == CHECK_USER_ACTION_REQUIRED]
        corrected = [item.name for item in items if item.status == CHECK_CORRECTED]

        if failed:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={
                    "reason": "quality_control_failed",
                    "failed_items": failed,
                    "checklist": checklist_data,
                    "artifact_id": artifact.id,
                },
            )

        if needs_user_action:
            return JobStepResult(
                target_status=JOB_USER_ACTION_REQUIRED,
                data={
                    "reason": "quality_control_needs_user_action",
                    "needs_user_action_items": needs_user_action,
                    "checklist": checklist_data,
                    "artifact_id": artifact.id,
                },
            )

        self._audit_log.record_checkpoint(
            job.id, CHECKPOINT_VALIDATED,
            data={"artifact_id": artifact.id, "corrected_items": corrected},
        )
        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={"checklist": checklist_data, "corrected_items": corrected, "artifact_id": artifact.id},
        )
