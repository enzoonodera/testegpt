# -*- coding: utf-8 -*-
"""
GATE 19.5 — ESTÁGIO 2 — PARTE 2 — PRIORIDADE 5 (gerar_textos.py / Ollama)

Testes adversariais determinísticos para generate_text()/ollama_chat(), sem
nenhuma dependência de rede ou de um Ollama real -- tudo via mock de
urllib.request.urlopen, chamando as funções REAIS do módulo.

Cobre um subconjunto representativo dos 17 cenários A-Q pedidos na Parte 1 do
Estágio 2 (não os 17 inteiros -- ver GATE_19_5_ESTAGIO2_RELATORIO.md pela
lista do que ainda falta). Objetivo central, citado explicitamente pelo
Estágio 2: "não transformar uma resposta inválida em falso SUCESSO".
"""
import io
import json
import unittest
import urllib.error
from unittest import mock

from _sistema import gerar_textos as gt


def fake_response(body_dict):
    body = json.dumps(body_dict).encode("utf-8")
    return io.BytesIO(body)


class _CM:
    """Context manager mínimo simulando o retorno de urlopen()."""
    def __init__(self, fh):
        self._fh = fh

    def __enter__(self):
        return self._fh

    def __exit__(self, *a):
        return False


class OllamaGenerateTextAdversarialTests(unittest.TestCase):
    def _mock_chat_response(self, content_str):
        """Simula o envelope real de /api/chat que ollama_chat() devolve."""
        return fake_response({"message": {"content": content_str}})

    def test_A_resposta_normal_youtube(self):
        content = json.dumps({
            "title": "Como fazer isso rápido",
            "description": "Um jeito simples de resolver o problema em poucos passos.",
            "hashtags": ["#Shorts", "#dicas", "#tutorial", "#rapido"],
        })
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(content))):
            result = gt.generate_text("http://localhost:11434", "llama3.2", "algum transcript", "youtube", "pt-BR", "Brasil")
        self.assertEqual("Como fazer isso rápido", result["titulo"])
        self.assertTrue(result["descricao"])
        self.assertEqual(4, len(result["hashtags"]))

    def test_B_ollama_offline_connection_refused(self):
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("offline")):
            with self.assertRaises(ConnectionRefusedError):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")

    def test_C_timeout(self):
        import socket
        with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(socket.timeout):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")

    def test_D_http_500_vira_runtimeerror_com_corpo_truncado(self):
        err = urllib.error.HTTPError(
            url="http://localhost:11434/api/chat", code=500, msg="Internal Server Error",
            hdrs=None, fp=io.BytesIO(b"algum corpo de erro do servidor"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")
        self.assertIn("500", str(ctx.exception))

    def test_E_json_invalido_nao_vira_sucesso_falso(self):
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response("isto nao e json"))):
            with self.assertRaises(RuntimeError):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")

    def test_F_resposta_vazia_nao_vira_sucesso_falso(self):
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(""))):
            with self.assertRaises(RuntimeError):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")

    def test_G_resposta_so_espacos_nao_vira_sucesso_falso(self):
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response("    \n\t  "))):
            with self.assertRaises(RuntimeError):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")

    def test_H_titulo_faltando_youtube_levanta_erro_explicito(self):
        content = json.dumps({"description": "so descricao", "hashtags": ["#a", "#b", "#c", "#d"]})
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(content))):
            with self.assertRaises(RuntimeError):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")

    def test_I_descricao_faltando_youtube_levanta_erro_explicito(self):
        content = json.dumps({"title": "so titulo", "hashtags": ["#a", "#b", "#c", "#d"]})
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(content))):
            with self.assertRaises(RuntimeError):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")

    def test_J_caption_faltando_tiktok_levanta_erro_explicito(self):
        content = json.dumps({"hashtags": ["#a", "#b", "#c", "#d"]})
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(content))):
            with self.assertRaises(RuntimeError):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "tiktok", "en-US", "USA")

    def test_K_unicode_ptbr_preservado(self):
        content = json.dumps({
            "title": "Segredo revelado em minutos",
            "description": "Ela não esperava aprender isso hoje à noite, mas funcionou muito bem.",
            "hashtags": ["#Shorts", "#dicas", "#viral", "#curiosidades"],
        }, ensure_ascii=False)
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(content))):
            result = gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "pt-BR", "Brasil")
        self.assertIn("não", result["descricao"])
        self.assertIn("à", result["descricao"])

    def test_L_resposta_com_lixo_ao_redor_do_json_usa_fallback_regex(self):
        content = "Aqui está o resultado: " + json.dumps({
            "caption": "Olha isso!", "hashtags": ["#fyp", "#viral", "#foryou", "#trending"],
        }) + " -- espero que ajude!"
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(content))):
            result = gt.generate_text("http://localhost:11434", "llama3.2", "x", "tiktok", "en-US", "USA")
        self.assertEqual("Olha isso!", result["caption"])

    def test_M_hashtags_normalizadas_para_exatamente_quatro_e_shorts_presente_youtube(self):
        content = json.dumps({
            "title": "Titulo valido aqui",
            "description": "Descricao valida aqui.",
            "hashtags": ["semhashtag", "#Já#Com#Cerquilha", "#shorts"],
        })
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(content))):
            result = gt.generate_text("http://localhost:11434", "llama3.2", "x", "youtube", "en-US", "USA")
        self.assertEqual(4, len(result["hashtags"]))
        self.assertTrue(any(x.lower() == "#shorts" for x in result["hashtags"]))
        # Achado de comportamento real (não bug): clean_tags() só MOVE
        # #Shorts para o início quando ele está AUSENTE da lista da IA; se a
        # IA já devolveu #shorts em outra posição, a ordem original é
        # preservada.
        self.assertEqual("#semhashtag", result["hashtags"][0].lower())

    def test_N_resposta_nao_reaproveita_resultado_anterior_apos_falha(self):
        """Requisito explícito do Estágio 2: nenhum resultado anterior deve
        ser reutilizado silenciosamente depois de uma falha. generate_text()
        em si não tem estado entre chamadas -- cada chamada recomeça do
        zero -- então uma resposta inválida nunca pode "herdar" um resultado
        anterior válido."""
        good = json.dumps({"caption": "Bom resultado", "hashtags": ["#a", "#b", "#c", "#d"]})
        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(good))):
            first = gt.generate_text("http://localhost:11434", "llama3.2", "x", "tiktok", "en-US", "USA")
        self.assertEqual("Bom resultado", first["caption"])

        with mock.patch("urllib.request.urlopen", return_value=_CM(self._mock_chat_response(""))):
            with self.assertRaises(RuntimeError):
                gt.generate_text("http://localhost:11434", "llama3.2", "x", "tiktok", "en-US", "USA")
        # Não há valor de retorno na chamada que falhou -- RuntimeError é a
        # única saída possível, provando que não existe caminho de código
        # que devolva `first` (ou qualquer cache) disfarçado de novo sucesso.


if __name__ == "__main__":
    unittest.main()
