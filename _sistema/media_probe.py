# -*- coding: utf-8 -*-
"""PROMPT 26 -- Media Probe: validação de mídia via ffprobe/ffmpeg.

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie MediaProbe usando ffprobe ou equivalente.

    Antes de processar verificar:
    container
    codec
    duration
    FPS
    resolution
    audio streams
    corruption
    file size

    Arquivos inválidos devem gerar erro estruturado e não derrubar o lote.

===========================================================================
0.1 -- PRECEDENTE DE GERAÇÃO 1 E AS DUAS LACUNAS CORRIGIDAS AQUI
===========================================================================

``_sistema/limpar_metadados_oficial.py`` (``checar_ffmpeg()``,
``obter_info_video()``) já tem um precedente real e funcional de uso de
ffprobe: detecta o binário via ``shutil.which``, invoca
``subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
"-show_streams", "-show_format", str(caminho)], ...)``, faz parse do JSON
e extrai resolução/fps/duração/presença de áudio. A lógica de invocação e
parsing deste módulo é REPRODUZIDA de forma independente a partir dessa
referência (mesmo espírito de ``VIDEO_EXTS``/``fingerprint`` no Prompt 24)
-- NUNCA importada (zero cross-import já estabelecido).

Duas lacunas reais desse precedente, corrigidas conscientemente aqui:

(a) ``obter_info_video()`` não usa nenhum ``timeout`` -- um arquivo
    corrompido de forma patológica pode travar ``ffprobe`` indefinidamente.
    Este módulo SEMPRE passa ``timeout=`` explícito a todo
    ``subprocess.run`` (``_FFPROBE_TIMEOUT_SECONDS_PADRAO``/
    ``_FFMPEG_DECODE_TIMEOUT_SECONDS_PADRAO``), e um estouro de timeout
    devolve um código de erro DEDICADO (``ERRO_TIMEOUT``), confirmado por
    teste que o timeout é real (``subprocess.TimeoutExpired`` efetivamente
    levantado por um timeout extremamente baixo contra um arquivo real --
    nunca apenas mockado/assumido).

(b) ``obter_info_video()`` devolve ``None`` em QUALQUER falha, sem
    diferenciar "arquivo inválido/corrompido" (problema do ARQUIVO) de
    "ffprobe/ffmpeg não está instalado nesta máquina" (problema do
    AMBIENTE -- relevante para um produto distribuído a 1000+ máquinas
    onde nem toda instalação vai ter FFmpeg no PATH). Este módulo usa
    ``ERRO_FFPROBE_AUSENTE``/``ERRO_FFMPEG_AUSENTE`` (verificados via
    ``shutil.which`` ANTES de qualquer tentativa de invocação) como
    códigos estruturalmente distintos de qualquer código de "arquivo
    inválido" -- nunca confundidos.

===========================================================================
0.2 -- DECISÃO CENTRAL: SEM INTEGRAÇÃO AUTOMÁTICA AO FLUXO DE IMPORT
===========================================================================

Diferente de ``SourceContextResolver`` (Prompt 25, ligado automaticamente
a toda importação porque é barato -- só parsing de string), ``MediaProbe``
NUNCA é disparado automaticamente por ``LocalFileImporter``/
``FolderImporter``/``UrlImporter``. Justificativa, confrontando
explicitamente o argumento de custo:

- ``ffprobe``/``ffmpeg`` são chamadas de PROCESSO EXTERNO com custo real
  (spawn de subprocesso, leitura/parse de metadados, potencialmente
  segundos por arquivo -- e a verificação PROFUNDA opcional, seção 1.5,
  decodifica o arquivo inteiro). Rodar isso automaticamente para TODO
  arquivo de um ``FolderImporter`` processando centenas/milhares de
  vídeos imporia um custo de tempo NÃO SOLICITADO a uma operação que hoje
  é rápida (referenciar/copiar um arquivo, ver ``source_import.py``) --
  violaria a expectativa de desempenho já estabelecida e testada nos
  Prompts 24/24b sem necessidade demonstrada.
- O próprio roadmap fala em "**antes de processar** verificar" -- leitura
  natural: a validação profunda de mídia é um PORTÃO antes de uma etapa de
  PROCESSAMENTO real (Fase 5/Editor, ainda não implementada -- Prompt 27
  em diante), não um passo do momento de IMPORTAÇÃO em si. Uma importação
  bem-sucedida (``SourceAsset`` persistido) e uma validação de mídia bem-
  sucedida são preocupações ortogonais neste momento do produto.
- Consequência prática: ``MediaProbe`` é um módulo INDEPENDENTE, sem
  acesso a banco de dados (seção 0.3), chamável SOB DEMANDA por qualquer
  camada futura (por ``SourceAsset.local_path``/qualquer ``Path``) antes
  de começar a trabalhar num arquivo -- nunca amarrado ao ciclo de vida de
  importação. Testado explicitamente (``test_media_probe_nao_e_chamado_
  automaticamente_pelos_importadores``) que nenhum dos três importadores
  referencia ``MediaProbe``/``media_probe`` em seu código-fonte.

===========================================================================
0.3 -- SEM ACESSO A BANCO DE DADOS
===========================================================================

``MediaProbe`` opera sobre um ``Path`` (caminho de arquivo), nunca sobre
uma entidade persistida -- mantém o módulo testável sem fixtures de banco
e reutilizável tanto para ``SourceAsset.local_path`` quanto para qualquer
arquivo futuro fora desse fluxo. Confirmado: nenhum import de
``storage.database``/``LocalDatabase`` neste módulo (verificado por AST).

===========================================================================
1.5 -- PROFUNDIDADE DE VERIFICAÇÃO DE CORRUPÇÃO (decisão + justificativa)
===========================================================================

``ffprobe`` (``-show_streams``/``-show_format``) só lê METADADOS do
container -- rápido, mas não detecta corrupção de dados de frame no meio
do arquivo enquanto o container permanecer estruturalmente íntegro
(confirmado empiricamente nesta rodada: um arquivo MP4 válido com um terço
do meio sobrescrito com zeros continua produzindo ``ffprobe`` com
``returncode=0`` e um JSON de streams/format completo e aparentemente
normal).

DECISÃO: as DUAS opções descritas pelo Prompt são implementadas, nenhuma
delas escondendo a outra:

- **``probe()`` -- verificação RASA (opção "a"), é o caminho PADRÃO.**
  Só ``ffprobe`` (metadados). Rápido -- custo aceitável mesmo para uso
  ocasional em lote. Detecta corrupção ESTRUTURAL (container quebrado --
  ex. "moov atom not found" quando o fim do arquivo MP4 é truncado, que
  quebra o parsing do ffprobe e produz ``returncode != 0``), mas NÃO
  detecta corrupção de dados de frame no meio do arquivo com container
  íntegro -- limitação conhecida, documentada aqui e no relatório de
  entrega (dívida técnica).
- **``probe_deep()`` -- verificação PROFUNDA (opção "b"), OPCIONAL, nunca
  chamada por ``probe()`` nem por ``probe_batch()`` por padrão.** Decodifica
  o arquivo inteiro via ``ffmpeg -v error -i <arquivo> -f null -`` (custo
  proporcional à duração do vídeo). Sinal de corrupção: com ``-v error``,
  qualquer saída em ``stderr`` OU um ``returncode`` diferente de zero
  indicam um problema de decodificação -- confirmado empiricamente nesta
  rodada que um arquivo com dados de frame corrompidos no meio (container
  íntegro) produz ``stderr`` não vazio mesmo com ``returncode == 0``
  (FFmpeg reporta os erros de decodificação mas ainda finaliza o processo
  com sucesso) -- por isso a checagem de corrupção profunda usa AMBOS os
  sinais (``returncode != 0 OR stderr não vazio``), nunca só o
  ``returncode``. Exposta como método separado, nunca ligado por padrão a
  toda chamada -- para quando uma camada futura precisar de garantia mais
  forte antes de um render caro (o próprio roadmap fala em "antes de
  processar").

===========================================================================
ORTOGONALIDADE
===========================================================================

Este módulo nunca importa nem chama
``circuit_breaker``/``retry_policy``/``publication_idempotency``/
``secrets_manager``/``domain.job_state_machine``, nunca importa nada de
Geração 1, e nunca cria ``Job``/``Publication``/``Schedule``/``Project``/
``Video``. Não implementa nenhuma correção/reparo automático de mídia --
só VALIDA e REPORTA.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ERRO_ARQUIVO_INEXISTENTE",
    "ERRO_NAO_E_ARQUIVO_REGULAR",
    "ERRO_ARQUIVO_VAZIO",
    "ERRO_FFPROBE_AUSENTE",
    "ERRO_FFMPEG_AUSENTE",
    "ERRO_TIMEOUT",
    "ERRO_FFPROBE_FALHOU",
    "ERRO_SAIDA_JSON_INVALIDA",
    "ERRO_SEM_STREAM_VIDEO",
    "ERRO_RESOLUCAO_INVALIDA",
    "ERRO_DURACAO_INVALIDA",
    "ERRO_DECODE_FALHOU",
    "ERRO_INESPERADO",
    "MediaProbeResult",
    "MediaProbe",
]

# ---------------------------------------------------------------------------
# Códigos de erro estruturados -- nunca uma exceção Python crua escapa do
# método público. Duas famílias claramente distintas:
#   - AMBIENTE (a máquina não tem o binário) -- ERRO_FFPROBE_AUSENTE/
#     ERRO_FFMPEG_AUSENTE.
#   - ARQUIVO (o arquivo em si é inválido/corrompido/inexistente) -- todos
#     os demais.
# Nunca confundidos entre si (item explícito do Prompt).
# ---------------------------------------------------------------------------

ERRO_ARQUIVO_INEXISTENTE = "ARQUIVO_INEXISTENTE"
ERRO_NAO_E_ARQUIVO_REGULAR = "NAO_E_ARQUIVO_REGULAR"
ERRO_ARQUIVO_VAZIO = "ARQUIVO_VAZIO"

# AMBIENTE -- problema da máquina, nunca do arquivo.
ERRO_FFPROBE_AUSENTE = "FFPROBE_AUSENTE"
ERRO_FFMPEG_AUSENTE = "FFMPEG_AUSENTE"

# Timeout -- mesmo código para probe() (ffprobe) e probe_deep() (ffmpeg):
# ambos significam "o processo não terminou a tempo", só o subcomando
# invocado muda.
ERRO_TIMEOUT = "TIMEOUT"

ERRO_FFPROBE_FALHOU = "FFPROBE_FALHOU"
ERRO_SAIDA_JSON_INVALIDA = "SAIDA_JSON_INVALIDA"
ERRO_SEM_STREAM_VIDEO = "SEM_STREAM_VIDEO"
ERRO_RESOLUCAO_INVALIDA = "RESOLUCAO_INVALIDA"
ERRO_DURACAO_INVALIDA = "DURACAO_INVALIDA"

# Só usado por probe_deep() -- decode real encontrou corrupção de frame.
ERRO_DECODE_FALHOU = "DECODE_FALHOU"

# Último recurso -- qualquer exceção verdadeiramente inesperada capturada
# no nível mais externo do método público (GATE 13: nunca deixar uma
# exceção crua escapar). Documentado como catch-all deliberado, mesmo
# padrão já usado em ``FolderImporter``/`AssertionStore`.
ERRO_INESPERADO = "INESPERADO"

_FFPROBE_TIMEOUT_SECONDS_PADRAO = 15.0
_FFMPEG_DECODE_TIMEOUT_SECONDS_PADRAO = 120.0


@dataclass(frozen=True)
class MediaProbeResult:
    """Resultado estruturado de uma validação de mídia -- mesmo padrão de
    ``ImportResult`` (``source_import.py``): sempre devolvido, nunca uma
    exceção crua no caminho de sucesso ou falha."""

    path: str
    valid: bool
    container: str | None = None
    codec: str | None = None
    duration: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False
    audio_stream_count: int = 0
    file_size: int | None = None
    deep_checked: bool = False
    error_code: str | None = None
    error_message: str | None = None


def _validate_file_for_probe(path: Path) -> None:
    """Validação de nível-ARQUIVO -- reproduzida (não importada) a partir
    do mesmo espírito de ``source_import._validate_file_for_import``, mas
    SEM checagem de extensão: ``MediaProbe`` pode ser chamado
    independentemente da importação (item 0.3), então não pode presumir
    que ``_validate_file_for_import`` já rodou nem restringir por
    ``VIDEO_EXTS`` -- um arquivo com extensão qualquer (ou nenhuma) ainda
    é uma pergunta válida para ``ffprobe`` responder."""
    if not path.exists():
        raise _ProbeFileError(ERRO_ARQUIVO_INEXISTENTE, f"Arquivo nao encontrado: {path}")
    if not path.is_file():
        raise _ProbeFileError(ERRO_NAO_E_ARQUIVO_REGULAR, f"Caminho nao e um arquivo regular: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _ProbeFileError(ERRO_ARQUIVO_INEXISTENTE, f"Nao foi possivel ler o arquivo: {path} ({exc})") from exc
    if size <= 0:
        raise _ProbeFileError(ERRO_ARQUIVO_VAZIO, f"Arquivo vazio: {path}")


class _ProbeFileError(RuntimeError):
    """Erro estruturado interno -- nunca escapa de um método público de
    ``MediaProbe`` (sempre convertido em ``MediaProbeResult`` com
    ``valid=False``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_fps(video_stream: dict[str, Any]) -> float | None:
    fps_txt = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    if not fps_txt:
        return None
    try:
        num_txt, den_txt = str(fps_txt).split("/", 1)
        num, den = float(num_txt), float(den_txt)
    except (ValueError, AttributeError):
        return None
    if den == 0:
        return None
    return num / den


def _parse_duration(video_stream: dict[str, Any], format_info: dict[str, Any]) -> float | None:
    duration_txt = video_stream.get("duration") or format_info.get("duration")
    if duration_txt is None:
        return None
    try:
        return float(duration_txt)
    except (TypeError, ValueError):
        return None


class MediaProbe:
    """Validação de mídia via ``ffprobe``/``ffmpeg`` -- ver docstring do
    módulo para o contrato completo (decisões 0.2/0.3/1.5).

    Sem acesso a banco de dados (item 0.3): opera exclusivamente sobre
    ``Path``.
    """

    def __init__(
        self,
        *,
        ffprobe_timeout: float = _FFPROBE_TIMEOUT_SECONDS_PADRAO,
        ffmpeg_decode_timeout: float = _FFMPEG_DECODE_TIMEOUT_SECONDS_PADRAO,
    ) -> None:
        if ffprobe_timeout <= 0:
            raise ValueError("ffprobe_timeout deve ser > 0")
        if ffmpeg_decode_timeout <= 0:
            raise ValueError("ffmpeg_decode_timeout deve ser > 0")
        self.ffprobe_timeout = ffprobe_timeout
        self.ffmpeg_decode_timeout = ffmpeg_decode_timeout

    # -- API pública -----------------------------------------------------

    def probe(self, file_path: "Path | str") -> MediaProbeResult:
        """Verificação RASA (só ``ffprobe`` -- metadados de container).
        Ver seção 1.5 da docstring do módulo para a limitação conhecida
        (não detecta corrupção de dados de frame com container íntegro)."""
        path = Path(file_path)
        path_str = str(path)
        try:
            _validate_file_for_probe(path)
            file_size = path.stat().st_size

            if shutil.which("ffprobe") is None:
                return MediaProbeResult(
                    path=path_str,
                    valid=False,
                    file_size=file_size,
                    error_code=ERRO_FFPROBE_AUSENTE,
                    error_message="ffprobe nao encontrado no PATH desta maquina (problema de ambiente, nao do arquivo)",
                )

            cmd = [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", path_str,
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=self.ffprobe_timeout, check=False,
                )
            except subprocess.TimeoutExpired:
                return MediaProbeResult(
                    path=path_str, valid=False, file_size=file_size,
                    error_code=ERRO_TIMEOUT,
                    error_message=f"ffprobe excedeu o timeout de {self.ffprobe_timeout}s",
                )

            if result.returncode != 0:
                return MediaProbeResult(
                    path=path_str, valid=False, file_size=file_size,
                    error_code=ERRO_FFPROBE_FALHOU,
                    error_message=f"ffprobe falhou (codigo {result.returncode}): {result.stderr[-500:]}",
                )

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                return MediaProbeResult(
                    path=path_str, valid=False, file_size=file_size,
                    error_code=ERRO_SAIDA_JSON_INVALIDA,
                    error_message=f"saida do ffprobe nao e JSON valido: {exc}",
                )

            video_stream: dict[str, Any] | None = None
            audio_count = 0
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video" and video_stream is None:
                    video_stream = stream
                elif stream.get("codec_type") == "audio":
                    audio_count += 1

            if video_stream is None:
                return MediaProbeResult(
                    path=path_str, valid=False, file_size=file_size,
                    error_code=ERRO_SEM_STREAM_VIDEO,
                    error_message="nenhum stream de video encontrado",
                )

            width = int(video_stream.get("width") or 0)
            height = int(video_stream.get("height") or 0)
            if width <= 0 or height <= 0:
                return MediaProbeResult(
                    path=path_str, valid=False, file_size=file_size,
                    error_code=ERRO_RESOLUCAO_INVALIDA,
                    error_message=f"resolucao invalida: {width}x{height}",
                )

            format_info = data.get("format", {}) or {}
            duration = _parse_duration(video_stream, format_info)
            if duration is None or duration <= 0:
                return MediaProbeResult(
                    path=path_str, valid=False, file_size=file_size,
                    error_code=ERRO_DURACAO_INVALIDA,
                    error_message=f"duracao invalida: {duration!r}",
                )

            fps = _parse_fps(video_stream)

            return MediaProbeResult(
                path=path_str,
                valid=True,
                container=format_info.get("format_name"),
                codec=video_stream.get("codec_name"),
                duration=duration,
                fps=fps,
                width=width,
                height=height,
                has_audio=audio_count > 0,
                audio_stream_count=audio_count,
                file_size=file_size,
                deep_checked=False,
            )
        except _ProbeFileError as exc:
            return MediaProbeResult(
                path=path_str, valid=False,
                error_code=exc.code, error_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 -- catch-all deliberado, GATE 13
            return MediaProbeResult(
                path=path_str, valid=False,
                error_code=ERRO_INESPERADO,
                error_message=f"falha inesperada ao validar midia: {exc}",
            )

    def probe_deep(self, file_path: "Path | str", *, timeout: float | None = None) -> MediaProbeResult:
        """Verificação PROFUNDA (opcional -- ver seção 1.5 da docstring do
        módulo): roda ``probe()`` primeiro (falha rápido em qualquer
        problema estrutural/de ambiente), e só então decodifica o arquivo
        inteiro via ``ffmpeg -v error -i <arquivo> -f null -``. Nunca
        chamada automaticamente por ``probe()``/``probe_batch()``."""
        shallow = self.probe(file_path)
        if not shallow.valid:
            return shallow

        path_str = shallow.path
        effective_timeout = timeout if timeout is not None else self.ffmpeg_decode_timeout

        if shutil.which("ffmpeg") is None:
            return _replace_for_error(
                shallow, error_code=ERRO_FFMPEG_AUSENTE,
                error_message="ffmpeg nao encontrado no PATH desta maquina (problema de ambiente, nao do arquivo)",
            )

        cmd = ["ffmpeg", "-v", "error", "-i", path_str, "-f", "null", "-"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=effective_timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return _replace_for_error(
                shallow, error_code=ERRO_TIMEOUT,
                error_message=f"ffmpeg (decode profundo) excedeu o timeout de {effective_timeout}s",
            )
        except Exception as exc:  # noqa: BLE001 -- catch-all deliberado, GATE 13
            return _replace_for_error(
                shallow, error_code=ERRO_INESPERADO,
                error_message=f"falha inesperada durante decode profundo: {exc}",
            )

        # Sinal de corrupção: com "-v error", QUALQUER saida em stderr
        # indica um problema de decodificacao, mesmo quando o returncode
        # e 0 (confirmado empiricamente -- ver secao 1.5 da docstring).
        stderr_text = (result.stderr or "").strip()
        if result.returncode != 0 or stderr_text:
            return _replace_for_error(
                shallow, error_code=ERRO_DECODE_FALHOU,
                error_message=f"decode profundo encontrou erro(s): {stderr_text[-500:] or f'codigo {result.returncode}'}",
            )

        return MediaProbeResult(
            path=shallow.path,
            valid=True,
            container=shallow.container,
            codec=shallow.codec,
            duration=shallow.duration,
            fps=shallow.fps,
            width=shallow.width,
            height=shallow.height,
            has_audio=shallow.has_audio,
            audio_stream_count=shallow.audio_stream_count,
            file_size=shallow.file_size,
            deep_checked=True,
        )

    def probe_batch(self, file_paths: "list[Path | str] | tuple[Path | str, ...]") -> list[MediaProbeResult]:
        """Processa vários arquivos com isolamento por item -- um arquivo
        inválido/corrompido NUNCA interrompe a validação dos demais
        (mesmo princípio já usado em ``FolderImporter``/``AssertionStore``/
        ``SourceContextResolver``). ``probe()`` já é robusta (nunca lança),
        mas o laço aqui também isola qualquer falha verdadeiramente
        inesperada por item, em vez de deixar uma exceção interromper o
        lote inteiro."""
        results: list[MediaProbeResult] = []
        for file_path in file_paths:
            try:
                results.append(self.probe(file_path))
            except Exception as exc:  # noqa: BLE001 -- isolamento por item, GATE 13
                results.append(
                    MediaProbeResult(
                        path=str(file_path), valid=False,
                        error_code=ERRO_INESPERADO,
                        error_message=f"falha inesperada ao validar midia: {exc}",
                    )
                )
        return results


def _replace_for_error(base: MediaProbeResult, *, error_code: str, error_message: str) -> MediaProbeResult:
    """Constrói um ``MediaProbeResult`` de falha reaproveitando os campos
    de contexto (``path``/``file_size``) de um resultado raso já bem-
    sucedido -- usado por ``probe_deep`` quando a checagem profunda em si
    falha depois que a checagem rasa já havia passado."""
    return MediaProbeResult(
        path=base.path,
        valid=False,
        file_size=base.file_size,
        deep_checked=False,
        error_code=error_code,
        error_message=error_message,
    )
