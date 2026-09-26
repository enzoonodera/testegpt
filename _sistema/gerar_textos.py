# -*- coding: utf-8 -*-
from pathlib import Path
import argparse, base64, csv, hashlib, json, os, re, shutil, subprocess, time, urllib.request, urllib.error

try:
    from .app_paths import account_dir_from_env, account_paths
    from .state_json import load_state_json
    from .time_utils import utc_now_iso
except ImportError:
    from app_paths import account_dir_from_env, account_paths
    from state_json import load_state_json
    from time_utils import utc_now_iso

BASE = account_dir_from_env(standalone_namespace="gerar_textos")
ACCOUNT_PATHS = account_paths(BASE)
DATA_DIR = ACCOUNT_PATHS.data
VIDEO_DIR = ACCOUNT_PATHS.videos
TEXTS_FILE = DATA_DIR / 'textos_postagem.json'
CSV_FILE = DATA_DIR / 'textos_postagem.csv'
TRANSCRIPT_DIR = DATA_DIR / 'transcricoes'
FRAME_DIR = DATA_DIR / 'frames_ia_temp'
CONFIG_FILE = ACCOUNT_PATHS.config
VIDEO_EXTS = {'.mp4','.mov','.m4v','.webm','.avi','.mkv','.mts','.m2ts'}
PIPELINE_BASE = 'TEXTOS_MULTIPLATAFORMA_V1'

LANG_NAMES = {
    'en-US':'American English','en-GB':'British English','en-CA':'Canadian English',
    'pt-BR':'Brazilian Portuguese','es-MX':'Mexican Spanish','es-ES':'Spanish (Spain)',
    'de-DE':'German','fr-FR':'French','it-IT':'Italian','ja-JP':'Japanese',
    'ko-KR':'Korean','hi-IN':'Hindi','id-ID':'Indonesian',
}

def load_json(path, default):
    return load_state_json(path, default)

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)

def natural_key(p):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', p.name.lower())]

def fingerprint(path):
    h = hashlib.sha256(); size = path.stat().st_size; h.update(str(size).encode('ascii')); chunk=1024*1024
    with path.open('rb') as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(0,size-chunk)); h.update(f.read(chunk))
    return h.hexdigest()

def check_cmd(name): return shutil.which(name) is not None

def video_duration(video):
    r=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(video)],capture_output=True,text=True)
    try: return max(0.0,float((r.stdout or '0').strip()))
    except Exception: return 0.0

def extract_one_frame(video,max_width=320):
    FRAME_DIR.mkdir(parents=True,exist_ok=True)
    work=FRAME_DIR/fingerprint(video)[:16]
    shutil.rmtree(work,ignore_errors=True); work.mkdir(parents=True,exist_ok=True)
    sec=video_duration(video)*0.5; out=work/'frame.jpg'; vf=f"scale='min({max_width},iw)':-2"
    cmds=[
        ['ffmpeg','-y','-loglevel','error','-ss',f'{sec:.3f}','-i',str(video),'-frames:v','1','-vf',vf,'-q:v','6',str(out)],
        ['ffmpeg','-y','-loglevel','error','-i',str(video),'-frames:v','1','-vf',vf,'-q:v','6',str(out)],
    ]
    for cmd in cmds:
        subprocess.run(cmd,capture_output=True,text=True)
        if out.exists() and out.stat().st_size>0: return out,work
    return None,work

def extract_visual_frames(video, max_width=384):
    """Extrai até 3 frames (25%, 50%, 75%) para fallback da IA visual."""
    FRAME_DIR.mkdir(parents=True,exist_ok=True)
    work=FRAME_DIR/(fingerprint(video)[:16]+'_vision')
    shutil.rmtree(work,ignore_errors=True); work.mkdir(parents=True,exist_ok=True)
    duration=video_duration(video)
    ratios=[0.25,0.50,0.75] if duration>0.5 else [0.0]
    frames=[]
    for idx,ratio in enumerate(ratios,1):
        sec=max(0.0, duration*ratio)
        if duration>0.1: sec=min(sec,max(0.0,duration-0.05))
        out=work/f'frame_{idx}.jpg'
        vf=f"scale='min({max_width},iw)':-2"
        cmd=['ffmpeg','-y','-loglevel','error','-ss',f'{sec:.3f}','-i',str(video),'-frames:v','1','-vf',vf,'-q:v','5',str(out)]
        subprocess.run(cmd,capture_output=True,text=True)
        if out.exists() and out.stat().st_size>0: frames.append(out)
    if not frames:
        out=work/'frame_fallback.jpg'
        subprocess.run(['ffmpeg','-y','-loglevel','error','-i',str(video),'-frames:v','1','-vf',f"scale='min({max_width},iw)':-2",'-q:v','5',str(out)],capture_output=True,text=True)
        if out.exists() and out.stat().st_size>0: frames.append(out)
    return frames,work

