# -*- coding: utf-8 -*-
"""
GATE 19.5 -- CORREÇÃO CRÍTICA (rodada de 21/09/2026) -- seção 0 -- TIKTOK.

Decisão nova e explícita do usuário: quando a política de aviso de direitos
autorais desta execução é ALLOW, schedule_one() não deve esperar nem ler a
verificação do TikTok NENHUM POUCO -- zero chamada a wait_for_tiktok_checks(),
nos DOIS pontos onde ele é chamado (a tentativa original e o retry depois do
modal "Continuar publicando?"). Com BLOCK, o comportamento de espera/leitura
continua idêntico ao de antes desta rodada.

Este arquivo NÃO toca _find_checks_section(), _gather_check_items(),
_read_status_group_state(), aggregate_check_status(),
decide_tiktok_checks_outcome() nem wait_for_tiktok_checks() por dentro --
só prova, via spy, que schedule_one() deixa de CHAMAR essa função quando
ALLOW, e continua chamando normalmente quando BLOCK.

Reaproveita a infraestrutura já existente em test_agendar_tiktok_time_layers
(build_page, _register_checks_passed) em vez de duplicar fixtures de DOM.
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import unittest

from _sistema import agendar_tiktok as tiktok
from tests.fakes_playwright import FakeElement, FakeLocator
from tests.test_agendar_tiktok_time_layers import build_page, _register_checks_passed

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def _make_ready_page(target_dt):
    page, _time_field, _date_field, final_button = build_page(
        target_dt,
        observed_hour_text=f"{target_dt.hour:02d}",
        observed_minute_text=f"{target_dt.minute:02d}",
        observed_date_text=target_dt.strftime("%d/%m/%Y"),
    )
    file_input_el = FakeElement(tag="input", attrs={"type": "file"})
    file_input_el.set_input_files = lambda *a, **kw: None
    page._registry['input[type="file"]'] = FakeLocator([file_input_el])
    caption_editor = FakeElement(tag="div", attrs={"contenteditable": "true", "role": "textbox"}, text="")
    page._registry['[contenteditable="true"][role="textbox"]'] = FakeLocator([caption_editor])
    page._registry["body"] = FakeLocator([FakeElement(text="scheduled")])
    return page, final_button


def _run_schedule_one(cfg, *, register_checks=True, spy_wait_for_checks=False):
    target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
    page, final_button = _make_ready_page(target_dt)
    if register_checks:
        _register_checks_passed(page)

    wait_calls = []
    real_wait = tiktok.wait_for_tiktok_checks

    def spying_wait(*args, **kwargs):
        wait_calls.append((args, kwargs))
        return real_wait(*args, **kwargs)

    fixed_now = target_dt.astimezone(timezone.utc)
    with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
         mock.patch.object(tiktok, "save_debug", return_value=None), \
         mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None), \
         mock.patch.object(tiktok, "set_caption", return_value=None), \
         mock.patch.object(tiktok, "click_schedule_toggle", return_value=True), \
         mock.patch.object(tiktok, "challenge_detected", return_value=False), \
         mock.patch.object(tiktok.time, "sleep", return_value=None):
        if spy_wait_for_checks:
            with mock.patch.object(tiktok, "wait_for_tiktok_checks", side_effect=spying_wait):
                tiktok.schedule_one(page, cfg, Path("video.mp4"), "legenda", target_dt)
        else:
            tiktok.schedule_one(page, cfg, Path("video.mp4"), "legenda", target_dt)

    return final_button.call_log, wait_calls


class TikTokAllowSkipsChecksEntirelyTests(unittest.TestCase):
    def test_1_allow_nunca_chama_wait_for_tiktok_checks(self):
        # Nem sequer registra a seção "Verificações" -- se o código
        # tentasse lê-la mesmo assim, cairia em UNKNOWN/timeout, o que
        # este teste também detectaria (schedule_one nunca chegaria ao
        # clique final a tempo).
        clicks, wait_calls = _run_schedule_one(
            {"tiktok_copyright_warning_policy": "ALLOW"},
            register_checks=False,
            spy_wait_for_checks=True,
        )
        self.assertEqual(0, len(wait_calls), "com ALLOW, zero chamadas a wait_for_tiktok_checks()")
        self.assertEqual(1, clicks.count("click"), "deveria ir direto para o clique final")

    def test_2_block_continua_chamando_wait_for_tiktok_checks_normalmente(self):
        clicks, wait_calls = _run_schedule_one(
            {"tiktok_copyright_warning_policy": "BLOCK"},
            register_checks=True,
            spy_wait_for_checks=True,
        )
        self.assertEqual(1, len(wait_calls), "com BLOCK, a espera/leitura continua acontecendo normalmente")
        self.assertEqual(1, clicks.count("click"))

    def test_3_ausencia_de_policy_na_config_e_block_por_padrao_e_ainda_espera(self):
        # Sem a chave configurada, o default é BLOCK (_copyright_warning_
        # policy_from_cfg) -- comportamento de espera precisa continuar
        # sendo o padrão de fábrica, sem exigir configuração extra.
        clicks, wait_calls = _run_schedule_one({}, register_checks=True, spy_wait_for_checks=True)
        self.assertEqual(1, len(wait_calls))
        self.assertEqual(1, clicks.count("click"))

    def test_4_allow_pula_tambem_a_segunda_chamada_apos_retry_do_modal(self):
        # As DUAS chamadas de wait_for_tiktok_checks() em schedule_one() --
        # a original e a do retry pós-modal "Continuar publicando?" -- têm
        # que ser puladas quando ALLOW, para a política não ficar
        # inconsistente dentro do mesmo vídeo. Simula o modal aparecendo
        # UMA vez (retry único) e confirma: zero chamadas a wait_for_
        # tiktok_checks() em QUALQUER dos dois pontos, e o clique final
        # acontece duas vezes (a tentativa original + o retry).
        target_dt = datetime(2026, 10, 1, 18, 0, tzinfo=SAO_PAULO)
        page, final_button = _make_ready_page(target_dt)
        # Não registra a seção de verificações -- não deveria ser lida.

        modal_calls = []

        def fake_modal(page_arg):
            modal_calls.append("modal")
            # Aparece só na primeira chamada (depois do 1o clique final);
            # não aparece mais depois do retry.
            return len(modal_calls) == 1

        wait_calls = []
        real_wait = tiktok.wait_for_tiktok_checks

        def spying_wait(*args, **kwargs):
            wait_calls.append((args, kwargs))
            return real_wait(*args, **kwargs)

        fixed_now = target_dt.astimezone(timezone.utc)
        with mock.patch.object(tiktok, "utc_now", return_value=fixed_now), \
             mock.patch.object(tiktok, "save_debug", return_value=None), \
             mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None), \
             mock.patch.object(tiktok, "set_caption", return_value=None), \
             mock.patch.object(tiktok, "click_schedule_toggle", return_value=True), \
             mock.patch.object(tiktok, "challenge_detected", return_value=False), \
             mock.patch.object(tiktok, "wait_for_tiktok_checks", side_effect=spying_wait), \
             mock.patch.object(tiktok, "handle_continue_publishing_modal_if_present", side_effect=fake_modal), \
             mock.patch.object(tiktok.time, "sleep", return_value=None):
            tiktok.schedule_one(page, {"tiktok_copyright_warning_policy": "ALLOW"}, Path("video.mp4"), "legenda", target_dt)

        self.assertEqual(0, len(wait_calls), "nenhuma das duas chamadas (original + retry) deveria acontecer")
        self.assertEqual(2, modal_calls.count("modal"))
        self.assertEqual(2, final_button.call_log.count("click"), "clique original + clique do retry")


if __name__ == "__main__":
    unittest.main()
