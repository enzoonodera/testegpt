# -*- coding: utf-8 -*-
"""
GATE 19.5 -- NOVA FUNCIONALIDADE (rodada de 21/09/2026): recomendação de
quantidade/horários de postagem por tema do canal, via Ollama local, com
editor manual por dia da semana como alternativa -- YouTube e TikTok.

Este arquivo cobre painel_oficial.py: validação estrita do schema da
recomendação, os cinco cenários obrigatórios de
gerar_recomendacao_horarios_ollama() (resposta válida; não-JSON; JSON fora
do schema; Ollama indisponível; timeout), o editor manual por dia da
semana (linha vazia mantém valor anterior; horário inválido reper gunta só
aquele dia; confirmação final), a edição posterior de uma conta já
existente, e um teste end-to-end de add_account() com Ollama mockado
retornando uma recomendação válida que o usuário aceita.

A extensão simétrica de agendar_tiktok.py build_slots() para
horarios_por_dia está coberta em tests/test_schedule_slots.py
(TiktokHorariosPorDiaTests), não aqui -- este arquivo é só painel_oficial.py.

NÃO toca a pergunta interativa de política de copyright, o escopo do card
de verificações do TikTok, nem build_slots()/schedule_one() em si.
"""
from pathlib import Path
from unittest import mock
import json
import tempfile
import unittest

from _sistema import painel_oficial as painel


def _recomendacao_valida():
    return {
        "segunda": ["09:00", "18:00"],
        "terca": ["09:00", "18:00"],
        "quarta": ["09:00", "18:00"],
        "quinta": ["09:00", "18:00"],
        "sexta": ["09:00", "18:00", "21:00"],
        "sabado": ["11:00", "20:00"],
        "domingo": ["11:00", "20:00"],
    }


# ============================================================================
# A. normalizar_hhmm() / _validar_recomendacao_horarios() -- validação estrita
# ============================================================================
class NormalizarHhmmTests(unittest.TestCase):
    def test_horario_valido_e_normalizado(self):
        self.assertEqual("09:05", painel.normalizar_hhmm("9:05"))
        self.assertEqual("23:59", painel.normalizar_hhmm("23:59"))
        self.assertEqual("00:00", painel.normalizar_hhmm("00:00"))

    def test_hora_fora_do_intervalo_e_invalida(self):
        self.assertIsNone(painel.normalizar_hhmm("24:00"))
        self.assertIsNone(painel.normalizar_hhmm("-1:00"))

    def test_minuto_fora_do_intervalo_e_invalido(self):
        self.assertIsNone(painel.normalizar_hhmm("10:60"))

    def test_formato_nao_reconhecido_e_invalido(self):
        self.assertIsNone(painel.normalizar_hhmm("dez horas"))
        self.assertIsNone(painel.normalizar_hhmm("10h00"))
        self.assertIsNone(painel.normalizar_hhmm(""))


class ValidarRecomendacaoHorariosTests(unittest.TestCase):
    def test_recomendacao_completa_e_valida_e_aceita(self):
        resultado = painel._validar_recomendacao_horarios(_recomendacao_valida())
        self.assertIsNotNone(resultado)
        self.assertEqual(set(painel.DIAS_SEMANA_ORDEM), set(resultado.keys()))
        self.assertEqual(["09:00", "18:00", "21:00"], resultado["sexta"])

    def test_falta_um_dia_e_rejeitada_por_inteiro(self):
        obj = _recomendacao_valida()
        del obj["domingo"]
        self.assertIsNone(painel._validar_recomendacao_horarios(obj))

    def test_horario_mal_formatado_rejeita_a_recomendacao_inteira(self):
        obj = _recomendacao_valida()
        obj["segunda"] = ["9h", "18:00"]
        self.assertIsNone(painel._validar_recomendacao_horarios(obj))

    def test_mais_horarios_que_o_teto_e_rejeitado(self):
        obj = _recomendacao_valida()
        obj["sabado"] = [f"{h:02d}:00" for h in range(0, painel.HORARIOS_POR_DIA_TETO + 1)]
        self.assertGreater(len(obj["sabado"]), painel.HORARIOS_POR_DIA_TETO)
        self.assertIsNone(painel._validar_recomendacao_horarios(obj))

    def test_dia_com_lista_vazia_e_rejeitado(self):
        obj = _recomendacao_valida()
        obj["quarta"] = []
        self.assertIsNone(painel._validar_recomendacao_horarios(obj))

    def test_nao_e_dict_e_rejeitado(self):
        self.assertIsNone(painel._validar_recomendacao_horarios(["nao", "e", "dict"]))
        self.assertIsNone(painel._validar_recomendacao_horarios(None))

    def test_item_nao_string_dentro_da_lista_e_rejeitado(self):
        obj = _recomendacao_valida()
        obj["sexta"] = [9, "18:00"]
        self.assertIsNone(painel._validar_recomendacao_horarios(obj))


