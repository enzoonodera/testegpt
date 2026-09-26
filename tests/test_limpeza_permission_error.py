# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — PARTE 2 — CANDIDATO A (PermissionError na limpeza de vídeo)

A Parte 1 apontou, por leitura de código, que `processar_pasta()` em
_sistema/limpar_metadados_oficial.py chama `saida.mkdir(parents=True,
exist_ok=True)` sem tratamento de PermissionError, e classificou isso
como "candidato", explicitamente sem assumir severidade.

Este teste reproduz de forma determinística: monkeypatcha Path.mkdir para
levantar PermissionError exatamente no ponto identificado e observa o
comportamento real do programa.
"""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _sistema import limpar_metadados_oficial as limpeza


class PermissionErrorOnOutputMkdirTests(unittest.TestCase):
    def test_permission_error_ao_criar_pasta_de_saida_propaga_sem_tratamento(self):
        with tempfile.TemporaryDirectory() as d:
            entrada = Path(d) / "entrada"
            entrada.mkdir()
            (entrada / "video1.mp4").write_bytes(b"fake video bytes")
            saida = Path(d) / "saida_sem_permissao"

            original_mkdir = Path.mkdir

            def failing_mkdir(self, *a, **kw):
                if self == saida:
                    raise PermissionError(13, "Permission denied", str(saida))
                return original_mkdir(self, *a, **kw)

            with mock.patch.object(Path, "mkdir", new=failing_mkdir):
                with self.assertRaises(PermissionError):
                    limpeza.processar_pasta(entrada, saida, jobs=1)

            # PROVA do achado da Parte 1: o programa não captura o erro, não
            # grava nenhum estado/log amigável, e não deixa rastro de
            # diagnóstico para o usuário -- ele simplesmente propaga a
            # exceção crua. Isso classifica o CANDIDATO A como REPRODUCED.
            self.assertFalse(saida.exists())
            log_padrao = saida.parent / "dados" / "limpeza_estado.json"
            self.assertFalse(log_padrao.exists())

    def test_video_original_nunca_e_tocado_quando_saida_falha_ao_criar(self):
        """Verificação complementar: mesmo neste caminho de falha, o vídeo de
        entrada permanece intacto -- FFmpeg nunca chega a ser invocado
        porque a falha ocorre antes de qualquer processamento."""
        with tempfile.TemporaryDirectory() as d:
            entrada = Path(d) / "entrada"
            entrada.mkdir()
            original_bytes = b"fake video bytes - nao deve mudar"
            video = entrada / "video1.mp4"
            video.write_bytes(original_bytes)
            saida = Path(d) / "saida_sem_permissao"

            original_mkdir = Path.mkdir

            def failing_mkdir(self, *a, **kw):
                if self == saida:
                    raise PermissionError(13, "Permission denied", str(saida))
                return original_mkdir(self, *a, **kw)

            with mock.patch.object(Path, "mkdir", new=failing_mkdir):
                with self.assertRaises(PermissionError):
                    limpeza.processar_pasta(entrada, saida, jobs=1)

            self.assertEqual(original_bytes, video.read_bytes())


if __name__ == "__main__":
    unittest.main()
