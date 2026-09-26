# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 (CONTINUAÇÃO) — PERGUNTA ÚNICA POR EXECUÇÃO/CONTA
PARA A POLÍTICA DE DIREITOS AUTORAIS (SEM PERSISTÊNCIA EM DISCO) — TIKTOK.

Cobre o que foi adicionado nesta rodada em _sistema/agendar_tiktok.py:

  A. _ask_interactive_copyright_warning_policy() -- pergunta isolada,
     fail-closed, sem estado.
  B. _cfg_for_interactive_run()                  -- cópia rasa em memória,
     nunca muta o cfg original, sempre força SKIP_AND_CONTINUE.
  C. main(input_fn=...) de ponta a ponta          -- não-persistência
     (CONFIG_FILE byte-idêntico antes/depois), SKIP_AND_CONTINUE sempre
     aplicado ao lote, e ausência TOTAL de vazamento de estado entre duas
     chamadas sucessivas de main() para a MESMA conta com respostas
     DIFERENTES (nas duas ordens).

O comportamento de process_prepared_batch() em si (pular vídeo com FAILED/
UNKNOWN real mantendo o lote vivo; erro genérico sempre para o lote,
mesmo com SKIP_AND_CONTINUE) já é coberto, sem nenhuma alteração de
lógica nesta rodada, por TikTokBatchPolicyTests em
test_agendar_tiktok_copyright_policy.py (testes 20-25) -- não duplicado
aqui.
"""
from pathlib import Path
from unittest import mock
import contextlib
import tempfile
import unittest

from _sistema import agendar_tiktok as tiktok
from _sistema.app_paths import account_paths
from tests.fakes_playwright import FakeSyncPlaywrightCM, FakePlaywrightContext, install_fake_playwright


# ============================================================================
# A. _ask_interactive_copyright_warning_policy() -- pergunta isolada
# ============================================================================
class AskInteractiveCopyrightPolicyTests(unittest.TestCase):
    def test_1_sim_e_allow(self):
        self.assertEqual(
            tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
            tiktok._ask_interactive_copyright_warning_policy(lambda prompt: "sim"),
        )

    def test_2_variacoes_reconhecidas_de_sim(self):
        for resposta in ["s", "S", "Sim", "SIM", "  sim  ", "y", "Y", "yes", "YES"]:
            with self.subTest(resposta=resposta):
                self.assertEqual(
                    tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
                    tiktok._ask_interactive_copyright_warning_policy(lambda p, r=resposta: r),
                )

    def test_3_nao_e_block(self):
        self.assertEqual(
            tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK,
            tiktok._ask_interactive_copyright_warning_policy(lambda prompt: "não"),
        )

    def test_4_vazio_e_block_fail_closed(self):
        self.assertEqual(
            tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK,
            tiktok._ask_interactive_copyright_warning_policy(lambda prompt: ""),
        )

    def test_5_lixo_e_block_fail_closed(self):
        for resposta in ["talvez", "1", "xyz", "sim por favor", None]:
            with self.subTest(resposta=resposta):
                self.assertEqual(
                    tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK,
                    tiktok._ask_interactive_copyright_warning_policy(lambda p, r=resposta: r),
                )

    def test_6_excecao_ao_ler_entrada_e_block_fail_closed(self):
        def input_fn(prompt):
            raise EOFError("stdin fechado")

        self.assertEqual(
            tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK,
            tiktok._ask_interactive_copyright_warning_policy(input_fn),
        )

    def test_7_nao_tem_efeito_colateral_nem_memoria_entre_chamadas(self):
        respostas = iter(["sim", "não", "sim"])
        resultados = [
            tiktok._ask_interactive_copyright_warning_policy(lambda p: next(respostas))
            for _ in range(3)
        ]
        self.assertEqual(
            [
                tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
                tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK,
                tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
            ],
            resultados,
        )


# ============================================================================
# B. _cfg_for_interactive_run() -- cópia rasa em memória
# ============================================================================
class CfgForInteractiveRunTests(unittest.TestCase):
    def test_8_forca_skip_and_continue_com_allow(self):
        cfg = {"tiktok_blocked_item_policy": "STOP_BATCH"}
        cfg_for_run = tiktok._cfg_for_interactive_run(cfg, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual("ALLOW", cfg_for_run["tiktok_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", cfg_for_run["tiktok_blocked_item_policy"])

    def test_9_forca_skip_and_continue_com_block(self):
        cfg = {"tiktok_blocked_item_policy": "STOP_BATCH"}
        cfg_for_run = tiktok._cfg_for_interactive_run(cfg, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK)
        self.assertEqual("BLOCK", cfg_for_run["tiktok_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", cfg_for_run["tiktok_blocked_item_policy"])

    def test_10_nao_muta_o_cfg_original(self):
        cfg = {"tiktok_blocked_item_policy": "STOP_BATCH", "nome_conta": "conta X"}
        original = dict(cfg)
        tiktok._cfg_for_interactive_run(cfg, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual(original, cfg, "cfg original não pode ser alterado por esta função")

    def test_11_e_uma_copia_independente_nao_o_mesmo_objeto(self):
        cfg = {"a": 1}
        cfg_for_run = tiktok._cfg_for_interactive_run(cfg, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertIsNot(cfg, cfg_for_run)
        cfg_for_run["a"] = 999
        self.assertEqual(1, cfg["a"])

    def test_12_preserva_demais_chaves_do_cfg_original(self):
        cfg = {"nome_conta": "conta X", "horarios": ["10:00"], "timezone_iana": "America/Sao_Paulo"}
        cfg_for_run = tiktok._cfg_for_interactive_run(cfg, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK)
        self.assertEqual("conta X", cfg_for_run["nome_conta"])
        self.assertEqual(["10:00"], cfg_for_run["horarios"])
        self.assertEqual("America/Sao_Paulo", cfg_for_run["timezone_iana"])


# ============================================================================
# C. main(input_fn=...) de ponta a ponta -- não-persistência + SKIP forçado
#    + zero vazamento entre chamadas sucessivas
# ============================================================================
class MainInteractiveEndToEndTests(unittest.TestCase):
    """Monta uma conta TikTok isolada em disco (tmp dir), com 1 vídeo
    pronto (texto já gerado), e roda main() de ponta a ponta -- process_
    prepared_batch() é mockeado (só captura o cfg recebido) para não
    depender de um Chrome real; o resto do fluxo (load/save de config,
    cálculo de pendências/horários) roda com o código de produção de
    verdade."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.account_dir = Path(self._tmp.name) / "conta_teste"
        self.paths = account_paths(self.account_dir)

        self.paths.data.mkdir(parents=True, exist_ok=True)
        self.paths.videos.mkdir(parents=True, exist_ok=True)
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        self.paths.profile_tiktok.mkdir(parents=True, exist_ok=True)

        video_path = self.paths.videos / "001.mp4"
        video_path.write_bytes(b"conteudo de video de teste")
        fp = tiktok.fingerprint(video_path)

        texts_file = self.paths.data / "textos_postagem.json"
        texts_file.write_text(
            tiktok.json.dumps({fp: {"status": "done", "caption": "legenda de teste", "hashtags": []}}),
            encoding="utf-8",
        )

        self.config_file = self.paths.config
        self.config_file.write_text(
            tiktok.json.dumps({
                "timezone_iana": "America/Sao_Paulo",
                "horarios": ["10:00", "14:00"],
                "tiktok_copyright_warning_policy": "BLOCK",
                "tiktok_blocked_item_policy": "STOP_BATCH",
            }),
            encoding="utf-8",
        )

        self._patches = [
            mock.patch.object(tiktok, "ACCOUNT_PATHS", self.paths),
            mock.patch.object(tiktok, "DATA_DIR", self.paths.data),
            mock.patch.object(tiktok, "VIDEO_DIR", self.paths.videos),
            mock.patch.object(tiktok, "LOG_DIR", self.paths.logs),
            mock.patch.object(tiktok, "PROFILE_DIR", self.paths.profile_tiktok),
            mock.patch.object(tiktok, "STATE_FILE", self.paths.data / "estado_tiktok.json"),
            mock.patch.object(tiktok, "CONFIG_FILE", self.config_file),
            mock.patch.object(tiktok, "TEXTS_FILE", texts_file),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _run_main(self, resposta, captured_calls):
        """Roda main() com sync_playwright/process_prepared_batch fakeados
        e devolve o exit code. `captured_calls` recebe um dict com o cfg
        efetivamente passado para process_prepared_batch."""

        def fake_process_prepared_batch(page, cfg, prepared, state):
            captured_calls.append(dict(cfg))
            return len(prepared), None

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tiktok, "process_prepared_batch", side_effect=fake_process_prepared_batch))
            stack.enter_context(install_fake_playwright(
                FakeSyncPlaywrightCM(context_factory=lambda **kw: FakePlaywrightContext()),
            ))
            return tiktok.main(input_fn=lambda prompt: resposta)

    def test_13_resposta_sim_resulta_em_allow_config_file_intocado(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("sim", calls)
        after = self.config_file.read_bytes()

        self.assertEqual(before, after, "CONFIG_FILE precisa continuar byte-idêntico")
        self.assertEqual(1, len(calls))
        self.assertEqual("ALLOW", calls[0]["tiktok_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", calls[0]["tiktok_blocked_item_policy"])

    def test_14_resposta_nao_resulta_em_block_config_file_intocado(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("não", calls)
        after = self.config_file.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual(1, len(calls))
        self.assertEqual("BLOCK", calls[0]["tiktok_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", calls[0]["tiktok_blocked_item_policy"])

    def test_15_resposta_vazia_resulta_em_block_config_file_intocado(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("", calls)
        after = self.config_file.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual("BLOCK", calls[0]["tiktok_copyright_warning_policy"])

    def test_16_resposta_lixo_resulta_em_block_config_file_intocado(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("blablabla", calls)
        after = self.config_file.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual("BLOCK", calls[0]["tiktok_copyright_warning_policy"])

    def test_17_config_file_em_disco_nunca_ganha_as_chaves_de_politica_interativa(self):
        # A config em disco já tinha tiktok_copyright_warning_policy=BLOCK e
        # tiktok_blocked_item_policy=STOP_BATCH ANTES da execução. Depois de
        # responder "sim" (que produz ALLOW/SKIP_AND_CONTINUE em memória),
        # o arquivo em disco tem que continuar com os valores ORIGINAIS.
        calls = []
        self._run_main("sim", calls)
        reloaded = tiktok.load_json(self.config_file, {})
        self.assertEqual("BLOCK", reloaded["tiktok_copyright_warning_policy"])
        self.assertEqual("STOP_BATCH", reloaded["tiktok_blocked_item_policy"])

    def test_18_chamadas_sucessivas_sim_depois_nao_nao_vazam_estado(self):
        calls = []
        self._run_main("sim", calls)
        self._run_main("não", calls)

        self.assertEqual(2, len(calls))
        self.assertEqual("ALLOW", calls[0]["tiktok_copyright_warning_policy"])
        self.assertEqual("BLOCK", calls[1]["tiktok_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", calls[0]["tiktok_blocked_item_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", calls[1]["tiktok_blocked_item_policy"])

    def test_19_chamadas_sucessivas_nao_depois_sim_nao_vazam_estado_ordem_inversa(self):
        calls = []
        self._run_main("não", calls)
        self._run_main("sim", calls)

        self.assertEqual(2, len(calls))
        self.assertEqual("BLOCK", calls[0]["tiktok_copyright_warning_policy"])
        self.assertEqual("ALLOW", calls[1]["tiktok_copyright_warning_policy"])

    def test_20_config_file_continua_intocado_apos_duas_chamadas_em_qualquer_ordem(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("sim", calls)
        self._run_main("não", calls)
        after = self.config_file.read_bytes()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
