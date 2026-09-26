# -*- coding: utf-8 -*-
"""PROMPT 26 -- Media Probe: testes.

Usa ffmpeg/ffprobe DE VERDADE quando disponível no ambiente (gerando
vídeos pequenos localmente, sem internet) -- marcado com
``@pytest.mark.skipif`` quando ausente, nunca substituído por mocks
silenciosos (mesma lição já aplicada ao GATE 3 do Prompt 24b: uma exceção
capturada/um mock que nunca prova a integração real não é teste honesto).
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import _sistema.media_probe as media_probe
from _sistema.media_probe import (
    ERRO_ARQUIVO_INEXISTENTE,
    ERRO_ARQUIVO_VAZIO,
    ERRO_DECODE_FALHOU,
    ERRO_DURACAO_INVALIDA,
    ERRO_FFMPEG_AUSENTE,
    ERRO_FFPROBE_AUSENTE,
    ERRO_FFPROBE_FALHOU,
    ERRO_NAO_E_ARQUIVO_REGULAR,
    ERRO_RESOLUCAO_INVALIDA,
    ERRO_SAIDA_JSON_INVALIDA,
    ERRO_SEM_STREAM_VIDEO,
    ERRO_TIMEOUT,
    MediaProbe,
    MediaProbeResult,
)

FFMPEG_DISPONIVEL = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_SKIP_REASON = "ffmpeg/ffprobe nao encontrados no PATH deste ambiente -- teste de integracao real pulado"


# ---------------------------------------------------------------------------
# Helpers de geração de vídeo real (só usados quando ffmpeg está presente)
# ---------------------------------------------------------------------------


def _gerar_video_valido(path: Path, *, duration: int = 1, size: str = "64x64", rate: int = 10) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _gerar_video_sem_audio(path: Path, *, duration: int = 1) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:a", "aac", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _truncar_bytes_finais(origem: Path, destino: Path, *, n_bytes: int) -> None:
    data = origem.read_bytes()
    destino.write_bytes(data[:-n_bytes])


def _corromper_bytes_do_meio(origem: Path, destino: Path) -> None:
    data = bytearray(origem.read_bytes())
    start, end = len(data) // 3, (2 * len(data)) // 3
    for i in range(start, end):
        data[i] = 0
    destino.write_bytes(bytes(data))


@pytest.fixture
def probe() -> MediaProbe:
    return MediaProbe()


# ---------------------------------------------------------------------------
# 1. Integração real -- ffmpeg/ffprobe de verdade
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_probe_video_real_campos_batem_com_geracao(probe, tmp_path):
    video = tmp_path / "valido.mp4"
    _gerar_video_valido(video, duration=1, size="64x64", rate=10)

    result = probe.probe(video)

    assert result.valid is True
    assert result.error_code is None
    assert result.width == 64
    assert result.height == 64
    assert result.fps == pytest.approx(10.0, abs=0.01)
    assert result.duration == pytest.approx(1.0, abs=0.05)
    assert result.codec == "h264"
    assert "mov" in (result.container or "") or "mp4" in (result.container or "")
    assert result.has_audio is True
    assert result.audio_stream_count == 1
    assert result.file_size == video.stat().st_size
    assert result.deep_checked is False


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_probe_deep_video_real_valido_ok(probe, tmp_path):
    video = tmp_path / "valido.mp4"
    _gerar_video_valido(video)

    result = probe.probe_deep(video)

    assert result.valid is True
    assert result.deep_checked is True
    assert result.error_code is None


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_probe_video_sem_stream_de_video(probe, tmp_path):
    audio_only = tmp_path / "audio.m4a"
    _gerar_video_sem_audio(audio_only)

    result = probe.probe(audio_only)

    assert result.valid is False
    assert result.error_code == ERRO_SEM_STREAM_VIDEO


# ---------------------------------------------------------------------------
# 2. Corrupção real -- não simulada
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_corrupcao_estrutural_truncamento_detectada_pela_verificacao_rasa(probe, tmp_path):
    """Truncar o fim do arquivo quebra o container (moov atom) -- corrupção
    ESTRUTURAL, detectada até pela verificação rasa (só ffprobe)."""
    original = tmp_path / "original.mp4"
    _gerar_video_valido(original)
    corrupto = tmp_path / "truncado.mp4"
    _truncar_bytes_finais(original, corrupto, n_bytes=2000)

    result = probe.probe(corrupto)

    assert result.valid is False
    assert result.error_code == ERRO_FFPROBE_FALHOU


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_corrupcao_de_frame_no_meio_nao_e_detectada_pela_verificacao_rasa_mas_e_pela_profunda(probe, tmp_path):
    """LIMITAÇÃO CONHECIDA E DOCUMENTADA (seção 1.5 do módulo): corrupção de
    dados de frame no MEIO do arquivo, com o container estruturalmente
    íntegro, passa despercebida pela verificação rasa (só ffprobe lê
    metadados) -- só a verificação profunda (decode real via ffmpeg)
    detecta. Este teste prova as DUAS metades dessa afirmação com o MESMO
    arquivo corrompido de verdade."""
    original = tmp_path / "original.mp4"
    _gerar_video_valido(original)
    corrupto = tmp_path / "corrompido_no_meio.mp4"
    _corromper_bytes_do_meio(original, corrupto)

    shallow = probe.probe(corrupto)
    assert shallow.valid is True, (
        "limitacao esperada: verificacao rasa nao detecta corrupcao de frame "
        "com container integro"
    )

    deep = probe.probe_deep(corrupto)
    assert deep.valid is False
    assert deep.error_code == ERRO_DECODE_FALHOU


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_probe_deep_arquivo_com_falha_rasa_nao_tenta_decodificar(probe, tmp_path):
    """``probe_deep`` roda ``probe()`` primeiro e falha rápido -- nunca
    tenta o decode caro num arquivo que já falhou na checagem rasa."""
    original = tmp_path / "original.mp4"
    _gerar_video_valido(original)
    corrupto = tmp_path / "truncado.mp4"
    _truncar_bytes_finais(original, corrupto, n_bytes=2000)

    result = probe.probe_deep(corrupto)

    assert result.valid is False
    assert result.error_code == ERRO_FFPROBE_FALHOU
    assert result.deep_checked is False


# ---------------------------------------------------------------------------
# 3. Validação de nível-arquivo (sem depender de ffmpeg estar instalado)
# ---------------------------------------------------------------------------


def test_arquivo_inexistente(probe, tmp_path):
    result = probe.probe(tmp_path / "nao_existe.mp4")
    assert result.valid is False
    assert result.error_code == ERRO_ARQUIVO_INEXISTENTE


def test_arquivo_vazio(probe, tmp_path):
    vazio = tmp_path / "vazio.mp4"
    vazio.write_bytes(b"")
    result = probe.probe(vazio)
    assert result.valid is False
    assert result.error_code == ERRO_ARQUIVO_VAZIO


def test_caminho_e_diretorio_nao_arquivo_regular(probe, tmp_path):
    pasta = tmp_path / "uma_pasta"
    pasta.mkdir()
    result = probe.probe(pasta)
    assert result.valid is False
    assert result.error_code == ERRO_NAO_E_ARQUIVO_REGULAR


def test_probe_nao_restringe_por_extensao_arquivo_generico_ainda_tenta_ffprobe(probe, tmp_path):
    """Diferente de ``source_import._validate_file_for_import``,
    ``MediaProbe`` NÃO restringe por ``VIDEO_EXTS`` -- pode ser chamado
    independentemente da importação (item 0.3). Um arquivo de conteúdo
    qualquer com extensão ``.txt`` ainda deve chegar até o ffprobe (e
    falhar lá como conteúdo inválido, não por causa da extensão)."""
    arquivo_generico = tmp_path / "nao_e_video.txt"
    arquivo_generico.write_bytes(b"isto claramente nao e um arquivo de video valido")
    result = probe.probe(arquivo_generico)
    assert result.valid is False
    # Nunca um erro de "extensao invalida" -- este modulo nao tem esse
    # conceito. O erro vem do ffprobe genuinamente tentando e falhando.
    assert result.error_code in (ERRO_FFPROBE_FALHOU, ERRO_SAIDA_JSON_INVALIDA)


# ---------------------------------------------------------------------------
# 4. Ambiente sem ffprobe/ffmpeg -- distinto de arquivo inválido
# ---------------------------------------------------------------------------


def test_ffprobe_ausente_do_path_codigo_dedicado(probe, tmp_path, monkeypatch):
    arquivo = tmp_path / "qualquer.mp4"
    arquivo.write_bytes(b"conteudo qualquer, nao importa para este teste")

    monkeypatch.setattr(media_probe.shutil, "which", lambda _name: None)

    result = probe.probe(arquivo)

    assert result.valid is False
    assert result.error_code == ERRO_FFPROBE_AUSENTE
    # Nunca confundido com um codigo de arquivo invalido.
    assert result.error_code not in (
        ERRO_FFPROBE_FALHOU, ERRO_SAIDA_JSON_INVALIDA, ERRO_SEM_STREAM_VIDEO,
    )


def test_ffmpeg_ausente_apenas_afeta_probe_deep_nunca_probe_raso(probe, tmp_path, monkeypatch):
    """Ausência de ``ffmpeg`` (usado só por ``probe_deep``) nunca deve
    afetar ``probe()`` raso, que usa exclusivamente ``ffprobe``."""
    if not FFMPEG_DISPONIVEL:
        pytest.skip(_SKIP_REASON)
    video = tmp_path / "valido.mp4"
    _gerar_video_valido(video)

    original_which = shutil.which

    def _fake_which(name):
        if name == "ffmpeg":
            return None
        return original_which(name)

    monkeypatch.setattr(media_probe.shutil, "which", _fake_which)

    shallow = probe.probe(video)
    assert shallow.valid is True

    deep = probe.probe_deep(video)
    assert deep.valid is False
    assert deep.error_code == ERRO_FFMPEG_AUSENTE


# ---------------------------------------------------------------------------
# 5. Timeout -- real e mockado, nunca uma exceção crua escapando
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_timeout_real_baixissimo_contra_arquivo_real_levanta_e_e_capturado(tmp_path):
    """Timeout REAL (não mockado): ``subprocess.TimeoutExpired``
    genuinamente levantado por um timeout extremamente baixo contra um
    ffprobe de verdade, confirmando que ``subprocess.run(timeout=...)``
    de fato mata o processo (comportamento da stdlib, confirmado aqui em
    vez de apenas assumido)."""
    video = tmp_path / "valido.mp4"
    _gerar_video_valido(video)

    probe_com_timeout_baixo = MediaProbe(ffprobe_timeout=0.0000001)
    result = probe_com_timeout_baixo.probe(video)

    assert result.valid is False
    assert result.error_code == ERRO_TIMEOUT


def test_timeout_mockado_no_probe_raso(probe, tmp_path, monkeypatch):
    arquivo = tmp_path / "qualquer.mp4"
    arquivo.write_bytes(b"conteudo qualquer")

    def _fake_run(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(media_probe.subprocess, "run", _fake_run)

    result = probe.probe(arquivo)
    assert result.valid is False
    assert result.error_code == ERRO_TIMEOUT


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_timeout_mockado_no_probe_deep(probe, tmp_path, monkeypatch):
    video = tmp_path / "valido.mp4"
    _gerar_video_valido(video)

    original_run = subprocess.run

    def _fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg":
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=kwargs.get("timeout", 1))
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(media_probe.subprocess, "run", _fake_run)

    result = probe.probe_deep(video)
    assert result.valid is False
    assert result.error_code == ERRO_TIMEOUT


# ---------------------------------------------------------------------------
# 6. ffprobe retorna código != 0 / JSON inválido / campos inválidos (mockado)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ffprobe_saida_nao_e_json_valido(probe, tmp_path, monkeypatch):
    arquivo = tmp_path / "qualquer.mp4"
    arquivo.write_bytes(b"x")

    monkeypatch.setattr(
        media_probe.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout="isto nao e json{{{"),
    )

    result = probe.probe(arquivo)
    assert result.valid is False
    assert result.error_code == ERRO_SAIDA_JSON_INVALIDA


def test_ffprobe_codigo_saida_diferente_de_zero(probe, tmp_path, monkeypatch):
    arquivo = tmp_path / "qualquer.mp4"
    arquivo.write_bytes(b"x")

    monkeypatch.setattr(
        media_probe.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(1, stdout="", stderr="erro qualquer"),
    )

    result = probe.probe(arquivo)
    assert result.valid is False
    assert result.error_code == ERRO_FFPROBE_FALHOU


def test_resolucao_invalida_largura_zero(probe, tmp_path, monkeypatch):
    arquivo = tmp_path / "qualquer.mp4"
    arquivo.write_bytes(b"x")

    payload = {
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 0, "height": 64, "duration": "1.0"}],
        "format": {"format_name": "mov,mp4"},
    }
    monkeypatch.setattr(
        media_probe.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=json.dumps(payload)),
    )

    result = probe.probe(arquivo)
    assert result.valid is False
    assert result.error_code == ERRO_RESOLUCAO_INVALIDA


def test_duracao_invalida_zero(probe, tmp_path, monkeypatch):
    arquivo = tmp_path / "qualquer.mp4"
    arquivo.write_bytes(b"x")

    payload = {
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 64, "height": 64, "duration": "0"}],
        "format": {"format_name": "mov,mp4", "duration": "0"},
    }
    monkeypatch.setattr(
        media_probe.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=json.dumps(payload)),
    )

    result = probe.probe(arquivo)
    assert result.valid is False
    assert result.error_code == ERRO_DURACAO_INVALIDA


def test_duracao_nao_numerica(probe, tmp_path, monkeypatch):
    arquivo = tmp_path / "qualquer.mp4"
    arquivo.write_bytes(b"x")

    payload = {
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 64, "height": 64, "duration": "nao-e-numero"}],
        "format": {"format_name": "mov,mp4"},
    }
    monkeypatch.setattr(
        media_probe.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=json.dumps(payload)),
    )

    result = probe.probe(arquivo)
    assert result.valid is False
    assert result.error_code == ERRO_DURACAO_INVALIDA


# ---------------------------------------------------------------------------
# 7. Lote -- isolamento por item
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_probe_batch_isolamento_arquivo_invalido_nao_afeta_os_demais(probe, tmp_path):
    valido1 = tmp_path / "valido1.mp4"
    valido2 = tmp_path / "valido2.mp4"
    _gerar_video_valido(valido1)
    _gerar_video_valido(valido2)
    inexistente = tmp_path / "nao_existe.mp4"

    results = probe.probe_batch([valido1, inexistente, valido2])

    assert len(results) == 3
    assert results[0].valid is True
    assert results[1].valid is False
    assert results[1].error_code == ERRO_ARQUIVO_INEXISTENTE
    assert results[2].valid is True


def test_probe_batch_lista_vazia(probe):
    assert probe.probe_batch([]) == []


def test_probe_batch_isolamento_falha_inesperada_de_um_item(probe, tmp_path, monkeypatch):
    """Isolamento por item mesmo quando ``probe()`` em si lança algo
    verdadeiramente inesperado (defesa em profundidade além do try/except
    interno de ``probe()``)."""
    arquivo1 = tmp_path / "a.mp4"
    arquivo2 = tmp_path / "b.mp4"
    arquivo1.write_bytes(b"x")
    arquivo2.write_bytes(b"y")

    original_probe = MediaProbe.probe
    calls = {"count": 0}

    def _probe_quebra_no_primeiro(self, file_path):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("falha simulada verdadeiramente inesperada")
        return original_probe(self, file_path)

    monkeypatch.setattr(MediaProbe, "probe", _probe_quebra_no_primeiro)

    results = probe.probe_batch([arquivo1, arquivo2])

    assert len(results) == 2
    assert results[0].valid is False
    assert results[0].error_code == media_probe.ERRO_INESPERADO
    # segundo item processado normalmente, isolado da falha do primeiro
    assert results[1].error_code != media_probe.ERRO_INESPERADO


# ---------------------------------------------------------------------------
# 8. Garantias estruturais (AST)
# ---------------------------------------------------------------------------


def test_media_probe_nao_importa_modulos_protegidos():
    tree = ast.parse(Path(media_probe.__file__).read_text(encoding="utf-8"))
    proibidos = (
        "circuit_breaker",
        "retry_policy",
        "publication_idempotency",
        "secrets_manager",
        "job_state_machine",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for termo in proibidos:
            assert termo not in names.lower()


def test_media_probe_nao_importa_nada_de_geracao_1():
    tree = ast.parse(Path(media_probe.__file__).read_text(encoding="utf-8"))
    proibidos = (
        "agendar_youtube",
        "agendar_tiktok",
        "limpar_metadados_oficial",
        "gerar_textos",
        "painel_oficial",
        "login_conta",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        for termo in proibidos:
            assert termo not in names.lower()


def test_media_probe_nao_importa_storage_database():
    """Item 0.3: MediaProbe nao tem acesso a banco de dados."""
    tree = ast.parse(Path(media_probe.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        assert "storage.database" not in names
        assert "LocalDatabase" not in names


def test_nenhum_job_publication_schedule_project_ou_video_criado():
    source = Path(media_probe.__file__).read_text(encoding="utf-8")
    for termo in ("Job(", "Publication(", "Schedule(", "Project(", "Video("):
        assert termo not in source


# ---------------------------------------------------------------------------
# 9. MediaProbe NÃO é chamado automaticamente pelos importadores (seção 0.2)
# ---------------------------------------------------------------------------


def test_media_probe_nao_e_chamado_automaticamente_pelos_importadores():
    """Garantia por ausência, verificada em código real (AST) -- a
    docstring de ``source_import.py`` CITA propositalmente "MediaProbe"
    em prosa (referenciando o Prompt 26 como trabalho futuro fora de
    escopo do Prompt 24), então um grep textual ingênuo colidiria com a
    própria documentação (mesma lição já aplicada nos testes equivalentes
    dos Prompts 24b/25). Aqui a checagem inspeciona apenas nós AST reais
    de import e de chamada."""
    import _sistema.source_import as source_import

    tree = ast.parse(Path(source_import.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
        else:
            continue
        assert "media_probe" not in names.lower()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in ("MediaProbe", "probe", "probe_deep", "probe_batch")


# ---------------------------------------------------------------------------
# 10. Construção -- validação de parâmetros
# ---------------------------------------------------------------------------


def test_media_probe_rejeita_timeout_nao_positivo():
    with pytest.raises(ValueError):
        MediaProbe(ffprobe_timeout=0)
    with pytest.raises(ValueError):
        MediaProbe(ffmpeg_decode_timeout=-1)


def test_media_probe_result_e_dataclass_imutavel():
    result = MediaProbeResult(path="x", valid=True)
    with pytest.raises(Exception):
        result.valid = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 11. GATE adversarial -- "o que acontece se duas instâncias chamarem isto
# ao mesmo tempo?" (item 14 do CLAUDE.md). MediaProbe não tem nenhum estado
# mutável compartilhado (não persiste nada, cada chamada spawna seu próprio
# subprocesso independente) -- duas threads reais confirmam, de forma
# determinística (sem depender de timing/sorte), que não há interferência.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FFMPEG_DISPONIVEL, reason=_SKIP_REASON)
def test_duas_instancias_concorrentes_fazendo_probe_do_mesmo_arquivo(tmp_path):
    import threading

    video = tmp_path / "concorrente.mp4"
    _gerar_video_valido(video)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    results: list[MediaProbeResult] = []
    lock = threading.Lock()

    def worker():
        local_probe = MediaProbe()
        barrier.wait(timeout=5)
        try:
            result = local_probe.probe(video)
            with lock:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - só se algo quebrar
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    assert len(results) == 2
    assert all(r.valid for r in results)
    assert all(r.width == 64 and r.height == 64 for r in results)
