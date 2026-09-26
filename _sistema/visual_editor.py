# -*- coding: utf-8 -*-
"""PROMPT 29 -- Editor: vídeo e imagem.

TEXTO LITERAL DO ROADMAP (Fase 5, imediatamente após o Prompt 28):

    PROMPT 29 — Editor: vídeo e imagem
    Crie módulo de transformações visuais.

    Suportar:
    - crop;
    - resize;
    - fit/fill;
    - zoom;
    - position;
    - rotation quando necessário;
    - brightness;
    - contrast;
    - saturation;
    - gamma;
    - sharpen;
    - noise;
    - ajustes existentes no protótipo.

    Tudo deve poder funcionar sozinho e em lote.

===========================================================================
0. CAMADA DE PERSISTÊNCIA -- REAPROVEITAMENTO, NÃO REINVENÇÃO
===========================================================================

Mesma fase (FASE 5 -- EDITOR MODULAR NÃO DESTRUTIVO) e mesma regra central
dos Prompts 27/27.5/28: este módulo grava DECISÕES sobre transformações
visuais, nunca processa/renderiza o vídeo real. ``EditProjectManager``
(Prompt 27) e ``EditProjectManager.update_category`` (Prompt 28) continuam
sendo a ÚNICA camada de persistência -- nenhuma tabela nova, nenhuma
migration nova (``LATEST_SCHEMA_VERSION`` permanece 9), nenhuma
reimplementação de fingerprint/revisão/concorrência.

===========================================================================
0.1 -- DECISÃO: DUAS CATEGORIAS, NÃO UMA (E POR QUÊ)
===========================================================================

``EditProjectManager`` expõe ``CROP = "crop"`` como categoria de
CONVENIÊNCIA (nomenclatura, nunca uma lista fechada -- qualquer string não
vazia é uma categoria válida). Este módulo usa:

- categoria ``CROP`` (já reservada) para TODAS as transformações de
  ENQUADRAMENTO: ``crop``, ``resize``, ``fit_mode``, ``zoom``,
  ``position``, ``rotation``. Razão: são geometricamente interdependentes
  -- um ``rotation`` de 90° muda o que ``crop``/``position`` significam
  espacialmente, um ``zoom`` interage com ``crop``. Mesmo raciocínio já
  usado no Prompt 28 para decidir que ``speed`` vive dentro de ``cuts``
  (seção 1.1 de ``timeline_editor.py``): campos que precisam ser lidos e
  entendidos JUNTOS, como um único estado de enquadramento coerente,
  vivem no mesmo ``data`` -- nunca em categorias separadas que poderiam
  dessincronizar entre si.
- categoria NOVA ``VISUAL_ADJUSTMENTS = "visual_adjustments"`` para
  ``brightness``/``contrast``/``saturation``/``gamma``/``sharpen``/
  ``noise``. Razão: são ajustes de IMAGEM/COR, conceitualmente
  independentes de ENQUADRAMENTO -- não há interdependência geométrica
  entre "quão saturado" e "que região do frame é exibida". Mantê-los na
  MESMA categoria que ``crop``/``zoom``/etc. obrigaria qualquer alteração
  de brilho a recomputar fingerprint/revisão do enquadramento inteiro
  (e vice-versa), quebrando a granularidade por categoria que é a garantia
  central do Prompt 27 ("trocar apenas um elemento não deve invalidar
  processamento que ainda seja reutilizável"). Uma nova categoria
  (``"visual_adjustments"``) é uma string livre válida -- ``EditProjectManager``
  não exige que toda categoria esteja pré-declarada como constante.

``REFRAME`` (categoria já reservada para o Prompt 33 -- Auto Reframe/9:16
automático) NÃO é usada aqui. São conceitos diferentes: este módulo
representa decisões MANUAIS do usuário sobre enquadramento; ``REFRAME``,
quando implementado, representará o resultado de um detector automático.
Este Prompt não implementa o Prompt 33.

===========================================================================
0.2 -- SEM VALIDAÇÃO CONTRA RESOLUÇÃO/ARQUIVO REAL
===========================================================================

Nem ``Video``/``SourceAsset`` têm hoje resolução/dimensões persistidas
(mesma situação já documentada para duração no Prompt 28). Toda validação
aqui é ESTRUTURAL, nunca contra o arquivo real -- ``MediaProbe`` continua
stateless e sob demanda, nunca chamado automaticamente por este módulo.

Consequência direta de design: ``crop``/``position`` usam coordenadas
NORMALIZADAS (fração do frame, ``0.0``-``1.0``), não pixels -- isso torna
a validação geométrica (``x + width <= 1.0``, etc.) inteiramente
autocontida, sem precisar conhecer a resolução real do vídeo. ``resize``
usa dimensões absolutas em pixels (um alvo de saída é inerentemente
absoluto), validadas apenas estruturalmente (inteiros positivos, dentro de
um teto de produto documentado abaixo) -- nunca comparadas contra a
resolução real de origem, que este módulo não conhece.

===========================================================================
0.3 -- PROTÓTIPO REAL (Geração 1) COMO REFERÊNCIA, NUNCA COMO IMPORT
===========================================================================

``_sistema/limpar_metadados_oficial.py`` (``montar_filtro_video``/
``gerar_ajustes_visuais``) já aplica, em produção, uma cadeia de filtros
ffmpeg com zoom progressivo (``zoompan``), ``eq`` (contrast/saturation/
brightness/gamma), ``colorbalance``, ``vignette``, ``noise`` e ``unsharp``
(nitidez). Usado aqui SOMENTE como referência de que essas grandezas são
multiplicadores/deslocamentos pequenos em torno de um valor neutro --
NUNCA importado (zero cross-import entre Geração 1 e Geração 2, já
estabelecido em todos os Prompts anteriores) e as faixas exatas aceitas
por este módulo são decisão própria, documentada abaixo (seção 1.2),
sem replicar os valores exatos do preset "BALA" (calibrado para um
objetivo diferente: variação anti-fingerprint discreta e automática, não
um usuário ajustando conscientemente um vídeo).

===========================================================================
0.4 -- BADGE DO CATÁLOGO: NENHUMA MUDANÇA EM ``media_catalog.py``
===========================================================================

O badge ``EDITED`` já dispara automaticamente assim que um ``Project`` tem
qualquer categoria definida em ``edit_state`` (mesma evidência já usada e
confirmada no Prompt 28). Gravar a primeira decisão de enquadramento OU de
ajuste visual através deste módulo já torna o vídeo ``EDITED`` no
catálogo, sem nenhuma mudança em ``media_catalog.py`` -- confirmado por
teste de integração read-only. Não existe, no roadmap do Prompt 27.5,
nenhum badge dedicado a "transformação visual aplicada" (``REFRAMED_9_16``
é especificamente sobre o resultado do Prompt 33, não sobre este Prompt).

===========================================================================
1. SCHEMA
===========================================================================

1.1 -- Categoria ``CROP`` (enquadramento) -- ``data``:

    {
      "crop":     {"x": 0.0-1.0, "y": 0.0-1.0, "width": >0, "height": >0}
                  (fração do frame original; x+width<=1, y+height<=1),
      "resize":   {"width": int 1-7680, "height": int 1-7680} (pixels),
      "fit_mode": "FIT" | "FILL" | "STRETCH",
      "zoom":     float, 1.0-4.0 (fator de ampliação; 1.0 = sem zoom),
      "position": {"x": -1.0-1.0, "y": -1.0-1.0} (deslocamento, fração do
                  frame),
      "rotation": 0 | 90 | 180 | 270 (graus, sentido horário)
    }

Cada chave é INDEPENDENTE -- ausente significa "não definido", nunca um
valor neutro implícito gravado automaticamente (item 1.d do Prompt:
"funcionar sozinho"). Cada ``set_*`` só escreve a SUA própria chave,
preservando as demais já definidas.

1.2 -- Categoria ``VISUAL_ADJUSTMENTS`` (imagem/cor) -- ``data``:

    {
      "brightness": -1.0 a 1.0   (neutro = 0.0),
      "contrast":    0.0 a 3.0   (neutro = 1.0),
      "saturation":  0.0 a 3.0   (neutro = 1.0),
      "gamma":       0.1 a 3.0   (neutro = 1.0),
      "sharpen":     0.0 a 5.0   (neutro = 0.0, intensidade de nitidez),
      "noise":       0.0 a 1.0   (neutro = 0.0, intensidade normalizada)
    }

Faixas escolhidas para serem amplas o bastante para uso real de produto
(nunca os micro-ajustes do preset "BALA", que são deliberadamente quase
imperceptíveis) sem permitir valores absurdos (ex.: saturação 1000x) --
decisão de produto documentada aqui, não derivada de nenhum arquivo real.

``rotation``/``fit_mode`` usam um VOCABULÁRIO FECHADO (nunca uma string
livre não verificada, item 1.a do Prompt): ``rotation`` é limitado a
``{0, 90, 180, 270}`` porque um ângulo arbitrário exigiria matemática de
bounding-box dependente da resolução real (indisponível -- seção 0.2);
múltiplos de 90° preservam retângulo e cobrem o caso real do produto
(correção de orientação vertical/horizontal).

===========================================================================
2. "SOZINHO E EM LOTE" (item 1.d do Prompt, texto literal do roadmap)
===========================================================================

SOZINHO: cada ``set_*`` funciona isoladamente -- nenhum exige que as
demais transformações estejam definidas (schema com chaves independentes,
seção 1).

EM LOTE: ``bulk_set_frame``/``bulk_set_adjustments`` aplicam os MESMOS
campos a VÁRIOS ``project_id`` numa única chamada, no mesmo espírito de
``BulkEditResult`` do Prompt 27.5 (``requested``/``updated``/``unchanged``/
``failed``) -- um projeto inexistente/inválido no meio do lote nunca
aborta os demais. Os valores são validados UMA VEZ, antes de tocar
qualquer projeto (falha rápida e idêntica para todos, evita validar N
vezes o mesmo valor) -- só a EXISTÊNCIA do projeto é resolvida por item do
lote.

===========================================================================
3. NÃO ALTERA O VÍDEO ORIGINAL / NÃO PROCESSA MÍDIA
===========================================================================

Este módulo NUNCA chama ``subprocess``/``ffmpeg``/``ffprobe``, nunca
escreve/move/renomeia/apaga qualquer arquivo referenciado por
``SourceAsset.local_path``, nunca importa ``circuit_breaker``/
``retry_policy``/``publication_idempotency``/``secrets_manager``/
``domain.job_state_machine``/nada de Geração 1 (incluindo
``limpar_metadados_oficial.py``, citado na seção 0.3 apenas como
referência de leitura, nunca como import), nunca cria
``Job``/``Artifact``/``Publication``/``Schedule``, nunca abre uma
``transaction()`` própria nem acessa ``Project.edit_state``/
``edit_state_json``/métodos de ``LocalDatabase`` diretamente -- toda
leitura/escrita passa exclusivamente por ``EditProjectManager``.
Confirmado por testes estruturais via AST (nunca grep textual ingênuo --
esta própria docstring cita "ffmpeg"/"subprocess" para explicar por que
não são usados).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence

from .edit_project import CROP, EditProjectManager, ProjectNaoEncontradoError
from .storage.database import LocalDatabase

__all__ = [
    "VISUAL_ADJUSTMENTS",
    "FIT_MODES",
    "ROTATIONS",
    "BRIGHTNESS_RANGE",
    "CONTRAST_RANGE",
    "SATURATION_RANGE",
    "GAMMA_RANGE",
    "SHARPEN_RANGE",
    "NOISE_RANGE",
    "ZOOM_RANGE",
    "RESIZE_RANGE",
    "FrameState",
    "AdjustmentsState",
    "BulkVisualEditResult",
    "VisualEditorError",
    "CampoInvalidoError",
    "GeometriaInvalidaError",
    "ModoInvalidoError",
    "VisualEditor",
]

# Categoria nova (string livre válida -- ver seção 0.1 da docstring do módulo).
VISUAL_ADJUSTMENTS = "visual_adjustments"

# Vocabulários fechados e faixas -- ver seções 1.1/1.2 da docstring do módulo.
FIT_MODES = ("FIT", "FILL", "STRETCH")
ROTATIONS = (0, 90, 180, 270)

BRIGHTNESS_RANGE = (-1.0, 1.0)
CONTRAST_RANGE = (0.0, 3.0)
SATURATION_RANGE = (0.0, 3.0)
GAMMA_RANGE = (0.1, 3.0)
SHARPEN_RANGE = (0.0, 5.0)
NOISE_RANGE = (0.0, 1.0)
ZOOM_RANGE = (1.0, 4.0)
POSITION_RANGE = (-1.0, 1.0)
RESIZE_RANGE = (1, 7680)  # pixels, teto de produto -- ver seção 1.1/1.2.


# ---------------------------------------------------------------------------
# Erros estruturados (mesmo padrão ``.code`` de timeline_editor.py/
# edit_project.py/media_catalog.py)
# ---------------------------------------------------------------------------
class VisualEditorError(RuntimeError):
    """Base para todo erro estruturado deste módulo -- nunca um
    ``ValueError``/``KeyError`` cru escapa de um método público de
    ``VisualEditor``. Erros de ``EditProjectManager`` (ex.:
    ``ProjectNaoEncontradoError``) já são estruturados e se propagam sem
    wrapping."""

    code: ClassVar[str] = "VISUAL_EDITOR_ERRO"


class CampoInvalidoError(VisualEditorError):
    """Um campo escalar (brightness/contrast/saturation/gamma/sharpen/
    noise/zoom/coordenadas de crop-position) tem tipo errado, não é
    finito, ou está fora da faixa documentada."""

    code: ClassVar[str] = "CAMPO_INVALIDO"


class GeometriaInvalidaError(VisualEditorError):
    """``crop``/``resize`` produziria uma geometria inválida (largura/
    altura <= 0, ou um crop que ultrapassa os limites normalizados do
    frame)."""

    code: ClassVar[str] = "GEOMETRIA_INVALIDA"


class ModoInvalidoError(VisualEditorError):
    """``fit_mode``/``rotation`` fora do vocabulário fechado documentado
    (seção 1.1/1.2 da docstring do módulo)."""

    code: ClassVar[str] = "MODO_INVALIDO"


# ---------------------------------------------------------------------------
# Estados de leitura (somente os campos já definidos; None = não definido)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FrameState:
    crop: "dict[str, float] | None" = None
    resize: "dict[str, int] | None" = None
    fit_mode: "str | None" = None
    zoom: "float | None" = None
    position: "dict[str, float] | None" = None
    rotation: "int | None" = None


@dataclass(frozen=True)
class AdjustmentsState:
    brightness: "float | None" = None
    contrast: "float | None" = None
    saturation: "float | None" = None
    gamma: "float | None" = None
    sharpen: "float | None" = None
    noise: "float | None" = None


@dataclass(frozen=True)
class BulkVisualEditResult:
    """``requested``/``updated``/``unchanged``/``failed`` -- mesmo
    vocabulário de ``BulkEditResult`` (Prompt 27.5). ``failed`` traz
    ``(project_id, code)``, nunca texto de exceção cru."""

    requested: "tuple[str, ...]"
    updated: "tuple[str, ...]"
    unchanged: "tuple[str, ...]"
    failed: "tuple[tuple[str, str], ...]"


# ---------------------------------------------------------------------------
# Validação -- nunca contra o arquivo real (seção 0.2 da docstring do módulo)
# ---------------------------------------------------------------------------
def _as_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampoInvalidoError(f"{field_name} deve ser numérico, recebido: {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise CampoInvalidoError(f"{field_name} deve ser finito, recebido: {value!r}")
    return number


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampoInvalidoError(f"{field_name} deve ser um inteiro, recebido: {value!r}")
    return value


def _validate_range(value: float, field_name: str, bounds: "tuple[float, float]") -> float:
    low, high = bounds
    if not (low <= value <= high):
        raise CampoInvalidoError(f"{field_name} deve estar entre {low} e {high}, recebido: {value!r}")
    return value


def _validate_crop(payload: Any) -> dict:
    if not isinstance(payload, Mapping):
        raise CampoInvalidoError(f"crop deve ser um objeto com x/y/width/height, recebido: {payload!r}")
    x = _validate_range(_as_float(payload.get("x"), "crop.x"), "crop.x", (0.0, 1.0))
    y = _validate_range(_as_float(payload.get("y"), "crop.y"), "crop.y", (0.0, 1.0))
    width = _as_float(payload.get("width"), "crop.width")
    height = _as_float(payload.get("height"), "crop.height")
    if width <= 0:
        raise GeometriaInvalidaError(f"crop.width deve ser > 0, recebido: {width!r}")
    if height <= 0:
        raise GeometriaInvalidaError(f"crop.height deve ser > 0, recebido: {height!r}")
    if x + width > 1.0 + 1e-9:
        raise GeometriaInvalidaError(f"crop ultrapassa a borda direita do frame: x={x!r} width={width!r}")
    if y + height > 1.0 + 1e-9:
        raise GeometriaInvalidaError(f"crop ultrapassa a borda inferior do frame: y={y!r} height={height!r}")
    return {"x": x, "y": y, "width": width, "height": height}


def _validate_resize(payload: Any) -> dict:
    if not isinstance(payload, Mapping):
        raise CampoInvalidoError(f"resize deve ser um objeto com width/height, recebido: {payload!r}")
    width = _as_int(payload.get("width"), "resize.width")
    height = _as_int(payload.get("height"), "resize.height")
    low, high = RESIZE_RANGE
    if not (low <= width <= high):
        raise GeometriaInvalidaError(f"resize.width deve estar entre {low} e {high}, recebido: {width!r}")
    if not (low <= height <= high):
        raise GeometriaInvalidaError(f"resize.height deve estar entre {low} e {high}, recebido: {height!r}")
    return {"width": width, "height": height}


def _validate_fit_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in FIT_MODES:
        raise ModoInvalidoError(f"fit_mode deve ser um de {FIT_MODES}, recebido: {value!r}")
    return value


def _validate_zoom(value: Any) -> float:
    zoom = _as_float(value, "zoom")
    return _validate_range(zoom, "zoom", ZOOM_RANGE)


def _validate_position(payload: Any) -> dict:
    if not isinstance(payload, Mapping):
        raise CampoInvalidoError(f"position deve ser um objeto com x/y, recebido: {payload!r}")
    x = _validate_range(_as_float(payload.get("x"), "position.x"), "position.x", POSITION_RANGE)
    y = _validate_range(_as_float(payload.get("y"), "position.y"), "position.y", POSITION_RANGE)
    return {"x": x, "y": y}


def _validate_rotation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in ROTATIONS:
        raise ModoInvalidoError(f"rotation deve ser um de {ROTATIONS}, recebido: {value!r}")
    return value


def _validate_adjustment(value: Any, field_name: str, bounds: "tuple[float, float]") -> float:
    number = _as_float(value, field_name)
    return _validate_range(number, field_name, bounds)


_FRAME_VALIDATORS = {
    "crop": _validate_crop,
    "resize": _validate_resize,
    "fit_mode": _validate_fit_mode,
    "zoom": _validate_zoom,
    "position": _validate_position,
    "rotation": _validate_rotation,
}

_ADJUSTMENT_RANGES = {
    "brightness": BRIGHTNESS_RANGE,
    "contrast": CONTRAST_RANGE,
    "saturation": SATURATION_RANGE,
    "gamma": GAMMA_RANGE,
    "sharpen": SHARPEN_RANGE,
    "noise": NOISE_RANGE,
}


def _as_dict_or_none(payload: Any, field_name: str) -> "dict | None":
    """Decodifica um sub-objeto (``crop``/``resize``/``position``) já
    persistido -- nunca confia cegamente no formato: um valor corrompido
    (ex.: uma string) levantaria um ``ValueError`` cru de ``dict(...)``
    (``dict("x")`` tenta iterar a string como pares chave/valor) se não
    fosse verificado aqui primeiro. Achado no GATE ADVERSARIAL."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise CampoInvalidoError(f"{field_name} persistido é inválido (esperado objeto, recebido: {payload!r})")
    return dict(payload)


