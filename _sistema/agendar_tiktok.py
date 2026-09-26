# -*- coding: utf-8 -*-
"""
TikTok Auto Agendador
- 3 vídeos por dia (configurável)
- usa textos persistentes gerados pelo painel, por vídeo
- guarda estado para não repetir vídeos
- usa perfil persistente do Chrome para manter login
- não contorna CAPTCHA, 2FA ou desafios de segurança

IMPORTANTE:
A interface do TikTok muda com frequência. O script usa vários seletores/fallbacks
e salva screenshot/HTML em logs/ se não encontrar algum controle.
"""

from pathlib import Path
from datetime import timedelta
import hashlib
import json
import os
import re
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

BASE = account_dir_from_env(standalone_namespace="agendar_tiktok")
ACCOUNT_PATHS = account_paths(BASE)
DATA_DIR = ACCOUNT_PATHS.data
VIDEO_DIR = ACCOUNT_PATHS.videos
LOG_DIR = ACCOUNT_PATHS.logs
PROFILE_DIR = ACCOUNT_PATHS.profile_tiktok
STATE_FILE = DATA_DIR / "estado_tiktok.json"
CONFIG_FILE = ACCOUNT_PATHS.config
TEXTS_FILE = DATA_DIR / "textos_postagem.json"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}

def natural_key(p: Path):
    parts = re.split(r"(\d+)", p.name.lower())
    return [int(x) if x.isdigit() else x for x in parts]

def load_json(path, default):
    return load_state_json(path, default)

def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

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

def caption_for(video: Path, texts):
    fp = fingerprint(video)
    row = texts.get(fp) if isinstance(texts, dict) else None
    if not isinstance(row, dict) or row.get("status") != "done" or not row.get("caption"):
        return fp, None
    caption = str(row.get("caption") or "").strip()
    tags = row.get("hashtags") or []
    if tags:
        caption = (caption + " " + " ".join(str(x) for x in tags)).strip()
    return fp, caption

def visible(locator):
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False

def wait_any(page, selectors, timeout_ms=15000):
    end = time.time() + timeout_ms/1000
    while time.time() < end:
        for sel in selectors:
            loc = page.locator(sel)
            if visible(loc):
                return loc.first
        time.sleep(.25)
    return None

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
    print(f"[DEBUG] Salvei diagnóstico em: {png.name} / {html.name}")

def pause_for_human(page, message):
    print("\n" + "="*72)
    print(message)
    print("Resolva no navegador aberto e depois volte aqui.")
    input("Quando estiver pronto, pressione ENTER...")
    print("="*72 + "\n")

def ensure_login_and_upload_page(page, cfg):
    """
    Abre a página de upload usando a sessão já salva no perfil_chrome.

    O LOGIN NÃO deve ser feito dentro deste navegador controlado pelo Playwright,
    porque o fluxo de autenticação do TikTok pode travar/pausar o debugger.
    Para fazer login, use 1_LOGIN_TIKTOK_NORMAL.bat, feche o Chrome e só depois
    rode o agendador.
    """
    page.goto(cfg["upload_url"], wait_until="domcontentloaded", timeout=90000)
    time.sleep(5)

    current_url = (page.url or "").lower()
    file_input = page.locator('input[type="file"]')

    if "/login" in current_url or "login?" in current_url:
        save_debug(page, "login_necessario")
        raise RuntimeError(
            "TikTok pediu login. Feche este Chrome e rode primeiro "
            "1_LOGIN_TIKTOK_NORMAL.bat. Faça login no Chrome normal, "
            "espere abrir o TikTok Studio e FECHE o Chrome completamente. "
            "Depois rode AGENDAR_TIKTOK.bat de novo."
        )

    if file_input.count() == 0:
        # Wait a bit more for TikTok Studio to hydrate.
        end_wait = time.time() + 20
        while time.time() < end_wait:
            if page.locator('input[type="file"]').count() > 0:
                return
            if "/login" in (page.url or "").lower():
                break
            time.sleep(1)

    if page.locator('input[type="file"]').count() == 0:
        save_debug(page, "sem_input_upload")
        raise RuntimeError(
            "Não encontrei a área de upload. Se você ainda não fez login pelo "
            "Chrome normal, rode 1_LOGIN_TIKTOK_NORMAL.bat primeiro. "
            "Se já fez, envie o print mais recente da pasta logs/."
        )

def set_caption(page, caption):
    # TikTok has used contenteditable/DraftJS/Lexical editors at different times.
    selectors = [
        '[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        'textarea[placeholder*="caption" i]',
        'textarea[placeholder*="legenda" i]',
        'textarea'
    ]
    # Prefer content-editable boxes that are reasonably large and visible.
    candidates = []
    for sel in selectors:
        loc = page.locator(sel)
        try:
            for idx in range(min(loc.count(), 12)):
                el = loc.nth(idx)
                if el.is_visible():
                    box = el.bounding_box()
                    if box and box["width"] > 180 and box["height"] > 20:
                        candidates.append(el)
        except Exception:
            pass

    if not candidates:
        save_debug(page, "sem_legenda")
        raise RuntimeError("Não achei o campo de legenda.")

    el = candidates[0]
    try:
        el.click()
        # Ctrl+A works for both textarea and contenteditable in Chromium.
        el.press("Control+A")
        el.press("Backspace")
        el.fill(caption)
    except Exception:
        try:
            el.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(caption)
        except Exception:
            save_debug(page, "erro_legenda")
            raise RuntimeError("Achei o campo de legenda, mas não consegui preenchê-lo.")

def click_schedule_toggle(page):
    """
    TikTok Studio atual:
      input[name="postSchedule"][value="post_now"]  -> Agora
      input[name="postSchedule"][value="schedule"]  -> Programar

    Usa o rádio real, em vez de procurar checkbox/switch genérico.
    """
    radio = page.locator('input[name="postSchedule"][value="schedule"]')

    if radio.count() == 0:
        save_debug(page, "sem_radio_programar")
        return False

    try:
        radio.first.check(force=True)
    except Exception:
        try:
            page.locator('label:has(input[name="postSchedule"][value="schedule"])').first.click()
        except Exception:
            try:
                page.get_by_text("Programar", exact=True).first.click()
            except Exception:
                save_debug(page, "falha_click_programar")
                return False

    # Aguarda o React atualizar o estado do rádio.
    end = time.time() + 8
    while time.time() < end:
        try:
            if radio.first.is_checked() or radio.first.get_attribute("aria-checked") == "true":
                break
        except Exception:
            pass
        time.sleep(.25)
    else:
        save_debug(page, "radio_programar_nao_marcou")
        return False

    time.sleep(1)

    # Em algumas contas aparece uma autorização na primeira vez que agenda.
    try:
        footer = page.locator('div[class*="common-modal-footer"]')
        if footer.count() and footer.first.is_visible():
            for label in ["Permitir", "Allow", "Ativar", "Turn on", "Continuar", "Continue"]:
                btn = footer.first.locator(f'button:has-text("{label}")')
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    time.sleep(1)
                    break
    except Exception:
        pass

    # Confirma novamente, caso o modal tenha recriado o controle.
    radio = page.locator('input[name="postSchedule"][value="schedule"]')
    try:
        if radio.count() and not radio.first.is_checked():
            radio.first.check(force=True)
    except Exception:
        pass

    # Os campos de hora/data aparecem somente depois de Programar.
    end = time.time() + 12
    while time.time() < end:
        fields = page.locator('div[data-e2e="schedule_container"] input.TUXTextInputCore-input')
        if fields.count() >= 2:
            return True
        time.sleep(.3)

    save_debug(page, "programar_sem_campos")
    return False

def find_date_time_inputs(page):
    """
    No TikTok Studio atual os campos de Programar usam:
      input.TUXTextInputCore-input

    Normalmente a ordem renderizada é:
      1º hora
      2º data
    Ainda assim, tenta identificar pelos atributos antes de usar a ordem.
    """
    fields = page.locator('div[data-e2e="schedule_container"] input.TUXTextInputCore-input')

    if fields.count() < 2:
        fields = page.locator('input.TUXTextInputCore-input')

    visible_fields = []
    for i in range(min(fields.count(), 10)):
        el = fields.nth(i)
        try:
            if el.is_visible():
                visible_fields.append(el)
        except Exception:
            pass

    if len(visible_fields) < 2:
        return None, None

    date_el = None
    time_el = None

    for el in visible_fields:
        attrs = " ".join([
            (el.get_attribute("placeholder") or ""),
            (el.get_attribute("aria-label") or ""),
            (el.get_attribute("value") or ""),
        ]).lower()

        if time_el is None and any(x in attrs for x in ["hora", "time", "hh:", ":mm"]):
            time_el = el

        if date_el is None and any(x in attrs for x in ["data", "date", "dd/", "/yyyy", "/aaaa"]):
            date_el = el

    # Estrutura observada no Studio: hora primeiro, data depois.
    if time_el is None:
        time_el = visible_fields[0]
    if date_el is None:
        date_el = visible_fields[1]

    return date_el, time_el

def parse_observed_clock_text(text):
    """Interpreta o texto exibido no campo de horário do TikTok Studio.

    Só aceita HH:MM / H:MM em 24h -- não há evidência no código atual de que
    o Studio apresente 12h/AM-PM nesse campo, então qualquer formato fora
    disso é tratado como desconhecido e levanta ValueError (falha fechada),
    em vez de tentar adivinhar.
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
    """Interpreta o texto exibido no campo de data do TikTok Studio.

    Aceita dois formatos, os únicos com evidência real de uso neste projeto:

      - DD/MM/YYYY com separador '/', '-' ou '.' (o que set_date() já produzia
        e o Studio já mostrou historicamente);
      - YYYY-MM-DD (ISO), confirmado em evidência Windows real de 19/09/2026:
        o TikTok Studio chegou a devolver '2026-09-20' no campo de data.

    Qualquer coisa fora disso é tratada como desconhecida -- falha fechada,
    sem tentar um parser universal de datas.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("campo de data vazio")

    m_iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if m_iso:
        year, month, day = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        return day, month, year

    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", text)
    if not m:
        raise ValueError(f"formato de data não reconhecido: {text!r}")
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return day, month, year


def _read_field_value(el):
    try:
        v = el.input_value()
    except Exception:
        v = None
    if not v:
        try:
            v = el.get_attribute("value")
        except Exception:
            v = None
    return v or ""


