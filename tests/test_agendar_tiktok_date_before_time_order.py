# -*- coding: utf-8 -*-
"""
GATE 19.5 -- CORREÇÃO (rodada de 21/09/2026): "Não encontrei o dia X no
calendário" -- evidência real, Windows, conta @vem.na.bio69, 058.mp4.

Causa raiz investigada: em set_schedule_datetime(), a HORA/MINUTO eram
preenchidos ANTES da DATA. Com a data do formulário ainda em "hoje" e uma
hora-alvo numericamente menor que a hora real do relógio no momento da
execução (ex.: alvo 10:00, execução às 13h), aplicar a hora primeiro fazia
o TikTok considerar o agendamento sem os 15 minutos mínimos de
antecedência -- o campo de horário entrava em estado de erro transitório
(evidência real: `data-has-error="true"`/`aria-invalid="true"` no HTML
capturado no momento da falha, com a mensagem "Agende a publicação com
pelo menos 15 minutos de antecedência" visível, e ZERO ocorrências de
"calendar"/"calendar-wrapper" no DOM -- ou seja, o popover do calendário já
tinha fechado quando o debug foi salvo).

Correção: DATA agora é preenchida ANTES de HORA/MINUTO em
set_schedule_datetime(). Este arquivo prova, via spy de ordem de chamadas
(não há um simulador real de `data-has-error` no fake de DOM -- isso exigiria
reimplementar a validação de front-end do TikTok, fora de escopo), que:

  1. o clique no campo de DATA acontece ANTES de qualquer clique relacionado
     a HORA/MINUTO -- a ordem antiga (hora primeiro) não é mais usada;
  2. isso continua valendo tanto quando a hora-alvo é MENOR que a hora atual
     simulada (o cenário real do 058.mp4) quanto quando é MAIOR (cenário que
     provavelmente já funcionava antes -- sem regressão);
  3. a navegação de mês (month_delta > 0) continua funcionando com a nova
     ordem;
  4. a política de aviso de direitos autorais (ALLOW/BLOCK) NÃO influencia
     esta etapa -- set_schedule_datetime() nem recebe `cfg` como parâmetro,
     e schedule_one() chama esta função bem antes de qualquer leitura de
     copyright_warning_policy (comprovado estruturalmente, não só por
     inspeção).

NÃO toca `_find_checks_section()`, `_gather_check_items()`,
`wait_for_tiktok_checks()` (internals), a pergunta interativa de política,
nem `_ask_interactive_copyright_warning_policy()` -- nada disso está sob
suspeita nesta rodada. NÃO toca agendar_youtube.py.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import inspect
import unittest

from _sistema import agendar_tiktok as tiktok
from tests.fakes_playwright import FakeElement, FakeLocator, FakePage
from tests.test_agendar_tiktok_time_layers import (
    build_page,
    _register_checks_passed,
    _make_option_list,
    _make_time_option_item,
)

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def _order_spy_page(target_dt, *, observed_hour_text, observed_minute_text, observed_date_text):
    """Como build_page(), mas com date_el.click()/time_el.click() decorados
    para registrar, numa lista COMPARTILHADA e em ORDEM, qual campo foi
    clicado primeiro -- a prova direta de que a correção desta rodada
    (DATA antes de HORA) está mesmo em vigor."""
    page, time_field, date_field, final_button = build_page(
        target_dt, observed_hour_text=observed_hour_text, observed_minute_text=observed_minute_text,
        observed_date_text=observed_date_text,
    )
    order = []
    real_time_click = time_field.click
    real_date_click = date_field.click

    def spying_time_click(force=False):
        order.append("HORA")
        return real_time_click(force=force)

    def spying_date_click(force=False):
        order.append("DATA")
        return real_date_click(force=force)

    time_field.click = spying_time_click
    date_field.click = spying_date_click
    return page, order, final_button


class DataAntesDeHoraOrderTests(unittest.TestCase):
    def _run(self, target_dt, observed_hour_text, observed_minute_text, observed_date_text):
        page, order, final_button = _order_spy_page(
            target_dt, observed_hour_text=observed_hour_text,
            observed_minute_text=observed_minute_text, observed_date_text=observed_date_text,
        )
        fixed_now = target_dt.astimezone(timezone.utc)
        with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
             mock.patch.object(tiktok, "save_debug", return_value=None):
            tiktok.set_schedule_datetime(page, target_dt)
        return order

    def test_1_clique_na_data_acontece_antes_do_clique_na_hora(self):
        """Cenário genérico -- confirma a nova ordem estrutural, independente
        de a hora-alvo ser maior ou menor que qualquer relógio."""
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        order = self._run(target_dt, "18", "00", "01/10/2026")
        self.assertEqual(["DATA", "HORA"], order)

    def test_2_reproducao_do_caso_real_058mp4_hora_alvo_menor_que_agora(self):
        """Cenário real do 058.mp4: hora-alvo (10:00) numericamente MENOR
        que a hora do relógio no momento simulado da execução (13:13) --
        exatamente o padrão que, com a ordem antiga (hora primeiro), fazia
        o TikTok considerar o agendamento sem antecedência suficiente e
        colocar o campo de horário em estado de erro antes da data ser
        corrigida. Com a nova ordem (data primeiro), a data já está certa
        antes da hora ser aplicada -- e o fluxo completo (incluindo o
        clique final) tem que terminar sem erro."""
        target_dt = datetime(2026, 9, 22, 10, 0, tzinfo=SAO_PAULO)
        agora_simulado_utc = datetime(2026, 9, 21, 16, 13, tzinfo=timezone.utc)  # ~13:13 em SP
        page, order, final_button = _order_spy_page(
            target_dt, observed_hour_text="10", observed_minute_text="00", observed_date_text="22/09/2026",
        )
        with mock.patch.object(tiktok, "utc_now", return_value=agora_simulado_utc), \
             mock.patch.object(tiktok, "save_debug", return_value=None):
            tiktok.set_schedule_datetime(page, target_dt)  # não deve levantar
        self.assertEqual(["DATA", "HORA"], order, "data precisa ser preenchida antes da hora, mesmo com hora-alvo < hora atual")

    def test_3_hora_alvo_maior_que_agora_continua_funcionando_sem_regressao(self):
        """Cenário que provavelmente já funcionava antes desta correção --
        hora-alvo (20:00) MAIOR que a hora atual simulada (10:00). Confirma
        que inverter a ordem não quebrou esse caso."""
        target_dt = datetime(2026, 10, 1, 20, 0, tzinfo=SAO_PAULO)
        agora_simulado_utc = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)  # ~10h em SP
        page, order, final_button = _order_spy_page(
            target_dt, observed_hour_text="20", observed_minute_text="00", observed_date_text="01/10/2026",
        )
        with mock.patch.object(tiktok, "utc_now", return_value=agora_simulado_utc), \
             mock.patch.object(tiktok, "save_debug", return_value=None):
            tiktok.set_schedule_datetime(page, target_dt)  # não deve levantar
        self.assertEqual(["DATA", "HORA"], order)

    def test_4_ordem_antiga_hora_antes_da_data_nao_e_mais_usada(self):
        """Teste que FALHARIA se alguém reintroduzisse a ordem antiga
        (hora/minuto antes da data)."""
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        order = self._run(target_dt, "18", "00", "01/10/2026")
        self.assertNotEqual(["HORA", "DATA"], order)
        self.assertEqual("DATA", order[0], "o primeiro clique do preenchimento tem que ser o da DATA")


class ViradaDeMesComNovaOrdemTests(unittest.TestCase):
    def test_virada_de_mes_month_delta_positivo_continua_funcionando(self):
        """month_delta > 0 (o mês-alvo é depois do mês 'atual' simulado) --
        confirma que navegar o calendário (clicar a seta de avançar mês)
        continua funcionando com a data sendo preenchida primeiro."""
        target_dt = datetime(2026, 11, 5, 14, 0, tzinfo=SAO_PAULO)
        # "Hoje" simulado é 20/09/2026 -- 2 meses antes do mês-alvo (novembro).
        agora_simulado_utc = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

        hour_txt, minute_txt = "14", "00"
        time_field = FakeElement(
            tag="input", attrs={"placeholder": "hh:mm", "aria-label": "hora", "value": f"{hour_txt}:{minute_txt}"},
        )
        date_field = FakeElement(
            tag="input", attrs={"placeholder": "dd/mm/aaaa", "aria-label": "data", "value": "05/11/2026"},
        )
        hour_list = _make_option_list([_make_time_option_item(hour_txt)])
        minute_list = _make_option_list([_make_time_option_item(minute_txt)])

        day_el = FakeElement(
            tag="span", text=str(target_dt.day), attrs={"class": "calendar-day"},
            parent=FakeElement(attrs={"class": ""}),
        )
        month_arrow_calls = []
        next_arrow = FakeElement(tag="button", attrs={"class": "arrow"}, visible=True, call_log=month_arrow_calls)
        prev_arrow = FakeElement(tag="button", attrs={"class": "arrow"}, visible=True)

        class MonthAwareCalendarAdapter(FakeElement):
            def __init__(self):
                super().__init__(tag="div", attrs={"class": "calendar-wrapper"}, visible=True)

            def locator(self, selector):
                if selector == ".arrow":
                    return FakeLocator([prev_arrow, next_arrow])
                if selector == 'span[class*="day"]':
                    return FakeLocator([day_el])
                if selector == "button":
                    return FakeLocator([])
                return super().locator(selector)

        calendar_adapter = MonthAwareCalendarAdapter()

        registry = {
            'div[data-e2e="schedule_container"] input.TUXTextInputCore-input':
                FakeLocator([time_field, date_field]),
            "div.tiktok-timepicker-option-list": FakeLocator([hour_list, minute_list]),
            ".calendar-wrapper": FakeLocator([calendar_adapter]),
        }
        page = FakePage(registry=registry)

        with mock.patch.object(tiktok, "utc_now", return_value=agora_simulado_utc), \
             mock.patch.object(tiktok, "save_debug", return_value=None):
            tiktok.set_schedule_datetime(page, target_dt)  # não deve levantar

        # month_delta = 2 (setembro -> novembro) -- a seta "próximo mês" tem
        # que ter sido clicada exatamente 2 vezes.
        self.assertEqual(2, month_arrow_calls.count("click"))


class CopyrightPolicyNaoInfluenciaEstaEtapaTests(unittest.TestCase):
    def test_1_set_schedule_datetime_nao_recebe_cfg_estruturalmente_isolada_de_politica(self):
        """set_schedule_datetime() nem sequer recebe `cfg`/política de
        copyright como parâmetro -- estruturalmente não há como a resposta
        da pergunta interativa alcançar esta função."""
        params = list(inspect.signature(tiktok.set_schedule_datetime).parameters)
        self.assertEqual(["page", "target_dt"], params)

    def test_2_schedule_one_chama_set_schedule_datetime_antes_de_ler_a_politica(self):
        """No código-fonte de schedule_one(), a chamada a
        set_schedule_datetime() precisa aparecer ANTES de qualquer uso de
        copyright_warning_policy/_copyright_warning_policy_from_cfg --
        confirma estruturalmente (via posição no arquivo-fonte) que a
        política de copyright não pode influenciar o preenchimento de
        data/hora, sem depender de reproduzir o efeito colateral em UI."""
        source, start_line = inspect.getsourcelines(tiktok.schedule_one)
        source_text = "".join(source)
        idx_set_schedule = source_text.find("set_schedule_datetime(page, target_dt)")
        idx_policy = source_text.find("_copyright_warning_policy_from_cfg(cfg)")
        self.assertNotEqual(-1, idx_set_schedule)
        self.assertNotEqual(-1, idx_policy)
        self.assertLess(
            idx_set_schedule, idx_policy,
            "set_schedule_datetime() precisa ser chamada antes de qualquer leitura da política de copyright",
        )

    def test_3_mesmo_fluxo_completo_com_allow_e_block_produz_a_mesma_ordem_de_cliques(self):
        """Roda schedule_one() ponta a ponta duas vezes -- uma com política
        ALLOW, outra com BLOCK -- no MESMO cenário de data/hora, e confirma
        que a ordem de preenchimento (DATA antes de HORA) é idêntica nas
        duas, e que ambas chegam ao clique final. Se a política de copyright
        influenciasse esta etapa de alguma forma inesperada, as ordens
        divergiriam ou uma delas falharia aqui."""
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)

        def run_with_policy(policy_value):
            page, order, final_button = _order_spy_page(
                target_dt, observed_hour_text="18", observed_minute_text="00", observed_date_text="01/10/2026",
            )
            file_input_el = FakeElement(tag="input", attrs={"type": "file"})
            file_input_el.set_input_files = lambda *a, **kw: None
            page._registry['input[type="file"]'] = FakeLocator([file_input_el])
            caption_editor = FakeElement(tag="div", attrs={"contenteditable": "true", "role": "textbox"}, text="")
            page._registry['[contenteditable="true"][role="textbox"]'] = FakeLocator([caption_editor])
            page._registry["body"] = FakeLocator([FakeElement(text="scheduled")])
            _register_checks_passed(page)

            fixed_now = target_dt.astimezone(timezone.utc)
            cfg = {"tiktok_copyright_warning_policy": policy_value} if policy_value else {}
            with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
                 mock.patch.object(tiktok, "save_debug", return_value=None), \
                 mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None), \
                 mock.patch.object(tiktok, "set_caption", return_value=None), \
                 mock.patch.object(tiktok, "click_schedule_toggle", return_value=True), \
                 mock.patch.object(tiktok, "challenge_detected", return_value=False), \
                 mock.patch.object(tiktok.time, "sleep", return_value=None):
                tiktok.schedule_one(page, cfg, Path("video.mp4"), "legenda", target_dt)
            return order, final_button.call_log

        order_allow, clicks_allow = run_with_policy("ALLOW")
        order_block, clicks_block = run_with_policy("BLOCK")

        self.assertEqual(["DATA", "HORA"], order_allow)
        self.assertEqual(["DATA", "HORA"], order_block)
        self.assertEqual(order_allow, order_block)
        self.assertEqual(1, clicks_allow.count("click"))
        self.assertEqual(1, clicks_block.count("click"))


if __name__ == "__main__":
    unittest.main()
