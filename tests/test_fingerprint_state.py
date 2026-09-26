from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

import pytest

from _sistema import agendar_tiktok as tiktok
from _sistema import agendar_youtube as youtube
from _sistema import gerar_textos as textos
from _sistema import limpar_metadados_oficial as limpeza
from _sistema import painel_oficial as painel
from _sistema.state_json import StateJsonReadError


class FingerprintTests(unittest.TestCase):
    def test_mesmo_conteudo_produz_mesmo_fingerprint_em_todos_os_modulos(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            a = root / "001.mp4"
            b = root / "outro_nome.mp4"
            payload = (b"video-test-data-" * 300)
            a.write_bytes(payload)
            b.write_bytes(payload)

            esperado = painel.fingerprint(a)
            self.assertEqual(esperado, painel.fingerprint(b))
            self.assertEqual(esperado, youtube.fingerprint(a))
            self.assertEqual(esperado, tiktok.fingerprint(a))
            self.assertEqual(esperado, textos.fingerprint(a))
            self.assertEqual(esperado, limpeza.fingerprint_origem(a))

    def test_alteracao_de_conteudo_altera_fingerprint(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.mp4"
            b = root / "b.mp4"
            a.write_bytes(b"A" * 4096)
            b.write_bytes(b"B" * 4096)
            self.assertNotEqual(painel.fingerprint(a), painel.fingerprint(b))


LOAD_JSON_IMPLEMENTATIONS = [
    pytest.param("painel_oficial", painel.load_json, id="painel_oficial"),
    pytest.param("agendar_youtube", youtube.load_json, id="agendar_youtube"),
    pytest.param("agendar_tiktok", tiktok.load_json, id="agendar_tiktok"),
    pytest.param("gerar_textos", textos.load_json, id="gerar_textos"),
    pytest.param("limpar_metadados_oficial", limpeza.load_json, id="limpar_metadados_oficial"),
]


@pytest.mark.parametrize("loader_name,load_json", LOAD_JSON_IMPLEMENTATIONS)
def test_load_json_arquivo_inexistente_retorna_default(loader_name, load_json, tmp_path):
    path = tmp_path / "estado.json"
    default = {"ok": True, "loader": loader_name}

    assert load_json(path, default) == default


CORRUPTED_LOAD_JSON_IMPLEMENTATIONS = [
    pytest.param("painel_oficial", painel.load_json, id="painel_oficial"),
    pytest.param("agendar_youtube", youtube.load_json, id="agendar_youtube"),
    pytest.param("agendar_tiktok", tiktok.load_json, id="agendar_tiktok"),
    pytest.param("gerar_textos", textos.load_json, id="gerar_textos"),
    pytest.param("limpar_metadados_oficial", limpeza.load_json, id="limpar_metadados_oficial"),
]


@pytest.mark.parametrize("loader_name,load_json", CORRUPTED_LOAD_JSON_IMPLEMENTATIONS)
def test_load_json_corrompido_gera_erro_explicito(loader_name, load_json, tmp_path):
    path = tmp_path / "estado.json"
    default = {"items": {}, "scheduled": [], "loader": loader_name}
    original = b"{json-corrompido"
    path.write_bytes(original)

    with pytest.raises(StateJsonReadError, match="JSON de estado inválido ou ilegível"):
        load_json(path, default)

    assert path.read_bytes() == original


class JsonAndStateTests(unittest.TestCase):
    def test_save_json_e_atomico_e_nao_deixa_tmp(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "dados" / "estado.json"
            painel.save_json(p, {"valor": "ç"})
            self.assertEqual({"valor": "ç"}, json.loads(p.read_text(encoding="utf-8")))
            self.assertFalse(p.with_suffix(".json.tmp").exists())

    def test_carregar_estado_limpeza_normaliza_estrutura_invalida(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "limpeza_estado.json"
            p.write_text(json.dumps({"version": 7, "items": []}), encoding="utf-8")
            st = limpeza.carregar_estado_limpeza(p)
            self.assertEqual(7, st["version"])
            self.assertEqual({}, st["items"])

    def test_salvar_estado_limpeza_persiste_updated_at(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "limpeza_estado.json"
            st = limpeza.estado_limpeza_padrao()
            limpeza.salvar_estado_limpeza(p, st)
            saved = json.loads(p.read_text(encoding="utf-8"))
            self.assertIsInstance(saved.get("updated_at"), str)
            self.assertTrue(saved["updated_at"])


if __name__ == "__main__":
    unittest.main()
