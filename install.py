#!/usr/bin/env python3
"""Interactive Ubuntu/Debian installer. --check validates without installing."""
import argparse
import ast
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).resolve().parent

def paint(text, color='36'):
    return '\033[' + color + 'm' + text + '\033[0m' if sys.stdout.isatty() and 'NO_COLOR' not in os.environ else text

def banner():
    print(paint('\n  ╭──────────────────────────────────────────────╮\n'
                '  │  N O O K                                    │\n'
                '  │  Твой помощник, который живёт на сервере     │\n'
                '  │  Codex  ·  Antigravity  ·  Mercury           │\n'
                '  ╰──────────────────────────────────────────────╯', '1;36'))
    print('  Настроим Telegram, модели и автоматический запуск.\n')

def section(number, title):
    print(paint('\n  [' + str(number) + '/7] ' + title, '1;36'), flush=True)

def quiet(args, label, **kwargs):
    log = Path.home() / '.cache/nook/install.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    stopped = threading.Event()
    def spin():
        frames = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        n = 0
        while not stopped.wait(0.1):
            print('\r  ' + paint(frames[n % len(frames)]) + ' ' + label, end='', flush=True)
            n += 1
    spinner = threading.Thread(target=spin, daemon=True) if sys.stdout.isatty() else None
    print('  … ' + label, end='' if spinner else '\n', flush=True)
    if spinner:
        spinner.start()
    try:
        with log.open('a') as f:
            f.write('\n--- ' + label + ' ---\n')
            f.flush()
            run(args, stdout=f, stderr=subprocess.STDOUT, **kwargs)
    except BaseException:
        stopped.set()
        if spinner:
            spinner.join()
        print('\r  ' + paint('✗', '31') + ' ' + label + '\n  Подробности: ' + str(log), flush=True)
        raise
    else:
        stopped.set()
        if spinner:
            spinner.join()
        print('\r  ' + paint('✓', '32') + ' ' + label, flush=True)

def run(args, **kw):
    subprocess.run([str(x) for x in args], check=True, **kw)

def validate(root):
    manifest = json.loads((root / 'MANIFEST.json').read_text())
    for rel, expected in manifest['sha256'].items():
        p = root / rel
        if not p.is_file() or p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            raise SystemExit('Повреждён файл: ' + rel)
    for p in root.rglob('*'):
        if p.is_file() and p.suffix == '.py':
            ast.parse(p.read_text(), filename=str(p))
    run(['bash', '-n', root / 'install.sh'])
    print('Состав, контрольные суммы и синтаксис проверены.')

def install_codex():
    if shutil.which('codex'):
        return
    machine = {'x86_64': 'x86_64', 'aarch64': 'aarch64'}.get(platform.machine())
    if not machine:
        raise SystemExit('Нужен Codex CLI для архитектуры ' + platform.machine())
    asset = f'codex-{machine}-unknown-linux-musl'
    url = f'https://github.com/openai/codex/releases/latest/download/{asset}.tar.gz'
    dest = Path.home() / '.local/bin'
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nook-codex-') as temp:
        archive = Path(temp) / 'codex.tar.gz'
        print('Загружаю официальный Codex CLI с github.com/openai/codex…')
        with urllib.request.urlopen(url, timeout=120) as response, archive.open('wb') as f:
            shutil.copyfileobj(response, f)
        with tarfile.open(archive) as tf:
            matches = [m for m in tf.getmembers() if m.isfile() and Path(m.name).name in (asset, 'codex')]
            if len(matches) != 1:
                raise SystemExit('Неожиданный состав официального архива Codex.')
            content = tf.extractfile(matches[0])
            with (dest / 'codex').open('wb') as f:
                shutil.copyfileobj(content, f)
        (dest / 'codex').chmod(0o755)

def render(root):
    replacements = {'__NOOK_USER_HOME__': str(Path.home()),
                    '__NOOK_UID__': str(os.getuid()), '__NOOK_USERNAME__': getpass.getuser()}
    for p in root.rglob('*'):
        if p.is_file() and '.venv' not in p.parts and '.git' not in p.parts:
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            for old, new in replacements.items():
                text = text.replace(old, new)
            # Paths in the portable source use the default project name.
            text = text.replace(str(Path.home() / 'nook'), str(root))
            p.write_text(text)

