"""Private, integrity-checked Nook instance backups and new-directory restores."""
import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
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
PROVIDER_HOME_FILES = (
    '.codex/auth.json',
    '.codex/config.toml',
    '.gemini/antigravity-cli/antigravity-oauth-token',
)


class BackupError(Exception):
    pass


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
                                              and parts[2] == 'staged'):
        return True
    return False


def _files(home):
    root = Path(home).resolve()
    result = []
    total = 0

    def add_file(path, rel):
        nonlocal total
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise BackupError('Резервирование остановлено: ссылка или специальный файл `'
                              + rel.as_posix() + '`.')
        if info.st_size > MAX_FILE_SIZE:
            raise BackupError('Слишком большой файл для резервирования: ' + rel.as_posix())
        total += info.st_size
        if total > MAX_TOTAL_SIZE or len(result) >= MAX_FILES:
            raise BackupError('Инстанс превышает лимит размера резервной копии.')
        result.append((path, rel.as_posix(), info))

    for parent, dirs, names in os.walk(root, followlinks=False):
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

    provider_home = Path.home().resolve()

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

    sessions = provider_path('.codex/sessions')
    if sessions.exists():
        if not sessions.is_dir():
            raise BackupError('Каталог сессий Codex не является обычной папкой.')
        for parent, dirs, names in os.walk(sessions, followlinks=False):
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
    return root, result, total


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
    spool = root / 'spool'
    if spool.is_symlink():
        raise BackupError('Каталог spool не может быть символической ссылкой.')
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_root = _backup_root(root)
    if backup_root.is_symlink():
        raise BackupError('Каталог резервных копий не может быть символической ссылкой.')
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_root.chmod(0o700)
    lock_path = backup_root / '.lock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        with os.fdopen(fd, 'a+b') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield backup_root
    finally:
        pass


def create_backup(home):
    root = Path(home).resolve()
    with _lock(root) as backup_root:
        root, files, total = _files(root)
        if not files:
            raise BackupError('В каталоге нет файлов для резервирования.')
        if shutil.disk_usage(backup_root).free < total + 16 * 1024 * 1024:
            raise BackupError('Недостаточно места для безопасной резервной копии.')
        target = backup_root / ('nook-backup-' + time.strftime('%Y%m%d-%H%M%S') + '-'
                                + uuid.uuid4().hex[:8] + '.tar.gz')
        fd, temp_name = tempfile.mkstemp(prefix='.backup-', suffix='.tmp', dir=backup_root)
        os.close(fd)
        manifest_files = {}
        try:
            with tarfile.open(temp_name, 'w:gz', format=tarfile.PAX_FORMAT) as archive:
                for path, rel, before in files:
                    with path.open('rb') as stream:
                        current = os.fstat(stream.fileno())
                        if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                                before.st_ino, before.st_size, before.st_mtime_ns):
                            raise BackupError('Файл изменился во время резервирования: ' + rel)
                        reader = _HashingReader(stream)
                        info = tarfile.TarInfo(rel)
                        info.size = before.st_size
                        info.mode = stat.S_IMODE(before.st_mode)
                        info.mtime = before.st_mtime
                        archive.addfile(info, reader)
                        after = os.fstat(stream.fileno())
                    if (after.st_ino, after.st_size, after.st_mtime_ns) != (
                            before.st_ino, before.st_size, before.st_mtime_ns):
                        raise BackupError('Файл изменился во время резервирования: ' + rel)
                    manifest_files[rel] = {
                        'sha256': reader.hash.hexdigest(),
                        'size': before.st_size,
                        'mode': stat.S_IMODE(before.st_mode),
                    }
                payload = json.dumps({
                    'format': 1,
                    'created': int(time.time()),
                    'files': manifest_files,
                }, sort_keys=True, separators=(',', ':')).encode()
                info = tarfile.TarInfo(MANIFEST)
                info.size = len(payload)
                info.mode = 0o600
                info.mtime = int(time.time())
                archive.addfile(info, io.BytesIO(payload))
            os.chmod(temp_name, 0o600)
            verify_backup(temp_name)
            os.replace(temp_name, target)
            return target
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _read_manifest(archive):
    archive = Path(archive)
    if (archive.is_symlink() or not archive.is_file()
            or archive.stat().st_size > MAX_ARCHIVE_SIZE):
        raise BackupError('Архив не является обычным локальным файлом.')
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            members = []
            seen, total, manifest_data = set(), 0, None
            file_members = {}
            for member in tar:
                if len(members) >= MAX_FILES + 1:
                    raise BackupError('В резервной копии слишком много файлов.')
                members.append(member)
                safe_name(member.name)
                if member.name in seen or not member.isfile():
                    raise BackupError('В резервной копии есть повтор или небезопасный объект.')
                seen.add(member.name)
                if member.name == MANIFEST:
                    if member.size > 16 * 1024 * 1024:
                        raise BackupError('Манифест резервной копии слишком велик.')
                    stream = tar.extractfile(member)
                    manifest_data = stream.read(16 * 1024 * 1024 + 1)
                    if len(manifest_data) > 16 * 1024 * 1024:
                        raise BackupError('Манифест резервной копии слишком велик.')
                    continue
                if member.size < 0 or member.size > MAX_FILE_SIZE:
                    raise BackupError('Некорректный размер файла в резервной копии.')
                total += member.size
                if total > MAX_TOTAL_SIZE:
                    raise BackupError('Резервная копия превышает допустимый размер.')
                file_members[member.name] = member
            if manifest_data is None:
                raise BackupError('В резервной копии отсутствует манифест.')
            manifest = json.loads(manifest_data)
    except BackupError:
        raise
    except (OSError, EOFError, tarfile.TarError, json.JSONDecodeError, UnicodeDecodeError):
        raise BackupError('Резервная копия повреждена или имеет неизвестный формат.') from None
    if manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
        raise BackupError('Неизвестный формат манифеста резервной копии.')
    if set(manifest['files']) != set(file_members):
        raise BackupError('Состав файлов не совпадает с манифестом.')
    return manifest, file_members


