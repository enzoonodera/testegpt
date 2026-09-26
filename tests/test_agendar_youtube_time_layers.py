# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — CORREÇÃO CIRÚRGICA DO CANDIDATO C (YouTube)

Histórico deste arquivo:
  ANTES (Parte 2): confirm_success() era provado como aceitando o status
  "Programado" sem checar horário nenhum -- não havia qualquer read-back
  entre set_time() e o clique final (click_done()).
  DEPOIS (esta correção): schedule_one() agora chama
  read_observed_schedule_datetime() logo após set_date()/set_time() e ANTES
  de click_done(). Se a data/hora relida da UI (campos relocalizados, não
  reaproveitados) não bater com o calculado, levanta RuntimeError e
  click_done() NUNCA é chamado.

confirm_success() continua existindo e sendo testado separadamente (ainda só
confirma STATUS, não horário -- isso é esperado, porque agora a checagem de
horário acontece ANTES, como um gate que impede chegar lá com um horário
errado).
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import unittest

from _sistema import agendar_youtube as youtube
from tests.fakes_playwright import FakeElement, FakeLocator, FakePage


SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def build_read_back_page(observed_date_text, observed_time_text):
    """Registry mínimo para exercitar read_observed_schedule_datetime()."""
    date_input = FakeElement(tag="input", attrs={"value": observed_date_text}, visible=True)
    time_field = FakeElement(tag="input", attrs={"aria-label": "hora", "value": observed_time_text}, visible=True)
    trigger = FakeElement(tag="ytcp-text-dropdown-trigger", visible=True)

    registry = {
        "#datepicker-trigger": FakeLocator([trigger]),
        "ytcp-date-picker input": FakeLocator([date_input]),
        "ytcp-visibility-scheduler input": FakeLocator([time_field]),
    }
    return FakePage(registry=registry), date_input, time_field