def read_observed_schedule_datetime(page, expected_dt):
    """CANDIDATO C -- correção cirúrgica.

    Relocaliza (não reaproveita as referências antigas de set_schedule_datetime)
    os campos de data/hora reais do TikTok Studio, lê o que está EFETIVAMENTE
    exibido neles agora, e devolve um datetime comparável a ``expected_dt``.

    Levanta RuntimeError com uma mensagem clara -- nunca contendo HTML bruto,
    cookie ou token -- se os campos não puderem ser relocalizados, estiverem
    vazios, ou o texto não puder ser interpretado com segurança. O chamador
    (set_schedule_datetime) deve tratar qualquer uma dessas falhas como motivo
    para NÃO prosseguir até o clique final.
    """
    date_el, time_el = find_date_time_inputs(page)
    if date_el is None or time_el is None:
        raise RuntimeError(
            "Não consegui relocalizar os campos de data/hora do TikTok para "
            "confirmar o agendamento antes do clique final."
        )

    time_text = _read_field_value(time_el)
    date_text = _read_field_value(date_el)

    if not time_text:
        raise RuntimeError(
            "Campo de horário do TikTok ficou vazio/ilegível ao tentar "
            "confirmar antes do clique final."
        )
    if not date_text:
        raise RuntimeError(
            "Campo de data do TikTok ficou vazio/ilegível ao tentar "
            "confirmar antes do clique final."
        )

    try:
        hour, minute = parse_observed_clock_text(time_text)
    except ValueError as exc:
        raise RuntimeError(f"Horário exibido no TikTok não pôde ser interpretado: {exc}") from exc
    try:
        day, month, year = parse_observed_date_text(date_text)
    except ValueError as exc:
        raise RuntimeError(f"Data exibida no TikTok não pôde ser interpretada: {exc}") from exc

    try:
        observed = expected_dt.replace(
            year=year, month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0
        )
    except ValueError as exc:
        raise RuntimeError(f"Data/hora observada no TikTok é inválida: {exc}") from exc
    return observed


def fill_input_force(el, value):
    try:
        el.click()
        el.press("Control+A")
        el.fill(value)
        el.press("Tab")
        return True
    except Exception:
        try:
            el.evaluate("""(e, v) => {
              const proto = Object.getPrototypeOf(e);
              const desc = Object.getOwnPropertyDescriptor(proto, 'value');
              if (desc && desc.set) desc.set.call(e, v); else e.value=v;
              e.dispatchEvent(new Event('input',{bubbles:true}));
              e.dispatchEvent(new Event('change',{bubbles:true}));
              e.dispatchEvent(new Event('blur',{bubbles:true}));
            }""", value)
            return True
        except Exception:
            return False

def _locate_time_option(container, wanted_text):
    """Localiza, DENTRO do container informado (a lista de HORA ou a lista de
    MINUTO -- nunca a página inteira), os itens reais de opção do TikTok
    Studio:

        div.tiktok-timepicker-option-item
            span.tiktok-timepicker-option-text

    O texto usado na comparação vem preferencialmente do span filho; quando
    ele não existir (estrutura mais antiga), cai para o texto do próprio
    item. Devolve a lista de elementos cujo texto bate exatamente com
    wanted_text -- propositalmente sem qualquer busca de texto global na
    página, exatamente para não confundir a lista de HORA com a de MINUTO
    (ambas podem conter o mesmo texto, ex.: hora "10" e minuto "10").
    """
    items = container.locator("div.tiktok-timepicker-option-item")
    matches = []
    for i in range(min(items.count(), 200)):
        el = items.nth(i)
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue
        try:
            text_locator = el.locator("span.tiktok-timepicker-option-text")
            if text_locator.count():
                text = (text_locator.first.inner_text() or "").strip()
            else:
                text = (el.inner_text() or "").strip()
        except Exception:
            continue
        if text == wanted_text:
            matches.append(el)
    return matches


def _click_option_in_list(page, list_index, wanted_text):
    """Clica na opção ``wanted_text`` dentro da lista de índice ``list_index``
    (0 = HORA, 1 = MINUTO) das listas ``div.tiktok-timepicker-option-list``.

    Relocaliza as listas a cada chamada -- nunca reaproveita uma referência
    antiga, porque o TikTok pode recriar o DOM do seletor entre os cliques de
    hora e minuto. A busca do item continua sempre escopada à lista
    ``list_index``; nunca um ``get_by_text``/query genérico na página, que é
    exatamente o padrão proibido nesta correção (pode casar com o texto
    errado em outra lista).
    """
    lists = page.locator("div.tiktok-timepicker-option-list")
    if lists.count() <= list_index:
        return False
    container = lists.nth(list_index)

    for el in _locate_time_option(container, wanted_text):
        try:
            el.scroll_into_view_if_needed()
            el.click()
            return True
        except Exception:
            continue

    # Fallback estrutural equivalente ao acima, ainda escopado à MESMA lista
    # (list_index) -- nunca um querySelector/get_by_text global da página.
    return bool(page.evaluate(
        """([idx, wanted]) => {
            const lists = document.querySelectorAll('div.tiktok-timepicker-option-list');
            const root = lists[idx];
            if (!root) return false;
            const items = [...root.querySelectorAll('div.tiktok-timepicker-option-item')];
            const hit = items.find(it => {
                const span = it.querySelector('span.tiktok-timepicker-option-text');
                const text = (span ? span.textContent : it.textContent) || '';
                return text.trim() === wanted;
            });
            if (!hit) return false;
            hit.click();
            return true;
        }""",
        [list_index, wanted_text],
    ))


def _read_time_field_now(page):
    """Relocaliza (nunca reaproveita referência antiga) e lê o texto
    efetivamente exibido agora no campo de horário."""
    _, time_el = find_date_time_inputs(page)
    if time_el is None:
        return ""
    return _read_field_value(time_el)


def _hour_ui_matches(expected_hour):
    def check(text):
        try:
            ui_hour, _unused = parse_observed_clock_text(text)
        except ValueError:
            return False
        return ui_hour == expected_hour
    return check


def _hour_minute_ui_matches(expected_hour, expected_minute):
    def check(text):
        try:
            ui_hour, ui_minute = parse_observed_clock_text(text)
        except ValueError:
            return False
        return ui_hour == expected_hour and ui_minute == expected_minute
    return check


def _select_time_component(page, list_index, wanted_text, component_label, ui_matches, max_attempts=2):
    """Clica a opção ``wanted_text`` na lista ``list_index`` e CONFIRMA via
    read-back (``ui_matches``, aplicado ao texto relido do campo de horário)
    que a UI realmente refletiu esse valor -- em vez de assumir sucesso só
    porque o clique "aconteceu".

    BUG REAL WINDOWS que motivou isto: um clique na opção certa pode não
    alterar a UI (ou alterar para outro valor), e o fluxo antigo seguia em
    frente mesmo assim. Agora: click sem mudança de UI = tentativa falha.

    Concede no máximo 1 retry controlado (para o caso do TikTok recriar o
    DOM/picker no meio da seleção). Depois disso, ABORTA. Nunca escolhe outro
    horário, nunca tenta um horário "próximo", nunca usa fallback aleatório.
    """
    last_seen = None
    for attempt in range(1, max_attempts + 1):
        clicked = _click_option_in_list(page, list_index, wanted_text)
        if not clicked:
            last_seen = None
            if attempt < max_attempts:
                time.sleep(.3)
                continue
            raise RuntimeError(
                f"Não encontrei {component_label} {wanted_text!r} no seletor do TikTok."
            )
        time.sleep(.35)
        last_seen = _read_time_field_now(page)
        if ui_matches(last_seen):
            return last_seen
        if attempt < max_attempts:
            time.sleep(.3)
    raise RuntimeError(
        f"Cliquei em {component_label} {wanted_text!r}, mas o campo de horário do TikTok "
        f"continuou mostrando {last_seen!r} em vez de refletir esse valor "
        f"(sem efeito ou seleção incorreta -- abortado antes de prosseguir)."
    )


def set_schedule_datetime(page, target_dt):
    date_el, time_el = find_date_time_inputs(page)
    if date_el is None or time_el is None:
        save_debug(page, "sem_data_hora")
        raise RuntimeError("Marquei Programar, mas os campos de data/hora não apareceram.")

    # TikTok aceita minutos em passos de 5.
    minute = int(round(target_dt.minute / 5.0) * 5)
    hour = target_dt.hour
    if minute == 60:
        minute = 0
        hour = (hour + 1) % 24

    hour_txt = f"{hour:02d}"
    minute_txt = f"{minute:02d}"

    try:
        print(f"    TARGET_DT: {target_dt:%d/%m/%Y %H:%M}")
    except Exception:
        pass

    # ---------- DATA ----------
    # GATE 19.5 -- correção crítica (evidência real: conta @vem.na.bio69,
    # 058.mp4, "Não encontrei o dia 22 no calendário"): a DATA agora é
    # preenchida ANTES da hora/minuto (ordem invertida em relação a antes
    # desta rodada). Motivo: com a data do formulário ainda em "hoje" e uma
    # hora-alvo numericamente menor que a hora real do relógio (ex.: alvo
    # 10:00 rodando às 13h), aplicar a hora primeiro faz o TikTok considerar
    # o agendamento sem os 15 minutos mínimos de antecedência e entrar num
    # estado de erro transitório (data-has-error/aria-invalid no campo de
    # horário) -- padrão confirmado no DOM real capturado no momento da
    # falha (mensagem "Agende a publicação com pelo menos 15 minutos de
    # antecedência" visível, popover do calendário já fechado). Definindo a
    # data primeiro, a combinação data+hora nunca fica momentaneamente "no
    # passado" durante o preenchimento. find_date_time_inputs() já localiza
    # date_el/time_el pelos atributos reais do campo, não pela ordem em que
    # são preenchidos -- trocar a ordem aqui não afeta essa localização.
    try:
        date_el.click()
    except Exception:
        date_el.click(force=True)

    end = time.time() + 8
    while time.time() < end:
        cal = page.locator(".calendar-wrapper")
        if cal.count() and cal.first.is_visible():
            break
        time.sleep(.2)
    else:
        save_debug(page, "sem_calendario")
        raise RuntimeError("Cliquei na data, mas o calendário não abriu.")

    cal = page.locator(".calendar-wrapper").first

    # O calendário abre no mês atual/selecionado.
    # Para a janela normal do TikTok isso será o mês atual ou o seguinte.
    # O mês atual precisa ser calculado no mesmo timezone do slot, não no
    # timezone atual do Windows. Isso não altera a estratégia de interação do
    # Connector; apenas remove a dependência do relógio local da máquina.
    today = utc_now().astimezone(target_dt.tzinfo).date()
    month_delta = (target_dt.year - today.year) * 12 + (target_dt.month - today.month)

    if month_delta < 0:
        raise RuntimeError("A data escolhida ficou no passado.")

    for _ in range(month_delta):
        arrows = cal.locator(".arrow")
        if arrows.count() >= 2:
            arrows.nth(1).click()
        else:
            # fallback: último botão/ícone de seta do calendário
            buttons = cal.locator("button")
            if buttons.count() >= 2:
                buttons.nth(buttons.count() - 1).click()
            else:
                save_debug(page, "sem_seta_calendario")
                raise RuntimeError("Não achei a seta para avançar o mês.")
        time.sleep(.4)

    day_txt = str(target_dt.day)

    # A implementação atual usa spans cuja classe contém "day".
    day_candidates = cal.locator('span[class*="day"]').filter(has_text=re.compile(rf"^{re.escape(day_txt)}$"))

    clicked = False
    for i in range(day_candidates.count()):
        el = day_candidates.nth(i)
        try:
            if not el.is_visible():
                continue
            # Evita dias desativados/fora do mês, quando houver indicação.
            cls = ((el.get_attribute("class") or "") + " " +
                   (el.locator("xpath=..").get_attribute("class") or "")).lower()
            if any(x in cls for x in ["disabled", "disable", "outside"]):
                continue
            el.scroll_into_view_if_needed()
            el.click()
            clicked = True
            break
        except Exception:
            pass

    if not clicked:
        # fallback simples, igual à estrutura usada pelo TikTok.
        clicked = bool(page.evaluate(
            """([day]) => {
                const cal = document.querySelector('.calendar-wrapper');
                if (!cal) return false;
                const els = [...cal.querySelectorAll('span[class*="day"]')];
                const hit = els.find(e => (e.textContent || '').trim() === day);
                if (!hit) return false;
                hit.click();
                return true;
            }""",
            [day_txt],
        ))

    if not clicked:
        save_debug(page, "dia_nao_encontrado")
        raise RuntimeError(f"Não encontrei o dia {day_txt} no calendário.")

    time.sleep(.8)

    # Diagnóstico amigável no terminal.
    try:
        print(f"    Data programada para: {target_dt:%d/%m/%Y}")
    except Exception:
        pass

    # ---------- HORA ----------
    try:
        time_el.click()
    except Exception:
        time_el.click(force=True)

    end = time.time() + 8
    while time.time() < end:
        lists = page.locator("div.tiktok-timepicker-option-list")
        if lists.count() >= 2:
            break
        time.sleep(.2)
    else:
        save_debug(page, "sem_timepicker")
        raise RuntimeError("Cliquei no horário, mas o seletor de hora/minuto não abriu.")

    # BUG REAL WINDOWS (GATE 19.5 -- correção TARGET 10:00 -> UI 21:00):
    # não basta clicar na opção certa dentro de uma lista assumida por
    # índice -- é preciso relocalizar a lista a cada tentativa, escopar a
    # busca do item SOMENTE a ela (nunca texto global da página, que pode
    # casar com "10" tanto na lista de HORA quanto na de MINUTO) e, depois do
    # clique, reler o campo de horário para confirmar que a UI realmente
    # mudou para o valor pedido antes de prosseguir.
    try:
        print(f"    Hora solicitada: {hour_txt}")
    except Exception:
        pass

    try:
        ui_after_hour = _select_time_component(
            page, 0, hour_txt, "hora", _hour_ui_matches(hour),
        )
    except RuntimeError:
        save_debug(page, "hora_nao_confirmada")
        raise
    try:
        print(f"    UI após hora: {ui_after_hour}")
        print(f"    Minuto solicitado: {minute_txt}")
    except Exception:
        pass

    try:
        ui_after_minute = _select_time_component(
            page, 1, minute_txt, "minuto", _hour_minute_ui_matches(hour, minute),
        )
    except RuntimeError:
        save_debug(page, "minuto_nao_confirmado")
        raise
    try:
        print(f"    UI após minuto: {ui_after_minute}")
    except Exception:
        pass

    # Diagnóstico amigável no terminal.
    try:
        print(f"    Programado para: {target_dt:%d/%m/%Y %H:%M}")
    except Exception:
        pass

    # CANDIDATO C -- correção cirúrgica (Estágio 2, Parte "correção").
    # ANTES do clique final: relê data+hora efetivamente exibidos na UI e
    # compara com o horário REALMENTE solicitado (já arredondado para o
    # passo de 5 minutos que o TikTok aceita, exatamente como foi pedido à
    # plataforma). Não basta ter clicado na opção certa -- é preciso provar
    # que a UI ficou no valor certo antes de deixar o chamador prosseguir
    # para click_final_schedule().
    expected_dt = target_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    observed_dt = read_observed_schedule_datetime(page, expected_dt)
    try:
        status = "OK" if observed_dt == expected_dt else "DIVERGENTE"
        print(
            f"    Validação final: {expected_dt:%d/%m/%Y %H:%M} == "
            f"{observed_dt:%d/%m/%Y %H:%M} -> {status}"
        )
    except Exception:
        pass
    if observed_dt != expected_dt:
        save_debug(page, "horario_divergente_antes_de_confirmar")
        raise RuntimeError(
            "Abortado ANTES do clique final: a data/hora exibida no TikTok não bate "
            f"com a calculada. esperado: {expected_dt:%d/%m/%Y %H:%M} | "
            f"observado: {observed_dt:%d/%m/%Y %H:%M}"
        )

