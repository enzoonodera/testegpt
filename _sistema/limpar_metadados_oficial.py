#!/usr/bin/env python3
# -*- coding: utf-8 -*-

r"""
LIMPAR META DADOS OFICIAL — integrado ao Painel Multicanal

Processa uma pasta inteira de vídeos e gera APENAS 1 saída por original.

Preset "BALA":
- remove metadados do contêiner
- zoom leve de ~4,5%
- pequeno movimento suave de enquadramento
- ajuste visual MUITO leve por vídeo (contraste / saturação / brilho / gamma)
- volume +1% (quando existe áudio)
- H.264 / CRF aleatório entre 21 e 25 / preset medium
- mantém resolução e FPS do original
- saída MP4 compatível com TikTok/Reels

Uso simples:
    dê dois cliques em ABRIR_TRATADOR_SPEED.bat

Uso por terminal:
    python limpa_metadados_v4_bala_noise_speed.py -i "C:\pasta\entrada" -o "C:\pasta\saida"

Requisitos:
    Python 3.9+
    ffmpeg e ffprobe no PATH
"""

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .time_utils import utc_now_iso
    from .state_json import load_state_json
except ImportError:
    from time_utils import utc_now_iso
    from state_json import load_state_json

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mts", ".m2ts"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tratador_bala")
ERRO_LOG_LOCK = threading.Lock()


def limpar_tela():
    os.system("cls" if os.name == "nt" else "clear")


def checar_ffmpeg():
    faltando = []
    if shutil.which("ffmpeg") is None:
        faltando.append("ffmpeg")
    if shutil.which("ffprobe") is None:
        faltando.append("ffprobe")

    if faltando:
        log.error("Não encontrei no PATH: %s", ", ".join(faltando))
        log.error("Instale o FFmpeg e confirme que ffmpeg/ffprobe funcionam no CMD.")
        return False

    return True


