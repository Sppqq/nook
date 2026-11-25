"""Private, integrity-checked Nook instance backups and new-directory restores."""
import argparse
import contextlib
import ctypes
from dataclasses import dataclass
import errno
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid

MANIFEST = 'NOOK_BACKUP.json'
MAX_FILES = 50000
MAX_FILE_SIZE = 512 * 1024 * 1024
MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_SIZE = MAX_TOTAL_SIZE + 32 * 1024 * 1024
EXCLUDED_DIRS = {'.git', '.venv', '__pycache__', 'dist'}
EXCLUDED_FILES = {'.nook-runtime.lock'}
UPDATE_EPHEMERAL = {'lock', 'health.json', 'request.json', 'staged'}
PROVIDER_HOME_FILES = (
    '.codex/auth.json',
    '.codex/config.toml',
    '.gemini/antigravity-cli/antigravity-oauth-token',
)


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupLimits:
    files: int = MAX_FILES
    file_size: int = MAX_FILE_SIZE
    total_size: int = MAX_TOTAL_SIZE
    archive_size: int = MAX_ARCHIVE_SIZE


DEFAULT_LIMITS = BackupLimits()
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
SQLITE_HEADER = b'SQLite format 3\0'
SQLITE_SIDECARS = ('-wal', '-shm', '-journal')


def safe_name(name):
    path = PurePosixPath(name)
    if (not isinstance(name, str) or not path.parts or path.is_absolute()
            or '..' in path.parts or '\\' in name or str(path) != name):
        raise BackupError('В архиве обнаружен небезопасный путь.')
    return path


def _backup_root(home):
    return Path(home) / 'spool' / 'backups'


def _excluded(rel, name):
    parts = rel.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    if any(part.startswith('.') and part.endswith('.tmp') for part in parts):
        return True
    if name in EXCLUDED_FILES or name == MANIFEST:
        return True
    if parts[:2] == ('spool', 'backups') or (len(parts) >= 3 and parts[:2] == ('spool', 'updates')
                                              and parts[2] in UPDATE_EPHEMERAL):
        return True
    return False