def _frame_state_from_data(data: Any) -> FrameState:
    if data is None:
        return FrameState()
    if not isinstance(data, Mapping):
        raise CampoInvalidoError(f"data da categoria 'crop' deve ser um objeto, recebido: {data!r}")
    return FrameState(
        crop=_as_dict_or_none(data.get("crop"), "crop"),
        resize=_as_dict_or_none(data.get("resize"), "resize"),
        fit_mode=data.get("fit_mode"),
        zoom=data.get("zoom"),
        position=_as_dict_or_none(data.get("position"), "position"),
        rotation=data.get("rotation"),
    )


def _adjustments_state_from_data(data: Any) -> AdjustmentsState:
    if data is None:
        return AdjustmentsState()
    if not isinstance(data, Mapping):
        raise CampoInvalidoError(f"data da categoria 'visual_adjustments' deve ser um objeto, recebido: {data!r}")
    return AdjustmentsState(
        brightness=data.get("brightness"),
        contrast=data.get("contrast"),
        saturation=data.get("saturation"),
        gamma=data.get("gamma"),
        sharpen=data.get("sharpen"),
        noise=data.get("noise"),
    )


class VisualEditor:
    """Serviço de transformações visuais não destrutivas -- opera
    exclusivamente sobre as categorias ``crop``/``visual_adjustments`` de
    ``EditProjectManager`` (ver docstring do módulo). Nunca modifica o
    vídeo original, nunca abre uma transaction própria, nunca cria
    ``Job``/``Artifact``/``Publication``/``Schedule``."""

    def __init__(self, database: LocalDatabase) -> None:
        if not isinstance(database, LocalDatabase):
            raise TypeError("database deve ser uma instância de LocalDatabase")
        self._manager = EditProjectManager(database)

    # -- leitura -------------------------------------------------------

    def get_frame(self, project_id: str) -> FrameState:
        """Estado de enquadramento atual -- campos ausentes retornam
        ``None`` (nunca um valor neutro implícito, item 1.d do Prompt)."""
        state = self._manager.get_category(project_id, CROP)
        return _frame_state_from_data(state.data if state is not None else None)

    def get_adjustments(self, project_id: str) -> AdjustmentsState:
        state = self._manager.get_category(project_id, VISUAL_ADJUSTMENTS)
        return _adjustments_state_from_data(state.data if state is not None else None)

    # -- enquadramento (categoria CROP) ---------------------------------

    def set_crop(self, project_id: str, x: float, y: float, width: float, height: float) -> FrameState:
        value = _validate_crop({"x": x, "y": y, "width": width, "height": height})
        return self._set_frame_field(project_id, "crop", value)

    def set_resize(self, project_id: str, width: int, height: int) -> FrameState:
        value = _validate_resize({"width": width, "height": height})
        return self._set_frame_field(project_id, "resize", value)

    def set_fit_mode(self, project_id: str, mode: str) -> FrameState:
        value = _validate_fit_mode(mode)
        return self._set_frame_field(project_id, "fit_mode", value)

    def set_zoom(self, project_id: str, factor: float) -> FrameState:
        value = _validate_zoom(factor)
        return self._set_frame_field(project_id, "zoom", value)

    def set_position(self, project_id: str, x: float, y: float) -> FrameState:
        value = _validate_position({"x": x, "y": y})
        return self._set_frame_field(project_id, "position", value)

    def set_rotation(self, project_id: str, degrees: int) -> FrameState:
        value = _validate_rotation(degrees)
        return self._set_frame_field(project_id, "rotation", value)

    # -- ajustes de imagem/cor (categoria VISUAL_ADJUSTMENTS) -----------

    def set_brightness(self, project_id: str, value: float) -> AdjustmentsState:
        return self._set_adjustment_field(project_id, "brightness", value)

    def set_contrast(self, project_id: str, value: float) -> AdjustmentsState:
        return self._set_adjustment_field(project_id, "contrast", value)

    def set_saturation(self, project_id: str, value: float) -> AdjustmentsState:
        return self._set_adjustment_field(project_id, "saturation", value)

    def set_gamma(self, project_id: str, value: float) -> AdjustmentsState:
        return self._set_adjustment_field(project_id, "gamma", value)

    def set_sharpen(self, project_id: str, value: float) -> AdjustmentsState:
        return self._set_adjustment_field(project_id, "sharpen", value)

    def set_noise(self, project_id: str, value: float) -> AdjustmentsState:
        return self._set_adjustment_field(project_id, "noise", value)

    # -- lote (item 1.d/seção 2 da docstring do módulo) ------------------

    def bulk_set_frame(self, project_ids: Sequence[str], **fields: Any) -> BulkVisualEditResult:
        """Aplica os mesmos campos de enquadramento a vários projetos.
        Valores validados UMA VEZ antes de tocar qualquer projeto -- um
        ``project_id`` inexistente no meio do lote nunca aborta os
        demais (``failed`` acumula, os outros continuam)."""
        validated = self._validate_frame_fields(fields)
        return self._bulk_apply(project_ids, CROP, validated)

    def bulk_set_adjustments(self, project_ids: Sequence[str], **fields: Any) -> BulkVisualEditResult:
        validated = self._validate_adjustment_fields(fields)
        return self._bulk_apply(project_ids, VISUAL_ADJUSTMENTS, validated)

    # -- internos -----------------------------------------------------------

    @staticmethod
    def _validate_frame_fields(fields: "Mapping[str, Any]") -> dict:
        if not fields:
            raise CampoInvalidoError("bulk_set_frame exige ao menos um campo")
        unknown = set(fields) - set(_FRAME_VALIDATORS)
        if unknown:
            raise CampoInvalidoError(f"campo(s) de enquadramento desconhecido(s): {sorted(unknown)!r}")
        return {name: _FRAME_VALIDATORS[name](value) for name, value in fields.items()}

    @staticmethod
    def _validate_adjustment_fields(fields: "Mapping[str, Any]") -> dict:
        if not fields:
            raise CampoInvalidoError("bulk_set_adjustments exige ao menos um campo")
        unknown = set(fields) - set(_ADJUSTMENT_RANGES)
        if unknown:
            raise CampoInvalidoError(f"campo(s) de ajuste desconhecido(s): {sorted(unknown)!r}")
        return {
            name: _validate_adjustment(value, name, _ADJUSTMENT_RANGES[name])
            for name, value in fields.items()
        }

    def _set_frame_field(self, project_id: str, field_name: str, value: Any) -> FrameState:
        def mutator(current_data: Any) -> dict:
            merged = dict(current_data) if isinstance(current_data, Mapping) else {}
            merged[field_name] = value
            return merged

        state = self._manager.update_category(project_id, CROP, mutator)
        return _frame_state_from_data(state.data)

    def _set_adjustment_field(self, project_id: str, field_name: str, value: Any) -> AdjustmentsState:
        validated = _validate_adjustment(value, field_name, _ADJUSTMENT_RANGES[field_name])

        def mutator(current_data: Any) -> dict:
            merged = dict(current_data) if isinstance(current_data, Mapping) else {}
            merged[field_name] = validated
            return merged

        state = self._manager.update_category(project_id, VISUAL_ADJUSTMENTS, mutator)
        return _adjustments_state_from_data(state.data)

    def _bulk_apply(self, project_ids: Sequence[str], category: str, validated_fields: "Mapping[str, Any]") -> BulkVisualEditResult:
        requested = tuple(_dedupe_preserve_order(project_ids))
        updated: list[str] = []
        unchanged: list[str] = []
        failed: list[tuple[str, str]] = []

        for project_id in requested:
            # ``changed`` é computado DENTRO do mutator -- ou seja, dentro da
            # mesma transaction atômica de ``update_category`` -- nunca por
            # uma leitura separada antes da escrita (isso reabriria a mesma
            # janela de TOCTOU que a seção 0.1 do Prompt 28 já eliminou; aqui
            # usar apenas comparar o resultado de uma leitura anterior
            # comporia leitura+decisão em DUAS transações diferentes).
            changed_flag: list[bool] = []

            def mutator(current_data: Any, _flag=changed_flag) -> dict:
                merged = dict(current_data) if isinstance(current_data, Mapping) else {}
                before = dict(merged)
                merged.update(validated_fields)
                _flag.append(merged != before)
                return merged

            try:
                self._manager.update_category(project_id, category, mutator)
            except ProjectNaoEncontradoError as exc:
                # projeto não existe -- falha ISOLADA deste item, nunca
                # aborta o lote (item 1.d do Prompt).
                failed.append((project_id, exc.code))
                continue
            except ValueError:
                # ``project_id`` malformado (não é um UUID) -- mesma
                # categoria de falha isolada, código próprio e honesto
                # (nunca ``str(exc)`` bruto persistido, GATE 10).
                failed.append((project_id, "PROJETO_ID_INVALIDO"))
                continue
            if changed_flag and changed_flag[0]:
                updated.append(project_id)
            else:
                unchanged.append(project_id)

        return BulkVisualEditResult(
            requested=requested,
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            failed=tuple(failed),
        )


def _dedupe_preserve_order(values: Sequence[str]) -> "tuple[str, ...]":
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)