def verify_backup(archive):
    archive = Path(archive)
    manifest, members = _read_manifest(archive)
    with tarfile.open(archive, 'r:gz') as tar:
        for name, member in members.items():
            spec = manifest['files'].get(name)
            if (not isinstance(spec, dict)
                    or type(spec.get('size')) is not int
                    or spec['size'] != member.size
                    or type(spec.get('mode')) is not int
                    or spec['mode'] < 0 or spec['mode'] > 0o777
                    or not isinstance(spec.get('sha256'), str)
                    or len(spec['sha256']) != 64):
                raise BackupError('Некорректная запись манифеста: ' + name)
            stream = tar.extractfile(member)
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != spec['sha256']:
                raise BackupError('Контрольная сумма не совпала: ' + name)
    return {'created': manifest.get('created'), 'files': len(members),
            'bytes': sum(member.size for member in members.values())}


def restore_to_new_directory(archive, destination):
    archive, destination = Path(archive), Path(destination)
    report = verify_backup(archive)
    if destination.exists() or destination.is_symlink():
        raise BackupError('Восстановление возможно только в новый, несуществующий каталог.')
    existing_parent = destination.parent
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    if shutil.disk_usage(existing_parent).free < report['bytes'] + 16 * 1024 * 1024:
        raise BackupError('Недостаточно места для восстановления в новый каталог.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.resolve()
    destination = parent / destination.name
    stage = parent / ('.' + destination.name + '.restore-' + uuid.uuid4().hex[:10])
    stage.mkdir(mode=0o700)
    try:
        manifest, members = _read_manifest(archive)
        with tarfile.open(archive, 'r:gz') as tar:
            for name, member in members.items():
                rel = safe_name(name)
                path = stage
                for part in rel.parts[:-1]:
                    path = path / part
                    path.mkdir(mode=0o700, exist_ok=True)
                    if path.is_symlink() or not path.is_dir():
                        raise BackupError('Небезопасный путь восстановления: ' + name)
                target = path / rel.parts[-1]
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(target, flags, 0o600)
                digest = hashlib.sha256()
                with os.fdopen(fd, 'wb') as out, tar.extractfile(member) as source:
                    while True:
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        digest.update(chunk)
                        out.write(chunk)
                spec = manifest['files'][name]
                if digest.hexdigest() != spec['sha256']:
                    raise BackupError('Контрольная сумма не совпала при восстановлении: ' + name)
                os.chmod(target, spec['mode'] & 0o777)
        if destination.exists() or destination.is_symlink():
            raise BackupError('Каталог назначения появился во время восстановления.')
        os.replace(stage, destination)
        return destination
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('create', help='создать проверенную локальную копию')
    verify = sub.add_parser('verify', help='проверить манифест и контрольные суммы')
    verify.add_argument('archive', type=Path)
    restore = sub.add_parser('restore', help='восстановить в новый каталог, не поверх работающей установки')
    restore.add_argument('archive', type=Path)
    restore.add_argument('--to', required=True, type=Path)
    args = parser.parse_args(argv)
    home = Path(__file__).resolve().parent
    try:
        if args.action == 'create':
            path = create_backup(home)
            result = verify_backup(path)
            print(f"Создано и проверено: {path} ({result['files']} файлов)")
        elif args.action == 'verify':
            print(json.dumps(verify_backup(args.archive), ensure_ascii=False))
        else:
            path = restore_to_new_directory(args.archive, args.to)
            print('Проверенная копия восстановлена в новый каталог: ' + str(path))
    except BackupError as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
