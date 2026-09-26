"""AudioEngine -- Prompt 30 (Fase 5: Editor Modular Não Destrutivo).

=======================================================================
0. VISÃO GERAL E ESCOPO
=======================================================================

Este módulo implementa decisões de áudio de um Project, seguindo
EXATAMENTE o mesmo padrão arquitetural já estabelecido em
``timeline_editor.py`` (Prompt 28) e ``visual_editor.py`` (Prompt 29):

- Nenhum áudio real é lido, decodificado, filtrado ou re-encodado aqui.
- Nenhum ``subprocess``/FFmpeg/FFprobe é chamado.
- Nenhum arquivo em ``SourceAsset.local_path`` é tocado.
- Tudo que este módulo faz é ler e escrever DECISÕES estruturadas dentro
  da categoria ``AUDIO_SETTINGS`` de ``EditProjectManager`` (já reservada
  desde o Prompt 27 -- ver ``edit_project.py`` linha ~204). A aplicação
  real dessas decisões (efetivamente processar o áudio) é trabalho de um
  futuro Render Engine, fora do escopo deste Prompt.
- ``AudioEngine`` não abre transaction própria, não chama
  ``LocalDatabase.transaction()``/``.get(Project``/``.save(`` diretamente
  e não mantém nenhum SQLite próprio: toda leitura/escrita passa por
  ``EditProjectManager.get_category``/``set_category``/``update_category``.
- Geração 1 (``_sistema/limpar_metadados_oficial.py``) NÃO é importado
  nem chamado por este módulo. Ele é citado apenas nesta docstring, como
  referência histórica: o único precedente real de manipulação de áudio
  no protótipo é o filtro fixo ``-af atempo={velocidade},volume=1.01``
  aplicado ao preset "BALA", sempre acoplado a uma mudança de velocidade,
  nunca configurável isoladamente pelo usuário. ``AudioEngine`` não herda
  nada dessa lógica -- é um novo conjunto de decisões, independente.

=======================================================================
0.1 POR QUE ``AUDIO_SETTINGS`` E NÃO UMA CATEGORIA NOVA
=======================================================================

``AUDIO_SETTINGS = "audio_settings"`` já existe em ``edit_project.py``
desde o Prompt 27, reservada explicitamente para este Prompt. Usá-la
mantém o mesmo modelo de fingerprint/revisão/isolamento por categoria já
usado por ``CUTS``, ``SPEED``, ``CROP``, ``VISUAL_ADJUSTMENTS`` e
``TEMPLATE`` -- sem exigir nenhuma mudança em ``EditProjectManager`` nem
em migrations.

=======================================================================
0.2 DISTINÇÃO CRÍTICA DE BADGES -- LER COM ATENÇÃO
=======================================================================

Diferente do padrão dos Prompts 28/29 (onde qualquer categoria de edição
preenchida automaticamente faz o badge ``EDITED`` acender, via a
expressão SQL real em ``media_catalog.py`` que verifica
``EXISTS (... json_each(json_extract(p.edit_state_json, '$.categories')) ...)``),
o badge ``BADGE_AUDIO_PROCESSED = "AUDIO_PROCESSED"`` está hardcoded como
``"0"`` (sempre falso) em ``_BADGE_SQL_EXPRESSIONS`` -- confirmado por
leitura direta de ``media_catalog.py`` linhas 150-220 nesta sessão.

Gravar uma decisão em ``AUDIO_SETTINGS``:
- FAZ o badge ``EDITED`` acender (automático, herdado da expressão SQL
  genérica de ``EDITED`` -- nenhuma mudança de código necessária aqui).
- NÃO FAZ e NUNCA DEVE FAZER o badge ``AUDIO_PROCESSED`` acender. Esse
  badge só deve virar verdadeiro quando um passo de processamento REAL
  (futuro Render Engine, ou uma execução real de áudio) produzir
  evidência estruturada genuína de que o áudio foi de fato processado.

``media_catalog.py`` NÃO é tocado por este Prompt -- nem para adicionar
lógica nova a ``AUDIO_PROCESSED``, nem por qualquer outro motivo. Existe
um teste negativo explícito (``test_gravar_audio_settings_nao_acende_badge_audio_processed``)
provando isso.

=======================================================================
0.3 DECISÕES ARQUITETURAIS -- CADA CAMPO, COM JUSTIFICATIVA
=======================================================================

Todas as decisões abaixo foram deliberadas nesta etapa, não implementadas
silenciosamente. Cada campo funciona SOZINHO (nenhum exige que outro já
esteja definido) e cada ``set_*`` escreve apenas a própria fatia da
categoria ``AUDIO_SETTINGS``, via ``EditProjectManager.update_category``
(mutator atômico dentro da mesma transaction de leitura+escrita -- nunca
``get_category`` + ``set_category`` como duas chamadas separadas, pelo
mesmo motivo de TOCTOU/lost-update já documentado em ``edit_project.py``
e reaplicado em ``timeline_editor.py``/``visual_editor.py``).

--- volume (float, multiplicador linear) ---
Representado como multiplicador linear (não dB), no intervalo
``[0.0, 3.0]``, neutro em ``1.0``. Motivo: "volume" é o controle de
mixagem criativa voltado ao usuário final -- multiplicadores lineares são
mais intuitivos para essa camada ("dobrar o volume" = ``2.0``) e mapeiam
diretamente para um fator de ganho aplicado em uma futura renderização
(``volume`` do FFmpeg aceita um fator linear diretamente). ``0.0`` é
permitido (silêncio contínuo) e é DELIBERADAMENTE distinto do campo
``mute`` (booleano) -- ``volume=0.0`` é um valor num controle contínuo
(pode ser parte de uma curva/automação futura), enquanto ``mute`` é um
interruptor binário explícito e mais fácil de checar na UI ("este vídeo
está mudo?"). Os dois campos coexistem sem redundância.

--- gain (float, dB) ---
Representado em decibéis, no intervalo ``[-24.0, 24.0]``, neutro em
``0.0``. Motivo pelo qual ``gain`` NÃO é redundante com ``volume``:
``gain`` representa um trim técnico de correção (ex.: "este áudio foi
gravado baixo demais, compensar tecnicamente antes de qualquer
mixagem"), tipicamente aplicado ANTES de normalização/mixagem criativa,
enquanto ``volume`` é o ajuste de mixagem final voltado ao usuário. Essa
distinção (trim técnico em dB vs. fader de mixagem linear) é comum em
editores de áudio profissionais e justifica dois campos independentes em
vez de duplicar o mesmo conceito. Documentada aqui explicitamente, como
pedido pelo Prompt, em vez de implementar dois campos idênticos sem
justificativa.

--- mute (bool) ---
Interruptor binário independente de ``volume``/``gain``. Um render
futuro deve tratar ``mute=True`` como silenciar o áudio
independentemente dos valores numéricos de ``volume``/``gain`` já
persistidos (para não perder essas configurações caso o usuário
desmarque ``mute`` depois).

--- normalization (objeto: enabled + target_lufs opcional) ---
Representado como um objeto ``{"enabled": bool, "target_lufs": float | None}``
em vez de um booleano isolado, porque um alvo de loudness explícito é
informação real e útil (plataformas de streaming/redes sociais têm
metas de loudness padronizadas, tipicamente entre -6 e -36 LUFS) e não
seria uma funcionalidade inventada -- é a representação mínima
suficiente para "normalização automática" enquanto deixa espaço para o
usuário (ou uma futura predefinição por plataforma) escolher um alvo
específico. ``target_lufs`` é opcional (``None``) e só é validado
estruturalmente quando fornecido; ``None`` significa "usar o padrão que
uma etapa de renderização futura decidir", NUNCA um valor mágico
implícito calculado aqui. Intervalo documentado: ``[-36.0, -6.0]``
(cobre os alvos de loudness comumente usados por plataformas de
streaming e broadcast). Um único setter (``set_normalization``) grava os
dois campos juntos, porque ``target_lufs`` só faz sentido semântico
quando acompanhado do estado de ``enabled`` -- gravá-los separadamente
arriscaria um estado inconsistente (``target_lufs`` órfão sem
``enabled`` correspondente).

--- clipping_protection (bool) ---
Interruptor binário simples ("evitar que o volume ultrapasse o teto
digital"). Não há parâmetro numérico adicional pedido pelo Prompt para
este campo -- implementar um limiar configurável aqui seria escopo não
solicitado; documentado como decisão deliberada de manter simples.

--- fade_in_seconds / fade_out_seconds (dois campos, não um objeto) ---
Decisão: dois campos independentes no nível superior de
``AUDIO_SETTINGS`` (não aninhados em um objeto ``fade``), cada um com seu
próprio setter (``set_fade_in``/``set_fade_out``). Motivo: um usuário
frequentemente quer só fade-in OU só fade-out, e campos de nível
superior permitem que cada ``set_*`` escreva sua própria chave sem
precisar ler-modificar-mesclar um objeto aninhado primeiro (mesmo
padrão de simplicidade já usado por ``SPEED``/``ROTATION`` em
``timeline_editor.py``/``visual_editor.py``, que também são campos
soltos, não agrupados).

Ambos são segundos relativos ao VÍDEO ORIGINAL (mesma convenção de
``start``/``end`` de ``timeline_editor.py`` -- ver seção 1.1 daquele
módulo), NÃO relativos a qualquer corte já aplicado em ``CUTS``. Isso
preserva a possibilidade de, numa etapa futura, recalcular fades em
relação a uma timeline editada sem perder a intenção original do
usuário (o mesmo motivo pelo qual ``timeline_editor.py`` usa segundos do
vídeo original, não índices de segmento).

Intervalo documentado: ``[0.0, 30.0]`` segundos cada -- um teto de
produto generoso para vídeos curtos, não vinculado à duração real do
arquivo (que este módulo nunca lê nem persiste, ver seção 0.6 abaixo).

--- noise_reduction (objeto: enabled + intensity) ---
Representado como ``{"enabled": bool, "intensity": float}``, com
``intensity`` no intervalo ``[0.0, 1.0]`` (neutro ``0.0``). Um único
setter (``set_noise_reduction``) grava os dois juntos, pelo mesmo motivo
de coerência semântica de ``normalization``. ``intensity`` é validada
estruturalmente mesmo quando ``enabled=False`` (validação estrutural,
não policiamento de combinação semântica -- mesma filosofia já aplicada
em ``visual_editor.py``, que valida ranges independentemente de outros
campos relacionados estarem "ativos").

--- background_music (DELIBERADAMENTE NÃO IMPLEMENTADO NESTE PROMPT) ---
O Prompt pede explicitamente para decidir COMO documentar o adiamento,
não para inventar a funcionalidade. Decisão: o nome de campo
``background_music`` NÃO é escrito em nenhum schema, não tem setter, não
tem getter e não é reservado como chave-fantasma dentro de
``AUDIO_SETTINGS`` neste Prompt. Um stub que "existe mas não faz nada"
seria pior do que a ausência total -- criaria a aparência de uma
funcionalidade parcialmente implementada onde não há nenhuma. Quando um
Prompt futuro implementar seleção/mixagem de música de fundo (biblioteca
de faixas, volume relativo à voz, ducking, etc. -- todos fora de escopo
aqui), esse Prompt introduzirá o campo (ou uma categoria própria) na
íntegra, com sua própria validação e testes. Este parágrafo é o registro
documentado do adiamento deliberado.

=======================================================================
0.4 OPERAÇÃO EM LOTE (BATCH)
=======================================================================

Decisão: SIM, implementar batch, seguindo o mesmo padrão já estabelecido
em ``MediaCatalogService.bulk_edit`` (Prompt 27.5) e
``VisualEditor.bulk_set_frame``/``bulk_set_adjustments`` (Prompt 29):
``bulk_set_audio_settings(project_ids, **campos)`` devolve um
``BulkAudioEditResult`` (``requested``/``updated``/``unchanged``/``failed``).
Motivo: este é o terceiro módulo consecutivo da Fase 5 e o produto já
estabeleceu esse padrão para operações que plausivelmente serão
aplicadas a múltiplos vídeos de uma vez (ex.: "normalizar o volume de
todos os vídeos deste lote") -- manter consistência evita uma API
inconsistente entre módulos irmãos, sem introduzir nenhum conceito novo.

=======================================================================
0.5 ERROS ESTRUTURADOS
=======================================================================

Mesma filosofia dos módulos irmãos: nenhum ``ValueError``/``KeyError``
cru escapa de um método público. Todos os erros de validação de campo
usam ``CampoInvalidoError`` (único subtipo necessário -- este módulo não
tem nenhum campo de vocabulário fechado tipo ``fit_mode``/``rotation``,
então uma segunda subclasse de erro seria distinção sem diferença).
``bool`` é sempre explicitamente rejeitado onde um ``float`` é esperado
(``isinstance(x, bool)`` é verificado ANTES de ``isinstance(x, (int, float))``,
porque em Python ``bool`` é subclasse de ``int`` -- a mesma armadilha já
documentada e testada em ``timeline_editor.py``/``visual_editor.py``).
``inf``/``-inf``/``nan`` são sempre rejeitados estruturalmente antes de
qualquer checagem de intervalo.

=======================================================================
0.6 LEITURA DA TIMELINE (``CUTS``) -- DECISÃO: NÃO LER
=======================================================================

O Prompt convida explicitamente a decidir se ``AudioEngine`` deve ler,
somente para validação, a categoria ``CUTS`` (via
``EditProjectManager.get_category``) para checar por exemplo se um fade
não é mais longo que a timeline já cortada.

Decisão: NÃO implementar essa leitura cruzada nesta etapa. Motivos:

1. Modularidade (Princípio A do CLAUDE.md): "nenhuma dessas funções deve
   obrigar o usuário a passar pelas demais". Um usuário deve poder
   configurar áudio em um Project que ainda não tem nenhuma decisão de
   ``CUTS`` registrada (timeline nunca inicializada) -- exigir a leitura
   de uma categoria que pode legitimamente não existir criaria um
   acoplamento implícito entre dois módulos que devem funcionar
   isoladamente.
2. Duração real do vídeo nunca é persistida por nenhum módulo desta
   fase (mesmo motivo pelo qual ``visual_editor.py`` usa coordenadas
   normalizadas em vez de pixels) -- mesmo lendo ``CUTS``, não há uma
   "duração total" confiável e sempre presente contra a qual validar um
   fade quando a timeline não foi inicializada.
3. O teto de produto de 30 segundos por fade (seção 0.3) já produz uma
   validação estrutural significativa sem depender de outra categoria.

Uma validação cruzada fade-vs-timeline-cortada fica registrada aqui como
possível melhoria futura (ex.: no Render Engine, momento em que a
duração real já é conhecida), não implementada neste Prompt.

=======================================================================
1. USO
=======================================================================

    from _sistema.audio_engine import AudioEngine
    from _sistema.edit_project import EditProjectManager

    manager = EditProjectManager(database)
    engine = AudioEngine(manager)

    engine.set_volume(project_id, 1.5)
    engine.set_mute(project_id, False)
    engine.set_gain(project_id, -3.0)
    engine.set_normalization(project_id, enabled=True, target_lufs=-14.0)
    engine.set_clipping_protection(project_id, True)
    engine.set_fade_in(project_id, 1.5)
    engine.set_fade_out(project_id, 2.0)
    engine.set_noise_reduction(project_id, enabled=True, intensity=0.4)

    settings = engine.get_audio_settings(project_id)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence

from .edit_project import AUDIO_SETTINGS, EditProjectManager, ProjectNaoEncontradoError

# ---------------------------------------------------------------------
# Constantes de validação
# ---------------------------------------------------------------------

VOLUME_RANGE = (0.0, 3.0)
GAIN_RANGE = (-24.0, 24.0)
TARGET_LUFS_RANGE = (-36.0, -6.0)
FADE_RANGE = (0.0, 30.0)
NOISE_INTENSITY_RANGE = (0.0, 1.0)


# ---------------------------------------------------------------------
# Erros estruturados
# ---------------------------------------------------------------------

class AudioEngineError(RuntimeError):
    """Classe base de todos os erros deste módulo."""

    code: ClassVar[str] = "AUDIO_ENGINE_ERRO"


class CampoInvalidoError(AudioEngineError):
    """Um campo recebeu um valor de tipo errado, fora de intervalo, ou
    não-finito (inf/-inf/nan)."""

    code: ClassVar[str] = "CAMPO_INVALIDO"


# ---------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class AudioSettingsState:
    """Snapshot somente-leitura da categoria ``AUDIO_SETTINGS`` de um
    Project. Todos os campos são ``Optional`` -- ``None`` significa "não
    definido ainda", não um valor neutro implícito."""

    volume: "float | None" = None
    mute: "bool | None" = None
    gain: "float | None" = None
    normalization_enabled: "bool | None" = None
    normalization_target_lufs: "float | None" = None
    clipping_protection: "bool | None" = None
    fade_in_seconds: "float | None" = None
    fade_out_seconds: "float | None" = None
    noise_reduction_enabled: "bool | None" = None
    noise_reduction_intensity: "float | None" = None


