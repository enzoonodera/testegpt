from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from _sistema import agendar_tiktok as tiktok
from _sistema import agendar_youtube as youtube
from _sistema import painel_oficial as painel
from _sistema import limpar_metadados_oficial as limpeza


class ConfigTests(unittest.TestCase):
    def test_default_config_youtube_preserva_campos_baseline(self):
        cfg = painel.default_config("youtube", "Canal A", "Brasil", "pt-BR")
        self.assertEqual("youtube", cfg["plataforma"])
        self.assertEqual("Canal A", cfg["nome_conta"])
        self.assertEqual("Brasil", cfg["pais_alvo"])
        self.assertEqual("pt-BR", cfg["idioma_metadata"])
        self.assertEqual("America/Sao_Paulo", cfg["timezone_iana"])
        self.assertTrue(cfg["exigir_formato_short"])
        self.assertEqual(180, cfg["duracao_maxima_short_segundos"])
        self.assertEqual(0, cfg["max_uploads_por_execucao"])
        self.assertIn("horarios_por_dia", cfg)
        self.assertEqual("small", cfg["ia_local"]["whisper_model_size"])

    def test_default_config_tiktok_preserva_campos_baseline(self):
        cfg = painel.default_config("tiktok", "Conta B", "México", "es-MX")
        self.assertEqual("tiktok", cfg["plataforma"])
        self.assertEqual(["10:00", "15:00", "20:00"], cfg["horarios"])
        self.assertEqual(9, cfg["dias_janela"])
        self.assertEqual("America/Mexico_City", cfg["timezone_iana"])
        self.assertIn("tiktokstudio/upload", cfg["upload_url"])
        self.assertTrue(cfg["marcar_comentarios"])

    def test_ensure_account_cria_estrutura_youtube_sem_sobrescrever_estado(self):
        with TemporaryDirectory() as td:
            acc = Path(td) / "youtube" / "Canal_A"
            acc.mkdir(parents=True)
            dados = acc / "dados"
            dados.mkdir()
            existente = {"version": 8, "scheduled": [{"file": "001.mp4"}]}
            (dados / "estado_youtube.json").write_text(json.dumps(existente), encoding="utf-8")

            painel.ensure_account(acc, "youtube")

            for nome in ["videos", "dados", "logs", "perfil_youtube", "bloqueados"]:
                self.assertTrue((acc / nome).exists(), nome)
            self.assertEqual(existente, json.loads((dados / "estado_youtube.json").read_text(encoding="utf-8")))
            self.assertTrue((dados / "limpeza_estado.json").exists())
            self.assertTrue((dados / "textos_postagem.json").exists())

    def test_ensure_account_persiste_timezone_iana_em_config_legado(self):
        with TemporaryDirectory() as td:
            acc = Path(td) / "youtube" / "Canal_A"
            acc.mkdir(parents=True)
            (acc / "config_canal.json").write_text(
                json.dumps({"plataforma": "youtube", "pais_alvo": "Brasil"}),
                encoding="utf-8",
            )

            painel.ensure_account(acc, "youtube")

            cfg = json.loads((acc / "config_canal.json").read_text(encoding="utf-8"))
            self.assertEqual("America/Sao_Paulo", cfg["timezone_iana"])

    def test_ensure_account_cria_estado_tiktok_version_2(self):
        with TemporaryDirectory() as td:
            acc = Path(td) / "tiktok" / "Conta"
            acc.mkdir(parents=True)
            painel.ensure_account(acc, "tiktok")
            state = json.loads((acc / "dados" / "estado_tiktok.json").read_text(encoding="utf-8"))
            self.assertEqual(2, state["version"])
            self.assertEqual([], state["scheduled"])
            self.assertTrue((acc / "perfil_tiktok").is_dir())