class YoutubeReadBackCorrectionTests(unittest.TestCase):
    def test_1_esperado_igual_observado_nao_levanta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page("01/10/2026", "18:00")
        observed = youtube.read_observed_schedule_datetime(page, target_dt)
        self.assertEqual(target_dt.replace(second=0, microsecond=0), observed)

    def test_2_hora_divergente_e_detectavel(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page("01/10/2026", "18:30")
        observed = youtube.read_observed_schedule_datetime(page, target_dt)
        self.assertNotEqual(target_dt.replace(second=0, microsecond=0), observed)

    def test_3_data_divergente_e_detectavel_mesmo_com_hora_igual(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page("02/10/2026", "18:00")
        observed = youtube.read_observed_schedule_datetime(page, target_dt)
        self.assertNotEqual(target_dt.replace(second=0, microsecond=0), observed)

    def test_4_campo_de_horario_vazio_aborta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, time_field = build_read_back_page("01/10/2026", "")
        with self.assertRaises(RuntimeError) as ctx:
            youtube.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("horário", str(ctx.exception))

    def test_5_campo_ilegivel_aborta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page("01/10/2026", "seis da tarde")
        with self.assertRaises(RuntimeError) as ctx:
            youtube.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("não pôde ser interpretad", str(ctx.exception))

    def test_6_elemento_recriado_e_relocalizado_via_locator_de_novo(self):
        """A cada chamada, page.locator(...) é invocado de novo -- não há
        cache de referência entre a escrita (set_time) e a leitura."""
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, _, _ = build_read_back_page("01/10/2026", "18:00")
        original_locator = page.locator
        calls = []

        def counting_locator(selector):
            calls.append(selector)
            return original_locator(selector)

        page.locator = counting_locator
        youtube.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("ytcp-visibility-scheduler input", calls)
        self.assertIn("#datepicker-trigger", calls)

    def test_7_leitura_lanca_excecao_aborta(self):
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, date_input, _ = build_read_back_page("01/10/2026", "18:00")

        def boom():
            raise RuntimeError("elemento desanexado do DOM")
        date_input.input_value = boom
        date_input.attrs.pop("value", None)

        with self.assertRaises(RuntimeError) as ctx:
            youtube.read_observed_schedule_datetime(page, target_dt)
        self.assertIn("data", str(ctx.exception).lower())

    def test_9_e_10_nunca_chama_click_done_quando_mismatch_chama_quando_match(self):
        """Teste mais importante: instrumenta o botão final (#done-button) e
        prova, via schedule_one() real (com os passos anteriores -- login,
        upload, título/descrição, direitos autorais -- mockados/desligados
        por configuração), que:
          - horário batendo -> click_done() clica o botão (click count = 1);
          - horário divergente -> RuntimeError é levantado e o botão final
            NUNCA é clicado (click count = 0).
        """
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        cfg = {"nao_e_para_criancas": False, "cancelar_video_com_direitos_autorais": False}

        def make_page(observed_time_text):
            done_calls = []
            done_btn = FakeElement(tag="button", visible=True, call_log=done_calls)
            title_box = FakeElement(tag="div", visible=True)
            desc_box = FakeElement(tag="div", visible=True)
            schedule_radio = FakeElement(visible=True)
            next_btn = FakeElement(visible=True)
            body = FakeElement(text="vídeo programado")

            date_input = FakeElement(tag="input", attrs={"value": "01/10/2026"}, visible=True)
            time_field = FakeElement(tag="input", attrs={"aria-label": "hora", "value": observed_time_text}, visible=True)
            trigger = FakeElement(tag="ytcp-text-dropdown-trigger", visible=True)

            registry = {
                "#title-textarea #textbox": FakeLocator([title_box]),
                "#description-textarea #textbox": FakeLocator([desc_box]),
                "#schedule-radio-button": FakeLocator([schedule_radio]),
                "#next-button": FakeLocator([next_btn]),
                "#done-button": FakeLocator([done_btn]),
                "#datepicker-trigger": FakeLocator([trigger]),
                "ytcp-date-picker input": FakeLocator([date_input]),
                "ytcp-visibility-scheduler input": FakeLocator([time_field]),
                "body": FakeLocator([body]),
            }
            return FakePage(registry=registry), done_calls

        item = {"path": Path("video.mp4")}

        # MATCH: chega ao click_done().
        page_match, done_calls_match = make_page("18:00")
        with mock.patch.object(youtube, "wait_upload_page", return_value=None), \
             mock.patch.object(youtube, "upload_video_file", return_value=None), \
             mock.patch.object(youtube, "set_date", return_value=None), \
             mock.patch.object(youtube, "set_time", return_value=None), \
             mock.patch.object(youtube, "save_debug", return_value=None), \
             mock.patch.object(youtube.time, "sleep", return_value=None):
            youtube.schedule_one(page_match, cfg, item, "Meu Título", "Minha descrição", target_dt)
        self.assertEqual(1, done_calls_match.count("click"), "horário batendo deveria chegar ao clique final (click_done)")

        # MISMATCH: RuntimeError é levantado e click_done NUNCA é chamado.
        page_mismatch, done_calls_mismatch = make_page("19:30")
        with mock.patch.object(youtube, "wait_upload_page", return_value=None), \
             mock.patch.object(youtube, "upload_video_file", return_value=None), \
             mock.patch.object(youtube, "set_date", return_value=None), \
             mock.patch.object(youtube, "set_time", return_value=None), \
             mock.patch.object(youtube, "save_debug", return_value=None), \
             mock.patch.object(youtube.time, "sleep", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                youtube.schedule_one(page_mismatch, cfg, item, "Meu Título", "Minha descrição", target_dt)
        self.assertIn("Abortado ANTES do clique final", str(ctx.exception))
        self.assertEqual(0, done_calls_mismatch.count("click"), "click_done NUNCA deveria ser chamado após um mismatch")


class YoutubeConfirmSuccessTests(unittest.TestCase):
    def test_confirm_success_ainda_so_confirma_status_nao_horario(self):
        """Achado da Parte 2, ainda válido e intencional: confirm_success()
        continua só confirmando STATUS ('Programado'), não horário -- porque
        agora a checagem de horário acontece ANTES dele, em
        read_observed_schedule_datetime(). Este teste documenta essa divisão
        de responsabilidades, não é mais um achado de bug isolado."""
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        expected_title = "Meu Video Teste"
        row = FakeElement(text=f"{expected_title}  Programado  23:59", visible=True)
        body = FakeElement(text="", visible=True)
        registry = {"body": FakeLocator([body]), "ytcp-video-row": FakeLocator([row])}
        page = FakePage(registry=registry, url="https://studio.youtube.com/channel/x/videos/upload")

        result = youtube.confirm_success(page, expected_title, target_dt)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
