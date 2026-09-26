from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import unittest

from _sistema import agendar_youtube as youtube
from _sistema import gerar_textos as textos
from _sistema import limpar_metadados_oficial as limpeza


class FfprobeMockTests(unittest.TestCase):
    def test_video_duration_via_mock(self):
        fake = SimpleNamespace(stdout="12.345\n")
        with patch.object(textos.subprocess, "run", return_value=fake) as run:
            self.assertAlmostEqual(12.345, textos.video_duration(Path("video.mp4")), places=3)
        self.assertEqual("ffprobe", run.call_args.args[0][0])

    def test_probe_short_vertical_valido_via_mock(self):
        payload = {"streams": [{"width": 1080, "height": 1920}], "format": {"duration": "59.5"}}
        fake = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch.object(youtube.shutil, "which", return_value="/fake/ffprobe"), \
             patch.object(youtube.subprocess, "run", return_value=fake):
            info = youtube.probe_short(Path("short.mp4"), max_seconds=180)
        self.assertTrue(info["ok"])
        self.assertEqual(1080, info["width"])
        self.assertFalse(info["over_60"])

    def test_probe_short_horizontal_e_rejeitado_via_mock(self):
        payload = {"streams": [{"width": 1920, "height": 1080}], "format": {"duration": "30"}}
        fake = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch.object(youtube.shutil, "which", return_value="/fake/ffprobe"), \
             patch.object(youtube.subprocess, "run", return_value=fake):
            info = youtube.probe_short(Path("horizontal.mp4"), max_seconds=180)
        self.assertFalse(info["ok"])
        self.assertIn("Horizontal", info["reason"])

    def test_obter_info_video_parseia_ffprobe_via_mock(self):
        payload = {
            "streams": [
                {"codec_type": "video", "width": 720, "height": 1280, "avg_frame_rate": "30000/1001", "duration": "8.5"},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "8.5"},
        }
        fake = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch.object(limpeza, "executar_capture", return_value=fake):
            info = limpeza.obter_info_video(Path("video.mp4"))
        self.assertEqual(720, info["width"])
        self.assertEqual(1280, info["height"])
        self.assertTrue(info["has_audio"])
        self.assertAlmostEqual(30000 / 1001, info["fps"], places=4)
        self.assertEqual(8.5, info["duration"])


class FfmpegProcessMockTests(unittest.TestCase):
    def _patches_basicos(self):
        ajustes = {
            "zoom_direction": "in", "red_shift": 0.0, "blue_shift": 0.0,
            "contrast": 1.0, "saturation": 1.0, "brightness": 0.0, "gamma": 1.0,
        }
        meta = {
            "creation_time": "2026-01-01T10:00:00",
            "quicktime_creationdate": "2026-01-01T10:00:00-03:00",
            "gps": "-23.00000-046.00000/",
            "make": "Apple", "model": "iPhone 15", "software": "iOS",
        }
        return ajustes, meta

    def test_processar_video_monta_ffmpeg_sem_executar_binario_real(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            origem = root / "in.mp4"
            destino = root / "out.mp4"
            origem.write_bytes(b"a" * 100)
            ajustes, meta = self._patches_basicos()
            comandos = []

            class FakeProc:
                def __init__(self, cmd, **kwargs):
                    comandos.append(cmd)
                    Path(cmd[-1]).write_bytes(b"b" * 120)
                    self.stdout = iter([])
                def wait(self):
                    return 0

            with patch.object(limpeza, "obter_info_video", return_value={"width": 720, "height": 1280, "has_audio": True, "fps": 30.0, "duration": 10.0}), \
                 patch.object(limpeza, "gerar_ajustes_visuais", return_value=ajustes), \
                 patch.object(limpeza, "gerar_metadados_smartphone", return_value=meta), \
                 patch.object(limpeza.random, "uniform", return_value=1.0), \
                 patch.object(limpeza.random, "randint", return_value=23), \
                 patch.object(limpeza.subprocess, "Popen", side_effect=FakeProc):
                ok = limpeza.processar_video(origem, destino, threads_por_video=3)

            self.assertTrue(ok)
            self.assertEqual(1, len(comandos))
            cmd = comandos[0]
            self.assertEqual("ffmpeg", cmd[0])
            self.assertIn("-map_metadata", cmd)
            self.assertIn("-vf", cmd)
            self.assertIn("-af", cmd)
            self.assertIn("3", cmd[cmd.index("-threads") + 1])
            self.assertEqual(str(destino), cmd[-1])

    def test_processar_video_remove_saida_parcial_quando_ffmpeg_falha(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            origem = root / "in.mp4"
            destino = root / "out.mp4"
            origem.write_bytes(b"a" * 100)
            ajustes, meta = self._patches_basicos()

            class FakeProc:
                def __init__(self, cmd, **kwargs):
                    Path(cmd[-1]).write_bytes(b"parcial")
                    self.stdout = iter(["erro simulado\n"])
                def wait(self):
                    return 1

            with patch.object(limpeza, "obter_info_video", return_value={"width": 720, "height": 1280, "has_audio": False, "fps": 30.0, "duration": 10.0}), \
                 patch.object(limpeza, "gerar_ajustes_visuais", return_value=ajustes), \
                 patch.object(limpeza, "gerar_metadados_smartphone", return_value=meta), \
                 patch.object(limpeza.random, "uniform", return_value=1.0), \
                 patch.object(limpeza.random, "randint", return_value=23), \
                 patch.object(limpeza.subprocess, "Popen", side_effect=FakeProc):
                ok = limpeza.processar_video(origem, destino)

            self.assertFalse(ok)
            self.assertFalse(destino.exists())


if __name__ == "__main__":
    unittest.main()
