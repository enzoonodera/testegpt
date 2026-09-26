from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import json
import sys
import unittest

from _sistema import gerar_textos as textos
from _sistema import agendar_tiktok as tiktok


class AiParsingTests(unittest.TestCase):
    def test_parse_json_text_aceita_json_puro_e_json_embutido(self):
        self.assertEqual({"a": 1}, textos.parse_json_text('{"a": 1}'))
        self.assertEqual({"b": 2}, textos.parse_json_text('```json\n{"b": 2}\n```'))
        self.assertIsNone(textos.parse_json_text("sem json"))

    def test_clean_tags_normaliza_deduplica_e_limita(self):
        yt = textos.clean_tags(["shorts", "Viral", "#viral", "tema", "extra"], "youtube")
        self.assertEqual("#shorts", yt[0].lower())
        self.assertEqual(4, len(yt))
        self.assertEqual(len({x.lower() for x in yt}), len(yt))

        tt = textos.clean_tags("fyp, teste #TESTE", "tiktok")
        self.assertEqual(4, len(tt))
        self.assertIn("#fyp", [x.lower() for x in tt])
        self.assertEqual(1, sum(x.lower() == "#teste" for x in tt))

    def test_metadata_signature_e_estavel_e_muda_com_config_relevante(self):
        base = {
            "plataforma": "youtube",
            "idioma_metadata": "pt-BR",
            "pais_alvo": "Brasil",
            "ia_local": {"ollama_modelo_texto": "llama3.2", "ollama_modelo_visao": "moondream"},
        }
        a = textos.metadata_signature(base)
        b = textos.metadata_signature(json.loads(json.dumps(base)))
        mod = json.loads(json.dumps(base))
        mod["idioma_metadata"] = "en-US"
        self.assertEqual(a, b)
        self.assertNotEqual(a, textos.metadata_signature(mod))
        self.assertTrue(a.startswith("TXT_"))

    def test_generate_text_youtube_usa_ollama_mock_sem_rede(self):
        resposta = {"message": {"content": json.dumps({
            "title": "  Um título curto  ",
            "description": "Descrição específica.",
            "hashtags": ["tema", "viral", "teste", "extra"],
        })}}
        with patch.object(textos, "ollama_chat", return_value=resposta) as chat:
            out = textos.generate_text("http://127.0.0.1:11434", "modelo", "Transcript: conteúdo", "youtube", "pt-BR", "Brasil")
        self.assertEqual("Um título curto", out["titulo"])
        self.assertEqual("Descrição específica.", out["descricao"])
        self.assertEqual(4, len(out["hashtags"]))
        self.assertEqual("#shorts", out["hashtags"][0].lower())
        chat.assert_called_once()

    def test_generate_text_tiktok_usa_ollama_mock_sem_rede(self):
        resposta = {"message": {"content": json.dumps({
            "caption": "  Olha isso acontecer!  ",
            "hashtags": ["tema", "viral", "fyp", "teste"],
        })}}
        with patch.object(textos, "ollama_chat", return_value=resposta):
            out = textos.generate_text("http://127.0.0.1:11434", "modelo", "Visual description: algo", "tiktok", "pt-BR", "Brasil")
        self.assertEqual("Olha isso acontecer!", out["caption"])
        self.assertEqual(4, len(out["hashtags"]))

    def test_ollama_alive_mocka_urlopen(self):
        resposta = Mock()
        resposta.close.return_value = None
        with patch.object(textos.urllib.request, "urlopen", return_value=resposta) as urlopen:
            self.assertTrue(textos.ollama_alive("http://localhost:11434"))
        self.assertIn("/api/tags", urlopen.call_args.args[0])


class CacheTests(unittest.TestCase):
    def test_get_transcript_salva_e_reutiliza_cache_sem_retranscrever(self):
        with TemporaryDirectory() as td:
            cache_dir = Path(td) / "transcricoes"
            video = Path(td) / "001.mp4"
            video.write_bytes(b"dummy")
            with patch.object(textos, "TRANSCRIPT_DIR", cache_dir):
                with patch.object(textos, "transcribe", return_value=("fala teste", "pt")) as transcribe:
                    first = textos.get_transcript(object(), video, "fp123")
                self.assertEqual(("fala teste", "pt", False), first)
                transcribe.assert_called_once()

                with patch.object(textos, "transcribe", side_effect=AssertionError("não deveria retranscrever")):
                    second = textos.get_transcript(object(), video, "fp123")
                self.assertEqual(("fala teste", "pt", True), second)

    def test_caption_for_reaproveita_texto_persistido_por_fingerprint(self):
        with TemporaryDirectory() as td:
            video = Path(td) / "001.mp4"
            video.write_bytes(b"conteudo")
            fp = tiktok.fingerprint(video)
            texts = {fp: {"status": "done", "caption": "Legenda", "hashtags": ["#um", "#dois"]}}
            got_fp, caption = tiktok.caption_for(video, texts)
            self.assertEqual(fp, got_fp)
            self.assertEqual("Legenda #um #dois", caption)

    def test_main_textos_detecta_video_ja_processado_e_nao_chama_dependencias(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            video_dir = base / "videos"
            data_dir = base / "dados"
            video_dir.mkdir()
            data_dir.mkdir()
            video = video_dir / "001.mp4"
            video.write_bytes(b"conteudo atual")
            cfg = {
                "plataforma": "youtube",
                "nome_conta": "Teste",
                "pais_alvo": "Brasil",
                "idioma_metadata": "pt-BR",
                "ia_local": {"ollama_modelo_texto": "llama3.2", "ollama_modelo_visao": "moondream"},
            }
            config_file = base / "config_canal.json"
            config_file.write_text(json.dumps(cfg), encoding="utf-8")
            fp = textos.fingerprint(video)
            sig = textos.metadata_signature(cfg)
            texts_file = data_dir / "textos_postagem.json"
            texts_file.write_text(json.dumps({fp: {
                "arquivo": "001.mp4", "status": "done", "pipeline_versao": sig,
                "titulo": "Já pronto", "descricao": "Descrição", "hashtags": ["#Shorts"]
            }}), encoding="utf-8")

            patches = [
                patch.object(textos, "BASE", base),
                patch.object(textos, "DATA_DIR", data_dir),
                patch.object(textos, "VIDEO_DIR", video_dir),
                patch.object(textos, "TEXTS_FILE", texts_file),
                patch.object(textos, "CSV_FILE", data_dir / "textos_postagem.csv"),
                patch.object(textos, "TRANSCRIPT_DIR", data_dir / "transcricoes"),
                patch.object(textos, "FRAME_DIR", data_dir / "frames_ia_temp"),
                patch.object(textos, "CONFIG_FILE", config_file),
                patch.object(textos, "check_cmd", side_effect=AssertionError("FFmpeg não deve ser consultado")),
                patch.object(textos, "try_start_ollama", side_effect=AssertionError("Ollama não deve ser iniciado")),
                patch.object(sys, "argv", ["gerar_textos.py"]),
            ]
            for p in patches:
                p.start()
            try:
                rc = textos.main()
            finally:
                for p in reversed(patches):
                    p.stop()

            self.assertEqual(0, rc)
            self.assertTrue((data_dir / "textos_postagem.csv").exists())


if __name__ == "__main__":
    unittest.main()