@dataclass(frozen=True)
class BulkAudioEditResult:
    """Resultado de uma operação em lote sobre ``AUDIO_SETTINGS``."""

    requested: "tuple[str, ...]"
    updated: "tuple[str, ...]"
    unchanged: "tuple[str, ...]"
    failed: "tuple[tuple[str, str], ...]"


# ---------------------------------------------------------------------
# Helpers de validação
# ---------------------------------------------------------------------

def _as_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampoInvalidoError(
            f"{field_name} deve ser numérico (recebido: {value!r})"
        )
    result = float(value)
    if not math.isfinite(result):
        raise CampoInvalidoError(
            f"{field_name} deve ser um número finito (recebido: {value!r})"
        )
    return result


def _as_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CampoInvalidoError(
            f"{field_name} deve ser booleano (recebido: {value!r})"
        )
    return value


def _validate_range(value: float, field_name: str, bounds: "tuple[float, float]") -> float:
    low, high = bounds
    if not (low <= value <= high):
        raise CampoInvalidoError(
            f"{field_name} deve estar entre {low} e {high} (recebido: {value!r})"
        )
    return value


def _validate_optional_float(
    value: Any, field_name: str, bounds: "tuple[float, float]"
) -> "float | None":
    if value is None:
        return None
    parsed = _as_float(value, field_name)
    return _validate_range(parsed, field_name, bounds)


