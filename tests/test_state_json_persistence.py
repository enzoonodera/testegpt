# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — PARTE 2 — PRIORIDADE 4 (state_json / restart)

A Parte 1 afirmou, por leitura de código, que _sistema/state_json.py tem
contrato de erro explícito e que a escrita (implementada em cada chamador via
tmp+os.replace) é atômica. Esta parte PROVA isso com testes reais, usando o
próprio `_sistema/state_json.load_state_json` e o padrão de escrita atômica
real usado em produção (idêntico em painel_oficial.py, gerar_textos.py,
limpar_metadados_oficial.py e nos dois agendadores).
"""
from pathlib import Path
import json
import os
import unittest
from unittest import mock

from _sistema.state_json import StateJsonReadError, load_state_json


def save_json_atomic(path: Path, data) -> None:
    """Mesmo padrão usado em produção (copiado deliberadamente aqui, não
    importado, para não acoplar o teste a um único módulo chamador -- o
    padrão é idêntico nos 5 pontos de escrita do projeto)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class LoadMissingOrEmptyTests(unittest.TestCase):
    def test_arquivo_inexistente_usa_default(self):
        p = Path("/tmp/does_not_exist_state_json_test/foo.json")
        self.assertEqual({"x": 1}, load_state_json(p, {"x": 1}))

    def test_arquivo_vazio_levanta_erro_explicito_nao_vira_default_silencioso(self):
        """Achado confirmado: um JSON de 0 bytes (ex.: disco cheio no meio da
        primeira escrita) NÃO é tratado como "sem estado ainda" -- é tratado
        como corrupção, corretamente."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "estado.json"
            p.write_text("", encoding="utf-8")
            with self.assertRaises(StateJsonReadError):
                load_state_json(p, {"default": True})

    def test_json_invalido_levanta_stateJsonReadError(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "estado.json"
            p.write_text("{isso nao e json valido", encoding="utf-8")
            with self.assertRaises(StateJsonReadError) as ctx:
                load_state_json(p, {})
            self.assertIn(str(p), str(ctx.exception))

    def test_json_truncado_no_meio_de_uma_string_levanta_erro(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "estado.json"
            full = json.dumps({"items": {"a": "b", "c": "d"}})
            # Simula um corte no meio da escrita (ex.: processo morto no meio
            # do write do sistema operacional para um arquivo NÃO temporário,
            # que é justamente o que a estratégia tmp+replace evita).
            p.write_text(full[: len(full) // 2], encoding="utf-8")
            with self.assertRaises(StateJsonReadError):
                load_state_json(p, {})

    def test_permission_error_na_leitura_vira_stateJsonReadError_nao_crash_cru(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "estado.json"
            p.write_text("{}", encoding="utf-8")
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("sem permissao")):
                with self.assertRaises(StateJsonReadError):
                    load_state_json(p, {})


class AtomicWriteRestartTests(unittest.TestCase):
    """Ciclo completo: salvar -> 'destruir' a instância (não há estado de
    processo Python a destruir de fato; o que importa é que nada além do
    arquivo em disco carrega estado) -> nova instância -> carregar -> comparar."""

    def test_save_close_reload_compara_igual(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dados" / "estado.json"
            original = {"version": 1, "items": {"abc": {"status": "done"}}, "updated_at": None}

            save_json_atomic(p, original)
            # "fecha a instância": não existe nenhum estado em memória de
            # processo para limpar aqui -- é exatamente esse o ponto: tudo
            # que importa está no arquivo.
            reloaded = load_state_json(p, None)
            self.assertEqual(original, reloaded)

            # "nova instância" == chamar load de novo, do zero, sem reusar
            # nenhuma referência da primeira leitura.
            reloaded_again = load_state_json(p, None)
            self.assertEqual(original, reloaded_again)
            self.assertIsNot(reloaded, reloaded_again)

    def test_dois_canais_diferentes_nao_compartilham_arquivo_nem_vazam_dado(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p_a = Path(d) / "canal_a" / "dados" / "estado.json"
            p_b = Path(d) / "canal_b" / "dados" / "estado.json"

            save_json_atomic(p_a, {"canal": "A", "items": {"x": 1}})
            save_json_atomic(p_b, {"canal": "B", "items": {"y": 2}})

            state_a = load_state_json(p_a, None)
            state_b = load_state_json(p_b, None)

            self.assertEqual("A", state_a["canal"])
            self.assertEqual("B", state_b["canal"])
            self.assertNotIn("y", state_a["items"])
            self.assertNotIn("x", state_b["items"])

    def test_falha_durante_escrita_do_tmp_nao_destroi_o_ultimo_estado_valido(self):
        """PROVA da invariante pedida explicitamente pela Parte 2: uma falha
        durante uma NOVA escrita não pode destruir o último estado válido
        gravado em disco."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "estado.json"
            good_state = {"version": 1, "items": {"ok": True}}
            save_json_atomic(p, good_state)
            self.assertEqual(good_state, load_state_json(p, None))

            bad_state = {"version": 2, "items": {"nunca_deveria_aparecer": True}}
            with mock.patch.object(Path, "write_text", side_effect=PermissionError("disco negou escrita")):
                with self.assertRaises(PermissionError):
                    save_json_atomic(p, bad_state)

            # O arquivo REAL (não o .tmp) tem que continuar sendo o último
            # estado válido -- a escrita nunca chegou a substituí-lo, porque
            # o write_text falhou ANTES do os.replace/tmp.replace.
            self.assertEqual(good_state, load_state_json(p, None))

    def test_falha_durante_o_replace_final_nao_deixa_arquivo_truncado(self):
        """Mesmo se o replace() final falhar (ex.: race de permissão entre o
        write e o rename), o arquivo de destino não pode ficar truncado —
        só pode continuar com o conteúdo antigo (replace nunca é parcial no
        nível do SO) ou levantar erro explícito, nunca os dois merged."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "estado.json"
            good_state = {"version": 1, "items": {"ok": True}}
            save_json_atomic(p, good_state)

            bad_state = {"version": 2, "items": {"nao_deveria_aparecer": True}}
            with mock.patch.object(Path, "replace", side_effect=PermissionError("replace negado")):
                with self.assertRaises(PermissionError):
                    save_json_atomic(p, bad_state)

            # O .tmp pode ter ficado no disco (é aceitável -- próxima escrita
            # sobrescreve o mesmo .tmp), mas o arquivo real não pode ter sido
            # corrompido nem substituído parcialmente.
            self.assertEqual(good_state, load_state_json(p, None))
            tmp = p.with_suffix(p.suffix + ".tmp")
            self.assertTrue(tmp.exists(), "o .tmp intermediário deveria existir intacto após o replace falhar")


if __name__ == "__main__":
    unittest.main()
