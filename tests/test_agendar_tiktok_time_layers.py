# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — TIKTOK — CORREÇÃO DO BUG REAL WINDOWS (TARGET 10:00 -> UI 21:00)

Histórico deste arquivo:
  Rodada anterior (Candidato C -- correção cirúrgica): estes testes provavam
  que set_schedule_datetime() relia data+hora da UI ANTES do clique final e
  abortava em caso de divergência, usando uma comparação escopada por
  container (hour_list/minute_list assumidos por índice + get_by_text) e um
  parser de data que só aceitava DD/MM/YYYY.

  Evidência Windows real de 19/09/2026 mostrou dois problemas que os testes
  antigos não cobriam:

    BUG 1 -- o TikTok Studio real devolveu a data como '2026-09-20' (ISO),
    formato que parse_observed_date_text() não reconhecia -- abortando mesmo
    quando a data estava certa.

    BUG 2 -- TARGET_TIME = 10:00, mas a UI real ficou mostrando 21:00 depois
    da interação com o seletor de horário. O clique "aconteceu", mas nada
    garantia que a lista certa (HORA vs MINUTO) tinha sido a que realmente
    recebeu o clique, nem que a UI de fato mudou para o valor pedido antes de
    prosseguir.

  Esta correção:
    - aceita YYYY-MM-DD além de DD/MM/YYYY em parse_observed_date_text();
    - reestrutura a seleção de hora/minuto para usar a estrutura real do
      TikTok (div.tiktok-timepicker-option-item / span.tiktok-timepicker-option-text),
      escopada estritamente à lista (HORA ou MINUTO) sendo preenchida --
      nunca uma busca de texto global na página;
    - lê de volta o campo de horário DEPOIS de cada clique (hora, depois
      minuto) e só prossegue se a UI realmente refletir o valor pedido;
    - permite no máximo 1 retry controlado por componente (DOM recriado no
      meio da seleção) e aborta definitivamente se ainda não bater -- nunca
      escolhe outro horário, nunca tenta um horário "próximo";
    - preserva o gate final já existente (read_observed_schedule_datetime),
      que continua sendo a última barreira antes de click_final_schedule().

Este arquivo é organizado em 4 partes:
  A. TikTokReadBackFunctionTests       -- unidade de read_observed_schedule_datetime()
  B. TikTokTimePickerSelectionTests    -- unidade de _select_time_component() / _click_option_in_list()
  C. TikTokScheduleDatetimeIntegrationTests -- set_schedule_datetime() ponta a ponta
  D. TikTokScheduleOneClickCountTests  -- schedule_one() ponta a ponta (nunca clica o botão final em mismatch)
