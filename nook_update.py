#!/usr/bin/env python3
"""Conservative source updates. Never run the installer against an existing bot."""
import argparse
import ast
import contextlib
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
REPO = 'Sppqq/nook'
STATE = '.nook-install.json'
TOP = {'MANIFEST.json', 'README.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'LICENSE',
       'SECURITY.md', '.gitignore', 'config.example.json', 'secrets.env.example',
       'requirements.txt', 'requirements-voice.txt', 'bootstrap.sh', 'install.sh'}
# These paths are source only. In particular: no runtime templates are seeded,
# no policy/configuration directory is updated, no service/provider setup runs.
DIRS = {'bin', 'mods', 'docs', 'tests', 'tools', 'setup'}


class UpdateError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.nook-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def json_write(path, data):
    atomic(path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())


def safe_rel(name):
    p = PurePosixPath(name)
    if (not isinstance(name, str) or not p.parts or p.is_absolute() or
            '..' in p.parts or '\\' in name or str(p) != name):
        raise UpdateError('Небезопасный путь в обновлении.')
    return p


def managed(name):
    p = safe_rel(name)
    return (name in TOP or (len(p.parts) == 1 and p.suffix == '.py') or
            p.parts[0] in DIRS or
            (p.parts[:2] == ('.agents', 'skills')))


def path_at(root, name):
    p = root
    for part in safe_rel(name).parts:
        p = p / part
        if p.is_symlink():
            raise UpdateError('Символическая ссылка: ' + name)
    return p


def workspace(home):
    base = path_at(home, 'spool/updates')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


