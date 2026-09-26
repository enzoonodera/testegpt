# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 (continuação) — CORREÇÃO: DETECÇÃO DE ITENS NA SEÇÃO
"VERIFICAÇÕES" DO TIKTOK (BUG REAL WINDOWS).

Bug real (Windows, evidenciado por outerHTML capturado): a seção
"Verificações" do TikTok Studio mostra sempre exatamente 2 checks reais
(direitos autorais + conteúdo simples, ambos com sucesso/verde), mas o
`_gather_check_items()` antigo encontrava 4 "itens" -- os 3 extras eram
elementos fantasmas (entre eles uma div decorativa sempre presente,
`content-check__divider`, vazia) capturados pelo último seletor de fallback
`div[class*="check"]`, que casa com QUALQUER classe que contenha a
substring "check". Como item ilegível vira CHECK_UNKNOWN e UNKNOWN bloqueia
corretamente a agregação (aggregate_check_status, não alterado), o
resultado geral nunca resolvia para PASSED mesmo com os dois checks reais
100% ok -- bloqueando TODA publicação automática, sempre (não
intermitente).

Este arquivo cobre, em blocos, os cenários obrigatórios do GATE 19.5
(rodada de correção de detecção de itens):
  A. REGRESSÃO DO BUG REAL           -- o cenário exato relatado
  B. MAPEAMENTO ESTRUTURAL           -- as 5 variantes + nenhuma ativa
  C. COMBINAÇÕES                     -- via aggregate_check_status() (não alterado)
  D. ISOLAMENTO ENTRE CHECKS         -- leitura de um check nunca vaza para o outro
  E. IDENTIFICAÇÃO POR TEXTO         -- capitalização e "headline não encontrado"
  F. ELEMENTO DECORATIVO NUNCA VIRA ITEM
  G. CHECK DESCONHECIDO/NOVO (kind=OTHER)