# ============================================================================
# B. gerar_recomendacao_horarios_ollama() -- os 5 cenários obrigatórios
# ============================================================================
class GerarRecomendacaoOllamaTests(unittest.TestCase):
    def _ia_cfg(self):
        return {"ollama_url": "http://localhost:11434", "ollama_modelo_texto": "llama3.2"}

    def test_a_resposta_json_valida_e_completa_e_aceita(self):
        payload_resposta = {"message": {"content": json.dumps(_recomendacao_valida())}}
        with mock.patch.object(painel, "ollama_alive", return_value=True), \
             mock.patch.object(painel, "ollama_chat", return_value=payload_resposta) as m_chat:
            resultado = painel.gerar_recomendacao_horarios_ollama(
                "humor", "youtube", "Estados Unidos", "en-US", self._ia_cfg(),
            )
        self.assertIsNotNone(resultado)
        self.assertEqual(set(painel.DIAS_SEMANA_ORDEM), set(resultado.keys()))
        m_chat.assert_called_once()

    def test_b_resposta_nao_json_e_tratada_como_falha(self):
        payload_resposta = {"message": {"content": "isto não é JSON de jeito nenhum"}}
        with mock.patch.object(painel, "ollama_alive", return_value=True), \
             mock.patch.object(painel, "ollama_chat", return_value=payload_resposta):
            resultado = painel.gerar_recomendacao_horarios_ollama(
                "humor", "youtube", "Estados Unidos", "en-US", self._ia_cfg(),
            )
        self.assertIsNone(resultado)

    def test_c_json_valido_mas_fora_do_schema_e_rejeitado_integralmente(self):
        obj_incompleto = _recomendacao_valida()
        del obj_incompleto["domingo"]  # falta um dia
        payload_resposta = {"message": {"content": json.dumps(obj_incompleto)}}
        with mock.patch.object(painel, "ollama_alive", return_value=True), \
             mock.patch.object(painel, "ollama_chat", return_value=payload_resposta):
            resultado = painel.gerar_recomendacao_horarios_ollama(
                "humor", "youtube", "Estados Unidos", "en-US", self._ia_cfg(),
            )
        self.assertIsNone(resultado)

    def test_d_ollama_indisponivel_pula_direto_sem_travar(self):
        with mock.patch.object(painel, "ollama_alive", return_value=False), \
             mock.patch.object(painel, "try_start_ollama", return_value=False) as m_start, \
             mock.patch.object(painel, "ollama_chat") as m_chat:
            resultado = painel.gerar_recomendacao_horarios_ollama(
                "humor", "youtube", "Estados Unidos", "en-US", self._ia_cfg(),
            )
        self.assertIsNone(resultado)
        m_start.assert_called_once()
        m_chat.assert_not_called()

    def test_e_timeout_do_ollama_e_falha_nao_fatal(self):
        with mock.patch.object(painel, "ollama_alive", return_value=True), \
             mock.patch.object(painel, "ollama_chat", side_effect=TimeoutError("timed out")):
            resultado = painel.gerar_recomendacao_horarios_ollama(
                "humor", "youtube", "Estados Unidos", "en-US", self._ia_cfg(),
            )
        self.assertIsNone(resultado)

    def test_ollama_indisponivel_mas_try_start_ollama_recupera(self):
        """Confirma que o caminho de tentar iniciar o Ollama local (como já
        acontece em gerar_textos.py) é exercitado antes de desistir."""
        payload_resposta = {"message": {"content": json.dumps(_recomendacao_valida())}}
        with mock.patch.object(painel, "ollama_alive", return_value=False), \
             mock.patch.object(painel, "try_start_ollama", return_value=True) as m_start, \
             mock.patch.object(painel, "ollama_chat", return_value=payload_resposta):
            resultado = painel.gerar_recomendacao_horarios_ollama(
                "humor", "youtube", "Estados Unidos", "en-US", self._ia_cfg(),
            )
        self.assertIsNotNone(resultado)
        m_start.assert_called_once()