def executar_capture(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def obter_info_video(caminho: Path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(caminho),
    ]

    r = executar_capture(cmd)
    if r.returncode != 0:
        log.error("ffprobe falhou em %s: %s", caminho.name, r.stderr[-500:])
        return None

    try:
        dados = json.loads(r.stdout)
    except json.JSONDecodeError:
        log.error("Não consegui interpretar o ffprobe de %s", caminho.name)
        return None

    video = None
    audio = None

    for s in dados.get("streams", []):
        if s.get("codec_type") == "video" and video is None:
            video = s
        elif s.get("codec_type") == "audio" and audio is None:
            audio = s

    if not video:
        log.error("Nenhum stream de vídeo encontrado em %s", caminho.name)
        return None

    largura = int(video.get("width") or 0)
    altura = int(video.get("height") or 0)

    if largura <= 0 or altura <= 0:
        log.error("Resolução inválida em %s", caminho.name)
        return None

    # FPS e duração são usados pelo zoom progressivo para atravessar o vídeo
    # inteiro sem alterar a duração base.
    fps_txt = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    try:
        num, den = fps_txt.split("/", 1)
        fps = float(num) / float(den) if float(den) else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    duracao_txt = video.get("duration") or dados.get("format", {}).get("duration") or "0"
    try:
        duracao = float(duracao_txt)
    except (TypeError, ValueError):
        duracao = 0.0

    if fps <= 0:
        fps = 30.0
    if duracao <= 0:
        duracao = 1.0

    return {
        "width": largura,
        "height": altura,
        "has_audio": audio is not None,
        "fps": fps,
        "duration": duracao,
    }


def numero_par_acima(n):
    n = int(math.ceil(n))
    return n if n % 2 == 0 else n + 1


def montar_filtro_video(width: int, height: int, fps: float, duration: float, ajustes_visuais):
    """
    Aplica zoom progressivo centralizado ao longo de todo o vídeo:
    100% -> 104% ou 104% -> 100%, escolhido uma vez por vídeo.
    Sem movimento lateral. Mantém a resolução final original.
    """
    total_frames = max(2, int(round(duration * fps)))
    ultimo = max(1, total_frames - 1)
    direcao = ajustes_visuais["zoom_direction"]

    if direcao == "in":
        # on = número do frame de saída do zoompan.
        zoom_expr = f"min(1.04,1+0.04*on/{ultimo})"
    else:
        zoom_expr = f"max(1.0,1.04-0.04*on/{ultimo})"

    red = ajustes_visuais["red_shift"]
    blue = ajustes_visuais["blue_shift"]

    filtros = [
        (
            "zoompan="
            f"z='{zoom_expr}':"
            "x='iw/2-(iw/zoom/2)':"
            "y='ih/2-(ih/zoom/2)':"
            "d=1:"
            f"s={width}x{height}:"
            f"fps={fps:.6f}"
        ),
        (
            "eq="
            f"contrast={ajustes_visuais['contrast']}:"
            f"saturation={ajustes_visuais['saturation']}:"
            f"brightness={ajustes_visuais['brightness']}:"
            f"gamma={ajustes_visuais['gamma']}"
        ),
        # Microajuste independente de R e B; verde permanece neutro.
        (
            "colorbalance="
            f"rs={red}:rm={red}:rh={red}:"
            f"bs={blue}:bm={blue}:bh={blue}"
        ),
        # Vinheta extremamente discreta nas bordas.
        "vignette=angle=0.1",
        # Grain/noise temporal bem leve.
        "noise=alls=3:allf=t+u",
        # Recupera um pouco da nitidez após o processamento.
        "unsharp=5:5:0.22:5:5:0.0",
    ]

    return ",".join(filtros)

def gerar_ajustes_visuais():
    """
    Gera pequenas variações por vídeo. Todos os valores ficam em faixas
    muito discretas e são escolhidos apenas uma vez por arquivo.
    """
    return {
        "contrast": f"{random.uniform(1.008, 1.028):.4f}",
        "saturation": f"{random.uniform(1.010, 1.040):.4f}",
        "brightness": f"{random.uniform(-0.004, 0.004):.4f}",
        "gamma": f"{random.uniform(0.992, 1.012):.4f}",
        # Zoom progressivo: metade dos vídeos aproxima, metade afasta.
        "zoom_direction": random.choice(["in", "out"]),
        # ColorBalance muito sutil e independente nos canais R e B.
        "red_shift": f"{random.uniform(-0.010, 0.010):.4f}",
        "blue_shift": f"{random.uniform(-0.010, 0.010):.4f}",
    }


def load_json(path: Path, default):
    return load_state_json(path, default)


def save_json_atomic(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def fingerprint_origem(path: Path):
    """Fingerprint rápido e estável pelo conteúdo (tamanho + 1 MiB inicial/final)."""
    h = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode("ascii"))
    chunk = 1024 * 1024
    with path.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(0, size - chunk))
            h.update(f.read(chunk))
    return h.hexdigest()


def estado_limpeza_padrao():
    return {"version": 1, "items": {}, "updated_at": None}


def carregar_estado_limpeza(path: Path):
    st = load_json(path, estado_limpeza_padrao())
    if not isinstance(st, dict):
        st = estado_limpeza_padrao()
    st.setdefault("version", 1)
    if not isinstance(st.get("items"), dict):
        st["items"] = {}
    return st


def salvar_estado_limpeza(path: Path, st):
    st["updated_at"] = utc_now_iso()
    save_json_atomic(path, st)


def numero_do_nome(nome: str):
    try:
        return int(Path(nome).stem)
    except (ValueError, TypeError):
        return 0


