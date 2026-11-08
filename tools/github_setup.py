"""Optional GitHub identity setup; never changes the host user's Git credentials."""
import getpass
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_token(home):
    for line in (Path(home) / 'secrets.env').read_text().splitlines():
        if line.startswith('GITHUB_PERSONAL_ACCESS_TOKEN='):
            return line.split('=', 1)[1].strip()
    return ''


def atomic_private(path, text):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.github-', delete=False) as f:
        temp = Path(f.name)
        f.write(text)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def setup_github(home, mode=None, token_file=None, expected=None):
    home = Path(home)
    if mode is None:
        choices = {'1': 'owner', '2': 'agent', '3': 'skip'}
        print('GitHub: 1 — аккаунт хозяина; 2 — отдельный аккаунт агента; 3 — позже')
        while mode is None:
            mode = choices.get(input('Твой выбор [Enter = 3]: ').strip() or '3')
    if mode == 'skip':
        print('GitHub пропущен; подключить позже: install.py --github-only.')
        return False
    if mode not in ('owner', 'agent'):
        raise ValueError('Неизвестный тип GitHub-аккаунта')
    if mode == 'agent':
        print('Создай отдельный аккаунт на https://github.com/signup и подтверди почту.')
        print('GitHub требует ручную регистрацию человеком. Готовый аккаунт можно использовать сразу.')
        print('Для своих приватных репозиториев пригласи агента как collaborator и прими приглашение.')
        print('Для PR в чужие репозитории и доступа collaborator нужен classic PAT: https://github.com/settings/tokens/new')
        print('Для публичных проектов достаточно public_repo. Для приватных нужен repo: он даёт доступ ко всем доступным аккаунту репозиториям.')
        print('Fine-grained PAT подходит для собственных репозиториев агента, но ограничен для PR в чужие.')
    else:
        print('В выбранном аккаунте создай fine-grained PAT: https://github.com/settings/personal-access-tokens/new')
        print('Выбери нужные репозитории: Contents и Pull requests — Read and write; Metadata — Read.')
    print('Доступ к Actions workflows добавляй только если агенту нужно менять файлы workflow.')
    expected = expected or input('Логин выбранного GitHub-аккаунта (Enter — пропустить подключение): ').strip()
    if not expected:
        return False
    token = Path(token_file).read_text().strip() if token_file else getpass.getpass('GitHub PAT (ввод скрыт; Enter — позже): ').strip()
    if not token:
        return False
    if not re.fullmatch(r'[A-Za-z0-9_]+', token):
        raise SystemExit('Неверный формат GitHub PAT; настройки не изменены.')
    req = urllib.request.Request('https://api.github.com/user', headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'Nook'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            user = json.load(response)
    except (OSError, ValueError):
        print('GitHub: не удалось проверить доступ. Настройки не изменены; повтори с --github-only.')
        return False
    if not isinstance(user, dict):
        print('GitHub: неверный ответ сервиса. Настройки не изменены.')
        return False
    login, uid = user.get('login', ''), user.get('id')
    if (not isinstance(login, str) or login.casefold() != expected.casefold()
            or not re.fullmatch(r'[A-Za-z0-9-]+', login) or type(uid) is not int or uid <= 0):
        print('GitHub: токен принадлежит другому аккаунту или ответ неверен. Настройки не изменены.')
        return False
    cfg_path, secrets_path = home / 'config.json', home / 'secrets.env'
    cfg = json.loads(cfg_path.read_text())
    content = secrets_path.read_text() if secrets_path.exists() else ''
    content = re.sub(r'^\s*(?:export\s+)?GITHUB_PERSONAL_ACCESS_TOKEN=.*\n?', '', content, flags=re.M)
    identity = {'mode': mode, 'login': login, 'id': uid,
                'email': f'{uid}+{login}@users.noreply.github.com'}
    cfg['github'] = identity
    atomic_private(secrets_path, content.rstrip() + '\nGITHUB_PERSONAL_ACCESS_TOKEN=' + token + '\n')
    atomic_private(cfg_path, json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    print('GitHub подключён: ' + login + '. Личность проверена; права на конкретный репозиторий проверяются при работе.')
    print('Для Git используй bin/github-git, для API — bin/github-api. Общие настройки Git не менялись.')
    return True
