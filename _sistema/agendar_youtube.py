# -*- coding: utf-8 -*-
"""
YOUTUBE SHORTS AUTO V18

Foco exclusivo em YouTube Shorts:
- upload automático
- título e descrição próprios para YouTube
- validação de Short (vertical/quadrado e <= 3 min)
- audiência: não é conteúdo para crianças
- agenda 3 por dia
- controla progresso por fingerprint do arquivo
- aceita 001.mp4, 002.mp4 etc. novamente em lotes novos
- não apaga arquivo em caso de erro
"""

from pathlib import Path
from datetime import datetime, timedelta
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

try:
    from .app_paths import account_dir_from_env, account_paths
    from .state_json import load_state_json
    from .time_utils import (
        SCHEDULE_TIME_MANUAL,
        NonexistentLocalTimeError,
        canonical_schedule_fields,
        ensure_timezone_config,
        MissingTimezoneConfigurationError,
        iana_zone,
        local_today,
        local_wall_time_to_utc,
        schedule_utc_from_record,
        timezone_name_from_config,
        utc_now,
        utc_now_iso,
        utc_to_local,
    )
except ImportError:
    from app_paths import account_dir_from_env, account_paths
    from state_json import load_state_json
    from time_utils import (
        SCHEDULE_TIME_MANUAL,
        NonexistentLocalTimeError,
        canonical_schedule_fields,
        ensure_timezone_config,
        MissingTimezoneConfigurationError,
        iana_zone,
        local_today,
        local_wall_time_to_utc,
        schedule_utc_from_record,
        timezone_name_from_config,
        utc_now,
        utc_now_iso,
        utc_to_local,
    )

BASE = account_dir_from_env("YT_CHANNEL_DIR", standalone_namespace="agendar_youtube")
ACCOUNT_PATHS = account_paths(BASE)
DATA_DIR = ACCOUNT_PATHS.data
VIDEO_DIR = ACCOUNT_PATHS.videos
PROFILE_DIR = ACCOUNT_PATHS.profile_youtube
LOG_DIR = ACCOUNT_PATHS.logs
STATE_FILE = DATA_DIR / "estado_youtube.json"
CONFIG_FILE = ACCOUNT_PATHS.config
TITLE_FILE = DATA_DIR / "titulos_youtube.txt"
DESC_FILE = DATA_DIR / "descricoes_youtube.txt"
AI_TITLES_FILE = DATA_DIR / "textos_postagem.json"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}

def load_json(path, default):
    return load_state_json(path, default)

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

def natural_key(p: Path):
    parts = re.split(r"(\d+)", p.name.lower())
    return [int(x) if x.isdigit() else x for x in parts]

def fingerprint(path: Path):
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