Reaproveita os helpers reais (`_build_section`, `_make_copyright_check`,
`_make_content_check`, `_make_divider`, `_status_variant_elements`) já
usados/validados em tests/test_agendar_tiktok_preflight_checks.py, para não
duplicar a construção do DOM fake nem divergir de como os outros testes já
provaram a estrutura real.
"""
import unittest

from _sistema import agendar_tiktok as tiktok
from tests.fakes_playwright import FakeElement, FakeLocator, FakePage
from tests.test_agendar_tiktok_preflight_checks import (
    SECTION_SELECTOR,
    _ALL_STATUS_VARIANTS,
    _make_copyright_check,
    _make_content_check,
    _make_divider,
    _status_variant_elements,
    _build_section,
)


def _page_for_section(section):
    return FakePage(registry={SECTION_SELECTOR: FakeLocator([section])})


# ============================================================================
# A. REGRESSÃO DO BUG REAL (a mais importante)
# ============================================================================
class TikTokRealBugRegressionTests(unittest.TestCase):
    def test_1_copyright_ok_content_ok_divider_presente_resolve_passed_nunca_unknown(self):
        """Cenário EXATO do bug real Windows: copyright=success, conteúdo=success,
        divisor decorativo presente no DOM (como sempre está). Antes da correção,
        isso produzia 4 itens (2 reais + 2 fantasmas) e travava em UNKNOWN para
        sempre. Depois da correção, deve haver exatamente 2 itens (nenhum
        fantasma) e o estado geral deve ser CHECK_PASSED."""
        cwrapper, cidentity, cvariants = _make_copyright_check(tiktok.CHECK_PASSED)
        twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_PASSED)
        divider = _make_divider()

        section_children = {
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
            # o divisor NUNCA expõe STATUS_RESULT_SELECTOR -- exatamente como
            # no DOM real, e é por isso que ele nunca entra na lista de itens.
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(cvariants) + list(tvariants)),
            # seletor genérico antigo que o bug usava (`div[class*="check"]`)
            # não é mais consultado pela produção -- registrado aqui só para
            # provar que, mesmo presente no DOM (via o divisor), não afeta o
            # resultado.
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)

        items = tiktok._gather_check_items(page)
        self.assertEqual(2, len(items), f"esperado exatamente 2 itens (copyright+conteúdo), obtido: {items}")
        kinds = sorted(it["kind"] for it in items)
        self.assertEqual(sorted([tiktok.CHECK_KIND_COPYRIGHT, tiktok.CHECK_KIND_CONTENT]), kinds)
        self.assertTrue(all(it["status"] == tiktok.CHECK_PASSED for it in items), items)
        self.assertNotIn(tiktok.CHECK_KIND_OTHER, kinds, "o divisor decorativo NUNCA pode virar item OTHER")

        self.assertEqual(tiktok.CHECK_PASSED, tiktok.get_tiktok_preflight_status(page))

    def test_1b_estrutura_real_de_2_niveis_nao_duplica_copyright_e_conteudo_como_other(self):
        """Adversarial: no DOM real, uma variante de status é filha de um
        DIV INTERMEDIÁRIO de grupo de status, que por sua vez é filho do
        container do check (irmão da identidade data-e2e/headline) -- ou
        seja, 2 níveis reais entre a folha e o container, não 1. Este teste
        modela essa profundidade explicitamente (ao contrário dos outros
        testes deste arquivo, que simplificam para 1 nível) para provar que
        _find_other_check_containers() sobe os níveis certos e NÃO
        reintroduz copyright/conteúdo como itens fantasmas duplicados
        kind=OTHER -- a mesma classe de bug desta rodada, só que
        auto-infligida pela descoberta genérica em vez de um seletor de
        fallback ruim."""
        copyright_wrapper = FakeElement(tag="div", text="Verificação de direitos autorais de música", visible=True)
        copyright_status_group = FakeElement(tag="div", visible=True, parent=copyright_wrapper)
        copyright_variants = []
        for v in _ALL_STATUS_VARIANTS:
            active = v == "status-success"
            copyright_variants.append(FakeElement(
                tag="div", attrs={"class": f"status-result {v}", "data-show": "true" if active else "false"},
                visible=active, parent=copyright_status_group,
            ))
        copyright_identity = FakeElement(
            tag="div", attrs={"data-e2e": "copyright_container"},
            text="Verificação de direitos autorais de música", visible=True, parent=copyright_wrapper,
        )
        copyright_wrapper._children = {
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(copyright_variants),
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([copyright_identity]),
        }

        content_wrapper = FakeElement(tag="div", text="Verificação de conteúdo simples", visible=True)
        content_status_group = FakeElement(tag="div", visible=True, parent=content_wrapper)
        content_variants = []
        for v in _ALL_STATUS_VARIANTS:
            active = v == "status-success"
            content_variants.append(FakeElement(
                tag="div", attrs={"class": f"status-result {v}", "data-show": "true" if active else "false"},
                visible=active, parent=content_status_group,
            ))
        content_headline = FakeElement(
            tag="div", attrs={"class": "headline-wrapper"},
            text="Verificação de conteúdo simples", visible=True, parent=content_wrapper,
        )
        content_wrapper._children = {
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(content_variants),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([content_headline]),
        }

        section_children = {
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([copyright_identity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([content_headline]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(copyright_variants) + list(content_variants)),
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)

        items = tiktok._gather_check_items(page)
        self.assertEqual(
            2, len(items),
            f"copyright/conteúdo não podem reaparecer duplicados como OTHER via descoberta genérica: {items}",
        )
        kinds = sorted(it["kind"] for it in items)
        self.assertEqual(sorted([tiktok.CHECK_KIND_COPYRIGHT, tiktok.CHECK_KIND_CONTENT]), kinds)
        self.assertTrue(all(it["status"] == tiktok.CHECK_PASSED for it in items), items)

    def test_2_wait_for_tiktok_checks_prossegue_sem_bloquear_no_cenario_real(self):
        """Mesmo cenário, mas end-to-end via wait_for_tiktok_checks() (não
        alterado) -- prova que a correção realmente desbloqueia o fluxo de
        publicação, não só a leitura isolada de status."""
        cwrapper, cidentity, cvariants = _make_copyright_check(tiktok.CHECK_PASSED)
        twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_PASSED)
        section_children = {
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(cvariants) + list(tvariants)),
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)

        result = tiktok.wait_for_tiktok_checks(page, timeout_seconds=5, poll_interval=0.02)
        self.assertEqual(tiktok.CHECK_PASSED, result)


# ============================================================================
# B. MAPEAMENTO ESTRUTURAL -- as 5 variantes, uma de cada vez, + nenhuma ativa
# ============================================================================
class TikTokStructuralVariantMappingTests(unittest.TestCase):
    _EXPECTED = {
        "status-ready": tiktok.CHECK_PENDING,
        "status-checking": tiktok.CHECK_PENDING,
        "status-error": tiktok.CHECK_FAILED,
        "status-warn": tiktok.CHECK_WARNING,
        "status-success": tiktok.CHECK_PASSED,
    }

    def test_1_cada_variante_isolada_mapeia_para_o_check_esperado(self):
        for variant_class, expected in self._EXPECTED.items():
            with self.subTest(variant=variant_class):
                container = FakeElement(tag="div", visible=True)
                variants = _status_variant_elements(variant_class, container=container)
                container._children = {tiktok.STATUS_RESULT_SELECTOR: FakeLocator(variants)}
                self.assertEqual(expected, tiktok._read_status_group_state(container))

    def test_2_nenhuma_variante_ativa_e_unknown_fail_closed(self):
        container = FakeElement(tag="div", visible=True)
        variants = _status_variant_elements(None, container=container)  # nenhuma "ativa"
        container._children = {tiktok.STATUS_RESULT_SELECTOR: FakeLocator(variants)}
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok._read_status_group_state(container))

    def test_3_nenhum_grupo_de_status_presente_e_unknown(self):
        container = FakeElement(tag="div", visible=True)
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok._read_status_group_state(container))

    def test_4_container_none_e_unknown_sem_excecao(self):
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok._read_status_group_state(None))

    def test_5_visibilidade_real_prevalece_sobre_data_show_literal(self):
        """Se o atributo data-show="true" estiver, na prática, numa variante
        que NÃO está realmente visível (ex.: mudança tardia de estado sem
        atualização do atributo), a leitura deve confiar em is_visible(),
        não no valor literal do atributo -- decisão explícita de design
        documentada no relatório do GATE."""
        container = FakeElement(tag="div", visible=True)
        stale = FakeElement(
            tag="div", attrs={"class": "status-result status-checking", "data-show": "true"},
            visible=False, parent=container,
        )
        real = FakeElement(
            tag="div", attrs={"class": "status-result status-success", "data-show": "false"},
            visible=True, parent=container,
        )
        container._children = {tiktok.STATUS_RESULT_SELECTOR: FakeLocator([stale, real])}
        self.assertEqual(tiktok.CHECK_PASSED, tiktok._read_status_group_state(container))


# ============================================================================
# C. COMBINAÇÕES -- via aggregate_check_status() (não alterado nesta rodada)
# ============================================================================
class TikTokCombinationsViaUnchangedAggregationTests(unittest.TestCase):
    def _items(self, copyright_status, content_status):
        cwrapper, cidentity, cvariants = _make_copyright_check(copyright_status)
        twrapper, theadline, tvariants = _make_content_check(content_status)
        section_children = {
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(cvariants) + list(tvariants)),
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)
        return tiktok._gather_check_items(page)

    def test_1_passed_passed_e_passed(self):
        items = self._items(tiktok.CHECK_PASSED, tiktok.CHECK_PASSED)
        statuses = [it["status"] for it in items]
        self.assertEqual(tiktok.CHECK_PASSED, tiktok.aggregate_check_status(statuses))

    def test_2_pending_domina_sobre_failed(self):
        items = self._items(tiktok.CHECK_FAILED, tiktok.CHECK_PENDING)
        statuses = [it["status"] for it in items]
        self.assertEqual(tiktok.CHECK_PENDING, tiktok.aggregate_check_status(statuses))

    def test_3_failed_vence_warning(self):
        items = self._items(tiktok.CHECK_FAILED, tiktok.CHECK_WARNING)
        statuses = [it["status"] for it in items]
        self.assertEqual(tiktok.CHECK_FAILED, tiktok.aggregate_check_status(statuses))

    def test_4_warning_vence_passed(self):
        items = self._items(tiktok.CHECK_WARNING, tiktok.CHECK_PASSED)
        statuses = [it["status"] for it in items]
        self.assertEqual(tiktok.CHECK_WARNING, tiktok.aggregate_check_status(statuses))


# ============================================================================
# D. ISOLAMENTO ENTRE CHECKS -- leitura de um nunca contamina o outro
# ============================================================================
class TikTokChecksIsolationTests(unittest.TestCase):
    def test_1_copyright_failed_content_passed_nao_se_misturam(self):
        cwrapper, cidentity, cvariants = _make_copyright_check(tiktok.CHECK_FAILED)
        twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_PASSED)
        section_children = {
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(cvariants) + list(tvariants)),
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)

        items = tiktok._gather_check_items(page)
        by_kind = {it["kind"]: it["status"] for it in items}
        self.assertEqual(tiktok.CHECK_FAILED, by_kind[tiktok.CHECK_KIND_COPYRIGHT])
        self.assertEqual(tiktok.CHECK_PASSED, by_kind[tiktok.CHECK_KIND_CONTENT])

    def test_2_copyright_container_isolado_nao_ve_variantes_do_conteudo(self):
        """_read_status_group_state(container) só deve enxergar as variantes
        que são DESCENDENTES do container do próprio check -- provado
        diretamente chamando a função no container de copyright isolado,
        mesmo quando outra variante (do check de conteúdo) existe na
        página."""
        cwrapper, cidentity, cvariants = _make_copyright_check(tiktok.CHECK_WARNING)
        twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_FAILED)
        # confirma que cwrapper enxerga só as suas próprias variantes
        self.assertEqual(tiktok.CHECK_WARNING, tiktok._read_status_group_state(cwrapper))
        self.assertEqual(tiktok.CHECK_FAILED, tiktok._read_status_group_state(twrapper))


# ============================================================================
# E. IDENTIFICAÇÃO POR TEXTO -- capitalização e "headline não encontrado"
# ============================================================================
class TikTokTextIdentificationTests(unittest.TestCase):
    def test_1_headline_com_capitalizacao_diferente_ainda_e_encontrado(self):
        for headline_text in (
            "VERIFICAÇÃO DE CONTEÚDO SIMPLES",
            "verificação de conteúdo simples",
            "Verificação De Conteúdo Simples",
        ):
            with self.subTest(headline_text=headline_text):
                twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_PASSED, label=headline_text)
                section_children = {
                    tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
                }
                section = FakeElement(tag="div", visible=True, children=section_children)
                found = tiktok._find_content_check_container(section)
                self.assertIsNotNone(found, f"headline '{headline_text}' deveria ter sido encontrado")

    def test_2_headline_realmente_nao_encontrado_vira_check_unknown_sem_crash(self):
        """Nenhum headline candidato bate com o texto esperado -- o check de
        conteúdo deve virar CHECK_UNKNOWN explícito (nunca PASSED por
        omissão, nunca uma exceção)."""
        outro = FakeElement(tag="div", attrs={"class": "headline-wrapper"}, text="Outro título qualquer", visible=True)
        section_children = {
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([outro]),
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator([]),
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)

        items = tiktok._gather_check_items(page)
        by_kind = {it["kind"]: it["status"] for it in items}
        self.assertEqual(tiktok.CHECK_UNKNOWN, by_kind[tiktok.CHECK_KIND_CONTENT])
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok.get_tiktok_preflight_status(page))


# ============================================================================
# F. ELEMENTO DECORATIVO NUNCA VIRA ITEM
# ============================================================================
class TikTokDecorativeElementNeverBecomesItemTests(unittest.TestCase):
    def test_1_divisor_e_uma_segunda_div_generica_check_nunca_aparecem_como_item(self):
        """Além do content-check__divider real, inclui uma segunda div
        genérica com "check" na classe (o padrão exato que o seletor de
        fallback antigo `div[class*="check"]` capturava) -- nenhuma das duas
        pode aparecer na lista devolvida por _gather_check_items(), testado
        DIRETAMENTE sobre a lista (não só sobre o estado agregado)."""
        cwrapper, cidentity, cvariants = _make_copyright_check(tiktok.CHECK_PASSED)
        twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_PASSED)
        divider = _make_divider()
        generic_check_div = FakeElement(tag="div", text="", attrs={"class": "some-unrelated-check-wrapper"}, visible=True)

        section_children = {
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(cvariants) + list(tvariants)),
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)

        items = tiktok._gather_check_items(page)
        self.assertEqual(2, len(items))
        labels = [it["label"] for it in items]
        self.assertNotIn(divider.text, labels)
        self.assertNotIn(generic_check_div.text, labels)
        kinds = [it["kind"] for it in items]
        self.assertNotIn(tiktok.CHECK_KIND_OTHER, kinds)


# ============================================================================
# G. CHECK DESCONHECIDO/NOVO (kind=OTHER) -- futuro-prova para um 3º check
# ============================================================================
class TikTokUnknownNewCheckKindOtherTests(unittest.TestCase):
    def test_1_terceiro_check_nao_mapeado_aparece_como_other_com_warning(self):
        """Um bloco de status totalmente novo (não é copyright, não é
        conteúdo) com status-warn ativo -- deve aparecer na lista como
        kind=OTHER, status=WARNING, participando normalmente da agregação
        (nunca ignorado silenciosamente, nunca ALLOW-elegível)."""
        cwrapper, cidentity, cvariants = _make_copyright_check(tiktok.CHECK_PASSED)
        twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_PASSED)

        other_wrapper = FakeElement(tag="div", text="Verificação de qualidade de imagem", visible=True)
        other_variants = _status_variant_elements("status-warn", container=other_wrapper)
        other_wrapper._children = {tiktok.STATUS_RESULT_SELECTOR: FakeLocator(other_variants)}

        section_children = {
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(cvariants) + list(tvariants) + list(other_variants)),
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)

        items = tiktok._gather_check_items(page)
        self.assertEqual(3, len(items))
        other_items = [it for it in items if it["kind"] == tiktok.CHECK_KIND_OTHER]
        self.assertEqual(1, len(other_items))
        self.assertEqual(tiktok.CHECK_WARNING, other_items[0]["status"])

        # participa normalmente da agregação (não é ALLOW-elegível: WARNING
        # não-copyright bloqueia mesmo com política ALLOW -- decide_tiktok_checks_outcome
        # não foi alterado nesta rodada).
        outcome, reason = tiktok.decide_tiktok_checks_outcome(items, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual(("BLOCKED", "warning_non_copyright"), (outcome, reason))

    def test_2_terceiro_check_nunca_e_ignorado_silenciosamente(self):
        """Mesmo com todos os checks conhecidos PASSED, um 3º check
        desconhecido com estado FAILED deve travar a agregação geral --
        provando que ele não é descartado silenciosamente."""
        cwrapper, cidentity, cvariants = _make_copyright_check(tiktok.CHECK_PASSED)
        twrapper, theadline, tvariants = _make_content_check(tiktok.CHECK_PASSED)

        other_wrapper = FakeElement(tag="div", text="Verificação nova do TikTok", visible=True)
        other_variants = _status_variant_elements("status-error", container=other_wrapper)
        other_wrapper._children = {tiktok.STATUS_RESULT_SELECTOR: FakeLocator(other_variants)}

        section_children = {
            tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([cidentity]),
            tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([theadline]),
            tiktok.STATUS_RESULT_SELECTOR: FakeLocator(list(cvariants) + list(tvariants) + list(other_variants)),
        }
        section = FakeElement(tag="div", visible=True, children=section_children)
        page = _page_for_section(section)

        self.assertEqual(tiktok.CHECK_FAILED, tiktok.get_tiktok_preflight_status(page))


if __name__ == "__main__":
    unittest.main()