def image_b64(path): return base64.b64encode(path.read_bytes()).decode('ascii')

def ollama_chat(base_url,payload,timeout=120):
    req=urllib.request.Request(base_url.rstrip('/')+'/api/chat',data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body=e.read().decode('utf-8',errors='replace')
        raise RuntimeError(f'Ollama HTTP {e.code}: {body[:800]}') from e

def ollama_alive(base_url,timeout=4):
    try:
        urllib.request.urlopen(base_url.rstrip('/')+'/api/tags',timeout=timeout).close(); return True
    except Exception: return False

def try_start_ollama(base_url):
    if ollama_alive(base_url): return True
    exe=shutil.which('ollama')
    if not exe and os.name=='nt':
        p=Path(os.environ.get('LOCALAPPDATA',''))/'Programs'/'Ollama'/'ollama.exe'
        if p.exists(): exe=str(p)
    if not exe: return False
    try:
        flags=0x08000000 if os.name=='nt' else 0
        subprocess.Popen([str(exe),'serve'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
        for _ in range(12):
            time.sleep(0.5)
            if ollama_alive(base_url): return True
    except Exception: pass
    return False

def parse_json_text(text):
    text=(text or '').strip()
    try: return json.loads(text)
    except Exception: pass
    m=re.search(r'\{.*\}',text,flags=re.S)
    if m:
        try: return json.loads(m.group(0))
        except Exception: pass
    return None

def visual_summary(base_url,model,frame):
    """Descrição visual com fallback /api/chat -> /api/generate para compatibilidade do Ollama."""
    prompt=('Describe only what is visibly happening in this short-video frame in one concise sentence. '
            'Do not invent names, context, brands, or events that are not visible. Return only the description.')
    img=image_b64(frame)

    # 1) API chat (preferida)
    try:
        payload={'model':model,'messages':[{'role':'user','content':prompt,'images':[img]}],
                 'stream':False,'keep_alive':'30m','options':{'temperature':0.15,'num_predict':90}}
        data=ollama_chat(base_url,payload,120)
        text=str(((data.get('message') or {}).get('content')) or '').strip()
        if text: return text
    except Exception:
        pass

    # 2) Alguns modelos/builds (especialmente visão) respondem melhor em /api/generate.
    try:
        payload={'model':model,'prompt':prompt,'images':[img],'stream':False,'keep_alive':'30m',
                 'options':{'temperature':0.15,'num_predict':90}}
        req=urllib.request.Request(base_url.rstrip('/')+'/api/generate',data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=120) as resp:
            data=json.loads(resp.read().decode('utf-8'))
        text=str(data.get('response') or '').strip()
        if text: return text
    except Exception:
        pass

    return ''

def transcribe(model,video):
    segs,info=model.transcribe(str(video),task='transcribe',language=None,vad_filter=True,beam_size=1,condition_on_previous_text=False)
    parts=[]
    for s in segs:
        t=(s.text or '').strip()
        if t: parts.append(t)
    return ' '.join(parts).strip(), getattr(info,'language',None)

def transcript_cache(fp): return TRANSCRIPT_DIR/f'{fp}.json'

def get_transcript(model,video,fp):
    p=transcript_cache(fp); row=load_json(p,{})
    if isinstance(row,dict) and 'text' in row:
        return str(row.get('text') or ''), row.get('language'), True
    text,lang=transcribe(model,video)
    save_json(p,{'text':text,'language':lang,'saved_at':utc_now_iso()})
    return text,lang,False

def clean_tags(vals,platform):
    if isinstance(vals,str): vals=re.split(r'[\s,]+',vals)
    out=[]
    for x in vals or []:
        x=re.sub(r'\s+','',str(x).strip())
        if not x: continue
        if not x.startswith('#'): x='#'+x
        if x.lower() not in {y.lower() for y in out}: out.append(x)
    if platform=='youtube' and not any(x.lower()=='#shorts' for x in out): out.insert(0,'#Shorts')
    fallbacks=['#ViralVideo','#Trending','#ShortVideo','#ForYou'] if platform=='youtube' else ['#fyp','#viral','#foryou','#trending']
    for x in fallbacks:
        if len(out)>=4: break
        if x.lower() not in {y.lower() for y in out}: out.append(x)
    return out[:4]

def metadata_signature(cfg):
    relevant={'pipeline':PIPELINE_BASE,'platform':cfg.get('plataforma'),'locale':cfg.get('idioma_metadata'),'country':cfg.get('pais_alvo'),
              'text_model':(cfg.get('ia_local') or {}).get('ollama_modelo_texto','llama3.2'),
              'vision_model':(cfg.get('ia_local') or {}).get('ollama_modelo_visao','moondream')}
    raw=json.dumps(relevant,sort_keys=True,ensure_ascii=False).encode('utf-8')
    return 'TXT_'+hashlib.sha1(raw).hexdigest()[:12]

def generate_text(base_url,model,source,platform,locale,country):
    lang=LANG_NAMES.get(locale,locale)
    if platform=='youtube':
        prompt=f'''Create localized YouTube Shorts post text for this target market.
Target country/market: {country}
Language/locale: {lang}
Source content: {source[:5000]}
Requirements:
- Write naturally for viewers in {country}; do not use forced slang or literal translation.
- TITLE: 3-8 words, factual curiosity/clarity, <=100 characters, no hashtags, no quotes.
- DESCRIPTION: 1-2 short sentences, specific to the content, natural and compact. Do not invent facts.
- HASHTAGS: exactly 4, including #Shorts plus 3 topic-relevant hashtags.
- Return valid JSON only: {{"title":"...","description":"...","hashtags":["#...","#...","#...","#..."]}}.'''
    else:
        prompt=f'''Create localized TikTok post text for this target market.
Target country/market: {country}
Language/locale: {lang}
Source content: {source[:5000]}
Requirements:
- Write naturally for viewers in {country}; do not use forced slang or literal translation.
- CAPTION: one short engaging sentence, ideally <=120 characters before hashtags, specific to the content.
- HASHTAGS: exactly 4, topic-relevant and natural for this market. Do not spam generic tags if a specific tag is better.
- Do not invent facts unsupported by the source.
- Return valid JSON only: {{"caption":"...","hashtags":["#...","#...","#...","#..."]}}.'''
    payload={'model':model,'messages':[{'role':'user','content':prompt}],'stream':False,'format':'json','keep_alive':'30m','options':{'temperature':0.35,'num_predict':320}}
    obj=parse_json_text(((ollama_chat(base_url,payload,120).get('message') or {}).get('content')))
    if not isinstance(obj,dict): raise RuntimeError('A IA não devolveu JSON válido.')
    tags=clean_tags(obj.get('hashtags'),platform)
    if platform=='youtube':
        title=re.sub(r'\s+',' ',str(obj.get('title') or '').strip().strip('"\''))[:100].strip()
        desc=str(obj.get('description') or '').strip()
        if not title or not desc: raise RuntimeError('Título/descrição inválidos retornados pela IA.')
        return {'titulo':title,'descricao':desc,'hashtags':tags}
    caption=re.sub(r'\s+',' ',str(obj.get('caption') or '').strip()).strip()
    if not caption: raise RuntimeError('Caption inválida retornada pela IA.')
    return {'caption':caption,'hashtags':tags}

def export_csv(data,platform):
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    rows=[]
    for fp,e in data.items():
        if not isinstance(e,dict): continue
        rows.append({'arquivo':e.get('arquivo',''),'titulo':e.get('titulo',''),'descricao':e.get('descricao',''),
                     'caption':e.get('caption',''),'hashtags':' '.join(e.get('hashtags') or []),
                     'pais_alvo':e.get('pais_alvo',''),'idioma':e.get('idioma',''),'status':e.get('status',''),
                     'atualizado_em':e.get('atualizado_em',''),'fingerprint':fp})
    rows.sort(key=lambda r:[int(x) if x.isdigit() else x for x in re.split(r'(\d+)',r['arquivo'].lower())])
    fields=['arquivo','titulo','descricao','caption','hashtags','pais_alvo','idioma','status','atualizado_em','fingerprint']
    with CSV_FILE.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--forcar',action='store_true'); ap.add_argument('--limite',type=int,default=0); args=ap.parse_args()
    cfg=load_json(CONFIG_FILE,{})
    platform=str(cfg.get('plataforma') or '').lower(); country=cfg.get('pais_alvo','Estados Unidos'); locale=cfg.get('idioma_metadata','en-US')
    if platform not in ('youtube','tiktok'):
        print('[ERRO] Plataforma inválida na conta.'); return 2
    ia=cfg.get('ia_local') or {}; whisper_size=ia.get('whisper_model_size','small'); base_url=ia.get('ollama_url','http://localhost:11434')
    text_model=ia.get('ollama_modelo_texto','llama3.2'); vision_model=ia.get('ollama_modelo_visao','moondream')
    DATA_DIR.mkdir(parents=True,exist_ok=True); TRANSCRIPT_DIR.mkdir(parents=True,exist_ok=True); VIDEO_DIR.mkdir(parents=True,exist_ok=True)
    files=sorted([p for p in VIDEO_DIR.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],key=natural_key)
    data=load_json(TEXTS_FILE,{})
    sig=metadata_signature(cfg)
    pending=[]
    for p in files:
        fp=fingerprint(p); e=data.get(fp)
        ok=isinstance(e,dict) and e.get('status')=='done' and (e.get('manual_edit') or e.get('pipeline_versao')==sig)
        if args.forcar or not ok: pending.append((p,fp))
    if args.limite>0: pending=pending[:args.limite]
    print('='*76); print(f'TEXTOS DAS POSTAGENS — {platform.upper()} — {cfg.get("nome_conta",BASE.name)}'); print('='*76)
    print('País/mercado :',country); print('Idioma       :',LANG_NAMES.get(locale,locale)); print('Vídeos       :',len(files)); print('Já prontos   :',len(files)-len(pending)); print('Pendentes    :',len(pending)); print()
    if not pending:
        export_csv(data,platform); print('Nada novo para gerar. O histórico já está em dia.'); return 0
    if not check_cmd('ffmpeg') or not check_cmd('ffprobe'):
        print('[ERRO] FFmpeg/ffprobe não encontrados. Use Dependências no painel.'); return 2
    if not try_start_ollama(base_url):
        print('[ERRO] Ollama não está disponível. Use Dependências no painel.'); return 2
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print('[ERRO] faster-whisper não instalado. Use Dependências no painel.'); return 2
    print('Carregando Whisper:',whisper_size)
    model=WhisperModel(whisper_size,device='cpu',compute_type='int8')
    okn=fail=0
    for i,(video,fp) in enumerate(pending,1):
        print(f'\n[{i}/{len(pending)}] {video.name}')
        work=None
        try:
            text,lang,cached=get_transcript(model,video,fp)
            if cached: print('    [CACHE] Transcrição reaproveitada.')
            if text:
                source='Transcript: '+text
            else:
                print('    Sem fala detectada; analisando frames do vídeo...')
                frames,work=extract_visual_frames(video)
                if not frames: raise RuntimeError('Não consegui extrair frames do vídeo.')
                summary=''
                for pos,frame in enumerate(frames,1):
                    print(f'    IA visual: tentativa {pos}/{len(frames)}...')
                    summary=visual_summary(base_url,vision_model,frame)
                    if summary: break
                if not summary: raise RuntimeError('IA visual não conseguiu descrever nenhum dos frames.')
                source='Visual description: '+summary
            result=generate_text(base_url,text_model,source,platform,locale,country)
            e={'arquivo':video.name,'plataforma':platform,'pais_alvo':country,'idioma':locale,'status':'done',
               'pipeline_versao':sig,'atualizado_em':utc_now_iso(),**result}
            data[fp]=e; save_json(TEXTS_FILE,data); export_csv(data,platform)
            print('    [SALVO]', result.get('titulo') or result.get('caption'))
            okn+=1
        except Exception as ex:
            e=data.get(fp) if isinstance(data.get(fp),dict) else {'arquivo':video.name}
            e.update({'plataforma':platform,'status':'failed','ultimo_erro':str(ex),'atualizado_em':utc_now_iso()})
            data[fp]=e; save_json(TEXTS_FILE,data); export_csv(data,platform)
            try:
                logdir=ACCOUNT_PATHS.logs; logdir.mkdir(parents=True,exist_ok=True)
                with (logdir/'textos_erros.log').open('a',encoding='utf-8') as lf:
                    lf.write(f"{utc_now_iso()} | {video.name} | {ex}\n")
            except Exception:
                pass
            print('    [ERRO]',ex); print('    [RESUME] Esse vídeo continua pendente para a próxima execução.')
            fail+=1
        finally:
            if work: shutil.rmtree(work,ignore_errors=True)
    print('\n'+'='*76); print(f'Concluído: {okn} OK / {fail} falha(s)'); print('Histórico:',TEXTS_FILE); print('CSV:',CSV_FILE); print('Na próxima execução, somente os pendentes serão processados.'); print('='*76)
    return 0 if fail==0 else 1

if __name__=='__main__': raise SystemExit(main())
