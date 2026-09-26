"""CaptionsStyleEngine -- Prompt 32 (Fase 5: Editor Modular Não Destrutivo).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie sistema de estilos de legenda. Configurar: font, size, position,
    alignment, stroke, shadow, background, max words, lines, animation,
    current-word highlight. Criar no mínimo três estilos profissionais.
    Nada hardcoded no renderer.

=======================================================================
0. VISÃO GERAL E ESCOPO -- VOLTA AO PADRÃO PURO DE DECISÃO
=======================================================================

Este módulo é 100% decisão -- MESMO padrão de ``timeline_editor.py``
(Prompt 28), ``visual_editor.py`` (Prompt 29) e ``audio_engine.py``
(Prompt 30), NUNCA o padrão do Prompt 31 (``captions_engine.py``), que
tinha uma metade de processamento real (Whisper). Não existe hoje nenhum
``RenderEngine``/``def render`` no produto (confirmado por leitura direta
de ``_sistema/`` nesta sessão) -- "nada hardcoded no renderer" é,
portanto, uma restrição sobre o MODELO DE DADOS aqui, pensando num
renderer futuro que ainda não existe: todo valor visual (cor, posição,
tamanho, timing de animação etc.) tem que vir de configuração persistida
por este módulo, nunca ser uma constante mágica embutida em código de
renderização futuro. Isso NÃO autoriza construir um renderer agora.

Este módulo NUNCA chama ``subprocess``/``ffmpeg``/``ffprobe`` (não
processa mídia -- só grava decisões), nunca lê/escreve/move/renomeia
qualquer arquivo referenciado por ``SourceAsset.local_path`` (diferente
do Prompt 31 Parte B -- este módulo é só decisão, sem exceção para tocar
o arquivo original), nunca cria ``Job``/``Artifact``/``Video``/
``Publication``/``Schedule``, nunca importa ``circuit_breaker``/
``retry_policy``/``publication_idempotency``/``secrets_manager``/
``domain.job_state_machine``/nada de Geração 1, nunca abre uma
``transaction()`` própria nem acessa ``Project.edit_state``/
``edit_state_json``/métodos de ``LocalDatabase`` diretamente -- toda
leitura/escrita passa exclusivamente por ``EditProjectManager``.

``set_mode``/``get_mode`` de ``captions_engine.py`` (Prompt 31,
``OFF``/``SEPARATE``/``BURNED``) continuam sendo a única parte "real" do
sistema de legendas -- este módulo só adiciona a APARÊNCIA da legenda
quando exibida, nunca decide SE ela é exibida. Este módulo NUNCA importa
``captions_engine.py``/``visual_editor.py``/``audio_engine.py``/
``timeline_editor.py``/``media_catalog.py`` -- zero acoplamento entre
módulos irmãos da Fase 5, cada um funciona isoladamente (Princípio A do
CLAUDE.md).

=======================================================================
0.1 -- POR QUE UMA CATEGORIA NOVA (``captions_style``), NÃO ``CAPTIONS``
     NEM ``TEXT_LAYERS``
=======================================================================

Investigação obrigatória concluída (``edit_project.py``, categorias já
reservadas desde o Prompt 27): ``CAPTIONS`` (``"captions"``) e
``TEXT_LAYERS`` (``"text_layers"``) já existem como convenções de
nomenclatura, mas NENHUMA das duas é o lugar certo aqui:

- ``CAPTIONS`` já tem um contrato definido e em uso real desde o Prompt
  31: ``{"mode": "OFF"|"SEPARATE"|"BURNED"}`` (ver
  ``captions_engine.py``, ``CaptionsEngine.set_mode``/``get_mode``, que
  já lê/escreve essa categoria via ``EditProjectManager.update_category``
  com um mutator que só toca a chave ``"mode"``). Misturar ``style``
  dentro da mesma categoria arriscaria colisão de escrita entre
  ``captions_engine.py`` e este módulo novo -- os dois módulos passariam
  a competir pela mesma categoria sem coordenação (o mesmo tipo de risco
  de *lost update*/mistura de contratos que ``edit_project.py`` já evita
  por categoria isolada; misturar dois contratos DENTRO da mesma
  categoria reintroduziria esse risco a um nível mais alto). Também
  quebraria a garantia central de ``edit_project.py`` de que uma
  categoria representa UM contrato coerente, não dois contratos não
  relacionados que só coincidem em "tem a ver com legenda".
- ``TEXT_LAYERS``, pelo nome e pela lista de exemplos do roadmap do
  Prompt 27 ("cuts, speed, crop, reframe, audio settings, captions,
  template, text layers, metadata mode" -- itens PARALELOS, não
  hierárquicos, cada um citado uma vez, nunca um sendo sub-item de
  outro), parece destinada a camadas de texto GENÉRICAS na tela
  (títulos, callouts, texto livre sobreposto ao vídeo), não à aparência
  da legenda TRANSCRITA especificamente. Estilizar a legenda transcrita
  (que já tem sua própria categoria, ``CAPTIONS``, desde o Prompt 27) é
  conceitualmente distinto de posicionar uma camada de texto arbitrária
  que o usuário adiciona manualmente -- usar ``TEXT_LAYERS`` aqui seria
  forçar dois conceitos diferentes na mesma categoria, na direção oposta
  do problema anterior.

Decisão: categoria NOVA, ``CAPTIONS_STYLE = "captions_style"``.
Precedente já aceito no produto: ``VisualEditor`` (Prompt 29) criou
``VISUAL_ADJUSTMENTS = "visual_adjustments"`` fora da lista original de
nove convenções quando nenhuma categoria existente servia, sem exigir
nenhuma migration (categoria é só uma string dentro de
``edit_state_json["categories"]``, nunca uma coluna própria --
``_validate_category_name`` em ``edit_project.py`` aceita qualquer
string não vazia, nunca um enum fechado de nomes). Nenhuma mudança em
``EditProjectManager`` foi necessária para isso, mesma trilha já usada
pelo Prompt 29.

=======================================================================
0.2 -- DISTINÇÃO: CAIXA DE LEGENDA (este Prompt) x ZONA DE TEMPLATE
     (Prompt 39, futuro)
=======================================================================

Investigação obrigatória concluída (releitura da seção do roadmap do
Prompt 39 -- Layout Mapper, "VIDEO, CAPTION, AI_TEXT, IMAGE/LOGO"): esse
Prompt futuro define ZONAS de um TEMPLATE (onde, dentro de uma imagem de
template importada, cada tipo de conteúdo pode aparecer -- um conceito
de layout de template, independente de vídeo específico). O campo
``position`` deste módulo é outra coisa: é o ponto de ANCORAGEM da
legenda transcrita DENTRO DO FRAME DO VÍDEO em si (a mesma superfície
onde ``crop``/``zoom``/``rotation`` do Prompt 29 já operam), não uma zona
de template. Os dois conceitos são relacionados (ambos acabam
determinando "onde a legenda aparece na tela"), mas não são o mesmo dado
nem a mesma categoria -- este Prompt não antecipa nem depende do formato
que o Layout Mapper venha a usar; quando o Prompt 39 existir, ele decidirá
como (ou se) reconciliar zona de template com posição de legenda, sem que
este módulo precise ser tocado.

=======================================================================
0.3 -- PADRÃO PRESET NOMEADO + CUSTOM (mesmo espírito do Prompt 24)
=======================================================================

``source_import.py`` (Prompt 24) já estabeleceu o padrão conceitual
PRESET NOMEADO + ``CUSTOM`` no produto (``ORIGINAL``/``ALREADY_EDITED``/
``READY_FOR_AI``/``USER_MARKED_READY``/``CUSTOM``, ver ``PRESETS``/
``_PRESET_ASSERTIONS`` naquele módulo). Reaproveitado aqui EM ESPÍRITO
(mesma ideia de presets nomeados com valores concretos definidos em
código, nunca inventados a cada chamada, mais a possibilidade de um
estilo inteiramente customizado), mas SEM importar nada de
``source_import.py`` (módulos irmãos da Fase 5 não se importam entre si,
seção 0). Diferente de ``source_import.py``, este módulo não tem uma
constante de preset chamada ``CUSTOM`` -- não é necessária: um estilo
"customizado" aqui é simplesmente o resultado de ``set_style``/
``bulk_set_style`` (campo a campo ou em lote), sem nenhum "modo custom"
explícito para acionar -- a categoria ``captions_style`` sempre reflete
o último valor gravado, seja por preset ou por campo individual, sem
precisar de uma flag adicional dizendo qual dos dois caminhos foi usado
por último (informação que não teria nenhum consumidor real hoje).

=======================================================================
0.4 -- API: UM ÚNICO ``set_style(project_id, **campos)`` GENÉRICO, SEM
     SETTERS INDIVIDUAIS POR CAMPO
=======================================================================

O Prompt sugere explicitamente ``set_style(project_id, **campos)`` "ou o
formato que decidir, desde que documentado". Decisão: usar exatamente
esse formato genérico como ÚNICA via de escrita campo-a-campo (em vez de
replicar o padrão de ``audio_engine.py``/``visual_editor.py``, que têm um
``set_*`` dedicado por campo escalar). Motivo: a maioria dos campos deste
módulo é um OBJETO aninhado (``position``, ``stroke``, ``shadow``,
``background``, ``current_word_highlight``), não um escalar solto -- a
validação de cada campo já precisa estar centralizada em
``_validate_style_fields`` para ser reaproveitada tanto por
``set_style`` (um projeto) quanto por ``bulk_set_style`` (vários
projetos) quanto pela CONSTRUÇÃO dos próprios presets (seção 0.5,
``require_all=True``); criar N métodos ``set_font``/``set_size``/... só
para chamar essa mesma função central um campo de cada vez adicionaria
superfície de API duplicada sem nenhum ganho de validação (diferente de
``audio_engine.py``, onde a maioria dos campos é escalar e setters
individuais têm valor de legibilidade real). ``set_style(project_id,
font="Arial")`` já cobre "campo a campo" (um único kwarg) sem exigir
métodos extras; ``set_style(project_id, font="Arial", size=40.0)`` cobre
"em lote de campos" (para o mesmo projeto). Decisão documentada aqui
como pedido pelo Prompt, não uma omissão silenciosa.

=======================================================================
0.5 -- CADA CAMPO, COM FORMATO E JUSTIFICATIVA
=======================================================================

--- ``font`` (string livre, unicode permitido) ---
Nome/família da fonte. Decisão explícita: este Prompt NÃO valida se a
fonte existe de fato instalada no sistema -- isso é preocupação de uma
etapa futura de gerenciamento de assets/fontes (fora de escopo aqui,
igual a como ``captions_engine.py`` nunca valida se ``faster-whisper``
está instalado além de isolar o import). Único limite aplicado: string
não vazia, comprimento máximo de sanidade de produto
(``FONT_NAME_MAX_LEN = 200``, nunca um limite de existência de fonte).
Unicode/emoji são aceitos livremente (diferente de ``alignment``/
``animation``, que são vocabulário FECHADO) -- um nome de fonte
localizado (ex.: CJK) é um caso real de produto internacional (Princípio
L do CLAUDE.md).

--- ``size`` (float, range validado) ---
Tamanho da fonte, em unidades abstratas (não pixels -- pixels exigiriam
saber a resolução final de render, indisponível, mesmo motivo pelo qual
``visual_editor.py`` usa coordenadas normalizadas em vez de pixels
absolutos para geometria). Faixa ``SIZE_RANGE = (8.0, 200.0)``: teto de
produto generoso para legendas de vídeo curto sem permitir valores
absurdos, mesmo espírito das faixas de ``visual_editor.py``/
``audio_engine.py`` (documentadas como decisão de produto, não derivadas
de nenhum arquivo real).

--- ``position`` (``{"x": 0.0-1.0, "y": 0.0-1.0}``) ---
Ponto de ANCORAGEM da caixa de legenda dentro do frame do vídeo, em
coordenadas normalizadas -- reaproveita a MESMA convenção já usada por
``crop.x``/``crop.y`` em ``visual_editor.py`` (fração absoluta do frame,
``0.0``-``1.0``), item 3 da investigação obrigatória. DELIBERADAMENTE
DIFERENTE do campo ``position`` de ``visual_editor.py`` (que representa
um DESLOCAMENTO/offset, ``-1.0``-``1.0``) -- são dois conceitos
diferentes que compartilham nome por coincidência em módulos diferentes;
aqui ``position`` é um ponto absoluto de ancoragem (onde o CENTRO da
caixa de legenda fica no frame), nunca um deslocamento relativo a outra
posição. Este módulo NÃO modela largura/altura da caixa de legenda --
essas dimensões dependem de métricas reais de texto renderizado (fonte,
tamanho, comprimento da string) que só um renderer futuro pode calcular;
modelar aqui seria inventar geometria sem informação real para
sustentá-la (mesmo tipo de adiamento documentado que
``audio_engine.py`` já aplica a ``background_music``, seção 0.6 daquele
módulo).

--- ``alignment`` (vocabulário fechado: ``LEFT``/``CENTER``/``RIGHT``) ---
Alinhamento horizontal do texto dentro da caixa de legenda. Vocabulário
fechado, nunca string livre (item do Prompt) -- mesmo padrão de
``fit_mode``/``rotation`` em ``visual_editor.py``.

--- ``stroke`` (``{"enabled": bool, "color": <hex>, "width": 0.0-20.0}``) ---
Contorno do texto. Forma escolhida: objeto com interruptor + cor +
espessura (em vez de campos soltos no nível superior), porque os três só
fazem sentido semântico juntos -- mesmo raciocínio de coerência já usado
por ``normalization``/``noise_reduction`` em ``audio_engine.py`` (evita
um ``width`` órfão sem ``enabled`` correspondente). ``width`` em
unidades abstratas (mesmo motivo de ``size``), faixa ``(0.0, 20.0)``.

--- ``shadow`` (``{"enabled": bool, "color": <hex>, "blur": 0.0-20.0,
     "offset_x": -20.0-20.0, "offset_y": -20.0-20.0}``) ---
Sombra do texto. Mesma forma de decisão de ``stroke`` (objeto coerente),
com os dois parâmetros adicionais que uma sombra realista precisa
(deslocamento e desfoque) -- não é overengineering: são os parâmetros
mínimos padrão de qualquer sombra de texto (CSS ``text-shadow``, After
Effects, etc.), sem os quais "sombra" seria apenas uma segunda cópia de
``stroke`` sem função própria. Unidades abstratas, mesma faixa de
``stroke.width`` para ``blur``; deslocamento simétrico ``(-20.0, 20.0)``
permite sombra em qualquer direção.

--- ``background`` (``{"enabled": bool, "color": <hex>, "opacity":
     0.0-1.0}``) ---
Caixa de fundo atrás do texto. Forma EXIGIDA explicitamente pelo texto
do Prompt ("habilitado/desabilitado + cor e opacidade"), diferente de
``stroke``/``shadow`` (onde a opacidade, se necessária, pode vir do
próprio canal alfa de ``#RRGGBBAA`` -- ver decisão de formato de cor
abaixo). Aqui ``opacity`` é mantido como um campo SEPARADO do alfa da
cor porque o próprio Prompt pede os dois explicitamente lado a lado;
documentado para não parecer redundância acidental: se ``color`` for
``#RRGGBBAA``, o alfa da cor é o tingimento próprio da cor escolhida,
enquanto ``opacity`` é um multiplicador de opacidade do BLOCO inteiro por
cima disso (composição ``alfa_efetivo = alfa_da_cor * opacity``,
responsabilidade de um renderer futuro combinar os dois -- este módulo
só grava os dois valores, nunca os combina).

--- CORES: formato único em todo o módulo ---
``#RRGGBB`` (6 dígitos hex) OU ``#RRGGBBAA`` (8 dígitos hex, com canal
alfa) -- um único formato, validado explicitamente por regex
(``^#[0-9A-Fa-f]{6}$`` ou ``^#[0-9A-Fa-f]{8}$``). Uma cor
estruturalmente inválida (comprimento errado, caracteres não-hex, `#`
ausente) nunca é aceita silenciosamente -- levanta ``CorInvalidaError``.
Decisão adicional: a cor validada é normalizada para maiúsculas antes de
ser persistida (``#abc123`` grava como ``#ABC123``) -- forma canônica
única evita que a mesma cor produza fingerprints diferentes por causa só
de caixa alta/baixa (mesmo espírito de canonicalização já aplicado ao
JSON de ``edit_project.py``, aqui aplicado ao nível do valor de cada
campo).

--- ``max_words`` (int, range validado) ---
Número máximo de palavras exibidas por vez. ``MAX_WORDS_RANGE = (1, 20)``
-- teto de produto para legendas de vídeo curto (uma legenda com
dezenas de palavras simultâneas não é um caso de uso real desta
categoria de produto).

--- ``lines`` (int, range validado) ---
Número máximo de linhas simultâneas. ``LINES_RANGE = (1, 5)`` -- mesmo
raciocínio de teto de produto.

--- ``animation`` (vocabulário fechado: ``NONE``/``FADE``/``POP``/
     ``SLIDE``) ---
Vocabulário fechado, nunca string livre. ``SLIDE`` acrescentado além dos
três exemplos literais do roadmap (``NONE``/``FADE``/``POP``) por ser
uma animação de legenda genuinamente comum em produto real (entrada
deslizante) -- decisão documentada, não um valor inventado sem
propósito; nenhuma OUTRA animação além dessas quatro é aceita nesta
etapa (vocabulário fechado real, não uma lista aberta disfarçada).

--- ``current_word_highlight`` (``{"enabled": bool, "color": <hex>}``) ---
Karaokê/palavra-atual-destacada. Forma mínima suficiente: interruptor +
cor de destaque, mesmo formato de cor do resto do módulo. Não modela
timing/sincronização da palavra atual aqui -- isso depende dos
timestamps por palavra que só uma transcrição real (Prompt 31) e um
renderer futuro podem calcular/aplicar; este módulo só decide SE o
karaokê deve ser aplicado e COM QUE COR, nunca QUANDO cada palavra
acende.

=======================================================================
0.6 -- PRESETS PROFISSIONAIS (mínimo 3, exigido pelo roadmap)
=======================================================================

Três presets nomeados como constantes de módulo, com valores completos e
DISTINTOS entre si em vários campos (nunca o mesmo preset com nome
diferente):

- ``PRESET_CLASSIC`` ("CLASSIC"): legível, discreto -- contorno fino
  preto, sem fundo, sem karaokê, sem animação, posição inferior central,
  fonte/tamanho moderados, até 2 linhas.
- ``PRESET_BOLD_SOCIAL`` ("BOLD_SOCIAL"): chamativo, estilo redes
  sociais -- fonte grande, contorno grosso, sombra ativada, fundo
  semi-opaco ativado, animação ``POP``, poucas palavras por vez (1
  linha), posição central da tela.
- ``PRESET_KARAOKE`` ("KARAOKE"): ``current_word_highlight`` ativado com
  cor de destaque viva, animação ``FADE``, posição inferior central, sem
  fundo.

Cada preset é construído em código chamando a MESMA função de validação
central usada por ``set_style``/``bulk_set_style``
(``_validate_style_fields(..., require_all=True)``) -- garante que um
preset nunca poderia representar um estado que a validação normal
rejeitaria, e evita duplicar lógica de validação entre "campo
individual" e "preset completo". Os valores concretos ficam em
``_PRESET_RAW_FIELDS`` (dict Python literal, nunca inventados a cada
chamada) e são validados UMA VEZ, na importação do módulo -- um erro de
construção de preset falha imediatamente ao carregar o módulo (falha
rápida), nunca silenciosamente em produção.

=======================================================================
0.7 -- OPERAÇÃO EM LOTE
=======================================================================

Mesmo padrão já estabelecido em ``MediaCatalogService.bulk_edit``
(Prompt 27.5), ``VisualEditor.bulk_set_frame``/``bulk_set_adjustments``
(Prompt 29) e ``AudioEngine.bulk_set_audio_settings`` (Prompt 30):
``bulk_set_style(project_ids, **campos)`` e
``bulk_apply_preset(project_ids, preset_name)``, ambos devolvendo
``BulkCaptionsStyleResult`` (``requested``/``updated``/``unchanged``/
``failed``). Em ambos, a validação (dos campos, ou do nome do preset) é
feita UMA ÚNICA VEZ antes de tocar qualquer projeto -- falha rápida e
idêntica para todos, nunca uma escrita parcial seguida de uma falha de
validação tardia. Um projeto inexistente/inválido NO MEIO do lote nunca
aborta os demais (cada item é isolado -- Princípio "CONFIABILIDADE" do
CLAUDE.md: "falha de um vídeo não derruba o lote").

=======================================================================
0.8 -- ERROS ESTRUTURADOS
=======================================================================

Nenhum ``ValueError``/``KeyError`` cru escapa de um método público.
Quatro subtipos, cada um com um propósito distinto (mais granular que
``audio_engine.py``, porque este módulo tem vocabulário fechado E cor
estruturada, nenhum dos dois presentes naquele módulo):

- ``CampoInvalidoError``: tipo errado, fora de range, campo
  desconhecido, ``project_id`` malformado.
- ``VocabularioInvalidoError``: ``alignment``/``animation`` fora do
  vocabulário fechado.
- ``CorInvalidaError``: formato de cor estruturalmente inválido.
- ``PresetInvalidoError``: nome de preset desconhecido em
  ``apply_preset``/``bulk_apply_preset``.

``bool`` é sempre explicitamente rejeitado onde um ``float``/``int`` é
esperado (``isinstance(x, bool)`` verificado ANTES de
``isinstance(x, (int, float))``, mesma armadilha já documentada e
testada em ``timeline_editor.py``/``visual_editor.py``/
``audio_engine.py``). ``inf``/``-inf``/``nan`` são sempre rejeitados
estruturalmente antes de qualquer checagem de intervalo. ``project_id``
que não é sequer um UUID válido (``ValueError`` cru de
``LocalDatabase``) nunca escapa -- reclassificado como
``CampoInvalidoError`` em todo método público, regressão já virando
padrão desde o Prompt 30.

=======================================================================
0.9 -- MIGRATIONS
=======================================================================

Nenhuma migration nova. ``LATEST_SCHEMA_VERSION`` continua ``9``. A
categoria ``captions_style`` é só uma string dentro do JSON já existente
de ``edit_state_json`` (coluna congelada desde ``m001_initial.py``,
schema comportando qualquer JSON) -- mesmo raciocínio já aplicado pelos
Prompts 28-31 Parte A.

=======================================================================
1. USO
=======================================================================

    from _sistema.captions_style import CaptionsStyleEngine, PRESET_BOLD_SOCIAL
    from _sistema.edit_project import EditProjectManager

    manager = EditProjectManager(database)
    engine = CaptionsStyleEngine(manager)

    engine.apply_preset(project_id, PRESET_BOLD_SOCIAL)
    engine.set_style(project_id, size=44.0, alignment="CENTER")
    style = engine.get_style(project_id)

    engine.bulk_apply_preset([id1, id2, id3], PRESET_BOLD_SOCIAL)
    engine.bulk_set_style([id1, id2], max_words=4, lines=1)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence

from .edit_project import EditProjectManager, ProjectNaoEncontradoError

# ---------------------------------------------------------------------
# Categoria (string livre nova -- ver seção 0.1 da docstring do módulo).
# ---------------------------------------------------------------------

CAPTIONS_STYLE = "captions_style"

# ---------------------------------------------------------------------
# Vocabulários fechados e faixas -- ver seção 0.5 da docstring do módulo.
# ---------------------------------------------------------------------

ALIGNMENTS = ("LEFT", "CENTER", "RIGHT")
ANIMATIONS = ("NONE", "FADE", "POP", "SLIDE")

SIZE_RANGE = (8.0, 200.0)
POSITION_RANGE = (0.0, 1.0)
STROKE_WIDTH_RANGE = (0.0, 20.0)
SHADOW_BLUR_RANGE = (0.0, 20.0)
SHADOW_OFFSET_RANGE = (-20.0, 20.0)
BACKGROUND_OPACITY_RANGE = (0.0, 1.0)
MAX_WORDS_RANGE = (1, 20)
LINES_RANGE = (1, 5)
FONT_NAME_MAX_LEN = 200

_COLOR_RE_6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
_COLOR_RE_8 = re.compile(r"^#[0-9A-Fa-f]{8}$")

# ---------------------------------------------------------------------
# Presets (nomes) -- ver seção 0.6 da docstring do módulo.
# ---------------------------------------------------------------------

PRESET_CLASSIC = "CLASSIC"
PRESET_BOLD_SOCIAL = "BOLD_SOCIAL"
PRESET_KARAOKE = "KARAOKE"
PRESETS = frozenset({PRESET_CLASSIC, PRESET_BOLD_SOCIAL, PRESET_KARAOKE})


# ---------------------------------------------------------------------------
# Erros estruturados -- ver seção 0.8 da docstring do módulo.
# ---------------------------------------------------------------------------
class CaptionsStyleError(RuntimeError):
    """Base para todo erro estruturado deste módulo -- nunca um
    ``ValueError``/``KeyError`` cru escapa de um método público de
    ``CaptionsStyleEngine``. Erros de ``EditProjectManager`` (ex.:
    ``ProjectNaoEncontradoError``) já são estruturados e se propagam sem
    modificação."""

    code: ClassVar[str] = "CAPTIONS_STYLE_ERRO"


class CampoInvalidoError(CaptionsStyleError):
    """Um campo tem tipo errado, está fora de intervalo, não é finito
    (inf/-inf/nan), é um campo desconhecido, ou ``project_id`` é
    malformado."""

    code: ClassVar[str] = "CAMPO_INVALIDO"


class VocabularioInvalidoError(CaptionsStyleError):
    """``alignment``/``animation`` fora do vocabulário fechado
    documentado."""

    code: ClassVar[str] = "VOCABULARIO_INVALIDO"


class CorInvalidaError(CaptionsStyleError):
    """Cor estruturalmente inválida: comprimento errado, caracteres
    não-hexadecimais, ou ``#`` ausente."""

    code: ClassVar[str] = "COR_INVALIDA"