def quote_unit(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'

def install_antigravity(env):
    dest = Path.home() / '.local/bin'
    binary = dest / 'agy'
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    existing = shutil.which('agy', path=env['PATH'])
    if existing:
        dest.mkdir(parents=True, exist_ok=True)
        binary.symlink_to(Path(existing).resolve())
        return binary
    print('Устанавливаю Antigravity официальным установщиком Google…')
    with tempfile.TemporaryDirectory(prefix='nook-agy-') as temp:
        script = Path(temp) / 'install.sh'
        with urllib.request.urlopen('https://antigravity.google/cli/install.sh', timeout=60) as response:
            script.write_bytes(response.read())
        quiet(['bash', script, '--dir', dest], 'Antigravity CLI и проверка загрузки', env=env)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit('Установщик Google не создал исполняемый agy.')
    return binary

def antigravity_ready(binary, env):
    # No inference, no account details in installer output.
    token = Path.home() / '.gemini/antigravity-cli/antigravity-oauth-token'
    if not token.is_file():
        return False
    try:
        return subprocess.run([str(binary), 'models'], env=env, timeout=30,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except subprocess.TimeoutExpired:
        return False

def setup_mercury(target, key_file=None):
    print('Mercury 3: создай свой API-ключ на https://platform.inceptionlabs.ai/ .')
    while True:
        key = key_file.read_text().strip() if key_file else getpass.getpass(
            'Ключ INCEPTION_API_KEY (ввод скрыт; Enter — пропустить): ').strip()
        if not key:
            print('Mercury пропущен. Настроить позже можно через --providers-only.')
            return False
        if not re.fullmatch(r'[A-Za-z0-9_.-]{16,}', key):
            if key_file:
                raise SystemExit('Неверный формат ключа Mercury в файле.')
            print('Ключ должен быть одной строкой без пробелов. Попробуй ещё раз.')
            continue
        print('Проверяю доступ к Mercury 3 коротким запросом (до 32 токенов)…')
        req = urllib.request.Request('https://api.inceptionlabs.ai/v1/chat/completions',
            data=json.dumps({'model': 'mercury-3', 'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                             'reasoning_effort': 'low', 'max_completion_tokens': 32}).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        verified = False
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                verified = bool(json.load(response).get('choices'))
        except urllib.error.HTTPError as exc:
            print('Проверка Mercury: HTTP ' + str(exc.code) + '. Проверь ключ, доступ и баланс.')
        except (OSError, ValueError):
            print('Не удалось проверить Mercury: ошибка сети или ответа сервиса.')
        if not verified:
            action = input('Повторить ввод [r], сохранить без проверки [s], пропустить [Enter]: ').strip().lower()
            if action == 'r' and not key_file:
                continue
            if action != 's':
                return False
        path = target / 'secrets.env'
        content = path.read_text() if path.exists() else ''
        content = re.sub(r'^\s*(?:export\s+)?INCEPTION_API_KEY=.*\n?', '', content, flags=re.M)
        # Temporary files are private from creation, including --providers-only.
        with tempfile.NamedTemporaryFile(mode='w', dir=target, prefix='.secrets-', delete=False) as temp:
            secret_temp = Path(temp.name)
            temp.write(content.rstrip() + '\nINCEPTION_API_KEY=' + key + '\n')
        try:
            os.replace(secret_temp, path)
        finally:
            secret_temp.unlink(missing_ok=True)
        print('Mercury: ' + ('доступ проверен, ключ сохранён.' if verified else 'ключ сохранён без проверки доступа.'))
        return True

def setup_providers(args, target, env):
    if not args.skip_codex:
        section(4, 'Codex · вход в аккаунт')
        install_codex()
        result = subprocess.run(['codex', 'login', 'status'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            print('Войди в СВОЙ аккаунт Codex. Код авторизации никому не пересылай.')
            run(['codex', 'login', '--device-auth'], env=env)
    if not args.skip_antigravity:
        section(5, 'Antigravity · вход в Google')
        binary = install_antigravity(env)
        while not antigravity_ready(binary, env):
            if not sys.stdin.isatty():
                raise SystemExit('Для входа в Antigravity нужен интерактивный SSH-терминал. Повтори с --providers-only или используй --skip-antigravity.')
            print('Сейчас откроется Antigravity. Войди в свой Google-аккаунт по ссылке на экране.')
            print('После входа выйди из Antigravity через /exit или Ctrl+C — установщик продолжится.')
            input('Нажми Enter, чтобы открыть вход: ')
            with tempfile.TemporaryDirectory(prefix='nook-agy-login-') as temp:
                try:
                    subprocess.run([str(binary)], cwd=temp, env=env)
                except KeyboardInterrupt:
                    pass
            if antigravity_ready(binary, env):
                print('Antigravity: вход проверен.')
                break
            if input('Вход не подтверждён. Enter — повторить, s — пропустить: ').strip().lower() == 's':
                print('Antigravity пока не авторизован. Повтори настройку через --providers-only.')
                break
        else:
            print('Antigravity: существующий вход проверен.')
    if not args.skip_mercury:
        section(6, 'Mercury · подключение API')
        setup_mercury(target, args.mercury_key_file)

def setup_service(target, timezone, no_start, path_env):
    section(7, 'Автозапуск и завершение')
    python = target / '.venv/bin/python'
    service_dir = Path.home() / '.config/systemd/user'
    service_dir.mkdir(parents=True, exist_ok=True)
    service = service_dir / 'nook.service'
    if service.exists():
        raise SystemExit('nook.service уже существует; он не перезаписан. Бот установлен без нового автозапуска.')
    service.write_text('[Unit]\nDescription=Nook Telegram assistant\nAfter=network-online.target\n\n[Service]\nType=simple\n'
                       + 'WorkingDirectory=' + str(target).replace('%', '%%') + '\n'
                       + 'ExecStart=' + quote_unit(python) + ' ' + quote_unit(target / 'bot.py') + '\n'
                       + 'Environment=' + quote_unit('PATH=' + path_env) + '\n'
                       + 'Environment=' + quote_unit('TZ=' + timezone) + '\n'
                       + 'Environment=' + quote_unit('NOOK_HOME=' + str(target)) + '\n'
                       + 'Restart=always\nRestartSec=5\nUMask=0077\n\n[Install]\nWantedBy=default.target\n')
    quiet(['systemctl', '--user', 'daemon-reload'], 'Обновление списка служб')
    quiet(['systemctl', '--user', 'enable', 'nook.service'], 'Автозапуск после перезагрузки')
    if not no_start:
        quiet(['systemctl', '--user', 'start', 'nook.service'], 'Запуск Nook')
        quiet(['systemctl', '--user', 'is-active', '--quiet', 'nook.service'], 'Проверка службы')
    print('Включаю работу пользовательской службы после выхода из SSH:')
    run(['sudo', 'loginctl', 'enable-linger', getpass.getuser()])
    print(paint('\n  ✓ Nook установлен!', '1;32'))
    print('  ' + ('Бот подготовлен; запуск отложен (--no-start).' if no_start else 'Открой своего бота в Telegram и напиши ему привет.'))
    print('  Папка: ' + str(target) + '\n  Инструкция: README.md\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--target', type=Path, default=Path.home() / 'nook')
    ap.add_argument('--no-start', action='store_true')
    ap.add_argument('--no-system-packages', action='store_true')
    ap.add_argument('--skip-codex', action='store_true')
    ap.add_argument('--skip-antigravity', action='store_true')
    ap.add_argument('--skip-mercury', action='store_true')
    ap.add_argument('--mercury-key-file', type=Path)
    ap.add_argument('--finish-service', action='store_true', help='Создать службу после прерванной установки')
    ap.add_argument('--providers-only', action='store_true', help='Настроить модели в уже установленном боте')
    ap.add_argument('--voice', action='store_true')
    ap.add_argument('--timezone', default='Europe/Moscow')
    ap.add_argument('--owner-id', type=int)
    ap.add_argument('--token-file', type=Path)
    args = ap.parse_args()
    if args.check or not (args.providers_only or args.finish_service):
        validate(SOURCE)
    if args.check:
        return
    if sys.version_info < (3, 10) or platform.system() != 'Linux':
        raise SystemExit('Нужен Linux и Python 3.10+. Рекомендуется Ubuntu 22.04/24.04.')
    if os.getuid() == 0:
        raise SystemExit('Запускай от обычного пользователя с sudo, не от root.')
    banner()
    ZoneInfo(args.timezone)
    target = args.target.expanduser().resolve()
    path_env = f"{target}/.venv/bin:{target}/bin:{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin"
    env = dict(os.environ, PATH=path_env)
    if args.providers_only and args.finish_service:
        raise SystemExit('Выбери один режим: --providers-only или --finish-service.')
    if args.finish_service:
        if not (target / 'config.json').is_file() or not (target / '.venv/bin/python').is_file():
            raise SystemExit('Сначала установи бота и Python-окружение.')
        setup_service(target, args.timezone, args.no_start, path_env)
        return
    if args.providers_only:
        if not (target / 'config.json').is_file():
            raise SystemExit('Сначала установи бота; --providers-only требует существующий config.json.')
        setup_providers(args, target, env)
        print('Настройка моделей завершена. Если бот уже запущен, перезапусти его службу.')
        return
    if any(c in str(target) for c in ('\n', '"', '\\')):
        raise SystemExit('Путь содержит неподдерживаемые символы.')
    if (target / 'config.json').exists() or (target != SOURCE and target.exists() and any(target.iterdir())):
        raise SystemExit('Папка назначения уже используется. Установка ничего не перезапишет.')
    section(1, 'Знакомство · Telegram')
    token = args.token_file.read_text().strip() if args.token_file else getpass.getpass('Токен НОВОГО Telegram-бота от BotFather (ввод скрыт): ').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{30,}', token):
        raise SystemExit('Неверный формат токена.')
    owner = args.owner_id
    if owner is None:
        value = input('Твой числовой Telegram ID (можно узнать у @userinfobot): ').strip()
        if not value.isdigit() or int(value) <= 0:
            raise SystemExit('Нужен положительный числовой Telegram ID.')
        owner = int(value)
    if owner <= 0:
        raise SystemExit('Telegram ID должен быть положительным.')
    section(2, 'Подготовка системы')
    if not args.no_system_packages:
        run(['sudo', '-v'])
        quiet(['sudo', '-n', 'apt-get', 'update'], 'Обновление списка пакетов')
        quiet(['sudo', '-n', 'apt-get', 'install', '-y', 'python3-venv', 'python3-pip', 'ca-certificates', 'git', 'curl', 'openssh-client', 'procps', 'iproute2', 'ffmpeg'], 'Системные зависимости')
    os.umask(0o077)
    if target != SOURCE:
        shutil.copytree(SOURCE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git', 'dist', '__pycache__', '*.pyc', '*.bak*'))
    shutil.copytree(target / "templates/runtime", target, dirs_exist_ok=True)
    render(target)
    for folder in ('memory','logs','inbox','keys','spool/outbox','spool/mods','spool/root','tmp'):
        (target / folder).mkdir(parents=True, exist_ok=True)
    cfg = json.loads((target / 'config.example.json').read_text())
    cfg.update(bot_token=token, owner_id=owner, pair_code=secrets.token_urlsafe(24))
    (target / 'config.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n')
    (target / 'config.json').chmod(0o600)
    (target / 'secrets.env').write_text((target / 'secrets.env.example').read_text())
    (target / 'secrets.env').chmod(0o600)
    (target / 'state.json').write_text('{"ui":"classic"}\n')
    section(3, 'Окружение Nook')
    quiet([sys.executable, '-m', 'venv', target / '.venv'], 'Изолированное окружение Python')
    python = target / '.venv/bin/python'
    quiet([python, '-m', 'pip', 'install', '-r', target / 'requirements.txt'], 'Установка компонентов бота')
    if args.voice:
        quiet([python, '-m', 'pip', 'install', '-r', target / 'requirements-voice.txt'], 'Распознавание голосовых')
    setup_providers(args, target, env)
    setup_service(target, args.timezone, args.no_start, path_env)

if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        # Do not print credentials, command environment or request URLs.
        sys.exit('Установка остановлена (' + type(exc).__name__ + '). Проверь последнюю операцию выше; настройки сохранены с правами 600.')