def _dedupe_preserve_order(values: Sequence[str]) -> "list[str]":
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _as_dict_or_none(payload: Any, field_name: str) -> "dict | None":
    """Guarda contra ``dict(payload)`` levantando ``ValueError`` cru
    quando ``payload`` é um valor corrompido não-Mapping persistido
    diretamente (mesmo padrão defensivo de ``visual_editor.py``)."""

    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise CampoInvalidoError(
            f"{field_name} persistido é inválido (esperado objeto, recebido: {payload!r})"
        )
    return dict(payload)


# ---------------------------------------------------------------------
# AudioEngine
# ---------------------------------------------------------------------

class AudioEngine:
    """Serviço de decisões de áudio de um Project. Lê/escreve
    exclusivamente através de ``EditProjectManager`` na categoria
    ``AUDIO_SETTINGS`` -- ver seção 0 da docstring do módulo."""

    def __init__(self, manager: EditProjectManager) -> None:
        if not isinstance(manager, EditProjectManager):
            raise TypeError(
                f"manager deve ser EditProjectManager (recebido: {type(manager)!r})"
            )
        self._manager = manager

    # -- leitura -------------------------------------------------------

    def get_audio_settings(self, project_id: str) -> AudioSettingsState:
        try:
            state = self._manager.get_category(project_id, AUDIO_SETTINGS)
        except ValueError as exc:
            # Mesmo achado do GATE ADVERSARIAL documentado em
            # ``_set_field``: project_id que não é um UUID válido não
            # pode deixar um ValueError cru escapar deste método público.
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        data = _as_dict_or_none(state.data if state is not None else None, "audio_settings")
        if data is None:
            return AudioSettingsState()

        normalization = _as_dict_or_none(data.get("normalization"), "normalization") or {}
        noise_reduction = _as_dict_or_none(data.get("noise_reduction"), "noise_reduction") or {}

        return AudioSettingsState(
            volume=data.get("volume"),
            mute=data.get("mute"),
            gain=data.get("gain"),
            normalization_enabled=normalization.get("enabled"),
            normalization_target_lufs=normalization.get("target_lufs"),
            clipping_protection=data.get("clipping_protection"),
            fade_in_seconds=data.get("fade_in_seconds"),
            fade_out_seconds=data.get("fade_out_seconds"),
            noise_reduction_enabled=noise_reduction.get("enabled"),
            noise_reduction_intensity=noise_reduction.get("intensity"),
        )

    # -- setters individuais --------------------------------------------

    def set_volume(self, project_id: str, volume: Any) -> AudioSettingsState:
        value = _validate_range(_as_float(volume, "volume"), "volume", VOLUME_RANGE)
        return self._set_field(project_id, "volume", value)

    def set_mute(self, project_id: str, mute: Any) -> AudioSettingsState:
        value = _as_bool(mute, "mute")
        return self._set_field(project_id, "mute", value)

    def set_gain(self, project_id: str, gain: Any) -> AudioSettingsState:
        value = _validate_range(_as_float(gain, "gain"), "gain", GAIN_RANGE)
        return self._set_field(project_id, "gain", value)

    def set_clipping_protection(self, project_id: str, enabled: Any) -> AudioSettingsState:
        value = _as_bool(enabled, "clipping_protection")
        return self._set_field(project_id, "clipping_protection", value)

    def set_fade_in(self, project_id: str, seconds: Any) -> AudioSettingsState:
        value = _validate_range(_as_float(seconds, "fade_in_seconds"), "fade_in_seconds", FADE_RANGE)
        return self._set_field(project_id, "fade_in_seconds", value)

    def set_fade_out(self, project_id: str, seconds: Any) -> AudioSettingsState:
        value = _validate_range(_as_float(seconds, "fade_out_seconds"), "fade_out_seconds", FADE_RANGE)
        return self._set_field(project_id, "fade_out_seconds", value)

    def set_normalization(
        self, project_id: str, enabled: Any, target_lufs: Any = None
    ) -> AudioSettingsState:
        enabled_value = _as_bool(enabled, "normalization.enabled")
        target_value = _validate_optional_float(
            target_lufs, "normalization.target_lufs", TARGET_LUFS_RANGE
        )
        payload = {"enabled": enabled_value, "target_lufs": target_value}
        return self._set_field(project_id, "normalization", payload)

    def set_noise_reduction(
        self, project_id: str, enabled: Any, intensity: Any = 0.0
    ) -> AudioSettingsState:
        enabled_value = _as_bool(enabled, "noise_reduction.enabled")
        intensity_value = _validate_range(
            _as_float(intensity, "noise_reduction.intensity"),
            "noise_reduction.intensity",
            NOISE_INTENSITY_RANGE,
        )
        payload = {"enabled": enabled_value, "intensity": intensity_value}
        return self._set_field(project_id, "noise_reduction", payload)

    # -- batch -----------------------------------------------------------

    def bulk_set_audio_settings(
        self, project_ids: Sequence[str], **fields: Any
    ) -> BulkAudioEditResult:
        """Aplica os mesmos campos, já validados, a múltiplos Projects.
        Cada campo aceito é validado ANTES de qualquer escrita (nenhuma
        escrita parcial se um campo for inválido). Campos aceitos:
        ``volume``, ``mute``, ``gain``, ``clipping_protection``,
        ``fade_in_seconds``, ``fade_out_seconds`` (escalares); e os pares
        ``normalization``/``normalization_target_lufs`` e
        ``noise_reduction_enabled``/``noise_reduction_intensity`` para os
        campos aninhados."""

        requested = tuple(_dedupe_preserve_order(project_ids))
        if not requested:
            raise CampoInvalidoError("project_ids não pode ser vazio")
        if not fields:
            raise CampoInvalidoError("nenhum campo fornecido para atualização em lote")

        validated = self._validate_bulk_fields(fields)

        updated: "list[str]" = []
        unchanged: "list[str]" = []
        failed: "list[tuple[str, str]]" = []

        for project_id in requested:
            changed_flag: "list[bool]" = []

            def mutator(current_data: Any, _flag: "list[bool]" = changed_flag) -> dict:
                merged = dict(current_data) if isinstance(current_data, Mapping) else {}
                before = dict(merged)
                merged.update(validated)
                _flag.append(merged != before)
                return merged

            try:
                self._manager.update_category(project_id, AUDIO_SETTINGS, mutator)
            except ProjectNaoEncontradoError as exc:
                failed.append((project_id, exc.code))
                continue
            except ValueError:
                failed.append((project_id, "PROJETO_ID_INVALIDO"))
                continue

            if changed_flag and changed_flag[0]:
                updated.append(project_id)
            else:
                unchanged.append(project_id)

        return BulkAudioEditResult(
            requested=requested,
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            failed=tuple(failed),
        )

    def _validate_bulk_fields(self, fields: Mapping[str, Any]) -> dict:
        validated: dict = {}
        remaining = dict(fields)

        if "volume" in remaining:
            validated["volume"] = _validate_range(
                _as_float(remaining.pop("volume"), "volume"), "volume", VOLUME_RANGE
            )
        if "mute" in remaining:
            validated["mute"] = _as_bool(remaining.pop("mute"), "mute")
        if "gain" in remaining:
            validated["gain"] = _validate_range(
                _as_float(remaining.pop("gain"), "gain"), "gain", GAIN_RANGE
            )
        if "clipping_protection" in remaining:
            validated["clipping_protection"] = _as_bool(
                remaining.pop("clipping_protection"), "clipping_protection"
            )
        if "fade_in_seconds" in remaining:
            validated["fade_in_seconds"] = _validate_range(
                _as_float(remaining.pop("fade_in_seconds"), "fade_in_seconds"),
                "fade_in_seconds",
                FADE_RANGE,
            )
        if "fade_out_seconds" in remaining:
            validated["fade_out_seconds"] = _validate_range(
                _as_float(remaining.pop("fade_out_seconds"), "fade_out_seconds"),
                "fade_out_seconds",
                FADE_RANGE,
            )
        if "normalization" in remaining or "normalization_target_lufs" in remaining:
            if "normalization" not in remaining:
                raise CampoInvalidoError(
                    "normalization_target_lufs requer normalization (enabled) no mesmo lote"
                )
            enabled_value = _as_bool(remaining.pop("normalization"), "normalization.enabled")
            target_raw = remaining.pop("normalization_target_lufs", None)
            target_value = _validate_optional_float(
                target_raw, "normalization.target_lufs", TARGET_LUFS_RANGE
            )
            validated["normalization"] = {"enabled": enabled_value, "target_lufs": target_value}
        if "noise_reduction_enabled" in remaining or "noise_reduction_intensity" in remaining:
            if "noise_reduction_enabled" not in remaining:
                raise CampoInvalidoError(
                    "noise_reduction_intensity requer noise_reduction_enabled no mesmo lote"
                )
            enabled_value = _as_bool(
                remaining.pop("noise_reduction_enabled"), "noise_reduction.enabled"
            )
            intensity_raw = remaining.pop("noise_reduction_intensity", 0.0)
            intensity_value = _validate_range(
                _as_float(intensity_raw, "noise_reduction.intensity"),
                "noise_reduction.intensity",
                NOISE_INTENSITY_RANGE,
            )
            validated["noise_reduction"] = {
                "enabled": enabled_value,
                "intensity": intensity_value,
            }

        if remaining:
            unknown = ", ".join(sorted(remaining))
            raise CampoInvalidoError(f"campo(s) desconhecido(s) para atualização em lote: {unknown}")

        return validated

    # -- interno -----------------------------------------------------------

    def _set_field(self, project_id: str, field_name: str, value: Any) -> AudioSettingsState:
        def mutator(current_data: Any) -> dict:
            merged = dict(current_data) if isinstance(current_data, Mapping) else {}
            merged[field_name] = value
            return merged

        try:
            self._manager.update_category(project_id, AUDIO_SETTINGS, mutator)
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            # Achado no GATE ADVERSARIAL: um project_id que não é sequer
            # um UUID válido faz o LocalDatabase levantar um ValueError
            # cru ("entity_id deve ser UUID válido"), diferente de
            # ProjectNaoEncontradoError (UUID válido mas inexistente).
            # Nenhum ValueError cru pode escapar de um método público --
            # reclassificado como erro estruturado deste módulo.
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        return self.get_audio_settings(project_id)