def click_final_schedule(page):
    """
    O TikTok mantém um botão com data-e2e="post_video_button".
    Quando Programar está ativo, o mesmo botão passa a executar o agendamento.
    """
    btn = page.locator('button[data-e2e="post_video_button"]')

    if btn.count() == 0:
        # fallback por texto
        for label in ["Programar", "Agendar", "Schedule", "Publicar", "Post"]:
            cand = page.locator(f'button:has-text("{label}")')
            if cand.count():
                btn = cand
                break

    if btn.count() == 0:
        save_debug(page, "sem_botao_final")
        raise RuntimeError("Não achei o botão final para programar.")

    btn = btn.first
    end = time.time() + 180
    while time.time() < end:
        try:
            if btn.is_visible() and btn.is_enabled() and btn.get_attribute("aria-disabled") != "true":
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        save_debug(page, "botao_final_desabilitado")
        raise RuntimeError("O botão final ficou desabilitado por muito tempo.")

    btn.scroll_into_view_if_needed()
    btn.click()
    time.sleep(5)

def challenge_detected(page):
    text = ""
    try:
        text = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
        pass
    words = ["captcha", "verify", "verifique", "verification", "confirme que", "security check"]
    return any(w in text for w in words)


# ----------------------------------------------------------------------------
# GATE 19.5 -- BUG REAL WINDOWS: verificação de direitos autorais/conteúdo
# incompleta antes do agendamento.
#
# Evidência real: depois de data/hora corretamente aplicadas e confirmadas
# (read-back), o clique no botão final pode disparar um modal do TikTok --
# "Continuar publicando?" -- avisando que a verificação de direitos
# autorais/conteúdo ainda está em andamento e perguntando se o usuário quer
# publicar mesmo assim ("Publicar agora") ou "Cancelar".
#
# Por padrão o produto NUNCA clica em "Publicar agora". Em vez disso, esperamos
# as verificações concluírem (polling determinístico, com timeout) ANTES do
# clique final. Se o modal aparecer mesmo assim (corrida entre o clique final
# e o fim da verificação), cancelamos com segurança, esperamos de novo, e
# permitimos no máximo 1 retry controlado da ação final.
# ----------------------------------------------------------------------------

CHECK_PENDING = "PENDING"
CHECK_PASSED = "PASSED"
CHECK_WARNING = "WARNING"
CHECK_FAILED = "FAILED"
CHECK_UNKNOWN = "UNKNOWN"

# GATE 19.5 -- ESTÁGIO 2 (rodada "VERIFICAÇÕES + POLÍTICA AUTOMÁTICA DE
# COPYRIGHT + CONTROLE DE LOTE"): cada verificação individual (direitos
# autorais, conteúdo, etc.) tem um "kind" próprio, usado para decidir se um
# WARNING específico é elegível à política de auto-continuar (somente
# copyright) -- nunca para agregar o estado geral (isso continua sendo feito
# só pelos CHECK_* acima).
CHECK_KIND_COPYRIGHT = "COPYRIGHT"
CHECK_KIND_CONTENT = "CONTENT"
CHECK_KIND_OTHER = "OTHER"

CONTINUE_PUBLISHING_MODAL_TITLE = "Continuar publicando?"

# Política de aviso (WARNING) de direitos autorais. NUNCA um booleano
# genérico "ignore_copyright" -- o conceito é nomeado e restrito
# exclusivamente ao caso em que a própria plataforma classifica o vídeo como
# ainda publicável apesar do aviso.
TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK = "BLOCK"
TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW = "ALLOW"
TIKTOK_COPYRIGHT_WARNING_POLICIES = frozenset(
    {TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK, TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW}
)

# Política de item bloqueado (FAILED/UNKNOWN/timeout de verificação/WARNING
# não-copyright/WARNING de copyright com policy=BLOCK): decide o que o
# processamento em LOTE faz quando um vídeo específico não pode ser agendado
# automaticamente.
TIKTOK_BLOCKED_ITEM_POLICY_STOP_BATCH = "STOP_BATCH"
TIKTOK_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE = "SKIP_AND_CONTINUE"
TIKTOK_BLOCKED_ITEM_POLICIES = frozenset(
    {TIKTOK_BLOCKED_ITEM_POLICY_STOP_BATCH, TIKTOK_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE}
)


def _copyright_warning_policy_from_cfg(cfg):
    """Lê tiktok_copyright_warning_policy da config da CONTA (já isolada por
    perfil/canal via account_dir_from_env/CONFIG_FILE -- não é criada
    nenhuma segunda fonte de verdade). Qualquer valor ausente/inválido cai no
    padrão mais seguro (BLOCK) -- nunca falha aberto para ALLOW."""
    value = str(cfg.get("tiktok_copyright_warning_policy", TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK) or "").strip().upper()
    if value not in TIKTOK_COPYRIGHT_WARNING_POLICIES:
        return TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK
    return value


def _blocked_item_policy_from_cfg(cfg):
    """Lê tiktok_blocked_item_policy da config da conta. Padrão mais seguro
    (compatível com o comportamento anterior a esta rodada): STOP_BATCH."""
    value = str(cfg.get("tiktok_blocked_item_policy", TIKTOK_BLOCKED_ITEM_POLICY_STOP_BATCH) or "").strip().upper()
    if value not in TIKTOK_BLOCKED_ITEM_POLICIES:
        return TIKTOK_BLOCKED_ITEM_POLICY_STOP_BATCH
    return value


class TikTokChecksBlockedError(RuntimeError):
    """Levantado quando as verificações do TikTok terminam de resolver
    (deixam de ser PENDING) mas não é seguro clicar no botão final -- ou
    quando o polling nunca sai de PENDING/UNKNOWN dentro do timeout.

    ``reason`` é um rótulo estável usado pelo chamador de lote (main()) para
    aplicar tiktok_blocked_item_policy:
      "failed"                 -- alguma verificação voltou FAILED.
      "unknown"                -- não foi possível determinar o estado.
      "copyright_warning_block" -- copyright=WARNING (publicável) e a
                                    política configurada é BLOCK.
      "warning_non_copyright"  -- WARNING em alguma verificação que não é de
                                    direitos autorais (a política ALLOW nunca
                                    se aplica a isso).
      "pending_timeout"        -- ainda PENDING quando o timeout expirou.
    """

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


