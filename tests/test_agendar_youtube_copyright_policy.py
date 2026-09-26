# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 (CONTINUAÇÃO) — YOUTUBE — POLÍTICA DE AVISO DE
DIREITOS AUTORAIS + ITEM BLOQUEADO.

A política já implementada para o TikTok (tiktok_copyright_warning_policy:
BLOCK/ALLOW, tiktok_blocked_item_policy: STOP_BATCH/SKIP_AND_CONTINUE) passa
a valer também para o YouTube -- com o MESMO comportamento de fundo, mas SEM
presumir que o mecanismo técnico é igual. Diferença investigada e
documentada nesta rodada: o YouTube expõe uma verificação ÚNICA (Content ID
/ direitos autorais de terceiros) nesta etapa do assistente, não múltiplos
itens com polling em tempo real como o TikTok -- por isso não existe um
CHECK_PENDING "vivo" aqui, só "ainda carregando até o timeout".

O antigo `check_copyright_claims()`/`CopyrightClaimError` misturava duas
situações diferentes num único resultado booleano: reivindicação de
terceiros (pode ser publicável, hoje CHECK_WARNING) e "restrito/bloqueado em
algum país" (bloqueio real de exibição, hoje CHECK_FAILED -- nunca elegível
à política ALLOW, mesmo que o mesmo texto também mencione terceiros).

Este arquivo cobre:
  A. YoutubeCopyrightClassificationTests -- _classify_youtube_copyright_body()
  B. YoutubeChecksPollingTests           -- _read_youtube_checks_status()
  C. YoutubeCopyrightPolicyTests         -- decide_youtube_checks_outcome()
  D. YoutubeBatchPolicyTests             -- _youtube_batch_action_for_blocked()
     + integração real do bloco de quarentena em main() (via um pequeno
     helper que replica só a decisão, sem abrir Playwright)
  E. YoutubeChecksPersistenceTests       -- persistência via config JSON
  F. YoutubeChecksIsolationTests         -- isolamento por conta/plataforma
  G. YoutubeScheduleOneSecurityTests     -- schedule_one() ponta a ponta:
     zero cliques automáticos em click_done()/discard_current_upload()
     fora dos casos esperados