def maior_indice_postado(raiz_saida: Path):
    """Lê históricos de postagem da conta para nunca reutilizar números já usados.
    Isso é essencial quando a plataforma/pacote antigo apagou os MP4 após agendar.
    """
    maior = 0
    dados = raiz_saida.parent / "dados"
    for nome in ("estado_youtube.json", "estado_tiktok.json", "estado.json"):
        st = load_json(dados / nome, {})
        if not isinstance(st, dict):
            continue
        for row in st.get("scheduled", []) or []:
            if not isinstance(row, dict):
                continue
            maior = max(maior, numero_do_nome(row.get("file", "")))
    return maior


def proximo_indice_com_estado(raiz_saida: Path, estado):
    maior = max(proximo_indice_saida(raiz_saida) - 1, maior_indice_postado(raiz_saida))
    for item in (estado.get("items") or {}).values():
        if isinstance(item, dict):
            maior = max(maior, numero_do_nome(item.get("output", "")))
    return maior + 1


def proximo_indice_saida(raiz_saida: Path):
    """Retorna o próximo número livre (001, 002...) sem sobrescrever vídeos existentes."""
    maior = 0
    if raiz_saida.exists():
        for p in raiz_saida.iterdir():
            if not p.is_file() or p.suffix.lower() != ".mp4":
                continue
            try:
                n = int(p.stem)
            except ValueError:
                continue
            maior = max(maior, n)
    return maior + 1


def destino_para(origem: Path, raiz_entrada: Path, raiz_saida: Path, indice: int, total: int):
    """
    Gera nomes prontos para o agendador do TikTok:
    001.mp4, 002.mp4, 003.mp4...

    A largura mínima é de 3 dígitos e aumenta automaticamente
    se houver mais de 999 vídeos.
    """
    largura = max(3, len(str(total)))
    nome = f"{indice:0{largura}d}.mp4"
    destino = raiz_saida / nome
    destino.parent.mkdir(parents=True, exist_ok=True)
    return destino



def gerar_metadados_smartphone():
    """
    Gera metadados plausiveis de captura por smartphone.
    A data nunca passa de 12/09/2026 e a localizacao fica em Sao Paulo/SP.
    """
    inicio = datetime(2024, 1, 1, 8, 0, 0)
    fim = datetime(2026, 9, 12, 18, 59, 59)
    segundos = int((fim - inicio).total_seconds())
    data_local = inicio + timedelta(seconds=random.randint(0, segundos))

    # Mantem todos os pontos na regiao de Sao Paulo sem usar localizacao pessoal exata.
    lat = -23.5505 + random.uniform(-0.0300, 0.0300)
    lon = -46.6333 + random.uniform(-0.0300, 0.0300)

    # ISO 6709, formato usado por metadados QuickTime/Apple.
    gps = f"{lat:+09.5f}{lon:+010.5f}/"

    # creation_time sem offset para evitar normalizacao para 13/09/2026 em UTC.
    data_ffmpeg = data_local.strftime("%Y-%m-%dT%H:%M:%S")
    data_quicktime = data_local.strftime("%Y-%m-%dT%H:%M:%S-03:00")

    modelos = [
        "iPhone 13",
        "iPhone 14",
        "iPhone 15",
    ]

    return {
        "creation_time": data_ffmpeg,
        "quicktime_creationdate": data_quicktime,
        "gps": gps,
        "make": "Apple",
        "model": random.choice(modelos),
        "software": "iOS",
    }