def _find_checks_section(page):
    """Localiza o container ESCOPADO da seção "Verificações" do TikTok
    Studio -- nunca a página inteira nem o formulário de upload inteiro.

    GATE 19.5 -- ESTÁGIO 2 (continuação, 2ª ocorrência do bug real Windows):
    a nota anterior desta docstring dizia que esta função "já localiza o
    card corretamente" -- estava ERRADA. HTML real de diagnóstico
    (*_bloqueado_*.html, salvo automaticamente pelo próprio programa no
    abort) provou, parseado com um parser HTML de verdade:
      - os 3 seletores estruturais abaixo (`post_verifications`,
        `verification`, `check_list`) davam ZERO resultados nesta versão
        real do TikTok Studio -- SEMPRE, não só às vezes.
      - o fallback antigo (`get_by_text("Verificações") -> xpath=".."`)
        subia só 1 nível a partir do título e resolvia para
        `div.jsx-1566941760.main` -- o painel do FORMULÁRIO INTEIRO de
        upload (contém `cover_container`, `caption_container`,
        `poi_container`, `schedule_container`,
        `video_visibility_container`, `user_perm_container`,
        `advanced_settings_container`, `aigc_container`,
        `disclose_content_container` E `copyright_container`, todos
        juntos). Com esse escopo, `_find_other_check_containers()`
        encontrava `div[class*="status-result"]` de TOGGLES não
        relacionados (ex.: o toggle "Qualidade HD por padrão"), que viravam
        itens fantasmas `kind=OTHER status=CHECK_UNKNOWN` e travavam a
        agregação -- o MESMO sintoma final do bug da rodada anterior, só
        que por um mecanismo diferente (escopo largo demais, não seletor de
        item ganancioso).

    Por isso esse fallback de texto foi REMOVIDO (não só "melhorado") --
    não existe hoje uma forma comprovadamente segura de usá-lo sem
    reintroduzir esse risco dentro do escopo desta correção, e um
    fallback impreciso é pior que nenhum fallback aqui (nunca é melhor
    arriscar um escopo largo demais do que devolver "não encontrado" e
    deixar CHECK_UNKNOWN fazer seu trabalho, como já acontece hoje).

    Estratégia nova, PRIMÁRIA: partir do MESMO ponto de identidade que já é
    confiável (`div[data-e2e="copyright_container"]`, confirmado por HTML
    real) e subir pelos ancestrais reais até achar o PRIMEIRO (mais
    próximo) `div` cuja classe contenha "card" -- via eixo XPath
    `ancestor::`, que devolve os ancestrais em ordem do mais próximo para o
    mais distante quando indexado com `[1]` (não o primeiro do documento).
    Confirmado por HTML real: `div[class*="card"]` sozinho NÃO é único na
    página (4 ocorrências -- capa do vídeo, descrição, "Quando publicar" e
    o de verificações), mas só 1 desses 4 contém `copyright_container`
    dentro dele -- por isso a busca parte SEMPRE da identidade já
    confirmada, nunca da posição/ordem global na página.

    Os 3 seletores estruturais são mantidos tentados PRIMEIRO (não fazem mal
    -- hoje dão 0 resultados comprovadamente, mas um build futuro do TikTok
    pode voltar a expô-los, e nesse caso são mais diretos que subir
    ancestrais).

    Se `copyright_container` não existir na página (tela mudou/erro), a
    função devolve None -- o comportamento já existente de "seção não
    encontrada -> lista vazia -> CHECK_UNKNOWN geral" é preservado, sem
    inventar nenhum fallback adicional além do estritamente necessário."""
    for selector in (
        'div[data-e2e="post_verifications"]',
        'div[data-e2e*="verification"]',
        'div[data-e2e*="check_list"]',
    ):
        try:
            candidate = page.locator(selector)
        except Exception:
            continue
        if candidate.count() and candidate.first.is_visible():
            return candidate.first

    try:
        identity = page.locator(COPYRIGHT_CONTAINER_SELECTOR)
    except Exception:
        identity = None
    if identity is not None:
        try:
            if identity.count():
                card = identity.first.locator(CHECKS_CARD_ANCESTOR_XPATH)
                if card.count():
                    return card.first
        except Exception:
            pass

    return None


# Evidência real Windows (GATE 19.5, rodada de copyright policy): o TikTok
# mostra "Foram encontrados problemas de direitos autorais. Você ainda pode
# publicar este vídeo, mas ele será silenciado." -- isso é um WARNING
# explicitamente classificado pela própria plataforma como publicável, NUNCA
# um FAILED, mesmo contendo a palavra "problemas". Checado com prioridade
# máxima no fallback textual, antes de qualquer padrão genérico de FAILED.
_EXPLICIT_PUBLISHABLE_WARNING_TEXT = [
    "você ainda pode publicar",
    "voce ainda pode publicar",
    "será silenciado",
    "sera silenciado",
]


def _classify_check_item(el):
    """Classifica UM item de verificação (ex.: "Verificação de direitos
    autorais de música"), preferindo atributo/estado estrutural (classe,
    data-status, aria-label) quando disponível e caindo para o texto do item
    somente como fallback. Nunca devolve CHECK_PASSED por ausência de
    informação -- na dúvida, CHECK_UNKNOWN.
    """
    structural = ""
    for attr in ("class", "data-status", "aria-label"):
        try:
            structural += " " + (el.get_attribute(attr) or "")
        except Exception:
            pass
    structural = structural.lower()

    if any(x in structural for x in ["fail", "error", "danger", "blocked", "violat"]):
        return CHECK_FAILED
    if any(x in structural for x in ["warn", "attention", "atencao", "atenção"]):
        return CHECK_WARNING
    if any(x in structural for x in ["pending", "loading", "progress", "checking", "andamento"]):
        return CHECK_PENDING
    if any(x in structural for x in ["success", "passed", "complete", "concluded", " ok ", "done"]):
        return CHECK_PASSED

    text = ""
    try:
        text = (el.inner_text() or "").strip().lower()
    except Exception:
        pass

    if any(x in text for x in _EXPLICIT_PUBLISHABLE_WARNING_TEXT):
        return CHECK_WARNING
    if any(x in text for x in ["nenhum problema", "no issues", "sem problemas", "aprovado"]):
        return CHECK_PASSED
    if any(x in text for x in ["verificando", "em andamento", "em verificação", "em verificacao", "checking", "in progress"]):
        return CHECK_PENDING
    if any(x in text for x in ["problema encontrado", "failed", "removido", "bloqueado", "violação", "violacao", "copyright issue"]):
        return CHECK_FAILED
    if any(x in text for x in ["atenção", "atencao", "warning", "possível problema", "possivel problema"]):
        return CHECK_WARNING

    return CHECK_UNKNOWN


def _classify_check_kind(label):
    """Classifica a QUE verificação um item pertence (COPYRIGHT/CONTENT/
    OTHER) A PARTIR DE TEXTO, usado exclusivamente para decidir elegibilidade
    da política de copyright warning -- nunca para agregar o estado geral.

    NOTA (GATE 19.5, rodada de correção de detecção de itens): não é mais a
    forma primária de determinar o kind de copyright/conteúdo dentro de
    _gather_check_items() -- isso agora é feito por IDENTIDADE estrutural
    (_container_is_copyright_check/_container_is_content_check), muito mais
    confiável do que casar texto livre. Mantida por compatibilidade (não
    referenciada por nenhum teste diretamente) e como referência textual.
    """
    text = (label or "").lower()
    if "direitos autorais" in text or "copyright" in text:
        return CHECK_KIND_COPYRIGHT
    if "conteúdo" in text or "conteudo" in text or "content" in text:
        return CHECK_KIND_CONTENT
    return CHECK_KIND_OTHER


# ----------------------------------------------------------------------------
# GATE 19.5 -- ESTÁGIO 2 (continuação) -- CORREÇÃO: bug real Windows em que
# _gather_check_items() encontrava "itens fantasmas" (ex.: a div decorativa
# `content-check__divider`, vazia, sempre presente no DOM) através do
# seletor de fallback genérico `div[class*="check"]`, que batia com QUALQUER
# classe contendo a substring "check". Cada fantasma virava CHECK_UNKNOWN
# (texto vazio não bate com nenhum padrão), e como UNKNOWN corretamente
# bloqueia a agregação (aggregate_check_status -- não alterado), o resultado
# geral nunca resolvia para PASSED mesmo com os dois checks reais 100% ok.
#
# HTML real capturado no Windows (outerHTML do TikTok Studio) confirma:
#   - o check de copyright tem identidade estável via
#     `div[data-e2e="copyright_container"]`, dentro de um container que
#     também contém, como IRMÃO, um grupo com as 5 variantes de status
#     (`status-ready`/`status-checking`/`status-error`/`status-warn`/
#     `status-success`), só uma delas ativa por vez.
#   - o check de "conteúdo" (hoje "verificação de conteúdo simples") NÃO
#     tem data-e2e próprio -- é localizado pelo texto do headline dentro de
#     um `div[class*="headline"]`, e tem a MESMA estrutura de 5 variantes
#     como irmão do headline, dentro do mesmo container pai.
#   - `content-check__divider` é uma div vazia, sem as 5 variantes de
#     status -- por isso a nova lógica NUNCA a conta como item: em vez de
#     caçar "qualquer coisa com 'check' na classe" (lista negra frágil),
#     ela busca apenas containers que REALMENTE têm um grupo de status
#     reconhecível (lista branca por estrutura), o que exclui elementos
#     puramente decorativos por definição, sem precisar de exceção
#     hardcoded para o divisor especificamente.
#
# Correção aplicada SOMENTE nestas funções -- aggregate_check_status(),
# decide_tiktok_checks_outcome(), wait_for_tiktok_checks() e tudo de
# data/hora/política NÃO foram tocados.
# ----------------------------------------------------------------------------

# Nomes de classe reais (evidência Windows) -- semânticos e estáveis, ao
# contrário das classes com hash de build (`jsx-XXXXXXXXXX`), que NUNCA são
# usadas como seletor aqui de propósito (podem mudar a cada build do
# TikTok).
STATUS_RESULT_SELECTOR = 'div[class*="status-result"]'
COPYRIGHT_CONTAINER_SELECTOR = 'div[data-e2e="copyright_container"]'
CONTENT_HEADLINE_SELECTOR = 'div[class*="headline"]'
CONTENT_HEADLINE_TEXT_HINTS = ("conteúdo", "conteudo", "content")

# GATE 19.5 -- ESTÁGIO 2 (continuação, 2ª ocorrência do bug real Windows):
# usado por _find_checks_section() para escopar a seção "Verificações" ao
# card real, subindo a partir da identidade `copyright_container` (nunca a
# partir do texto do título "Verificações", que resolve para o formulário
# inteiro nesta versão real do TikTok Studio -- ver docstring de
# _find_checks_section). O eixo XPath `ancestor::` devolve os ancestrais em
# ordem de proximidade ao nó de contexto (o mais próximo primeiro) quando
# indexado com `[1]` -- por isso este XPath sempre resolve para o PRIMEIRO
# (mais próximo) `div` com "card" na classe subindo a árvore a partir de
# `copyright_container`, nunca para o primeiro `div.card` do documento
# inteiro (que não é único: HTML real confirma 4 ocorrências de
# `div[class*="card"]` na página, só 1 contendo copyright_container).
CHECKS_CARD_ANCESTOR_XPATH = 'xpath=ancestor::div[contains(@class, "card")][1]'

# Mapeamento direto classe-de-variante -> CHECK_*. Isso é MAIS confiável do
# que caçar frases em português/inglês (frágil, já foi fonte de bug antes) --
# por isso agora é a leitura PRIMÁRIA; o texto livre (_classify_check_item)
# vira fallback, usado só quando nenhuma das 5 variantes puder ser
# determinada.
_STATUS_VARIANT_TO_CHECK = {
    "status-ready": CHECK_PENDING,
    "status-checking": CHECK_PENDING,
    "status-error": CHECK_FAILED,
    "status-warn": CHECK_WARNING,
    "status-success": CHECK_PASSED,
}
_STATUS_VARIANT_CLASSES = tuple(_STATUS_VARIANT_TO_CHECK.keys())


