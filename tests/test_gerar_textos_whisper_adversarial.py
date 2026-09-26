# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — PARTE 2 — Whisper (dentro de gerar_textos.py)

Testes determinísticos para transcribe()/get_transcript(), usando um FAKE do
objeto `model` (equivalente a faster_whisper.WhisperModel) -- nunca carregando
um modelo real, conforme exigido ("não carregar modelo pesado na suíte
unitária").
"""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from _sistema import gerar_textos as gt


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeWhisperModel:
    """Fake mínimo de faster_whisper.WhisperModel.transcribe()."""
    def __init__(self, segments, language="pt"):
        self._segments = segments
        self._language = language
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        info = SimpleNamespace(language=self._language)
        return iter(self._segments), info


class TranscribeTests(unittest.TestCase):
    def test_transcricao_normal_ptbr_junta_segmentos_nao_vazios(self):
        model = FakeWhisperModel([FakeSegment("Olá "), FakeSegment(""), FakeSegment("mundo!")], language="pt")
        text, lang = gt.transcribe(model, Path("video.mp4"))
        self.assertEqual("Olá mundo!", text)
        self.assertEqual("pt", lang)

    def test_transcricao_ingles(self):
        model = FakeWhisperModel([FakeSegment("Hello"), FakeSegment("world")], language="en")
        text, lang = gt.transcribe(model, Path("video.mp4"))
        self.assertEqual("Hello world", text)
        self.assertEqual("en", lang)

    def test_sem_fala_texto_vazio_nao_e_erro(self):
        """Vídeo sem fala: transcribe() retorna string vazia, sem levantar
        exceção -- é o caminho normal que empurra main() para o fallback de
        descrição visual (visual_summary), não uma falha."""
        model = FakeWhisperModel([], language=None)
        text, lang = gt.transcribe(model, Path("video_mudo.mp4"))
        self.assertEqual("", text)

    def test_segmentos_so_espacos_sao_descartados(self):
        model = FakeWhisperModel([FakeSegment("   "), FakeSegment("\n\t")], language="pt")
        text, _ = gt.transcribe(model, Path("video.mp4"))
        self.assertEqual("", text)

    def test_excecao_do_model_transcribe_propaga_nao_e_engolida_silenciosamente(self):
        class BoomModel:
            def transcribe(self, path, **kwargs):
                raise RuntimeError("modelo corrompido / arquivo de pesos inválido")

        with self.assertRaises(RuntimeError):
            gt.transcribe(BoomModel(), Path("video.mp4"))

    def test_parametros_passados_ao_model_transcribe_batem_com_o_documentado(self):
        model = FakeWhisperModel([FakeSegment("ok")], language="en")
        gt.transcribe(model, Path("video.mp4"))
        _, kwargs = model.calls[0]
        self.assertEqual("transcribe", kwargs["task"])
        self.assertIsNone(kwargs["language"])
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(1, kwargs["beam_size"])
        self.assertFalse(kwargs["condition_on_previous_text"])


class GetTranscriptCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.transcript_dir = Path(self._tmpdir.name)
        self._patch = mock.patch.object(gt, "TRANSCRIPT_DIR", self.transcript_dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_primeira_chamada_transcreve_e_grava_cache_segunda_chamada_reaproveita(self):
        model = FakeWhisperModel([FakeSegment("conteudo unico")], language="pt")
        fp = "fingerprint123"

        text1, lang1, cached1 = gt.get_transcript(model, Path("video.mp4"), fp)
        self.assertEqual("conteudo unico", text1)
        self.assertFalse(cached1)
        self.assertEqual(1, len(model.calls))

        # Segunda chamada: não deve tocar o model.transcribe() de novo.
        text2, lang2, cached2 = gt.get_transcript(model, Path("video.mp4"), fp)
        self.assertEqual("conteudo unico", text2)
        self.assertTrue(cached2)
        self.assertEqual(1, len(model.calls), "get_transcript não deveria retranscrever com cache disponível")

    def test_fingerprints_diferentes_nao_compartilham_cache(self):
        model = FakeWhisperModel([FakeSegment("texto A")], language="pt")
        gt.get_transcript(model, Path("a.mp4"), "fpA")

        model2 = FakeWhisperModel([FakeSegment("texto B")], language="en")
        text_b, lang_b, cached_b = gt.get_transcript(model2, Path("b.mp4"), "fpB")
        self.assertEqual("texto B", text_b)
        self.assertFalse(cached_b)


if __name__ == "__main__":
    unittest.main()