def processar_video(origem: Path, destino: Path, threads_por_video: int = 1, log_erros: Path | None = None, modo: str = "normal"):
    info = obter_info_video(origem)
    if not info:
        return False

    ajustes_visuais = gerar_ajustes_visuais()
    filtro_base = montar_filtro_video(info["width"], info["height"], info["fps"], info["duration"], ajustes_visuais)

    # Variação MUITO leve de velocidade por vídeo:
    # 0.99x a 1.01x, escolhida uma vez para manter o vídeo natural.
    velocidade = round(random.uniform(0.99, 1.01), 4)

    # setpts:
    # velocidade > 1.0 = vídeo um pouco mais rápido
    # velocidade < 1.0 = vídeo um pouco mais lento
    filtro_video = f"{filtro_base},setpts=PTS/{velocidade}"

    meta = gerar_metadados_smartphone()

    # CRF aleatório por vídeo: 21, 22, 23, 24 ou 25.
    crf = random.randint(21, 25)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i", str(origem),

        "-map", "0:v:0",
        "-map", "0:a:0?",

        "-vf", filtro_video,

        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(crf),
        "-threads", str(max(1, threads_por_video)),
        "-pix_fmt", "yuv420p",

        "-map_metadata", "-1",
        "-map_chapters", "-1",

        "-movflags", "+faststart+use_metadata_tags",
    ]

    if info["has_audio"]:
        # atempo usa o mesmo fator para manter áudio e vídeo sincronizados.
        cmd += [
            "-af", f"atempo={velocidade},volume=1.01",
            "-c:a", "aac",
            "-b:a", "128k",
        ]
    else:
        cmd += ["-an"]

    cmd += [
        "-metadata", "title=",
        "-metadata", "comment=",
        "-metadata", "artist=",
        "-metadata", "copyright=",
        "-metadata", "description=",

        # Metadados de captura por smartphone / Sao Paulo.
        "-metadata", f"creation_time={meta['creation_time']}",
        "-metadata", f"date={meta['creation_time']}",
        "-metadata", f"location={meta['gps']}",
        "-metadata", f"location-eng={meta['gps']}",
        "-metadata", f"com.apple.quicktime.location.ISO6709={meta['gps']}",
        "-metadata", f"com.apple.quicktime.creationdate={meta['quicktime_creationdate']}",
        "-metadata", f"com.apple.quicktime.make={meta['make']}",
        "-metadata", f"com.apple.quicktime.model={meta['model']}",
        "-metadata", f"com.apple.quicktime.software={meta['software']}",
        "-metadata:s:v:0", f"creation_time={meta['creation_time']}",
    ]

    if info["has_audio"]:
        cmd += ["-metadata:s:a:0", f"creation_time={meta['creation_time']}"]

    cmd += [str(destino)]

    log.info(
        "%sProcessando: %s | velocidade: %.4fx | CRF: %d | zoom=%s | R=%s | B=%s | data: %s | GPS: %s | modelo: %s",
        "[RETRY] " if modo == "retry" else "",
        origem.name,
        velocidade,
        crf,
        ajustes_visuais["zoom_direction"],
        ajustes_visuais["red_shift"],
        ajustes_visuais["blue_shift"],
        meta["creation_time"],
        meta["gps"],
        meta["model"],
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    ultimas = deque(maxlen=200)
    assert proc.stdout is not None

    for linha in proc.stdout:
        linha = linha.rstrip()
        ultimas.append(linha)

    rc = proc.wait()

    if rc != 0:
        log.error("Falhou: %s | código de saída FFmpeg: %s", origem.name, rc)
        if ultimas:
            print("\n".join(list(ultimas)[-25:]))
        if log_erros is not None:
            bloco = [
                "=" * 90,
                f"ARQUIVO: {origem}",
                f"DESTINO: {destino}",
                f"MODO: {modo}",
                f"EXIT CODE: {rc}",
                "ÚLTIMAS LINHAS DO FFMPEG:",
                *list(ultimas),
                "",
            ]
            with ERRO_LOG_LOCK:
                with log_erros.open("a", encoding="utf-8", errors="replace") as f:
                    f.write("\n".join(bloco) + "\n")
        try:
            if destino.exists():
                destino.unlink()
        except OSError:
            pass
        return False

    tamanho_original = origem.stat().st_size
    tamanho_saida = destino.stat().st_size if destino.exists() else 0

    if tamanho_original > 0:
        rel = (tamanho_saida / tamanho_original) * 100
        log.info(
            "OK: %s -> %s | velocidade %.4fx | CRF=%d | contraste=%s | saturacao=%s | brilho=%s | gamma=%s | saída = %.1f%% do tamanho original",
            origem.name,
            destino.name,
            velocidade,
            crf,
            ajustes_visuais["contrast"],
            ajustes_visuais["saturation"],
            ajustes_visuais["brightness"],
            ajustes_visuais["gamma"],
            rel,
        )
    else:
        log.info("OK: %s -> %s", origem.name, destino.name)

    return True


def encontrar_videos(pasta: Path):
    return [
        p for p in sorted(pasta.rglob("*"))
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]


def processar_pasta(entrada: Path, saida: Path, jobs: int = 1, state_file: Path | None = None, log_dir: Path | None = None):
    if not entrada.exists() or not entrada.is_dir():
        log.error("Pasta de entrada não existe: %s", entrada)
        return 1

    saida.mkdir(parents=True, exist_ok=True)
    if state_file is None:
        state_file = saida.parent / "dados" / "limpeza_estado.json"
    if log_dir is None:
        log_dir = saida.parent / "logs"
    state_file = Path(state_file)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    videos = encontrar_videos(entrada)
    if not videos:
        log.warning("Nenhum vídeo encontrado em: %s", entrada)
        return 1

    estado = carregar_estado_limpeza(state_file)
    items = estado["items"]

    # Calcula fingerprints antes de reservar números. Assim o mesmo original,
    # mesmo se estiver em outra pasta/nome, não é tratado duas vezes no canal.
    fontes = []
    duplicados_no_lote = []
    vistos_no_lote = set()
    for video in videos:
        try:
            fp = fingerprint_origem(video)
        except OSError as exc:
            log.error("Não consegui ler %s: %s", video, exc)
            continue
        if fp in vistos_no_lote:
            duplicados_no_lote.append(video)
            continue
        vistos_no_lote.add(fp)
        fontes.append((video, fp))

    ja_prontos = []
    pendentes = []
    proximo = proximo_indice_com_estado(saida, estado)

    # Reserva os destinos ANTES do encode. Se o programa/PC parar no meio,
    # a próxima execução reaproveita os mesmos números e continua de onde parou.
    for video, fp in fontes:
        antigo = items.get(fp) if isinstance(items.get(fp), dict) else None
        if antigo:
            nome_saida = str(antigo.get("output") or "").strip()
            destino = saida / nome_saida if nome_saida else None
            if antigo.get("status") == "done" and destino and destino.exists() and destino.stat().st_size > 0:
                ja_prontos.append((video, fp, destino))
                continue
            if not nome_saida:
                nome_saida = f"{proximo:03d}.mp4"
                proximo += 1
                antigo["output"] = nome_saida
            destino = saida / nome_saida
            antigo.update({
                "source_name": video.name,
                "source_path": str(video),
                "source_size": video.stat().st_size,
                "status": "pending",
                "last_seen_at": utc_now_iso(),
            })
            pendentes.append((video, fp, destino))
        else:
            nome_saida = f"{proximo:03d}.mp4"
            proximo += 1
            destino = saida / nome_saida
            items[fp] = {
                "fingerprint": fp,
                "source_name": video.name,
                "source_path": str(video),
                "source_size": video.stat().st_size,
                "output": nome_saida,
                "status": "pending",
                "reserved_at": utc_now_iso(),
                "last_seen_at": utc_now_iso(),
            }
            pendentes.append((video, fp, destino))

    salvar_estado_limpeza(state_file, estado)

    print("\n" + "=" * 72)
    log.info("Originais únicos encontrados: %d", len(fontes))
    if duplicados_no_lote:
        log.info("Duplicados idênticos na pasta de origem: %d (serão ignorados)", len(duplicados_no_lote))
    log.info("Já tratados neste canal: %d (serão ignorados)", len(ja_prontos))
    log.info("Pendentes nesta execução: %d", len(pendentes))
    log.info("Estado/resume: %s", state_file)
    if ja_prontos:
        log.info("Retomada automática ativa: não refaz o que já foi concluído.")
    if not pendentes:
        log.info("Nada novo para tratar. Canal já está em dia.")
        return 0

    jobs = max(1, min(int(jobs), 10, len(pendentes)))
    cpus = os.cpu_count() or 4
    orcamento_threads = max(1, int(cpus * 0.75))
    threads_por_video = max(1, orcamento_threads // jobs)
    log_erros = log_dir / "FFMPEG_ERROS.log"

    log.info("Preset: zoom progressivo 100-104%% + colorbalance + vinheta discreta + variacao visual + noise leve + speed 0,99-1,01x + metadata smartphone/SP + CRF 21-25 aleatorio")
    log.info("Paralelismo: %d vídeo(s) ao mesmo tempo | CPU lógica: %d | orçamento CPU: ~75%% | threads por FFmpeg: %d", jobs, cpus, threads_por_video)
    log.info("Será gerada APENAS 1 saída por original.")
    log.info("Próximo número novo disponível após as reservas: %03d", proximo)
    print()

    ok = 0
    falhas = []

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futuros = {
            executor.submit(processar_video, video, destino, threads_por_video, log_erros, "normal"): (video, fp, destino)
            for video, fp, destino in pendentes
        }

        concluidos = 0
        for futuro in as_completed(futuros):
            video, fp, destino = futuros[futuro]
            concluidos += 1
            try:
                sucesso = bool(futuro.result())
            except Exception as exc:
                sucesso = False
                log.exception("Erro inesperado em %s: %s", video.name, exc)

            registro = items[fp]
            registro["last_attempt_at"] = utc_now_iso()
            if sucesso:
                ok += 1
                registro["status"] = "done"
                registro["completed_at"] = utc_now_iso()
                registro["output"] = destino.name
                registro.pop("last_error", None)
                log.info("PROGRESSO: %d/%d concluídos | OK: %s", concluidos, len(pendentes), destino.name)
            else:
                registro["status"] = "failed"
                registro["last_error"] = "Falha no FFmpeg; será tentado novamente na próxima execução."
                falhas.append((video, fp, destino))
                log.error("PROGRESSO: %d/%d concluídos | FALHOU: %s", concluidos, len(pendentes), video.name)
            salvar_estado_limpeza(state_file, estado)

    # Retry automático na mesma execução; mantém o MESMO número reservado.
    if falhas:
        log.warning("%d vídeo(s) falharam no paralelo. Iniciando retry automático, um por vez...", len(falhas))
        retry_threads = max(1, int(cpus * 0.75))
        ainda_falham = []
        for pos, (video, fp, destino) in enumerate(falhas, 1):
            log.info("RETRY %d/%d: %s -> %s | threads: %d", pos, len(falhas), video.name, destino.name, retry_threads)
            registro = items[fp]
            if processar_video(video, destino, retry_threads, log_erros, "retry"):
                ok += 1
                registro["status"] = "done"
                registro["completed_at"] = utc_now_iso()
                registro["output"] = destino.name
                registro.pop("last_error", None)
                log.info("RETRY OK: %s -> %s", video.name, destino.name)
            else:
                registro["status"] = "failed"
                registro["last_error"] = "Falhou também no retry; continuará pendente."
                ainda_falham.append((video, fp, destino))
                log.error("RETRY FALHOU: %s", video.name)
            registro["last_attempt_at"] = utc_now_iso()
            salvar_estado_limpeza(state_file, estado)
        falhas = ainda_falham

    print("\n" + "=" * 72)
    log.info("Concluído nesta execução: %d/%d novo(s).", ok, len(pendentes))
    log.info("Já existentes ignorados: %d", len(ja_prontos))
    log.info("Saída: %s", saida.resolve())
    log.info("Estado salvo: %s", state_file.resolve())

    if falhas:
        arq = log_dir / "falharam.txt"
        arq.write_text("\n".join(f"{video} -> {destino.name}" for video, _, destino in falhas), encoding="utf-8")
        log.warning("%d falha(s) mesmo após retry. Elas NÃO foram marcadas como concluídas.", len(falhas))
        log.warning("Na próxima execução o sistema tentará esses mesmos vídeos/números novamente.")
        log.warning("Lista: %s", arq)
        log.warning("Detalhes FFmpeg: %s", log_erros)
    else:
        arq = log_dir / "falharam.txt"
        try:
            if arq.exists():
                arq.unlink()
        except OSError:
            pass

    return 0 if not falhas else 1

def modo_interativo():
    limpar_tela()
    print("=" * 72)
    print("        LIMPAR META DADOS OFICIAL - PRESET ROBUSTO / 1 SAÍDA POR VÍDEO")
    print("=" * 72)
    print()
    print("O que ele faz:")
    print("  • limpa metadados")
    print("  • zoom progressivo 100%↔104% sem movimento lateral")
    print("  • ajuste visual discreto (contraste/saturação/brilho/gamma + colorbalance + vinheta)")
    print("  • mantém resolução e FPS")
    print("  • comprime em H.264 com CRF aleatório 21–25")
    print("  • gera SOMENTE 1 arquivo novo por original")
    print()

    entrada_txt = input("Cole a pasta onde estão os vídeos:\n> ").strip().strip('"')
    if not entrada_txt:
        print("\nNenhuma pasta informada.")
        return 1

    entrada = Path(entrada_txt).expanduser().resolve()

    padrao_saida = entrada.parent / f"{entrada.name}_TRATADOS"
    print(f"\nPasta de saída padrão:\n{padrao_saida}")
    saida_txt = input("ENTER para usar essa pasta ou cole outra:\n> ").strip().strip('"')
    saida = Path(saida_txt).expanduser().resolve() if saida_txt else padrao_saida

    cpus = os.cpu_count() or 4
    # 12 CPUs lógicas -> 3 simultâneos; evita saturar a máquina.
    recomendado = min(3, max(2, cpus // 4)) if cpus >= 8 else 1
    print(f"\nSeu Windows reporta {cpus} CPUs lógicas.")
    print(f"Quantos vídeos processar ao mesmo tempo? 1 a 10 | ENTER = {recomendado} (recomendado)")
    jobs_txt = input("> ").strip()
    try:
        jobs = int(jobs_txt) if jobs_txt else recomendado
    except ValueError:
        jobs = recomendado
    jobs = max(1, min(jobs, 10))

    print()
    return processar_pasta(entrada, saida, jobs)


def main():
    if not checar_ffmpeg():
        input("\nENTER para sair...")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Processa vídeos em lote com o preset BALA e gera 1 saída por original."
    )
    parser.add_argument("-i", "--input", help="Pasta de entrada")
    parser.add_argument("-o", "--output", help="Pasta de saída")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="Vídeos simultâneos (1 a 10)")
    parser.add_argument("--state-file", help="Arquivo JSON de estado/resume por canal")
    parser.add_argument("--log-dir", help="Pasta onde salvar logs e falhas")
    parser.add_argument("--sem-pausa", action="store_true", help="Não aguarda ENTER ao terminar (uso pelo painel multicanal)")
    args = parser.parse_args()

    if args.input:
        entrada = Path(args.input).expanduser().resolve()
        if args.output:
            saida = Path(args.output).expanduser().resolve()
        else:
            saida = entrada.parent / f"{entrada.name}_TRATADOS"
        rc = processar_pasta(entrada, saida, max(1, min(args.jobs, 10)), Path(args.state_file).resolve() if args.state_file else None, Path(args.log_dir).resolve() if args.log_dir else None)
    else:
        rc = modo_interativo()

    print()
    if not args.sem_pausa:
        try:
            input("ENTER para fechar...")
        except EOFError:
            pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