def load_titles():
    vals = [x.strip() for x in TITLE_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not vals:
        raise RuntimeError("titulos_youtube.txt está vazio.")
    return vals

def load_descriptions():
    raw = DESC_FILE.read_text(encoding="utf-8")
    vals = [x.strip() for x in raw.split("---SHORT---") if x.strip()]
    if not vals:
        raise RuntimeError("descricoes_youtube.txt está vazio.")
    return vals

def save_debug(page, tag):
    LOG_DIR.mkdir(exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    png = LOG_DIR / f"{stamp}_{tag}.png"
    html = LOG_DIR / f"{stamp}_{tag}.html"
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception:
        pass
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    print(f"[DEBUG] {png.name} / {html.name}")

def probe_short(path: Path, max_seconds=180):
    if shutil.which("ffprobe") is None:
        return {
            "ok": None,
            "reason": "ffprobe não encontrado; validação automática ignorada."
        }

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json",
        str(path)
    ]
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=30, check=False
        )
        if r.returncode != 0:
            return {"ok": None, "reason": "ffprobe falhou."}

        data = json.loads(r.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return {"ok": False, "reason": "Nenhum stream de vídeo."}

        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
        dur = float((data.get("format") or {}).get("duration") or 0)

        if w <= 0 or h <= 0:
            return {"ok": False, "reason": "Resolução inválida."}
        if h < w:
            return {
                "ok": False, "width": w, "height": h, "duration": dur,
                "reason": f"Horizontal {w}x{h}; não é Short."
            }
        if dur > float(max_seconds):
            return {
                "ok": False, "width": w, "height": h, "duration": dur,
                "reason": f"{dur:.1f}s > {max_seconds}s."
            }

        return {
            "ok": True, "width": w, "height": h, "duration": dur,
            "over_60": dur > 60.0
        }
    except Exception as e:
        return {"ok": None, "reason": str(e)}

def list_items(cfg):
    VIDEO_DIR.mkdir(exist_ok=True)
    files = sorted(
        [p for p in VIDEO_DIR.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
        key=natural_key
    )

    items = []
    rejected = []

    for p in files:
        fp = fingerprint(p)
        info = probe_short(p, cfg.get("duracao_maxima_short_segundos", 180))

        if cfg.get("exigir_formato_short", True) and info.get("ok") is False:
            rejected.append((p, info))
            print(f"[IGNORADO] {p.name}: {info.get('reason')}")
            continue

        if info.get("ok") is None:
            print(f"[AVISO] {p.name}: {info.get('reason')}")

        if info.get("over_60") and cfg.get(
            "preferir_ate_60s_para_reduzir_risco_content_id_bloqueado", True
        ):
            print(
                f"[AVISO >60s] {p.name}: se houver reivindicação ativa de "
                "Content ID, o Short pode ser bloqueado."
            )

        items.append({"path": p, "fp": fp, "info": info})

    if rejected:
        (ACCOUNT_PATHS.root/"youtube_nao_shorts.txt").write_text(
            "\n".join(f"{p.name} | {info.get('reason')}" for p, info in rejected),
            encoding="utf-8"
        )

    return items

def parse_dt(v, timezone_name="UTC"):
    """Compatibilidade: converte ISO legado/canônico em instante UTC aware."""
    if not v:
        return None
    return schedule_utc_from_record({"datetime": v}, timezone_name)


def build_slots(cfg, state, count, *, now_utc=None):
    """Gera slots no timezone IANA explícito da conta.

    O relógio do Windows não participa da decisão. Histórico novo usa UTC +
    timezone por registro; histórico legado naive é interpretado no timezone
    explícito da conta. Horários inexistentes por início de DST são pulados,
    nunca deslocados silenciosamente.
    """
    if count <= 0:
        return []

    timezone_name = timezone_name_from_config(cfg)
    now_utc = utc_now() if now_utc is None else now_utc.astimezone(iana_zone("UTC"))
    default_times = cfg.get("horarios", ["19:00", "20:00", "21:00"])
    by_day = cfg.get("horarios_por_dia", {})

    day_names = {
        0: "segunda",
        1: "terca",
        2: "quarta",
        3: "quinta",
        4: "sexta",
        5: "sabado",
        6: "domingo",
    }

    today = local_today(timezone_name, now_utc=now_utc)
    max_years = int(cfg.get("max_anos_agendamento", 5))
    max_day = today + timedelta(days=366 * max_years)

    instants = []
    for record in state.get("scheduled", []):
        if not isinstance(record, dict):
            continue
        instant = schedule_utc_from_record(record, timezone_name)
        if instant is not None:
            instants.append(instant)
    last_utc = max(instants) if instants else None

    d = (
        utc_to_local(last_utc, timezone_name).date()
        if last_utc is not None
        else (
            today + timedelta(days=1)
            if cfg.get("comecar_amanha_na_primeira_execucao", True)
            else today
        )
    )

    slots = []
    minimum_utc = now_utc + timedelta(minutes=20)

    while d <= max_day and len(slots) < count:
        day_key = day_names[d.weekday()]
        horarios = by_day.get(day_key, default_times)
        horarios = sorted(horarios, key=lambda x: tuple(map(int, x.split(":"))))

        for hhmm in horarios:
            try:
                candidate_utc = local_wall_time_to_utc(d, hhmm, timezone_name)
            except NonexistentLocalTimeError:
                # Ex.: 02:30 no salto de DST. Não alteramos a intenção para 03:30.
                continue

            if candidate_utc <= minimum_utc:
                continue
            if last_utc is not None and candidate_utc <= last_utc:
                continue

            slots.append(utc_to_local(candidate_utc, timezone_name))
            if len(slots) >= count:
                break

        d += timedelta(days=1)

    if len(slots) < count:
        raise RuntimeError(
            f"Faltaram slots para {count} vídeos. "
            f"Aumente max_anos_agendamento no config.json."
        )

    return slots

def login_required(page):
    url = (page.url or "").lower()
    return "accounts.google.com" in url or "signin" in url

def get_upload_file_inputs(page):
    """
    V18: NÃO usa is_attached(), pois isso estava falhando silenciosamente
    na instalação atual do Playwright.

    O HTML real do YouTube confirma:
      input[type="file"][name="Filedata"]
    """
    selectors = [
        'input[type="file"][name="Filedata"]',
        'ytcp-uploads-file-picker input[type="file"][name="Filedata"]',
        'ytcp-uploads-dialog input[type="file"][name="Filedata"]',
        'input[type="file"]',
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count > 0:
                print(f"    Upload input encontrado: {sel} (qtd={count})")
                return [loc.nth(i) for i in range(min(count, 10))]
        except Exception as e:
            print(f"    [DEBUG] selector {sel}: {e}")

    return []

def upload_video_file(page, video_path):
    """
    V18: envia diretamente para o input real name="Filedata".

    Correção principal:
    - remove is_attached()
    - usa Locator.wait_for(state="attached")
    - set_input_files funciona mesmo com input display:none
    """
    print("    Procurando input Filedata real...")

    selectors = [
        'input[type="file"][name="Filedata"]',
        'ytcp-uploads-file-picker input[type="file"][name="Filedata"]',
        'ytcp-uploads-dialog input[type="file"][name="Filedata"]',
    ]

    file_input = None
    selected_selector = None

    deadline = time.time() + 30

    while time.time() < deadline and file_input is None:
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = loc.count()

                if count > 0:
                    candidate = loc.last
                    # Método suportado e explícito para aguardar o elemento no DOM.
                    candidate.wait_for(state="attached", timeout=3000)
                    file_input = candidate
                    selected_selector = sel
                    break
            except Exception:
                pass

        if file_input is None:
            time.sleep(.25)

    if file_input is None:
        # Antes de falhar, registra diagnóstico de quantos inputs a página vê.
        try:
            all_files = page.locator('input[type="file"]').count()
        except Exception:
            all_files = -1

        try:
            all_filedata = page.locator('input[name="Filedata"]').count()
        except Exception:
            all_filedata = -1

        print(
            f"    [DEBUG] input[type=file]={all_files} | "
            f"input[name=Filedata]={all_filedata}"
        )

        save_debug(page, "sem_filedata_v18")
        raise RuntimeError(
            "O modal abriu, mas o Playwright não conseguiu anexar ao input "
            f"Filedata. Detectados: file={all_files}, Filedata={all_filedata}."
        )

    print(f"    Campo Filedata encontrado com: {selected_selector}")

    try:
        # Inputs file ocultos aceitam set_input_files normalmente.
        file_input.set_input_files(str(video_path), timeout=30000)
        print(f"    Arquivo enviado: {video_path.name}")
    except Exception as e:
        save_debug(page, "erro_set_filedata_v18")
        raise RuntimeError(
            f"Encontrei Filedata, mas set_input_files falhou: {e}"
        )

    # Aguarda a tela inicial de seleção desaparecer e os detalhes aparecerem.
    end = time.time() + 90
    while time.time() < end:
        # Título é a confirmação mais forte.
        for sel in [
            "#title-textarea #textbox",
            "ytcp-social-suggestions-textbox#title-textarea #textbox",
        ]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    print("    Upload confirmado: tela de detalhes aberta.")
                    return
            except Exception:
                pass

        # Se o botão de selecionar sumiu, o modal avançou.
        picker_visible = False
        for text in ["Selecionar ficheiros", "Selecionar arquivos", "Select files"]:
            try:
                loc = page.get_by_text(text, exact=True)
                for i in range(min(loc.count(), 5)):
                    if loc.nth(i).is_visible():
                        picker_visible = True
                        break
            except Exception:
                pass

        if not picker_visible:
            try:
                body = (page.locator("body").inner_text(timeout=1500) or "").lower()
                if any(x in body for x in [
                    "detalhes", "details", "título", "title",
                    "miniatura", "thumbnail", "público", "audience"
                ]):
                    print("    YouTube avançou para os detalhes.")
                    return
            except Exception:
                pass

        time.sleep(.5)

    save_debug(page, "filedata_upload_nao_avancou_v18")
    raise RuntimeError(
        "O MP4 foi colocado no Filedata, mas o YouTube não abriu a tela "
        "de detalhes em 90 segundos."
    )

def wait_upload_page(page):
    """
    Garante que a janela 'Carregar vídeos' esteja aberta.

    O YouTube Studio nem sempre expõe um input[type=file] acessível no DOM.
    Portanto esta versão considera a janela pronta quando encontra:
    - ytcp-uploads-dialog visível
    - botão 'Selecionar ficheiros' / 'Select files'
    - ou um input de arquivo válido
    """

    def upload_modal_ready():
        # 1) Modal oficial do Studio.
        try:
            dlg = page.locator("ytcp-uploads-dialog")
            for i in range(min(dlg.count(), 10)):
                if dlg.nth(i).is_visible():
                    return True
        except Exception:
            pass

        # 2) Botão da tela mostrada no print.
        for text in [
            "Selecionar ficheiros",
            "Selecionar arquivos",
            "Select files",
        ]:
            try:
                loc = page.get_by_text(text, exact=True)
                for i in range(min(loc.count(), 10)):
                    if loc.nth(i).is_visible():
                        return True
            except Exception:
                pass

        # 3) Fallback para input real, se existir.
        try:
            if get_upload_file_inputs(page):
                return True
        except Exception:
            pass

        return False

    # Se o modal já está aberto, não navega de novo.
    if upload_modal_ready():
        return

    # Tenta o atalho oficial de upload.
    try:
        page.goto(
            "https://www.youtube.com/upload",
            wait_until="domcontentloaded",
            timeout=90000
        )
    except Exception:
        pass

    time.sleep(2)

    if login_required(page):
        save_debug(page, "login_necessario")
        raise RuntimeError(
            "YouTube pediu login. Rode 1_LOGIN_YOUTUBE_NORMAL.bat primeiro."
        )

    # O atalho pode já ter aberto o modal.
    end = time.time() + 12
    while time.time() < end:
        if upload_modal_ready():
            return
        time.sleep(.4)

    # Se voltou para Conteúdo do canal, clica em Carregar vídeos.
    clicked_upload = False

    button_patterns = [
        r"^Carregar vídeos$",
        r"^Carregar videos$",
        r"^Upload videos$",
        r"^Enviar vídeos$",
        r"^Enviar videos$",
    ]

    for pat in button_patterns:
        try:
            btn = page.get_by_role("button", name=re.compile(pat, re.I))
            for i in range(min(btn.count(), 10)):
                el = btn.nth(i)
                if el.is_visible() and el.is_enabled():
                    el.click()
                    clicked_upload = True
                    break
        except Exception:
            pass
        if clicked_upload:
            break

    if not clicked_upload:
        for text in [
            "Carregar vídeos",
            "Carregar videos",
            "Upload videos",
            "Enviar vídeos",
            "Enviar videos",
        ]:
            try:
                loc = page.get_by_text(text, exact=True)
                for i in range(min(loc.count(), 10)):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click()
                        clicked_upload = True
                        break
            except Exception:
                pass
            if clicked_upload:
                break

    # Fallback: Criar -> Enviar vídeos.
    if not clicked_upload:
        try:
            create_btn = page.get_by_text("Criar", exact=True)
            if create_btn.count() and create_btn.first.is_visible():
                create_btn.first.click()
                time.sleep(.7)

                for text in [
                    "Enviar vídeos",
                    "Enviar videos",
                    "Carregar vídeos",
                    "Carregar videos",
                    "Upload videos",
                ]:
                    loc = page.get_by_text(text, exact=True)
                    if loc.count() and loc.first.is_visible():
                        loc.first.click()
                        clicked_upload = True
                        break
        except Exception:
            pass

    end = time.time() + 20
    while time.time() < end:
        if upload_modal_ready():
            return
        if login_required(page):
            break
        time.sleep(.4)

    save_debug(page, "nao_abriu_modal_upload")
    raise RuntimeError(
        "Não consegui abrir a janela 'Carregar vídeos' do YouTube."
    )

def visible_first(page, selectors, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        for sel in selectors:
            loc = page.locator(sel)
            try:
                for i in range(min(loc.count(), 10)):
                    el = loc.nth(i)
                    if el.is_visible():
                        return el
            except Exception:
                pass
        time.sleep(.3)
    return None

def fill_editable(el, text):
    el.click()
    try:
        el.press("Control+A")
        el.press("Backspace")
        el.fill(text)
    except Exception:
        el.click()
        el.press("Control+A")
        el.press("Backspace")
        el.page.keyboard.insert_text(text)

def choose_not_for_kids(page):
    for sel in [
        'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]',
        '[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'
    ]:
        loc = page.locator(sel)
        if loc.count():
            loc.first.click()
            time.sleep(.5)
            return

    loc = page.get_by_text(re.compile(r"não.*conteúdo.*crian|not.*made.*for.*kids", re.I))
    if loc.count():
        loc.first.click()
        time.sleep(.5)
        return

    save_debug(page, "audiencia_nao_encontrada")
    raise RuntimeError("Não achei a opção 'Não é conteúdo para crianças'.")

def click_next(page):
    btn = page.locator("#next-button")
    if btn.count() == 0:
        btn = page.get_by_role("button", name=re.compile(r"próxim|next", re.I))

    end = time.time() + 60
    checked_limit = 0
    while time.time() < end:
        try:
            if btn.count() and btn.first.is_visible() and btn.first.is_enabled():
                btn.first.click()
                time.sleep(1.1)
                return
        except Exception:
            pass

        # Não espera os 60s inteiros se o YouTube já mostrou o aviso de
        # limite diário de upload (botão fica travado por causa disso).
        if time.time() - checked_limit > 3:
            checked_limit = time.time()
            if detect_daily_upload_limit(page):
                save_debug(page, "next_bloqueado_limite_diario")
                raise RuntimeError(
                    "Botão PRÓXIMA travado: o YouTube mostrou aviso de "
                    "limite diário de upload."
                )

        time.sleep(.5)

    save_debug(page, "next_desabilitado")
    raise RuntimeError("Botão PRÓXIMA não disponível.")

def choose_schedule(page):
    for sel in [
        "#schedule-radio-button",
        'tp-yt-paper-radio-button[name="SCHEDULE"]',
        '[name="SCHEDULE"]'
    ]:
        loc = page.locator(sel)
        if loc.count():
            loc.first.click()
            time.sleep(1)
            return

    loc = page.get_by_text(re.compile(r"^(programar|agendar|schedule)$", re.I))
    if loc.count():
        loc.first.click()
        time.sleep(1)
        return

    save_debug(page, "schedule_nao_encontrado")
    raise RuntimeError("Não achei PROGRAMAR.")

def set_date(page, target_dt):
    trigger = page.locator("#datepicker-trigger")
    if trigger.count() == 0:
        trigger = page.locator("ytcp-visibility-scheduler ytcp-text-dropdown-trigger").first
    if trigger.count() == 0:
        save_debug(page, "datepicker_trigger_ausente")
        raise RuntimeError("Não achei seletor de data.")

    trigger.first.click()
    time.sleep(.5)

    date_input = visible_first(page, [
        "ytcp-date-picker input",
        'tp-yt-paper-dialog input[type="text"]',
        'ytcp-date-picker tp-yt-paper-input input'
    ], timeout=8)

    if date_input is None:
        save_debug(page, "datepicker_input_ausente")
        raise RuntimeError("Calendário abriu, mas campo de data não apareceu.")

    value = target_dt.strftime("%d/%m/%Y")
    date_input.click()
    date_input.press("Control+A")
    try:
        date_input.fill(value)
    except Exception:
        page.keyboard.insert_text(value)
    date_input.press("Enter")
    time.sleep(.8)

def set_time(page, target_dt):
    """
    Corrige o seletor customizado de horário do YouTube Studio.
    Além de preencher o horário, fecha explicitamente o dropdown antes
    de continuar.
    """
    wanted = target_dt.strftime("%H:%M")

    inputs = page.locator("ytcp-visibility-scheduler input")
    candidates = []

    for i in range(min(inputs.count(), 30)):
        el = inputs.nth(i)
        try:
            if not el.is_visible():
                continue

            placeholder = (el.get_attribute("placeholder") or "").lower()
            aria = (el.get_attribute("aria-label") or "").lower()
            try:
                value = (el.input_value() or "").lower()
            except Exception:
                value = ""

            attrs = f"{placeholder} {aria} {value}"

            if any(x in attrs for x in [
                "dd/", "/yyyy", "/aaaa", "data", "date"
            ]):
                continue

            if (
                "hora" in attrs
                or "time" in attrs
                or ":" in value
                or "hh:mm" in attrs
            ):
                candidates.append(el)
        except Exception:
            pass

    if not candidates:
        for i in range(min(inputs.count(), 30)):
            el = inputs.nth(i)
            try:
                if not el.is_visible():
                    continue

                try:
                    value = el.input_value() or ""
                except Exception:
                    value = ""

                placeholder = (el.get_attribute("placeholder") or "").lower()
                aria = (el.get_attribute("aria-label") or "").lower()
                attrs = f"{value} {placeholder} {aria}".lower()

                if not any(x in attrs for x in [
                    "/", "data", "date", "yyyy", "aaaa"
                ]):
                    candidates.append(el)
            except Exception:
                pass

    if not candidates:
        save_debug(page, "time_input_ausente")
        raise RuntimeError("Não achei o campo de horário do YouTube.")

    field = candidates[0]

    try:
        field.click()
    except Exception:
        field.click(force=True)

    time.sleep(.6)

    selected = False

    option_selectors = [
        "tp-yt-paper-item",
        "ytcp-text-menu tp-yt-paper-item",
        '[role="option"]',
        '[role="menuitem"]',
        "paper-item",
    ]

    for sel in option_selectors:
        loc = page.locator(sel).filter(
            has_text=re.compile(rf"^\s*{re.escape(wanted)}\s*$")
        )

        for i in range(min(loc.count(), 20)):
            opt = loc.nth(i)
            try:
                if opt.is_visible():
                    opt.scroll_into_view_if_needed()
                    opt.click()
                    selected = True
                    break
            except Exception:
                pass

        if selected:
            break

    if not selected:
        exact = page.get_by_text(wanted, exact=True)

        for i in range(min(exact.count(), 30)):
            opt = exact.nth(i)
            try:
                if not opt.is_visible():
                    continue

                tag = (opt.evaluate("(e) => e.tagName") or "").lower()
                if tag == "input":
                    continue

                opt.scroll_into_view_if_needed()
                opt.click()
                selected = True
                break
            except Exception:
                pass

    if not selected:
        try:
            field.click()
            field.press("Control+A")
            field.fill(wanted)
        except Exception:
            try:
                field.click()
                field.press("Control+A")
                page.keyboard.insert_text(wanted)
            except Exception:
                save_debug(page, "falha_preencher_horario")
                raise RuntimeError(
                    f"Não consegui definir o horário {wanted}."
                )

        try:
            field.press("Enter")
        except Exception:
            page.keyboard.press("Enter")

    time.sleep(.5)

    # CORREÇÃO: antes havia um page.keyboard.press("Escape") aqui, sem
    # checar nada antes. O problema é que Escape no assistente de upload
    # do YouTube fecha o DIÁLOGO INTEIRO (não só este menu de horário)
    # quando o menu já tinha fechado sozinho ao clicar na opção. Isso
    # descartava o agendamento inteiro em silêncio — sem levantar erro
    # — e só se percebia depois, quando o clique em "Programar" não
    # encontrava mais nada porque a tela já tinha voltado para
    # "Conteúdo do canal" (vazia). Por isso agora só mexemos em algo
    # DEPOIS de confirmar que o menu realmente continua aberto.
    dropdown_still_open = False

    for sel in [
        '[role="listbox"]',
        "tp-yt-paper-listbox",
        "ytcp-text-menu",
        "iron-dropdown",
    ]:
        loc = page.locator(sel)
        try:
            for i in range(min(loc.count(), 10)):
                if loc.nth(i).is_visible():
                    dropdown_still_open = True
                    break
        except Exception:
            pass

        if dropdown_still_open:
            break

    if dropdown_still_open:
        safe_targets = [
            "ytcp-visibility-scheduler",
            "ytcp-video-visibility-select",
            "ytcp-uploads-dialog",
        ]

        clicked = False

        for sel in safe_targets:
            loc = page.locator(sel)
            if not loc.count():
                continue

            try:
                box = loc.first.bounding_box()
                if box:
                    page.mouse.click(box["x"] + 15, box["y"] + 15)
                    clicked = True
                    break
            except Exception:
                pass

        if not clicked:
            # CORREÇÃO: antes era um clique em coordenadas fixas da
            # página inteira (20, 120), que podia acertar qualquer
            # elemento por trás do diálogo (inclusive o menu lateral do
            # Studio) se o diálogo já não estivesse mais ali. Agora o
            # clique de último recurso é sempre dentro dos limites do
            # próprio diálogo de upload, nunca em coordenadas cruas da
            # página.
            try:
                dlg = page.locator("ytcp-uploads-dialog").first
                box = dlg.bounding_box() if dlg.count() else None
                if box:
                    page.mouse.click(box["x"] + 20, box["y"] + 20)
                    clicked = True
            except Exception:
                pass

        # Só usamos Escape como ÚLTIMO recurso, e mesmo assim só quando
        # já confirmamos acima que o menu ainda estava aberto.
        if not clicked:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        time.sleep(.5)

    try:
        applied = field.input_value().strip()
    except Exception:
        applied = ""

    if applied and wanted not in applied:
        try:
            field.click()
            field.press("Control+A")
            field.fill(wanted)
            field.press("Enter")
            time.sleep(.3)
        except Exception:
            pass

    print(f"    Horário selecionado: {wanted}")


def parse_observed_clock_text(text):
    """Interpreta o texto exibido no campo de horário do YouTube Studio.

    Só aceita HH:MM / H:MM (24h) -- não há evidência no código atual de que
    esse campo específico apresente 12h/AM-PM, então qualquer formato fora
    disso é tratado como desconhecido: levanta ValueError (falha fechada) em
    vez de tentar adivinhar.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("campo de horário vazio")
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not m:
        raise ValueError(f"formato de horário não reconhecido: {text!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"horário fora do intervalo válido: {text!r}")
    return hour, minute


def parse_observed_date_text(text):
    """Interpreta o texto exibido no campo de data do YouTube Studio.

    Aceita DD/MM/YYYY (o formato que o próprio set_date() digita) com
    separador '/', '-' ou '.'. Qualquer coisa fora disso é desconhecida --
    falha fechada, sem tentar um parser universal de datas.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("campo de data vazio")
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", text)
    if not m:
        raise ValueError(f"formato de data não reconhecido: {text!r}")
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return day, month, year


def _locate_time_field_again(page):
    """Relocaliza (não reaproveita a referência de set_time()) o campo de
    horário, usando a mesma heurística já usada em produção por set_time()."""
    inputs = page.locator("ytcp-visibility-scheduler input")
    candidates = []
    for i in range(min(inputs.count(), 30)):
        el = inputs.nth(i)
        try:
            if not el.is_visible():
                continue
            placeholder = (el.get_attribute("placeholder") or "").lower()
            aria = (el.get_attribute("aria-label") or "").lower()
            try:
                value = (el.input_value() or "").lower()
            except Exception:
                value = ""
            attrs = f"{value} {placeholder} {aria}"
            if any(x in attrs for x in ["/", "data", "date", "yyyy", "aaaa"]):
                continue
            if "hora" in attrs or "time" in attrs or ":" in value or "hh:mm" in attrs:
                candidates.append(el)
        except Exception:
            pass
    return candidates[0] if candidates else None


def read_observed_schedule_datetime(page, target_dt):
    """CANDIDATO C -- correção cirúrgica.

    Relocaliza (não reaproveita as referências antigas de set_date()/
    set_time()) os campos reais de data/hora do YouTube Studio, lê o que
    está EFETIVAMENTE exibido agora, e devolve um datetime comparável a
    ``target_dt``. Levanta RuntimeError com uma mensagem clara -- nunca
    contendo HTML bruto, cookie ou token -- se os campos não puderem ser
    relocalizados, estiverem vazios, ou o texto não puder ser interpretado
    com segurança. O chamador (schedule_one) deve tratar qualquer uma
    dessas falhas como motivo para NÃO chamar click_done().
    """
    trigger = page.locator("#datepicker-trigger")
    if trigger.count() == 0:
        trigger = page.locator("ytcp-visibility-scheduler ytcp-text-dropdown-trigger").first
    if trigger.count() == 0:
        raise RuntimeError(
            "Não consegui relocalizar o seletor de data do YouTube para "
            "confirmar o agendamento antes do clique final."
        )

    try:
        trigger.first.click()
        time.sleep(.4)
    except Exception:
        pass

    date_input = visible_first(page, [
        "ytcp-date-picker input",
        'tp-yt-paper-dialog input[type="text"]',
        "ytcp-date-picker tp-yt-paper-input input",
    ], timeout=6)

    date_text = ""
    if date_input is not None:
        try:
            date_text = date_input.input_value() or ""
        except Exception:
            date_text = ""
        if not date_text:
            try:
                date_text = date_input.get_attribute("value") or ""
            except Exception:
                date_text = ""

    # Fecha o seletor de data sem confirmar nada novo (Escape, não Enter).
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    time.sleep(.2)

    time_field = _locate_time_field_again(page)
    time_text = ""
    if time_field is not None:
        try:
            time_text = time_field.input_value() or ""
        except Exception:
            time_text = ""
        if not time_text:
            try:
                time_text = time_field.get_attribute("value") or ""
            except Exception:
                time_text = ""

    if date_input is None or not date_text:
        raise RuntimeError(
            "Não consegui reler o campo de data do YouTube para confirmar "
            "o horário antes do clique final (campo vazio ou ilegível)."
        )
    if time_field is None or not time_text:
        raise RuntimeError(
            "Não consegui reler o campo de horário do YouTube para "
            "confirmar antes do clique final (campo vazio ou ilegível)."
        )

    try:
        day, month, year = parse_observed_date_text(date_text)
    except ValueError as exc:
        raise RuntimeError(f"Data exibida no YouTube não pôde ser interpretada: {exc}") from exc
    try:
        hour, minute = parse_observed_clock_text(time_text)
    except ValueError as exc:
        raise RuntimeError(f"Horário exibido no YouTube não pôde ser interpretado: {exc}") from exc

    try:
        observed = target_dt.replace(
            year=year, month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0
        )
    except ValueError as exc:
        raise RuntimeError(f"Data/hora observada no YouTube é inválida: {exc}") from exc
    return observed


def click_done(page):
    btn = page.locator("#done-button")
    if btn.count() == 0:
        for pat in [r"programar", r"agendar", r"schedule", r"salvar", r"save"]:
            loc = page.get_by_role("button", name=re.compile(pat, re.I))
            if loc.count():
                btn = loc
                break

    if btn.count() == 0:
        save_debug(page, "done_ausente")
        raise RuntimeError("Não achei botão final PROGRAMAR.")

    end = time.time() + 180
    checked_limit = 0
    while time.time() < end:
        try:
            if btn.first.is_visible() and btn.first.is_enabled():
                btn.first.click()
                return
        except Exception:
            pass

        if time.time() - checked_limit > 3:
            checked_limit = time.time()
            if detect_daily_upload_limit(page):
                save_debug(page, "done_bloqueado_limite_diario")
                raise RuntimeError(
                    "Botão final travado: o YouTube mostrou aviso de "
                    "limite diário de upload."
                )

        time.sleep(1)

    save_debug(page, "done_desabilitado")
    raise RuntimeError("Botão final permaneceu desabilitado.")

def confirm_success(page, expected_title, target_dt):
    """
    V17: confirmação RÍGIDA.

    Não considera mais "modal fechou" como sucesso.
    Depois de clicar em Programar, verifica o status do vídeo na tela
    Conteúdo do canal. Só aceita explicitamente Programado/Scheduled.
    Rascunho/Draft é falha.
    """
    end = time.time() + 60
    expected_lower = (expected_title or "").strip().lower()

    def normalize(txt):
        return re.sub(r"\s+", " ", (txt or "")).strip().lower()

    while time.time() < end:
        # Confirmação explícita em toast/dialog é válida.
        try:
            body = normalize(page.locator("body").inner_text(timeout=2500))
        except Exception:
            body = ""

        if any(x in body for x in [
            "vídeo programado",
            "vídeo agendado",
            "video scheduled",
        ]):
            print("    YouTube confirmou explicitamente o agendamento.")
            return True

        # Se voltou para Conteúdo do canal, verifica a linha do título.
        url = (page.url or "").lower()
        if "studio.youtube.com" in url and ("/videos" in url or "/content" in url):
            # Tenta localizar uma linha ytcp-video-row pelo título exato/parcial.
            rows = page.locator("ytcp-video-row")
            try:
                row_count = rows.count()
            except Exception:
                row_count = 0

            for i in range(min(row_count, 80)):
                row = rows.nth(i)
                try:
                    if not row.is_visible():
                        continue
                    txt = normalize(row.inner_text(timeout=1500))
                except Exception:
                    continue

                if expected_lower and expected_lower not in txt:
                    continue

                if any(x in txt for x in ["rascunho", "draft"]):
                    print("    ERRO: o YouTube salvou este vídeo como RASCUNHO.")
                    save_debug(page, "video_ficou_rascunho")
                    return False

                if any(x in txt for x in [
                    "programado", "scheduled", "agendado"
                ]):
                    print("    Status confirmado na lista: PROGRAMADO.")
                    return True

            # Fallback sem ytcp-video-row: sobe do texto do título para um ancestral.
            if expected_title:
                try:
                    title_loc = page.get_by_text(expected_title, exact=True)
                    for i in range(min(title_loc.count(), 10)):
                        el = title_loc.nth(i)
                        if not el.is_visible():
                            continue
                        container = el.locator("xpath=ancestor::*[self::ytcp-video-row or @role='row'][1]")
                        if container.count():
                            txt = normalize(container.first.inner_text(timeout=1500))
                            if any(x in txt for x in ["rascunho", "draft"]):
                                save_debug(page, "video_ficou_rascunho")
                                return False
                            if any(x in txt for x in ["programado", "scheduled", "agendado"]):
                                return True
                except Exception:
                    pass

        time.sleep(1)

    save_debug(page, "sem_confirmacao_programado")
    return False

def detect_daily_upload_limit(page):
    """
    Detecta mensagens comuns de limite diário de upload do YouTube.
    """
    try:
        body = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
        return False

    terms = [
        "limite diário de upload",
        "limite diário de envio",
        "limite diário de vídeos",
        "limite de carregamento diário",
        "carregamento diário atingido",
        "você atingiu seu limite diário",
        "atingiu o limite diário",
        "validação única",
        "não é possível carregar mais vídeos hoje",
        "não é possível enviar mais vídeos hoje",
        "limite de publicações diárias",
        "limite diário de publicações",
        "tente novamente amanhã",
        "try again tomorrow",
        "daily upload limit",
        "you've reached your daily upload limit",
        "you have reached your daily upload limit",
        "upload limit reached",
        "daily limit reached",
        "one-time verification",
        "can't upload more videos today",
        "cannot upload more videos today",
    ]
    return any(t in body for t in terms)


class DailyLimitAfterScheduleError(RuntimeError):
    """
    O vídeo virou Rascunho logo depois de clicar em Programar. Na prática
    isso quase sempre é o limite diário de upload do YouTube para canais
    novos/não verificados: o primeiro vídeo do dia consegue ser
    agendado, mas os seguintes são aceitos no assistente e depois
    revertidos para Rascunho pelo próprio YouTube.
    """
    pass


class CopyrightClaimError(RuntimeError):
    """
    O YouTube sinalizou correspondência de Content ID / direitos autorais
    de terceiros na etapa 'Verificações' do assistente de upload.

    NOTA (GATE 19.5, rodada de política de copyright): esta exceção não é
    mais levantada por schedule_one() -- foi substituída por
    YouTubeChecksBlockedError (que carrega um estado CHECK_*/reason
    utilizável pela política tiktok_copyright_warning_policy-equivalente).
    A classe continua aqui, funcional, só por compatibilidade -- nenhum
    código do projeto a referencia mais.
    """
    pass


# Termos compartilhados entre o classificador antigo (check_copyright_claims,
# mantido por compatibilidade) e o novo, por-política
# (_classify_youtube_copyright_body / _read_youtube_checks_status).
YOUTUBE_CHECK_OK_TERMS = [
    "nenhum problema encontrado",
    "não encontrámos problemas",
    "não encontramos problemas",
    "no issues found",
]
# Ainda checando/carregando: enquanto esses termos aparecerem, não dá pra
# confiar no restante do texto da página.
YOUTUBE_CHECK_CHECKING_TERMS = [
    "verificando",
    "a verificar",
    "a processar",
    "aguarde",
    "checking",
    "processing",
]
# GATE 19.5 -- separação obrigatória (antes misturada em claim_terms):
# "restrito/bloqueado em algum país" é um bloqueio REAL de exibição, não um
# simples aviso -- precisa ser CHECK_FAILED, nunca CHECK_WARNING, e portanto
# nunca elegível à política ALLOW.
YOUTUBE_CHECK_FAILED_TERMS = [
    "restrito em alguns países",
    "bloqueado em alguns países",
]
# Reivindicação de terceiros/Content ID sem bloqueio de exibição -- pode ser
# publicável (ex.: monetização redirecionada), candidato a CHECK_WARNING.
# Evitamos palavras soltas como "direitos autorais"/"copyright" porque são o
# título fixo da seção e aparecem sempre, com ou sem problema.
YOUTUBE_CHECK_WARNING_TERMS = [
    "reivindicou",
    "reivindicado por",
    "terceiros reivindicaram",
    "uma reivindicação de direitos autorais foi feita",
    "conteúdo de terceiros foi encontrado",
    "correspondência de conteúdo foi encontrada",
    "correspondências de conteúdo foram encontradas",
    "foi encontrado conteúdo reivindicado",
    "conteúdo reivindicado",
    "third-party content was found",
    "a content match was found",
    "claimed content was found",
    "copyright claim",
]


def check_copyright_claims(page, max_wait=45):
    """
    Lê a etapa 'Verificações' do assistente de upload e tenta detectar se
    o YouTube já encontrou correspondência de Content ID / direitos
    autorais de terceiros (ex.: música com copyright).

    Retorna (tem_problema: bool, detalhe: str).

    LIMITAÇÃO IMPORTANTE: essa verificação só pega o que o YouTube já
    identificou até este ponto do assistente. A varredura completa de
    Content ID continua em segundo plano depois de publicado/agendado,
    então isso reduz o risco mas não elimina 100% a chance de uma
    reivindicação aparecer depois.

    IMPORTANTE (falso positivo corrigido): o título da seção nessa etapa
    já contém as palavras "direitos autorais" / "copyright" / "content
    id" mesmo quando NÃO há nenhum problema — são só os rótulos fixos da
    tela. Por isso não basta procurar essas palavras soltas no texto da
    página; é preciso (1) esperar a checagem terminar de carregar e
    (2) procurar por frases que só aparecem quando existe mesmo uma
    reivindicação, e confirmar a leitura duas vezes seguidas antes de
    decidir.

    NOTA (GATE 19.5, rodada de política de copyright): mantida por
    compatibilidade, com o MESMO comportamento observável de antes desta
    rodada (inclusive falha ABERTA -- "seguindo em frente" -- se o timeout
    expirar sem uma leitura conclusiva). schedule_one() não usa mais esta
    função -- usa _read_youtube_checks_status(), que classifica FAILED
    separado de WARNING e falha FECHADA (CHECK_UNKNOWN, nunca avança
    automaticamente) no timeout. Ver relatório para a justificativa dessa
    mudança de comportamento no caminho realmente executado.
    """
    end = time.time() + max_wait
    last_hit = None
    combined_claim_terms = YOUTUBE_CHECK_FAILED_TERMS + YOUTUBE_CHECK_WARNING_TERMS
    while time.time() < end:
        try:
            body = (page.locator("body").inner_text(timeout=2000) or "").lower()
        except Exception:
            body = ""

        if any(t in body for t in YOUTUBE_CHECK_CHECKING_TERMS):
            last_hit = None
            time.sleep(1)
            continue

        if any(t in body for t in YOUTUBE_CHECK_OK_TERMS):
            return False, "Nenhum problema encontrado pelo YouTube."

        hit = next((t for t in combined_claim_terms if t in body), None)
        if hit:
            if last_hit == hit:
                return True, f"YouTube sinalizou possível direito autoral/Content ID ({hit})."
            last_hit = hit
            time.sleep(1)
            continue

        last_hit = None
        time.sleep(1)

    return False, f"Verificação não conclusiva em {max_wait}s; seguindo em frente."


# ----------------------------------------------------------------------------
# GATE 19.5 -- ESTÁGIO 2 (continuação) -- política de aviso de direitos
# autorais + item bloqueado, agora também para o YouTube.
#
# Diferença arquitetural deliberada em relação ao TikTok (não presumida --
# investigada nesta rodada): o TikTok expõe uma seção "Verificações" com
# MÚLTIPLOS itens (copyright, conteúdo, ...), cada um com estado observável
# via polling em tempo real ANTES do clique final -- por isso lá existe um
# CHECK_PENDING "vivo" que pode ser lido repetidamente. O YouTube, nesta
# etapa do assistente de upload, expõe uma verificação ÚNICA (Content ID /
# direitos autorais de terceiros) e a varredura completa continua em
# segundo plano DEPOIS de publicado/agendado -- não existe, nesta etapa, um
# segundo estado observável de espera equivalente. O que o assistente
# oferece é: "ainda carregando esta etapa" (tratado como PENDING só até
# max_wait) e, depois disso, uma leitura que já é o resultado final do que
# foi identificado até aqui. Essa limitação é documentada, não inventada:
# se o texto não bater com nenhum padrão reconhecido, o resultado é
# CHECK_UNKNOWN (nunca PASSED por omissão), exatamente como no TikTok.
# ----------------------------------------------------------------------------

CHECK_PASSED = "PASSED"
CHECK_WARNING = "WARNING"
CHECK_FAILED = "FAILED"
CHECK_UNKNOWN = "UNKNOWN"
CHECK_PENDING = "PENDING"

# GATE 19.5 -- correção crítica (evidência real: conta NextIdea, 006.mp4):
# 45s era o default há várias rodadas, tanto na assinatura de
# _read_youtube_checks_status() quanto no template de config de conta nova
# (painel_oficial.default_config()). O próprio YouTube informa, na mesma
# tela, que a verificação de direitos autorais pode levar "até 10 minutos"
# ("Faltam 10 minutos." -- HTML de evidência desta rodada). Com
# youtube_blocked_item_policy agora sempre forçado a SKIP_AND_CONTINUE (ver
# rodada anterior), um timeout de 45s contra um processo de até 600s fazia
# a MAIORIA dos vídeos (não uma exceção) ser lida como CHECK_UNKNOWN e
# movida para quarentena, sem nenhum problema real de direitos autorais.
# Novo piso: 600s (o próprio teto anunciado pelo YouTube) + 60s de margem
# = 660s. Configurável por conta (ver
# painel_oficial.configurar_verificacao_direitos_autorais_youtube(), opção
# 8 do menu principal); o valor aqui é só o fallback quando a conta não
# tem a chave configurada (contas antigas).
YOUTUBE_COPYRIGHT_CHECK_TIMEOUT_PADRAO_SEGUNDOS = 660
# Intervalo entre avisos de progresso impressos no terminal enquanto o
# estado observado continua sendo "ainda verificando" -- evita que o
# terminal fique mudo por até 11 minutos parecendo travado.
YOUTUBE_COPYRIGHT_CHECK_PROGRESSO_INTERVALO_SEGUNDOS = 30

YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK = "BLOCK"
YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW = "ALLOW"
YOUTUBE_COPYRIGHT_WARNING_POLICIES = frozenset(
    {YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK, YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW}
)

YOUTUBE_BLOCKED_ITEM_POLICY_STOP_BATCH = "STOP_BATCH"
YOUTUBE_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE = "SKIP_AND_CONTINUE"
YOUTUBE_BLOCKED_ITEM_POLICIES = frozenset(
    {YOUTUBE_BLOCKED_ITEM_POLICY_STOP_BATCH, YOUTUBE_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE}
)


def _youtube_copyright_warning_policy_from_cfg(cfg):
    """Lê youtube_copyright_warning_policy da config da CONTA. Valor
    ausente/inválido cai no padrão mais seguro (BLOCK) -- nunca falha
    aberto para ALLOW."""
    value = str(cfg.get("youtube_copyright_warning_policy", YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK) or "").strip().upper()
    if value not in YOUTUBE_COPYRIGHT_WARNING_POLICIES:
        return YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK
    return value


def _youtube_blocked_item_policy_from_cfg(cfg):
    """Lê youtube_blocked_item_policy da config da conta.

    Padrão deliberadamente DIFERENTE do TikTok: SKIP_AND_CONTINUE, não
    STOP_BATCH. Antes desta rodada, o YouTube JÁ pulava e colocava em
    quarentena qualquer vídeo com problema de direitos autorais (ver
    main(), tratamento antigo de CopyrightClaimError) -- SKIP_AND_CONTINUE
    é o único default que preserva esse comportamento já existente sem
    mudar silenciosamente o que já funcionava (CLAUDE.md #4). O TikTok não
    tinha esse conceito antes desta rodada, por isso pôde usar STOP_BATCH
    (o mais conservador) como default novo sem regressão.
    """
    value = str(cfg.get("youtube_blocked_item_policy", YOUTUBE_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE) or "").strip().upper()
    if value not in YOUTUBE_BLOCKED_ITEM_POLICIES:
        return YOUTUBE_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE
    return value


class YouTubeChecksBlockedError(RuntimeError):
    """Levantado quando a verificação de direitos autorais/Content ID do
    YouTube resolve para um estado que não é seguro prosseguir, ou quando o
    assistente não confirma o estado dentro do timeout.

    ``reason`` é um rótulo estável usado pelo chamador de lote (main()) para
    aplicar youtube_blocked_item_policy:
      "failed"                  -- restrição/bloqueio real de exibição em
                                    países (nunca elegível a ALLOW).
      "copyright_warning_block" -- WARNING de terceiros/Content ID e a
                                    política configurada é BLOCK.
      "pending_timeout"         -- não deu pra confirmar um estado (inclui
                                    tanto "ainda carregando até o timeout"
                                    quanto "texto não reconhecido" -- nesta
                                    etapa do YouTube as duas causas não são
                                    distinguíveis com uma segunda leitura,
                                    ao contrário do TikTok).
    """

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


def _classify_youtube_copyright_body(body):
    """Classifica UMA ÚNICA leitura (já em minúsculas) do texto da etapa
    'Verificações' do YouTube. Unidade pura, sem polling/confirmação dupla
    (essa parte fica em _read_youtube_checks_status) -- existe para poder
    testar a classificação isoladamente.

    Prioridade: CHECKING (ainda carregando) > FAILED (bloqueio/restrição
    real de exibição em países) > PASSED (nenhum problema) > WARNING
    (reivindicação de terceiros/Content ID) > UNKNOWN (não bate com nenhum
    padrão conhecido). FAILED tem prioridade sobre WARNING mesmo quando o
    mesmo texto também menciona reivindicação de terceiros -- um vídeo
    restrito/bloqueado em países nunca deve virar um simples aviso.

    Devolve (status, hit) -- ``hit`` é o termo específico encontrado
    (FAILED/WARNING) ou None (PASSED/PENDING/UNKNOWN).
    """
    if any(t in body for t in YOUTUBE_CHECK_CHECKING_TERMS):
        return CHECK_PENDING, None
    failed_hit = next((t for t in YOUTUBE_CHECK_FAILED_TERMS if t in body), None)
    if failed_hit:
        return CHECK_FAILED, failed_hit
    if any(t in body for t in YOUTUBE_CHECK_OK_TERMS):
        return CHECK_PASSED, None
    warning_hit = next((t for t in YOUTUBE_CHECK_WARNING_TERMS if t in body), None)
    if warning_hit:
        return CHECK_WARNING, warning_hit
    return CHECK_UNKNOWN, None


def _read_youtube_checks_status(
    page,
    max_wait=YOUTUBE_COPYRIGHT_CHECK_TIMEOUT_PADRAO_SEGUNDOS,
    *,
    progress_every=YOUTUBE_COPYRIGHT_CHECK_PROGRESSO_INTERVALO_SEGUNDOS,
):
    """Lê a etapa 'Verificações' do YouTube com polling determinístico (nunca
    um sleep fixo gigante) e confirmação dupla para FAILED/WARNING (mesma
    disciplina anti-falso-positivo já usada em check_copyright_claims() --
    evita decidir com base numa transição de tela). PASSED é aceito na
    primeira leitura, igual ao comportamento anterior.

    Devolve (status, detalhe). status é um CHECK_* -- nunca CHECK_PASSED por
    ausência de informação. Ao expirar o timeout sem uma leitura conclusiva,
    devolve CHECK_UNKNOWN (falha FECHADA -- diferente do comportamento
    antigo de check_copyright_claims(), que seguia em frente; ver
    YouTubeChecksBlockedError e o relatório desta rodada para a
    justificativa).

    GATE 19.5 (correção desta rodada): o default de `max_wait` deixou de
    ser 45s -- valor incompatível com o próprio YouTube anunciar "até 10
    minutos" para esta verificação (evidência real: conta NextIdea,
    006.mp4). A classificação em si (`_classify_youtube_copyright_body`,
    `YOUTUBE_CHECK_*_TERMS`) NÃO foi alterada -- só o prazo dado antes de
    desistir, e um aviso periódico de progresso (`progress_every`) para o
    terminal não parecer travado durante uma espera agora bem mais longa.
    """
    start = time.time()
    end = start + max_wait
    last_hit = None
    next_progress_at = start + progress_every
    while time.time() < end:
        try:
            body = (page.locator("body").inner_text(timeout=2000) or "").lower()
        except Exception:
            body = ""

        status, hit = _classify_youtube_copyright_body(body)

        if status == CHECK_PENDING:
            last_hit = None
            now = time.time()
            if progress_every > 0 and now >= next_progress_at:
                elapsed = int(now - start)
                print(f"    Ainda verificando direitos autorais... ({elapsed}s decorridos)")
                next_progress_at = now + progress_every
            time.sleep(1)
            continue
        if status == CHECK_PASSED:
            return CHECK_PASSED, "Nenhum problema encontrado pelo YouTube."
        if status == CHECK_FAILED:
            if last_hit == ("FAILED", hit):
                return CHECK_FAILED, f"YouTube sinalizou restrição/bloqueio de exibição em países ({hit})."
            last_hit = ("FAILED", hit)
            time.sleep(1)
            continue
        if status == CHECK_WARNING:
            if last_hit == ("WARNING", hit):
                return CHECK_WARNING, f"YouTube sinalizou possível direito autoral/Content ID ({hit})."
            last_hit = ("WARNING", hit)
            time.sleep(1)
            continue

        last_hit = None
        time.sleep(1)

    return CHECK_UNKNOWN, f"Verificação não conclusiva em {max_wait}s (estado não confirmado)."


def decide_youtube_checks_outcome(status, copyright_warning_policy=YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK):
    """Decide o que fazer com um status JÁ RESOLVIDO (não-PENDING) da
    verificação de direitos autorais do YouTube.

    Devolve (outcome, reason):
      ("PROCEED", None)                      -- seguro prosseguir.
      ("BLOCKED", "failed")                  -- restrição/bloqueio real.
      ("BLOCKED", "unknown")                 -- não determinado.
      ("BLOCKED", "copyright_warning_block") -- WARNING com política BLOCK.

    ALLOW só produz PROCEED para CHECK_WARNING -- nunca para
    FAILED/UNKNOWN, mesmo com ALLOW configurado.
    """
    if status == CHECK_PASSED:
        return "PROCEED", None
    if status == CHECK_FAILED:
        return "BLOCKED", "failed"
    if status == CHECK_WARNING:
        if copyright_warning_policy == YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW:
            return "PROCEED", None
        return "BLOCKED", "copyright_warning_block"
    return "BLOCKED", "unknown"


def _youtube_batch_action_for_blocked(blocked_policy):
    """Decisão pura (sem efeito colateral) de lote diante de um
    YouTubeChecksBlockedError: "SKIP" ou "STOP", conforme
    youtube_blocked_item_policy."""
    if blocked_policy == YOUTUBE_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE:
        return "SKIP"
    return "STOP"


def discard_current_upload(page):
    """
    Tenta cancelar/apagar o rascunho atual diretamente no assistente de
    upload (ícone de lixeira/eliminar no cabeçalho do modal), para não
    deixar um rascunho órfão ocupando espaço no canal.

    GATE 19.5 (investigação desta rodada, Bug B): confirmado com dois HTML
    reais (`*_bloqueado_direitos_autorais.html` e
    `*_falha_descartar_direitos_autorais.html`, conta NextIdea, 006.mp4),
    capturados pelo próprio programa no exato momento em que esta função
    roda -- na etapa "Verificações" do assistente de upload, NENHUM botão
    real com aria-label ou texto "Eliminar"/"Apagar"/"Descartar" existe.
    As únicas ocorrências de "Delete"/"Discard" no DOM inteiro são
    substrings de nomes de classe CSS internos do YouTube (ex.:
    `ytVideoFilesDialogDiscardButtonContainer`), nunca um botão visível
    com esse aria-label/texto -- ou seja, esta função está fadada a
    retornar `False` SEMPRE que chamada NESTA etapa específica do
    assistente, não é uma falha ocasional de seletor desatualizado.

    Não foi possível, nesta rodada, obter uma captura real da tela de
    listagem de conteúdo/rascunhos do Studio (fora deste modal) para
    confirmar se o apagar funciona por lá -- os seletores abaixo
    permanecem como best-effort (cobrem outras versões/estados do Studio
    onde um botão real pode existir) e o `close_btn`/Escape tenta ao menos
    fechar o modal sem deixá-lo pendurado. Mas o CHAMADOR (`schedule_one`)
    agora sempre confere o valor de retorno: quando é `False`, avisa
    explicitamente no terminal e registra em
    `direitos_autorais_bloqueados.txt` que o rascunho NÃO foi removido
    automaticamente -- nunca falha em silêncio como antes desta rodada.
    """
    delete_selectors = [
        'button[aria-label*="Eliminar" i]',
        'button[aria-label*="Apagar" i]',
        'button[aria-label*="Delete" i]',
        'ytcp-icon-button[aria-label*="Eliminar" i]',
        'ytcp-icon-button[aria-label*="Apagar" i]',
        'ytcp-icon-button[aria-label*="Delete" i]',
        'ytcp-button[aria-label*="Eliminar" i]',
    ]

    clicked = False
    for sel in delete_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                clicked = True
                break
        except Exception:
            pass

    if not clicked:
        try:
            close_btn = page.locator(
                'button[aria-label*="Fechar" i], button[aria-label*="Close" i]'
            )
            if close_btn.count() and close_btn.first.is_visible():
                close_btn.first.click()
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    time.sleep(.8)

    for text in ["Eliminar", "Apagar", "Delete", "Descartar", "Discard"]:
        try:
            loc = page.get_by_role("button", name=re.compile(rf"^{text}$", re.I))
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                time.sleep(1)
                return True
        except Exception:
            pass

    if not clicked:
        save_debug(page, "falha_descartar_direitos_autorais")

    return clicked


def schedule_one(page, cfg, item, title, description, target_dt):
    video = item.get("upload_path") or item["path"]
    print(f"\n[+] {item['path'].name} -> {target_dt:%d/%m/%Y %H:%M}")
    if video != item["path"]:
        print(f"    (enviando versão legendada: {video.relative_to(BASE)})")
    wait_upload_page(page)

    upload_video_file(page, video)

    title_box = visible_first(page, [
        "#title-textarea #textbox",
        "ytcp-social-suggestions-textbox#title-textarea #textbox"
    ], timeout=60)
    if title_box is None:
        save_debug(page, "titulo_ausente")
        raise RuntimeError("Não achei o campo de título.")

    fill_editable(title_box, title)

    desc_box = visible_first(page, [
        "#description-textarea #textbox",
        "ytcp-social-suggestions-textbox#description-textarea #textbox"
    ], timeout=10)
    if desc_box is not None:
        fill_editable(desc_box, description)

    if cfg.get("nao_e_para_criancas", True):
        choose_not_for_kids(page)

    # Detalhes -> elementos
    click_next(page)
    click_next(page)

    # elementos -> verificações: aqui o YouTube mostra se já encontrou
    # correspondência de Content ID / direitos autorais de terceiros.
    if cfg.get("cancelar_video_com_direitos_autorais", True):
        copyright_warning_policy = _youtube_copyright_warning_policy_from_cfg(cfg)
        if copyright_warning_policy == YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW:
            # GATE 19.5 (correção desta rodada, decisão explícita do
            # usuário): com ALLOW, não espera nem lê a verificação NENHUM
            # POUCO -- zero espera, segue direto para o próximo clique.
            # Risco aceito e documentado pelo usuário: um bloqueio REAL
            # (ex.: CHECK_FAILED por restrição de exibição em países) só
            # apareceria depois de publicado, se aparecer -- diferente de
            # CHECK_WARNING (aviso comum de Content ID de terceiros), que
            # já era tolerado com ALLOW mesmo antes desta mudança. Não
            # implementar uma checagem parcial "mais segura" aqui -- não
            # foi isso que o usuário pediu.
            print(
                "    Verificação de direitos autorais: PULADA "
                "(política desta execução = ignorar avisos)."
            )
        else:
            status, detalhe = _read_youtube_checks_status(
                page,
                max_wait=int(cfg.get(
                    "timeout_verificacao_direitos_autorais_segundos",
                    YOUTUBE_COPYRIGHT_CHECK_TIMEOUT_PADRAO_SEGUNDOS,
                )),
            )
            print(f"    Verificação de direitos autorais: {detalhe}")

            outcome, reason = decide_youtube_checks_outcome(status, copyright_warning_policy)
            if outcome == "PROCEED":
                if status == CHECK_WARNING:
                    print("    COPYRIGHT WARNING")
                    print(f"    POLICY = {copyright_warning_policy}")
                    print("    ACTION = CONTINUE")
            else:
                save_debug(page, "bloqueado_direitos_autorais")
                # GATE 19.5 (Bug B, correção desta rodada): o retorno de
                # discard_current_upload() agora é sempre conferido -- ver
                # a docstring dela para a evidência real de por que ela
                # falha sempre nesta etapa. Quando não consegue apagar, o
                # aviso vai tanto para o terminal quanto para o mesmo
                # `detalhe` que acaba registrado em
                # direitos_autorais_bloqueados.txt por quem chama em lote
                # (process_prepared_batch), para dar pra auditar depois.
                descartado = discard_current_upload(page)
                if not descartado:
                    aviso = (
                        "rascunho NÃO removido automaticamente -- pode ter ficado "
                        "salvo como privado no canal; revise manualmente no YouTube Studio."
                    )
                    print(f"    [AVISO] {aviso}")
                    detalhe = f"{detalhe} | {aviso}"
                raise YouTubeChecksBlockedError(f"{video.name}: {detalhe}", reason)

    # verificações -> visibilidade
    click_next(page)

    choose_schedule(page)
    set_date(page, target_dt)
    set_time(page, target_dt)

    # CANDIDATO C -- correção cirúrgica (Estágio 2, Parte "correção").
    # ANTES do clique final (click_done): relê data+hora efetivamente
    # exibidos na UI e compara com target_dt. Não basta ter clicado na opção
    # certa -- é preciso provar que a UI ficou no valor certo antes de
    # prosseguir. Valida DATA + HORA juntas (um horário certo em outra data
    # não deve passar).
    expected_dt = target_dt.replace(second=0, microsecond=0)
    observed_dt = read_observed_schedule_datetime(page, expected_dt)
    if observed_dt != expected_dt:
        save_debug(page, "horario_divergente_antes_de_confirmar")
        raise RuntimeError(
            "Abortado ANTES do clique final: a data/hora exibida no YouTube não bate "
            f"com a calculada. esperado: {expected_dt:%d/%m/%Y %H:%M} | "
            f"observado: {observed_dt:%d/%m/%Y %H:%M}"
        )

    # Rede de segurança: confirma que o diálogo de upload ainda está na
    # tela antes de tentar clicar em "Programar". Faz até 3 tentativas
    # rápidas (o check anterior podia pegar um instante de transição e
    # achar, errado, que o diálogo tinha fechado). Se depois de 3
    # tentativas ele realmente não achar o diálogo, só avisa no log —
    # NÃO trava mais a fila inteira por conta disso, porque um falso
    # positivo aqui é pior do que deixar o click_done() tentar mesmo
    # assim (ele tem sua própria checagem e mensagem de erro).
    dialog_visible = False
    for _ in range(3):
        try:
            dialog_visible = page.locator("ytcp-uploads-dialog").first.is_visible()
        except Exception:
            dialog_visible = False
        if dialog_visible:
            break
        time.sleep(0.7)
    if not dialog_visible:
        save_debug(page, "dialogo_upload_talvez_fechado")
        print("    [AVISO] Não confirmei visualmente o diálogo de upload; tentando concluir mesmo assim.")

    print(f"    Programar: {target_dt:%d/%m/%Y %H:%M}")
    click_done(page)

    if not confirm_success(page, title, target_dt):
        raise RuntimeError(
            "O YouTube não confirmou o status PROGRAMADO (pode ter salvo como rascunho). "
            "O vídeo não será marcado como concluído."
        )

    print("    OK: agendamento confirmado.")

def _ask_interactive_copyright_warning_policy(input_fn=input):
    """Pergunta única, feita a CADA chamada de main() (nunca cacheada em
    memória ou em disco), se avisos de direitos autorais de terceiros devem
    ser ignorados (ALLOW) ou bloquear o vídeo (BLOCK) NESTA execução.

    Fail-closed: resposta vazia, não reconhecida, ou qualquer exceção ao
    ler a entrada (ex.: stdin fechado) resolve para BLOCK -- nunca ALLOW
    por omissão. Aceita "sim"/"s"/"yes"/"y" (case-insensitive) como ALLOW;
    qualquer outra coisa é BLOCK.

    `input_fn` é injetável para testes (nunca chama o `input()` real numa
    suíte automatizada).
    """
    try:
        resposta = input_fn(
            "Ignorar avisos de direitos autorais NESTA execução? "
            "(não fica salvo, s/N): "
        )
    except Exception:
        resposta = ""
    resposta = str(resposta or "").strip().lower()
    if resposta in ("s", "sim", "y", "yes"):
        return YOUTUBE_COPYRIGHT_WARNING_POLICY_ALLOW
    return YOUTUBE_COPYRIGHT_WARNING_POLICY_BLOCK


def _cfg_for_interactive_run(cfg, copyright_policy):
    """Devolve uma CÓPIA RASA de cfg com a política de direitos autorais
    desta execução aplicada apenas em memória -- nunca passada para
    save_json(). youtube_blocked_item_policy é sempre forçada para
    SKIP_AND_CONTINUE neste fluxo interativo: tanto vídeos bloqueados por
    aviso de direitos autorais quanto problemas reais de verificação
    (FAILED/UNKNOWN -- rede, país restrito, etc.) devem pular apenas aquele
    vídeo e continuar o lote, nunca parar a fila inteira por causa de um
    único vídeo com problema de verificação.

    O dict `cfg` original passado aqui NUNCA é mutado por esta função.
    """
    cfg_for_run = dict(cfg)
    cfg_for_run["youtube_copyright_warning_policy"] = copyright_policy
    cfg_for_run["youtube_blocked_item_policy"] = YOUTUBE_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE
    return cfg_for_run


def reconcile_visible_drafts(page, state):
    """
    Remove do estado local registros que aparecem como Rascunho/Draft na
    página atual de Conteúdo do canal. Isso permite que esses arquivos voltem
    para a fila na próxima execução.
    """
    scheduled = state.get("scheduled", [])
    if not scheduled:
        return 0

    # Mapa por título; títulos podem repetir, então guarda listas.
    by_title = {}
    for rec in scheduled:
        title = (rec.get("title") or "").strip()
        if title:
            by_title.setdefault(title.lower(), []).append(rec)

    if not by_title:
        return 0

    removed_ids = set()
    rows = page.locator("ytcp-video-row")
    try:
        count = rows.count()
    except Exception:
        count = 0

    for i in range(min(count, 100)):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
            txt = re.sub(r"\s+", " ", row.inner_text(timeout=1500)).strip()
            low = txt.lower()
        except Exception:
            continue

        if "rascunho" not in low and "draft" not in low:
            continue

        for title_low, recs in by_title.items():
            if title_low and title_low in low:
                for rec in recs:
                    removed_ids.add(id(rec))

    if not removed_ids:
        return 0

    state["scheduled"] = [r for r in scheduled if id(r) not in removed_ids]
    save_json(STATE_FILE, state)
    print(f"[REPARO] Removi {len(removed_ids)} registro(s) do estado porque estão como RASCUNHO no YouTube.")
    return len(removed_ids)


def main(input_fn=input):
    """Ponto de entrada interativo do agendador do YouTube.

    GATE 19.5 (continuação) -- pergunta ÚNICA, feita em TODA chamada de
    main() (sem cache/memória entre chamadas, mesmo para a mesma conta no
    mesmo processo), se avisos de direitos autorais de terceiros devem ser
    ignorados NESTA execução. A resposta NUNCA é gravada em CONFIG_FILE --
    ver _cfg_for_interactive_run(). youtube_blocked_item_policy é sempre
    forçada para SKIP_AND_CONTINUE neste fluxo (tanto para avisos de
    direitos autorais quanto para problemas reais de verificação --
    FAILED/UNKNOWN), para que um único vídeo com problema nunca pare o
    lote inteiro quando o usuário está operando pelo menu interativo.

    `input_fn` é injetável para testes (nunca chama o `input()` real numa
    suíte automatizada); o valor padrão é o `input()` real do Python.

    O mecanismo antigo baseado em CONFIG_FILE
    (youtube_copyright_warning_policy / youtube_blocked_item_policy
    persistidos em disco) continua existindo e funcionando normalmente
    para uso programático/não-interativo -- ver process_prepared_batch(),
    que lê esses valores diretamente de `cfg` e não sabe nada sobre esta
    pergunta interativa.
    """
    cfg = load_json(CONFIG_FILE, {})

    # Pergunta feita ANTES de qualquer outra coisa, sempre -- nunca lida de
    # memória/cache. A resposta só existe na variável local abaixo; nunca é
    # atribuída a `cfg` (o único dict jamais passado para save_json()).
    copyright_policy_this_run = _ask_interactive_copyright_warning_policy(input_fn)
    print(
        "Política desta execução (não salva): "
        f"direitos autorais = {copyright_policy_this_run}, "
        f"itens bloqueados = {YOUTUBE_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE}"
    )

    try:
        _timezone_name, timezone_changed = ensure_timezone_config(cfg)
    except MissingTimezoneConfigurationError as exc:
        print(f"[ERRO] {exc}")
        print("Configure timezone_iana explicitamente nesta conta antes de agendar.")
        return
    if timezone_changed:
        save_json(CONFIG_FILE, cfg)

    # Cópia RASA só em memória (ver _cfg_for_interactive_run) -- `cfg`
    # original, o único jamais passado para save_json(), fica intocado.
    # Construída só agora, DEPOIS de ensure_timezone_config(cfg) já ter
    # finalizado cfg["timezone_iana"], para que a cópia não carregue um
    # valor de timezone desatualizado/ausente.
    cfg_for_batch = _cfg_for_interactive_run(cfg, copyright_policy_this_run)

    titles = load_titles()
    descriptions = load_descriptions()
    state = load_json(STATE_FILE, {"version": 7, "scheduled": []})
    items = list_items(cfg_for_batch)

    # Aplica os textos persistentes gerados pelo painel. Não existem subtítulos
    # nem versões alternativas de vídeo: sempre envia o arquivo original tratado.
    ai_map = load_json(AI_TITLES_FILE, {})
    if ai_map:
        aplicados = 0
        for it in items:
            entry = ai_map.get(it["fp"])
            if not isinstance(entry, dict) or entry.get("status") != "done":
                continue
            if entry.get("titulo"):
                it["ai_title"] = entry["titulo"]
            if entry.get("descricao"):
                tags = entry.get("hashtags") or []
                desc = str(entry["descricao"]).strip()
                if tags:
                    desc += "\n\n" + " ".join(str(x) for x in tags)
                it["ai_description"] = desc
            aplicados += 1
        if aplicados:
            print(f"[TEXTOS] {aplicados} vídeo(s) com título/descrição/# salvos no histórico.")

    done = {
        x.get("fingerprint")
        for x in state.get("scheduled", [])
        if x.get("fingerprint")
    }
    pending = [x for x in items if x["fp"] not in done]

    print("=" * 72)
    print(f"      YOUTUBE — {cfg_for_batch.get('nome_canal', BASE.name)}")
    print("=" * 72)
    print(f"Vídeos válidos na pasta : {len(items)}")
    print(f"Já agendados            : {len(done)}")
    print(f"Pendentes                : {len(pending)}")
    if cfg_for_batch.get("horarios_por_dia"):
        print("Estratégia                : horários otimizados por dia da semana")
        print("  Seg: " + ", ".join(cfg_for_batch["horarios_por_dia"].get("segunda", [])))
        print("  Ter: " + ", ".join(cfg_for_batch["horarios_por_dia"].get("terca", [])))
        print("  Qua: " + ", ".join(cfg_for_batch["horarios_por_dia"].get("quarta", [])))
        print("  Qui: " + ", ".join(cfg_for_batch["horarios_por_dia"].get("quinta", [])))
        print("  Sex: " + ", ".join(cfg_for_batch["horarios_por_dia"].get("sexta", [])))
        print("  Sáb: " + ", ".join(cfg_for_batch["horarios_por_dia"].get("sabado", [])))
        print("  Dom: " + ", ".join(cfg_for_batch["horarios_por_dia"].get("domingo", [])))
    else:
        print(f"Horários                 : {', '.join(cfg_for_batch.get('horarios', []))}")
    print()

    # Política impressa aqui já reflete os valores REALMENTE usados nesta
    # execução do lote (cfg_for_batch, com a resposta interativa aplicada
    # em memória) -- não os valores brutos persistidos em CONFIG_FILE.
    youtube_copyright_policy = _youtube_copyright_warning_policy_from_cfg(cfg_for_batch)
    youtube_blocked_policy = _youtube_blocked_item_policy_from_cfg(cfg_for_batch)
    print("Política YouTube (valores usados nesta execução do lote):")
    print(f"Avisos de direitos autorais: {youtube_copyright_policy}")
    print(f"Problemas impeditivos: {youtube_blocked_policy}")
    print()

    if not pending:
        print("Nada novo para agendar.")
        return 0

    max_run = int(cfg_for_batch.get("max_uploads_por_execucao", 0))
    if max_run > 0:
        pending = pending[:max_run]

    # Não bloqueia a fila inteira só porque existem vídeos futuros sem texto.
    # Agenda tudo que estiver pronto EM ORDEM e para antes do primeiro vídeo
    # ainda sem título/descrição. Assim a retomada continua correta e nenhum
    # número é pulado.
    prontos_em_ordem = []
    primeiro_sem_texto = None
    for x in pending:
        if x.get("ai_title") and x.get("ai_description"):
            prontos_em_ordem.append(x)
        else:
            primeiro_sem_texto = x
            break

    if not prontos_em_ordem:
        nome = primeiro_sem_texto["path"].name if primeiro_sem_texto else pending[0]["path"].name
        print(f"[ERRO] O PRÓXIMO vídeo da fila ({nome}) ainda não tem título/descrição gerados.")
        print("Use GERAR / EDITAR TEXTOS DAS POSTAGENS e rode novamente.")
        print("Os vídeos posteriores não impedem a postagem; somente o próximo da fila precisa estar pronto.")
        return 3

    if primeiro_sem_texto:
        print(
            f"[AVISO] {primeiro_sem_texto['path'].name} ainda está sem texto. "
            f"Vou agendar os {len(prontos_em_ordem)} vídeo(s) anteriores que já estão prontos e parar antes dele."
        )

    pending = prontos_em_ordem

    # Gera slots somente para o trecho pronto e contínuo da fila.
    slots = build_slots(cfg_for_batch, state, len(pending))

    if not slots:
        print("A janela configurada está cheia.")
        return 0

    return process_prepared_batch(cfg_for_batch, pending, slots, titles, descriptions, state)


def process_prepared_batch(cfg, pending, slots, titles, descriptions, state):
    """Executa o lote de uploads do YouTube já preparado (fila pronta e
    contínua, com título/descrição resolvidos, e horários já calculados em
    `slots`), usando as políticas presentes em `cfg`
    (youtube_copyright_warning_policy / youtube_blocked_item_policy).

    Extraída de main() nesta rodada (GATE 19.5, continuação) para existir
    como uma função própria e diretamente testável/chamável de forma
    programática -- exatamente como process_prepared_batch() já existe
    para o TikTok em agendar_tiktok.py. Isso prova e documenta
    explicitamente que o mecanismo antigo, baseado em CONFIG_FILE
    (cfg["youtube_blocked_item_policy"] podendo ser "STOP_BATCH"),
    continua funcionando normalmente para uso programático/não-interativo:
    quem chamar esta função diretamente com um cfg carregado do disco
    (sem passar por main() nem pela pergunta interativa) obtém o
    comportamento de STOP_BATCH intacto, sem nenhuma mudança de
    comportamento introduzida por esta rodada. main() é que constrói, só
    para o fluxo interativo, uma cópia rasa de cfg com
    youtube_blocked_item_policy forçada para SKIP_AND_CONTINUE (ver
    _cfg_for_interactive_run()) antes de chamar esta função -- esta função
    em si não sabe nem precisa saber que essa pergunta existe.

    Abre e fecha o próprio navegador (via Playwright) e cuida do reinício
    periódico do Chrome (reiniciar_chrome_cada); não recebe uma `page` já
    aberta porque esse reinício acontece no meio do lote.

    Devolve um código de saída inteiro: 0 = todos os vídeos permitidos
    nesta execução foram agendados; 2 = erro/parada (inclui STOP_BATCH);
    3 = limite diário do YouTube detectado.
    """
    from playwright.sync_api import sync_playwright

    youtube_blocked_policy = _youtube_blocked_item_policy_from_cfg(cfg)

    approx_days = (len(slots) + int(cfg.get("posts_por_dia", 3)) - 1) // int(cfg.get("posts_por_dia", 3))
    print(f"Fila desta execução       : {len(slots)} Short(s)")
    print(f"Dias ocupados aprox.      : {approx_days}")
    print(f"Primeiro horário          : {slots[0]:%d/%m/%Y %H:%M}")
    print(f"Último horário            : {slots[-1]:%d/%m/%Y %H:%M}")
    if int(cfg.get("max_uploads_por_execucao", 0)) == 0:
        print("Limite interno            : SEM LIMITE (até o YouTube bloquear)")
    else:
        print(f"Limite interno            : {cfg.get('max_uploads_por_execucao')}")
    print()

    with sync_playwright() as p:
        restart_every = int(cfg.get("reiniciar_chrome_cada", 25))
        context = None
        page = None
        successful_in_context = 0

        def open_browser():
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=not bool(cfg.get("navegador_visivel", True)),
                viewport={"width": 1440, "height": 1000},
                args=["--start-maximized"]
            )
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            return ctx, pg

        try:
            context, page = open_browser()

            # V17: tenta abrir Conteúdo do canal e corrige registros locais
            # que na verdade viraram rascunho.
            try:
                page.goto("https://studio.youtube.com/", wait_until="domcontentloaded", timeout=90000)
                time.sleep(3)

                # Abre Conteúdo se estivermos no painel inicial.
                content_link = page.get_by_text(re.compile(r"^(Conteúdo|Content)$", re.I), exact=True)
                if content_link.count() and content_link.first.is_visible():
                    content_link.first.click()
                    time.sleep(3)

                # Em algumas versões há abas Vídeos/Shorts; a lista padrão já serve.
                reconcile_visible_drafts(page, state)
            except Exception:
                pass

            for queue_index, (item, target_dt) in enumerate(zip(pending, slots), start=1):
                # Reinicia periodicamente para evitar Chrome pesado após centenas de uploads.
                if (
                    restart_every > 0
                    and successful_in_context >= restart_every
                ):
                    print(
                        f"\n[MANUTENÇÃO] {restart_every} uploads concluídos. "
                        "Reiniciando o Chrome..."
                    )
                    try:
                        context.close()
                    except Exception:
                        pass
                    time.sleep(2)
                    context, page = open_browser()
                    successful_in_context = 0

                idx = len(state.get("scheduled", [])) % len(titles)
                title = item.get("ai_title") or titles[idx]
                description = item.get("ai_description") or descriptions[idx % len(descriptions)]

                print(
                    f"\n[FILA {queue_index}/{len(pending)}] "
                    f"{item['path'].name}"
                )

                try:
                    schedule_one(page, cfg, item, title, description, target_dt)
                except YouTubeChecksBlockedError as e:
                    # GATE 19.5 (continuação): substitui o antigo
                    # CopyrightClaimError. youtube_blocked_item_policy
                    # decide entre pular (comportamento já existente antes
                    # desta rodada: quarentena + continua a fila) e parar o
                    # lote inteiro (novo, opt-in).
                    action = _youtube_batch_action_for_blocked(youtube_blocked_policy)
                    print(f"\n[BLOQUEADO] {e}")
                    print(item["path"].name)
                    print(f"Verificação: {e.reason}")
                    print(f"Política: {youtube_blocked_policy}")
                    if action == "STOP":
                        save_debug(page, "bloqueado_direitos_autorais")
                        print("Ação: STOPPED")
                        print(
                            "\nParei. O vídeo NÃO foi apagado nem marcado como concluído."
                        )
                        return 2

                    # SKIP_AND_CONTINUE -- mesmo comportamento de quarentena
                    # já existente antes desta rodada para o caso de
                    # direitos autorais: move o arquivo pra fora de videos\
                    # (nunca apagado, nunca reescrito) para não ser
                    # retentado indefinidamente, registra o motivo, segue.
                    blocked_dir = ACCOUNT_PATHS.blocked / "direitos_autorais"
                    blocked_dir.mkdir(exist_ok=True)
                    dest = blocked_dir / item["path"].name
                    try:
                        item["path"].rename(dest)
                        print(f"    Movido para: {dest.relative_to(BASE)}")
                    except Exception as move_err:
                        print(f"    [AVISO] Não consegui mover o arquivo: {move_err}")
                    try:
                        with (DATA_DIR / "direitos_autorais_bloqueados.txt").open(
                            "a", encoding="utf-8"
                        ) as f:
                            f.write(
                                f"{utc_now_iso()} | {e}\n"
                            )
                    except Exception:
                        pass
                    print("Ação: SKIPPED")
                    if queue_index < len(pending):
                        print(f"Próximo vídeo: {pending[queue_index]['path'].name}")
                    print("    Pulei este vídeo e continuei a fila.")
                    time.sleep(int(cfg.get("pausa_entre_posts_segundos", 8)))
                    continue
                except Exception as e:
                    # Se o YouTube bloqueou por limite diário, encerra LIMPO.
                    if detect_daily_upload_limit(page):
                        print("\n" + "=" * 72)
                        print("LIMITE DIÁRIO DO YOUTUBE DETECTADO")
                        print("=" * 72)
                        print(
                            "O vídeo atual NÃO foi marcado como concluído e NÃO foi apagado."
                        )
                        print(
                            "Na próxima execução, o programa continua dele e mantém "
                            "a sequência de datas."
                        )
                        save_debug(page, "limite_diario_youtube")
                        return 3

                    print(f"\n[ERRO] {item['path'].name}: {e}")
                    save_debug(page, f"erro_{item['path'].stem[:40]}")
                    print(
                        "Parei. O vídeo NÃO foi apagado nem marcado como concluído."
                    )
                    return 2

                schedule_time = canonical_schedule_fields(
                    target_dt,
                    timezone_name_from_config(cfg),
                    time_origin=SCHEDULE_TIME_MANUAL,
                )
                state.setdefault("scheduled", []).append({
                    "file": item["path"].name,
                    "fingerprint": item["fp"],
                    "title": title,
                    "description": description,
                    **schedule_time,
                    "duration": item.get("info", {}).get("duration"),
                    "registered_at": utc_now_iso(),
                })
                save_json(STATE_FILE, state)
                successful_in_context += 1

                if cfg.get("apagar_video_apos_agendar", False):
                    try:
                        item["path"].unlink()
                        print(f"    Apagado: {item['path'].name}")
                    except Exception as e:
                        print(f"    [AVISO] Não consegui apagar: {e}")

                remaining = len(pending) - queue_index
                print(
                    f"    PROGRESSO: {queue_index}/{len(pending)} | "
                    f"Restam nesta fila: {remaining}"
                )

                time.sleep(int(cfg.get("pausa_entre_posts_segundos", 8)))

        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    print("\nTodos os vídeos permitidos nesta execução foram agendados.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
    except Exception as e:
        print("\nERRO FATAL:", e)
        traceback.print_exc()
        input("\nENTER para sair...")
        raise