@contextlib.contextmanager
def lock(home):
    base = workspace(home)
    with path_at(base, 'lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError('Другое обновление уже выполняется.') from None
        yield


@contextlib.contextmanager
def runtime_lock(home):
    """Shared by the bot and updater; no source writes while a bot is alive."""
    with path_at(home, '.nook-runtime.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError('Бот уже работает; код не изменён.') from None
        yield


def rendered(data, home):
    text = data.decode('utf-8')
    for old, new in {('__NOOK_' + 'USER_HOME__'): str(Path.home()), ('__NOOK_' + 'UID__'): str(os.getuid()),
                     ('__NOOK_' + 'USERNAME__'): getpass.getuser()}.items():
        text = text.replace(old, new)
    return text.replace(str(Path.home() / 'nook'), str(home)).encode()


def record_install(home):
    """Call once, after the installer renders source, before the first bot start."""
    manifest = read_json(home / 'MANIFEST.json')
    files = {n: digest(path_at(home, n).read_bytes()) for n in manifest['sha256'] if managed(n)}
    files['MANIFEST.json'] = digest((home / 'MANIFEST.json').read_bytes())
    json_write(home / STATE, {'schema': 1, 'revision': 'installed', 'files': files})


def baseline(home):
    state = path_at(home, STATE)
    if state.exists():
        data = read_json(state)
        if data.get('schema') != 1:
            raise UpdateError('Нужна отдельная миграция формата установки.')
        for name, value in data['files'].items():
            if not managed(name) or not re.fullmatch('[a-f0-9]{64}', value):
                raise UpdateError('Некорректная запись установленной версии.')
        return data
    manifest = path_at(home, 'MANIFEST.json')
    if not manifest.is_file():
        raise UpdateError('Нет эталона публичного Nook. Эту частную или старую установку нельзя автоматически заменить кодом с GitHub; нужен проверенный перенос с сохранением данных.')
    # Legacy public installations: do not guess hashes of rendered/local changes.
    data = read_json(manifest)
    files = {n: h for n, h in data['sha256'].items() if managed(n)}
    files['MANIFEST.json'] = digest(manifest.read_bytes())
    return {'schema': 1, 'revision': 'legacy', 'files': files}


def download(url, dest, limit):
    req = urllib.request.Request(url, headers={'User-Agent': 'Nook-source-updater'})
    with urllib.request.urlopen(req, timeout=45) as response, dest.open('wb') as out:
        size = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise UpdateError('Слишком большой ответ GitHub.')
            out.write(chunk)


def unpack(archive, dest):
    total, seen = 0, set()
    with tarfile.open(archive) as tf:
        for i, member in enumerate(tf):
            parts = safe_rel(member.name.rstrip('/')).parts
            if i > 2000 or len(parts) < 1:
                raise UpdateError('Некорректный архив GitHub.')
            if member.isdir():
                continue
            if len(parts) < 2 or not member.isfile():
                raise UpdateError('Ссылки и специальные файлы в архиве запрещены.')
            name = '/'.join(parts[1:])
            if name in seen:
                raise UpdateError('Повтор файла в архиве.')
            seen.add(name)
            total += member.size
            if total > 50 * 1024 * 1024 or member.size > 5 * 1024 * 1024:
                raise UpdateError('Слишком большой архив GitHub.')
            p = path_at(dest, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(member) as data, p.open('wb') as out:
                shutil.copyfileobj(data, out)
            p.chmod(0o755 if member.mode & 0o111 else 0o644)


def verify_source(source):
    manifest = read_json(source / 'MANIFEST.json')
    if manifest.get('format') != 1 or manifest.get('update_api') != 1 or not manifest.get('sha256'):
        raise UpdateError('Неподдерживаемый манифест обновления.')
    expected = set(manifest['sha256']) | {'MANIFEST.json'}
    actual = {str(p.relative_to(source)) for p in source.rglob('*') if p.is_file()}
    if actual != expected:
        raise UpdateError('Состав архива не совпадает с манифестом.')
    for name, wanted in manifest['sha256'].items():
        p = path_at(source, name)
        if not re.fullmatch('[a-f0-9]{64}', wanted) or digest(p.read_bytes()) != wanted:
            raise UpdateError('Не совпала контрольная сумма: ' + name)
        if p.suffix == '.py':
            ast.parse(p.read_text(), filename=name)
    return manifest


def plan_source(home, source, revision, stage):
    base = baseline(home)
    manifest = verify_source(source)
    names = {n for n in manifest['sha256'] if managed(n)} | {'MANIFEST.json'}
    missing = set(base['files']) - names
    if missing:
        raise UpdateError('Обновление удаляет старые файлы; нужна отдельная миграция.')
    for name in ('requirements.txt', 'requirements-voice.txt'):
        if path_at(home, name).read_bytes() != (source / name).read_bytes():
            raise UpdateError('Изменились зависимости. Нужна отдельная миграция окружения; бот сохранён.')
    changes, conflicts = {}, []
    for name in sorted(names):
        src = path_at(source, name)
        data = rendered(src.read_bytes(), home)
        wanted = digest(data)
        dst = path_at(home, name)
        current = digest(dst.read_bytes()) if dst.is_file() else None
        old = base['files'].get(name)
        if current == wanted:
            continue
        if old is not None and current != old:
            # Preserve a local edit when upstream did not change that file.
            if wanted == old:
                continue
            conflicts.append(name)
            continue
        if old is None and dst.exists():
            conflicts.append(name)
            continue
        prepared = path_at(stage / 'files', name)
        atomic(prepared, data, src.stat().st_mode & 0o777)
        changes[name] = {'before': current, 'after': wanted,
                         'mode': src.stat().st_mode & 0o777}
    if conflicts:
        raise UpdateError('Сохранены местные правки; обновление отменено. Конфликты: ' + ', '.join(conflicts[:12]))
    plan = {'schema': 1, 'id': stage.name, 'revision': revision, 'changes': changes,
            'baseline': base, 'created': time.time()}
    json_write(stage / 'plan.json', plan)
    return plan


def prepare(home):
    with lock(home):
        baseline(home)  # Refuse private/unknown installs before downloading anything.
        base = workspace(home)
        if (base / 'transaction.json').exists():
            txn = read_json(base / 'transaction.json')
            if txn['status'] not in ('success', 'rolled_back'):
                raise UpdateError('Есть незавершённое обновление. Сначала проверь статус и выполни откат.')
        ident = uuid.uuid4().hex[:16]
        stage = base / 'staged' / ident
        stage.mkdir(parents=True, mode=0o700)
        download('https://api.github.com/repos/' + REPO + '/commits/main', stage / 'commit.json', 2 * 1024 * 1024)
        revision = read_json(stage / 'commit.json')['sha']
        if not re.fullmatch('[a-f0-9]{40}', revision):
            raise UpdateError('Некорректная версия GitHub.')
        download('https://codeload.github.com/' + REPO + '/tar.gz/' + revision,
                 stage / 'source.tar.gz', 10 * 1024 * 1024)
        source = stage / 'source'
        source.mkdir()
        unpack(stage / 'source.tar.gz', source)
        return plan_source(home, source, revision, stage)


def get_plan(home, ident):
    if not re.fullmatch('[a-f0-9]{16}', ident):
        raise UpdateError('Некорректный номер обновления.')
    stage = path_at(workspace(home), 'staged/' + ident)
    plan = read_json(path_at(stage, 'plan.json'))
    if plan['id'] != ident or time.time() - plan['created'] > 3600:
        raise UpdateError('Проверка устарела. Сначала проверь обновления ещё раз.')
    for name, item in plan['changes'].items():
        if not managed(name):
            raise UpdateError('Защищённый путь в плане обновления.')
        current = path_at(home, name)
        actual = digest(current.read_bytes()) if current.is_file() else None
        if actual != item['before']:
            raise UpdateError('После проверки файл изменился: ' + name)
        if digest(path_at(stage / 'files', name).read_bytes()) != item['after']:
            raise UpdateError('Подготовленный файл повреждён: ' + name)
    if baseline(home) != plan['baseline']:
        raise UpdateError('Установленная версия изменилась после проверки.')
    return stage, plan


def request(home, ident):
    with lock(home):
        _, plan = get_plan(home, ident)
        if not plan['changes']:
            return False
        json_write(workspace(home) / 'request.json', {'id': ident})
        return True


def apply_files(home, ident):
    """Caller holds lock; bot MUST be stopped. Persistent intent precedes writes."""
    stage, plan = get_plan(home, ident)
    base = workspace(home)
    backup = base / 'backups' / ident
    backup.mkdir(parents=True, mode=0o700)
    changes = dict(plan['changes'])
    state = dict(plan['baseline'], files=dict(plan['baseline']['files']), revision=plan['revision'])
    for name, item in changes.items():
        state['files'][name] = item['after']
    state_data = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()
    old_state = path_at(home, STATE)
    changes[STATE] = {'before': digest(old_state.read_bytes()) if old_state.exists() else None,
                      'after': digest(state_data), 'mode': 0o600}
    txn = {'id': ident, 'revision': plan['revision'], 'status': 'applying', 'files': changes, 'written': []}
    # Complete backups before the first live write.
    for name, item in changes.items():
        if item['before'] is not None:
            p = path_at(home, name)
            saved = path_at(backup, name)
            atomic(saved, p.read_bytes(), p.stat().st_mode & 0o777)
    json_write(base / 'transaction.json', txn)
    for name, item in changes.items():
        txn['written'].append(name)
        json_write(base / 'transaction.json', txn)
        data = state_data if name == STATE else path_at(stage / 'files', name).read_bytes()
        atomic(path_at(home, name), data, item['mode'])
    txn['status'] = 'awaiting_health'
    json_write(base / 'transaction.json', txn)
    return txn


def restore(home):
    """Rollback code only. Refuse to overwrite any edits made after the update."""
    base = workspace(home)
    txn = read_json(path_at(base, 'transaction.json'))
    backup = path_at(base, 'backups/' + txn['id'])
    # Preflight all targets before restoring anything.
    for name in txn['written']:
        if name != STATE and not managed(name):
            raise UpdateError('Небезопасный путь отката.')
        p = path_at(home, name)
        item = txn['files'][name]
        actual = digest(p.read_bytes()) if p.is_file() else None
        if actual not in (item['before'], item['after']):
            raise UpdateError('После обновления появились местные правки: ' + name)
        if item['before'] is not None and digest(path_at(backup, name).read_bytes()) != item['before']:
            raise UpdateError('Резервная копия повреждена: ' + name)
    for name in reversed(txn['written']):
        p = path_at(home, name)
        item = txn['files'][name]
        if item['before'] is None:
            if p.exists():
                p.unlink()  # Only our own newly installed source files.
        else:
            saved = path_at(backup, name)
            atomic(p, saved.read_bytes(), saved.stat().st_mode & 0o777)
    txn['status'] = 'rolled_back'
    json_write(base / 'transaction.json', txn)


def systemctl(*args, check=True):
    p = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=45)
    if check and p.returncode:
        raise UpdateError('Не удалось выполнить операцию со службой Nook.')
    return p


def service_matches(home, pid=None):
    result = systemctl('show', 'nook.service', '--property=WorkingDirectory,MainPID,ActiveState', check=False)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if values.get('WorkingDirectory') != str(home) or values.get('ActiveState') != 'active':
        raise UpdateError('Автообновление поддерживает только активную nook.service этой установки.')
    main_pid = int(values.get('MainPID', '0'))
    if main_pid <= 0 or (pid is not None and main_pid != pid):
        raise UpdateError('Служба перезапустилась. Проверь обновления ещё раз.')
    return main_pid


def offline(home):
    if not path_at(home, STATE).is_file():
        raise UpdateError('Старой установке сначала нужна проверенная миграция обновлятора.')
    # Never edit a running bot, including a manually launched installation.
    try:
        health = read_json(path_at(workspace(home), 'health.json'))
        if time.time() - health['time'] < 15:
            raise UpdateError('Бот работает. Используй обновление через меню.')
    except FileNotFoundError:
        pass
    p = systemctl('show', 'nook.service', '--property=ActiveState', '--value', check=False)
    if p.stdout.strip() in ('active', 'activating', 'deactivating', 'reloading'):
        raise UpdateError('Сначала останови nook.service; данные не изменены.')


def heartbeat(home, failed=()):
    """Called by the running bot only after Telegram startup has succeeded."""
    base = workspace(home)
    state = path_at(home, STATE)
    revision = read_json(state).get('revision') if state.exists() else 'legacy'
    json_write(base / 'health.json', {'pid': os.getpid(), 'time': time.time(),
                                     'revision': revision, 'failed_mods': sorted(failed)})


def healthy(home, revision, since, timeout=90, stable=20):
    deadline, good_since, last_pid = time.monotonic() + timeout, None, None
    while time.monotonic() < deadline:
        try:
            h = read_json(path_at(workspace(home), 'health.json'))
            pid = service_matches(home)
            good = (h['pid'] == pid and h['revision'] == revision and
                    h['time'] >= since and time.time() - h['time'] < 8 and not h['failed_mods'])
        except (OSError, ValueError, UpdateError):
            good, pid = False, None
        if good:
            if good_since is None or pid != last_pid:
                good_since = time.monotonic()
            if time.monotonic() - good_since >= stable:
                return True
        else:
            good_since = None
        last_pid = pid
        time.sleep(2)
    return False


def notify(home, message):
    try:
        cfg = read_json(home / 'config.json')
        req = urllib.request.Request('https://api.telegram.org/bot' + cfg['bot_token'] + '/sendMessage',
            data=json.dumps({'chat_id': cfg['owner_id'], 'text': message}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception:
        pass  # No token-bearing URLs or provider exceptions in output/logs.


def worker(home, ident, pid):
    with lock(home):
        get_plan(home, ident)
        service_matches(home, pid)
        # Everything required for rollback is in this independent transient unit.
        pending = path_at(workspace(home), 'request.json')
        if pending.exists():
            pending.unlink()
        stopped = False
        try:
            systemctl('stop', 'nook.service')
            stopped = True
            with runtime_lock(home):
                apply_files(home, ident)
            since = time.time()
            systemctl('start', 'nook.service')
            txn_path = workspace(home) / 'transaction.json'
            txn = read_json(txn_path)
            if not healthy(home, txn['revision'], since):
                raise UpdateError('Новая версия не прошла проверку запуска.')
            txn['status'] = 'success'
            json_write(txn_path, txn)
            notify(home, '✅ Nook обновлён с GitHub. Настройки, память и свои дополнения сохранены; проверка запуска пройдена.')
        except BaseException:
            if stopped:
                systemctl('stop', 'nook.service', check=False)
                txn_path = workspace(home) / 'transaction.json'
                try:
                    if txn_path.exists() and read_json(txn_path)['id'] == ident:
                        with runtime_lock(home):
                            restore(home)
                    systemctl('start', 'nook.service')
                    # The old release may predate the updater heartbeat.
                    service_matches(home)
                    notify(home, '↩️ Обновление Nook не прошло проверку. Старый код восстановлен, служба запущена. Настройки и память не заменялись.')
                except Exception:
                    notify(home, '⚠️ Обновление Nook остановлено. Автоматическое восстановление требует проверки; резервная копия кода сохранена в spool/updates/backups. Личные данные не заменялись.')
            raise


def launch(home, ident, pid):
    service_matches(home, pid)
    stage, _ = get_plan(home, ident)
    # Freeze supervisor code outside the files being replaced. User systemd keeps
    # this process alive while stopping/restarting the bot's own service.
    supervisor = stage / 'worker.py'
    atomic(supervisor, Path(__file__).read_bytes())
    p = subprocess.run(['systemd-run', '--user', '--collect', '--quiet',
        '--unit=nook-update-' + ident, '--property=UMask=0077', sys.executable,
        str(supervisor), '--home', str(home), '_worker', ident, '--pid', str(pid)],
        capture_output=True, timeout=20)
    if p.returncode:
        raise UpdateError('Не удалось запустить независимый обновлятор. Бот не изменён.')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--home', type=Path, default=ROOT)
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('check')
    req = sub.add_parser('request')
    req.add_argument('id')
    req.add_argument('--yes', action='store_true', required=True)
    apply = sub.add_parser('apply-offline')
    apply.add_argument('id')
    apply.add_argument('--yes', action='store_true', required=True)
    sub.add_parser('status')
    roll = sub.add_parser('rollback-offline')
    roll.add_argument('--yes', action='store_true', required=True)
    work = sub.add_parser('_worker')
    work.add_argument('id')
    work.add_argument('--pid', type=int, required=True)
    args = ap.parse_args()
    home = args.home.expanduser().resolve()
    os.umask(0o077)
    if args.command == 'check':
        plan = prepare(home)
        print(json.dumps({'id': plan['id'], 'revision': plan['revision'],
                          'changed': list(plan['changes'])}, ensure_ascii=False))
    elif args.command == 'request':
        print('Обновление запрошено; мод дождётся завершения задач.' if request(home, args.id) else 'Обновлений нет.')
    elif args.command == '_worker':
        worker(home, args.id, args.pid)
    elif args.command in ('apply-offline', 'rollback-offline'):
        with lock(home), runtime_lock(home):
            offline(home)
            if args.command == 'apply-offline':
                try:
                    apply_files(home, args.id)
                    txn_path = workspace(home) / 'transaction.json'
                    txn = read_json(txn_path)
                    txn['status'] = 'success'
                    json_write(txn_path, txn)
                except BaseException:
                    txn_path = workspace(home) / 'transaction.json'
                    if txn_path.exists() and read_json(txn_path)['id'] == args.id:
                        restore(home)
                    raise
            else:
                restore(home)
        print('Код сохранён. Запуск и проверка бота выполняются отдельно.')
    else:
        txn_path = workspace(home) / 'transaction.json'
        print(json.dumps(read_json(txn_path) if txn_path.exists() else {'status': 'no_updates'}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc) if isinstance(exc, UpdateError) else 'Обновление остановлено (' + type(exc).__name__ + '). Личные данные не заменялись.', file=sys.stderr)
        sys.exit(1)
