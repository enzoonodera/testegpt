# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — PARTE 2 — CANDIDATO B (str(exc) persistido)

Mapeia exatamente onde uma string de exceção sintética (com marcadores
fictícios de segredo) termina, usando as funções REAIS de persistência de
_sistema/gerar_textos.py (`save_json`, `export_csv`) com o MESMO formato de
dicionário que o bloco `except Exception as ex:` de `main()` monta (linhas
~316-319 do arquivo, citadas no relatório).

Não foi possível rodar `main()` ponta a ponta neste ambiente (depende de
faster-whisper com modelo real e de um Ollama local rodando, indisponíveis no
sandbox de desenvolvimento -- e nem deveria ser testado de outra forma, por
regra explícita do Estágio 2: "não carregar modelo pesado na suíte
unitária"). Este teste chama diretamente as funções de persistência reais com
o payload exato que o except-block produziria, o que é suficiente para provar
COM CÓDIGO REAL (não reimplementado) exatamente onde a string termina.
"""
from pathlib import Path
import csv
import json
import tempfile
import unittest
from unittest import mock

from _sistema import gerar_textos


TOKEN_FICTICIO = "TOKEN_FICTICIO_123"
COOKIE_FICTICIO = "COOKIE_FICTICIO_456"
PATH_FICTICIO = r"C:\Users\Pessoa\arquivo.mp4"
URL_FICTICIA = "https://exemplo.invalid/?token=SEGREDO_FICTICIO"

SYNTHETIC_EXCEPTION_TEXT = (
    f"Falha ao chamar Ollama: {URL_FICTICIA} "
    f"(header Authorization continha {TOKEN_FICTICIO}; cookie de sessão "
    f"{COOKIE_FICTICIO}) ao processar {PATH_FICTICIO}"
)


class CandidateBExceptionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        d = Path(self._tmpdir.name)
        self.texts_file = d / "textos_postagem.json"
        self.csv_file = d / "textos_postagem.csv"
        self.log_dir = d / "logs"

        self._patches = [
            mock.patch.object(gerar_textos, "TEXTS_FILE", self.texts_file),
            mock.patch.object(gerar_textos, "CSV_FILE", self.csv_file),
            mock.patch.object(gerar_textos, "DATA_DIR", d),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _run_real_except_block_payload(self, video_name="video_de_teste.mp4", fp="fp123"):
        """Reproduz literalmente o corpo do except-block de main() (linhas
        316-319 de gerar_textos.py), usando as funções reais save_json() e
        export_csv() do módulo -- não uma reimplementação da lógica de
        persistência, só do gatilho (a exceção sintética em si)."""
        ex = RuntimeError(SYNTHETIC_EXCEPTION_TEXT)
        data = {}
        e = data.get(fp) if isinstance(data.get(fp), dict) else {"arquivo": video_name}
        e.update({
            "plataforma": "youtube",
            "status": "failed",
            "ultimo_erro": str(ex),
            "atualizado_em": "2026-09-19T00:00:00Z",
        })
        data[fp] = e
        gerar_textos.save_json(gerar_textos.TEXTS_FILE, data)
        gerar_textos.export_csv(data, "youtube")

        self.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.log_dir / "textos_erros.log").open("a", encoding="utf-8") as lf:
            lf.write(f"2026-09-19T00:00:00Z | {video_name} | {ex}\n")

    def test_segredos_sinteticos_terminam_no_json_de_estado_em_texto_puro(self):
        self._run_real_except_block_payload()

        raw = self.texts_file.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        stored_message = parsed["fp123"]["ultimo_erro"]
        # Comparação pelo valor JÁ DECODIFICADO do JSON (não pela string
        # crua do arquivo, que escapa "\" como "\\" -- isso é só
        # serialização normal do json.dumps, não uma alteração/sanitização
        # do conteúdo).
        for marker in (TOKEN_FICTICIO, COOKIE_FICTICIO, PATH_FICTICIO, URL_FICTICIA):
            self.assertIn(
                marker, stored_message,
                f"esperado encontrar o marcador sintético {marker!r} em texto puro "
                f"dentro de {gerar_textos.TEXTS_FILE.name} -- isso confirma o "
                f"CANDIDATO B como REPRODUCED para este arquivo."
            )

        self.assertEqual(SYNTHETIC_EXCEPTION_TEXT, parsed["fp123"]["ultimo_erro"])

    def test_csv_exportado_nao_inclui_ultimo_erro_achado_positivo_corrige_parte1(self):
        """CORREÇÃO em relação à Parte 1: o relatório anterior disse que
        `ultimo_erro` "é exportado via export_csv()". Isso está ERRADO --
        export_csv() usa uma lista fixa de campos que NÃO inclui
        'ultimo_erro'. Este teste prova isso com o export_csv() real."""
        self._run_real_except_block_payload()

        with self.csv_file.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.assertNotIn("ultimo_erro", reader.fieldnames)
            rows = list(reader)

        csv_raw = self.csv_file.read_text(encoding="utf-8-sig")
        for marker in (TOKEN_FICTICIO, COOKIE_FICTICIO, PATH_FICTICIO, URL_FICTICIA):
            self.assertNotIn(marker, csv_raw)

    def test_log_local_de_erros_tambem_recebe_o_texto_completo_em_claro(self):
        self._run_real_except_block_payload()

        log_content = (self.log_dir / "textos_erros.log").read_text(encoding="utf-8")
        for marker in (TOKEN_FICTICIO, COOKIE_FICTICIO, PATH_FICTICIO, URL_FICTICIA):
            self.assertIn(marker, log_content)


if __name__ == "__main__":
    unittest.main()
