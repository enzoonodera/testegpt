# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 (CONTINUAÇÃO) — CORREÇÃO: ESCOPO DA SEÇÃO
"VERIFICAÇÕES" NUNCA FOI O CARD REAL (BUG REAL WINDOWS, 2ª OCORRÊNCIA).

A correção anterior (identidade estrutural para localizar copyright/
conteúdo + descoberta por "lista branca" via grupo de 5 variantes
`status-result`) passou em 1102 testes sintéticos, mas travou de novo no
Windows real com o MESMO sintoma final ("Estado geral: DESCONHECIDO"),
mesmo com as duas verificações reais aparecendo "Nenhum problema
encontrado." na tela.

Causa raiz (confirmada via o HTML real de diagnóstico que o próprio
programa salva no abort, parseado com um parser HTML de verdade): os 3
seletores estruturais de `_find_checks_section()` davam ZERO resultados
nesta versão real do TikTok Studio (sempre, não só às vezes), e o
fallback antigo (`get_by_text("Verificações") -> xpath="..`) subia só 1
nível a partir do título e resolvia para o painel do FORMULÁRIO INTEIRO de
upload (`div.jsx-1566941760.main`), que contém `cover_container`,
`caption_container`, `poi_container`, `schedule_container`,
`video_visibility_container`, `user_perm_container`,
`advanced_settings_container`, `aigc_container`,
`disclose_content_container` E `copyright_container` juntos. Com esse
escopo largo demais, `_find_other_check_containers()` encontrava
`div[class*="status-result"]` de TOGGLES não relacionados (ex.: o toggle
"Qualidade HD por padrão quando você publica a partir da versão Web do
Studio"), que viravam itens fantasmas `kind=OTHER status=CHECK_UNKNOWN` e
travavam a agregação de novo — o MESMO sintoma final da rodada anterior,
por um mecanismo diferente (escopo largo demais, não seletor de item
ganancioso).

Este arquivo cobre a correção: `_find_checks_section()` agora localiza o
escopo a partir do MESMO ponto de identidade já confiável
(`div[data-e2e="copyright_container"]`), subindo pelos ancestrais reais
até o primeiro `div` cuja classe contenha "card" (via eixo XPath
`ancestor::...[1]`, que devolve o ancestral MAIS PRÓXIMO quando indexado
com [1] -- não o primeiro do documento). O fallback de texto antigo foi
REMOVIDO (não só "melhorado"): não há hoje forma comprovadamente segura de
usá-lo sem reintroduzir esse risco.

NOTA SOBRE O FAKE: `tests/fakes_playwright.py` foi ajustado (retrocompatível,
ver comentário na própria função `FakeElement.locator`) para permitir que um
teste registre uma resposta ESPECÍFICA para um XPath distinto ao invés de
sempre cair no fallback genérico de "devolver `self.parent`" -- necessário
porque, nesta correção, o MESMO elemento de identidade (`copyright_container`)
precisa responder a dois XPaths diferentes com destinos diferentes: um hop
(`xpath=..`) até o wrapper do check, e um ancestor-walk
(`CHECKS_CARD_ANCESTOR_XPATH`) até o card. Isso é uma limitação do fake --
ele não valida a semântica REAL do eixo `ancestor::` do XPath (isso é
garantido pela especificação XPath, não por execução aqui); o fake só prova
que a produção *usa* o resultado desse locator corretamente.
"""
import unittest

from _sistema import agendar_tiktok as tiktok
from tests.fakes_playwright import FakeElement, FakeLocator, FakePage
from tests.test_agendar_tiktok_preflight_checks import (
    _make_copyright_check,
    _make_content_check,
)

# Os 9 data-e2e reais do formulário de upload inteiro (evidência Windows,
# HTML real de diagnóstico) que NUNCA podem aparecer dentro do escopo
# devolvido por _find_checks_section() -- provar isso é o requisito (c) da
# seção 3 do prompt desta rodada.
FORM_UNRELATED_E2E = [
    "cover_container",
    "caption_container",
    "poi_container",
    "schedule_container",
    "video_visibility_container",
    "user_perm_container",
    "advanced_settings_container",
    "aigc_container",
    "disclose_content_container",
]

STRAY_TOGGLE_LABELS = (
    "Qualidade HD por padrão quando você publica a partir da versão Web do Studio",
    "Quem pode ver esta publicação",
    "Carregamentos de alta qualidade",
)


def _build_narrow_real_card():
    """Monta o card REAL e estreito (só os 2 checks verdadeiros), do jeito
    que _find_checks_section() deve devolver depois da correção."""
    cwrapper, cidentity, cvariants = _make_copyright_check(tiktok.CHECK_PASSED)
    twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_PASSED)
    card = FakeElement(
        tag="div", attrs={"class": "jsx-777 card"}, visible=True,
        children={
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(cvariants) + list(tvariants)),
        },
    )
    return card, cidentity


def _build_wide_form_with_unrelated_containers_and_stray_toggle():
    """Simula o painel do formulário inteiro (`div.jsx-1566941760.main`) que
    o bug de 2ª ocorrência resolvia como escopo -- contém os 9 data-e2e
    de outras seções do formulário E um elemento `status-result` órfão
    (o toggle "Qualidade HD por padrão...", que não é nenhuma verificação
    real) fora do card real."""
    unrelated = {
        e2e: FakeElement(tag="div", attrs={"data-e2e": e2e}, text="", visible=True)
        for e2e in FORM_UNRELATED_E2E
    }
    stray_toggle = FakeElement(
        tag="div", attrs={"class": "jsx-519643315 status-result"},
        text=STRAY_TOGGLE_LABELS[0], visible=True,
    )
    wide_form_children = {
        f'div[data-e2e="{e2e}"]': FakeLocator([el]) for e2e, el in unrelated.items()
    }
    wide_form_children[tiktok.STATUS_RESULT_SELECTOR] = FakeLocator([stray_toggle])
    wide_form = FakeElement(
        tag="div", attrs={"class": "jsx-1566941760 main"}, visible=True,
        children=wide_form_children,
    )
    return wide_form, unrelated, stray_toggle


def _build_page_reproducing_2nd_occurrence_bug():
    """Reproduz fielmente a evidência real Windows da 2ª ocorrência: os 3
    seletores estruturais antigos dão ZERO resultados (não registrados na
    página), `copyright_container` existe no nível da PÁGINA (não já
    escopado a um card), e subir a partir dele (via o novo suporte a XPath
    exato do fake) resolve para o card ESTREITO -- nunca para o formulário
    inteiro, que existe na mesma página com um elemento status-result órfão
    e os 9 data-e2e de outras seções."""
    card, cidentity = _build_narrow_real_card()
    wide_form, unrelated, stray_toggle = _build_wide_form_with_unrelated_containers_and_stray_toggle()

    # o suporte novo do fake (exact-match antes do fallback genérico de
    # "xpath=" -> self.parent) permite que ESTE xpath específico resolva
    # para o card, mesmo que outro xpath (`xpath=..`) sobre o MESMO
    # elemento resolva para outra coisa (o wrapper do check, usado por
    # _find_copyright_check_container -- via cidentity.parent, inalterado).
    cidentity._children[tiktok.CHECKS_CARD_ANCESTOR_XPATH] = FakeLocator([card])

    registry = {
        # provado no HTML real: zero resultados nesta versão do TikTok
        # Studio -- registrados explicitamente vazios, não omitidos, para
        # que o teste prove isso ativamente (FakePage já devolveria vazio
        # para um seletor não registrado, mas ser explícito documenta a
        # evidência real diretamente na fixture).
        'div[data-e2e="post_verifications"]': FakeLocator([]),
        'div[data-e2e*="verification"]': FakeLocator([]),
        'div[data-e2e*="check_list"]': FakeLocator([]),
        # identidade real, no nível da PÁGINA -- é a partir dela que o novo
        # mecanismo sobe até o card.
        tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
    }
    page = FakePage(registry=registry)
    return page, card, wide_form, unrelated, stray_toggle


# ============================================================================
# A. REGRESSÃO DA 2ª OCORRÊNCIA -- escopo é o card estreito, nunca o form
# ============================================================================
class TikTokChecksCardScopeRegressionTests(unittest.TestCase):
    def test_1_find_checks_section_devolve_o_card_estreito_nao_o_form_inteiro(self):
        page, card, wide_form, unrelated, stray_toggle = _build_page_reproducing_2nd_occurrence_bug()
        section = tiktok._find_checks_section(page)
        self.assertIs(section, card, "deve devolver o card real, encontrado a partir da identidade")
        self.assertIsNot(section, wide_form, "NUNCA pode devolver o formulário inteiro")

    def test_2_os_3_seletores_estruturais_antigos_dao_zero_nesta_versao_real(self):
        """Confirma a evidência real: os seletores estruturais de
        _find_checks_section() não batem nesta versão do TikTok Studio --
        provando que o código realmente PRECISA da nova estratégia, não
        está só "por segurança"."""
        page, card, wide_form, unrelated, stray_toggle = _build_page_reproducing_2nd_occurrence_bug()
        for selector in (
            'div[data-e2e="post_verifications"]',
            'div[data-e2e*="verification"]',
            'div[data-e2e*="check_list"]',
        ):
            self.assertEqual(0, page.locator(selector).count(), selector)

    def test_3_gather_check_items_devolve_exatamente_2_sem_itens_do_formulario(self):
        page, card, wide_form, unrelated, stray_toggle = _build_page_reproducing_2nd_occurrence_bug()
        items = tiktok._gather_check_items(page)
        self.assertEqual(2, len(items), f"nunca 3+ -- nenhum toggle do formulário pode virar item: {items}")
        kinds = sorted(it["kind"] for it in items)
        self.assertEqual(sorted([tiktok.CHECK_KIND_COPYRIGHT, tiktok.CHECK_KIND_CONTENT]), kinds)
        self.assertNotIn(tiktok.CHECK_KIND_OTHER, kinds, "o toggle órfão nunca pode virar OTHER")
        labels = [it["label"] for it in items]
        for banned_text in STRAY_TOGGLE_LABELS:
            for label in labels:
                self.assertNotIn(banned_text, label, f"'{banned_text}' vazou para um item de verificação")

    def test_4_estado_geral_resolve_passed_nunca_unknown_no_cenario_real(self):
        page, card, wide_form, unrelated, stray_toggle = _build_page_reproducing_2nd_occurrence_bug()
        self.assertEqual(tiktok.CHECK_PASSED, tiktok.get_tiktok_preflight_status(page))

    def test_5_wait_for_tiktok_checks_prossegue_sem_bloquear_no_cenario_real(self):
        page, card, wide_form, unrelated, stray_toggle = _build_page_reproducing_2nd_occurrence_bug()
        result = tiktok.wait_for_tiktok_checks(page, timeout_seconds=5, poll_interval=0.02)
        self.assertEqual(tiktok.CHECK_PASSED, result)

    def test_6_escopo_resolvido_nao_contem_nenhum_data_e2e_de_outras_secoes_do_formulario(self):
        """Requisito (c) da seção 3 do prompt: prova que o escopo é
        ESTREITO, não só que o resultado final deu certo por acaso --
        verificado diretamente sobre o objeto devolvido por
        _find_checks_section(), nunca sobre o wide_form."""
        page, card, wide_form, unrelated, stray_toggle = _build_page_reproducing_2nd_occurrence_bug()
        section = tiktok._find_checks_section(page)
        for e2e_name in FORM_UNRELATED_E2E:
            selector = f'div[data-e2e="{e2e_name}"]'
            found = section.locator(selector)
            self.assertEqual(0, found.count(), f"escopo não pode conter {e2e_name}")

    def test_7_stray_toggle_fora_do_card_nunca_e_encontrado_a_partir_do_escopo(self):
        page, card, wide_form, unrelated, stray_toggle = _build_page_reproducing_2nd_occurrence_bug()
        section = tiktok._find_checks_section(page)
        # o card real só expõe as variantes dos 2 checks verdadeiros -- o
        # toggle órfão nunca está entre elas.
        variants_in_scope = section.locator(tiktok.STATUS_RESULT_SELECTOR)
        for i in range(variants_in_scope.count()):
            self.assertIsNot(variants_in_scope.nth(i), stray_toggle)


# ============================================================================
# B. FAIL-CLOSED PRESERVADO -- sem identidade, sem card, sem fallback novo
# ============================================================================
class TikTokChecksSectionFailClosedTests(unittest.TestCase):
    def test_1_copyright_container_nao_encontrado_devolve_none(self):
        """Se a tela mudou/deu erro e copyright_container nem existe, o
        comportamento já existente de "seção não encontrada -> lista vazia
        -> CHECK_UNKNOWN geral" é preservado -- nenhum fallback adicional
        foi inventado."""
        page = FakePage(registry={
            'div[data-e2e="post_verifications"]': FakeLocator([]),
            'div[data-e2e*="verification"]': FakeLocator([]),
            'div[data-e2e*="check_list"]': FakeLocator([]),
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([]),
        })
        self.assertIsNone(tiktok._find_checks_section(page))
        self.assertEqual([], tiktok._gather_check_items(page))
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok.get_tiktok_preflight_status(page))

    def test_2_copyright_container_existe_mas_sem_ancestral_com_classe_card_devolve_none(self):
        """copyright_container existe, mas nenhum ancestral tem "card" na
        classe (cenário de erro/estrutura totalmente diferente) -- não deve
        inventar um card errado, deve devolver None (fail closed)."""
        identity = FakeElement(tag="div", attrs={"data-e2e": "copyright_container"}, visible=True)
        # xpath do card registrado explicitamente vazio -- "não encontrei
        # nenhum ancestral com classe card".
        identity._children[tiktok.CHECKS_CARD_ANCESTOR_XPATH] = FakeLocator([])
        page = FakePage(registry={
            'div[data-e2e="post_verifications"]': FakeLocator([]),
            'div[data-e2e*="verification"]': FakeLocator([]),
            'div[data-e2e*="check_list"]': FakeLocator([]),
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([identity]),
        })
        self.assertIsNone(tiktok._find_checks_section(page))
        self.assertEqual([], tiktok._gather_check_items(page))
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok.get_tiktok_preflight_status(page))

    def test_3_texto_verificacoes_sozinho_nao_e_mais_usado_como_estrategia(self):
        """O fallback antigo (get_by_text("Verificações") -> xpath="..) foi
        REMOVIDO -- mesmo que "Verificações" esteja registrado via
        get_by_text, ele não deve mais ser consultado (produção não chama
        page.get_by_text nesta função). Confirmado indiretamente: sem
        copyright_container, o resultado é None mesmo com o título
        presente."""
        heading = FakeElement(tag="div", text="Verificações", visible=True)
        wide_form = FakeElement(tag="div", attrs={"class": "jsx-1566941760 main"}, visible=True)
        heading.parent = wide_form
        page = FakePage(registry={
            'div[data-e2e="post_verifications"]': FakeLocator([]),
            'div[data-e2e*="verification"]': FakeLocator([]),
            'div[data-e2e*="check_list"]': FakeLocator([]),
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([]),
            ("get_by_text", "Verificações"): FakeLocator([heading]),
        })
        self.assertIsNone(tiktok._find_checks_section(page))


# ============================================================================
# C. MÚLTIPLAS OCORRÊNCIAS DA MESMA CLASSE DE VARIANTE (status-ready)
#    coexistindo no DOM -- verificação explícita pedida na seção 2 do prompt
# ============================================================================
class TikTokMultipleSameVariantClassCoexistingTests(unittest.TestCase):
    def test_1_duas_ocorrencias_de_status_ready_inativas_nao_atrapalham_a_ativa(self):
        """Evidência Windows (2ª ocorrência): o card de conteúdo pode ter
        MAIS de um elemento com a MESMA classe de variante (status-ready)
        presentes simultaneamente no DOM (ex.: "Você atingiu o limite de
        verificações para hoje...", "Este recurso não está disponível para
        contas governamentais..."), mesmo que só um esteja de fato
        visível/ativo. _read_status_group_state() já lida com isso
        corretamente: itera TODAS as ocorrências (nunca deduplica por
        classe) e escolhe a que estiver realmente visível -- não precisou
        mudar para isso, mas fica provado e declarado aqui."""
        container = FakeElement(tag="div", visible=True)
        ready_1_inactive = FakeElement(
            tag="div", attrs={"class": "status-result status-ready", "data-show": "false"},
            visible=False, parent=container,
        )
        ready_2_inactive = FakeElement(
            tag="div", attrs={"class": "status-result status-ready", "data-show": "false"},
            visible=False, parent=container,
        )
        success_active = FakeElement(
            tag="div", attrs={"class": "status-result status-success", "data-show": "true"},
            visible=True, parent=container,
        )
        container._children = {
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator([ready_1_inactive, ready_2_inactive, success_active]),
        }
        self.assertEqual(tiktok.CHECK_PASSED, tiktok._read_status_group_state(container))

    def test_2_a_ocorrencia_ativa_de_status_ready_esta_entre_varias_inativas_da_mesma_classe(self):
        container = FakeElement(tag="div", visible=True)
        ready_inactive_1 = FakeElement(
            tag="div", attrs={"class": "status-result status-ready", "data-show": "false"},
            visible=False, parent=container,
        )
        ready_active = FakeElement(
            tag="div", attrs={"class": "status-result status-ready", "data-show": "true"},
            visible=True, parent=container,
        )
        ready_inactive_2 = FakeElement(
            tag="div", attrs={"class": "status-result status-ready", "data-show": "false"},
            visible=False, parent=container,
        )
        other_inactive = FakeElement(
            tag="div", attrs={"class": "status-result status-success", "data-show": "false"},
            visible=False, parent=container,
        )
        container._children = {
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(
                [ready_inactive_1, ready_active, ready_inactive_2, other_inactive]
            ),
        }
        self.assertEqual(tiktok.CHECK_PENDING, tiktok._read_status_group_state(container))

    def test_3_nenhuma_das_multiplas_status_ready_visivel_cai_para_data_show_ou_unknown(self):
        """Se por algum motivo NENHUMA das ocorrências duplicadas estiver
        visível (todas is_visible()=False), a função cai para o sinal
        secundário (data-show="true") -- e se nem isso houver, UNKNOWN
        fail-closed, nunca PASSED por omissão."""
        container = FakeElement(tag="div", visible=True)
        ready_1 = FakeElement(
            tag="div", attrs={"class": "status-result status-ready", "data-show": "false"},
            visible=False, parent=container,
        )
        ready_2_data_show_true = FakeElement(
            tag="div", attrs={"class": "status-result status-ready", "data-show": "true"},
            visible=False, parent=container,
        )
        container._children = {
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator([ready_1, ready_2_data_show_true]),
        }
        self.assertEqual(tiktok.CHECK_PENDING, tiktok._read_status_group_state(container))


if __name__ == "__main__":
    unittest.main()
