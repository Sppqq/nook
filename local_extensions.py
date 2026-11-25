"""Version, validate, activate and recover local Nook extensions."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from extension_sandbox import ExtensionError, MAX_JSON, MAX_SOURCE, encode_payload, execute
from nook_update import atomic, path_at
from release_channels import parse_version

KINDS = ('mod', 'skill', 'tool')
NAME = re.compile(r'[a-z][a-z0-9_]{0,19}')
HASH = re.compile(r'[0-9a-f]{64}')
POLICY_DIR = Path('/etc/nook/extensions')
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
EMPTY = {'schema': 1, 'versions': {}, 'active': None, 'previous': None, 'history': []}
MAX_HISTORY = 2000
MAX_VERSIONS = 1000


def read_fd(fd, limit, *, trusted=False):
    with os.fdopen(fd, 'rb') as stream:
        spec = os.fstat(stream.fileno())
        if not stat.S_ISREG(spec.st_mode) or spec.st_nlink != 1:
            raise ExtensionError('Ожидался обычный файл расширения.')
        if trusted and (spec.st_uid != 0 or spec.st_mode & 0o022):
            raise ExtensionError('Политика расширений должна принадлежать root.')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ExtensionError('Файл расширения превышает допустимый размер.')
        return data


def read_policy():
    fd = os.open('/', DIRECTORY)
    try:
        for part in POLICY_DIR.parts[1:]:
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            spec = os.fstat(fd)
            if spec.st_uid != 0 or spec.st_mode & 0o022:
                raise ExtensionError('Каталог политики должен принадлежать root.')
        policy_fd = os.open(str(os.getuid()) + '.json', READ, dir_fd=fd)
        policy = json.loads(read_fd(policy_fd, MAX_JSON, trusted=True))
        if not isinstance(policy, dict) or policy.get('schema') != 1 or not isinstance(policy.get('allow'), dict):
            raise ExtensionError('Неизвестный формат политики расширений.')
        if 'legacy' in policy and not isinstance(policy['legacy'], dict):
            raise ExtensionError('Политика доверенных модулей должна содержать хеши.')
        return policy
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ExtensionError('Не удалось безопасно прочитать политику расширений.') from None
    finally:
        os.close(fd)


def approval(kind, name, revision):
    policy = read_policy()
    return bool(policy and policy['allow'].get(f'{kind}:{name}') in (revision, 'isolated-v1'))


def trusted_module(name, path):
    policy = read_policy()
    if policy is None or 'legacy' not in policy:
        return None
    data = read_fd(os.open(path, READ), 5 * 1024 * 1024)
    if policy['legacy'].get(name) != hashlib.sha256(data).hexdigest():
        raise ExtensionError('Доверенный модуль не разрешён действующей политикой владельца.')
    return data


def identify(kind, name):
    if kind not in KINDS or not NAME.fullmatch(name):
        raise ExtensionError('Имя: до 20 букв a-z, цифр или подчёркиваний, начиная с буквы.')
    return kind + '-' + name


class Store:
    def __init__(self, home):
        self.home = Path(home)

    @contextmanager
    def lock(self, kind, name):
        root = path_at(self.home, 'spool/local-extensions/' + identify(kind, name))
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'a') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ExtensionError('Небезопасная блокировка расширения.')
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield root

    def status(self, kind, name):
        root = path_at(self.home, 'spool/local-extensions/' + identify(kind, name))
        return self.state(root)

    def catalog(self):
        root = path_at(self.home, 'spool/local-extensions')
        if not root.exists():
            return []
        entries = []
        for path in sorted(root.iterdir()):
            kind, _, name = path.name.partition('-')
            if kind in KINDS and NAME.fullmatch(name) and not path.is_symlink() and path.is_dir():
                entries.append((kind, name))
        return entries

    def state(self, root):
        try:
            fd = os.open(root / 'state.json', READ)
        except FileNotFoundError:
            return json.loads(json.dumps(EMPTY))
        state = json.loads(read_fd(fd, 1024 * 1024))
        if not isinstance(state, dict) or state.get('schema') != 1:
            raise ExtensionError('Неизвестный формат состояния расширения.')
        return state

    def record(self, root, state, event, version):
        state['history'].append({'event': event, 'version': version, 'at': int(time.time())})
        if len(state['history']) > MAX_HISTORY:
            state['history'].pop(0)
            state['history_dropped'] = state.get('history_dropped', 0) + 1
        data = json.dumps(state, ensure_ascii=False, indent=2).encode() + b'\n'
        if len(data) > 1024 * 1024:
            raise ExtensionError('Журнал расширения заполнен.')
        atomic(root / 'state.json', data)

    def stage(self, kind, name, version, source):
        if len(version) > 64:
            raise ExtensionError('Слишком длинный номер версии.')
        parse_version(version)
        data = read_fd(os.open(source, READ), MAX_JSON if kind == 'skill' else MAX_SOURCE)
        if not data or '\0' in data.decode('utf-8'):
            raise ExtensionError('Исходник должен быть непустым текстом UTF-8.')
        checksum = hashlib.sha256(data).hexdigest()
        with self.lock(kind, name) as root:
            state = self.state(root)
            old = state['versions'].get(version)
            if old is not None:
                if old['sha256'] != checksum:
                    raise ExtensionError('У этой версии уже другой исходник. Создай новую версию.')
                return checksum
            if len(state['versions']) >= MAX_VERSIONS:
                raise ExtensionError('Достигнут предел числа локальных версий.')
            atomic(root / checksum, data)
            state['versions'][version] = {'sha256': checksum, 'checked': False}
            self.record(root, state, 'staged', version)
        return checksum

    def revision(self, root, state, version):
        entry = state['versions'].get(version)
        checksum = entry.get('sha256') if isinstance(entry, dict) else None
        if not isinstance(checksum, str) or not HASH.fullmatch(checksum):
            raise ExtensionError('Версия расширения отсутствует.')
        data = read_fd(os.open(root / checksum, READ), MAX_SOURCE)
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ExtensionError('Исходник изменён после регистрации версии.')
        return entry, data

    def probe(self, kind, data):
        if kind == 'skill':
            if not data.decode('utf-8').strip():
                raise ExtensionError('Текст навыка пуст.')
        elif execute(data, {'healthcheck': True}) != {'ok': True}:
            raise ExtensionError('Проверка работоспособности вернула неожиданный результат.')

    def test(self, kind, name, version):
        with self.lock(kind, name) as root:
            state = self.state(root)
            entry, data = self.revision(root, state, version)
            self.probe(kind, data)
            entry['checked'] = True
            self.record(root, state, 'checked', version)

    def activate(self, kind, name, version):
        with self.lock(kind, name) as root:
            state = self.state(root)
            entry, data = self.revision(root, state, version)
            if not entry['checked'] or not approval(kind, name, entry['sha256']):
                raise ExtensionError('Нужны пройденная проверка и разрешение владельца.')
            self.probe(kind, data)
            if state['active'] != version:
                state['previous'], state['active'] = state['active'], version
                self.record(root, state, 'activated', version)

    def revert(self, kind, name, root, state):
        previous = state['previous']
        if previous is not None:
            try:
                entry, data = self.revision(root, state, previous)
                if not entry['checked'] or not approval(kind, name, entry['sha256']):
                    raise ExtensionError('Предыдущая версия не разрешена.')
                self.probe(kind, data)
            except (ExtensionError, OSError, ValueError):
                previous = None
        failed = state['active']
        state['active'], state['previous'] = previous, None
        self.record(root, state, 'rolled_back' if previous else 'disabled', failed)
        return previous

    def rollback(self, kind, name):
        with self.lock(kind, name) as root:
            state = self.state(root)
            if state['active'] is None:
                raise ExtensionError('Активной версии нет.')
            return self.revert(kind, name, root, state)

    def run(self, kind, name, payload):
        encode_payload(payload)
        with self.lock(kind, name) as root:
            state = self.state(root)
            if state['active'] is None:
                raise ExtensionError('Активной версии нет.')
            entry, data = self.revision(root, state, state['active'])
            if not entry['checked'] or not approval(kind, name, entry['sha256']):
                raise ExtensionError('Владелец не разрешил активную версию.')
            if kind == 'skill':
                return {'text': data.decode('utf-8')}
            try:
                return execute(data, payload)
            except ExtensionError:
                self.revert(kind, name, root, state)
                raise ExtensionError('Расширение завершилось с ошибкой. Выполнен откат или отключение.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('NOOK_HOME') or Path(__file__).parent))
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('stage', 'test', 'activate', 'status', 'rollback', 'run'):
        command = actions.add_parser(name)
        command.add_argument('kind', choices=KINDS)
        command.add_argument('name')
        if name in ('stage', 'test', 'activate'):
            command.add_argument('version')
        if name == 'stage':
            command.add_argument('source', type=Path)
        elif name == 'run':
            command.add_argument('--input', default='{}')
    args = vars(parser.parse_args())
    operation = args.pop('action')
    store = Store(args.pop('home').resolve())
    try:
        if operation == 'run':
            args['payload'] = json.loads(args.pop('input'))
        result = getattr(store, operation)(**args)
        print(json.dumps(result, ensure_ascii=False))
    except (ExtensionError, OSError, ValueError, KeyError) as error:
        sys.exit(str(error))


if __name__ == '__main__':
    main()
