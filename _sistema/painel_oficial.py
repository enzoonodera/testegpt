# -*- coding: utf-8 -*-
from pathlib import Path
import csv, hashlib, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request

try:
    from .app_paths import PATHS, account_paths, initialize_app_storage
    from .state_json import load_state_json
    from .time_utils import (
        MissingTimezoneConfigurationError, default_timezone_for_country,
        ensure_timezone_config, timezone_name_from_config, validate_timezone_name,
        utc_now, utc_now_iso
    )
except ImportError:
    from app_paths import PATHS, account_paths, initialize_app_storage
    from state_json import load_state_json
    from time_utils import (
        MissingTimezoneConfigurationError, default_timezone_for_country,
        ensure_timezone_config, timezone_name_from_config, validate_timezone_name,
        utc_now, utc_now_iso
    )

ROOT=PATHS.install_root
SYSTEM=PATHS.system
ACCOUNTS=PATHS.accounts
REMOVED=PATHS.removed_accounts
VIDEO_EXTS={'.mp4','.mov','.m4v','.webm','.avi','.mkv','.mts','.m2ts'}

PRESETS={
 '1':('Estados Unidos','en-US','English (US)'),
 '2':('Brasil','pt-BR','Português (Brasil)'),
 '3':('México','es-MX','Español (México/LatAm)'),
 '4':('Espanha','es-ES','Español (España)'),
 '5':('Reino Unido','en-GB','English (UK)'),
 '6':('Canadá','en-CA','English (Canada)'),
 '7':('Alemanha','de-DE','Deutsch'),
 '8':('França','fr-FR','Français'),
 '9':('Itália','it-IT','Italiano'),
 '10':('Japão','ja-JP','日本語'),
 '11':('Coreia do Sul','ko-KR','한국어'),
 '12':('Índia','hi-IN','हिन्दी'),
 '13':('Indonésia','id-ID','Bahasa Indonesia'),
}

DAY_TIMES={
 'segunda':['12:00','17:00','20:00'],'terca':['12:00','17:00','20:00'],
 'quarta':['12:00','17:00','20:00'],'quinta':['12:00','17:00','20:00'],
 'sexta':['12:00','17:00','20:00'],'sabado':['11:00','16:00','20:00'],
 'domingo':['11:00','16:00','20:00'],
}

# GATE 19.5 -- nova funcionalidade (recomendação/editor de horários por dia
# da semana, YouTube e TikTok). As chaves batem, de propósito, com as já
# usadas em DAY_TIMES/horarios_por_dia (agendar_youtube.py build_slots() e,
# a partir desta rodada, agendar_tiktok.py build_slots()) -- sem acento,
# para não haver divergência de encoding entre o que o editor/recomendação
# grava e o que os dois motores de agendamento já leem.
DIAS_SEMANA_ORDEM=['segunda','terca','quarta','quinta','sexta','sabado','domingo']
DIAS_SEMANA_LABEL={
 'segunda':'Segunda-feira','terca':'Terça-feira','quarta':'Quarta-feira',
 'quinta':'Quinta-feira','sexta':'Sexta-feira','sabado':'Sábado','domingo':'Domingo',
}
# Teto de horários por dia aceitos numa recomendação da IA local ou digitados
# no editor manual. Existe só para não aceitar uma resposta absurda do
# modelo (ex.: 40 horários num dia por alucinação) -- 6 já é mais do que
# qualquer conta real deste projeto usa hoje (o maior caso existente,
# DAY_TIMES, usa 3 por dia).
HORARIOS_POR_DIA_TETO=6

_HHMM_RE=re.compile(r'^([0-9]{1,2}):([0-9]{2})$')

def normalizar_hhmm(texto):
    """Valida um horário no formato HH:MM (24h) e devolve a forma
    normalizada com zero à esquerda (ex.: '9:5' -> None -- minuto de 1
    dígito não é um HH:MM válido; '09:05' -> '09:05'), ou None se inválido.

    Não há um util compartilhado de validação de HH:MM hoje neste projeto
    -- agendar_tiktok.py/agendar_youtube.py só validam o TEXTO EXIBIDO PELA
    UI depois de já ter sido digitado por este painel (parse_observed_clock_
    text/parse_observed_date_text, escopo diferente: leitura de DOM, não
    validação de entrada de usuário). Criado aqui, documentado, em vez de
    reinventar um parser mais permissivo."""
    m=_HHMM_RE.match(str(texto).strip())
    if not m: return None
    hour,minute=int(m.group(1)),int(m.group(2))
    if not (0<=hour<=23 and 0<=minute<=59): return None
    return f'{hour:02d}:{minute:02d}'

def _ordenar_horarios(valores):
    return sorted(set(valores),key=lambda x:tuple(map(int,x.split(':'))))

def cls(): os.system('cls' if os.name=='nt' else 'clear')
def pause(msg='\nENTER para continuar...'):
    try: input(msg)
    except EOFError: pass

def load_json(p,default):
    return load_state_json(p, default)

def save_json(p,data):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp')
    tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')
    tmp.replace(p)

def slugify(name):
    s=re.sub(r'[<>:"/\\|?*]+','_',name.strip())
    s=re.sub(r'\s+',' ',s).strip().rstrip('.')
    return s or 'Conta'

def py_cmd():
    if os.name=='nt' and shutil.which('py'): return ['py','-3']
    return [sys.executable]

def account_root(platform): return ACCOUNTS/platform

def accounts(platform):
    root=account_root(platform); root.mkdir(parents=True,exist_ok=True)
    return sorted([p for p in root.iterdir() if p.is_dir()],key=lambda p:p.name.lower())

def cfg_file(acc): return account_paths(acc).config
def account_cfg(acc): return load_json(cfg_file(acc),{})