def _read_status_group_state(container):
    """Lê o estado de um check já localizado, a partir das 5 variantes de
    status estruturais que são descendentes de ``container``.

    Prioriza a VISIBILIDADE REAL de cada variante (``is_visible()``) -- não
    confia cegamente no valor literal do atributo ``data-show``, que pode
    não refletir o estado visual real em todo momento -- e só usa
    ``data-show="true"`` como sinal secundário quando nenhuma variante
    estiver visivelmente ativa. Se nenhuma das 5 variantes for encontrada/
    reconhecível, devolve CHECK_UNKNOWN (fail closed -- nunca PASSED por
    omissão, regra que já existia e não muda).
    """
    if container is None:
        return CHECK_UNKNOWN
    try:
        variants = container.locator(STATUS_RESULT_SELECTOR)
    except Exception:
        return CHECK_UNKNOWN

    try:
        count = variants.count()
    except Exception:
        return CHECK_UNKNOWN

    active_by_visibility = None
    active_by_attribute = None
    for i in range(min(count, 10)):
        el = variants.nth(i)
        try:
            cls = (el.get_attribute("class") or "").lower()
        except Exception:
            cls = ""
        matched = next((v for v in _STATUS_VARIANT_CLASSES if v in cls), None)
        if matched is None:
            continue
        try:
            visible = el.is_visible()
        except Exception:
            visible = None
        if visible:
            active_by_visibility = matched
            break
        if active_by_attribute is None:
            try:
                if (el.get_attribute("data-show") or "").strip().lower() == "true":
                    active_by_attribute = matched
            except Exception:
                pass

    chosen = active_by_visibility if active_by_visibility is not None else active_by_attribute
    if chosen is None:
        return CHECK_UNKNOWN
    return _STATUS_VARIANT_TO_CHECK[chosen]


def _classify_check_container(container):
    """Classifica o estado de um check já localizado: primeiro via a leitura
    estrutural das 5 variantes (_read_status_group_state, mais confiável),
    e só cai para a heurística de texto livre já existente
    (_classify_check_item) se a estrutura não puder ser determinada. Nunca
    PASSED por omissão em nenhum dos dois caminhos."""
    structural = _read_status_group_state(container)
    if structural != CHECK_UNKNOWN:
        return structural
    if container is None:
        return CHECK_UNKNOWN
    return _classify_check_item(container)


def _find_copyright_check_container(section):
    """Localiza o container do check de direitos autorais por IDENTIDADE
    estável (`data-e2e="copyright_container"`), confirmada no HTML real.
    O container é o pai do elemento de identidade (mesmo nível do grupo de
    status, que é irmão dele) -- devolve None se não encontrado."""
    try:
        label = section.locator(COPYRIGHT_CONTAINER_SELECTOR)
    except Exception:
        return None
    try:
        if not label.count():
            return None
    except Exception:
        return None
    try:
        parent = label.first.locator("xpath=..")
    except Exception:
        return None
    if not parent.count():
        return None
    return parent.first


def _find_content_check_container(section):
    """Localiza o container do check de "conteúdo" pelo texto do headline
    (comparação manual em minúsculas -- funciona com qualquer
    capitalização), já que ele não tem data-e2e próprio hoje. Devolve None
    se nenhum headline candidato tiver um texto reconhecível -- o chamador
    trata isso como CHECK_UNKNOWN para esse check, nunca como PASSED por
    omissão."""
    try:
        headlines = section.locator(CONTENT_HEADLINE_SELECTOR)
    except Exception:
        return None
    try:
        count = headlines.count()
    except Exception:
        return None
    for i in range(min(count, 10)):
        el = headlines.nth(i)
        try:
            text = (el.inner_text() or "").strip().lower()
        except Exception:
            continue
        if any(hint in text for hint in CONTENT_HEADLINE_TEXT_HINTS):
            try:
                parent = el.locator("xpath=..")
            except Exception:
                continue
            if parent.count():
                return parent.first
    return None


def _container_is_copyright_check(container):
    """True se ``container`` é (ou contém) o check de copyright, via a
    mesma identidade estável usada em _find_copyright_check_container --
    usado para excluir esse container da descoberta genérica de checks
    "outros" (nunca contado duas vezes)."""
    try:
        return bool(container.locator(COPYRIGHT_CONTAINER_SELECTOR).count())
    except Exception:
        return False


def _container_is_content_check(container):
    """True se ``container`` é (ou contém) o check de conteúdo, pelo mesmo
    critério de texto de _find_content_check_container -- usado para excluir
    esse container da descoberta genérica de checks "outros"."""
    try:
        headlines = container.locator(CONTENT_HEADLINE_SELECTOR)
        count = headlines.count()
    except Exception:
        return False
    for i in range(min(count, 10)):
        try:
            text = (headlines.nth(i).inner_text() or "").strip().lower()
        except Exception:
            continue
        if any(hint in text for hint in CONTENT_HEADLINE_TEXT_HINTS):
            return True
    return False


def _status_group_check_container(variant):
    """A partir de uma variante de status (folha, ex.: `.status-success`),
    sobe até o CONTAINER do check a que ela pertence -- o mesmo nível
    devolvido por _find_copyright_check_container()/
    _find_content_check_container() (ex.: `.copyright-check`), não o grupo
    de status intermediário.

    No DOM real (evidência Windows, GATE 19.5) uma variante de status é
    filha do grupo de status (`div class="jsx-XXXXXXXXXX"`), que por sua vez
    é filho do container do check -- IRMÃO da identidade (data-e2e do
    copyright ou headline do conteúdo). São DOIS níveis reais, não um: subir
    só um nível pegaria o grupo de status em vez do container do check, o
    que quebraria silenciosamente a exclusão por contenção contra os checks
    já identificados em _find_other_check_containers() e faria
    copyright/conteúdo reaparecerem DUPLICADOS como itens kind=OTHER (a
    mesma classe de bug de itens fantasmas que esta rodada corrige,
    reintroduzida por engano se subíssemos só um nível).

    Sobe até 2 níveis reais e usa o resultado mais externo disponível; se a
    estrutura for mais rasa que o esperado (ex.: em fixtures de teste ou uma
    variação futura do TikTok), cai graciosamente para o nível disponível
    mais alto em vez de falhar -- o item nunca desaparece (a descoberta
    parte de STATUS_RESULT_SELECTOR, que sempre acha a folha); na pior
    hipótese só a granularidade do texto/dedup fica menos precisa.

    DÍVIDA TÉCNICA DECLARADA: assume que um 3º check futuro segue a MESMA
    profundidade estrutural (folha -> grupo de status -> container) dos
    dois checks conhecidos hoje. Documentado para revisão caso o TikTok
    mude essa estrutura."""
    try:
        one_up = variant.locator("xpath=..")
    except Exception:
        return None
    if not one_up.count():
        return None
    status_group = one_up.first
    try:
        two_up = status_group.locator("xpath=..")
    except Exception:
        return status_group
    if not two_up.count():
        return status_group
    return two_up.first


def _find_other_check_containers(section):
    """Descobre checks NÃO mapeados (nem copyright, nem conteúdo) que ainda
    assim têm um grupo de status reconhecível -- ex.: um terceiro check que
    o TikTok venha a adicionar no futuro. Nunca ignorado silenciosamente:
    entra na lista com kind=OTHER e tem seu estado lido pelo MESMO mecanismo
    estrutural de 5 variantes.

    Descoberta por LISTA BRANCA estrutural: busca todas as variantes de
    status na seção inteira (`STATUS_RESULT_SELECTOR`, nomes de classe
    semânticos e estáveis) e sobe até o container do check
    (_status_group_check_container, 2 níveis reais) -- isso exclui elementos
    puramente decorativos (como `content-check__divider`) por definição, já
    que eles nunca têm essas variantes como descendentes, sem precisar de
    uma exceção hardcoded especificamente para o divisor.

    DÍVIDA TÉCNICA DECLARADA: a deduplicação de containers usa o texto
    renderizado do container como chave (Playwright não oferece uma forma
    simples e estável de comparar identidade de nó real do DOM entre
    Locators construídos separadamente). Na prática, dois checks distintos
    sempre têm rótulos diferentes (é assim que um humano também os
    distingue), então isso é robusto para o caso real -- mas, em teoria, um
    check "outro" com texto vazio/idêntico a outro poderia ser
    sub-contado. Documentado aqui em vez de escondido.
    """
    try:
        all_variants = section.locator(STATUS_RESULT_SELECTOR)
    except Exception:
        return []
    try:
        count = all_variants.count()
    except Exception:
        return []

    seen_keys = set()
    out = []
    for i in range(min(count, 60)):
        variant = all_variants.nth(i)
        container = _status_group_check_container(variant)
        if container is None:
            continue
        if _container_is_copyright_check(container) or _container_is_content_check(container):
            continue
        try:
            key = (container.inner_text() or "").strip()
        except Exception:
            key = ""
        dedupe_key = key or f"__no_text_{id(container)}"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        out.append(container)
    return out


def _gather_check_items(page):
    """Devolve [{"label":..., "kind":..., "status":...}, ...] para os itens
    de verificação encontrados na seção "Verificações", ou [] se a seção não
    puder ser localizada.

    Os dois checks conhecidos (copyright, conteúdo) são localizados por
    IDENTIDADE estrutural (não por heurística de texto genérica) e SEMPRE
    aparecem no resultado -- se um deles não puder ser localizado (mudança
    de texto/estrutura), entra como CHECK_UNKNOWN explícito em vez de ser
    silenciosamente omitido (o que deixaria a agregação decidir só com base
    no que sobrou). Qualquer check adicional não mapeado aparece com
    kind=OTHER. ``kind`` é usado só para elegibilidade de política -- a
    agregação do estado geral (aggregate_check_status, não alterada) usa
    apenas ``status``.
    """
    section = _find_checks_section(page)
    if section is None:
        return []

    result = []

    copyright_container = _find_copyright_check_container(section)
    if copyright_container is not None:
        try:
            label = (copyright_container.inner_text() or "").strip()
        except Exception:
            label = ""
        result.append({
            "label": label or "Verificação de direitos autorais de música",
            "kind": CHECK_KIND_COPYRIGHT,
            "status": _classify_check_container(copyright_container),
        })
    else:
        result.append({"label": "", "kind": CHECK_KIND_COPYRIGHT, "status": CHECK_UNKNOWN})

    content_container = _find_content_check_container(section)
    if content_container is not None:
        try:
            label = (content_container.inner_text() or "").strip()
        except Exception:
            label = ""
        result.append({
            "label": label or "Verificação de conteúdo",
            "kind": CHECK_KIND_CONTENT,
            "status": _classify_check_container(content_container),
        })
    else:
        result.append({"label": "", "kind": CHECK_KIND_CONTENT, "status": CHECK_UNKNOWN})

    for other_container in _find_other_check_containers(section):
        try:
            label = (other_container.inner_text() or "").strip()
        except Exception:
            label = ""
        result.append({
            "label": label,
            "kind": CHECK_KIND_OTHER,
            "status": _classify_check_container(other_container),
        })

    return result


