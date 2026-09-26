# -*- coding: utf-8 -*-
"""PROMPT — Template Selector (Fase 5: Editor Modular Não Destrutivo).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Garanta arquiteturalmente que Template NÃO seja uma etapa obrigatória.

    Deve ser possível:
    - vídeo pronto → template → exportar
    - Smart Clip → salvar cortes sem template
    - vídeo editado → aplicar template
    - 200 vídeos → aplicar template em lote
    - trocar template depois da edição
    - remover template

    Trocar template não pode exigir repetir Whisper/análise/cortes se nada
    disso mudou.

===========================================================================
0. VISÃO GERAL E ESCOPO
===========================================================================

Este módulo NUNCA aplica de fato um template a um vídeo -- nenhum FFmpeg,
nenhum ``Job``, nenhum ``Artifact``, nenhum render. Ele só persiste QUAL
``Template`` do catálogo (``template_engine.py``), se algum, está
associado a um ``Project``. A aplicação real (Render Engine) é trabalho
de um Prompt futuro (42), explicitamente fora de escopo aqui.

Mesmo padrão arquitetural já estabelecido por ``audio_engine.py``
(Prompt 30)/``visual_editor.py`` (Prompt 29): um engine fino sobre
``EditProjectManager``, que lê/escreve exclusivamente a categoria
``TEMPLATE`` já reservada em ``edit_project.py`` desde o Prompt 27
(constante reaproveitada, nunca uma string nova redefinida aqui).

``TemplateSelector`` não abre transaction própria, não chama
``LocalDatabase.transaction()``/``.get(Project``/``.save(`` diretamente e
não mantém nenhum SQLite próprio: toda leitura/escrita de ``Project``
passa por ``EditProjectManager``. A única outra dependência é
``TemplateEngine`` (injetada no construtor, mesmo padrão de injeção de
dependência já usado em todo o projeto), usada exclusivamente para
validar que um ``template_id`` referenciado realmente existe no
catálogo -- ``TemplateSelector`` nunca lê/grava a tabela ``templates``
diretamente.

===========================================================================
0.1 REPRESENTAÇÃO PERSISTIDA
===========================================================================

A categoria ``TEMPLATE`` grava um objeto de campo único:

    {"template_id": "<uuid do Template no catálogo>"}

Um único campo (não vários) porque a associação Project→Template é
inteiramente descrita por QUAL template está selecionado -- não há hoje
nenhum outro parâmetro de associação pedido pelo roadmap (overrides por
projeto, por exemplo, seriam escopo de um Prompt futuro, nunca inventado
aqui).

===========================================================================
0.2 "REMOVER TEMPLATE" -- DECISÃO DE REPRESENTAÇÃO
===========================================================================

Decisão: a AUSÊNCIA da categoria ``TEMPLATE`` no ``edit_state`` do
Project É a representação de "nenhum template associado" -- não um valor
sentinela como ``{"template_id": None}`` gravado explicitamente.

Motivo: essa é também a representação NATURAL de um Project que nunca
teve ``set_template`` chamado (vídeo pronto/Smart Clip sem template
nunca passa por este módulo) -- ter duas representações distintas para
o mesmo significado ("nunca setado" via categoria ausente, e
"explicitamente removido" via ``{"template_id": None}`` gravado) seria
um estado duplicado sem benefício real, e ``get_template_selection``
teria que tratar as duas formas de qualquer forma para devolver o mesmo
``None`` em ambos os casos.

Consequência direta: ``clear_template`` é implementado via
``EditProjectManager.remove_category(project_id, TEMPLATE)``, que já
tem, por design (ver docstring de ``remove_category`` em
``edit_project.py``), a garantia exata pedida pelo Prompt: idempotente,
devolve ``False`` sem lançar quando a categoria já está ausente (NUNCA
grava nada nesse caso -- nem revisão de categoria, nem
``Project.revision``), e nunca toca nenhuma outra categoria do mesmo
Project. Reaproveitar essa garantia já existente evita reimplementar,
de forma paralela e potencialmente divergente, a mesma semântica de
"remover é seguro e idempotente" que ``remove_category`` já entrega.

===========================================================================
0.3 ``set_category`` vs. ``update_category`` -- ESCOLHA POR OPERAÇÃO
===========================================================================

``set_template`` (substituição de valor único, atômica):
usa ``EditProjectManager.set_category`` diretamente. Como a categoria
``TEMPLATE`` tem um único campo (``template_id``), o valor final já é
conhecido por completo antes de qualquer escrita -- não há necessidade
de "ler o atual para decidir o novo" (ao contrário de
``AUDIO_SETTINGS``, que tem vários campos independentes e por isso usa
``update_category`` com um mutator que MESCLA um campo no dict
existente, preservando os demais). Usar ``set_category`` aqui é mais
simples e igualmente seguro: ``set_category`` já executa leitura +
decisão de NO-OP + escrita dentro da MESMA ``BEGIN IMMEDIATE`` (ver
docstring de ``set_category``), então não há nenhuma janela de TOCTOU
introduzida por não usar um mutator.

``bulk_set_template`` (classificação updated/unchanged por item, sob
concorrência real): usa ``EditProjectManager.update_category`` com um
mutator que captura, via um flag de closure (mesmo padrão exato de
``AudioEngine.bulk_set_audio_settings``), se o valor mudou em relação ao
que já estava persistido -- a comparação "mudou ou não" acontece DENTRO
da mesma transação atômica que decide e escreve, nunca como uma leitura
separada antes da escrita (que reintroduziria uma janela de TOCTOU só
para fins de classificação do relatório de lote). Ver seção 0.5.

===========================================================================
0.4 VALIDAÇÃO DE FK REAL
===========================================================================

``set_template``/``bulk_set_template`` SEMPRE chamam
``TemplateEngine.get_template(template_id)`` antes de qualquer escrita
-- nunca aceitam uma string arbitrária sem checar contra o catálogo.
``TemplateNaoEncontradoError`` (de ``template_engine.py``) propaga
DIRETAMENTE ao chamador, sem ser envolvida num erro próprio deste
módulo.

Decisão (pedida explicitamente pelo Prompt): propagar diretamente, não
envolver. Motivo: ``TemplateNaoEncontradoError`` já representa
exatamente o mesmo conceito de domínio ("este ``template_id`` não existe
no catálogo") que este módulo precisaria comunicar -- envolvê-la numa
segunda classe de erro só para trocar o nome adicionaria uma camada de
tradução sem nenhuma informação nova, contrariando o princípio do
CLAUDE.md contra abstração desnecessária. O chamador que já sabe tratar
``TemplateNaoEncontradoError`` ao usar ``TemplateEngine`` diretamente
não precisa aprender um segundo tipo de erro para o mesmo conceito só
porque passou a chamar através de ``TemplateSelector``.

Nenhum tratamento especial por ``template_type`` (``BUILT_IN`` vs.
``CUSTOM``) -- ``get_template`` já não distingue os dois, e este módulo
não adiciona nenhuma distinção nova (testado explicitamente).

===========================================================================
0.5 OPERAÇÃO EM LOTE (BATCH)
===========================================================================

``bulk_set_template(project_ids, template_id)`` -- "200 vídeos → aplicar
template em lote" do roadmap. Mesmo padrão estrutural de
``AudioEngine.bulk_set_audio_settings``: ``template_id`` é validado
contra o catálogo UMA ÚNICA VEZ, antes do loop (nunca revalidado por
``project_id`` -- o template não muda durante o lote). Cada
``project_id`` é então processado individualmente: uma falha isolada
(``project_id`` inexistente ou malformado) cai em ``failed`` com
``(project_id, código_do_erro)`` e NÃO interrompe o processamento dos
demais (mesmo princípio de isolamento de falha do CLAUDE.md -- "falha de
um vídeo não derruba o lote"). Resultado estruturado
``BulkTemplateSelectionResult`` com ``requested``/``updated``/
``unchanged``/``failed``, mesmo formato de ``BulkAudioEditResult``.

``unchanged`` -- reenviar o MESMO ``template_id`` já associado a um
Project cai em ``unchanged``, nunca em ``updated`` (mesmo padrão do
NO-OP determinístico de ``set_category``/``update_category`` -- reenvios
idempotentes do mesmo estado não devem inflar a revisão da categoria).

``bulk_clear_template`` -- decisão: SIM, implementado por simetria com
``bulk_set_template`` (o roadmap não pede lote para remoção
explicitamente, mas "remover template" já é uma operação de primeira
classe deste módulo, e negar a mesma forma de lote só para remoção
criaria uma API assimétrica sem motivo -- decisão documentada, não
presumida silenciosamente). Mesma estrutura de resultado: cada
``project_id`` cuja categoria ``TEMPLATE`` já estava ausente cai em
``unchanged`` (mesmo comportamento de no-op de ``remove_category``,
refletido no relatório de lote), cada um que tinha uma associação
removida cai em ``updated``, falhas isoladas em ``failed``.

===========================================================================
0.6 BADGE ``BADGE_TEMPLATE_APPLIED`` -- DECISÃO (a)
===========================================================================

``media_catalog.py`` já reserva ``BADGE_TEMPLATE_APPLIED`` como ``"0"``
hardcoded (sempre falso), com o MESMO racional hoje documentado ao lado
de ``BADGE_AUDIO_PROCESSED``: "deliberadamente fora de escopo [...] é
100% decisão/configuração. Não há evidência real para referenciar hoje;
permanece '0' até que um passo real de processamento [...] exista".

Decisão: MANTER ``BADGE_TEMPLATE_APPLIED`` como ``"0"`` -- opção (a).
Este Prompt é, tal como o Prompt 30 (``AudioEngine``), puramente
decisão/configuração: nenhum ``Job``/``Artifact``/render real é criado
ao selecionar um template. "Aplicado" no sentido de badge deveria
significar "processado/renderizado com este template" (o mesmo padrão
semântico já usado por ``BADGE_REFRAMED_9_16``/``BADGE_TRANSCRIBED``,
que exigem um checkpoint + Artifact reais, nunca apenas uma decisão
persistida) -- não "selecionado no catálogo". Fazer o badge acender só
por causa de uma seleção seria uma falsa evidência de processamento
(risco de badge enganoso), exatamente o problema que o precedente de
``AUDIO_PROCESSED`` já identificou e evitou. A opção (b) (expressão SQL
real hoje, baseada apenas em "categoria TEMPLATE presente") ficaria sem
significado real até o Render Engine (Prompt 42) existir -- adicionar
essa expressão agora seria acender um badge sem que nenhum trabalho real
correspondente tenha ocorrido, semanticamente idêntico ao erro que o
Prompt 30 já preveniu para ``AUDIO_PROCESSED``.

``media_catalog.py`` NÃO é tocado por este Prompt -- nem para adicionar
lógica nova a ``BADGE_TEMPLATE_APPLIED``, nem por qualquer outro motivo.
Existe um teste negativo explícito
(``test_selecionar_template_nao_acende_badge_template_applied``) provando
isso, no mesmo espírito do teste negativo já existente para
``AUDIO_PROCESSED``.

===========================================================================
0.7 GARANTIA ARQUITETURAL CENTRAL -- ISOLAMENTO DE CATEGORIA
===========================================================================

"Trocar template não pode exigir repetir Whisper/análise/cortes se nada
disso mudou" -- a categoria ``TEMPLATE`` é totalmente isolada de
``CUTS``/``CROP``/``AUDIO_SETTINGS``/``CAPTIONS``/qualquer outra
categoria já existente. Este módulo NUNCA lê ou escreve qualquer
categoria além de ``TEMPLATE`` -- nem para validação (a validação de FK
consulta o catálogo de templates via ``TemplateEngine``, nunca outra
categoria do mesmo Project), nem para nenhum efeito colateral. Essa
garantia já é entregue, na camada de baixo nível, por
``EditProjectManager.set_category``/``update_category``/
``remove_category`` (cada um documentado para nunca tocar o
fingerprint/revisão de nenhuma outra categoria) -- este módulo apenas
NÃO A VIOLA, e um teste adversarial end-to end prova isso (ver
``tests/test_template_selector.py``): grava ``CAPTIONS``/``CUTS`` reais,
aplica/troca/remove um template só por este módulo, e relê
``CAPTIONS``/``CUTS`` depois de cada uma das três operações, provando
que ``fingerprint``/``revision``/``updated_at`` de ambas permanecem
EXATAMENTE os mesmos do início ao fim -- e o inverso: gravar/trocar
``CUTS`` depois de já haver ``TEMPLATE`` setado não altera
fingerprint/revisão de ``TEMPLATE``.

===========================================================================
0.8 INDEPENDÊNCIA DE ORDEM
===========================================================================

Nenhuma das categorias exige a outra como pré-condição -- já garantido
estruturalmente porque ``TemplateSelector`` nunca lê/valida contra
``CUTS``/qualquer outra categoria antes de gravar ``TEMPLATE``, e
``EditProjectManager`` nunca exige que uma categoria específica já
exista antes de outra ser gravada. Testado explicitamente: gravar
``CUTS`` sem nunca ter tido ``TEMPLATE`` funciona normalmente (regressão
zero, mas provada); aplicar um ``TEMPLATE`` num Project "cru" (nenhuma
outra categoria gravada ainda) também funciona normalmente.

===========================================================================
0.9 ERROS ESTRUTURADOS
===========================================================================

Mesma filosofia dos módulos irmãos: nenhum ``ValueError`` cru escapa de
um método público. ``ProjectNaoEncontradoError``/``TemplateNaoEncontradoError``
(ambas já existentes, dos módulos que este módulo já depende) propagam
diretamente -- reaproveitadas, nunca reimplementadas. Um ``project_id``
que não é sequer um UUID válido (``LocalDatabase`` levanta um
``ValueError`` cru nesse caso -- mesmo achado do GATE ADVERSARIAL já
documentado em ``audio_engine.py``) é reclassificado como
``CampoInvalidoError`` deste módulo, nunca deixado escapar cru.
``project_ids`` vazio em uma chamada de lote também é
``CampoInvalidoError`` (mesmo padrão de
``AudioEngine.bulk_set_audio_settings``).

===========================================================================
0.10 CONCORRÊNCIA REAL
===========================================================================

``set_template`` usa ``EditProjectManager.set_category``, que já executa
leitura + decisão + escrita dentro de uma única ``BEGIN IMMEDIATE``
(``LocalDatabase.transaction()``) -- SQLite serializa duas chamadas
concorrentes de ``set_category``/``update_category`` sobre o mesmo
``Project``: a segunda só abre sua própria transação depois que a
primeira commita, e então lê o estado JÁ ATUALIZADO pela primeira. Não
há nenhuma corrida capaz de corromper dados ou produzir um estado
intercalado -- exatamente a mesma garantia já testada e documentada em
``edit_project.py``/``audio_engine.py``.

Resultado esperado e testado (``threading.Barrier`` forçando
sobreposição real, mesmo padrão de toda a suíte): quando duas instâncias
de ``TemplateSelector`` aplicam templates DIFERENTES ao MESMO Project
simultaneamente, o resultado final é DETERMINISTICAMENTE um dos dois
valores por completo (nunca um estado misto/corrompido/parcial) -- QUAL
dos dois vence depende da ordem real de aquisição do lock de escrita do
SQLite pelas duas threads do SO (não definida por este módulo, nem
precisa ser: a garantia pedida é "não corromper", não "qual thread
ganha"). O teste verifica que o ``template_id`` final é exatamente um
dos dois enviados, nunca outro valor, nunca uma mistura.

===========================================================================
1. USO
===========================================================================

    from _sistema.edit_project import EditProjectManager
    from _sistema.template_engine import TemplateEngine
    from _sistema.template_selector import TemplateSelector

    manager = EditProjectManager(database)
    template_engine = TemplateEngine(database, storage_manager=storage, app_paths=app_paths)
    selector = TemplateSelector(manager, template_engine)

    selector.set_template(project_id, template_id)
    atual = selector.get_template_selection(project_id)
    selector.clear_template(project_id)

    resultado = selector.bulk_set_template(project_ids, template_id)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence

from .edit_project import (
    TEMPLATE,
    EditProjectManager,
    ProjectNaoEncontradoError,
)
from .template_engine import TemplateEngine, TemplateNaoEncontradoError

__all__ = [
    "TemplateSelectorError",
    "CampoInvalidoError",
    "ProjectNaoEncontradoError",
    "TemplateNaoEncontradoError",
    "BulkTemplateSelectionResult",
    "TemplateSelector",
]


# ---------------------------------------------------------------------
# Erros estruturados
# ---------------------------------------------------------------------

class TemplateSelectorError(RuntimeError):
    """Classe base de todos os erros próprios deste módulo."""

    code: ClassVar[str] = "TEMPLATE_SELECTOR_ERRO"


class CampoInvalidoError(TemplateSelectorError):
    """Um argumento recebeu um valor de tipo errado, vazio, ou de outra
    forma estruturalmente inválido (inclui ``project_id`` que não é
    sequer um UUID válido -- ver seção 0.9 da docstring do módulo)."""

    code: ClassVar[str] = "CAMPO_INVALIDO"


# ---------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class BulkTemplateSelectionResult:
    """Resultado de uma operação em lote (``bulk_set_template`` ou
    ``bulk_clear_template``)."""

    requested: "tuple[str, ...]"
    updated: "tuple[str, ...]"
    unchanged: "tuple[str, ...]"
    failed: "tuple[tuple[str, str], ...]"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _dedupe_preserve_order(values: Sequence[str]) -> "list[str]":
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _validate_template_id(template_id: Any) -> str:
    if not isinstance(template_id, str) or not template_id.strip():
        raise CampoInvalidoError(
            f"template_id deve ser uma string não vazia (recebido: {template_id!r})"
        )
    return template_id


def _as_dict_or_none(payload: Any) -> "dict | None":
    """Guarda contra dados persistidos corrompidos (não-Mapping) --
    mesmo padrão defensivo de ``audio_engine.py``/``visual_editor.py``."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise CampoInvalidoError(
            f"dados de TEMPLATE persistidos são inválidos (esperado objeto, recebido: {payload!r})"
        )
    return dict(payload)


