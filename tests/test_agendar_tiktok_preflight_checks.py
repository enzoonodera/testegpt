# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — TIKTOK — VERIFICAÇÃO DE DIREITOS AUTORAIS/CONTEÚDO
INCOMPLETA ANTES DO AGENDAMENTO (novo bug real Windows).

Depois da correção do bug de data/hora (TARGET 10:00 -> UI 21:00), o teste
real no Windows avançou até o clique no botão final e revelou um NOVO
bloqueador: o TikTok Studio pode exibir um modal --

    "Continuar publicando?"
    "A verificação de direitos autorais está incompleta. Publicar seu vídeo
    agora irá interromper a verificação. [...] Você deseja continuar
    publicando antes da verificação ser concluída?"
    [Cancelar]  [Publicar agora]

-- quando a verificação de direitos autorais/conteúdo ainda está em
andamento no momento do clique final. Por padrão o produto NUNCA clica em
"Publicar agora": ele espera as verificações concluírem (polling
determinístico, com timeout) ANTES do clique final, e se o modal aparecer
mesmo assim (corrida), cancela com segurança e permite no máximo 1 retry
controlado.

Este arquivo cobre, em 4 partes:
  A. TikTokPreflightStatusTests    -- unidade de get_tiktok_preflight_status()
  B. TikTokWaitForChecksTests      -- unidade de wait_for_tiktok_checks()
  C. TikTokContinuePublishingModalTests -- unidade do tratamento do modal
  D. TikTokScheduleOnePreflightIntegrationTests -- schedule_one() ponta a ponta