def aggregate_check_status(statuses):
    """Agrega uma lista de status individuais no estado geral.

    Regra obrigatória (GATE 19.5, rodada de copyright policy): PENDING
    domina QUALQUER outro estado, inclusive FAILED -- enquanto qualquer
    verificação estiver pendente, o estado geral é PENDING, sempre. Só
    depois de não haver mais nenhum PENDING é que a severidade
    FAILED > WARNING > (tudo PASSED) decide, com UNKNOWN como resultado
    residual sempre que sobrar algo não determinado (nunca é promovido a
    PASSED por omissão).
    """
    if not statuses:
        return CHECK_UNKNOWN
    if CHECK_PENDING in statuses:
        return CHECK_PENDING
    if CHECK_FAILED in statuses:
        return CHECK_FAILED
    if CHECK_WARNING in statuses:
        return CHECK_WARNING
    if CHECK_UNKNOWN in statuses:
        return CHECK_UNKNOWN
    return CHECK_PASSED


def get_tiktok_preflight_status(page):
    """Lê o estado agregado das verificações do TikTok (direitos autorais de
    música, conteúdo, etc.) e devolve um dentre CHECK_PENDING / CHECK_PASSED /
    CHECK_WARNING / CHECK_FAILED / CHECK_UNKNOWN. Ver aggregate_check_status
    para a regra de prioridade (PENDING sempre domina)."""
    items = _gather_check_items(page)
    if not items:
        return CHECK_UNKNOWN
    return aggregate_check_status([it["status"] for it in items])


def decide_tiktok_checks_outcome(items, copyright_warning_policy=TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK):
    """Decide o que fazer com um conjunto de itens de verificação JÁ
    RESOLVIDO (nenhum PENDING -- chamar só depois que aggregate_check_status
    não devolver mais CHECK_PENDING).

    Devolve (outcome, reason):
      ("PROCEED", None)               -- seguro clicar no botão final.
      ("BLOCKED", "failed")           -- alguma verificação FAILED.
      ("BLOCKED", "unknown")          -- não foi possível determinar.
      ("BLOCKED", "warning_non_copyright") -- WARNING fora de direitos
                                              autorais; a política ALLOW
                                              nunca se aplica a isso.
      ("BLOCKED", "copyright_warning_block") -- WARNING exclusivamente de
                                                 direitos autorais, mas a
                                                 política configurada é
                                                 BLOCK (ou não configurada).

    ALLOW só produz PROCEED quando TODO WARNING presente é de kind
    COPYRIGHT -- nunca para FAILED/UNKNOWN/PENDING, mesmo com ALLOW
    configurado.
    """
    statuses = [it["status"] for it in items] if items else []
    overall = aggregate_check_status(statuses)

    if overall == CHECK_PASSED:
        return "PROCEED", None
    if overall == CHECK_FAILED:
        return "BLOCKED", "failed"
    if overall == CHECK_UNKNOWN:
        return "BLOCKED", "unknown"
    if overall == CHECK_WARNING:
        warning_items = [it for it in items if it["status"] == CHECK_WARNING]
        all_copyright = bool(warning_items) and all(
            it["kind"] == CHECK_KIND_COPYRIGHT for it in warning_items
        )
        if not all_copyright:
            return "BLOCKED", "warning_non_copyright"
        if copyright_warning_policy == TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW:
            return "PROCEED", None
        return "BLOCKED", "copyright_warning_block"
    # overall == CHECK_PENDING nunca deveria chegar aqui (contrato da
    # função) -- tratado como bloqueio conservador em vez de estourar.
    return "BLOCKED", "unknown"


_CHECK_STATUS_LABEL_PT = {
    CHECK_PASSED: "OK",
    CHECK_PENDING: "VERIFICANDO",
    CHECK_WARNING: "AVISO",
    CHECK_FAILED: "PROBLEMA",
    CHECK_UNKNOWN: "DESCONHECIDO",
}


def _print_checks_progress(items, overall):
    for it in items:
        label = it["label"] or it["kind"]
        print(f"    {label}: {_CHECK_STATUS_LABEL_PT.get(it['status'], it['status'])}")
    print(f"    Estado geral: {_CHECK_STATUS_LABEL_PT.get(overall, overall)}")


def wait_for_tiktok_checks(page, timeout_seconds=240, poll_interval=3,
                            copyright_warning_policy=TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK):
    """Aguarda as verificações do TikTok (direitos autorais/conteúdo) ANTES
    do clique final, com polling determinístico e limite de tempo -- nunca um
    sleep fixo gigante.

    Regra obrigatória: enquanto QUALQUER verificação estiver PENDING, o
    estado geral é PENDING e o polling continua -- mesmo que outra
    verificação já esteja WARNING ou FAILED (aggregate_check_status cuida
    disso). Só depois que aggregate_check_status deixar de devolver
    CHECK_PENDING é que decide_tiktok_checks_outcome decide se prossegue
    (CHECK_PASSED, ou CHECK_WARNING de copyright com política ALLOW) ou
    bloqueia.

    Devolve o status geral (CHECK_PASSED normalmente; CHECK_WARNING quando
    liberado via política ALLOW) quando é seguro clicar no botão final.
    Levanta TikTokChecksBlockedError -- SEM clicar em nada -- em todo o
    resto: FAILED, UNKNOWN, WARNING não-copyright, WARNING de copyright com
    política BLOCK, ou timeout ainda em PENDING. UNKNOWN nunca é tratado
    como PASSED, e ALLOW nunca é aplicado enquanto houver PENDING.
    """
    deadline = time.time() + timeout_seconds
    last_snapshot = None

    while time.time() < deadline:
        items = _gather_check_items(page)
        statuses = [it["status"] for it in items]
        overall = aggregate_check_status(statuses)

        # Só decide (prosseguir ou bloquear) quando o estado geral for um dos
        # três terminais que uma leitura já consegue afirmar com confiança:
        # PASSED, WARNING ou FAILED. PENDING sempre continua esperando
        # (regra obrigatória desta rodada). UNKNOWN (seção ainda não
        # encontrada, ou item ilegível) TAMBÉM continua esperando até o
        # timeout, em vez de abortar na primeira leitura -- os seletores da
        # seção "Verificações" são dívida técnica declarada (ainda não
        # confirmados contra HTML real), então um UNKNOWN pode só significar
        # "a seção ainda não terminou de renderizar". Isso preserva o
        # comportamento já validado nesta rodada anterior (GATE 19.5,
        # rodada do modal) -- não alterado sem evidência de que atrapalha.
        if overall in (CHECK_PASSED, CHECK_WARNING, CHECK_FAILED):
            outcome, reason = decide_tiktok_checks_outcome(items, copyright_warning_policy)
            if outcome == "PROCEED":
                if overall == CHECK_WARNING:
                    print("    COPYRIGHT WARNING")
                    print(f"    POLICY = {copyright_warning_policy}")
                    print("    ACTION = CONTINUE")
                return overall
            raise TikTokChecksBlockedError(_blocked_checks_message(overall, reason, timeout_seconds), reason)

        snapshot = (overall, tuple((it["kind"], it["status"]) for it in items))
        if snapshot != last_snapshot:
            if last_snapshot is None:
                print("    Aguardando verificações do TikTok...")
            _print_checks_progress(items, overall)
            last_snapshot = snapshot
        time.sleep(poll_interval)

    raise TikTokChecksBlockedError(
        "Abortado ANTES do clique final: as verificações do TikTok (direitos autorais/"
        f"conteúdo) não concluíram em {timeout_seconds}s (último status: PENDING). "
        "Não é seguro prosseguir sem confirmação de que a verificação terminou.",
        "pending_timeout",
    )


def _blocked_checks_message(overall, reason, timeout_seconds):
    if reason == "failed":
        return (
            "Abortado ANTES do clique final: o TikTok sinalizou problema (FAILED) nas "
            "verificações de direitos autorais/conteúdo. Isso exige revisão manual -- "
            "não é seguro publicar automaticamente."
        )
    if reason == "unknown":
        return (
            "Abortado ANTES do clique final: não foi possível determinar o estado das "
            "verificações do TikTok (direitos autorais/conteúdo). Tratado como bloqueio "
            "por segurança -- não é seguro publicar automaticamente."
        )
    if reason == "warning_non_copyright":
        return (
            "Abortado ANTES do clique final: o TikTok sinalizou aviso (WARNING) em uma "
            "verificação que não é de direitos autorais. A política de auto-continuar é "
            "exclusiva para avisos de copyright explicitamente publicáveis -- isso exige "
            "revisão manual."
        )
    if reason == "copyright_warning_block":
        return (
            "Abortado ANTES do clique final: o TikTok sinalizou aviso (WARNING) de direitos "
            "autorais nas verificações. A política configurada é BLOCK -- isso exige revisão "
            "manual antes de publicar."
        )
    return (
        "Abortado ANTES do clique final: as verificações do TikTok (direitos autorais/"
        f"conteúdo) não concluíram em {timeout_seconds}s (último status: {overall}). "
        "Não é seguro prosseguir sem confirmação de que a verificação terminou."
    )


def _find_continue_publishing_modal(page):
    """Localiza o modal de interrupção "Continuar publicando?" -- escopado ao
    texto real do título, nunca assume que qualquer modal visível é esse."""
    for selector in ('div[class*="common-modal"]', 'div[role="dialog"]'):
        try:
            candidates = page.locator(selector)
        except Exception:
            continue
        for i in range(min(candidates.count(), 10)):
            el = candidates.nth(i)
            try:
                if not el.is_visible():
                    continue
                text = el.inner_text() or ""
            except Exception:
                continue
            if CONTINUE_PUBLISHING_MODAL_TITLE in text:
                return el
    return None


def handle_continue_publishing_modal_if_present(page):
    """Se o modal "Continuar publicando?" aparecer, NUNCA clica em "Publicar
    agora". Clica em "Cancelar" com segurança. Se não conseguir determinar ou
    fechar o modal, ABORTA -- nunca deixa a automação parada indefinidamente.

    Devolve True se o modal apareceu e foi cancelado; False se não apareceu.
    """
    modal = _find_continue_publishing_modal(page)
    if modal is None:
        return False

    save_debug(page, "modal_continuar_publicando")

    try:
        cancel_btn = modal.locator('button:has-text("Cancelar")')
        if cancel_btn.count() and cancel_btn.first.is_visible():
            cancel_btn.first.click()
            time.sleep(1)
            return True
    except Exception:
        pass

    raise RuntimeError(
        "O TikTok mostrou o modal 'Continuar publicando?' (verificação de direitos "
        "autorais/conteúdo incompleta) e não consegui clicar em 'Cancelar' com segurança. "
        "Abortado para NÃO publicar com a verificação incompleta."
    )