class PresetInvalidoError(CaptionsStyleError):
    """Nome de preset desconhecido em ``apply_preset``/
    ``bulk_apply_preset``."""

    code: ClassVar[str] = "PRESET_INVALIDO"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CaptionsStyleState:
    """Snapshot somente-leitura da categoria ``captions_style`` de um
    Project. Todos os campos são ``Optional`` -- ``None`` significa "não
    definido ainda", nunca um valor neutro implícito (mesmo contrato de
    ``AudioSettingsState``/``FrameState``)."""

    font: "str | None" = None
    size: "float | None" = None
    position: "dict[str, float] | None" = None
    alignment: "str | None" = None
    stroke: "dict[str, Any] | None" = None
    shadow: "dict[str, Any] | None" = None
    background: "dict[str, Any] | None" = None
    max_words: "int | None" = None
    lines: "int | None" = None
    animation: "str | None" = None
    current_word_highlight: "dict[str, Any] | None" = None


@dataclass(frozen=True)
class BulkCaptionsStyleResult:
    """Resultado de uma operação em lote (``bulk_set_style``/
    ``bulk_apply_preset``)."""

    requested: "tuple[str, ...]"
    updated: "tuple[str, ...]"
    unchanged: "tuple[str, ...]"
    failed: "tuple[tuple[str, str], ...]"


