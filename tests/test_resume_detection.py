from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from _sistema import limpar_metadados_oficial as limpeza


class ResumeAndAlreadyProcessedTests(unittest.TestCase):
    def test_item_done_com_saida_existente_nao_e_reprocessado(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            entrada = root / "originais"
            saida = root / "conta" / "videos"
            dados = root / "conta" / "dados"
            logs = root / "conta" / "logs"
            entrada.mkdir(parents=True)
            saida.mkdir(parents=True)
            dados.mkdir(parents=True)
            logs.mkdir(parents=True)

            origem = entrada / "video_original.mp4"
            origem.write_bytes(b"conteudo do video")
            fp = limpeza.fingerprint_origem(origem)
            output = saida / "001.mp4"
            output.write_bytes(b"resultado existente")
            state_file = dados / "limpeza_estado.json"
            state_file.write_text(json.dumps({
                "version": 1,
                "updated_at": None,
                "items": {
                    fp: {
                        "fingerprint": fp,
                        "source_name": origem.name,
                        "output": "001.mp4",
                        "status": "done",
                    }
                },
            }), encoding="utf-8")

            with patch.object(limpeza, "processar_video", side_effect=AssertionError("não deve reprocessar item done")) as proc:
                rc = limpeza.processar_pasta(entrada, saida, jobs=4, state_file=state_file, log_dir=logs)

            self.assertEqual(0, rc)
            proc.assert_not_called()
            st = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("done", st["items"][fp]["status"])
            self.assertEqual("001.mp4", st["items"][fp]["output"])

    def test_item_pending_reutiliza_numero_reservado(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            entrada = root / "originais"
            saida = root / "conta" / "videos"
            dados = root / "conta" / "dados"
            logs = root / "conta" / "logs"
            entrada.mkdir(parents=True)
            saida.mkdir(parents=True)
            dados.mkdir(parents=True)
            logs.mkdir(parents=True)

            origem = entrada / "original.mp4"
            origem.write_bytes(b"conteudo")
            fp = limpeza.fingerprint_origem(origem)
            state_file = dados / "limpeza_estado.json"
            state_file.write_text(json.dumps({
                "version": 1,
                "items": {fp: {"output": "007.mp4", "status": "pending"}},
                "updated_at": None,
            }), encoding="utf-8")
            chamados = []

            def fake_process(video, destino, *args, **kwargs):
                chamados.append(destino.name)
                destino.write_bytes(b"ok")
                return True

            with patch.object(limpeza, "processar_video", side_effect=fake_process):
                rc = limpeza.processar_pasta(entrada, saida, jobs=1, state_file=state_file, log_dir=logs)

            self.assertEqual(0, rc)
            self.assertEqual(["007.mp4"], chamados)
            st = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual("done", st["items"][fp]["status"])
            self.assertEqual("007.mp4", st["items"][fp]["output"])

    def test_duplicado_identico_no_mesmo_lote_e_processado_uma_vez(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            entrada = root / "originais"
            saida = root / "conta" / "videos"
            dados = root / "conta" / "dados"
            logs = root / "conta" / "logs"
            entrada.mkdir(parents=True)
            dados.mkdir(parents=True)
            logs.mkdir(parents=True)
            (entrada / "a.mp4").write_bytes(b"igual")
            (entrada / "b.mp4").write_bytes(b"igual")
            chamados = []

            def fake_process(video, destino, *args, **kwargs):
                chamados.append((video.name, destino.name))
                destino.write_bytes(b"ok")
                return True

            with patch.object(limpeza, "processar_video", side_effect=fake_process):
                rc = limpeza.processar_pasta(entrada, saida, jobs=2, state_file=dados / "limpeza_estado.json", log_dir=logs)

            self.assertEqual(0, rc)
            self.assertEqual(1, len(chamados))
            self.assertEqual("001.mp4", chamados[0][1])


if __name__ == "__main__":
    unittest.main()