# ---------------------------------------------------------------------
# TemplateSelector
# ---------------------------------------------------------------------

class TemplateSelector:
    """Associa/desassocia um ``Template`` do catálogo a um ``Project``.
    Lê/escreve exclusivamente através de ``EditProjectManager`` na
    categoria ``TEMPLATE`` -- ver seção 0 da docstring do módulo."""

    def __init__(self, manager: EditProjectManager, template_engine: TemplateEngine) -> None:
        if not isinstance(manager, EditProjectManager):
            raise TypeError(
                f"manager deve ser EditProjectManager (recebido: {type(manager)!r})"
            )
        if not isinstance(template_engine, TemplateEngine):
            raise TypeError(
                f"template_engine deve ser TemplateEngine (recebido: {type(template_engine)!r})"
            )
        self._manager = manager
        self._template_engine = template_engine

    # -- leitura -------------------------------------------------------

    def get_template_selection(self, project_id: Any) -> "str | None":
        """Devolve o ``template_id`` associado hoje, ou ``None`` se
        nenhum (Project inexistente na categoria ``TEMPLATE``, ou
        Project sem nenhuma associação ainda -- ver seção 0.2)."""
        try:
            state = self._manager.get_category(project_id, TEMPLATE)
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc

        if state is None:
            return None
        data = _as_dict_or_none(state.data)
        if data is None:
            return None
        template_id = data.get("template_id")
        return template_id if isinstance(template_id, str) else None

    # -- escrita individual ----------------------------------------------

    def set_template(self, project_id: Any, template_id: Any) -> str:
        """Associa (ou troca) o ``Template`` de um Project. Valida o FK
        contra o catálogo ANTES de qualquer escrita -- ver seção 0.4.
        Devolve o ``template_id`` efetivamente persistido."""
        validated_template_id = _validate_template_id(template_id)
        # Validação de FK real -- propaga TemplateNaoEncontradoError
        # diretamente se o template não existir no catálogo (decisão
        # documentada na seção 0.4).
        self._template_engine.get_template(validated_template_id)

        try:
            self._manager.set_category(project_id, TEMPLATE, {"template_id": validated_template_id})
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc

        return validated_template_id

    def clear_template(self, project_id: Any) -> bool:
        """Remove a associação de template do Project -- ver seção 0.2.
        No-op seguro (devolve ``False``, nunca lança) quando já não há
        template associado. Devolve ``True`` quando uma associação
        existente foi de fato removida."""
        try:
            return self._manager.remove_category(project_id, TEMPLATE)
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc

    # -- lote --------------------------------------------------------------

    def bulk_set_template(
        self, project_ids: Sequence[str], template_id: Any
    ) -> BulkTemplateSelectionResult:
        """Aplica o MESMO ``template_id``, validado uma única vez contra
        o catálogo, a múltiplos Projects -- ver seção 0.5. Uma falha
        isolada de ``project_id`` nunca interrompe os demais."""
        requested = tuple(_dedupe_preserve_order(project_ids))
        if not requested:
            raise CampoInvalidoError("project_ids não pode ser vazio")

        validated_template_id = _validate_template_id(template_id)
        # FK validada UMA única vez, antes do loop -- ver seção 0.5.
        self._template_engine.get_template(validated_template_id)

        updated: "list[str]" = []
        unchanged: "list[str]" = []
        failed: "list[tuple[str, str]]" = []

        for project_id in requested:
            changed_flag: "list[bool]" = []

            def mutator(current_data: Any, _flag: "list[bool]" = changed_flag) -> dict:
                new_data = {"template_id": validated_template_id}
                current = dict(current_data) if isinstance(current_data, Mapping) else None
                _flag.append(current != new_data)
                return new_data

            try:
                self._manager.update_category(project_id, TEMPLATE, mutator)
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

        return BulkTemplateSelectionResult(
            requested=requested,
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            failed=tuple(failed),
        )

    def bulk_clear_template(self, project_ids: Sequence[str]) -> BulkTemplateSelectionResult:
        """Remove a associação de template de múltiplos Projects -- ver
        seção 0.5 (implementado por simetria com ``bulk_set_template``).
        Um Project cuja categoria ``TEMPLATE`` já estava ausente cai em
        ``unchanged``, nunca em ``failed`` nem ``updated``."""
        requested = tuple(_dedupe_preserve_order(project_ids))
        if not requested:
            raise CampoInvalidoError("project_ids não pode ser vazio")

        updated: "list[str]" = []
        unchanged: "list[str]" = []
        failed: "list[tuple[str, str]]" = []

        for project_id in requested:
            try:
                removed = self._manager.remove_category(project_id, TEMPLATE)
            except ProjectNaoEncontradoError as exc:
                failed.append((project_id, exc.code))
                continue
            except ValueError:
                failed.append((project_id, "PROJETO_ID_INVALIDO"))
                continue

            if removed:
                updated.append(project_id)
            else:
                unchanged.append(project_id)

        return BulkTemplateSelectionResult(
            requested=requested,
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            failed=tuple(failed),
        )