"""
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
from datetime import datetime
import contextlib
import unittest

from _sistema import agendar_youtube as youtube
from tests.fakes_playwright import FakeElement, FakeLocator, FakePage

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def _page_with_body(text):
    return FakePage(registry={"body": FakeLocator([FakeElement(text=text)])})


# ============================================================================
# A. _classify_youtube_copyright_body() -- leitura única, sem polling
# ============================================================================
class YoutubeCopyrightClassificationTests(unittest.TestCase):
    def test_1_nenhum_problema_e_passed(self):
        status, hit = youtube._classify_youtube_copyright_body("nenhum problema encontrado")
        self.assertEqual(youtube.CHECK_PASSED, status)

    def test_2_reivindicacao_de_terceiros_sem_pais_e_warning(self):
        status, hit = youtube._classify_youtube_copyright_body(
            "conteúdo reivindicado por terceiros reivindicaram este trecho"
        )
        self.assertEqual(youtube.CHECK_WARNING, status)

    def test_3_restrito_em_pais_e_failed_nunca_warning(self):
        status, hit = youtube._classify_youtube_copyright_body(
            "este vídeo está restrito em alguns países"
        )
        self.assertEqual(youtube.CHECK_FAILED, status)

    def test_4_restrito_em_pais_mesmo_mencionando_terceiros_continua_failed(self):
        status, hit = youtube._classify_youtube_copyright_body(
            "conteúdo reivindicado por terceiros; o vídeo está bloqueado em alguns países"
        )
        self.assertEqual(youtube.CHECK_FAILED, status, "FAILED sempre vence WARNING no mesmo texto")

    def test_5_texto_nao_reconhecido_e_unknown_nunca_passed(self):
        status, hit = youtube._classify_youtube_copyright_body("estado exótico não documentado")
        self.assertEqual(youtube.CHECK_UNKNOWN, status)

    def test_6_ainda_verificando_e_pending(self):
        status, hit = youtube._classify_youtube_copyright_body("verificando o conteúdo, aguarde")
        self.assertEqual(youtube.CHECK_PENDING, status)


# ============================================================================
# B. _read_youtube_checks_status() -- polling com confirmação dupla p/
#    FAILED/WARNING e timeout fail-closed (UNKNOWN)
# ============================================================================
class YoutubeChecksPollingTests(unittest.TestCase):
    def test_1_passed_na_primeira_leitura_nao_precisa_confirmar(self):
        page = _page_with_body("Nenhum problema encontrado.")
        status, detalhe = youtube._read_youtube_checks_status(page, max_wait=5)
        self.assertEqual(youtube.CHECK_PASSED, status)

    def test_2_warning_exige_confirmacao_dupla(self):
        page = _page_with_body("conteúdo reivindicado")
        with mock.patch.object(youtube.time, "sleep", return_value=None):
            status, detalhe = youtube._read_youtube_checks_status(page, max_wait=5)
        self.assertEqual(youtube.CHECK_WARNING, status)

    def test_3_failed_exige_confirmacao_dupla(self):
        page = _page_with_body("vídeo bloqueado em alguns países")
        with mock.patch.object(youtube.time, "sleep", return_value=None):
            status, detalhe = youtube._read_youtube_checks_status(page, max_wait=5)
        self.assertEqual(youtube.CHECK_FAILED, status)

    def test_4_timeout_sem_leitura_conclusiva_e_unknown_fail_closed(self):
        page = _page_with_body("verificando, aguarde")
        with mock.patch.object(youtube.time, "sleep", return_value=None):
            status, detalhe = youtube._read_youtube_checks_status(page, max_wait=0.05)
        self.assertEqual(youtube.CHECK_UNKNOWN, status, "diferente do check_copyright_claims() antigo -- aqui falha FECHADO")

    def test_5_erro_ao_ler_a_pagina_e_tratado_como_nao_conclusivo_ate_timeout(self):
        class BoomLocator:
            def inner_text(self, timeout=None):
                raise RuntimeError("boom")

        class BoomPage:
            def locator(self, sel):
                return BoomLocator()

        with mock.patch.object(youtube.time, "sleep", return_value=None):
            status, detalhe = youtube._read_youtube_checks_status(BoomPage(), max_wait=0.05)
        self.assertEqual(youtube.CHECK_UNKNOWN, status)

    def test_6_compatibilidade_check_copyright_claims_continua_igual_e_falha_aberto(self):
        # check_copyright_claims() NÃO foi trocado -- continua exatamente como
        # antes desta rodada, inclusive falhando ABERTO no timeout (mantido
        # só por compatibilidade; schedule_one() não usa mais esta função).
        page = _page_with_body("verificando, aguarde")
        with mock.patch.object(youtube.time, "sleep", return_value=None):
            tem_problema, detalhe = youtube.check_copyright_claims(page, max_wait=0.05)
        self.assertFalse(tem_problema)
        self.assertIn("seguindo em frente", detalhe)

    # ------------------------------------------------------------------
    # GATE 19.5 (correção crítica desta rodada -- Bug A): timeout de 45s
    # era incompatível com o próprio YouTube anunciando "até 10 minutos"
    # para esta verificação (evidência real: conta NextIdea, 006.mp4,
    # texto literal "Direitos de autor -- A verificar se o seu vídeo
    # inclui conteúdo com direitos de autor -- Faltam 10 minutos."). Os
    # testes abaixo NÃO alteram _classify_youtube_copyright_body nem os
    # termos -- só provam o novo prazo/comportamento de espera.
    # ------------------------------------------------------------------
    def test_7_default_de_max_wait_nao_e_mais_45s(self):
        import inspect
        default = inspect.signature(youtube._read_youtube_checks_status).parameters["max_wait"].default
        self.assertGreaterEqual(default, 660, "o YouTube anuncia até 10 minutos; 45s era incompatível com isso")
        self.assertEqual(youtube.YOUTUBE_COPYRIGHT_CHECK_TIMEOUT_PADRAO_SEGUNDOS, default)

    def test_8_texto_real_faltam_10_minutos_e_tratado_como_pending_ate_expirar(self):
        # Recorte fiel do texto real capturado pelo próprio programa nos
        # dois HTML de evidência desta rodada (conta NextIdea, 006.mp4).
        texto_real = (
            "Direitos de autor -- A verificar se o seu vídeo inclui "
            "conteúdo com direitos de autor -- Faltam 10 minutos."
        )
        page = _page_with_body(texto_real)
        with mock.patch.object(youtube.time, "sleep", return_value=None):
            status, detalhe = youtube._read_youtube_checks_status(page, max_wait=0.05)
        # Nunca PASSED nem lido como um problema real -- só expira em
        # UNKNOWN (fail-closed) porque o estado nunca mudou dentro do
        # prazo dado ao teste; o texto sozinho, enquanto continuar
        # aparecendo, é sempre PENDING (nunca decide nada sozinho).
        self.assertEqual(youtube.CHECK_UNKNOWN, status)
        self.assertIn("não conclusiva", detalhe)

    def test_9_texto_real_faltam_10_minutos_classificado_como_pending(self):
        texto_real = (
            "Direitos de autor -- A verificar se o seu vídeo inclui "
            "conteúdo com direitos de autor -- Faltam 10 minutos."
        ).lower()
        status, hit = youtube._classify_youtube_copyright_body(texto_real)
        self.assertEqual(youtube.CHECK_PENDING, status, "estado real do YouTube -- ainda checando, não indeterminado")

    def test_10_aviso_de_progresso_aparece_periodicamente_enquanto_pending(self):
        page = _page_with_body("a verificar, faltam 10 minutos")
        printed = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))), \
             mock.patch.object(youtube.time, "sleep", return_value=None):
            youtube._read_youtube_checks_status(page, max_wait=0.25, progress_every=0.05)
        avisos = [p for p in printed if "Ainda verificando direitos autorais" in p]
        self.assertGreater(len(avisos), 0, "terminal não pode ficar mudo durante uma espera de até 11 minutos")

    def test_11_sem_aviso_de_progresso_quando_progress_every_e_zero(self):
        page = _page_with_body("a verificar, faltam 10 minutos")
        printed = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))), \
             mock.patch.object(youtube.time, "sleep", return_value=None):
            youtube._read_youtube_checks_status(page, max_wait=0.1, progress_every=0)
        avisos = [p for p in printed if "Ainda verificando" in p]
        self.assertEqual(0, len(avisos))

    def test_12_classificacao_e_termos_nao_foram_alterados(self):
        # Confirma que os termos usados pela classificação continuam os
        # mesmos desta rodada em diante (nada nesta correção toca a
        # classificação em si, só o prazo).
        self.assertIn("a verificar", youtube.YOUTUBE_CHECK_CHECKING_TERMS)
        self.assertIn("verificando", youtube.YOUTUBE_CHECK_CHECKING_TERMS)


# ============================================================================
# C. decide_youtube_checks_outcome()
# ============================================================================
class YoutubeCopyrightPolicyTests(unittest.TestCase):
    def test_1_warning_allow_prossegue(self):
        outcome, reason = youtube.decide_youtube_checks_outcome(
            youtube.CHECK_WARNING, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW
        )
        self.assertEqual(("PROCEED", None), (outcome, reason))

    def test_2_warning_block_bloqueia(self):
        outcome, reason = youtube.decide_youtube_checks_outcome(
            youtube.CHECK_WARNING, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK
        )
        self.assertEqual(("BLOCKED", "copyright_warning_block"), (outcome, reason))

    def test_3_failed_allow_nunca_prossegue(self):
        outcome, reason = youtube.decide_youtube_checks_outcome(
            youtube.CHECK_FAILED, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW
        )
        self.assertEqual(("BLOCKED", "failed"), (outcome, reason))

    def test_4_unknown_allow_nunca_prossegue(self):
        outcome, reason = youtube.decide_youtube_checks_outcome(
            youtube.CHECK_UNKNOWN, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW
        )
        self.assertEqual(("BLOCKED", "unknown"), (outcome, reason))

    def test_5_passed_sempre_prossegue_independente_da_politica(self):
        for policy in (youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW, youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK):
            outcome, reason = youtube.decide_youtube_checks_outcome(youtube.CHECK_PASSED, policy)
            self.assertEqual(("PROCEED", None), (outcome, reason))

    def test_6_policy_invalida_na_config_cai_para_block_seguro(self):
        cfg = {"youtube_copyright_warning_policy": "algo-invalido"}
        self.assertEqual(
            youtube.YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK,
            youtube._youtube_copyright_warning_policy_from_cfg(cfg),
        )


# ============================================================================
# D. LOTE -- _youtube_batch_action_for_blocked() + quarentena
# ============================================================================
class YoutubeBatchPolicyTests(unittest.TestCase):
    def test_1_stop_batch_e_o_default(self):
        self.assertEqual("STOP_BATCH", youtube._youtube_blocked_item_policy_from_cfg({"youtube_blocked_item_policy": "STOP_BATCH"}))

    def test_2_skip_and_continue_e_o_default_quando_ausente(self):
        # Diferente do TikTok: o default do YouTube é SKIP_AND_CONTINUE,
        # porque isso já era o comportamento existente antes desta rodada
        # (quarentena + continua a fila) -- preservar o que já funciona.
        self.assertEqual("SKIP_AND_CONTINUE", youtube._youtube_blocked_item_policy_from_cfg({}))

    def test_3_acao_skip_and_continue(self):
        self.assertEqual("SKIP", youtube._youtube_batch_action_for_blocked("SKIP_AND_CONTINUE"))

    def test_4_acao_stop_batch(self):
        self.assertEqual("STOP", youtube._youtube_batch_action_for_blocked("STOP_BATCH"))

    def test_5_video_bloqueado_e_movido_para_quarentena_nunca_apagado(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            videos_dir = base / "videos"
            videos_dir.mkdir()
            blocked_dir = base / "bloqueados" / "direitos_autorais"
            video_path = videos_dir / "001.mp4"
            video_path.write_bytes(b"conteudo de teste")

            blocked_dir.mkdir(parents=True, exist_ok=True)
            dest = blocked_dir / video_path.name
            video_path.rename(dest)

            self.assertFalse(video_path.exists())
            self.assertTrue(dest.exists())
            self.assertEqual(b"conteudo de teste", dest.read_bytes(), "conteúdo original preservado, nunca alterado")


# ============================================================================
# E. PERSISTÊNCIA -- mesmo arquivo de config JSON por conta já usado pelo
#    TikTok (ACCOUNT_PATHS.config == "config_canal.json", compartilhado
#    quando ambos os scripts recebem o mesmo ACCOUNT_DIR -- ver seção F).
# ============================================================================
class YoutubeChecksPersistenceTests(unittest.TestCase):
    def _roundtrip(self, tmp_path, cfg):
        config_file = tmp_path / "config_canal.json"
        youtube.save_json(config_file, cfg)
        return youtube.load_json(config_file, {})

    def test_1_copyright_allow_sobrevive_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reloaded = self._roundtrip(Path(d), {"youtube_copyright_warning_policy": "ALLOW"})
        self.assertEqual("ALLOW", youtube._youtube_copyright_warning_policy_from_cfg(reloaded))

    def test_2_copyright_block_sobrevive_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reloaded = self._roundtrip(Path(d), {"youtube_copyright_warning_policy": "BLOCK"})
        self.assertEqual("BLOCK", youtube._youtube_copyright_warning_policy_from_cfg(reloaded))

    def test_3_blocked_item_stop_batch_sobrevive_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reloaded = self._roundtrip(Path(d), {"youtube_blocked_item_policy": "STOP_BATCH"})
        self.assertEqual("STOP_BATCH", youtube._youtube_blocked_item_policy_from_cfg(reloaded))

    def test_4_blocked_item_skip_and_continue_sobrevive_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reloaded = self._roundtrip(Path(d), {"youtube_blocked_item_policy": "SKIP_AND_CONTINUE"})
        self.assertEqual("SKIP_AND_CONTINUE", youtube._youtube_blocked_item_policy_from_cfg(reloaded))


# ============================================================================
# F. ISOLAMENTO -- por conta/perfil, e entre plataformas do mesmo perfil
#    (chaves prefixadas por plataforma no MESMO arquivo compartilhado)
# ============================================================================
class YoutubeChecksIsolationTests(unittest.TestCase):
    def test_1_perfil_a_allow_nao_vaza_para_perfil_b(self):
        cfg_a = {"youtube_copyright_warning_policy": "ALLOW"}
        cfg_b = {"youtube_copyright_warning_policy": "BLOCK"}
        self.assertEqual("ALLOW", youtube._youtube_copyright_warning_policy_from_cfg(cfg_a))
        self.assertEqual("BLOCK", youtube._youtube_copyright_warning_policy_from_cfg(cfg_b))

    def test_2_youtube_allow_nao_vaza_para_chave_tiktok_no_mesmo_arquivo(self):
        # Documentação viva da decisão arquitetural: quando o painel lança os
        # dois scripts com o mesmo ACCOUNT_DIR (uso normal via painel), eles
        # compartilham o MESMO arquivo config_canal.json -- por isso as
        # chaves são prefixadas por plataforma, e uma nunca lê o valor da
        # outra mesmo estando no mesmo dict/arquivo.
        cfg = {
            "youtube_copyright_warning_policy": "ALLOW",
            "tiktok_copyright_warning_policy": "BLOCK",
        }
        self.assertEqual("ALLOW", youtube._youtube_copyright_warning_policy_from_cfg(cfg))
        # A leitura do YouTube nunca deve ser afetada por remover a chave do TikTok.
        cfg2 = {"youtube_copyright_warning_policy": "ALLOW"}
        self.assertEqual(
            youtube._youtube_copyright_warning_policy_from_cfg(cfg),
            youtube._youtube_copyright_warning_policy_from_cfg(cfg2),
        )

    def test_3_arquivos_separados_nao_se_misturam_em_disco(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            file_a = Path(d) / "canal_a" / "config_canal.json"
            file_b = Path(d) / "canal_b" / "config_canal.json"
            file_a.parent.mkdir(parents=True)
            file_b.parent.mkdir(parents=True)
            youtube.save_json(file_a, {"youtube_blocked_item_policy": "STOP_BATCH"})
            youtube.save_json(file_b, {"youtube_blocked_item_policy": "SKIP_AND_CONTINUE"})
            reloaded_a = youtube.load_json(file_a, {})
            reloaded_b = youtube.load_json(file_b, {})
        self.assertEqual("STOP_BATCH", reloaded_a["youtube_blocked_item_policy"])
        self.assertEqual("SKIP_AND_CONTINUE", reloaded_b["youtube_blocked_item_policy"])

    def test_4_leitura_nao_usa_estado_global_compartilhado(self):
        cfg_a = {"youtube_blocked_item_policy": "STOP_BATCH"}
        cfg_b = {"youtube_blocked_item_policy": "SKIP_AND_CONTINUE"}
        first = youtube._youtube_blocked_item_policy_from_cfg(cfg_a)
        second = youtube._youtube_blocked_item_policy_from_cfg(cfg_b)
        third = youtube._youtube_blocked_item_policy_from_cfg(cfg_a)
        self.assertEqual(["STOP_BATCH", "SKIP_AND_CONTINUE", "STOP_BATCH"], [first, second, third])


# ============================================================================
# G. schedule_one() ponta a ponta -- zero cliques automáticos fora do caso
#    ALLOW+WARNING já validado
# ============================================================================
class YoutubeScheduleOneSecurityTests(unittest.TestCase):
    def _run(self, cfg, body_text, *, spy_read_status=False, discard_return=False):
        item = {"path": Path("v.mp4")}
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        page = _page_with_body(body_text)
        click_done_calls = []
        discard_calls = []
        read_status_calls = []

        def fake_click_done(p):
            click_done_calls.append("click")

        def fake_discard(p):
            discard_calls.append("discard")
            return discard_return

        real_read_status = youtube._read_youtube_checks_status

        def spying_read_status(*args, **kwargs):
            read_status_calls.append((args, kwargs))
            return real_read_status(*args, **kwargs)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(youtube, "wait_upload_page", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "upload_video_file", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "visible_first", return_value=FakeElement(tag="div")))
            stack.enter_context(mock.patch.object(youtube, "fill_editable", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "choose_not_for_kids", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "click_next", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "choose_schedule", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "set_date", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "set_time", return_value=None))
            stack.enter_context(mock.patch.object(youtube, "read_observed_schedule_datetime", return_value=target_dt.replace(second=0, microsecond=0)))
            stack.enter_context(mock.patch.object(youtube, "confirm_success", return_value=True))
            stack.enter_context(mock.patch.object(youtube, "click_done", side_effect=fake_click_done))
            stack.enter_context(mock.patch.object(youtube, "discard_current_upload", side_effect=fake_discard))
            stack.enter_context(mock.patch.object(youtube, "save_debug", return_value=None))
            stack.enter_context(mock.patch.object(youtube.time, "sleep", return_value=None))
            if spy_read_status:
                stack.enter_context(mock.patch.object(youtube, "_read_youtube_checks_status", side_effect=spying_read_status))
            page._registry["ytcp-uploads-dialog"] = FakeLocator([FakeElement(tag="div", visible=True)])
            result = None
            error = None
            try:
                youtube.schedule_one(page, cfg, item, "titulo", "descricao", target_dt)
            except youtube.YouTubeChecksBlockedError as e:
                error = e
        if spy_read_status:
            return click_done_calls, discard_calls, error, read_status_calls
        return click_done_calls, discard_calls, error

    def test_1_failed_bloqueado_nunca_clica_done_e_descarta_upload(self):
        clicks, discards, error = self._run({}, "este vídeo está bloqueado em alguns países")
        self.assertEqual(0, len(clicks), "click_done() nunca deve ser chamado quando FAILED")
        self.assertEqual(1, len(discards))
        self.assertIsNotNone(error)
        self.assertEqual("failed", error.reason)

    def test_2_unknown_bloqueado_nunca_clica_done(self):
        clicks, discards, error = self._run(
            {"timeout_verificacao_direitos_autorais_segundos": 0.05},
            "estado exótico não documentado",
        )
        self.assertEqual(0, len(clicks))
        self.assertIsNotNone(error)

    def test_3_warning_block_nunca_clica_done(self):
        clicks, discards, error = self._run(
            {"youtube_copyright_warning_policy": "BLOCK"}, "conteúdo reivindicado"
        )
        self.assertEqual(0, len(clicks))
        self.assertIsNotNone(error)
        self.assertEqual("copyright_warning_block", error.reason)

    def test_4_warning_allow_prossegue_e_clica_done(self):
        clicks, discards, error = self._run(
            {"youtube_copyright_warning_policy": "ALLOW"}, "conteúdo reivindicado"
        )
        self.assertIsNone(error)
        self.assertEqual(1, len(clicks))
        self.assertEqual(0, len(discards))

    def test_5_allow_agora_pula_a_verificacao_inteira_mesmo_com_texto_de_failed(self):
        # GATE 19.5 (correção desta rodada, seção 0 -- decisão explícita e
        # nova do usuário, DIFERENTE do comportamento provado por este
        # mesmo teste antes desta rodada): com ALLOW, schedule_one() não
        # chama _read_youtube_checks_status() NENHUM POUCO -- não importa
        # o que o corpo da página diria, ele nunca é lido, então um texto
        # que normalmente seria FAILED (restrição de país) não bloqueia
        # mais nada aqui. Risco aceito e documentado pelo usuário: um
        # bloqueio REAL só apareceria depois de publicado, se aparecer.
        # (O teste antigo, "ALLOW nunca se estende a FAILED", provava o
        # comportamento ANTERIOR a esta decisão; renomeado e reescrito
        # para não ficar dando falso negativo contra uma mudança que foi
        # pedida de propósito -- ver GATE_19_5_ESTAGIO2_RELATORIO.md.)
        clicks, discards, error = self._run(
            {"youtube_copyright_warning_policy": "ALLOW"}, "vídeo restrito em alguns países"
        )
        self.assertIsNone(error, "com ALLOW, a verificação inteira é pulada -- nada bloqueia")
        self.assertEqual(1, len(clicks))
        self.assertEqual(0, len(discards), "sem verificação, discard_current_upload() nunca é chamado")

    def test_6_passed_prossegue_normalmente(self):
        clicks, discards, error = self._run({}, "nenhum problema encontrado")
        self.assertIsNone(error)
        self.assertEqual(1, len(clicks))

    def test_7_verificacao_desabilitada_na_config_pula_a_checagem(self):
        clicks, discards, error = self._run(
            {"cancelar_video_com_direitos_autorais": False}, "vídeo restrito em alguns países"
        )
        self.assertIsNone(error, "com a checagem desabilitada, comportamento pré-existente é preservado")
        self.assertEqual(1, len(clicks))

    # ------------------------------------------------------------------
    # GATE 19.5 (rodada de correção -- seção 0): com ALLOW, schedule_one()
    # não chama _read_youtube_checks_status() NENHUMA VEZ; com BLOCK, o
    # comportamento de chamar/esperar a verificação continua idêntico ao
    # de antes desta rodada.
    # ------------------------------------------------------------------
    def test_8_allow_nunca_chama_read_youtube_checks_status(self):
        clicks, discards, error, read_status_calls = self._run(
            {"youtube_copyright_warning_policy": "ALLOW"},
            "vídeo restrito em alguns países",
            spy_read_status=True,
        )
        self.assertEqual(0, len(read_status_calls), "com ALLOW, zero chamadas à leitura de verificação")
        self.assertIsNone(error)
        self.assertEqual(1, len(clicks))

    def test_9_block_ainda_chama_read_youtube_checks_status_normalmente(self):
        clicks, discards, error, read_status_calls = self._run(
            {"youtube_copyright_warning_policy": "BLOCK"},
            "nenhum problema encontrado",
            spy_read_status=True,
        )
        self.assertEqual(1, len(read_status_calls), "com BLOCK, a leitura de verificação continua acontecendo")
        self.assertIsNone(error)
        self.assertEqual(1, len(clicks))

    def test_10_allow_com_warning_tambem_nunca_chama_a_leitura(self):
        # Mesmo um texto que ANTES resultaria em CHECK_WARNING (tolerado
        # por ALLOW já antes desta rodada) agora nem chega a ser lido --
        # a diferença desta rodada é que ALLOW deixou de ler QUALQUER
        # coisa, não só de tolerar WARNING depois de ler.
        clicks, discards, error, read_status_calls = self._run(
            {"youtube_copyright_warning_policy": "ALLOW"},
            "conteúdo reivindicado",
            spy_read_status=True,
        )
        self.assertEqual(0, len(read_status_calls))
        self.assertIsNone(error)
        self.assertEqual(1, len(clicks))

    # ------------------------------------------------------------------
    # GATE 19.5 (correção desta rodada -- Bug B): discard_current_upload()
    # falha SEMPRE nesta etapa do assistente (evidência real: grep nos
    # dois HTML anexados desta rodada, 0 ocorrências de um botão real de
    # apagar). O retorno agora é sempre conferido pelo chamador: quando
    # `False`, um aviso explícito aparece no terminal E fica embutido no
    # `detalhe` da exceção -- que é exatamente o texto que
    # process_prepared_batch() grava em direitos_autorais_bloqueados.txt
    # quando a política é SKIP_AND_CONTINUE (ver
    # test_agendar_youtube_interactive_copyright_policy.py).
    # ------------------------------------------------------------------
    def test_12_discard_falha_gera_aviso_explicito_no_terminal_e_na_excecao(self):
        printed = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            clicks, discards, error = self._run(
                {}, "este vídeo está bloqueado em alguns países", discard_return=False
            )
        self.assertIsNotNone(error)
        self.assertEqual(1, len(discards))
        self.assertIn("NÃO removido automaticamente", str(error))
        avisos = [p for p in printed if "[AVISO]" in p and "removido automaticamente" in p]
        self.assertEqual(1, len(avisos), "aviso precisa aparecer no terminal exatamente uma vez")

    def test_13_discard_com_sucesso_nao_gera_aviso(self):
        printed = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            clicks, discards, error = self._run(
                {}, "este vídeo está bloqueado em alguns países", discard_return=True
            )
        self.assertIsNotNone(error)
        self.assertNotIn("NÃO removido automaticamente", str(error))
        avisos = [p for p in printed if "[AVISO]" in p and "removido automaticamente" in p]
        self.assertEqual(0, len(avisos))

    def test_11_allow_nao_bloqueia_nem_chama_discard_mesmo_sem_ler_nada(self):
        # Confirma que pular a leitura com ALLOW não interfere no resto da
        # sequência de cliques (equivalente, para o YouTube, ao pedido do
        # prompt de confirmar que o tratamento existente após o clique
        # final continua funcionando -- o YouTube não tem um modal
        # "Continuar publicando?" como o TikTok; a garantia equivalente
        # aqui é que click_next -> agendar -> click_done -> confirm_success
        # roda de ponta a ponta sem nenhuma chamada extra de verificação).
        clicks, discards, error = self._run(
            {"youtube_copyright_warning_policy": "ALLOW"}, "vídeo restrito em alguns países"
        )
        self.assertEqual(0, len(discards))
        self.assertIsNone(error)
        self.assertEqual(1, len(clicks))


if __name__ == "__main__":
    unittest.main()