class NameAndPathTests(unittest.TestCase):
    def test_slugify_remove_caracteres_invalidos_do_windows(self):
        self.assertEqual("Meu_Canal_ 01", painel.slugify('  Meu/Canal: 01.  '))
        self.assertEqual("Conta", painel.slugify("   ...   "))

    def test_natural_sort_usa_numeros_e_nao_ordem_lexicografica(self):
        paths = [Path("10.mp4"), Path("2.mp4"), Path("001.mp4"), Path("a11.mp4"), Path("a3.mp4")]
        yt = [p.name for p in sorted(paths, key=youtube.natural_key)]
        tt = [p.name for p in sorted(paths, key=tiktok.natural_key)]
        self.assertEqual(["001.mp4", "2.mp4", "10.mp4", "a3.mp4", "a11.mp4"], yt)
        self.assertEqual(yt, tt)

    def test_numero_e_proximo_indice_saida(self):
        self.assertEqual(7, limpeza.numero_do_nome("007.mp4"))
        self.assertEqual(0, limpeza.numero_do_nome("video.mp4"))
        with TemporaryDirectory() as td:
            out = Path(td)
            (out / "001.mp4").write_bytes(b"x")
            (out / "009.mp4").write_bytes(b"x")
            (out / "nome.mp4").write_bytes(b"x")
            (out / "100.txt").write_bytes(b"x")
            self.assertEqual(10, limpeza.proximo_indice_saida(out))

    def test_maior_indice_postado_e_estado_reservado_entram_na_numeracao(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            out = root / "videos"
            out.mkdir()
            dados = root / "dados"
            dados.mkdir()
            (dados / "estado_youtube.json").write_text(
                json.dumps({"scheduled": [{"file": "012.mp4"}, {"file": "abc.mp4"}]}),
                encoding="utf-8",
            )
            estado = {"items": {"fp": {"output": "015.mp4"}}}
            self.assertEqual(12, limpeza.maior_indice_postado(out))
            self.assertEqual(16, limpeza.proximo_indice_com_estado(out, estado))

    def test_destino_para_usa_minimo_tres_digitos_e_expande_para_lotes_grandes(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            d1 = limpeza.destino_para(root / "orig.mp4", root, root / "out", 7, 200)
            d2 = limpeza.destino_para(root / "orig.mp4", root, root / "out", 7, 1200)
            self.assertEqual("007.mp4", d1.name)
            self.assertEqual("0007.mp4", d2.name)


if __name__ == "__main__":
    unittest.main()


class TimezoneConfigSafetyTests(unittest.TestCase):
    def test_default_config_personalizado_sem_timezone_explicito_falha(self):
        with self.assertRaises(painel.MissingTimezoneConfigurationError):
            painel.default_config("youtube", "Canal", "Personalizado", "pt-BR")

    def test_default_config_personalizado_aceita_utc_somente_quando_explicito(self):
        cfg = painel.default_config(
            "youtube", "Canal", "Personalizado", "pt-BR", timezone_iana="UTC"
        )
        self.assertEqual("UTC", cfg["timezone_iana"])

    def test_default_config_timezone_explicito_vence_default_do_pais(self):
        cfg = painel.default_config(
            "youtube", "Canal", "Brasil", "pt-BR", timezone_iana="Asia/Tokyo"
        )
        self.assertEqual("Asia/Tokyo", cfg["timezone_iana"])

    def test_ensure_account_custom_sem_timezone_nao_inventa_utc_nem_altera_config(self):
        with TemporaryDirectory() as td:
            acc = Path(td) / "youtube" / "Canal_Custom"
            acc.mkdir(parents=True)
            original = {"plataforma": "youtube", "pais_alvo": "Personalizado", "nome_conta": "X"}
            (acc / "config_canal.json").write_text(json.dumps(original), encoding="utf-8")

            painel.ensure_account(acc, "youtube")

            after = json.loads((acc / "config_canal.json").read_text(encoding="utf-8"))
            self.assertEqual(original, after)
            self.assertNotIn("timezone_iana", after)
