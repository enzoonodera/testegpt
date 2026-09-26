# -*- coding: utf-8 -*-
"""
GATE 19.5 -- CORREÇÃO CRÍTICA (rodada de 21/09/2026) -- Bug A, parte
"configurável pelo menu".

Antes desta rodada, `timeout_verificacao_direitos_autorais_segundos` só
podia ser mudado editando `config_canal.json` na mão -- não existia
NENHUMA tela no menu do painel para editar essa (ou qualquer outra) opção
de uma conta já criada. Esta rodada adiciona uma tela mínima e focada
(opção 8 do menu principal, `configurar_verificacao_direitos_autorais_
youtube()`) -- não um editor de configurações genérico (fora de escopo
desta correção).

Este teste roda o FLUXO REAL do menu (função de produção chamada de
ponta a ponta, só `input()`/`print()` mockeados), não a função isolada de
leitura/gravação de JSON -- é a evidência exigida pelo prompt desta
rodada.
"""
from pathlib import Path
from unittest import mock
import json
import tempfile
import unittest

from _sistema import painel_oficial as painel


class ConfigurarVerificacaoDireitosAutoraisMenuTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent.parent))
        self.addCleanup(self._tmp.cleanup)
        self.accounts_root = Path(self._tmp.name) / "contas"
        self._patch = mock.patch.object(painel, "ACCOUNTS", self.accounts_root)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.acc = self.accounts_root / "youtube" / "Canal_Teste"
        self.acc.mkdir(parents=True)
        self.cfg_path = painel.cfg_file(self.acc)
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        painel.save_json(self.cfg_path, {
            "plataforma": "youtube",
            "nome_conta": "Canal Teste",
            "timezone_iana": "America/Sao_Paulo",
            "timeout_verificacao_direitos_autorais_segundos": 660,
        })

    def _run_menu(self, respostas):
        # `pause()` (chamado no fim de TODO caminho da função de produção,
        # inclusive nos de erro) também consome um input() -- por isso toda
        # lista de respostas aqui termina com um valor extra "" só para o
        # ENTER do pause().
        respostas_iter = iter(list(respostas) + [""])
        with mock.patch("builtins.input", side_effect=lambda *a, **k: next(respostas_iter)), \
             mock.patch("builtins.print"):
            painel.configurar_verificacao_direitos_autorais_youtube()

    def test_1_fluxo_do_menu_atualiza_o_config_da_conta_no_disco(self):
        # respostas: "1" escolhe a única conta listada; "900" é o novo valor.
        self._run_menu(["1", "900"])
        reloaded = painel.load_json(self.cfg_path, {})
        self.assertEqual(900, reloaded["timeout_verificacao_direitos_autorais_segundos"])

    def test_2_enter_vazio_mantem_o_valor_atual_sem_reescrever_nada_de_errado(self):
        before = self.cfg_path.read_bytes()
        self._run_menu(["1", ""])
        after = self.cfg_path.read_bytes()
        self.assertEqual(before, after, "ENTER vazio não deve alterar o arquivo")

    def test_3_valor_nao_numerico_e_rejeitado_sem_gravar(self):
        before = self.cfg_path.read_bytes()
        self._run_menu(["1", "abc"])
        after = self.cfg_path.read_bytes()
        self.assertEqual(before, after)

    def test_4_valor_fora_da_faixa_permitida_e_rejeitado(self):
        before = self.cfg_path.read_bytes()
        self._run_menu(["1", "5"])  # menor que o piso de 30s
        after = self.cfg_path.read_bytes()
        self.assertEqual(before, after)

    def test_5_demais_chaves_da_conta_preservadas_apos_atualizar(self):
        self._run_menu(["1", "700"])
        reloaded = painel.load_json(self.cfg_path, {})
        self.assertEqual("Canal Teste", reloaded["nome_conta"])
        self.assertEqual("America/Sao_Paulo", reloaded["timezone_iana"])
        self.assertEqual(700, reloaded["timeout_verificacao_direitos_autorais_segundos"])

    def test_6_opcao_8_do_menu_principal_chama_esta_tela(self):
        with mock.patch.object(painel, "configurar_verificacao_direitos_autorais_youtube") as fake, \
             mock.patch("builtins.input", side_effect=["8", "0"]), \
             mock.patch("builtins.print"), \
             mock.patch.object(painel, "initialize_app_storage") as fake_init:
            fake_init.return_value = mock.Mock(conflicts=False)
            painel.main()
        fake.assert_called_once()

    def test_7_novo_default_de_conta_nova_ja_nao_e_45s(self):
        cfg = painel.default_config("youtube", "Nova Conta", "Brasil", "pt-BR", "America/Sao_Paulo")
        self.assertGreaterEqual(cfg["timeout_verificacao_direitos_autorais_segundos"], 660)


if __name__ == "__main__":
    unittest.main()