@contextlib.contextmanager
def _directory(path, *, create=False):
    fd = os.open('/', DIRECTORY_FLAGS)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _regular_at(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError('Обнаружена ссылка или специальный файл.')
        yield stream, info


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _private_mode(rel, mode):
    parts = PurePosixPath(rel).parts
    mode &= 0o777
    if (parts[0] in {'provider-home', 'keys', 'memory', 'spool', '.ssh', '.codex'}
            or parts[-1] in {'config.json', 'secrets.env', '.env', 'ssh_config', 'state.json', 'nodes.json'}
            or parts[-1].endswith(('.key', '.pem'))):
        mode &= 0o700
    return mode


def _files(home, limits, include_providers):
    root = home
    result = []
    total = 0

    def add_file(path, rel):
        nonlocal total
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError('Резервирование остановлено: ссылка или специальный файл `'
                              + rel.as_posix() + '`.')
        if info.st_size > limits.file_size:
            raise BackupError('Слишком большой файл для резервирования: ' + rel.as_posix())
        total += info.st_size
        if total > limits.total_size or len(result) >= limits.files:
            raise BackupError('Инстанс превышает лимит размера резервной копии.')
        result.append((path, rel.as_posix(), info))

    def walk_error(error):
        raise error

    for parent, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        base = Path(parent)
        rel_parent = base.relative_to(root)
        if any((base / name).is_symlink() for name in dirs):
            raise BackupError('Резервирование остановлено: символическая ссылка в каталоге.')
        dirs[:] = sorted(d for d in dirs if not _excluded(rel_parent / d, d))
        for name in sorted(names):
            path = base / name
            rel = path.relative_to(root)
            if _excluded(rel, name):
                continue
            add_file(path, rel)

    if not include_providers:
        return result, total
    provider_home = Path.home()

    def provider_path(name):
        path = provider_home
        for part in PurePosixPath(name).parts:
            path = path / part
            if path.is_symlink():
                raise BackupError('Резервирование остановлено: ссылка в данных провайдера.')
        return path

    for name in PROVIDER_HOME_FILES:
        path = provider_path(name)
        if path.exists():
            add_file(path, PurePosixPath('provider-home') / name)

    saved = provider_path('.codex/accounts')
    if saved.exists():
        if not saved.is_dir():
            raise BackupError('Каталог аккаунтов Codex не является обычной папкой.')
        for path in sorted(saved.iterdir()):
            if path.name == 'registry.json' or re.fullmatch(r'[A-Za-z0-9_-]{1,240}\.auth\.json', path.name):
                add_file(path, PurePosixPath('provider-home/.codex/accounts') / path.name)

    sessions = provider_path('.codex/sessions')
    if sessions.exists():
        if not sessions.is_dir():
            raise BackupError('Каталог сессий Codex не является обычной папкой.')
        for parent, dirs, names in os.walk(sessions, followlinks=False, onerror=walk_error):
            base = Path(parent)
            if any((base / name).is_symlink() for name in dirs):
                raise BackupError('Резервирование остановлено: ссылка в сессиях Codex.')
            dirs[:] = sorted(dirs)
            for name in sorted(names):
                path = base / name
                if path.suffix != '.jsonl':
                    continue
                rel = PurePosixPath('provider-home/.codex/sessions') / path.relative_to(sessions).as_posix()
                add_file(path, rel)
    return result, total


class _HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.hash.update(data)
        return data


@contextlib.contextmanager
def _lock(root):
    with _directory(_backup_root(root), create=True) as root_fd:
        os.fchmod(root_fd, 0o700)
        fd = os.open('.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=root_fd)
        with os.fdopen(fd, 'a+b') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise BackupError('Небезопасный файл блокировки резервных копий.')
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield Path(f'/proc/self/fd/{root_fd}')


def _database_files(files):
    databases = set()
    for path, rel, info in files:
        if info.st_size < len(SQLITE_HEADER):
            continue
        with _directory(path.parent) as parent, _regular_at(parent, path.name) as (stream, current):
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise BackupError('Файл подменён во время резервирования: ' + rel)
            if stream.read(len(SQLITE_HEADER)) == SQLITE_HEADER:
                databases.add(rel)
    return databases, {name + suffix for name in databases for suffix in SQLITE_SIDECARS}


@contextlib.contextmanager
def _sqlite_snapshot(parent, name, stage, limits):
    deadline = time.monotonic() + 300

    def progress(status, remaining, total):
        if time.monotonic() >= deadline or total * page_size > limits.file_size:
            raise BackupError('SQLite не удалось скопировать в пределах времени или лимита размера.')

    with tempfile.TemporaryDirectory(prefix='.sqlite-', dir=stage) as work:
        snapshot = Path(work) / 'snapshot.db'
        source = Path(f'/proc/self/fd/{parent}') / name
        with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as live, \
                contextlib.closing(sqlite3.connect(snapshot)) as copy:
            page_size = live.execute('PRAGMA page_size').fetchone()[0]
            live.backup(copy, pages=256, progress=progress)
            copy.execute('PRAGMA journal_mode=DELETE')
        with snapshot.open('rb') as stream:
            yield stream, os.fstat(stream.fileno()).st_size


def _archive_file(archive, stream, rel, size, original):
    reader = _HashingReader(stream)
    info = tarfile.TarInfo(rel)
    info.size = size
    info.mode = _private_mode(rel, original.st_mode)
    info.mtime = int(original.st_mtime)
    archive.addfile(info, reader)
    return {'sha256': reader.hash.hexdigest(), 'size': size, 'mode': info.mode}


def _write_archive(output, root, stage, limits, include_providers):
    files, total = _files(root, limits, include_providers)
    if not files:
        raise BackupError('В каталоге нет файлов для резервирования.')
    databases, sidecars = _database_files(files)
    # WAL pages can make the snapshot larger than the original main database.
    reserve = max((info.st_size for _, rel, info in files if rel in databases | sidecars), default=0)
    if shutil.disk_usage(stage).free < total + reserve + 16 * 1024 * 1024:
        raise BackupError('Недостаточно места для безопасной резервной копии.')
    manifest_files = {}
    original = {}
    saved_bytes = 0
    with tarfile.open(fileobj=output, mode='w:gz', format=tarfile.PAX_FORMAT, compresslevel=6) as archive:
        for path, rel, before in files:
            if rel in sidecars:
                continue
            with _directory(path.parent) as parent, _regular_at(parent, path.name) as (stream, current):
                if rel in databases:
                    original[rel] = _stamp(before)[:4]
                    if _stamp(current)[:4] != original[rel] or stream.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                        raise BackupError('База подменена во время резервирования: ' + rel)
                    with _sqlite_snapshot(parent, path.name, stage, limits) as (snapshot, size):
                        manifest_files[rel] = _archive_file(archive, snapshot, rel, size, before)
                else:
                    original[rel] = _stamp(before)
                    if _stamp(current) != original[rel]:
                        raise BackupError('Файл изменился во время резервирования: ' + rel)
                    manifest_files[rel] = _archive_file(archive, stream, rel, before.st_size, before)
            saved_bytes += manifest_files[rel]['size']
            if saved_bytes > limits.total_size:
                raise BackupError('Данные превысили лимит размера во время резервирования.')
        final_files, _ = _files(root, limits, include_providers)
        final = {rel: _stamp(info)[:4] if rel in databases else _stamp(info)
                 for _, rel, info in final_files if rel not in sidecars}
        if original != final:
            raise BackupError('Состав или данные инстанса изменились во время резервирования. Повтори offline.')
        payload = json.dumps({'format': 1, 'created': int(time.time()), 'files': manifest_files},
                             sort_keys=True, separators=(',', ':')).encode()
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(payload)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(payload))


def _age(source, output, option, value):
    mode = ['--decrypt'] if option == '--identity' else []
    try:
        os.lseek(source.fileno(), 0, os.SEEK_SET)
        subprocess.run(['age', *mode, option, str(value)], stdin=source, stdout=output,
                       stderr=subprocess.DEVNULL, check=True, timeout=1800)
    except (OSError, subprocess.SubprocessError):
        raise BackupError('Не удалось обработать age-архив. Проверь age и выбранный ключ.') from None


def _rotate(root, newest, keep):
    suffix = '.tar.gz.age' if newest.name.endswith('.age') else '.tar.gz'
    older = sorted((path for path in root.glob('nook-backup-*' + suffix) if path != newest
                    and not path.is_symlink() and path.is_file()), reverse=True)
    for path in older[keep - 1:]:
        path.unlink()


def create_backup(home, *, online=False, include_providers=False, limits=DEFAULT_LIMITS,
                  keep=None, recipient=None, identity=None):
    from nook_update import UpdateError, lock, path_at, read_json, runtime_lock

    if (recipient is None) != (identity is None):
        raise BackupError('Для зашифрованной копии укажи получателя и проверочный age-ключ.')
    if keep is not None and keep < 1:
        raise BackupError('Политика хранения требует хотя бы одну резервную копию.')
    root = Path(os.path.abspath(home))
    try:
        with _directory(root), lock(root), contextlib.ExitStack() as stack:
            if not online:
                stack.enter_context(runtime_lock(root))
            journal = path_at(root, 'spool/updates/transaction.json')
            if journal.exists() and read_json(journal).get('status') not in {'success', 'rolled_back'}:
                raise BackupError('Сначала восстанови незавершённое обновление.')
            backup_root = stack.enter_context(_lock(root))
            suffix = '.tar.gz.age' if recipient else '.tar.gz'
            name = 'nook-backup-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + suffix
            with tempfile.NamedTemporaryFile(prefix='.backup-', suffix=suffix, dir=backup_root) as output:
                if recipient:
                    with tempfile.TemporaryFile(dir=backup_root) as plain:
                        _write_archive(plain, root, backup_root, limits, include_providers)
                        plain.seek(0)
                        _age(plain, output, '--recipient', recipient)
                else:
                    _write_archive(output, root, backup_root, limits, include_providers)
                output.flush()
                os.fsync(output.fileno())
                _verify_stream(output, limits, identity=identity, stage=backup_root)
                # A hard link publishes a verified file without replacing any existing backup.
                os.link(output.name, backup_root / name)
            with _directory(_backup_root(root)) as directory:
                if not os.path.samestat(os.fstat(directory), backup_root.stat()):
                    raise BackupError('Каталог резервных копий был подменён.')
                os.fsync(directory)
            if keep is not None:
                _rotate(backup_root, backup_root / name, keep)
            return _backup_root(root) / name
    except (OSError, EOFError, sqlite3.Error, tarfile.TarError, UpdateError) as error:
        raise BackupError('Резервная копия не создана: ' + str(error)) from None


class _BoundedTar:
    def __init__(self, stream, limit):
        self.stream = stream
        self.limit = limit

    def tell(self):
        return self.stream.tell()

    def seek(self, offset, whence=0):
        if whence != 0 or not 0 <= offset <= self.limit:
            raise BackupError('Данные tar превысили лимит размера.')
        return self.stream.seek(offset)

    def read(self, size):
        if size < 0 or self.tell() + size > self.limit:
            raise BackupError('Данные tar превысили лимит размера.')
        return self.stream.read(size)


@contextlib.contextmanager
def _backup_tar(stream, limits, identity, stage=None):
    if os.fstat(stream.fileno()).st_size > limits.archive_size:
        raise BackupError('Архив превысил лимит размера.')
    stream.seek(0)
    try:
        with contextlib.ExitStack() as stack:
            encrypted = stream.read(21) == b'age-encryption.org/v1'
            stream.seek(0)
            if encrypted:
                if identity is None:
                    raise BackupError('Для зашифрованного архива нужен age-ключ.')
                plain = stack.enter_context(tempfile.TemporaryFile(dir=stage))
                _age(stream, plain, '--identity', identity)
                plain.seek(0)
            else:
                plain = stream
            gz = stack.enter_context(gzip.GzipFile(fileobj=plain))
            bounded = _BoundedTar(gz, limits.total_size + 32 * 1024 * 1024 + limits.files * 4096)
            with tarfile.open(fileobj=bounded, mode='r:') as archive:
                yield archive
            while bounded.tell() < bounded.limit and bounded.read(min(65536, bounded.limit - bounded.tell())):
                pass
            if bounded.tell() == bounded.limit and gz.read(1):
                raise BackupError('Данные tar превысили лимит размера.')
    except (OSError, EOFError, tarfile.TarError, ValueError) as error:
        raise BackupError('Резервная копия повреждена или имеет неизвестный формат.') from error


def _read_manifest(tar, limits):
    seen, total, manifest_data = set(), 0, None
    file_members = {}
    try:
        for member in tar:
            if len(seen) >= limits.files + 1:
                raise BackupError('В резервной копии слишком много файлов.')
            safe_name(member.name)
            if member.name in seen or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.sparse:
                raise BackupError('В резервной копии есть повтор или небезопасный объект.')
            seen.add(member.name)
            if member.name == MANIFEST:
                if not 0 <= member.size <= 16 * 1024 * 1024:
                    raise BackupError('Манифест резервной копии слишком велик.')
                with tar.extractfile(member) as stream:
                    manifest_data = stream.read()
                continue
            if not 0 <= member.size <= limits.file_size:
                raise BackupError('Некорректный размер файла в резервной копии.')
            total += member.size
            if total > limits.total_size:
                raise BackupError('Резервная копия превышает допустимый размер.')
            file_members[member.name] = member
        if manifest_data is None:
            raise BackupError('В резервной копии отсутствует манифест.')
        manifest = json.loads(manifest_data)
    except BackupError:
        raise
    except (OSError, EOFError, tarfile.TarError, json.JSONDecodeError, UnicodeDecodeError):
        raise BackupError('Резервная копия повреждена или имеет неизвестный формат.') from None
    if not isinstance(manifest, dict) or manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
        raise BackupError('Неизвестный формат манифеста резервной копии.')
    if set(manifest['files']) != set(file_members):
        raise BackupError('Состав файлов не совпадает с манифестом.')
    for name, member in file_members.items():
        spec = manifest['files'][name]
        if (not isinstance(spec, dict) or type(spec.get('size')) is not int or spec['size'] != member.size
                or type(spec.get('mode')) is not int or not 0 <= spec['mode'] <= 0o777
                or not isinstance(spec.get('sha256'), str) or len(spec['sha256']) != 64):
            raise BackupError('Некорректная запись манифеста: ' + name)
    return manifest, file_members


def _verify_stream(stream, limits, *, identity=None, stage=None):
    before = _stamp(os.fstat(stream.fileno()))
    with _backup_tar(stream, limits, identity, stage) as tar:
        manifest, members = _read_manifest(tar, limits)
        for name, member in members.items():
            with tar.extractfile(member) as content:
                if hashlib.file_digest(content, 'sha256').hexdigest() != manifest['files'][name]['sha256']:
                    raise BackupError('Контрольная сумма не совпала: ' + name)
    if _stamp(os.fstat(stream.fileno())) != before:
        raise BackupError('Архив изменился во время проверки.')
    return {'created': manifest.get('created'), 'files': len(members),
            'bytes': sum(member.size for member in members.values())}


def verify_backup(archive, *, limits=DEFAULT_LIMITS, identity=None):
    try:
        archive = Path(archive)
        with _directory(archive.parent) as parent, _regular_at(parent, archive.name) as (stream, _):
            return _verify_stream(stream, limits, identity=identity)
    except OSError as error:
        raise BackupError('Не удалось открыть архив: ' + str(error)) from None


def _rename_noreplace(parent, staging_name, destination_name):
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    if rename(parent, os.fsencode(staging_name), parent, os.fsencode(destination_name), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise BackupError('Восстановление возможно только в новый, несуществующий каталог.')
        raise OSError(code, os.strerror(code))


def restore_to_new_directory(archive, destination, *, limits=DEFAULT_LIMITS, identity=None):
    archive, destination = Path(archive), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise BackupError('Восстановление возможно только в новый, несуществующий каталог.')
    try:
        with _directory(archive.parent) as source_dir, _regular_at(source_dir, archive.name) as (stream, info), \
                _directory(destination.parent, create=True) as parent:
            parent_path = Path(f'/proc/self/fd/{parent}')
            stage = Path(tempfile.mkdtemp(prefix='.nook-restore-', dir=parent_path))
            try:
                with _backup_tar(stream, limits, identity, stage=parent_path) as tar:
                    manifest, members = _read_manifest(tar, limits)
                    if shutil.disk_usage(parent_path).free < sum(m.size for m in members.values()) + 16 * 1024 * 1024:
                        raise BackupError('Недостаточно места для восстановления в новый каталог.')
                    for name, member in members.items():
                        target = stage / safe_name(name)
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with target.open('xb') as out, tar.extractfile(member) as data:
                            reader = _HashingReader(data)
                            shutil.copyfileobj(reader, out, 65536)
                            spec = manifest['files'][name]
                            if reader.hash.hexdigest() != spec['sha256']:
                                raise BackupError('Контрольная сумма не совпала при восстановлении: ' + name)
                            os.fchmod(out.fileno(), _private_mode(name, spec['mode']))
                if _stamp(os.fstat(stream.fileno())) != _stamp(info):
                    raise BackupError('Архив изменился во время восстановления.')
                _rename_noreplace(parent, stage.name, destination.name)
                os.fsync(parent)
                return destination
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
    except OSError as error:
        raise BackupError('Не удалось восстановить архив: ' + str(error)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create', help='создать проверенную копию остановленного инстанса')
    create.add_argument('--offline', action='store_true', help='явно выбрать offline (режим по умолчанию)')
    create.add_argument('--include-providers', action='store_true',
                        help='явно включить известные внешние учётные данные и сессии')
    create.add_argument('--keep', type=int, help='оставить столько архивов этого формата')
    create.add_argument('--recipient', help='публичный ключ age')
    verify = sub.add_parser('verify', help='проверить манифест и контрольные суммы')
    verify.add_argument('archive', type=Path)
    restore = sub.add_parser('restore', help='восстановить в новый каталог, не поверх работающей установки')
    restore.add_argument('archive', type=Path)
    restore.add_argument('--to', required=True, type=Path)
    mib = 1024 * 1024
    for command in (create, verify, restore):
        command.add_argument('--identity', type=Path, help='локальный приватный ключ age')
        command.add_argument('--max-files', type=int, default=DEFAULT_LIMITS.files)
        command.add_argument('--max-file-mib', type=int, default=DEFAULT_LIMITS.file_size // mib)
        command.add_argument('--max-total-mib', type=int, default=DEFAULT_LIMITS.total_size // mib)
        command.add_argument('--max-archive-mib', type=int, default=DEFAULT_LIMITS.archive_size // mib)
    args = parser.parse_args(argv)
    if min(args.max_files, args.max_file_mib, args.max_total_mib, args.max_archive_mib) < 1:
        parser.error('Лимиты должны быть положительными.')
    limits = BackupLimits(args.max_files, args.max_file_mib * mib,
                          args.max_total_mib * mib, args.max_archive_mib * mib)
    home = Path(__file__).resolve().parent
    try:
        if args.action == 'create':
            path = create_backup(home, include_providers=args.include_providers, limits=limits,
                                 keep=args.keep, recipient=args.recipient, identity=args.identity)
            print(f"Создано и проверено: {path}")
        elif args.action == 'verify':
            print(json.dumps(verify_backup(args.archive, limits=limits, identity=args.identity), ensure_ascii=False))
        else:
            path = restore_to_new_directory(args.archive, args.to, limits=limits, identity=args.identity)
            print('Проверенная копия восстановлена в новый каталог: ' + str(path))
    except BackupError as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