# ============================================================================
# C. editar_horarios_semana() -- editor manual
# ============================================================================
class EditarHorariosSemanaTests(unittest.TestCase):
    def _run(self, current_by_day, default_times, respostas):
        respostas_iter = iter(respostas)
        with mock.patch("builtins.input", side_effect=lambda *a, **k: next(respostas_iter)):
            return painel.editar_horarios_semana(current_by_day, default_times)

    def test_linha_vazia_mantem_valor_anterior_do_dia(self):
        current = {"segunda": ["10:00"]}
        # 7 dias: segunda mantém (ENTER), os outros 6 recebem ENTER também
        # (mantêm o default_times), e "s" confirma no final.
        respostas = [""] * 7 + ["s"]
        resultado = self._run(current, ["12:00", "17:00"], respostas)
        self.assertIsNotNone(resultado)
        self.assertEqual(["10:00"], resultado["segunda"])
        self.assertEqual(["12:00", "17:00"], resultado["terca"])

    def test_horario_invalido_reper_gunta_so_aquele_dia_sem_perder_os_demais(self):
        # segunda: 1a tentativa inválida, 2a válida; demais dias: ENTER.
        respostas = ["10h00", "10:00"] + [""] * 6 + ["s"]
        resultado = self._run({}, ["12:00"], respostas)
        self.assertIsNotNone(resultado)
        self.assertEqual(["10:00"], resultado["segunda"])
        self.assertEqual(["12:00"], resultado["terca"])

    def test_confirmacao_final_negativa_cancela_sem_salvar(self):
        respostas = [""] * 7 + ["n"]
        resultado = self._run({}, ["12:00"], respostas)
        self.assertIsNone(resultado)

    def test_cancelar_durante_um_dia_aborta_imediatamente(self):
        respostas = ["cancelar"]
        resultado = self._run({}, ["12:00"], respostas)
        self.assertIsNone(resultado)

    def test_varios_horarios_no_mesmo_dia_sao_normalizados_e_ordenados(self):
        respostas = ["18:00,9:00,09:00"] + [""] * 6 + ["s"]
        resultado = self._run({}, ["12:00"], respostas)
        self.assertIsNotNone(resultado)
        # "9:00" e "09:00" são o mesmo horário normalizado -- deduplicado.
        self.assertEqual(["09:00", "18:00"], resultado["segunda"])

    def test_mais_horarios_que_o_teto_reper_gunta_o_dia(self):
        excesso = ",".join(f"{h:02d}:00" for h in range(0, painel.HORARIOS_POR_DIA_TETO + 1))
        respostas = [excesso, "10:00"] + [""] * 6 + ["s"]
        resultado = self._run({}, ["12:00"], respostas)
        self.assertIsNotNone(resultado)
        self.assertEqual(["10:00"], resultado["segunda"])