def default_config(platform,name,country,locale,timezone_iana=None):
    timezone_name = validate_timezone_name(timezone_iana) if timezone_iana else default_timezone_for_country(country)
    base={
      'plataforma':platform,'nome_conta':name,'nome_canal':name,
      'pais_alvo':country,'idioma_metadata':locale,'timezone_iana':timezone_name,
      'horarios':['12:00','17:00','20:00'],'posts_por_dia':3,
      'comecar_amanha_na_primeira_execucao':True,'pausa_entre_posts_segundos':8,
      'timeout_upload_segundos':600,'navegador_visivel':True,
      'ia_local':{'whisper_model_size':'small','ollama_url':'http://localhost:11434','ollama_modelo_texto':'llama3.2','ollama_modelo_visao':'moondream'},
    }
    if platform=='youtube':
        base.update({
          'horarios_por_dia':DAY_TIMES,'dias_janela':3650,'max_uploads_por_execucao':0,
          'nao_e_para_criancas':True,'exigir_formato_short':True,'duracao_maxima_short_segundos':180,
          'preferir_ate_60s_para_reduzir_risco_content_id_bloqueado':True,
          'apagar_video_apos_agendar':False,'cancelar_video_com_direitos_autorais':True,
          # GATE 19.5 -- correção crítica: 45s era incompatível com o próprio
          # YouTube anunciando "até 10 minutos" para esta verificação
          # (evidência real: conta NextIdea, 006.mp4). Precisa continuar em
          # sincronia com YOUTUBE_COPYRIGHT_CHECK_TIMEOUT_PADRAO_SEGUNDOS em
          # _sistema/agendar_youtube.py (não importado aqui de propósito --
          # painel_oficial.py roda agendar_youtube.py como subprocesso
          # separado, nunca o importa; ver run_engine()).
          'timeout_verificacao_direitos_autorais_segundos':660,'max_anos_agendamento':5,'reiniciar_chrome_cada':25,
        })
    else:
        base.update({
          'upload_url':'https://www.tiktok.com/tiktokstudio/upload?from=creator_center',
          # GATE 19.5 -- nova funcionalidade (horários por dia da semana):
          # diferente do YouTube, o TikTok NÃO ganha um horarios_por_dia
          # default aqui (não existe um "DAY_TIMES do TikTok" pré-existente
          # para herdar) -- a chave só é adicionada por add_account(), via
          # fluxo_recomendacao_horarios(), se o usuário aceitar a
          # recomendação da IA local ou configurar manualmente. Sem isso,
          # a conta continua usando só `horarios` (lista fixa), exatamente
          # como antes desta rodada -- aditivo, nunca regressivo.
          'horarios':['10:00','15:00','20:00'],'dias_janela':9,
          'marcar_comentarios':True,'marcar_dueto':True,'marcar_stitch':True,'apagar_video_apos_agendar':False,
        })
    return base

def ensure_account(acc,platform=None):
    cfg=account_cfg(acc); platform=platform or cfg.get('plataforma') or acc.parent.name
    if cfg:
        try:
            _timezone_name, timezone_changed = ensure_timezone_config(cfg)
        except MissingTimezoneConfigurationError:
            # Conta legada/custom sem informação suficiente: preservar config
            # integralmente. Operações que exigem horário falham explicitamente
            # até timezone_iana ser configurado.
            timezone_changed = False
        if timezone_changed:
            save_json(cfg_file(acc), cfg)
    ap=account_paths(acc)
    for d in [ap.videos,ap.data,ap.logs]:
        d.mkdir(parents=True,exist_ok=True)
    (ap.profile_youtube if platform=='youtube' else ap.profile_tiktok).mkdir(parents=True,exist_ok=True)
    if platform=='youtube': ap.blocked.mkdir(parents=True,exist_ok=True)
    data=ap.data
    if not (data/'limpeza_estado.json').exists(): save_json(data/'limpeza_estado.json',{'version':1,'items':{},'updated_at':None})
    if not (data/'textos_postagem.json').exists(): save_json(data/'textos_postagem.json',{})
    if platform=='youtube':
        if not (data/'estado_youtube.json').exists(): save_json(data/'estado_youtube.json',{'version':8,'scheduled':[]})
        if not (data/'titulos_youtube.txt').exists(): (data/'titulos_youtube.txt').write_text('Amazing Short\nWait For It\nYou Need To See This\n',encoding='utf-8')
        if not (data/'descricoes_youtube.txt').exists(): (data/'descricoes_youtube.txt').write_text('Watch till the end.\n\n#Shorts #Video #Trending #Viral\n---SHORT---\nDon\'t miss the ending.\n\n#Shorts #Video #Trending #Viral\n',encoding='utf-8')
    else:
        if not (data/'estado_tiktok.json').exists(): save_json(data/'estado_tiktok.json',{'version':2,'scheduled':[]})

def choose_platform():
    print('\nQual plataforma?')
    print('1 - YouTube')
    print('2 - TikTok')
    x=input('> ').strip()
    return {'1':'youtube','2':'tiktok'}.get(x)

def choose_preset():
    print('\nPaís/idioma principal desta conta:')
    for k,(country,locale,label) in PRESETS.items(): print(f'{k:>2} - {country:<16} | {label}')
    print(' 0 - Personalizado')
    x=input('> ').strip()
    if x in PRESETS: return PRESETS[x][0],PRESETS[x][1]
    if x=='0':
        country=input('País/mercado: ').strip() or 'Personalizado'
        locale=input('Idioma/locale (ex.: en-US, pt-BR, es-MX): ').strip() or 'en-US'
        return country,locale
    return PRESETS['1'][0],PRESETS['1'][1]

def choose_account(platform):
    rows=accounts(platform)
    if not rows:
        print(f'\nNenhuma conta de {platform.upper()} cadastrada.')
        return None
    print(f'\nContas {platform.upper()}:')
    for i,p in enumerate(rows,1):
        c=account_cfg(p); print(f'{i:>2} - {c.get("nome_conta",p.name)}')
    try: n=int(input('Escolha: ').strip())
    except Exception: return None
    return rows[n-1] if 1<=n<=len(rows) else None