"""
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo
import unittest

from _sistema import agendar_tiktok as tiktok
from tests.fakes_playwright import FakeElement, FakeLocator, FakePage


SAO_PAULO = ZoneInfo("America/Sao_Paulo")


class CalendarAdapter(FakeElement):
    def __init__(self, day_el):
        super().__init__(tag="div", attrs={"class": "calendar-wrapper"}, visible=True)
        self._day_el = day_el

    def locator(self, selector):
        if selector == 'span[class*="day"]':
            return FakeLocator([self._day_el])
        if selector in (".arrow", "button"):
            return FakeLocator([])
        return super().locator(selector)


def build_read_back_page(target_dt, observed_hour_text, observed_minute_text, observed_date_text):
    """Registry mínimo para exercitar read_observed_schedule_datetime()
    ISOLADAMENTE, sem passar pelo fluxo de clique do seletor de hora/minuto
    (que tem sua própria suíte em TikTokTimePickerSelectionTests /
    TikTokScheduleDatetimeIntegrationTests)."""
    time_field = FakeElement(
        tag="input",
        attrs={"placeholder": "hh:mm", "aria-label": "hora", "value": f"{observed_hour_text}:{observed_minute_text}"},
    )
    date_field = FakeElement(
        tag="input",
        attrs={"placeholder": "dd/mm/aaaa", "aria-label": "data", "value": observed_date_text},
    )
    registry = {
        'div[data-e2e="schedule_container"] input.TUXTextInputCore-input':
            FakeLocator([time_field, date_field]),
    }
    return FakePage(registry=registry), time_field, date_field


def _make_time_option_item(label, call_log=None):
    """Um item real do timepicker do TikTok:
        div.tiktok-timepicker-option-item
            span.tiktok-timepicker-option-text  (texto = label)
    """
    text_span = FakeElement(tag="span", text=label, visible=True)
    return FakeElement(
        tag="div", visible=True, call_log=call_log,
        children={"span.tiktok-timepicker-option-text": FakeLocator([text_span])},
    )


def _make_option_list(items):
    """Uma lista div.tiktok-timepicker-option-list contendo os itens dados."""
    return FakeElement(
        tag="div", visible=True,
        children={"div.tiktok-timepicker-option-item": FakeLocator(list(items))},
    )


def _build_picker_page(hour_items, minute_items, time_value, date_value="01/10/2026"):
    """Registry mínimo para exercitar _select_time_component()/_click_option_in_list()
    isoladamente, sem o restante do fluxo de set_schedule_datetime()."""
    time_field = FakeElement(tag="input", attrs={"aria-label": "hora", "value": time_value})
    date_field = FakeElement(tag="input", attrs={"aria-label": "data", "value": date_value})
    hour_list = _make_option_list(hour_items)
    minute_list = _make_option_list(minute_items)
    registry = {
        'div[data-e2e="schedule_container"] input.TUXTextInputCore-input':
            FakeLocator([time_field, date_field]),
        "div.tiktok-timepicker-option-list": FakeLocator([hour_list, minute_list]),
    }
    return FakePage(registry=registry), time_field


def _register_checks_passed(page):
    """Registra uma seção "Verificações" já CHECK_PASSED, para que testes
    deste arquivo (focados em data/hora, não no gate de verificações do GATE
    19.5 seguinte) não fiquem presos no wait_for_tiktok_checks() real de
    schedule_one() por falta de seção registrada (o que UNKNOWN trata como
    PENDING até o timeout, nunca como sucesso -- comportamento correto, mas
    irrelevante para o que este arquivo testa).

    GATE 19.5 (rodada de correção de detecção de itens): usa a mesma
    estrutura real de identidade (data-e2e do check de copyright, headline
    do check de conteúdo) + grupo de 5 variantes de status que a produção
    agora exige -- o antigo `div[data-e2e*="verification_item"]` nunca é
    mais consultado por _gather_check_items()."""
    from _sistema import agendar_tiktok as _tiktok_mod

    all_variants = []

    copyright_wrapper = FakeElement(tag="div", text="Verificação de direitos autorais de música", visible=True)
    copyright_variants = []
    for v in ("status-ready", "status-checking", "status-error", "status-warn", "status-success"):
        active = v == "status-success"
        copyright_variants.append(FakeElement(
            tag="div", attrs={"class": f"status-result {v}", "data-show": "true" if active else "false"},
            visible=active, parent=copyright_wrapper,
        ))
    copyright_identity = FakeElement(
        tag="div", attrs={"data-e2e": "copyright_container"},
        text="Verificação de direitos autorais de música", visible=True, parent=copyright_wrapper,
    )
    copyright_wrapper._children = {
        _tiktok_mod.STATUS_RESULT_SELECTOR: FakeLocator(copyright_variants),
        _tiktok_mod.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([copyright_identity]),
    }
    all_variants.extend(copyright_variants)

    content_wrapper = FakeElement(tag="div", text="Verificação de conteúdo simples", visible=True)
    content_variants = []
    for v in ("status-ready", "status-checking", "status-error", "status-warn", "status-success"):
        active = v == "status-success"
        content_variants.append(FakeElement(
            tag="div", attrs={"class": f"status-result {v}", "data-show": "true" if active else "false"},
            visible=active, parent=content_wrapper,
        ))
    content_headline = FakeElement(
        tag="div", attrs={"class": "headline-wrapper"},
        text="Verificação de conteúdo simples", visible=True, parent=content_wrapper,
    )
    content_wrapper._children = {
        _tiktok_mod.STATUS_RESULT_SELECTOR: FakeLocator(content_variants),
        _tiktok_mod.CONTENT_HEADLINE_SELECTOR: FakeLocator([content_headline]),
    }
    all_variants.extend(content_variants)

    passed_section = FakeElement(
        tag="div", visible=True,
        children={
            _tiktok_mod.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([copyright_identity]),
            _tiktok_mod.CONTENT_HEADLINE_SELECTOR: FakeLocator([content_headline]),
            _tiktok_mod.STATUS_RESULT_SELECTOR: FakeLocator(all_variants),
        },
    )
    page._registry['div[data-e2e="post_verifications"]'] = FakeLocator([passed_section])


def build_page(target_dt, *, observed_hour_text, observed_minute_text, observed_date_text,
                final_button_calls=None, include_hour_item=True, include_minute_item=True):
    """Monta um FakePage cobrindo os seletores de set_schedule_datetime() E de
    read_observed_schedule_datetime(), com a estrutura real do timepicker
    (div.tiktok-timepicker-option-item / span.tiktok-timepicker-option-text).

    observed_*_text: o que o campo REAL mostraria depois da interação -- o
    ponto central do teste é que isso é lido de volta, não assumido a partir
    do que foi clicado (por isso é independente dos itens/labels clicáveis).
    """
    hour_txt = f"{target_dt.hour:02d}"
    minute = int(round(target_dt.minute / 5.0) * 5)
    minute_txt = f"{minute:02d}"

    time_field = FakeElement(
        tag="input",
        attrs={"placeholder": "hh:mm", "aria-label": "hora", "value": f"{observed_hour_text}:{observed_minute_text}"},
    )
    date_field = FakeElement(
        tag="input",
        attrs={"placeholder": "dd/mm/aaaa", "aria-label": "data", "value": observed_date_text},
    )

    hour_items = [_make_time_option_item(hour_txt)] if include_hour_item else []
    minute_items = [_make_time_option_item(minute_txt)] if include_minute_item else []
    hour_list = _make_option_list(hour_items)
    minute_list = _make_option_list(minute_items)

    day_el = FakeElement(
        tag="span", text=str(target_dt.day), attrs={"class": "calendar-day"},
        parent=FakeElement(attrs={"class": ""}),
    )
    calendar_adapter = CalendarAdapter(day_el)

    final_button = FakeElement(
        tag="button",
        attrs={"data-e2e": "post_video_button"},
        visible=True,
        call_log=final_button_calls if final_button_calls is not None else [],
    )

    registry = {
        'div[data-e2e="schedule_container"] input.TUXTextInputCore-input':
            FakeLocator([time_field, date_field]),
        "div.tiktok-timepicker-option-list": FakeLocator([hour_list, minute_list]),
        ".calendar-wrapper": FakeLocator([calendar_adapter]),
        'button[data-e2e="post_video_button"]': FakeLocator([final_button]),
    }

    page = FakePage(registry=registry)
    return page, time_field, date_field, final_button


# ============================================================================
# A. read_observed_schedule_datetime() isolado
# ============================================================================
class TikTokReadBackFunctionTests(unittest.TestCase):
    def test_1_esperado_igual_observado_nao_levanta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page(target_dt, "18", "00", "01/10/2026")
        observed = tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertEqual(target_dt, observed)

    def test_2_hora_divergente_e_detectavel(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page(target_dt, "18", "30", "01/10/2026")
        observed = tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertNotEqual(target_dt, observed)

    def test_3_data_divergente_e_detectavel_mesmo_com_hora_igual(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page(target_dt, "18", "00", "02/10/2026")
        observed = tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertNotEqual(target_dt, observed)

    def test_4_campo_de_horario_vazio_aborta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, time_field, _ = build_read_back_page(target_dt, "18", "00", "01/10/2026")
        time_field.attrs.pop("value", None)
        with self.assertRaises(RuntimeError) as ctx:
            tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("vazio", str(ctx.exception))

    def test_5_campo_de_data_vazio_aborta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, date_field = build_read_back_page(target_dt, "18", "00", "01/10/2026")
        date_field.attrs.pop("value", None)
        with self.assertRaises(RuntimeError) as ctx:
            tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("vazio", str(ctx.exception))

    def test_6_horario_ilegivel_aborta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page(target_dt, "dezoito", "zero", "01/10/2026")
        with self.assertRaises(RuntimeError) as ctx:
            tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("Horário exibido", str(ctx.exception))

    def test_7_data_ilegivel_aborta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page(target_dt, "18", "00", "primeiro de outubro")
        with self.assertRaises(RuntimeError) as ctx:
            tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("Data exibida", str(ctx.exception))

    def test_8_data_iso_yyyy_mm_dd_e_aceita(self):
        """BUG 1 (evidência Windows real, 19/09/2026): o TikTok Studio
        devolveu a data como '2026-09-20' (ISO). O parser antigo só aceitava
        DD/MM/YYYY e abortava mesmo quando a data estava correta."""
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page(target_dt, "10", "00", "2026-09-20")
        observed = tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertEqual(target_dt, observed)

    def test_9_data_iso_com_dia_mes_de_um_digito_e_aceita(self):
        target_dt = datetime(2026, 1, 5, 9, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page(target_dt, "09", "00", "2026-1-5")
        observed = tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertEqual(target_dt, observed)

    def test_10_leitura_com_excecao_no_input_value_aborta_de_forma_controlada(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, time_field, _ = build_read_back_page(target_dt, "18", "00", "01/10/2026")

        def boom():
            raise RuntimeError("DOM desconectado")
        time_field.input_value = boom
        time_field.attrs.pop("value", None)  # também falha o fallback get_attribute

        with self.assertRaises(RuntimeError) as ctx:
            tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("vazio", str(ctx.exception))

    def test_11_relocaliza_via_page_locator_em_vez_de_referencia_antiga(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page(target_dt, "18", "00", "01/10/2026")
        original_locator = page.locator
        calls = []

        def counting_locator(selector):
            calls.append(selector)
            return original_locator(selector)

        page.locator = counting_locator
        tiktok.read_observed_schedule_datetime(page, target_dt)
        self.assertIn('div[data-e2e="schedule_container"] input.TUXTextInputCore-input', calls)


# ============================================================================
# B. _select_time_component() / _click_option_in_list() / _locate_time_option()
#    isolados -- a camada que corrige o BUG 2 (TARGET 10:00 -> UI 21:00).
# ============================================================================
class TikTokTimePickerSelectionTests(unittest.TestCase):
    def test_1_selecao_com_sucesso_confirma_via_read_back(self):
        hour_item = _make_time_option_item("10")
        minute_item = _make_time_option_item("00")
        page, _ = _build_picker_page([hour_item], [minute_item], time_value="10:00")
        result = tiktok._select_time_component(page, 0, "10", "hora", tiktok._hour_ui_matches(10))
        self.assertEqual("10:00", result)
        self.assertIn("click", hour_item.call_log)

    def test_2_clique_sem_efeito_na_ui_aborta_apos_retry(self):
        """BUG 2 reproduzido no nível da função: o item existe e o clique
        "acontece" (fica no call_log), mas o campo de horário nunca reflete
        o valor pedido -- exatamente o padrão observado no Windows real
        (TARGET 10:00 -> UI 21:00)."""
        hour_item = _make_time_option_item("10")
        page, _ = _build_picker_page([hour_item], [_make_time_option_item("00")], time_value="21:00")
        with self.assertRaises(RuntimeError) as ctx:
            tiktok._select_time_component(page, 0, "10", "hora", tiktok._hour_ui_matches(10))
        self.assertIn("continuou mostrando", str(ctx.exception))
        # 1 tentativa inicial + 1 retry controlado -- nunca mais que isso.
        self.assertEqual(2, hour_item.call_log.count("click"))

    def test_3_item_nao_encontrado_apos_retries_aborta(self):
        page, _ = _build_picker_page([], [_make_time_option_item("00")], time_value="09:00")
        with self.assertRaises(RuntimeError) as ctx:
            tiktok._select_time_component(page, 0, "10", "hora", tiktok._hour_ui_matches(10))
        self.assertIn("Não encontrei", str(ctx.exception))

    def test_4_retry_controlado_quando_lista_e_recriada_no_meio_da_selecao(self):
        """Simula o TikTok recriando a lista de hora entre a 1ª e a 2ª
        tentativa (1ª tentativa: lista vazia; 2ª: item presente). Deve
        conseguir 1 retry e ter sucesso -- sem nunca escolher outro valor."""
        hour_item = _make_time_option_item("10")
        state = {"attempt": 0}

        def hour_list_factory():
            state["attempt"] += 1
            if state["attempt"] == 1:
                return _make_option_list([])
            return _make_option_list([hour_item])

        minute_list = _make_option_list([_make_time_option_item("00")])
        time_field = FakeElement(tag="input", attrs={"aria-label": "hora", "value": "10:00"})
        date_field = FakeElement(tag="input", attrs={"aria-label": "data", "value": "01/10/2026"})
        registry = {
            'div[data-e2e="schedule_container"] input.TUXTextInputCore-input':
                FakeLocator([time_field, date_field]),
            "div.tiktok-timepicker-option-list": lambda: FakeLocator([hour_list_factory(), minute_list]),
        }
        page = FakePage(registry=registry)
        result = tiktok._select_time_component(page, 0, "10", "hora", tiktok._hour_ui_matches(10))
        self.assertEqual("10:00", result)
        self.assertEqual(2, state["attempt"])

    def test_5_lista_de_hora_nao_confunde_com_lista_de_minuto(self):
        """A lista de HORA e a de MINUTO podem conter o mesmo texto (ex.:
        hora "10" e minuto "10"). Selecionar a hora "10" precisa clicar
        SOMENTE no item da lista de hora, nunca no item homônimo da lista de
        minuto -- é exatamente o tipo de confusão que pode ter causado o BUG 2."""
        hour_item_09 = _make_time_option_item("09")
        hour_item_10 = _make_time_option_item("10")
        minute_item_10 = _make_time_option_item("10")
        minute_item_15 = _make_time_option_item("15")
        page, _ = _build_picker_page(
            [hour_item_09, hour_item_10], [minute_item_10, minute_item_15], time_value="10:00",
        )
        tiktok._select_time_component(page, 0, "10", "hora", tiktok._hour_ui_matches(10))
        self.assertIn("click", hour_item_10.call_log)
        self.assertNotIn("click", minute_item_10.call_log)
        self.assertNotIn("click", hour_item_09.call_log)

    def test_6_lista_de_minuto_nao_confunde_com_lista_de_hora(self):
        hour_item_09 = _make_time_option_item("09")
        hour_item_10 = _make_time_option_item("10")
        minute_item_05 = _make_time_option_item("05")
        minute_item_10 = _make_time_option_item("10")
        page, _ = _build_picker_page(
            [hour_item_09, hour_item_10], [minute_item_05, minute_item_10], time_value="09:10",
        )
        tiktok._select_time_component(page, 1, "10", "minuto", tiktok._hour_minute_ui_matches(9, 10))
        self.assertIn("click", minute_item_10.call_log)
        self.assertNotIn("click", hour_item_10.call_log)
        self.assertNotIn("click", minute_item_05.call_log)


# ============================================================================
# C. set_schedule_datetime() ponta a ponta
# ============================================================================
class TikTokScheduleDatetimeIntegrationTests(unittest.TestCase):
    def _run(self, target_dt, observed_hour_text, observed_minute_text, observed_date_text, **kw):
        page, time_field, date_field, final_button = build_page(
            target_dt, observed_hour_text=observed_hour_text, observed_minute_text=observed_minute_text,
            observed_date_text=observed_date_text, **kw,
        )
        fixed_now = target_dt.astimezone(timezone.utc)
        with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
             mock.patch.object(tiktok, "save_debug", return_value=None):
            tiktok.set_schedule_datetime(page, target_dt)
        return page

    def test_1_caminho_feliz_sem_divergencia_nao_levanta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        self._run(target_dt, "18", "00", "01/10/2026")

    def test_2_bug_real_windows_target_10_00_ui_21_00_aborta(self):
        """Reprodução exata da evidência Windows real (19/09/2026):
        TARGET_TIME=10:00, campo de horário passou a exibir 21:00 depois da
        interação. Deve abortar ANTES do clique final, sem escolher outro
        horário nem tentar algo 'próximo'."""
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_button_calls = []
        with self.assertRaises(RuntimeError) as ctx:
            self._run(target_dt, "21", "00", "20/09/2026", final_button_calls=final_button_calls)
        self.assertIn("continuou mostrando", str(ctx.exception))
        self.assertEqual(0, final_button_calls.count("click"))

    def test_3_hora_correta_minuto_errado_aborta(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_button_calls = []
        with self.assertRaises(RuntimeError) as ctx:
            self._run(target_dt, "10", "30", "20/09/2026", final_button_calls=final_button_calls)
        self.assertIn("minuto", str(ctx.exception))
        self.assertEqual(0, final_button_calls.count("click"))

    def test_4_data_iso_yyyy_mm_dd_e_aceita_no_fluxo_completo(self):
        """BUG 1 no fluxo completo: quando data e hora realmente batem, o
        formato ISO devolvido pelo TikTok Studio real não pode mais fazer o
        gate final abortar por engano."""
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        self._run(target_dt, "10", "00", "2026-09-20")

    def test_5_data_divergente_aborta_no_gate_final_mesmo_com_hora_certa(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(target_dt, "18", "00", "02/10/2026")
        self.assertIn("não bate", str(ctx.exception))

    def test_6_opcao_de_hora_ausente_no_seletor_aborta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(target_dt, "18", "00", "01/10/2026", include_hour_item=False)
        self.assertIn("Não encontrei", str(ctx.exception))

    def test_7_arredondamento_de_5_minutos_continua_correto_na_comparacao(self):
        """target_dt.minute=17 arredonda para 15 (round-half-to-even:
        round(17/5)=round(3.4)=3 -> 15). A comparação usa o horário JÁ
        ARREDONDADO como esperado, não o minuto bruto do target_dt."""
        target_dt = datetime(2026, 10, 1, 18, 17, tzinfo=SAO_PAULO)
        self._run(target_dt, "18", "15", "01/10/2026")  # não deve levantar
        with self.assertRaises(RuntimeError):
            self._run(target_dt, "18", "17", "01/10/2026")  # UI "ficou" no minuto bruto -> diverge do arredondado

    def test_8_dom_recriado_entre_escrita_e_leituras_e_sempre_relocalizado(self):
        """find_date_time_inputs() é chamado de novo em cada leitura (após
        clicar a hora, após clicar o minuto, e no gate final) -- nunca
        reaproveita uma referência antiga. Trocamos os elementos do registro
        depois da primeira leitura (simulando o TikTok recriando o DOM) e
        confirmamos que set_schedule_datetime() continua enxergando o valor
        NOVO corretamente, em vez de travar com uma referência obsoleta."""
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, time_field, date_field, final_button = build_page(
            target_dt, observed_hour_text="18", observed_minute_text="00", observed_date_text="01/10/2026",
        )
        original_locator = page.locator
        call_count = {"n": 0}
        SEL = 'div[data-e2e="schedule_container"] input.TUXTextInputCore-input'

        def relocating_locator(selector):
            if selector == SEL:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return original_locator(selector)
                new_time = FakeElement(tag="input", attrs={"aria-label": "hora", "value": "18:00"})
                new_date = FakeElement(tag="input", attrs={"aria-label": "data", "value": "01/10/2026"})
                return FakeLocator([new_time, new_date])
            return original_locator(selector)

        page.locator = relocating_locator
        fixed_now = target_dt.astimezone(timezone.utc)
        with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
             mock.patch.object(tiktok, "save_debug", return_value=None):
            tiktok.set_schedule_datetime(page, target_dt)  # não deve levantar
        self.assertGreaterEqual(
            call_count["n"], 2,
            "esperava relocalizar os inputs de novo para as leituras de confirmação",
        )

    def test_9_nunca_chama_botao_final_quando_mismatch(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        final_button_calls = []
        with self.assertRaises(RuntimeError):
            self._run(target_dt, "18", "30", "01/10/2026", final_button_calls=final_button_calls)
        self.assertEqual(0, final_button_calls.count("click"), "click_final_schedule NUNCA deveria ser chamado após um mismatch")


# ============================================================================
# D. schedule_one() ponta a ponta -- prova no nível do fluxo completo real.
# ============================================================================
class TikTokScheduleOneClickCountTests(unittest.TestCase):
    def test_1_chama_botao_final_apenas_apos_validacao_positiva_fluxo_completo(self):
        """Com tudo mockado antes do agendamento de horário (login, upload,
        legenda, toggle), só o set_schedule_datetime()/click_final_schedule()
        reais rodam. Prova, no nível do fluxo completo: horário batendo ->
        botão final é clicado; horário divergente -> botão final NUNCA é
        clicado."""
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)

        def make_page_and_patches(observed_hour, observed_minute, final_button_calls):
            page, time_field, date_field, final_button = build_page(
                target_dt, observed_hour_text=observed_hour, observed_minute_text=observed_minute,
                observed_date_text="01/10/2026", final_button_calls=final_button_calls,
            )
            file_input_el = FakeElement(tag="input", attrs={"type": "file"})
            file_input_el.set_input_files = lambda *a, **kw: None
            page._registry['input[type="file"]'] = FakeLocator([file_input_el])
            caption_editor = FakeElement(tag="div", attrs={"contenteditable": "true", "role": "textbox"}, text="")
            page._registry['[contenteditable="true"][role="textbox"]'] = FakeLocator([caption_editor])
            page._registry["body"] = FakeLocator([FakeElement(text="scheduled")])
            _register_checks_passed(page)
            return page

        # Caso MATCH: botão final é clicado.
        final_calls_match = []
        page_match = make_page_and_patches("18", "00", final_calls_match)
        fixed_now = target_dt.astimezone(timezone.utc)
        with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
             mock.patch.object(tiktok, "save_debug", return_value=None), \
             mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None), \
             mock.patch.object(tiktok, "set_caption", return_value=None), \
             mock.patch.object(tiktok, "click_schedule_toggle", return_value=True), \
             mock.patch.object(tiktok, "challenge_detected", return_value=False), \
             mock.patch.object(tiktok.time, "sleep", return_value=None):
            tiktok.schedule_one(page_match, {}, __import__("pathlib").Path("video.mp4"), "legenda", target_dt)
        self.assertEqual(1, final_calls_match.count("click"), "horário batendo deveria chegar ao clique final")

        # Caso MISMATCH: botão final NUNCA é clicado, e a exceção propaga.
        final_calls_mismatch = []
        page_mismatch = make_page_and_patches("18", "45", final_calls_mismatch)
        with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
             mock.patch.object(tiktok, "save_debug", return_value=None), \
             mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None), \
             mock.patch.object(tiktok, "set_caption", return_value=None), \
             mock.patch.object(tiktok, "click_schedule_toggle", return_value=True), \
             mock.patch.object(tiktok, "challenge_detected", return_value=False), \
             mock.patch.object(tiktok.time, "sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                tiktok.schedule_one(page_mismatch, {}, __import__("pathlib").Path("video.mp4"), "legenda", target_dt)
        self.assertEqual(0, final_calls_mismatch.count("click"), "click_final_schedule NUNCA deveria ser chamado após um mismatch")

    def test_2_comportamento_seguro_de_nao_avancar_video_e_preservado(self):
        """O comportamento observado no Windows real -- 'Parei para NÃO
        pular nem duplicar vídeo. Na próxima execução ele tentará
        novamente' -- depende de schedule_one() propagar a exceção sem
        marcar o item como agendado. Prova isso diretamente: quando o
        horário diverge, schedule_one() levanta e NENHUM estado de sucesso
        é persistido pelo próprio schedule_one() (main() é quem decide o que
        persistir, e só persiste depois de schedule_one() retornar sem
        erro)."""
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, time_field, date_field, final_button = build_page(
            target_dt, observed_hour_text="18", observed_minute_text="45", observed_date_text="01/10/2026",
        )
        file_input_el = FakeElement(tag="input", attrs={"type": "file"})
        file_input_el.set_input_files = lambda *a, **kw: None
        page._registry['input[type="file"]'] = FakeLocator([file_input_el])
        caption_editor = FakeElement(tag="div", attrs={"contenteditable": "true", "role": "textbox"}, text="")
        page._registry['[contenteditable="true"][role="textbox"]'] = FakeLocator([caption_editor])
        page._registry["body"] = FakeLocator([FakeElement(text="scheduled")])
        _register_checks_passed(page)

        fixed_now = target_dt.astimezone(timezone.utc)
        with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
             mock.patch.object(tiktok, "save_debug", return_value=None), \
             mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None), \
             mock.patch.object(tiktok, "set_caption", return_value=None), \
             mock.patch.object(tiktok, "click_schedule_toggle", return_value=True), \
             mock.patch.object(tiktok, "challenge_detected", return_value=False), \
             mock.patch.object(tiktok.time, "sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                tiktok.schedule_one(page, {}, __import__("pathlib").Path("video.mp4"), "legenda", target_dt)


if __name__ == "__main__":
    unittest.main()