def schedule_one(page, cfg, video_path, caption, target_dt):
    print(f"\n[+] {video_path.name} -> {target_dt:%d/%m/%Y %H:%M}")
    ensure_login_and_upload_page(page, cfg)

    file_input = page.locator('input[type="file"]').first
    file_input.set_input_files(str(video_path))

    # Wait for the editor to appear / upload to start.
    time.sleep(5)
    if challenge_detected(page):
        pause_for_human(page, "O TikTok parece ter pedido uma verificação de segurança.")

    # Wait for caption editor; upload may still be processing in parallel.
    caption_editor = wait_any(
        page,
        ['[contenteditable="true"][role="textbox"]', 'div[contenteditable="true"]', 'textarea'],
        timeout_ms=60000,
    )
    if not caption_editor:
        save_debug(page, "upload_sem_editor")
        raise RuntimeError("O upload abriu, mas o editor de legenda não apareceu.")

    set_caption(page, caption)

    # Let TikTok process enough of the media before schedule controls.
    time.sleep(2)
    if not click_schedule_toggle(page):
        raise RuntimeError(
            "Não achei o controle de AGENDAR. Confirme se sua conta possui agendamento no desktop."
        )

    set_schedule_datetime(page, target_dt)

    # Dá tempo para o upload terminar. O botão final tem data-e2e estável,
    # independentemente de mostrar "Publicar" ou "Programar".
    deadline = time.time() + int(cfg.get("timeout_upload_segundos", 180))
    while time.time() < deadline:
        if challenge_detected(page):
            pause_for_human(page, "O TikTok pediu uma verificação de segurança.")

        final_btn = page.locator('button[data-e2e="post_video_button"]')
        try:
            if (final_btn.count() and final_btn.first.is_visible()
                    and final_btn.first.is_enabled()
                    and final_btn.first.get_attribute("aria-disabled") != "true"):
                break
        except Exception:
            pass
        time.sleep(2)

    # GATE 19.5 -- BUG REAL WINDOWS: a verificação de direitos autorais/
    # conteúdo pode ainda estar em andamento quando o botão final já está
    # habilitado. Esperar as verificações concluírem (polling com timeout)
    # ANTES do clique final -- nunca clicar "Publicar agora" no modal de
    # interrupção que o TikTok mostra quando a verificação está incompleta.
    copyright_warning_policy = _copyright_warning_policy_from_cfg(cfg)
    # GATE 19.5 (correção desta rodada, seção 0, decisão explícita do
    # usuário -- vale igualmente para TikTok e YouTube): com ALLOW, pula
    # INTEIRAMENTE a espera/leitura das verificações -- zero chamada a
    # wait_for_tiktok_checks(), zero espera -- e vai direto para o clique
    # final. Risco aceito e documentado pelo usuário: um problema REAL de
    # "conteúdo" (não relacionado a copyright) só apareceria depois de
    # publicado, se aparecer. wait_for_tiktok_checks() em si, aggregate_
    # check_status, decide_tiktok_checks_outcome etc. NÃO foram alterados
    # -- só deixam de ser chamados aqui quando ALLOW.
    pular_verificacoes = copyright_warning_policy == TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW
    if pular_verificacoes:
        print(
            "    Verificações do TikTok: PULADAS "
            "(política desta execução = ignorar avisos)."
        )
    else:
        check_status = wait_for_tiktok_checks(
            page,
            timeout_seconds=int(cfg.get("timeout_verificacoes_tiktok_segundos", 240)),
            copyright_warning_policy=copyright_warning_policy,
        )
        print(f"    CHECK_STATUS: {check_status}")

    click_final_schedule(page)

    # Corrida possível: o modal "Continuar publicando?" pode aparecer mesmo
    # depois das verificações terem sido lidas como concluídas (ex.: TikTok
    # reavaliou algo entre a leitura e o clique). Nunca clicar "Publicar
    # agora" -- cancela com segurança, espera as verificações de novo, e
    # permite no máximo 1 retry controlado do clique final. Com ALLOW, o
    # retry pula a espera igual à primeira tentativa -- a política vale
    # igual dentro do mesmo vídeo, nos dois pontos de chamada.
    if handle_continue_publishing_modal_if_present(page):
        if pular_verificacoes:
            print(
                "    Verificações do TikTok (após cancelar modal): PULADAS "
                "(política desta execução = ignorar avisos)."
            )
        else:
            check_status = wait_for_tiktok_checks(
                page,
                timeout_seconds=int(cfg.get("timeout_verificacoes_tiktok_segundos", 240)),
                copyright_warning_policy=copyright_warning_policy,
            )
            print(f"    CHECK_STATUS (após cancelar modal): {check_status}")
        click_final_schedule(page)
        if handle_continue_publishing_modal_if_present(page):
            raise RuntimeError(
                "O TikTok mostrou o modal 'Continuar publicando?' novamente mesmo depois "
                "de cancelar e reconfirmar as verificações. Abortado -- o retry único já "
                "foi usado, não vou tentar de novo automaticamente."
            )

    # Best-effort success detection. If TikTok stays on editor but reports success, state is still recorded.
    try:
        body = (page.locator("body").inner_text(timeout=4000) or "").lower()
        if any(x in body for x in ["scheduled", "agendado", "programado", "your post is being uploaded"]):
            print("    OK: TikTok confirmou/agiu como agendado.")
        else:
            print("    OK: clique de agendamento concluído.")
    except Exception:
        print("    OK: clique de agendamento concluído.")

# ----------------------------------------------------------------------------
# GATE 19.5 -- ESTÁGIO 2 (continuação): PERGUNTA ÚNICA POR EXECUÇÃO/CONTA
# PARA A POLÍTICA DE DIREITOS AUTORAIS (SEM PERSISTÊNCIA EM DISCO).
#
# Lacuna real encontrada: tiktok_copyright_warning_policy e
# tiktok_blocked_item_policy já funcionam corretamente quando definidos na
# config (BLOCK/ALLOW e STOP_BATCH/SKIP_AND_CONTINUE, validados em rodadas
# anteriores), mas não existia NENHUM jeito de o usuário configurar isso
# pelo terminal -- só editando o JSON de config na mão.
#
# Decisão do usuário para esta rodada (não é para reinterpretar):
#   - main() pergunta, TODA VEZ que roda (sem exceção, sem cache, sem
#     memória entre chamadas -- nem entre chamadas na MESMA sessão do
#     programa para a MESMA conta), se avisos de direitos autorais devem
#     ser ignorados NESTA execução.
#   - a resposta vale SÓ para o lote desta chamada de main() -- NUNCA é
#     escrita em CONFIG_FILE. Isso revoga, só para este fluxo interativo, a
#     exigência anterior de que a política sobrevivesse a restart -- o
#     mecanismo de config por arquivo continua existindo (ver
#     _copyright_warning_policy_from_cfg/_blocked_item_policy_from_cfg,
#     inalteradas, ainda usadas por quem chama process_prepared_batch()
#     diretamente fora do menu interativo), só deixa de ser a ÚNICA forma.
#   - independente da resposta, tiktok_blocked_item_policy é sempre
#     forçado a SKIP_AND_CONTINUE neste fluxo -- inclusive para
#     FAILED/UNKNOWN reais (rede, país restrito, etc.), decisão explícita
#     do usuário: um problema real de verificação não deve mais parar o
#     lote inteiro, só pular aquele vídeo. Uma exceção NÃO relacionada a
#     verificação (erro de automação genérico, DOM não encontrado,
#     divergência de data/hora) continua parando o lote -- isso não muda,
#     é decidido inteiramente dentro de process_prepared_batch() (não
#     tocada nesta rodada).
# ----------------------------------------------------------------------------

def _ask_interactive_copyright_warning_policy(input_fn=input):
    """Pergunta, no terminal, se avisos de direitos autorais devem ser
    ignorados NESTA execução -- SEM NENHUMA persistência e SEM NENHUMA
    memória entre chamadas: cada chamada desta função é totalmente
    independente (não há variável de módulo, cache, nem estado de
    qualquer tipo compartilhado entre uma chamada e a próxima -- só
    variáveis locais). Chamar esta função duas vezes em sequência, com
    respostas diferentes, sempre devolve o resultado correspondente a cada
    resposta, na ordem em que forem dadas, nunca a de uma chamada anterior.

    Entrada vazia ou não reconhecida = resposta mais segura = BLOCK
    (fail-closed -- nunca assume "sim" por omissão, nunca por causa de uma
    exceção na leitura da entrada).

    ``input_fn`` é injetável para testes (nunca chama o ``input()`` real
    embutido do Python em teste automatizado); o padrão é o ``input()``
    real, usado quando o programa roda de verdade no terminal."""
    try:
        resposta = input_fn(
            "Ignorar avisos de direitos autorais NESTA execução? "
            "(não fica salvo, s/N): "
        )
    except Exception:
        resposta = ""
    resposta = str(resposta or "").strip().lower()
    if resposta in ("s", "sim", "y", "yes"):
        return TIKTOK_COPYRIGHT_WARNING_POLICY_ALLOW
    return TIKTOK_COPYRIGHT_WARNING_POLICY_BLOCK


def _cfg_for_interactive_run(cfg, copyright_policy):
    """Devolve uma CÓPIA RASA de ``cfg`` com as duas chaves de política
    deste fluxo interativo sobrescritas SÓ EM MEMÓRIA -- ``cfg`` original
    nunca é mutado, e esta cópia nunca deve ser passada para
    ``save_json(CONFIG_FILE, ...)``. Usada exclusivamente para alimentar
    ``process_prepared_batch()`` a partir de ``main()``; qualquer outro
    chamador de ``process_prepared_batch()`` (uso programático/config pura,
    fora do menu interativo) continua passando sua própria config
    diretamente, sem passar por esta função, e continua lendo
    tiktok_blocked_item_policy normalmente do arquivo (inclusive
    STOP_BATCH, se for o que a config tiver) -- este forçamento para
    SKIP_AND_CONTINUE é exclusivo do fluxo interativo de main().

    tiktok_blocked_item_policy é sempre forçado a SKIP_AND_CONTINUE aqui
    (ver decisão do usuário no comentário acima desta seção)."""
    cfg_for_run = dict(cfg)
    cfg_for_run["tiktok_copyright_warning_policy"] = copyright_policy
    cfg_for_run["tiktok_blocked_item_policy"] = TIKTOK_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE
    return cfg_for_run