# ============================================================================
# GATE 19.5 -- nova funcionalidade: recomendação de horários por dia da
# semana via Ollama local + editor manual de fallback (YouTube e TikTok).
# ============================================================================
#
# Os quatro helpers abaixo (ollama_alive/try_start_ollama/ollama_chat/
# parse_json_text) DUPLICAM o comportamento já provado em
# gerar_textos.py (healthcheck, timeout, JSON validado, nunca travar o
# fluxo se falhar) -- decisão deliberada, não descuido: gerar_textos.py faz
# `BASE = account_dir_from_env(...)` a nível de módulo (mesmo padrão que já
# impediu importar agendar_youtube.py aqui, ver comentário em
# default_config() sobre YOUTUBE_COPYRIGHT_CHECK_TIMEOUT_PADRAO_SEGUNDOS) --
# importar gerar_textos.py aqui resolveria esse `BASE` usando o env do
# PRÓPRIO painel_oficial.py (sem ACCOUNT_DIR setado), o que é
# arquiteturalmente errado. A descoberta do executável (`find_ollama()`,
# abaixo nesta mesma classe de funções do painel) já existe neste arquivo e
# é reaproveitada por `try_start_ollama()` -- não duplicada de novo.
def ollama_alive(base_url,timeout=4):
    try:
        urllib.request.urlopen(base_url.rstrip('/')+'/api/tags',timeout=timeout).close(); return True
    except Exception: return False

