# -*- coding: utf-8 -*-
from pathlib import Path
import json, os, shutil, subprocess, sys

try:
    from .app_paths import account_dir_from_env, account_paths
except ImportError:
    from app_paths import account_dir_from_env, account_paths

BASE = account_dir_from_env(standalone_namespace="login_conta")
ACCOUNT_PATHS = account_paths(BASE)
CFG_FILE = ACCOUNT_PATHS.config

def load_cfg():
    try:
        return json.loads(CFG_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}

def find_chrome():
    candidates = []
    if os.name == 'nt':
        for env, tail in [
            ('ProgramFiles', r'Google\Chrome\Application\chrome.exe'),
            ('ProgramFiles(x86)', r'Google\Chrome\Application\chrome.exe'),
            ('LocalAppData', r'Google\Chrome\Application\chrome.exe'),
        ]:
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / tail)
    which = shutil.which('chrome') or shutil.which('google-chrome') or shutil.which('chromium')
    if which:
        candidates.append(Path(which))
    for p in candidates:
        if p.exists():
            return p
    return None

def main():
    cfg = load_cfg()
    platform = str(cfg.get('plataforma') or '').lower()
    name = cfg.get('nome_conta') or cfg.get('nome_canal') or BASE.name
    if platform not in ('youtube', 'tiktok'):
        print('[ERRO] Plataforma inválida na configuração da conta.')
        return 2
    chrome = find_chrome()
    if not chrome:
        print('[ERRO] Google Chrome não encontrado. Use a opção Dependências no painel.')
        return 2
    if platform == 'youtube':
        profile = ACCOUNT_PATHS.profile_youtube
        url = 'https://studio.youtube.com/'
        title = 'YOUTUBE'
    else:
        profile = ACCOUNT_PATHS.profile_tiktok
        url = 'https://www.tiktok.com/tiktokstudio/upload?from=creator_center'
        title = 'TIKTOK'
    profile.mkdir(parents=True, exist_ok=True)
    print('=' * 72)
    print(f'ADICIONAR / ATUALIZAR LOGIN {title}: {name}')
    print('=' * 72)
    print('Um Chrome normal será aberto usando APENAS o perfil desta conta.')
    print('Faça login na conta correta e, quando terminar, feche TODAS as janelas')
    print('desse Chrome. O painel continuará depois que o Chrome fechar.')
    print()
    cmd = [str(chrome), f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check', '--new-window', url]
    try:
        p = subprocess.Popen(cmd)
        p.wait()
    except KeyboardInterrupt:
        print('\nCancelado pelo usuário.')
        return 130
    print('\nLogin/perfil salvo em:', profile)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
