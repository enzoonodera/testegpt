# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 (CONTINUAÇÃO) — PERGUNTA ÚNICA POR EXECUÇÃO/CONTA
PARA A POLÍTICA DE DIREITOS AUTORAIS (SEM PERSISTÊNCIA EM DISCO) — YOUTUBE.

Cobertura equivalente à do TikTok (ver
test_agendar_tiktok_interactive_copyright_policy.py), adaptada às
diferenças reais de arquitetura do YouTube nesta rodada:

  A. _ask_interactive_copyright_warning_policy() -- pergunta isolada,
     fail-closed, sem estado (mesmo texto/mesma lógica do TikTok, função
     própria neste módulo).
  B. _cfg_for_interactive_run()                  -- cópia rasa em memória,
     nunca muta o cfg original, sempre força YOUTUBE_BLOCKED_ITEM_POLICY_
     SKIP_AND_CONTINUE.
  C. main(input_fn=...) de ponta a ponta          -- não-persistência
     (CONFIG_FILE byte-idêntico antes/depois), SKIP_AND_CONTINUE sempre
     aplicado ao lote, e ausência TOTAL de vazamento de estado entre duas
     chamadas sucessivas de main() para a MESMA conta com respostas
     DIFERENTES (nas duas ordens).
  D. process_prepared_batch() -- ao contrário do TikTok, esta função NÃO
     existia antes desta rodada para o YouTube (o loop de lote estava
     escrito inline dentro de main()); foi extraída nesta rodada
     especificamente para poder ser chamada de forma programática/testável
     com uma config contendo STOP_BATCH, exatamente como já acontecia para
     o TikTok -- ver docstring de agendar_youtube.process_prepared_batch().
     Esta seção prova: (1) um vídeo com problema REAL de verificação
     (FAILED ou UNKNOWN -- não só aviso de direitos autorais) é pulado e o
     lote continua quando a política é SKIP_AND_CONTINUE; (2) o mesmo caso
     para STOP_BATCH; (3) um erro NÃO relacionado a verificação sempre
     para o lote, mesmo com SKIP_AND_CONTINUE configurado.