# ============================================================================
# D. editar_horarios_conta_existente() e criação de conta -- fluxo do menu
# ============================================================================
class PainelHorariosMenuTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent.parent))
        self.addCleanup(self._tmp.cleanup)
        self.accounts_root = Path(self._tmp.name) / "contas"
        self._patch = mock.patch.object(painel, "ACCOUNTS", self.accounts_root)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _make_account(self, platform, name, extra_cfg=None):
        acc = self.accounts_root / platform / name
        acc.mkdir(parents=True)
        cfg = {"plataforma": platform, "nome_conta": name, "timezone_iana": "America/Sao_Paulo"}
        cfg.update(extra_cfg or {})
        painel.save_json(painel.cfg_file(acc), cfg)
        return acc

    def test_edicao_posterior_reabre_pre_preenchida_e_salva_na_conta_certa(self):
        acc = self._make_account("youtube", "Canal_A", {"horarios_por_dia": {"segunda": ["08:00"]}})
        self._make_account("youtube", "Canal_B")  # outra conta -- não pode ser afetada

        respostas = iter(["1", "08:00"] + [""] * 6 + ["s"])
        with mock.patch("builtins.input", side_effect=lambda *a, **k: next(respostas)), \
             mock.patch("builtins.print"):
            painel.editar_horarios_conta_existente()

        cfg_a = painel.load_json(painel.cfg_file(acc), {})
        self.assertEqual(["08:00"], cfg_a["horarios_por_dia"]["segunda"])
        cfg_b = painel.load_json(painel.cfg_file(self.accounts_root / "youtube" / "Canal_B"), {})
        self.assertNotIn("horarios_por_dia", cfg_b, "editar uma conta não pode afetar outra")

    def test_edicao_posterior_cancelada_nao_altera_o_arquivo(self):
        acc = self._make_account("tiktok", "Canal_C", {"horarios": ["10:00", "15:00"]})
        before = painel.cfg_file(acc).read_bytes()

        respostas = iter(["1", "cancelar"])
        with mock.patch("builtins.input", side_effect=lambda *a, **k: next(respostas)), \
             mock.patch("builtins.print"):
            painel.editar_horarios_conta_existente()

        after = painel.cfg_file(acc).read_bytes()
        self.assertEqual(before, after)

    def test_add_account_end_to_end_com_ollama_mockado_aceito(self):
        """Teste end-to-end obrigatório: criação de conta nova, tema
        fornecido, Ollama mockado retornando uma recomendação válida,
        usuário aceita -- confirma que horarios_por_dia gravado em
        cfg_file(acc) bate com o que foi aceito."""
        respostas = iter([
            "1",                 # plataforma: YouTube
            "Canal Teste E2E",   # nome
            "1",                 # preset: Estados Unidos / en-US
            "humor",             # tema do canal
            "s",                 # aceita a recomendação
            "n",                 # não tem pasta antiga
            "",                  # ENTER antes de abrir login
            "",                  # pause() final
        ])
        payload_resposta = {"message": {"content": json.dumps(_recomendacao_valida())}}
        with mock.patch("builtins.input", side_effect=lambda *a, **k: next(respostas)), \
             mock.patch.object(painel, "ollama_alive", return_value=True), \
             mock.patch.object(painel, "ollama_chat", return_value=payload_resposta), \
             mock.patch.object(painel, "run_engine", return_value=0):
            painel.add_account()

        acc = self.accounts_root / "youtube" / "Canal Teste E2E"
        self.assertTrue(acc.exists(), "a conta deveria ter sido criada")
        cfg = painel.load_json(painel.cfg_file(acc), {})
        self.assertEqual(_recomendacao_valida(), cfg.get("horarios_por_dia"))

    def test_add_account_nunca_trava_esperando_ollama_indisponivel(self):
        """A criação de conta NUNCA pode travar esperando o Ollama -- com
        Ollama indisponível E o usuário cancelando o editor manual
        também, a conta ainda é criada (com o horarios_por_dia default de
        default_config(), sem override)."""
        respostas = iter([
            "1",                  # plataforma: YouTube
            "Canal Sem Ollama",   # nome
            "1",                  # preset
            "humor",              # tema
            "cancelar",           # cancela o editor manual logo no 1o dia
            "n",                  # não tem pasta antiga
            "",                   # ENTER antes de login
            "",                   # pause() final
        ])
        with mock.patch("builtins.input", side_effect=lambda *a, **k: next(respostas)), \
             mock.patch.object(painel, "ollama_alive", return_value=False), \
             mock.patch.object(painel, "try_start_ollama", return_value=False), \
             mock.patch.object(painel, "run_engine", return_value=0):
            painel.add_account()

        acc = self.accounts_root / "youtube" / "Canal Sem Ollama"
        self.assertTrue(acc.exists(), "a conta tem que ser criada mesmo sem Ollama disponível")
        cfg = painel.load_json(painel.cfg_file(acc), {})
        # Sem recomendação aceita e editor cancelado -- fica o default de
        # default_config() (DAY_TIMES para YouTube), nunca um estado
        # quebrado/sem horarios_por_dia nenhum.
        self.assertEqual(painel.DAY_TIMES, cfg.get("horarios_por_dia"))


if __name__ == "__main__":
    unittest.main()