# ---------------------------------------------------------------------------
# Helpers de validação -- tipo/range
# ---------------------------------------------------------------------------
def _as_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampoInvalidoError(f"{field_name} deve ser numérico (recebido: {value!r})")
    result = float(value)
    if not math.isfinite(result):
        raise CampoInvalidoError(f"{field_name} deve ser um número finito (recebido: {value!r})")
    return result


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampoInvalidoError(f"{field_name} deve ser um inteiro (recebido: {value!r})")
    return value


def _as_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CampoInvalidoError(f"{field_name} deve ser booleano (recebido: {value!r})")
    return value


def _as_str(value: Any, field_name: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampoInvalidoError(
            f"{field_name} deve ser uma string não vazia (recebido: {value!r})"
        )
    if len(value) > max_len:
        raise CampoInvalidoError(
            f"{field_name} excede o comprimento máximo de {max_len} caracteres"
        )
    return value


def _validate_range(value: float, field_name: str, bounds: "tuple[float, float]") -> float:
    low, high = bounds
    if not (low <= value <= high):
        raise CampoInvalidoError(f"{field_name} deve estar entre {low} e {high} (recebido: {value!r})")
    return value


def _validate_int_range(value: int, field_name: str, bounds: "tuple[int, int]") -> int:
    low, high = bounds
    if not (low <= value <= high):
        raise CampoInvalidoError(f"{field_name} deve estar entre {low} e {high} (recebido: {value!r})")
    return value


def _validate_color(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not (_COLOR_RE_6.match(value) or _COLOR_RE_8.match(value)):
        raise CorInvalidaError(
            f"{field_name} deve ser uma cor hex no formato #RRGGBB ou #RRGGBBAA "
            f"(recebido: {value!r})"
        )
    return value.upper()


def _validate_alignment(value: Any) -> str:
    if not isinstance(value, str) or value not in ALIGNMENTS:
        raise VocabularioInvalidoError(
            f"alignment deve ser um de {ALIGNMENTS}, recebido: {value!r}"
        )
    return value


def _validate_animation(value: Any) -> str:
    if not isinstance(value, str) or value not in ANIMATIONS:
        raise VocabularioInvalidoError(
            f"animation deve ser um de {ANIMATIONS}, recebido: {value!r}"
        )
    return value


def _require_mapping(payload: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise CampoInvalidoError(f"{field_name} deve ser um objeto (recebido: {payload!r})")
    return payload


def _validate_position(payload: Any) -> dict:
    payload = _require_mapping(payload, "position")
    x = _validate_range(_as_float(payload.get("x"), "position.x"), "position.x", POSITION_RANGE)
    y = _validate_range(_as_float(payload.get("y"), "position.y"), "position.y", POSITION_RANGE)
    return {"x": x, "y": y}


def _validate_stroke(payload: Any) -> dict:
    payload = _require_mapping(payload, "stroke")
    enabled = _as_bool(payload.get("enabled"), "stroke.enabled")
    color = _validate_color(payload.get("color"), "stroke.color")
    width = _validate_range(
        _as_float(payload.get("width"), "stroke.width"), "stroke.width", STROKE_WIDTH_RANGE
    )
    return {"enabled": enabled, "color": color, "width": width}


def _validate_shadow(payload: Any) -> dict:
    payload = _require_mapping(payload, "shadow")
    enabled = _as_bool(payload.get("enabled"), "shadow.enabled")
    color = _validate_color(payload.get("color"), "shadow.color")
    blur = _validate_range(
        _as_float(payload.get("blur"), "shadow.blur"), "shadow.blur", SHADOW_BLUR_RANGE
    )
    offset_x = _validate_range(
        _as_float(payload.get("offset_x"), "shadow.offset_x"), "shadow.offset_x", SHADOW_OFFSET_RANGE
    )
    offset_y = _validate_range(
        _as_float(payload.get("offset_y"), "shadow.offset_y"), "shadow.offset_y", SHADOW_OFFSET_RANGE
    )
    return {"enabled": enabled, "color": color, "blur": blur, "offset_x": offset_x, "offset_y": offset_y}


def _validate_background(payload: Any) -> dict:
    payload = _require_mapping(payload, "background")
    enabled = _as_bool(payload.get("enabled"), "background.enabled")
    color = _validate_color(payload.get("color"), "background.color")
    opacity = _validate_range(
        _as_float(payload.get("opacity"), "background.opacity"),
        "background.opacity",
        BACKGROUND_OPACITY_RANGE,
    )
    return {"enabled": enabled, "color": color, "opacity": opacity}


def _validate_current_word_highlight(payload: Any) -> dict:
    payload = _require_mapping(payload, "current_word_highlight")
    enabled = _as_bool(payload.get("enabled"), "current_word_highlight.enabled")
    color = _validate_color(payload.get("color"), "current_word_highlight.color")
    return {"enabled": enabled, "color": color}


def _validate_font(value: Any) -> str:
    return _as_str(value, "font", FONT_NAME_MAX_LEN)


def _validate_size(value: Any) -> float:
    return _validate_range(_as_float(value, "size"), "size", SIZE_RANGE)


def _validate_max_words(value: Any) -> int:
    return _validate_int_range(_as_int(value, "max_words"), "max_words", MAX_WORDS_RANGE)


def _validate_lines(value: Any) -> int:
    return _validate_int_range(_as_int(value, "lines"), "lines", LINES_RANGE)


# ---------------------------------------------------------------------------
# Registro central de validadores por campo -- reaproveitado por
# ``set_style``, ``bulk_set_style`` e pela construção dos presets
# (seção 0.6 da docstring do módulo).
# ---------------------------------------------------------------------------
_FIELD_VALIDATORS: "dict[str, Any]" = {
    "font": _validate_font,
    "size": _validate_size,
    "position": _validate_position,
    "alignment": _validate_alignment,
    "stroke": _validate_stroke,
    "shadow": _validate_shadow,
    "background": _validate_background,
    "max_words": _validate_max_words,
    "lines": _validate_lines,
    "animation": _validate_animation,
    "current_word_highlight": _validate_current_word_highlight,
}


def _validate_style_fields(fields: Mapping[str, Any], *, require_all: bool = False) -> dict:
    """Valida um sub-conjunto (ou o conjunto completo, se
    ``require_all=True``) dos campos conhecidos de ``captions_style``.
    Nenhum campo desconhecido é aceito silenciosamente -- ver seção 0.8
    da docstring do módulo."""
    validated: dict = {}
    remaining = dict(fields)
    for name, validator in _FIELD_VALIDATORS.items():
        if name in remaining:
            validated[name] = validator(remaining.pop(name))
        elif require_all:
            raise CampoInvalidoError(f"campo obrigatório ausente: {name!r}")
    if remaining:
        unknown = ", ".join(sorted(remaining))
        raise CampoInvalidoError(f"campo(s) desconhecido(s): {unknown}")
    return validated


def _dedupe_preserve_order(values: Sequence[str]) -> "list[str]":
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _as_dict_or_none(payload: Any, field_name: str) -> "dict | None":
    """Guarda contra um valor persistido corrompido (não-Mapping) --
    mesmo padrão defensivo de ``visual_editor.py``/``audio_engine.py``."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise CampoInvalidoError(
            f"{field_name} persistido é inválido (esperado objeto, recebido: {payload!r})"
        )
    return dict(payload)


# ---------------------------------------------------------------------------
# Presets -- valores concretos + validação na importação do módulo
# (seção 0.6 da docstring).
# ---------------------------------------------------------------------------
_PRESET_RAW_FIELDS: "dict[str, dict[str, Any]]" = {
    PRESET_CLASSIC: dict(
        font="Arial",
        size=32.0,
        position={"x": 0.5, "y": 0.85},
        alignment="CENTER",
        stroke={"enabled": True, "color": "#000000", "width": 2.0},
        shadow={"enabled": False, "color": "#000000", "blur": 0.0, "offset_x": 0.0, "offset_y": 0.0},
        background={"enabled": False, "color": "#000000", "opacity": 0.0},
        max_words=6,
        lines=2,
        animation="NONE",
        current_word_highlight={"enabled": False, "color": "#FFFFFF"},
    ),
    PRESET_BOLD_SOCIAL: dict(
        font="Montserrat",
        size=52.0,
        position={"x": 0.5, "y": 0.5},
        alignment="CENTER",
        stroke={"enabled": True, "color": "#000000", "width": 5.0},
        shadow={"enabled": True, "color": "#000000", "blur": 6.0, "offset_x": 2.0, "offset_y": 2.0},
        background={"enabled": True, "color": "#000000AA", "opacity": 0.6},
        max_words=3,
        lines=1,
        animation="POP",
        current_word_highlight={"enabled": False, "color": "#FFFF00"},
    ),
    PRESET_KARAOKE: dict(
        font="Poppins",
        size=40.0,
        position={"x": 0.5, "y": 0.9},
        alignment="CENTER",
        stroke={"enabled": True, "color": "#000000", "width": 3.0},
        shadow={"enabled": False, "color": "#000000", "blur": 0.0, "offset_x": 0.0, "offset_y": 0.0},
        background={"enabled": False, "color": "#000000", "opacity": 0.0},
        max_words=8,
        lines=1,
        animation="FADE",
        current_word_highlight={"enabled": True, "color": "#FFEE00"},
    ),
}

# Validados UMA VEZ na importação do módulo -- ver seção 0.6 da
# docstring. Um erro aqui falha imediatamente ao importar o módulo.
_PRESETS_VALIDATED: "dict[str, dict[str, Any]]" = {
    name: _validate_style_fields(raw, require_all=True) for name, raw in _PRESET_RAW_FIELDS.items()
}


# ---------------------------------------------------------------------------
# CaptionsStyleEngine
# ---------------------------------------------------------------------------
class CaptionsStyleEngine:
    """Serviço de decisões de estilo de legenda de um Project. Lê/escreve
    exclusivamente através de ``EditProjectManager`` na categoria
    ``captions_style`` -- ver seção 0 da docstring do módulo."""

    def __init__(self, manager: EditProjectManager) -> None:
        if not isinstance(manager, EditProjectManager):
            raise TypeError(f"manager deve ser EditProjectManager (recebido: {type(manager)!r})")
        self._manager = manager

    # -- leitura ---------------------------------------------------------

    def get_style(self, project_id: str) -> CaptionsStyleState:
        try:
            state = self._manager.get_category(project_id, CAPTIONS_STYLE)
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        data = _as_dict_or_none(state.data if state is not None else None, "captions_style")
        if data is None:
            return CaptionsStyleState()

        stroke = _as_dict_or_none(data.get("stroke"), "stroke")
        shadow = _as_dict_or_none(data.get("shadow"), "shadow")
        background = _as_dict_or_none(data.get("background"), "background")
        position = _as_dict_or_none(data.get("position"), "position")
        highlight = _as_dict_or_none(data.get("current_word_highlight"), "current_word_highlight")

        return CaptionsStyleState(
            font=data.get("font"),
            size=data.get("size"),
            position=position,
            alignment=data.get("alignment"),
            stroke=stroke,
            shadow=shadow,
            background=background,
            max_words=data.get("max_words"),
            lines=data.get("lines"),
            animation=data.get("animation"),
            current_word_highlight=highlight,
        )

    # -- escrita campo a campo (um projeto) -------------------------------

    def set_style(self, project_id: str, **fields: Any) -> CaptionsStyleState:
        """Define/atualiza um ou mais campos de ``captions_style`` -- ver
        seção 0.4 da docstring do módulo (por que um único método
        genérico, sem setters individuais por campo)."""
        if not fields:
            raise CampoInvalidoError("nenhum campo fornecido para atualização")
        validated = _validate_style_fields(fields, require_all=False)

        def mutator(current_data: Any) -> dict:
            merged = dict(current_data) if isinstance(current_data, Mapping) else {}
            merged.update(validated)
            return merged

        try:
            self._manager.update_category(project_id, CAPTIONS_STYLE, mutator)
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        return self.get_style(project_id)

    # -- presets (um projeto) --------------------------------------------

    def apply_preset(self, project_id: str, preset_name: str) -> CaptionsStyleState:
        payload = _resolve_preset(preset_name)

        def mutator(_current_data: Any) -> dict:
            return dict(payload)

        try:
            self._manager.update_category(project_id, CAPTIONS_STYLE, mutator)
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        return self.get_style(project_id)

    # -- batch -------------------------------------------------------------

    def bulk_set_style(self, project_ids: Sequence[str], **fields: Any) -> BulkCaptionsStyleResult:
        """Aplica os mesmos campos, já validados, a múltiplos Projects.
        Pré-validação única antes de qualquer escrita (mesma matriz do
        Prompt 30 para ``bulk_set_audio_settings``)."""
        requested = tuple(_dedupe_preserve_order(project_ids))
        if not requested:
            raise CampoInvalidoError("project_ids não pode ser vazio")
        if not fields:
            raise CampoInvalidoError("nenhum campo fornecido para atualização em lote")

        validated = _validate_style_fields(fields, require_all=False)
        return self._apply_payload_bulk(requested, validated)

    def bulk_apply_preset(self, project_ids: Sequence[str], preset_name: str) -> BulkCaptionsStyleResult:
        """Aplica o mesmo preset (substituição completa da categoria) a
        múltiplos Projects. Nome do preset validado UMA VEZ antes de
        qualquer escrita."""
        requested = tuple(_dedupe_preserve_order(project_ids))
        if not requested:
            raise CampoInvalidoError("project_ids não pode ser vazio")

        payload = _resolve_preset(preset_name)
        return self._apply_payload_bulk(requested, payload, full_replace=True)

    # -- interno -----------------------------------------------------------

    def _apply_payload_bulk(
        self,
        requested: "tuple[str, ...]",
        payload: Mapping[str, Any],
        *,
        full_replace: bool = False,
    ) -> BulkCaptionsStyleResult:
        updated: "list[str]" = []
        unchanged: "list[str]" = []
        failed: "list[tuple[str, str]]" = []

        for project_id in requested:
            changed_flag: "list[bool]" = []

            def mutator(current_data: Any, _flag: "list[bool]" = changed_flag) -> dict:
                before = dict(current_data) if isinstance(current_data, Mapping) else {}
                if full_replace:
                    after = dict(payload)
                else:
                    after = dict(before)
                    after.update(payload)
                _flag.append(after != before)
                return after

            try:
                self._manager.update_category(project_id, CAPTIONS_STYLE, mutator)
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

        return BulkCaptionsStyleResult(
            requested=requested,
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            failed=tuple(failed),
        )


def _resolve_preset(preset_name: Any) -> "dict[str, Any]":
    if not isinstance(preset_name, str) or preset_name not in PRESETS:
        raise PresetInvalidoError(f"preset desconhecido: {preset_name!r} (válidos: {sorted(PRESETS)})")
    return _PRESETS_VALIDATED[preset_name]


__all__ = [
    "CAPTIONS_STYLE",
    "ALIGNMENTS",
    "ANIMATIONS",
    "SIZE_RANGE",
    "POSITION_RANGE",
    "STROKE_WIDTH_RANGE",
    "SHADOW_BLUR_RANGE",
    "SHADOW_OFFSET_RANGE",
    "BACKGROUND_OPACITY_RANGE",
    "MAX_WORDS_RANGE",
    "LINES_RANGE",
    "FONT_NAME_MAX_LEN",
    "PRESET_CLASSIC",
    "PRESET_BOLD_SOCIAL",
    "PRESET_KARAOKE",
    "PRESETS",
    "CaptionsStyleError",
    "CampoInvalidoError",
    "VocabularioInvalidoError",
    "CorInvalidaError",
    "PresetInvalidoError",
    "CaptionsStyleState",
    "BulkCaptionsStyleResult",
    "CaptionsStyleEngine",
]