"""
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import contextlib
import tempfile
import unittest

from _sistema import agendar_youtube as youtube
from _sistema.app_paths import account_paths
from tests.fakes_playwright import FakeSyncPlaywrightCM, FakePlaywrightContext, install_fake_playwright

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


# ============================================================================
# A. _ask_interactive_copyright_warning_policy() -- pergunta isolada
# ============================================================================
class AskInteractiveCopyrightPolicyTests(unittest.TestCase):
    def test_1_sim_e_allow(self):
        self.assertEqual(
            youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW,
            youtube._ask_interactive_copyright_warning_policy(lambda prompt: "sim"),
        )

    def test_2_variacoes_reconhecidas_de_sim(self):
        for resposta in ["s", "S", "Sim", "SIM", "  sim  ", "y", "Y", "yes", "YES"]:
            with self.subTest(resposta=resposta):
                self.assertEqual(
                    youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW,
                    youtube._ask_interactive_copyright_warning_policy(lambda p, r=resposta: r),
                )

    def test_3_nao_e_block(self):
        self.assertEqual(
            youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK,
            youtube._ask_interactive_copyright_warning_policy(lambda prompt: "não"),
        )

    def test_4_vazio_e_block_fail_closed(self):
        self.assertEqual(
            youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK,
            youtube._ask_interactive_copyright_warning_policy(lambda prompt: ""),
        )

    def test_5_lixo_e_block_fail_closed(self):
        for resposta in ["talvez", "1", "xyz", "sim por favor", None]:
            with self.subTest(resposta=resposta):
                self.assertEqual(
                    youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK,
                    youtube._ask_interactive_copyright_warning_policy(lambda p, r=resposta: r),
                )

    def test_6_excecao_ao_ler_entrada_e_block_fail_closed(self):
        def input_fn(prompt):
            raise EOFError("stdin fechado")

        self.assertEqual(
            youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK,
            youtube._ask_interactive_copyright_warning_policy(input_fn),
        )

    def test_7_nao_tem_efeito_colateral_nem_memoria_entre_chamadas(self):
        respostas = iter(["sim", "não", "sim"])
        resultados = [
            youtube._ask_interactive_copyright_warning_policy(lambda p: next(respostas))
            for _ in range(3)
        ]
        self.assertEqual(
            [
                youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW,
                youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK,
                youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW,
            ],
            resultados,
        )


# ============================================================================
# B. _cfg_for_interactive_run() -- cópia rasa em memória
# ============================================================================
class CfgForInteractiveRunTests(unittest.TestCase):
    def test_8_forca_skip_and_continue_com_allow(self):
        cfg = {"youtube_blocked_item_policy": "STOP_BATCH"}
        cfg_for_run = youtube._cfg_for_interactive_run(cfg, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual("ALLOW", cfg_for_run["youtube_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", cfg_for_run["youtube_blocked_item_policy"])

    def test_9_forca_skip_and_continue_com_block(self):
        cfg = {"youtube_blocked_item_policy": "STOP_BATCH"}
        cfg_for_run = youtube._cfg_for_interactive_run(cfg, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK)
        self.assertEqual("BLOCK", cfg_for_run["youtube_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", cfg_for_run["youtube_blocked_item_policy"])

    def test_10_nao_muta_o_cfg_original(self):
        cfg = {"youtube_blocked_item_policy": "STOP_BATCH", "nome_canal": "canal X"}
        original = dict(cfg)
        youtube._cfg_for_interactive_run(cfg, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual(original, cfg, "cfg original não pode ser alterado por esta função")

    def test_11_e_uma_copia_independente_nao_o_mesmo_objeto(self):
        cfg = {"a": 1}
        cfg_for_run = youtube._cfg_for_interactive_run(cfg, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertIsNot(cfg, cfg_for_run)
        cfg_for_run["a"] = 999
        self.assertEqual(1, cfg["a"])

    def test_12_preserva_demais_chaves_do_cfg_original(self):
        cfg = {"nome_canal": "canal X", "horarios": ["10:00"], "timezone_iana": "America/Sao_Paulo"}
        cfg_for_run = youtube._cfg_for_interactive_run(cfg, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK)
        self.assertEqual("canal X", cfg_for_run["nome_canal"])
        self.assertEqual(["10:00"], cfg_for_run["horarios"])
        self.assertEqual("America/Sao_Paulo", cfg_for_run["timezone_iana"])


# ============================================================================
# C. main(input_fn=...) de ponta a ponta -- não-persistência + SKIP forçado
#    + zero vazamento entre chamadas sucessivas
# ============================================================================
class MainInteractiveEndToEndTests(unittest.TestCase):
    """Monta uma conta YouTube isolada em disco (tmp dir); list_items()/
    load_titles()/load_descriptions() são mockeadas para não depender de
    ffprobe nem de arquivos reais de título/descrição (isso já é
    responsabilidade de outros testes, não desta rodada) -- o que importa
    aqui é o fluxo de política em main(): pergunta -> cópia rasa -> cfg
    passado para process_prepared_batch() -> nunca persistido. process_
    prepared_batch() em si é mockeada (só captura o cfg recebido) para não
    depender de Playwright."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.account_dir = Path(self._tmp.name) / "canal_teste"
        self.paths = account_paths(self.account_dir)

        self.paths.data.mkdir(parents=True, exist_ok=True)
        self.paths.videos.mkdir(parents=True, exist_ok=True)
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        self.paths.profile_youtube.mkdir(parents=True, exist_ok=True)
        self.paths.blocked.mkdir(parents=True, exist_ok=True)

        self.config_file = self.paths.config
        self.config_file.write_text(
            youtube.json.dumps({
                "timezone_iana": "America/Sao_Paulo",
                "horarios": ["10:00", "14:00"],
                "youtube_copyright_warning_policy": "BLOCK",
                "youtube_blocked_item_policy": "STOP_BATCH",
            }),
            encoding="utf-8",
        )

        self._patches = [
            mock.patch.object(youtube, "ACCOUNT_PATHS", self.paths),
            mock.patch.object(youtube, "DATA_DIR", self.paths.data),
            mock.patch.object(youtube, "VIDEO_DIR", self.paths.videos),
            mock.patch.object(youtube, "LOG_DIR", self.paths.logs),
            mock.patch.object(youtube, "PROFILE_DIR", self.paths.profile_youtube),
            mock.patch.object(youtube, "STATE_FILE", self.paths.data / "estado_youtube.json"),
            mock.patch.object(youtube, "CONFIG_FILE", self.config_file),
            mock.patch.object(youtube, "AI_TITLES_FILE", self.paths.data / "textos_postagem.json"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

        self._video_path = self.paths.videos / "001.mp4"
        self._video_path.write_bytes(b"conteudo de video de teste")

    def _one_item(self):
        return {
            "path": self._video_path,
            "fp": "fp-001",
            "ai_title": "titulo gerado",
            "ai_description": "descricao gerada",
            "info": {"duration": 12.0},
        }

    def _run_main(self, resposta, captured_calls):
        """Roda main() com list_items/load_titles/load_descriptions/
        process_prepared_batch fakeados e devolve o exit code.
        `captured_calls` recebe o cfg efetivamente passado para
        process_prepared_batch em cada chamada."""

        def fake_process_prepared_batch(cfg, pending, slots, titles, descriptions, state):
            captured_calls.append(dict(cfg))
            return 0

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(youtube, "list_items", return_value=[self._one_item()]))
            stack.enter_context(mock.patch.object(youtube, "load_titles", return_value=["Título padrão"]))
            stack.enter_context(mock.patch.object(youtube, "load_descriptions", return_value=["Descrição padrão"]))
            stack.enter_context(mock.patch.object(youtube, "process_prepared_batch", side_effect=fake_process_prepared_batch))
            return youtube.main(input_fn=lambda prompt: resposta)

    def test_13_resposta_sim_resulta_em_allow_config_file_intocado(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("sim", calls)
        after = self.config_file.read_bytes()

        self.assertEqual(before, after, "CONFIG_FILE precisa continuar byte-idêntico")
        self.assertEqual(1, len(calls))
        self.assertEqual("ALLOW", calls[0]["youtube_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", calls[0]["youtube_blocked_item_policy"])

    def test_14_resposta_nao_resulta_em_block_config_file_intocado(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("não", calls)
        after = self.config_file.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual(1, len(calls))
        self.assertEqual("BLOCK", calls[0]["youtube_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", calls[0]["youtube_blocked_item_policy"])

    def test_15_resposta_vazia_resulta_em_block_config_file_intocado(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("", calls)
        after = self.config_file.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual("BLOCK", calls[0]["youtube_copyright_warning_policy"])

    def test_16_resposta_lixo_resulta_em_block_config_file_intocado(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("blablabla", calls)
        after = self.config_file.read_bytes()

        self.assertEqual(before, after)
        self.assertEqual("BLOCK", calls[0]["youtube_copyright_warning_policy"])

    def test_17_config_file_em_disco_nunca_ganha_as_chaves_de_politica_interativa(self):
        calls = []
        self._run_main("sim", calls)
        reloaded = youtube.load_json(self.config_file, {})
        self.assertEqual("BLOCK", reloaded["youtube_copyright_warning_policy"])
        self.assertEqual("STOP_BATCH", reloaded["youtube_blocked_item_policy"])

    def test_18_chamadas_sucessivas_sim_depois_nao_nao_vazam_estado(self):
        calls = []
        self._run_main("sim", calls)
        self._run_main("não", calls)

        self.assertEqual(2, len(calls))
        self.assertEqual("ALLOW", calls[0]["youtube_copyright_warning_policy"])
        self.assertEqual("BLOCK", calls[1]["youtube_copyright_warning_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", calls[0]["youtube_blocked_item_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", calls[1]["youtube_blocked_item_policy"])

    def test_19_chamadas_sucessivas_nao_depois_sim_nao_vazam_estado_ordem_inversa(self):
        calls = []
        self._run_main("não", calls)
        self._run_main("sim", calls)

        self.assertEqual(2, len(calls))
        self.assertEqual("BLOCK", calls[0]["youtube_copyright_warning_policy"])
        self.assertEqual("ALLOW", calls[1]["youtube_copyright_warning_policy"])

    def test_20_config_file_continua_intocado_apos_duas_chamadas_em_qualquer_ordem(self):
        before = self.config_file.read_bytes()
        calls = []
        self._run_main("sim", calls)
        self._run_main("não", calls)
        after = self.config_file.read_bytes()
        self.assertEqual(before, after)


# ============================================================================
# D. process_prepared_batch() -- extraída nesta rodada; prova que o
#    mecanismo antigo (STOP_BATCH via CONFIG_FILE, chamado programaticamente
#    e sem qualquer relação com a pergunta interativa) continua funcionando,
#    e que FAILED/UNKNOWN reais (não só aviso de direitos autorais) também
#    são pulados quando a política é SKIP_AND_CONTINUE.
# ============================================================================
class ProcessPreparedBatchTests(unittest.TestCase):
    def setUp(self):
        # dir= aponta para dentro do repositório (não /tmp bruto): o rename()
        # usado pela quarentena de itens bloqueados precisa que origem e
        # destino estejam sob uma raiz permitida por este sandbox de testes.
        self._tmp = tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent.parent))
        self.addCleanup(self._tmp.cleanup)
        self.account_dir = Path(self._tmp.name) / "canal_teste_lote"
        self.paths = account_paths(self.account_dir)
        self.paths.data.mkdir(parents=True, exist_ok=True)
        self.paths.videos.mkdir(parents=True, exist_ok=True)
        self.paths.blocked.mkdir(parents=True, exist_ok=True)

        self._patches = [
            mock.patch.object(youtube, "ACCOUNT_PATHS", self.paths),
            mock.patch.object(youtube, "DATA_DIR", self.paths.data),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _items(self, n):
        items = []
        for i in range(1, n + 1):
            p = self.paths.videos / f"{i:03d}.mp4"
            p.write_bytes(b"video de teste")
            items.append({"path": p, "fp": f"fp{i}", "ai_title": f"t{i}", "ai_description": f"d{i}"})
        return items

    def _slots(self, n):
        return [datetime(2026, 9, 20, 10, i, tzinfo=SAO_PAULO) for i in range(n)]

    def _run(self, cfg, pending, schedule_side_effect):
        state = {"scheduled": []}
        titles = ["Título"]
        descriptions = ["Descrição"]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(youtube, "schedule_one", side_effect=schedule_side_effect))
            stack.enter_context(mock.patch.object(youtube, "save_debug", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "save_json", return_value=None))
            stack.enter_context(mock.patch.object(youtube.time, "sleep", return_value=None))
            stack.enter_context(install_fake_playwright(
                FakeSyncPlaywrightCM(context_factory=lambda **kw: FakePlaywrightContext()),
            ))
            stop_code = youtube.process_prepared_batch(cfg, pending, self._slots(len(pending)), titles, descriptions, state)
        return stop_code, state

    def test_21_failed_real_skip_and_continue_pula_e_continua(self):
        pending = self._items(2)

        def side_effect(page, cfg, item, title, description, target_dt):
            if item["path"].name == "001.mp4":
                raise youtube.YouTubeChecksBlockedError("restrito", "failed")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "youtube_blocked_item_policy": "SKIP_AND_CONTINUE"}
        stop_code, state = self._run(cfg, pending, side_effect)
        self.assertEqual(0, stop_code)
        self.assertEqual(["002.mp4"], [x["file"] for x in state["scheduled"]])
        self.assertFalse((self.paths.videos / "001.mp4").exists(), "vídeo bloqueado deve ser movido para quarentena")
        self.assertTrue((self.paths.blocked / "direitos_autorais" / "001.mp4").exists())

    def test_22_unknown_real_skip_and_continue_pula_e_continua(self):
        pending = self._items(2)

        def side_effect(page, cfg, item, title, description, target_dt):
            if item["path"].name == "001.mp4":
                raise youtube.YouTubeChecksBlockedError("não confirmado", "unknown")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "youtube_blocked_item_policy": "SKIP_AND_CONTINUE"}
        stop_code, state = self._run(cfg, pending, side_effect)
        self.assertEqual(0, stop_code)
        self.assertEqual(["002.mp4"], [x["file"] for x in state["scheduled"]])

    def test_23_failed_real_stop_batch_para_o_lote(self):
        pending = self._items(2)

        def side_effect(page, cfg, item, title, description, target_dt):
            if item["path"].name == "001.mp4":
                raise youtube.YouTubeChecksBlockedError("restrito", "failed")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "youtube_blocked_item_policy": "STOP_BATCH"}
        stop_code, state = self._run(cfg, pending, side_effect)
        self.assertEqual(2, stop_code)
        self.assertEqual([], state["scheduled"])
        self.assertTrue((self.paths.videos / "001.mp4").exists(), "STOP_BATCH não move nem apaga o vídeo")

    def test_24_copyright_warning_block_skip_and_continue_tambem_pula(self):
        pending = self._items(2)

        def side_effect(page, cfg, item, title, description, target_dt):
            if item["path"].name == "001.mp4":
                raise youtube.YouTubeChecksBlockedError("aviso", "copyright_warning_block")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "youtube_blocked_item_policy": "SKIP_AND_CONTINUE"}
        stop_code, state = self._run(cfg, pending, side_effect)
        self.assertEqual(0, stop_code)
        self.assertEqual(["002.mp4"], [x["file"] for x in state["scheduled"]])

    def test_25_erro_generico_nao_relacionado_sempre_para_o_lote(self):
        pending = self._items(2)

        def side_effect(page, cfg, item, title, description, target_dt):
            if item["path"].name == "001.mp4":
                raise RuntimeError("DOM não encontrado")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "youtube_blocked_item_policy": "SKIP_AND_CONTINUE"}
        stop_code, state = self._run(cfg, pending, side_effect)
        self.assertEqual(2, stop_code, "erro genérico nunca é pulado, mesmo com SKIP_AND_CONTINUE")
        self.assertEqual([], state["scheduled"])
        self.assertTrue((self.paths.videos / "001.mp4").exists(), "erro genérico não move nem apaga o vídeo")

    def test_26_proximo_video_processado_apos_pulo(self):
        pending = self._items(3)
        processed = []

        def side_effect(page, cfg, item, title, description, target_dt):
            processed.append(item["path"].name)
            if item["path"].name == "002.mp4":
                raise youtube.YouTubeChecksBlockedError("restrito", "failed")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "youtube_blocked_item_policy": "SKIP_AND_CONTINUE"}
        stop_code, state = self._run(cfg, pending, side_effect)
        self.assertEqual(["001.mp4", "002.mp4", "003.mp4"], processed)
        self.assertEqual(0, stop_code)
        self.assertEqual(["001.mp4", "003.mp4"], [x["file"] for x in state["scheduled"]])

    def test_27_chamada_direta_programatica_com_stop_batch_do_config_em_disco_funciona(self):
        # Simula uso NÃO-interativo/programático: cfg vindo direto de
        # CONFIG_FILE (sem passar por main() nem pela pergunta interativa),
        # com STOP_BATCH configurado explicitamente -- precisa continuar
        # funcionando exatamente como antes desta rodada.
        pending = self._items(1)

        def side_effect(page, cfg, item, title, description, target_dt):
            raise youtube.YouTubeChecksBlockedError("restrito", "failed")

        cfg_from_disk = youtube._youtube_blocked_item_policy_from_cfg  # apenas para deixar claro que é a MESMA leitura de cfg usada em produção
        cfg = {"timezone_iana": "America/Sao_Paulo", "youtube_blocked_item_policy": "STOP_BATCH", "youtube_copyright_warning_policy": "BLOCK"}
        stop_code, state = self._run(cfg, pending, side_effect)
        self.assertEqual(2, stop_code)
        self.assertEqual([], state["scheduled"])
        self.assertEqual("STOP_BATCH", cfg_from_disk(cfg))

    def test_28_aviso_de_discard_falho_fica_registrado_no_log_de_bloqueados(self):
        # GATE 19.5 (correção desta rodada -- Bug B): quando o outcome é
        # BLOCKED, a política é SKIP_AND_CONTINUE, e schedule_one() não
        # conseguiu apagar o rascunho (embutindo o aviso no `detalhe` da
        # exceção -- ver test_agendar_youtube_copyright_policy.py,
        # test_12_discard_falha_...), o mesmo texto precisa aparecer no
        # arquivo de log de direitos autorais bloqueados, para dar pra
        # auditar quantos rascunhos órfãos ficaram acumulados.
        pending = self._items(1)
        aviso = (
            "rascunho NÃO removido automaticamente -- pode ter ficado salvo "
            "como privado no canal; revise manualmente no YouTube Studio."
        )

        def side_effect(page, cfg, item, title, description, target_dt):
            raise youtube.YouTubeChecksBlockedError(f"{item['path'].name}: restrito | {aviso}", "failed")

        cfg = {"timezone_iana": "America/Sao_Paulo", "youtube_blocked_item_policy": "SKIP_AND_CONTINUE"}
        self._run(cfg, pending, side_effect)

        log_path = self.paths.data / "direitos_autorais_bloqueados.txt"
        self.assertTrue(log_path.exists())
        conteudo = log_path.read_text(encoding="utf-8")
        self.assertIn(aviso, conteudo)


if __name__ == "__main__":
    unittest.main()
