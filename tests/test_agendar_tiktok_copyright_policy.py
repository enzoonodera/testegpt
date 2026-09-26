# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — TIKTOK — VERIFICAÇÕES POR TIPO + POLÍTICA AUTOMÁTICA
DE COPYRIGHT + CONTROLE DE LOTE.

Novo relato real Windows (video 095.mp4): o TikTok pode mostrar WARNING em
UMA verificação (direitos autorais: "Foram encontrados problemas de direitos
autorais. Você ainda pode publicar este vídeo, mas ele será silenciado.")
enquanto OUTRA verificação (conteúdo: "Verificação em andamento. Isso levará
cerca de 10 minutos...") ainda está PENDING. Isso prova que WARNING != estado
final quando outras verificações ainda não terminaram, e exige um modelo por
verificação (COPYRIGHT/CONTENT/...) com regra de agregação onde PENDING
sempre domina sobre FAILED/WARNING.

Este arquivo cobre os 40 cenários obrigatórios do prompt, organizados em:
  A. TikTokAggregationStateTests        -- estados 1-10
  B. TikTokCopyrightPolicyTests         -- política 11-19 (+ extras)
  C. TikTokBatchPolicyTests             -- lote 20-25
  D. TikTokPolicyPersistenceTests       -- persistência 26-29
  E. TikTokPolicyIsolationTests         -- isolamento por perfil/conta 30-33
  F. TikTokPolicySecurityTests          -- segurança 34-40
"""
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import contextlib
import json
import unittest

from _sistema import agendar_tiktok as tiktok
from tests.fakes_playwright import FakeElement, FakeLocator, FakePage
from tests.test_agendar_tiktok_time_layers import build_page
from tests.test_agendar_tiktok_preflight_checks import (
    SECTION_SELECTOR,
    ITEM_SELECTOR,
    _modal_appears_n_times,
)

SAO_PAULO = ZoneInfo("America/Sao_Paulo")

COPY_LABEL = "Verificação de direitos autorais de música"
CONTENT_LABEL = "Verificação de conteúdo"
OTHER_LABEL = "Verificação de qualidade de imagem"


class _CheckDescriptor:
    """Descreve um check já construído no formato real (identidade +
    eventual grupo de variantes de status), pronto para ser montado dentro
    de uma seção "Verificações" por _section_from_items(). ``kind`` é
    "copyright"/"content"/"other" -- usado só para saber ONDE, na seção,
    registrar a identidade (data-e2e ou headline); a classificação real de
    kind/status dentro da produção continua vindo de
    _gather_check_items()/_classify_check_container(), nunca deste objeto."""

    def __init__(self, kind, wrapper, identity_el=None, headline_el=None, status_variants=()):
        self.kind = kind
        self.wrapper = wrapper
        self.identity_el = identity_el
        self.headline_el = headline_el
        self.status_variants = list(status_variants)


def _copyright_item(status):
    """Item de copyright construído SEM variantes de status estruturais
    (apenas a identidade data-e2e) -- exercita deliberadamente o fallback de
    texto de _classify_check_item(), preservando a intenção original destes
    testes (classificação por texto livre de cada estado)."""
    body = {
        tiktok.CHECK_WARNING: (
            "Foram encontrados problemas de direitos autorais. Você ainda "
            "pode publicar este vídeo, mas ele será silenciado."
        ),
        tiktok.CHECK_FAILED: "Problema encontrado: vídeo bloqueado por direitos autorais.",
        tiktok.CHECK_PENDING: "Verificação em andamento. Isso levará cerca de 10 minutos.",
        tiktok.CHECK_PASSED: "Nenhum problema encontrado.",
    }.get(status, "Estado exótico não documentado.")
    text = f"{COPY_LABEL}\n{body}"
    wrapper = FakeElement(tag="div", text=text, visible=True)
    identity = FakeElement(tag="div", attrs={"data-e2e": "copyright_container"}, text=COPY_LABEL, visible=True, parent=wrapper)
    wrapper._children = {tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([identity])}
    return _CheckDescriptor("copyright", wrapper, identity_el=identity)


def _content_item(status):
    """Análogo a _copyright_item(), localizado por headline em vez de
    data-e2e, também exercitando o fallback textual."""
    body = {
        tiktok.CHECK_WARNING: "Atenção: possível problema de conteúdo.",
        tiktok.CHECK_FAILED: "Problema encontrado no conteúdo.",
        tiktok.CHECK_PENDING: "Em andamento.",
        tiktok.CHECK_PASSED: "Nenhum problema encontrado.",
    }.get(status, "Estado exótico não documentado.")
    text = f"{CONTENT_LABEL}\n{body}"
    wrapper = FakeElement(tag="div", text=text, visible=True)
    headline = FakeElement(tag="div", attrs={"class": "headline-wrapper"}, text=CONTENT_LABEL, visible=True, parent=wrapper)
    wrapper._children = {tiktok.CONTENT_HEADLINE_SELECTOR: FakeLocator([headline])}
    return _CheckDescriptor("content", wrapper, headline_el=headline)


def _other_item(status):
    """Simula um TERCEIRO check não mapeado (nem copyright, nem conteúdo).
    Na arquitetura nova, um check só é descoberto pela varredura genérica
    (Pass 2 de _gather_check_items) se tiver pelo menos um elemento que bata
    com STATUS_RESULT_SELECTOR -- por isso este helper inclui uma variante
    "status-mystery" (não reconhecida por nenhuma das 5 variantes reais),
    sempre visível/ativa, para que o container seja encontrado, mas cuja
    leitura ESTRUTURAL não resolve (kind=OTHER, status inicialmente
    CHECK_UNKNOWN por estrutura) -- caindo então para o mesmo fallback
    textual usado por _copyright_item/_content_item, preservando a
    intenção original de cada cenário (inclusive o "EXOTICO" -> UNKNOWN)."""
    body = {
        tiktok.CHECK_WARNING: "Atenção: possível problema.",
        tiktok.CHECK_FAILED: "Problema encontrado.",
        tiktok.CHECK_PENDING: "Verificando...",
        tiktok.CHECK_PASSED: "Nenhum problema encontrado.",
    }.get(status, "Estado exótico.")
    text = f"{OTHER_LABEL}\n{body}"
    wrapper = FakeElement(tag="div", text=text, visible=True)
    mystery = FakeElement(
        tag="div", attrs={"class": "status-result status-mystery", "data-show": "true"},
        visible=True, parent=wrapper,
    )
    wrapper._children = {tiktok.STATUS_RESULT_SELECTOR: FakeLocator([mystery])}
    return _CheckDescriptor("other", wrapper, status_variants=[mystery])


def _section_from_items(descriptors):
    """Monta a seção "Verificações" real a partir de uma lista de
    _CheckDescriptor -- preserva a assinatura antiga (lista de "itens") para
    que o corpo dos 40 testes deste arquivo não precise mudar, só a
    construção interna de cada item."""
    copyright_desc = next((d for d in descriptors if d.kind == "copyright"), None)
    content_desc = next((d for d in descriptors if d.kind == "content"), None)

    section_children = {
        tiktok.COPYRIGHT_CONTAINER_SELECTOR: (
            FakeLocator([copyright_desc.identity_el]) if copyright_desc else FakeLocator([])
        ),
        tiktok.CONTENT_HEADLINE_SELECTOR: (
            FakeLocator([content_desc.headline_el]) if content_desc else FakeLocator([])
        ),
    }
    all_variants = []
    for d in descriptors:
        all_variants.extend(d.status_variants)
    section_children[tiktok.STATUS_RESULT_SELECTOR] = FakeLocator(all_variants)
    return FakeElement(tag="div", visible=True, children=section_children)


def build_multi_check_page(item_snapshots):
    """FakePage cuja seção "Verificações" devolve, em chamadas sucessivas, os
    snapshots de item_snapshots em ordem (o último repete depois de
    esgotado). Lista vazia simula seção nunca encontrada (UNKNOWN)."""
    state = {"n": 0}

    def factory():
        if not item_snapshots:
            return FakeLocator([])
        idx = min(state["n"], len(item_snapshots) - 1)
        items = item_snapshots[idx]
        state["n"] += 1
        return FakeLocator([_section_from_items(items)])

    return FakePage(registry={SECTION_SELECTOR: factory}), state


def _items_for(page):
    return tiktok._gather_check_items(page)


# ============================================================================
# A. ESTADOS 1-10 -- aggregate_check_status() / get_tiktok_preflight_status()
#    / wait_for_tiktok_checks() com verificações independentes por tipo
# ============================================================================
class TikTokAggregationStateTests(unittest.TestCase):
    def test_1_warning_copyright_mais_pending_content_e_pending_geral(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PENDING)]])
        self.assertEqual(tiktok.CHECK_PENDING, tiktok.get_tiktok_preflight_status(page))

    def test_2_warning_copyright_mais_passed_content_e_warning_geral(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)]])
        self.assertEqual(tiktok.CHECK_WARNING, tiktok.get_tiktok_preflight_status(page))

    def test_3_passed_copyright_mais_pending_content_e_pending_geral(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_PASSED), _content_item(tiktok.CHECK_PENDING)]])
        self.assertEqual(tiktok.CHECK_PENDING, tiktok.get_tiktok_preflight_status(page))

    def test_4_passed_mais_passed_e_passed_geral(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_PASSED), _content_item(tiktok.CHECK_PASSED)]])
        self.assertEqual(tiktok.CHECK_PASSED, tiktok.get_tiktok_preflight_status(page))

    def test_5_failed_mais_pending_e_pending_geral_pending_domina_ate_failed(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_FAILED), _content_item(tiktok.CHECK_PENDING)]])
        self.assertEqual(tiktok.CHECK_PENDING, tiktok.get_tiktok_preflight_status(page))

    def test_6_failed_mais_passed_e_failed_geral(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_FAILED), _content_item(tiktok.CHECK_PASSED)]])
        self.assertEqual(tiktok.CHECK_FAILED, tiktok.get_tiktok_preflight_status(page))

    def test_7_unknown_mais_passed_e_unknown_geral_nunca_passed(self):
        page, _ = build_multi_check_page([[_other_item("EXOTICO"), _content_item(tiktok.CHECK_PASSED)]])
        self.assertEqual(tiktok.CHECK_UNKNOWN, tiktok.get_tiktok_preflight_status(page))

    def test_8_sequencia_warning_pending_depois_warning_passed_resolve_warning(self):
        page, state = build_multi_check_page([
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PENDING)],
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)],
        ])
        result = tiktok.wait_for_tiktok_checks(
            page, timeout_seconds=5, poll_interval=0.02,
            copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
        )
        self.assertEqual(tiktok.CHECK_WARNING, result)
        self.assertGreaterEqual(state["n"], 2)

    def test_9_pending_ate_timeout_aborta_com_reason_pending_timeout(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_PASSED), _content_item(tiktok.CHECK_PENDING)]])
        with self.assertRaises(tiktok.TikTokChecksBlockedError) as ctx:
            tiktok.wait_for_tiktok_checks(page, timeout_seconds=0.2, poll_interval=0.05)
        self.assertEqual("pending_timeout", ctx.exception.reason)

    def test_10_aviso_intermediario_nao_dispara_decisao_enquanto_pending(self):
        # Primeiro snapshot já tem um WARNING (copyright) mas o geral ainda é
        # PENDING (por causa de conteúdo) -- não pode virar decisão ainda.
        calls = {"n": 0}
        real_decide = tiktok.decide_tiktok_checks_outcome

        def spying_decide(items, policy):
            calls["n"] += 1
            return real_decide(items, policy)

        page, _ = build_multi_check_page([
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PENDING)],
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)],
        ])
        with mock.patch.object(tiktok, "decide_tiktok_checks_outcome", side_effect=spying_decide):
            tiktok.wait_for_tiktok_checks(
                page, timeout_seconds=5, poll_interval=0.02,
                copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
            )
        self.assertEqual(1, calls["n"], "decisão só deve ser chamada quando o geral deixa de ser PENDING")


# ============================================================================
# B. POLÍTICA 11-19 -- decide_tiktok_checks_outcome() / wait_for_tiktok_checks()
# ============================================================================
class TikTokCopyrightPolicyTests(unittest.TestCase):
    def test_11_warning_copyright_allow_prossegue(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)]])
        items = _items_for(page)
        outcome, reason = tiktok.decide_tiktok_checks_outcome(items, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual(("PROCEED", None), (outcome, reason))

    def test_12_warning_copyright_block_bloqueia(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)]])
        items = _items_for(page)
        outcome, reason = tiktok.decide_tiktok_checks_outcome(items, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK)
        self.assertEqual(("BLOCKED", "copyright_warning_block"), (outcome, reason))

    def test_13_warning_pending_allow_nao_prossegue_ainda(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PENDING)]])
        with self.assertRaises(tiktok.TikTokChecksBlockedError) as ctx:
            tiktok.wait_for_tiktok_checks(
                page, timeout_seconds=0.2, poll_interval=0.05,
                copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
            )
        self.assertEqual("pending_timeout", ctx.exception.reason)

    def test_14_failed_allow_nunca_prossegue(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_FAILED), _content_item(tiktok.CHECK_PASSED)]])
        items = _items_for(page)
        outcome, reason = tiktok.decide_tiktok_checks_outcome(items, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual(("BLOCKED", "failed"), (outcome, reason))

    def test_15_bloqueado_explicito_allow_nunca_prossegue(self):
        # texto explícito com a palavra "bloqueado" -- também deve virar FAILED,
        # nunca contornado por ALLOW.
        wrapper = FakeElement(tag="div", text=f"{COPY_LABEL}\nVídeo bloqueado.", visible=True)
        identity = FakeElement(tag="div", attrs={"data-e2e": "copyright_container"}, text=COPY_LABEL, visible=True, parent=wrapper)
        wrapper._children = {tiktok.COPYRIGHT_CONTAINER_SELECTOR: FakeLocator([identity])}
        item = _CheckDescriptor("copyright", wrapper, identity_el=identity)
        page, _ = build_multi_check_page([[item, _content_item(tiktok.CHECK_PASSED)]])
        items = _items_for(page)
        outcome, reason = tiktok.decide_tiktok_checks_outcome(items, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual(("BLOCKED", "failed"), (outcome, reason))

    def test_16_unknown_allow_nunca_prossegue(self):
        page, _ = build_multi_check_page([[_other_item("EXOTICO"), _content_item(tiktok.CHECK_PASSED)]])
        items = _items_for(page)
        outcome, reason = tiktok.decide_tiktok_checks_outcome(items, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual(("BLOCKED", "unknown"), (outcome, reason))

    def test_17_pending_allow_continua_esperando_ate_timeout(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_PASSED), _content_item(tiktok.CHECK_PENDING)]])
        with self.assertRaises(tiktok.TikTokChecksBlockedError) as ctx:
            tiktok.wait_for_tiktok_checks(
                page, timeout_seconds=0.2, poll_interval=0.05,
                copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
            )
        self.assertEqual("pending_timeout", ctx.exception.reason)

    def test_18_warning_copyright_passed_content_allow_prossegue_automaticamente(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)]])
        result = tiktok.wait_for_tiktok_checks(
            page, timeout_seconds=5, poll_interval=0.02,
            copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
        )
        self.assertEqual(tiktok.CHECK_WARNING, result)

    def test_19_warning_copyright_passed_content_block_bloqueia(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)]])
        with self.assertRaises(tiktok.TikTokChecksBlockedError) as ctx:
            tiktok.wait_for_tiktok_checks(
                page, timeout_seconds=5, poll_interval=0.02,
                copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK,
            )
        self.assertEqual("copyright_warning_block", ctx.exception.reason)

    def test_19b_warning_nao_copyright_nunca_e_liberado_por_allow(self):
        page, _ = build_multi_check_page([[_other_item(tiktok.CHECK_WARNING), _copyright_item(tiktok.CHECK_PASSED)]])
        items = _items_for(page)
        outcome, reason = tiktok.decide_tiktok_checks_outcome(items, tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW)
        self.assertEqual(("BLOCKED", "warning_non_copyright"), (outcome, reason))

    def test_19c_policy_invalida_na_config_cai_para_block_seguro(self):
        cfg = {"tiktok_copyright_warning_policy": "algo-invalido"}
        self.assertEqual(
            tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK,
            tiktok._copyright_warning_policy_from_cfg(cfg),
        )


# ============================================================================
# C. LOTE 20-25 -- process_prepared_batch()
# ============================================================================
class TikTokBatchPolicyTests(unittest.TestCase):
    def _prepared(self, n):
        return [
            (Path(f"{i:03d}.mp4"), f"fp{i}", f"legenda {i}",
             datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO))
            for i in range(1, n + 1)
        ]

    def _run(self, cfg, prepared, schedule_side_effect):
        state = {"scheduled": []}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tiktok, "schedule_one", side_effect=schedule_side_effect))
            stack.enter_context(mock.patch.object(tiktok, "save_debug", return_value=None))
            stack.enter_context(mock.patch.object(tiktok, "save_json", return_value=None))
            stack.enter_context(mock.patch.object(tiktok.time, "sleep", return_value=None))
            scheduled_count, stop_code = tiktok.process_prepared_batch(page=object(), cfg=cfg, prepared=prepared, state=state)
        return scheduled_count, stop_code, state

    def test_20_failed_skip_and_continue_pula_e_continua(self):
        prepared = self._prepared(2)

        def side_effect(page, cfg, video, caption, target_dt):
            if video.name == "001.mp4":
                raise tiktok.TikTokChecksBlockedError("bloqueado", "failed")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "tiktok_blocked_item_policy": "SKIP_AND_CONTINUE"}
        scheduled_count, stop_code, state = self._run(cfg, prepared, side_effect)
        self.assertIsNone(stop_code)
        self.assertEqual(1, scheduled_count)
        self.assertEqual(["002.mp4"], [x["file"] for x in state["scheduled"]])

    def test_21_failed_stop_batch_para_o_lote(self):
        prepared = self._prepared(2)

        def side_effect(page, cfg, video, caption, target_dt):
            if video.name == "001.mp4":
                raise tiktok.TikTokChecksBlockedError("bloqueado", "failed")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "tiktok_blocked_item_policy": "STOP_BATCH"}
        scheduled_count, stop_code, state = self._run(cfg, prepared, side_effect)
        self.assertEqual(2, stop_code)
        self.assertEqual(0, scheduled_count)
        self.assertEqual([], state["scheduled"])

    def test_22_video_pulado_nunca_vira_agendado_ou_sucesso(self):
        prepared = self._prepared(1)

        def side_effect(page, cfg, video, caption, target_dt):
            raise tiktok.TikTokChecksBlockedError("bloqueado", "unknown")

        cfg = {"timezone_iana": "America/Sao_Paulo", "tiktok_blocked_item_policy": "SKIP_AND_CONTINUE"}
        scheduled_count, stop_code, state = self._run(cfg, prepared, side_effect)
        self.assertEqual(0, scheduled_count)
        self.assertEqual([], state["scheduled"])
        self.assertEqual(1, len(state.get("skipped_log", [])))
        self.assertEqual("unknown", state["skipped_log"][0]["reason"])

    def test_23_proximo_video_processado_apos_pulo(self):
        prepared = self._prepared(3)
        processed = []

        def side_effect(page, cfg, video, caption, target_dt):
            processed.append(video.name)
            if video.name == "002.mp4":
                raise tiktok.TikTokChecksBlockedError("bloqueado", "failed")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "tiktok_blocked_item_policy": "SKIP_AND_CONTINUE"}
        scheduled_count, stop_code, state = self._run(cfg, prepared, side_effect)
        self.assertEqual(["001.mp4", "002.mp4", "003.mp4"], processed)
        self.assertEqual(2, scheduled_count)

    def test_24_copyright_warning_block_skip_and_continue_tambem_pula(self):
        prepared = self._prepared(2)

        def side_effect(page, cfg, video, caption, target_dt):
            if video.name == "001.mp4":
                raise tiktok.TikTokChecksBlockedError("aviso", "copyright_warning_block")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "tiktok_blocked_item_policy": "SKIP_AND_CONTINUE"}
        scheduled_count, stop_code, state = self._run(cfg, prepared, side_effect)
        self.assertIsNone(stop_code)
        self.assertEqual(1, scheduled_count)

    def test_25_erro_generico_nao_relacionado_sempre_para_o_lote(self):
        prepared = self._prepared(2)

        def side_effect(page, cfg, video, caption, target_dt):
            if video.name == "001.mp4":
                raise RuntimeError("DOM não encontrado")
            return None

        cfg = {"timezone_iana": "America/Sao_Paulo", "tiktok_blocked_item_policy": "SKIP_AND_CONTINUE"}
        scheduled_count, stop_code, state = self._run(cfg, prepared, side_effect)
        self.assertEqual(2, stop_code, "erro genérico nunca é pulado, mesmo com SKIP_AND_CONTINUE")
        self.assertEqual(0, scheduled_count)


# ============================================================================
# D. PERSISTÊNCIA 26-29 -- config sobrevive a save/reload (mesma infra JSON
#    por conta já usada para todo o resto da config do TikTok -- não é criada
#    nenhuma segunda fonte de verdade nem uma migration SQLite nova; ver
#    GATE_19_5_ESTAGIO2_RELATORIO.md para a justificativa arquitetural).
# ============================================================================
class TikTokPolicyPersistenceTests(unittest.TestCase):
    def _roundtrip(self, tmp_path, cfg):
        config_file = tmp_path / "config.json"
        tiktok.save_json(config_file, cfg)
        reloaded = tiktok.load_json(config_file, {})
        return reloaded

    def test_26_copyright_allow_sobrevive_a_restart(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reloaded = self._roundtrip(Path(d), {"tiktok_copyright_warning_policy": "ALLOW"})
        self.assertEqual("ALLOW", tiktok._copyright_warning_policy_from_cfg(reloaded))

    def test_27_copyright_block_sobrevive_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reloaded = self._roundtrip(Path(d), {"tiktok_copyright_warning_policy": "BLOCK"})
        self.assertEqual("BLOCK", tiktok._copyright_warning_policy_from_cfg(reloaded))

    def test_28_blocked_item_skip_and_continue_sobrevive_a_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reloaded = self._roundtrip(Path(d), {"tiktok_blocked_item_policy": "SKIP_AND_CONTINUE"})
        self.assertEqual("SKIP_AND_CONTINUE", tiktok._blocked_item_policy_from_cfg(reloaded))

    def test_29_blocked_item_stop_batch_e_o_default_e_sobrevive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            reloaded = self._roundtrip(Path(d), {})
        self.assertEqual("STOP_BATCH", tiktok._blocked_item_policy_from_cfg(reloaded))


# ============================================================================
# E. ISOLAMENTO 30-33 -- escopo por conta (CONFIG_FILE já é por
#    account_dir_from_env/account_paths -- reaproveitado, não uma nova fonte)
# ============================================================================
class TikTokPolicyIsolationTests(unittest.TestCase):
    def test_30_perfil_a_allow_nao_vaza_para_perfil_b(self):
        cfg_a = {"tiktok_copyright_warning_policy": "ALLOW"}
        cfg_b = {"tiktok_copyright_warning_policy": "BLOCK"}
        self.assertEqual("ALLOW", tiktok._copyright_warning_policy_from_cfg(cfg_a))
        self.assertEqual("BLOCK", tiktok._copyright_warning_policy_from_cfg(cfg_b))

    def test_31_perfil_a_skip_and_continue_nao_vaza_para_perfil_b_default(self):
        cfg_a = {"tiktok_blocked_item_policy": "SKIP_AND_CONTINUE"}
        cfg_b = {}
        self.assertEqual("SKIP_AND_CONTINUE", tiktok._blocked_item_policy_from_cfg(cfg_a))
        self.assertEqual("STOP_BATCH", tiktok._blocked_item_policy_from_cfg(cfg_b))

    def test_32_arquivos_de_config_separados_nao_se_misturam_em_disco(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            file_a = Path(d) / "conta_a" / "config.json"
            file_b = Path(d) / "conta_b" / "config.json"
            file_a.parent.mkdir(parents=True)
            file_b.parent.mkdir(parents=True)
            tiktok.save_json(file_a, {"tiktok_copyright_warning_policy": "ALLOW"})
            tiktok.save_json(file_b, {"tiktok_copyright_warning_policy": "BLOCK"})
            reloaded_a = tiktok.load_json(file_a, {})
            reloaded_b = tiktok.load_json(file_b, {})
        self.assertEqual("ALLOW", reloaded_a["tiktok_copyright_warning_policy"])
        self.assertEqual("BLOCK", reloaded_b["tiktok_copyright_warning_policy"])

    def test_33_leitura_de_politica_nao_usa_estado_global_compartilhado(self):
        cfg_a = {"tiktok_copyright_warning_policy": "ALLOW"}
        cfg_b = {"tiktok_copyright_warning_policy": "BLOCK"}
        first = tiktok._copyright_warning_policy_from_cfg(cfg_a)
        second = tiktok._copyright_warning_policy_from_cfg(cfg_b)
        third = tiktok._copyright_warning_policy_from_cfg(cfg_a)
        self.assertEqual(["ALLOW", "BLOCK", "ALLOW"], [first, second, third])


# ============================================================================
# F. SEGURANÇA 34-40
# ============================================================================
class TikTokPolicySecurityTests(unittest.TestCase):
    def test_34_allow_nunca_converte_failed_em_sucesso(self):
        page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_FAILED), _content_item(tiktok.CHECK_PASSED)]])
        with self.assertRaises(tiktok.TikTokChecksBlockedError) as ctx:
            tiktok.wait_for_tiktok_checks(
                page, timeout_seconds=5, poll_interval=0.02,
                copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
            )
        self.assertEqual("failed", ctx.exception.reason)

    def test_35_allow_nunca_converte_unknown_em_sucesso(self):
        page, _ = build_multi_check_page([[_other_item("EXOTICO"), _content_item(tiktok.CHECK_PASSED)]])
        with self.assertRaises(tiktok.TikTokChecksBlockedError) as ctx:
            tiktok.wait_for_tiktok_checks(
                page, timeout_seconds=0.2, poll_interval=0.05,
                copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
            )
        # seção "found" (tem itens) mas 1 item ilegível -> UNKNOWN geral,
        # tratado (como sempre) como pending-até-timeout, nunca como sucesso.
        self.assertEqual("pending_timeout", ctx.exception.reason)

    def test_36_allow_nunca_ignora_pending(self):
        page, state = build_multi_check_page([
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PENDING)],
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PENDING)],
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)],
        ])
        result = tiktok.wait_for_tiktok_checks(
            page, timeout_seconds=5, poll_interval=0.01,
            copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
        )
        self.assertEqual(tiktok.CHECK_WARNING, result)
        self.assertGreaterEqual(state["n"], 3, "precisa ter consultado os 3 snapshots -- não pulou o PENDING")

    def test_37_publicar_agora_zero_cliques_em_cenario_bloqueado(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        page, time_field, date_field, final_button = build_page(
            target_dt, observed_hour_text=f"{target_dt.hour:02d}",
            observed_minute_text=f"{int(round(target_dt.minute / 5.0) * 5):02d}",
            observed_date_text=f"{target_dt.day:02d}/{target_dt.month:02d}/{target_dt.year}",
        )
        file_input_el = FakeElement(tag="input", attrs={"type": "file"})
        file_input_el.set_input_files = lambda *a, **kw: None
        page._registry['input[type="file"]'] = FakeLocator([file_input_el])
        caption_editor = FakeElement(tag="div", attrs={"contenteditable": "true", "role": "textbox"}, text="")
        page._registry['[contenteditable="true"][role="textbox"]'] = FakeLocator([caption_editor])
        page._registry["body"] = FakeLocator([FakeElement(text="scheduled")])

        checks_page, _ = build_multi_check_page([[_copyright_item(tiktok.CHECK_FAILED), _content_item(tiktok.CHECK_PASSED)]])
        page._registry[SECTION_SELECTOR] = checks_page._registry[SECTION_SELECTOR]
        publish_now_calls = []
        factory, _ = _modal_appears_n_times(0, publish_now_calls=publish_now_calls)
        page._registry['div[class*="common-modal"]'] = factory

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tiktok, "utc_now", return_value=target_dt.astimezone(ZoneInfo("UTC"))))
            stack.enter_context(mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None))
            stack.enter_context(mock.patch.object(tiktok, "set_caption", return_value=None))
            stack.enter_context(mock.patch.object(tiktok, "click_schedule_toggle", return_value=True))
            stack.enter_context(mock.patch.object(tiktok, "challenge_detected", return_value=False))
            stack.enter_context(mock.patch.object(tiktok, "save_debug", return_value=None))
            stack.enter_context(mock.patch.object(tiktok.time, "sleep", return_value=None))
            with self.assertRaises(tiktok.TikTokChecksBlockedError):
                tiktok.schedule_one(page, {}, Path("v.mp4"), "legenda", target_dt)
        self.assertEqual(0, publish_now_calls.count("click"))

    def test_38_allow_so_prossegue_apos_todas_verificacoes_terminarem(self):
        page, state = build_multi_check_page([
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PENDING)],
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)],
        ])
        decisions = []
        real_decide = tiktok.decide_tiktok_checks_outcome

        def spy(items, policy):
            result = real_decide(items, policy)
            decisions.append(result)
            return result

        with mock.patch.object(tiktok, "decide_tiktok_checks_outcome", side_effect=spy):
            tiktok.wait_for_tiktok_checks(
                page, timeout_seconds=5, poll_interval=0.01,
                copyright_warning_policy=tiktok.TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW,
            )
        self.assertEqual([("PROCEED", None)], decisions, "só decidiu 1 vez, depois de tudo resolvido")

    def test_39_aviso_intermediario_nao_dispara_acao_final(self):
        target_dt = datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO)
        final_calls = []
        page, time_field, date_field, final_button = build_page(
            target_dt, observed_hour_text=f"{target_dt.hour:02d}",
            observed_minute_text=f"{int(round(target_dt.minute / 5.0) * 5):02d}",
            observed_date_text=f"{target_dt.day:02d}/{target_dt.month:02d}/{target_dt.year}",
            final_button_calls=final_calls,
        )
        file_input_el = FakeElement(tag="input", attrs={"type": "file"})
        file_input_el.set_input_files = lambda *a, **kw: None
        page._registry['input[type="file"]'] = FakeLocator([file_input_el])
        caption_editor = FakeElement(tag="div", attrs={"contenteditable": "true", "role": "textbox"}, text="")
        page._registry['[contenteditable="true"][role="textbox"]'] = FakeLocator([caption_editor])
        page._registry["body"] = FakeLocator([FakeElement(text="scheduled")])

        checks_page, checks_state = build_multi_check_page([
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PENDING)],
            [_copyright_item(tiktok.CHECK_WARNING), _content_item(tiktok.CHECK_PASSED)],
        ])
        page._registry[SECTION_SELECTOR] = checks_page._registry[SECTION_SELECTOR]

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tiktok, "utc_now", return_value=target_dt.astimezone(ZoneInfo("UTC"))))
            stack.enter_context(mock.patch.object(tiktok, "ensure_login_and_upload_page", return_value=None))
            stack.enter_context(mock.patch.object(tiktok, "set_caption", return_value=None))
            stack.enter_context(mock.patch.object(tiktok, "click_schedule_toggle", return_value=True))
            stack.enter_context(mock.patch.object(tiktok, "challenge_detected", return_value=False))
            stack.enter_context(mock.patch.object(tiktok, "save_debug", return_value=None))
            stack.enter_context(mock.patch.object(tiktok.time, "sleep", return_value=None))
            cfg = {"tiktok_copyright_warning_policy": "ALLOW"}
            tiktok.schedule_one(page, cfg, Path("v.mp4"), "legenda", target_dt)
        self.assertEqual(1, final_calls.count("click"), "clique final só depois do WARNING+PENDING resolver para WARNING+PASSED")

    def test_40_allow_persistido_nao_exige_confirmacao_por_video(self):
        prepared = [
            (Path(f"{i:03d}.mp4"), f"fp{i}", f"legenda {i}",
             datetime(2026, 9, 20, 10, 0, tzinfo=SAO_PAULO))
            for i in range(1, 3)
        ]

        def side_effect(page, cfg, video, caption, target_dt):
            # Simula que schedule_one() já resolveu WARNING+ALLOW internamente
            # (sem perguntar nada) e clicou -- aqui só validamos que o LOTE
            # não pede confirmação nenhuma por vídeo.
            return None

        state = {"scheduled": []}
        cfg = {"timezone_iana": "America/Sao_Paulo", "tiktok_copyright_warning_policy": "ALLOW"}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tiktok, "schedule_one", side_effect=side_effect))
            stack.enter_context(mock.patch.object(tiktok, "save_debug", return_value=None))
            stack.enter_context(mock.patch.object(tiktok, "save_json", return_value=None))
            stack.enter_context(mock.patch.object(tiktok.time, "sleep", return_value=None))
            scheduled_count, stop_code = tiktok.process_prepared_batch(page=object(), cfg=cfg, prepared=prepared, state=state)
        self.assertIsNone(stop_code)
        self.assertEqual(2, scheduled_count)
        self.assertEqual(["001.mp4", "002.mp4"], [x["file"] for x in state["scheduled"]])


if __name__ == "__main__":
    unittest.main()