"""
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo
import contextlib
import unittest

from _sistema import agendar_tiktok as tiktok
from tests.fakes_playwright import FakeElement, FakeLocator, FakePage
from tests.test_agendar_tiktok_time_layers import build_page


SAO_PAULO = ZoneInfo("America/Sao_Paulo")

SECTION_SELECTOR = 'div[data-e2e="post_verifications"]'

# GATE 19.5 -- ESTÁGIO 2 (correção de detecção de itens): ITEM_SELECTOR era o
# ÚLTIMO seletor de fallback de _gather_check_items() antigo
# (`div[data-e2e*="verification_item"]`), que nunca bate no DOM real do
# TikTok Studio -- por isso os testes que dependiam dele exercitavam um
# caminho que nunca era percorrido na prática (bug real Windows). Mantido
# aqui só porque `tests/test_agendar_tiktok_copyright_policy.py` ainda o
# importa por compatibilidade; não é mais usado por nenhuma fixture deste
# arquivo nem pela produção.
ITEM_SELECTOR = 'div[data-e2e*="verification_item"]'

# Variantes estruturais reais (evidência Windows, GATE 19.5) usadas por
# _gather_check_items() para localizar e ler o estado de cada verificação --
# ver _sistema/agendar_tiktok.py para a documentação completa do mecanismo.
_ALL_STATUS_VARIANTS = ("status-ready", "status-checking", "status-error", "status-warn", "status-success")
_VARIANT_FOR_STATUS = {
    tiktok.CHECK_PASSED: "status-success",
    tiktok.CHECK_WARNING: "status-warn",
    tiktok.CHECK_FAILED: "status-error",
    tiktok.CHECK_PENDING: "status-checking",
}


def _status_variant_elements(active_variant, container=None):
    """Cria os 5 elementos-variante de status reais, com is_visible()
    coerente com qual delas está "ativa" (active_variant=None simula nenhuma
    variante reconhecível ativa -- estado estruturalmente desconhecido)."""
    elements = []
    for v in _ALL_STATUS_VARIANTS:
        is_active = v == active_variant
        elements.append(FakeElement(
            tag="div",
            attrs={"class": f"status-result {v}", "data-show": "true" if is_active else "false"},
            visible=is_active,
            parent=container,
        ))
    return elements


def _make_copyright_check(status=None, label="Verificação de direitos autorais de música", body_text=None):
    """Constrói o container real do check de copyright: um wrapper contendo,
    como filhos diretos (mesmo nível, nunca misturados com outro check),
    o elemento de identidade `data-e2e="copyright_container"` e o grupo das
    5 variantes de status. ``status`` (CHECK_*) escolhe a variante ativa;
    None simula nenhuma variante reconhecível ativa (cai para o fallback de
    texto). ``body_text``, quando informado, é anexado ao texto do wrapper
    para exercitar especificamente o fallback textual de
    ``_classify_check_item``."""
    variant = _VARIANT_FOR_STATUS.get(status)
    text = label if body_text is None else f"{label}\n{body_text}"
    wrapper = FakeElement(tag="div", text=text, visible=True)
    variants = _status_variant_elements(variant, container=wrapper)
    identity = FakeElement(tag="div", attrs={"data-e2e": "copyright_container"}, text=label, visible=True, parent=wrapper)
    wrapper._children = {
        tiktok.STATUS_RESULT_SELECTOR: FakeLocator(variants),
        tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([identity]),
    }
    return wrapper, identity, variants


def _make_content_check(status=None, label="Verificação de conteúdo simples", body_text=None):
    """Análogo a _make_copyright_check(), mas localizado por texto do
    headline (sem data-e2e próprio), como no DOM real."""
    variant = _VARIANT_FOR_STATUS.get(status)
    text = label if body_text is None else f"{label}\n{body_text}"
    wrapper = FakeElement(tag="div", text=text, visible=True)
    variants = _status_variant_elements(variant, container=wrapper)
    headline = FakeElement(tag="div", attrs={"class": "headline-wrapper"}, text=label, visible=True, parent=wrapper)
    wrapper._children = {
        tiktok.STATUS_RESULT_SELECTOR: FakeLocator(variants),
        tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([headline]),
    }
    return wrapper, headline, variants


def _make_divider():
    """Elemento puramente decorativo, real (evidência Windows): sempre
    presente, sem texto e SEM nenhuma das 5 variantes de status como
    descendente -- por isso nunca é contado como item pela nova
    _gather_check_items() (descoberta é lista branca por estrutura, não
    lista negra de nomes de classe)."""
    return FakeElement(tag="div", text="", attrs={"class": "jsx-2534002137 content-check__divider"}, visible=True)


def _build_section(copyright_status=None, content_status=None,
                    include_copyright=True, include_content=True,
                    copyright_body_text=None, content_body_text=None,
                    extra_status_variants=None):
    """Monta a seção "Verificações" real: os dois checks conhecidos (se
    incluídos) mais quaisquer variantes de status extras (para simular um
    3º check ainda não mapeado, kind=OTHER)."""
    section_children = {}
    all_variants = list(extra_status_variants or [])

    if include_copyright:
        cwrapper, cidentity, cvariants = _make_copyright_check(copyright_status, body_text=copyright_body_text)
        section_children[tiktok.COPYRIGHT_CONTAINER_SELECTOR] = FakeLocator([cidentity])
        all_variants.extend(cvariants)
    else:
        section_children[tiktok.COPYRIGHT_CONTAINER_SELECTOR] = FakeLocator([])

    if include_content:
        twrapper, theadline, tvariants = _make_content_check(content_status, body_text=content_body_text)
        section_children[tiktok.CONTENT_HEADLINE_SELECTOR] = FakeLocator([theadline])
        all_variants.extend(tvariants)
    else:
        section_children[tiktok.CONTENT_HEADLINE_SELECTOR] = FakeLocator([])

    section_children[tiktok.STATUS_RESULT_SELECTOR] = FakeLocator(all_variants)
    return FakeElement(tag="div", visible=True, children=section_children)


def build_checks_page(status_sequence, extra_registry=None):
    """FakePage cuja seção "Verificações" devolve, em chamadas sucessivas de
    get_tiktok_preflight_status()/_gather_check_items(), os status de
    status_sequence em ordem (a última entrada repete depois de esgotada) --
    cada entrada é aplicada IGUALMENTE aos dois checks conhecidos (copyright
    e conteúdo), preservando o comportamento histórico deste helper (testar
    a agregação/polling em função de "o estado geral"). status_sequence
    vazia simula a seção não encontrada (UNKNOWN)."""
    state = {"n": 0}

    def section_factory():
        if not status_sequence:
            return FakeLocator([])
        idx = min(state["n"], len(status_sequence) - 1)
        status = status_sequence[idx]
        state["n"] += 1
        section = _build_section(copyright_status=status, content_status=status)
        return FakeLocator([section])

    registry = {SECTION_SELECTOR: section_factory}
    if extra_registry:
        registry.update(extra_registry)
    return FakePage(registry=registry), state


def _make_continue_publishing_modal(cancel_calls=None, publish_now_calls=None, cancel_clickable=True):
    body_text = (
        "Continuar publicando?\n"
        "A verificação de direitos autorais está incompleta. Publicar seu "
        "vídeo agora irá interromper a verificação.\n"
        "Ainda estamos verificando se há possíveis problemas com seu vídeo. "
        "Você deseja continuar publicando antes da verificação ser concluída?"
    )
    children = {
        'button:has-text("Publicar agora")': FakeLocator(
            [FakeElement(tag="button", text="Publicar agora", visible=True, call_log=publish_now_calls)]
        ),
    }
    if cancel_clickable:
        children['button:has-text("Cancelar")'] = FakeLocator(
            [FakeElement(tag="button", text="Cancelar", visible=True, call_log=cancel_calls)]
        )
    return FakeElement(tag="div", text=body_text, visible=True, children=children)


def _modal_appears_n_times(n, cancel_calls=None, publish_now_calls=None, cancel_clickable=True):
    """Registry-factory para 'div[class*="common-modal"]': o modal
    "Continuar publicando?" aparece nas primeiras ``n`` consultas e some
    depois disso (simulando que Cancelar realmente fechou o modal)."""
    state = {"calls": 0}
    modal = _make_continue_publishing_modal(
        cancel_calls=cancel_calls, publish_now_calls=publish_now_calls, cancel_clickable=cancel_clickable,
    )

    def factory():
        state["calls"] += 1
        if state["calls"] <= n:
            return FakeLocator([modal])
        return FakeLocator([])

    return factory, state


# ============================================================================
# A. get_tiktok_preflight_status() / _classify_check_item()
# ============================================================================
class TikTokPreflightStatusTests(unittest.TestCase):
    def test_1_status_passed_via_estrutura(self):
        page, _ = build_checks_page([tiktok.CHECK_PASSED])
        self.assertEqual(tiktok.CHECK_PASSED, tiktok.get_tiktok_preflight_status(page))

    def test_2_status_pending_via_estrutura(self):
        page, _ = build_checks_page([tiktok.CHECK_PENDING])
        self.assertEqual(tiktok.CHECK_PENDING, tiktok.get_tiktok_preflight_status(page))

    def test_3_status_warning_via_estrutura(self):
        page, _ = build_checks_page([tiktok.CHECK_WARNING])
        self.assertEqual(tiktok.CHECK_WARNING, tiktok.get_tiktok_preflight_status(page))

    def test_4_status_failed_via_estrutura(self):
        page, _ = build_checks_page([tiktok.CHECK_FAILED])
        self.assertEqual(tiktok.CHECK_FAILED, tiktok.get_tiktok_preflight_status(page))

    def test_5_secao_nao_encontrada_e_unknown_nao_passed(self):
        page, _ = build_checks_page([])
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok.get_tiktok_preflight_status(page))

    def test_6_classificacao_cai_para_texto_quando_sem_dica_estrutural(self):
        # Copyright sem nenhuma variante estrutural ativa (status=None), mas
        # com texto reconhecível -- deve cair para o fallback textual
        # (_classify_check_item) e resolver PASSED; conteúdo passa via
        # estrutura normalmente.
        section = _build_section(
            copyright_status=None, content_status=tiktok.CHECK_PASSED,
            copyright_body_text="Nenhum problema encontrado.",
        )
        page = FakePage(registry={SECTION_SELECTOR: FakeLocator([section])})
        self.assertEqual(tiktok.CHECK_PASSED, tiktok.get_tiktok_preflight_status(page))

    def test_7_texto_nao_reconhecido_e_unknown_nunca_passed(self):
        section = _build_section(
            copyright_status=None, content_status=tiktok.CHECK_PASSED,
            copyright_body_text="Estado exótico não documentado",
        )
        page = FakePage(registry={SECTION_SELECTOR: FakeLocator([section])})
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok.get_tiktok_preflight_status(page))

    def test_8_agregacao_por_severidade_failed_vence_passed(self):
        section = _build_section(copyright_status=tiktok.CHECK_FAILED, content_status=tiktok.CHECK_PASSED)
        page = FakePage(registry={SECTION_SELECTOR: FakeLocator([section])})
        self.assertEqual(tiktok.CHECK_FAILED, tiktok.get_tiktok_preflight_status(page))

    def test_9_agregacao_por_severidade_pending_vence_passed(self):
        section = _build_section(copyright_status=tiktok.CHECK_PENDING, content_status=tiktok.CHECK_PASSED)
        page = FakePage(registry={SECTION_SELECTOR: FakeLocator([section])})
        self.assertEqual(tiktok.CHECK_PENDING, tiktok.get_tiktok_preflight_status(page))

    def test_10_agregacao_por_severidade_unknown_vence_passed(self):
        # Copyright sem variante ativa e sem texto reconhecível (UNKNOWN),
        # conteúdo PASSED -- geral não pode virar PASSED por omissão.
        section = _build_section(copyright_status=None, content_status=tiktok.CHECK_PASSED)
        page = FakePage(registry={SECTION_SELECTOR: FakeLocator([section])})
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok.get_tiktok_preflight_status(page))


# ============================================================================
# B. wait_for_tiktok_checks()
# ============================================================================
class TikTokWaitForChecksTests(unittest.TestCase):
    def test_1_checks_ja_concluidos_prossegue_sem_erro(self):
        page, _ = build_checks_page([tiktok.CHECK_PASSED])
        result = tiktok.wait_for_tiktok_checks(page, timeout_seconds=5, poll_interval=0.05)
        self.assertEqual(tiktok.CHECK_PASSED, result)

    def test_2_pending_depois_passed_prossegue(self):
        page, state = build_checks_page([tiktok.CHECK_PENDING, tiktok.CHECK_PENDING, tiktok.CHECK_PASSED])
        result = tiktok.wait_for_tiktok_checks(page, timeout_seconds=5, poll_interval=0.02)
        self.assertEqual(tiktok.CHECK_PASSED, result)
        self.assertGreaterEqual(state["n"], 3)

    def test_3_pending_ate_timeout_aborta_sem_publicar(self):
        page, _ = build_checks_page([tiktok.CHECK_PENDING])
        with self.assertRaises(RuntimeError) as ctx:
            tiktok.wait_for_tiktok_checks(page, timeout_seconds=0.2, poll_interval=0.05)
        self.assertIn("não concluíram", str(ctx.exception))

    def test_4_warning_aborta_sem_esperar_timeout_inteiro(self):
        page, _ = build_checks_page([tiktok.CHECK_WARNING])
        with self.assertRaises(RuntimeError) as ctx:
            tiktok.wait_for_tiktok_checks(page, timeout_seconds=30, poll_interval=0.05)
        self.assertIn("aviso (WARNING)", str(ctx.exception))

    def test_5_failed_aborta_sem_esperar_timeout_inteiro(self):
        page, _ = build_checks_page([tiktok.CHECK_FAILED])
        with self.assertRaises(RuntimeError) as ctx:
            tiktok.wait_for_tiktok_checks(page, timeout_seconds=30, poll_interval=0.05)
        self.assertIn("problema (FAILED)", str(ctx.exception))

    def test_6_estado_desconhecido_e_tratado_como_pending_ate_timeout_fail_closed(self):
        page, _ = build_checks_page([])  # seção não encontrada -> UNKNOWN
        with self.assertRaises(RuntimeError) as ctx:
            tiktok.wait_for_tiktok_checks(page, timeout_seconds=0.2, poll_interval=0.05)
        self.assertIn("não concluíram", str(ctx.exception))


# ============================================================================
# C. handle_continue_publishing_modal_if_present()
# ============================================================================
class TikTokContinuePublishingModalTests(unittest.TestCase):
    def test_1_modal_ausente_devolve_false_sem_clicar_em_nada(self):
        page = FakePage(registry={})
        self.assertFalse(tiktok.handle_continue_publishing_modal_if_present(page))

    def test_2_modal_presente_clica_cancelar_nunca_publicar_agora(self):
        cancel_calls, publish_now_calls = [], []
        modal = _make_continue_publishing_modal(cancel_calls=cancel_calls, publish_now_calls=publish_now_calls)
        page = FakePage(registry={'div[class*="common-modal"]': FakeLocator([modal])})
        with mock.patch.object(tiktok, "save_debug", return_value=None):
            handled = tiktok.handle_continue_publishing_modal_if_present(page)
        self.assertTrue(handled)
        self.assertEqual(1, cancel_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"))

    def test_3_cancelar_nao_encontrado_aborta_em_vez_de_arriscar_publicar_agora(self):
        cancel_calls, publish_now_calls = [], []
        modal = _make_continue_publishing_modal(
            cancel_calls=cancel_calls, publish_now_calls=publish_now_calls, cancel_clickable=False,
        )
        page = FakePage(registry={'div[class*="common-modal"]': FakeLocator([modal])})
        with mock.patch.object(tiktok, "save_debug", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                tiktok.handle_continue_publishing_modal_if_present(page)
        self.assertIn("Cancelar", str(ctx.exception))
        self.assertEqual(0, publish_now_calls.count("click"), "NUNCA clicar em Publicar agora")

    def test_4_modal_generico_sem_o_titulo_nao_e_confundido(self):
        outro_modal = FakeElement(tag="div", text="Tem certeza que deseja sair sem salvar?", visible=True)
        page = FakePage(registry={'div[class*="common-modal"]': FakeLocator([outro_modal])})
        self.assertFalse(tiktok.handle_continue_publishing_modal_if_present(page))


# ============================================================================
# D. schedule_one() ponta a ponta com a espera de verificações
# ============================================================================
class TikTokScheduleOnePreflightIntegrationTests(unittest.TestCase):
    def _prepare_page(self, target_dt, *, status_sequence, modal_appears_times=0,
                       cancel_clickable=True, final_button_calls=None,
                       publish_now_calls=None, cancel_calls=None):
        page, time_field, date_field, final_button = build_page(
            target_dt, observed_hour_text=f"{target_dt.hour:02d}",
            observed_minute_text=f"{int(round(target_dt.minute / 5.0) * 5):02d}",
            observed_date_text=f"{target_dt.day:02d}/{target_dt.month:02d}/{target_dt.year}",
            final_button_calls=final_button_calls,
        )
        file_input_el = FakeElement(tag="input", attrs={"type": "file"})
        file_input_el.set_input_files = lambda *a, **kw: None
        page._registry['input[type="file"]'] = FakeLocator([file_input_el])
        caption_editor = FakeElement(tag="div", attrs={"contenteditable": "true", "role": "textbox"}, text="")
        page._registry['[contenteditable="true"][role="textbox"]'] = FakeLocator([caption_editor])
        page._registry["body"] = FakeLocator([FakeElement(text="scheduled")])

        checks_page, checks_state = build_checks_page(status_sequence)
        page._registry[SECTION_SELECTOR] = checks_page._registry[SECTION_SELECTOR]

        if modal_appears_times:
            factory, modal_state = _modal_appears_n_times(
                modal_appears_times, cancel_calls=cancel_calls, publish_now_calls=publish_now_calls,
                cancel_clickable=cancel_clickable,
            )
            page._registry['div[class*="common-modal"]'] = factory

        return page

    def _run(self, page, cfg, target_dt, capture_print=False):
        """Roda schedule_one() real com os passos anteriores ao agendamento
        de horário mockados (login, upload, legenda, toggle). time.sleep()
        também é mockado (no-op) para não pagar os sleeps fixos do fluxo real
        (upload, etc.) -- isso NÃO afeta a correção do timeout de
        wait_for_tiktok_checks(), que usa time.time() (real) como relógio,
        não a contagem de sleeps."""
        fixed_now = target_dt.astimezone(ZoneInfo("UTC"))
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tiktok, "utc_now", return_value=fixed_now))
            stack.enter_context(mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None))
            stack.enter_context(mock.patch.object(tiktok, "set_caption", return_value=None))
            stack.enter_context(mock.patch.object(tiktok, "click_schedule_toggle", return_value=True))
            stack.enter_context(mock.patch.object(tiktok, "challenge_detected", return_value=False))
            stack.enter_context(mock.patch.object(tiktok, "save_debug", return_value=None))
            stack.enter_context(mock.patch.object(tiktok.time, "sleep", return_value=None))
            mock_print = stack.enter_context(mock.patch("builtins.print")) if capture_print else None
            tiktok.schedule_one(page, cfg, __import__("pathlib").Path("v.mp4"), "legenda", target_dt)
            return mock_print

    def test_1_checks_ja_concluidos_agenda_normalmente(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls, publish_now_calls = [], []
        page = self._prepare_page(
            target_dt, status_sequence=[tiktok.CHECK_PASSED],
            final_button_calls=final_calls, publish_now_calls=publish_now_calls,
        )
        self._run(page, {}, target_dt)
        self.assertEqual(1, final_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"))

    def test_2_pending_depois_passed_prossegue(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls, publish_now_calls = [], []
        page = self._prepare_page(
            target_dt, status_sequence=[tiktok.CHECK_PENDING, tiktok.CHECK_PENDING, tiktok.CHECK_PASSED],
            final_button_calls=final_calls, publish_now_calls=publish_now_calls,
        )
        self._run(page, {"timeout_verificacoes_tiktok_segundos": 5}, target_dt)
        self.assertEqual(1, final_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"))

    def test_3_pending_ate_timeout_aborta_sem_clicar_final(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls, publish_now_calls = [], []
        page = self._prepare_page(
            target_dt, status_sequence=[tiktok.CHECK_PENDING],
            final_button_calls=final_calls, publish_now_calls=publish_now_calls,
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._run(page, {"timeout_verificacoes_tiktok_segundos": 0.2}, target_dt)
        self.assertIn("não concluíram", str(ctx.exception))
        self.assertEqual(0, final_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"))

    def test_4_warning_nao_forca_publicacao(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls, publish_now_calls = [], []
        page = self._prepare_page(
            target_dt, status_sequence=[tiktok.CHECK_WARNING],
            final_button_calls=final_calls, publish_now_calls=publish_now_calls,
        )
        with self.assertRaises(RuntimeError):
            self._run(page, {}, target_dt)
        self.assertEqual(0, final_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"))

    def test_5_failed_nao_forca_publicacao(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls, publish_now_calls = [], []
        page = self._prepare_page(
            target_dt, status_sequence=[tiktok.CHECK_FAILED],
            final_button_calls=final_calls, publish_now_calls=publish_now_calls,
        )
        with self.assertRaises(RuntimeError):
            self._run(page, {}, target_dt)
        self.assertEqual(0, final_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"))

    def test_6_estado_desconhecido_fail_closed(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls, publish_now_calls = [], []
        page = self._prepare_page(
            target_dt, status_sequence=[],  # seção não encontrada -> UNKNOWN
            final_button_calls=final_calls, publish_now_calls=publish_now_calls,
        )
        with self.assertRaises(RuntimeError):
            self._run(page, {"timeout_verificacoes_tiktok_segundos": 0.2}, target_dt)
        self.assertEqual(0, final_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"))

    def test_7_modal_aparece_uma_vez_cancela_e_permite_retry_apos_checks_positivos(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls, publish_now_calls, cancel_calls = [], [], []
        page = self._prepare_page(
            target_dt, status_sequence=[tiktok.CHECK_PASSED],
            modal_appears_times=1, final_button_calls=final_calls,
            publish_now_calls=publish_now_calls, cancel_calls=cancel_calls,
        )
        self._run(page, {}, target_dt)
        self.assertEqual(2, final_calls.count("click"), "1ª tentativa + 1 retry após cancelar o modal")
        self.assertEqual(1, cancel_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"), "NUNCA clicar em Publicar agora")

    def test_8_modal_aparece_de_novo_apos_retry_aborta_definitivamente(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls, publish_now_calls, cancel_calls = [], [], []
        page = self._prepare_page(
            target_dt, status_sequence=[tiktok.CHECK_PASSED],
            modal_appears_times=2, final_button_calls=final_calls,
            publish_now_calls=publish_now_calls, cancel_calls=cancel_calls,
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._run(page, {}, target_dt)
        self.assertIn("retry único já", str(ctx.exception))
        self.assertEqual(2, final_calls.count("click"), "nunca uma 3ª tentativa automática")
        self.assertEqual(2, cancel_calls.count("click"))
        self.assertEqual(0, publish_now_calls.count("click"), "NUNCA clicar em Publicar agora")

    def test_9_nenhum_cenario_de_bloqueio_produz_mensagem_de_sucesso(self):
        """Em qualquer cenário de bloqueio (pending/timeout, warning, failed,
        unknown, modal repetido), a exceção propaga ANTES do bloco de
        detecção de sucesso -- nunca "confirma" uma publicação que não
        aconteceu."""
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        page = self._prepare_page(target_dt, status_sequence=[tiktok.CHECK_FAILED])
        printed_calls = []

        def spy_print(*args, **kwargs):
            printed_calls.append(" ".join(str(a) for a in args))

        with self.assertRaises(RuntimeError):
            fixed_now = target_dt.astimezone(ZoneInfo("UTC"))
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(tiktok, "utc_now", return_value=fixed_now))
                stack.enter_context(mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None))
                stack.enter_context(mock.patch.object(tiktok, "set_caption", return_value=None))
                stack.enter_context(mock.patch.object(tiktok, "click_schedule_toggle", return_value=True))
                stack.enter_context(mock.patch.object(tiktok, "challenge_detected", return_value=False))
                stack.enter_context(mock.patch.object(tiktok, "save_debug", return_value=None))
                stack.enter_context(mock.patch.object(tiktok.time, "sleep", return_value=None))
                stack.enter_context(mock.patch("builtins.print", side_effect=spy_print))
                tiktok.schedule_one(page, {}, __import__("pathlib").Path("v.mp4"), "legenda", target_dt)
        self.assertNotIn(
            True,
            ["TikTok confirmou/agiu como agendado" in p for p in printed_calls],
        )


if __name__ == "__main__":
    unittest.main()