def process_prepared_batch(page, cfg, prepared, state):
    """Processa `prepared` (lista de (video, fingerprint, caption,
    target_dt)) sequencialmente, chamando schedule_one() para cada um.

    Extraído de main() nesta rodada (GATE 19.5 -- política de copyright/lote)
    para ser testável sem Playwright real e sem duplicar a lógica de
    decisão. Comportamento padrão (tiktok_blocked_item_policy=STOP_BATCH,
    que é o default quando a config não define nada) é IDÊNTICO ao código
    anterior: para no primeiro erro, sem persistir nada daquele vídeo, sem
    avançar para o próximo.

    Com tiktok_blocked_item_policy=SKIP_AND_CONTINUE, um TikTokChecksBlockedError
    (verificações concluíram bloqueadas, ou nunca saíram de PENDING) NÃO para
    o lote -- o vídeo é registrado em state["skipped_log"] (auditoria,
    nunca fonte de verdade de agendamento) e o processamento segue para o
    próximo vídeo elegível. O vídeo pulado NUNCA é gravado em
    state["scheduled"] -- autoridade única sobre "o que foi agendado"
    continua sendo exclusivamente essa lista. Qualquer outra exceção (não
    relacionada às verificações -- ex.: DOM não encontrado, divergência de
    data/hora) sempre para o lote, independente da política, porque essas
    indicam um problema de automação mais amplo, não "este vídeo específico
    tem um aviso/problema de conteúdo".

    Devolve (scheduled_count, stop_code): stop_code é None se o lote
    elegível foi todo percorrido (mesmo com pulos), ou um código de retorno
    inteiro se parou antes do fim.
    """
    copyright_policy = _copyright_warning_policy_from_cfg(cfg)
    blocked_policy = _blocked_item_policy_from_cfg(cfg)
    # GATE 19.5 (continuação): texto deixa explícito que estes são os
    # valores EFETIVAMENTE usados nesta execução do lote -- verdadeiro
    # tanto para quem chega aqui via a pergunta interativa de main() (que
    # NUNCA persiste, ver _cfg_for_interactive_run) quanto para quem chama
    # process_prepared_batch() diretamente com uma config persistida (uso
    # programático fora do menu, ainda suportado sem alteração).
    print("\nPolítica TikTok (valores usados nesta execução do lote):")
    print(f"Avisos de direitos autorais: {copyright_policy}")
    print(f"Problemas impeditivos: {blocked_policy}")

    scheduled_count = 0
    total = len(prepared)

    for idx, (video, fp, caption, target_dt) in enumerate(prepared):
        try:
            schedule_one(page, cfg, video, caption, target_dt)
        except TikTokChecksBlockedError as e:
            print(f"\n[BLOQUEADO] {video.name}: {e}")
            save_debug(page, f"bloqueado_{video.stem[:40]}")
            if blocked_policy == TIKTOK_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE:
                print(f"\n{video.name}")
                print(f"Verificação: {e.reason}")
                print(f"Política: {blocked_policy}")
                print("Ação: SKIPPED")
                state.setdefault("skipped_log", []).append({
                    "file": video.name,
                    "fingerprint": fp,
                    "reason": e.reason,
                    "at": utc_now_iso(),
                })
                save_json(STATE_FILE, state)
                nxt = prepared[idx + 1][0].name if idx + 1 < total else None
                if nxt:
                    print(f"Próximo vídeo: {nxt}")
                continue
            print("\nParei para NÃO pular nem duplicar vídeo.")
            print(f"Na próxima execução ele tentará novamente: {video.name}")
            return scheduled_count, 2
        except Exception as e:
            print(f"\n[ERRO] {video.name}: {e}")
            save_debug(page, f"erro_{video.stem[:40]}")
            print("\nParei para NÃO pular nem duplicar vídeo.")
            print(f"Na próxima execução ele tentará novamente: {video.name}")
            return scheduled_count, 2

        schedule_time = canonical_schedule_fields(
            target_dt,
            timezone_name_from_config(cfg),
            time_origin=SCHEDULE_TIME_MANUAL,
        )
        state.setdefault("scheduled", []).append({
            "file": video.name,
            "fingerprint": fp,
            "caption": caption,
            **schedule_time,
            "registered_at": utc_now_iso(),
        })
        save_json(STATE_FILE, state)
        scheduled_count += 1
        print(f"    Salvo no histórico: {video.name}")
        time.sleep(int(cfg.get("pausa_entre_posts_segundos", 8)))

    return scheduled_count, None


def build_slots(cfg, state, video_count, *, now_utc=None):
    """Gera no máximo ``video_count`` slots no timezone IANA da conta.

    O algoritmo compara instantes em UTC e devolve datetimes aware expressos no
    timezone da conta. Histórico legado sem offset continua legível usando o
    timezone explícito da conta. Horários inexistentes por DST são pulados.
    """
    if video_count <= 0:
        return []

    timezone_name = timezone_name_from_config(cfg)
    now_utc = utc_now() if now_utc is None else now_utc.astimezone(iana_zone("UTC"))
    default_times = cfg.get("horarios", ["10:00", "15:00", "20:00"])
    # GATE 19.5 -- nova funcionalidade (recomendação/editor de horários por
    # dia da semana): mesmo suporte que já existia em agendar_youtube.py
    # build_slots() (horarios_por_dia opcional, cai em `horarios` para
    # qualquer dia ausente do dicionário) -- byte-idêntico em padrão, para
    # ficar simétrico entre as duas plataformas. Contas antigas sem
    # horarios_por_dia continuam usando só `horarios`, sem nenhuma mudança
    # de comportamento.
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
    max_day = today + timedelta(days=int(cfg.get("dias_janela", 9)))

    scheduled = state.get("scheduled", [])
    instants = []
    for item in scheduled:
        if not isinstance(item, dict):
            continue
        instant = schedule_utc_from_record(item, timezone_name)
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
    minimum_utc = now_utc + timedelta(minutes=20)
    slots = []

    while d <= max_day and len(slots) < video_count:
        day_key = day_names[d.weekday()]
        horarios = by_day.get(day_key, default_times)
        horarios = sorted(horarios, key=lambda x: tuple(map(int, x.split(":"))))

        for hhmm in horarios:
            try:
                candidate_utc = local_wall_time_to_utc(d, hhmm, timezone_name)
            except NonexistentLocalTimeError:
                continue
            if candidate_utc <= minimum_utc:
                continue
            if last_utc is not None and candidate_utc <= last_utc:
                continue
            slots.append(utc_to_local(candidate_utc, timezone_name))
            # Bug conhecido corrigido: nunca ultrapassar a quantidade pedida.
            if len(slots) >= video_count:
                break
        d += timedelta(days=1)

    return slots

def main(input_fn=input):
    """``input_fn`` é injetável para testes automatizados (permite rodar
    ``main()`` de ponta a ponta sem esperar entrada real de terminal, e
    provar que chamadas sucessivas não compartilham nenhum estado); o
    padrão é o ``input()`` real do Python, usado quando o programa roda de
    verdade."""
    from playwright.sync_api import sync_playwright

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    cfg = load_json(CONFIG_FILE, {})

    # GATE 19.5 (continuação): pergunta feita AQUI, logo após load_json,
    # ANTES de qualquer uso de cfg relacionado a política de copyright --
    # SEMPRE, em toda chamada de main(), sem exceção, sem cache, sem
    # memória de nenhuma resposta anterior (ver docstring de
    # _ask_interactive_copyright_warning_policy). A resposta só é usada
    # mais abaixo, através de uma cópia rasa (_cfg_for_interactive_run)
    # construída depois que timezone_iana já foi resolvido em `cfg` -- o
    # `cfg` original nunca é mutado por essas duas chaves e nunca é
    # passado para save_json() com elas.
    copyright_policy_this_run = _ask_interactive_copyright_warning_policy(input_fn)
    print(
        "Política desta execução (não salva): "
        f"direitos autorais = {copyright_policy_this_run}, "
        f"itens bloqueados = {TIKTOK_BLOCKED_ITEM_POLICY_SKIP_AND_CONTINUE}"
    )

    try:
        _timezone_name, timezone_changed = ensure_timezone_config(cfg)
    except MissingTimezoneConfigurationError as exc:
        print(f"[ERRO] {exc}")
        print("Configure timezone_iana explicitamente nesta conta antes de agendar.")
        return
    if timezone_changed:
        save_json(CONFIG_FILE, cfg)
    texts = load_json(TEXTS_FILE, {})
    state = load_json(STATE_FILE, {"version":2, "scheduled":[]})

    videos = sorted(
        [p for p in VIDEO_DIR.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
        key=natural_key
    )
    done_names = {x.get("file") for x in state.get("scheduled", []) if x.get("file")}
    done_fps = {x.get("fingerprint") for x in state.get("scheduled", []) if x.get("fingerprint")}
    pending = []
    for p in videos:
        fp = fingerprint(p)
        if p.name in done_names or fp in done_fps:
            continue
        pending.append((p, fp))

    print("="*72)
    print(f"TIKTOK — {cfg.get('nome_conta', BASE.name)}")
    print("="*72)
    print(f"Vídeos encontrados : {len(videos)}")
    print(f"Já agendados       : {len(state.get('scheduled', []))}")
    print(f"Restantes          : {len(pending)}")
    if cfg.get("horarios_por_dia"):
        print("Estratégia         : horários otimizados por dia da semana")
        print("  Seg: " + ", ".join(cfg["horarios_por_dia"].get("segunda", [])))
        print("  Ter: " + ", ".join(cfg["horarios_por_dia"].get("terca", [])))
        print("  Qua: " + ", ".join(cfg["horarios_por_dia"].get("quarta", [])))
        print("  Qui: " + ", ".join(cfg["horarios_por_dia"].get("quinta", [])))
        print("  Sex: " + ", ".join(cfg["horarios_por_dia"].get("sexta", [])))
        print("  Sáb: " + ", ".join(cfg["horarios_por_dia"].get("sabado", [])))
        print("  Dom: " + ", ".join(cfg["horarios_por_dia"].get("domingo", [])))
    else:
        print(f"Horários/dia       : {', '.join(cfg.get('horarios', []))}")
    print("Retomada            : automática pelo histórico da conta")

    if not pending:
        print("\nNada para agendar.")
        return 0

    slots = build_slots(cfg, state, len(pending))
    if not slots:
        print("\nA janela configurada já está cheia.")
        print("Rode novamente quando abrir um novo horário; ele continuará do próximo vídeo.")
        return 0

    batch = [(video, fp, dt) for (video, fp), dt in zip(pending[:len(slots)], slots)]

    # Não bloqueia a fila inteira por causa de textos que faltam em vídeos futuros.
    # Prepara somente o trecho contínuo, em ordem, até o primeiro vídeo sem caption.
    prepared = []
    primeiro_sem_texto = None
    for video, fp, dt in batch:
        _fp, caption = caption_for(video, texts)
        if not caption:
            primeiro_sem_texto = video
            break
        prepared.append((video, fp, caption, dt))

    if not prepared:
        nome = primeiro_sem_texto.name if primeiro_sem_texto else batch[0][0].name
        print(f"\n[ERRO] O PRÓXIMO vídeo da fila ({nome}) ainda não tem texto da postagem gerado.")
        print("Use GERAR / EDITAR TEXTOS DAS POSTAGENS e rode novamente.")
        print("Vídeos futuros sem texto não bloqueiam a fila; somente o próximo precisa estar pronto.")
        return 3

    if primeiro_sem_texto:
        print(
            f"\n[AVISO] {primeiro_sem_texto.name} ainda está sem texto. "
            f"Vou agendar os {len(prepared)} vídeo(s) anteriores que já estão prontos e parar antes dele."
        )

    print(f"Posts que cabem nesta execução: {len(prepared)}")
    print(f"De {prepared[0][3]:%d/%m %H:%M} até {prepared[-1][3]:%d/%m %H:%M}")
    print("Durante o agendamento, não feche a janela do Chrome.\n")

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR), channel="chrome",
                headless=not bool(cfg.get("navegador_visivel", True)),
                viewport={"width":1440,"height":1000}, args=["--start-maximized"],
            )
        except Exception:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=not bool(cfg.get("navegador_visivel", True)),
                viewport={"width":1440,"height":1000}, args=["--start-maximized"],
            )
        page = context.pages[0] if context.pages else context.new_page()
        # cópia rasa só em memória (ver _cfg_for_interactive_run) -- `cfg`
        # original, o único jamais passado para save_json(), fica intocado.
        cfg_for_batch = _cfg_for_interactive_run(cfg, copyright_policy_this_run)
        try:
            scheduled_count, stop_code = process_prepared_batch(page, cfg_for_batch, prepared, state)
        finally:
            context.close()

    if stop_code is not None:
        return stop_code

    remaining = len(pending) - scheduled_count
    print("\n" + "="*72)
    print(f"CONCLUÍDO: {scheduled_count} vídeo(s) agendado(s) nesta execução.")
    print(f"Restantes: {remaining}")
    print("="*72)
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelado pelo usuário.")
    except Exception as e:
        print("\nERRO FATAL:", e)
        traceback.print_exc()
        input("\nPressione ENTER para sair...")
        raise