def try_start_ollama(base_url):
    if ollama_alive(base_url): return True
    exe=find_ollama()
    if not exe: return False
    try:
        flags=0x08000000 if os.name=='nt' else 0
        subprocess.Popen([str(exe),'serve'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
        for _ in range(12):
            time.sleep(0.5)
            if ollama_alive(base_url): return True
    except Exception: pass
    return False

def ollama_chat(base_url,payload,timeout=120):
    req=urllib.request.Request(base_url.rstrip('/')+'/api/chat',data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body=e.read().decode('utf-8',errors='replace')
        raise RuntimeError(f'Ollama HTTP {e.code}: {body[:800]}') from e

def parse_json_text(text):
    text=(text or '').strip()
    try: return json.loads(text)
    except Exception: pass
    m=re.search(r'\{.*\}',text,flags=re.S)
    if m:
        try: return json.loads(m.group(0))
        except Exception: pass
    return None

def _validar_recomendacao_horarios(obj):
    """Valida ESTRITAMENTE o schema esperado da resposta da IA -- ou é
    válido e completo (os 7 dias presentes, cada um com 1 a
    HORARIOS_POR_DIA_TETO horários em HH:MM válido), ou é descartado POR
    INTEIRO. Nunca completa um dia ausente/inválido com um valor
    arbitrário -- essa é a regra explícita desta rodada."""
    if not isinstance(obj,dict): return None
    if not set(DIAS_SEMANA_ORDEM).issubset(obj.keys()): return None
    resultado={}
    for key in DIAS_SEMANA_ORDEM:
        valores=obj.get(key)
        if not isinstance(valores,list) or not (1<=len(valores)<=HORARIOS_POR_DIA_TETO):
            return None
        normalizados=[]
        for v in valores:
            if not isinstance(v,str): return None
            norm=normalizar_hhmm(v)
            if norm is None: return None
            normalizados.append(norm)
        horarios=_ordenar_horarios(normalizados)
        if not horarios: return None
        resultado[key]=horarios
    return resultado

def _prompt_recomendacao_horarios(tema,platform,country,locale):
    plataforma_label='YouTube Shorts' if platform=='youtube' else 'TikTok'
    return (
        f'Você é um estrategista de postagem de vídeos curtos para {plataforma_label}.\n'
        f'Tema/nicho do canal: {tema}\n'
        f'Mercado/país-alvo: {country}\n'
        f'Idioma do público: {locale}\n'
        'Sugira os melhores horários de postagem para CADA dia da semana, pensando em '
        'quando a audiência deste tema costuma estar mais ativa nesta plataforma e neste mercado.\n'
        'Regras:\n'
        f'- Cada um dos 7 dias da semana precisa ter pelo menos 1 horário e no máximo {HORARIOS_POR_DIA_TETO} horários.\n'
        '- Horários no formato 24 horas "HH:MM".\n'
        '- Pode variar a quantidade e os horários entre os dias (ex.: mais horários no fim de semana, '
        'se fizer sentido para este tema).\n'
        '- Responda APENAS com um JSON válido, exatamente neste formato, sem nenhum texto antes ou depois:\n'
        '{"segunda": ["HH:MM", ...], "terca": [...], "quarta": [...], "quinta": [...], '
        '"sexta": [...], "sabado": [...], "domingo": [...]}'
    )

def gerar_recomendacao_horarios_ollama(tema,platform,country,locale,ia_local_cfg):
    """Tenta gerar uma recomendação de horários por dia da semana via
    Ollama local. NUNCA levanta -- qualquer falha (Ollama indisponível,
    timeout, resposta fora do schema) devolve None, e o chamador cai no
    editor manual. Isso garante que a criação de conta nunca trava
    esperando o Ollama."""
    base_url=(ia_local_cfg or {}).get('ollama_url','http://localhost:11434')
    model=(ia_local_cfg or {}).get('ollama_modelo_texto','llama3.2')
    if not ollama_alive(base_url):
        if not try_start_ollama(base_url):
            return None
    prompt=_prompt_recomendacao_horarios(tema,platform,country,locale)
    payload={'model':model,'messages':[{'role':'user','content':prompt}],'stream':False,'format':'json',
             'keep_alive':'30m','options':{'temperature':0.4,'num_predict':400}}
    try:
        data=ollama_chat(base_url,payload,90)
    except Exception:
        return None
    try:
        content=(data.get('message') or {}).get('content')
    except Exception:
        return None
    obj=parse_json_text(content)
    return _validar_recomendacao_horarios(obj)

def editar_horarios_semana(current_by_day,default_times):
    """Editor manual por dia da semana (seção 2 do GATE 19.5) -- 7 linhas,
    repete automaticamente todo mês (não é por dia do calendário). Usado
    tanto na criação de conta (fallback da recomendação) quanto para
    reeditar uma conta já existente (seção 3), sempre pré-preenchido.

    `current_by_day`: dict atual (pode ser {} se a conta ainda não tem
    horarios_por_dia configurado). `default_times`: lista usada como valor
    inicial de qualquer dia ainda sem entrada específica em
    `current_by_day` (o `horarios`/DAY_TIMES vigente da conta/plataforma).

    Devolve o dict completo (7 chaves) pronto para salvar, ou None se o
    usuário cancelar (digitando "cancelar" em qualquer dia, ou recusando a
    confirmação final) -- cancelar nunca altera nada."""
    novo={}
    for key in DIAS_SEMANA_ORDEM:
        atual=current_by_day.get(key) or default_times or []
        atual_txt=', '.join(atual) if atual else '(nenhum)'
        while True:
            raw=input(
                f'\n{DIAS_SEMANA_LABEL[key]} -- horários separados por vírgula (ex.: 10:00,15:00,20:00).\n'
                f'  Atual: {atual_txt}\n'
                '  ENTER mantém o atual, ou digite "cancelar" para sair sem salvar nada: '
            ).strip()
            if raw.lower()=='cancelar':
                print('\nCancelado -- nada foi alterado.')
                return None
            if not raw:
                novo[key]=list(atual)
                break
            partes=[p.strip() for p in raw.split(',') if p.strip()]
            if not partes:
                print('  [ERRO] Informe pelo menos um horário, ou ENTER para manter o atual.')
                continue
            normalizados=[]
            algum_invalido=False
            for p in partes:
                norm=normalizar_hhmm(p)
                if norm is None:
                    print(f'  [ERRO] Horário inválido: {p!r} -- use o formato HH:MM (ex.: 09:30). '
                          'Digite de novo só este dia.')
                    algum_invalido=True
                    break
                normalizados.append(norm)
            if algum_invalido:
                continue
            if len(normalizados)>HORARIOS_POR_DIA_TETO:
                print(f'  [ERRO] No máximo {HORARIOS_POR_DIA_TETO} horários por dia. Digite de novo só este dia.')
                continue
            novo[key]=_ordenar_horarios(normalizados)
            break

    print('\n=== RESUMO DOS HORÁRIOS ===')
    for key in DIAS_SEMANA_ORDEM:
        print(f'  {DIAS_SEMANA_LABEL[key]:<14}: {", ".join(novo[key])}')
    if input('\nConfirma salvar estes horários? [S/N]: ').strip().lower() not in ('s','sim','y','yes'):
        print('\nCancelado -- nada foi alterado.')
        return None
    return novo

def perguntar_tema_canal():
    """Pergunta o tema/nicho do canal (texto livre, campo mínimo de 1
    caractere). Vazio pede confirmação explícita antes de pular a
    recomendação automática -- evita tanto travar num loop sem saída
    quanto descartar a recomendação por um ENTER acidental."""
    print('\nPara sugerir os melhores horários de postagem deste canal, me conte sobre ele.')
    while True:
        tema=input('Qual é o tema deste canal? (ex.: humor, receitas, notícias, gaming...): ').strip()
        if tema:
            return tema
        confirma=input(
            'Deixar em branco PULA a recomendação automática e abre a configuração manual '
            'de horários. Confirma deixar em branco? [S/N]: '
        ).strip().lower()
        if confirma in ('s','sim','y','yes'):
            return None

def fluxo_recomendacao_horarios(platform,cfg,country,locale):
    """Seções 1-2 do GATE 19.5: pergunta o tema, tenta a recomendação via
    Ollama, pede confirmação, e cai SEMPRE no editor manual se recusada,
    indisponível ou inválida -- nunca trava a criação da conta esperando o
    Ollama. Devolve o dict de 7 dias pronto para gravar em
    `horarios_por_dia`, ou None se o padrão de default_config() deve ser
    mantido sem alteração (usuário cancelou o editor manual também)."""
    tema=perguntar_tema_canal()
    recomendacao=None
    if tema:
        recomendacao=gerar_recomendacao_horarios_ollama(tema,platform,country,locale,cfg.get('ia_local') or {})
        if recomendacao is None:
            print(
                '\n[AVISO] Não consegui gerar uma recomendação automática agora '
                '(Ollama indisponível ou resposta inválida) -- configure manualmente abaixo.'
            )
    if recomendacao:
        print('\n=== RECOMENDAÇÃO DE HORÁRIOS (via IA local) ===')
        for key in DIAS_SEMANA_ORDEM:
            print(f'  {DIAS_SEMANA_LABEL[key]:<14}: {", ".join(recomendacao[key])}')
        # Decisão desta rodada: um ENTER vazio ou resposta inválida aqui
        # NÃO aceita a recomendação (mesmo o prompt do usuário sugerindo a
        # notação "[S/n]") -- o padrão mais seguro é exigir confirmação
        # explícita antes de usar uma sugestão gerada por IA, nunca aceitar
        # silenciosamente por omissão. A UI mostra "[s/N]" (não "[S/n]")
        # para não prometer visualmente um default que o código não usa.
        aceita=input('\nUsar esta recomendação? [s/N]: ').strip().lower()
        if aceita in ('s','sim','y','yes'):
            return recomendacao
        print('\nSem problema -- vamos configurar manualmente.')
    current_by_day=cfg.get('horarios_por_dia') or {}
    default_times=cfg.get('horarios') or (['12:00','17:00','20:00'] if platform=='youtube' else ['10:00','15:00','20:00'])
    print('\n=== HORÁRIOS DE POSTAGEM POR DIA DA SEMANA ===')
    return editar_horarios_semana(current_by_day,default_times)

def editar_horarios_conta_existente():
    """Seção 3 do GATE 19.5: reabre o editor da seção 2 para uma conta já
    cadastrada, pré-preenchido com os valores atuais (horarios_por_dia se
    já existir, senão o horarios/DAY_TIMES vigente). Fecha a lacuna real
    de hoje (só dava pra mudar horários editando o JSON na mão)."""
    cls(); print('=== EDITAR HORÁRIOS DE POSTAGEM (POR DIA DA SEMANA) ===')
    platform=choose_platform()
    if not platform: return
    acc=choose_account(platform)
    if not acc: pause(); return
    cfg=account_cfg(acc)
    print(f'\nConta: {cfg.get("nome_conta",acc.name)}')
    current_by_day=cfg.get('horarios_por_dia') or {}
    default_times=cfg.get('horarios') or (['12:00','17:00','20:00'] if platform=='youtube' else ['10:00','15:00','20:00'])
    novo=editar_horarios_semana(current_by_day,default_times)
    if novo is None:
        pause(); return
    cfg['horarios_por_dia']=novo
    save_json(cfg_file(acc),cfg)
    print('\nHorários atualizados. Vale a partir do próximo agendamento desta conta.')
    pause()

def choose_folder(title):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root=tk.Tk(); root.withdraw()
        try: root.attributes('-topmost',True)
        except Exception: pass
        result=filedialog.askdirectory(title=title,mustexist=True); root.destroy()
        if result: return Path(result).expanduser().resolve()
    except Exception: pass
    raw=input('Cole o caminho da pasta (ENTER cancela):\n> ').strip().strip('"')
    return Path(raw).expanduser().resolve() if raw else None

def run_engine(acc,script,args=None):
    ensure_account(acc)
    env=os.environ.copy(); env['ACCOUNT_DIR']=str(acc)
    cmd=py_cmd()+[str(SYSTEM/script)]+(args or [])
    try: return subprocess.call(cmd,env=env,cwd=str(ROOT))
    except KeyboardInterrupt: return 130

def fingerprint(path):
    h=hashlib.sha256(); size=path.stat().st_size; h.update(str(size).encode('ascii')); chunk=1024*1024
    with path.open('rb') as f:
        h.update(f.read(chunk))
        if size>chunk:
            f.seek(max(0,size-chunk)); h.update(f.read(chunk))
    return h.hexdigest()

def export_texts_csv(acc):
    ap=account_paths(acc); cfg=account_cfg(acc); data=load_json(ap.data/'textos_postagem.json',{})
    rows=[]
    for fp,e in data.items():
        if not isinstance(e,dict): continue
        rows.append({'arquivo':e.get('arquivo',''),'titulo':e.get('titulo',''),'descricao':e.get('descricao',''),'caption':e.get('caption',''),
                     'hashtags':' '.join(e.get('hashtags') or []),'pais_alvo':e.get('pais_alvo',''),'idioma':e.get('idioma',''),
                     'status':e.get('status',''),'atualizado_em':e.get('atualizado_em',''),'fingerprint':fp})
    rows.sort(key=lambda r:[int(x) if x.isdigit() else x for x in re.split(r'(\d+)',r['arquivo'].lower())])
    path=account_paths(acc).data/'textos_postagem.csv'; fields=['arquivo','titulo','descricao','caption','hashtags','pais_alvo','idioma','status','atualizado_em','fingerprint']
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)

def import_legacy(acc,platform,src):
    print('\nImportando histórico antigo...')
    ap=account_paths(acc)
    if (src/'videos').exists():
        try: shutil.copytree(src/'videos',ap.videos,dirs_exist_ok=True)
        except Exception as e: print('[AVISO] Não consegui copiar todos os vídeos:',e)
    profile_candidates=[src/('perfil_youtube' if platform=='youtube' else 'perfil_tiktok')]
    if platform=='tiktok': profile_candidates.append(src/'perfil_chrome')
    for pp in profile_candidates:
        if pp.exists():
            try:
                shutil.copytree(pp,ap.profile_youtube if platform=='youtube' else ap.profile_tiktok,dirs_exist_ok=True)
            except Exception as e:
                print('[AVISO] Não consegui copiar todo o perfil do navegador:',e)
            break
    data=ap.data
    if platform=='youtube':
        for sp in [src/'estado_youtube.json',src/'dados'/'estado_youtube.json']:
            if sp.exists(): shutil.copy2(sp,data/'estado_youtube.json'); break
        # Prefer canonical new text history; otherwise convert old IA history.
        canonical=None
        for sp in [src/'textos_postagem.json',src/'dados'/'textos_postagem.json']:
            if sp.exists(): canonical=sp; break
        if canonical:
            shutil.copy2(canonical,data/'textos_postagem.json')
        else:
            old=None
            for sp in [src/'titulos_ia_local.json',src/'dados'/'titulos_ia_local.json']:
                if sp.exists(): old=sp; break
            if old:
                raw=load_json(old,{})
                converted={}
                cfg=account_cfg(acc)
                for fp,e in raw.items():
                    if not isinstance(e,dict) or not e.get('titulo'): continue
                    tags=e.get('hashtags') or []
                    converted[fp]={'arquivo':e.get('arquivo_original',''),'plataforma':'youtube','titulo':e.get('titulo',''),
                                   'descricao':e.get('descricao',''),'hashtags':tags,'pais_alvo':cfg.get('pais_alvo'),
                                   'idioma':cfg.get('idioma_metadata'),'status':'done','manual_edit':True,
                                   'atualizado_em':utc_now_iso()}
                save_json(data/'textos_postagem.json',converted)
    else:
        for sp in [src/'estado.json',src/'estado_tiktok.json',src/'dados'/'estado_tiktok.json']:
            if sp.exists(): shutil.copy2(sp,data/'estado_tiktok.json'); break
        for sp in [src/'textos_postagem.json',src/'dados'/'textos_postagem.json']:
            if sp.exists(): shutil.copy2(sp,data/'textos_postagem.json'); break
    for sp in [src/'dados'/'limpeza_estado.json',src/'limpeza_estado.json']:
        if sp.exists(): shutil.copy2(sp,data/'limpeza_estado.json'); break
    # Aproveita horários antigos sem substituir plataforma/idioma novos.
    oldcfg={}
    for sp in [src/'config_canal.json',src/'config.json',src/'dados'/'config.json']:
        if sp.exists(): oldcfg=load_json(sp,{}); break
    if oldcfg:
        cfg=account_cfg(acc)
        for k in ['horarios','horarios_por_dia','posts_por_dia','dias_janela','comecar_amanha_na_primeira_execucao','pausa_entre_posts_segundos','timeout_upload_segundos','max_uploads_por_execucao','timezone_iana']:
            if k in oldcfg: cfg[k]=oldcfg[k]
        ensure_timezone_config(cfg)
        save_json(cfg_file(acc),cfg)
    export_texts_csv(acc)
    print('Importação concluída.')

def add_account():
    cls(); print('=== ADICIONAR CONTA ===')
    platform=choose_platform()
    if not platform: return
    name=input('\nNome do canal/conta: ').strip()
    if not name: return
    # Case-insensitive duplicate detection.
    existing=None
    for p in accounts(platform):
        c=account_cfg(p)
        if str(c.get('nome_conta',p.name)).casefold()==name.casefold() or p.name.casefold()==slugify(name).casefold(): existing=p; break
    if existing:
        print('\nEssa conta já está cadastrada; não vou criar duplicada.')
        if input('Abrir o login dela novamente? [S/N]: ').strip().lower() in ('s','sim','y','yes'):
            run_engine(existing,'login_conta.py')
        pause(); return
    country,locale=choose_preset()
    try:
        timezone_name=default_timezone_for_country(country)
    except MissingTimezoneConfigurationError:
        print('\nEsse mercado não possui timezone padrão seguro.')
        while True:
            raw_tz=input('Timezone IANA da conta (ex.: Europe/Lisbon, America/Argentina/Buenos_Aires, UTC): ').strip()
            try:
                timezone_name=validate_timezone_name(raw_tz)
                break
            except Exception as exc:
                print(f'Timezone inválido: {exc}')
    cfg=default_config(platform,name,country,locale,timezone_name)
    # GATE 19.5 -- nova funcionalidade: recomendação/editor de horários por
    # dia da semana, ANTES de salvar a config -- para a conta já nascer com
    # o horarios_por_dia escolhido (recomendado pela IA local ou editado à
    # mão), em vez de sempre herdar o valor genérico de default_config().
    # fluxo_recomendacao_horarios() NUNCA trava esperando o Ollama -- toda
    # falha (indisponível, timeout, resposta fora do schema) cai no editor
    # manual, que por sua vez também pode ser cancelado sem travar nada.
    horarios_por_dia=fluxo_recomendacao_horarios(platform,cfg,country,locale)
    if horarios_por_dia:
        cfg['horarios_por_dia']=horarios_por_dia
    acc=account_root(platform)/slugify(name); acc.mkdir(parents=True,exist_ok=False)
    save_json(cfg_file(acc),cfg); ensure_account(acc,platform)
    print(f'\nConta criada: {platform.upper()} > {name}')
    print(f'País/idioma: {country} / {locale}')
    if input('\nPossui uma PASTA ANTIGA desta conta para importar histórico/login/vídeos? [S/N]: ').strip().lower() in ('s','sim','y','yes'):
        src=choose_folder('Selecione a pasta ANTIGA desta conta')
        if src and src.exists(): import_legacy(acc,platform,src)
    print('\nAgora vou abrir o Chrome para salvar o login desta conta.')
    pause('ENTER para abrir o login...')
    run_engine(acc,'login_conta.py')
    pause()

def remove_account():
    cls(); print('=== REMOVER CONTA ===')
    platform=choose_platform()
    if not platform: return
    acc=choose_account(platform)
    if not acc: pause(); return
    name=account_cfg(acc).get('nome_conta',acc.name)
    print(f'\nConta: {platform.upper()} > {name}')
    print('Ela sairá do painel, mas será movida para o backup local de contas removidas para evitar perda acidental.')
    if input('Digite REMOVER para confirmar: ').strip().upper()!='REMOVER': print('Cancelado.'); pause(); return
    dest=REMOVED/platform/f'{acc.name}_{utc_now():%Y%m%d_%H%M%S}'; dest.parent.mkdir(parents=True,exist_ok=True)
    shutil.move(str(acc),str(dest)); print('Conta removida do painel. Backup local:',dest); pause()

def show_accounts():
    cls(); print('=== CONTAS CADASTRADAS ===')
    anyrow=False
    for platform in ('youtube','tiktok'):
        rows=accounts(platform); print(f'\n{platform.upper()}')
        if not rows: print('  (nenhuma)')
        for p in rows:
            anyrow=True; c=account_cfg(p)
            try: tz_label=timezone_name_from_config(c)
            except MissingTimezoneConfigurationError: tz_label='TIMEZONE NÃO CONFIGURADO'
            print(f'  - {c.get("nome_conta",p.name)} | {c.get("pais_alvo","")} | {c.get("idioma_metadata","")} | {tz_label}')
    pause()

def clean_metadata():
    cls(); print('=== LIMPAR META DADOS OFICIAL ===')
    src=choose_folder('Selecione a pasta com os vídeos ORIGINAIS')
    if not src or not src.exists(): print('Cancelado/pasta inválida.'); pause(); return
    found=[p for p in src.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    if not found: print('Nenhum vídeo encontrado nessa pasta.'); pause(); return
    print(f'\nEncontrados: {len(found)} vídeo(s).')
    platform=choose_platform()
    if not platform: return
    acc=choose_account(platform)
    if not acc: pause(); return
    ensure_account(acc,platform); cfg=account_cfg(acc)
    cpus=os.cpu_count() or 4; jobs=min(3,max(2,cpus//4)) if cpus>=8 else 1
    print(f'\nDestino: {platform.upper()} > {cfg.get("nome_conta",acc.name)}')
    print('O histórico identificará originais já tratados e continuará a numeração do canal.')
    ap=account_paths(acc)
    rc=run_engine(acc,'limpar_metadados_oficial.py',['-i',str(src),'-o',str(ap.videos),'-j',str(jobs),'--state-file',str(ap.data/'limpeza_estado.json'),'--log-dir',str(ap.logs),'--sem-pausa'])
    print('\nTratamento concluído.' if rc==0 else f'\nTratamento terminou com código {rc}; o que concluiu ficou salvo.')
    if input('Deseja gerar/continuar os TEXTOS DAS POSTAGENS agora? [S/N]: ').strip().lower() in ('s','sim','y','yes'):
        run_engine(acc,'gerar_textos.py')
    pause()

def edit_text(acc,platform):
    ap=account_paths(acc)
    videos=sorted([p for p in ap.videos.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],key=lambda p:[int(x) if x.isdigit() else x for x in re.split(r'(\d+)',p.name.lower())])
    if not videos: print('\nNão há vídeos nessa conta.'); pause(); return
    raw=input('\nDigite o número do vídeo para editar (ex.: 8 ou 008.mp4): ').strip()
    try: target_num=int(Path(raw).stem)
    except Exception: print('Número inválido.'); pause(); return
    video=next((p for p in videos if p.stem.isdigit() and int(p.stem)==target_num),None)
    if not video: print('Vídeo não encontrado.'); pause(); return
    fp=fingerprint(video); data=load_json(ap.data/'textos_postagem.json',{}); e=data.get(fp)
    if not isinstance(e,dict) or e.get('status')!='done': print('Esse vídeo ainda não tem texto gerado. Use Gerar/continuar primeiro.'); pause(); return
    print(f'\nArquivo: {video.name}')
    if platform=='youtube':
        print('Título atual:',e.get('titulo','')); nv=input('Novo título (ENTER mantém): ').strip()
        if nv: e['titulo']=nv[:100]
        print('Descrição atual:',e.get('descricao','')); nv=input('Nova descrição (ENTER mantém): ').strip()
        if nv: e['descricao']=nv
    else:
        print('Texto atual:',e.get('caption','')); nv=input('Novo texto/caption (ENTER mantém): ').strip()
        if nv: e['caption']=nv
    print('Hashtags atuais:',' '.join(e.get('hashtags') or [])); nv=input('Novas hashtags separadas por espaço (ENTER mantém): ').strip()
    if nv:
        tags=[]
        for x in nv.split():
            x=x if x.startswith('#') else '#'+x
            if x.lower() not in {t.lower() for t in tags}: tags.append(x)
        e['hashtags']=tags[:8]
    e['status']='done'; e['manual_edit']=True; e['atualizado_em']=utc_now_iso(); data[fp]=e
    save_json(ap.data/'textos_postagem.json',data); export_texts_csv(acc); print('Salvo.'); pause()

def texts_menu():
    cls(); print('=== GERAR / EDITAR TEXTOS DAS POSTAGENS ===')
    platform=choose_platform()
    if not platform: return
    acc=choose_account(platform)
    if not acc: pause(); return
    print('\n1 - Gerar / continuar somente o que falta')
    print('2 - Editar manualmente um vídeo já gerado')
    print('0 - Voltar')
    op=input('> ').strip()
    if op=='1': run_engine(acc,'gerar_textos.py'); pause()
    elif op=='2': edit_text(acc,platform)

def post_schedule():
    cls(); print('=== POSTAR / AGENDAR ===')
    platform=choose_platform()
    if not platform: return
    acc=choose_account(platform)
    if not acc: pause(); return
    cfg=account_cfg(acc); ensure_account(acc,platform)
    print(f'\nIniciando: {platform.upper()} > {cfg.get("nome_conta",acc.name)}')
    print('O histórico desta conta define o próximo vídeo. Se o último concluído foi 007, o próximo será 008.')
    script='agendar_youtube.py' if platform=='youtube' else 'agendar_tiktok.py'
    run_engine(acc,script); pause()

def configurar_verificacao_direitos_autorais_youtube():
    """GATE 19.5 -- correção desta rodada: antes só dava pra mudar
    `timeout_verificacao_direitos_autorais_segundos` editando o JSON da
    conta na mão. Tela mínima, focada só neste campo (não um editor de
    configurações genérico) -- ver relatório desta rodada para a decisão
    de escopo."""
    cls(); print('=== VERIFICAÇÃO DE DIREITOS AUTORAIS (YOUTUBE) ===')
    acc=choose_account('youtube')
    if not acc: pause(); return
    cfg=account_cfg(acc)
    atual=int(cfg.get('timeout_verificacao_direitos_autorais_segundos',660))
    print(f'\nConta: {cfg.get("nome_conta",acc.name)}')
    print(f'Tempo máximo de espera atual pela verificação: {atual}s ({atual/60:.1f} min)')
    print('\nO YouTube pode levar até 10 minutos para concluir esta verificação.')
    print('Não recomendamos menos que 660s (11 min) para não bloquear vídeos saudáveis.')
    raw=input('\nNovo valor em segundos (ENTER para manter o atual): ').strip()
    if not raw:
        print('\nMantido sem alteração.'); pause(); return
    try:
        novo=int(raw)
    except ValueError:
        print('\n[ERRO] Digite um número inteiro de segundos.'); pause(); return
    if novo<30 or novo>3600:
        print('\n[ERRO] Use um valor entre 30 e 3600 segundos (1h).'); pause(); return
    cfg['timeout_verificacao_direitos_autorais_segundos']=novo
    save_json(cfg_file(acc),cfg)
    print(f'\nAtualizado: {novo}s ({novo/60:.1f} min). Vale a partir do próximo agendamento desta conta.')
    pause()

def find_chrome():
    if os.name=='nt':
        for env,tail in [('ProgramFiles',r'Google\Chrome\Application\chrome.exe'),('ProgramFiles(x86)',r'Google\Chrome\Application\chrome.exe'),('LocalAppData',r'Google\Chrome\Application\chrome.exe')]:
            base=os.environ.get(env)
            if base and (Path(base)/tail).exists(): return str(Path(base)/tail)
    return shutil.which('chrome') or shutil.which('google-chrome') or shutil.which('chromium')

def find_ollama():
    x=shutil.which('ollama')
    if x: return x
    if os.name=='nt':
        p=Path(os.environ.get('LOCALAPPDATA',''))/'Programs'/'Ollama'/'ollama.exe'
        if p.exists(): return str(p)
    return None

def import_ok(module): return subprocess.call(py_cmd()+['-c',f'import {module}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)==0

def ollama_models():
    try:
        with urllib.request.urlopen('http://localhost:11434/api/tags',timeout=3) as r: data=json.loads(r.read().decode('utf-8'))
        return {str(x.get('name') or x.get('model') or '').split(':')[0].lower() for x in data.get('models',[])}
    except Exception: return set()

def dependency_status():
    mods=ollama_models()
    return {
      'Python':sys.version_info>=(3,10), 'FFmpeg':bool(shutil.which('ffmpeg')), 'FFprobe':bool(shutil.which('ffprobe')),
      'Google Chrome':bool(find_chrome()), 'Ollama':bool(find_ollama()), 'playwright':import_ok('playwright'),
      'faster-whisper':import_ok('faster_whisper'), 'tzdata':import_ok('tzdata'),
      'modelo llama3.2':'llama3.2' in mods, 'modelo moondream':'moondream' in mods,
    }

def winget_install(pkgid):
    if not shutil.which('winget'): return False
    return subprocess.call(['winget','install','--id',pkgid,'-e','--accept-package-agreements','--accept-source-agreements'])==0

def dependencies():
    while True:
        cls(); print('=== INSTALAR / VERIFICAR DEPENDÊNCIAS ===\n')
        st=dependency_status()
        for k,v in st.items(): print(f'{k:<20}: {"OK" if v else "FALTANDO"}')
        print('\n1 - Instalar/corrigir automaticamente o que for possível')
        print('2 - Verificar novamente')
        print('0 - Voltar')
        op=input('> ').strip()
        if op=='0': return
        if op=='2': continue
        if op!='1': continue
        print('\nInstalando pacotes Python...')
        subprocess.call(py_cmd()+['-m','pip','install','--upgrade','playwright','faster-whisper','tzdata'])
        subprocess.call(py_cmd()+['-m','playwright','install','chromium'])
        if not find_chrome():
            print('\nTentando instalar Google Chrome pelo winget...'); winget_install('Google.Chrome')
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            print('\nTentando instalar FFmpeg pelo winget...'); winget_install('Gyan.FFmpeg')
        if not find_ollama():
            print('\nTentando instalar Ollama pelo winget...'); winget_install('Ollama.Ollama')
        ollama=find_ollama()
        if ollama:
            # Tenta iniciar o serviço, depois baixa os dois modelos usados.
            try:
                flags=0x08000000 if os.name=='nt' else 0
                subprocess.Popen([ollama,'serve'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
            except Exception: pass
            print('\nVerificando modelos Ollama...')
            subprocess.call([ollama,'pull','llama3.2']); subprocess.call([ollama,'pull','moondream'])
        print('\nInstalação/verificação terminou. Se FFmpeg/Ollama acabaram de ser instalados,')
        print('pode ser necessário fechar e abrir o painel para o PATH do Windows atualizar.')
        pause()

def main():
    migration=initialize_app_storage()
    for p in [ACCOUNTS/'youtube',ACCOUNTS/'tiktok',REMOVED]: p.mkdir(parents=True,exist_ok=True)
    if migration.conflicts:
        print('[ATENÇÃO] A migração de dados antigos encontrou conflitos e NÃO sobrescreveu nenhum arquivo.')
        print('Detalhes locais:', PATHS.migration_state)
        pause()
    while True:
        cls(); print('='*62); print('       PAINEL OFICIAL - YOUTUBE + TIKTOK'); print('='*62)
        print('''\n1 - LIMPAR META DADOS OFICIAL
2 - GERAR / EDITAR TEXTOS DAS POSTAGENS
3 - POSTAR / AGENDAR
4 - ADICIONAR CONTA
5 - REMOVER CONTA
6 - VER CONTAS CADASTRADAS
7 - INSTALAR / VERIFICAR DEPENDÊNCIAS
8 - VERIFICAÇÃO DE DIREITOS AUTORAIS (YOUTUBE)
9 - EDITAR HORÁRIOS DE POSTAGEM (POR DIA DA SEMANA)

0 - SAIR''')
        print('\n'+'='*62)
        op=input('Escolha uma opção: ').strip()
        if op=='0': return
        if op=='1': clean_metadata()
        elif op=='2': texts_menu()
        elif op=='3': post_schedule()
        elif op=='4': add_account()
        elif op=='5': remove_account()
        elif op=='6': show_accounts()
        elif op=='7': dependencies()
        elif op=='8': configurar_verificacao_direitos_autorais_youtube()
        elif op=='9': editar_horarios_conta_existente()

if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: print('\nCancelado pelo usuário.')
