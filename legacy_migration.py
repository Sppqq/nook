"""Fail-closed source planner for the two published pre-channel Nook installs."""
from dataclasses import dataclass
import ctypes
import errno
import hashlib
import json
import os
import argparse
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

import nook_update


LEGACY_COMMITS = {
    '0.1.0': '4a4effab305745d2e569ca28885b8d58ccf549d6',
    '0.1.1': '5f4a9bfb1811160c763dc7380a48f7dbd50835b3',
}
MAX_SOURCE_FILES = 2500
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_TREE_ENTRIES = 50000
MAX_TREE_BYTES = 2 * 1024 * 1024 * 1024


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class InstallProfile:
    version: str
    commit: str
    updater: str
    managed_files: int
    dependency_change_required: bool


def _read_json(path):
    if path.is_symlink() or not path.is_file():
        raise MigrationError('Отсутствует обычный файл MANIFEST.json.')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MigrationError('MANIFEST.json повреждён или не читается.') from None
    if not isinstance(value, dict):
        raise MigrationError('Некорректная структура MANIFEST.json.')
    return value


def _safe_file(root, name):
    pure = PurePosixPath(name) if isinstance(name, str) else None
    if (pure is None or not pure.parts or pure.is_absolute() or '..' in pure.parts
            or str(pure) != name or '\\' in name):
        raise MigrationError('Небезопасный путь в манифесте.')
    path = root
    for part in pure.parts:
        path = path / part
        if path.is_symlink():
            raise MigrationError('Символическая ссылка в управляемом исходнике: ' + name)
    if not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
        raise MigrationError('Файл исходника отсутствует или превышает лимит: ' + name)
    return path


def _source_manifest(root, *, legacy_version=None):
    manifest = _read_json(root / 'MANIFEST.json')
    version = manifest.get('version')
    hashes = manifest.get('sha256')
    if (manifest.get('format') != 1 or not isinstance(version, str)
            or not isinstance(hashes, dict) or not hashes or len(hashes) > MAX_SOURCE_FILES):
        raise MigrationError('Неподдерживаемый манифест исходников.')
    if legacy_version is None:
        if manifest.get('update_api') != 1:
            raise MigrationError('Целевая версия использует неподдерживаемый update_api.')
    elif version != legacy_version:
        raise MigrationError('Снимок исходной версии не соответствует определённой версии инстанса.')
    expected = set(hashes) | {'MANIFEST.json'}
    actual = {item.relative_to(root).as_posix() for item in root.rglob('*') if item.is_file()}
    if actual != expected:
        raise MigrationError('Фактический состав исходника не совпадает с MANIFEST.json.')
    for name, wanted in hashes.items():
        try:
            nook_update.safe_rel(name)
            _safe_file(root, name)
        except (nook_update.UpdateError, MigrationError):
            raise MigrationError('Недопустимый путь MANIFEST.json: ' + str(name)) from None
        if not isinstance(wanted, str) or not re.fullmatch(r'[a-f0-9]{64}', wanted):
            raise MigrationError('Недопустимая запись MANIFEST.json: ' + str(name))
        data = _safe_file(root, name).read_bytes()
        if hashlib.sha256(data).hexdigest() != wanted:
            raise MigrationError('Не совпал хеш исходника: ' + name)
    return manifest


def identify_install(home):
    """Recognize exact legacy major versions without changing the installation."""
    home = Path(home)
    manifest = _read_json(home / 'MANIFEST.json')
    version = manifest.get('version')
    commit = LEGACY_COMMITS.get(version)
    if not commit:
        raise MigrationError('Поддерживаются только точные версии Nook 0.1.0 и 0.1.1.')
    hashes = manifest.get('sha256')
    if manifest.get('format') != 1 or not isinstance(hashes, dict):
        raise MigrationError('Формат исходного MANIFEST.json не поддерживается.')
    for name, wanted in hashes.items():
        if not isinstance(wanted, str) or not re.fullmatch(r'[a-f0-9]{64}', wanted):
            raise MigrationError('Повреждённая или небезопасная запись MANIFEST.json.')
        try:
            nook_update.safe_rel(name)
        except nook_update.UpdateError:
            raise MigrationError('Небезопасный путь MANIFEST.json: ' + name) from None
        installed = home / name
        if installed.is_symlink():
            raise MigrationError('Ссылка вместо исходного файла: ' + name)
    updater = 'telegram' if version == '0.1.1' else 'one-shot'
    if (home / 'mods/self_update.py').is_file() != (version == '0.1.1'):
        raise MigrationError('Содержимое инстанса не соответствует опубликованному профилю ' + version + '.')
    return InstallProfile(version, commit, updater, len(hashes), False)


def fetch_legacy_source(profile, destination):
    """Download only the pinned historical commit after resolving its public tag."""
    if not isinstance(profile, InstallProfile) or LEGACY_COMMITS.get(profile.version) != profile.commit:
        raise MigrationError('Неизвестный профиль исходной версии.')
    try:
        resolved = nook_update.release_commit('v' + profile.version)
        if resolved != profile.commit:
            raise MigrationError('Исторический Git-тег больше не указывает на закреплённый commit.')
        archive = Path(destination).with_suffix('.tar.gz')
        nook_update.download('https://codeload.github.com/Sppqq/nook/tar.gz/' + profile.commit,
                             archive, 50 * 1024 * 1024)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        nook_update.unpack(archive, destination)
        manifest = _source_manifest(destination, legacy_version=profile.version)
        if len(manifest['sha256']) != profile.managed_files:
            raise MigrationError('Исторический исходник не совпал с установленным MANIFEST.json.')
        return destination
    except MigrationError:
        raise
    except Exception:
        raise MigrationError('Не удалось загрузить и проверить закреплённый исходник версии.') from None
    finally:
        if 'archive' in locals():
            archive.unlink(missing_ok=True)


def fetch_target_release(release, destination):
    """Fetch a target only through the updater's Sigstore-verified release path."""
    destination = Path(destination)
    try:
        destination.mkdir(parents=True, exist_ok=False)
        source, revision = nook_update.fetch_release_source(release, destination)
        manifest = _source_manifest(source)
        if manifest['version'] != release.get('version'):
            raise MigrationError('Подписанный источник не совпадает с выбранной версией.')
        provenance = destination / 'MIGRATION_SOURCE.json'
        provenance.write_text(json.dumps({
            'schema': 1, 'version': manifest['version'], 'revision': revision,
            'verified_by': 'nook_update.fetch_release_source',
        }, sort_keys=True) + '\n', encoding='utf-8')
        os.chmod(provenance, 0o600)
        return source, revision
    except MigrationError:
        raise
    except Exception:
        raise MigrationError('Не удалось подтвердить происхождение целевой версии.') from None


def plan_source_migration(home, old_source, target_source, *, old_revision):
    """Plan a conflict-safe source merge; never writes to the live instance."""
    home, old_source, target_source = map(Path, (home, old_source, target_source))
    profile = identify_install(home)
    if old_revision != profile.commit:
        raise MigrationError('Исторический источник не закреплён на ожидаемом commit.')
    old_manifest = _source_manifest(old_source, legacy_version=profile.version)
    target_manifest = _source_manifest(target_source)
    installed_manifest = _read_json(home / 'MANIFEST.json')
    if installed_manifest.get('sha256') != old_manifest['sha256']:
        raise MigrationError('Манифест установленной версии не совпадает с историческим источником.')

    old_names = {name for name in old_manifest['sha256'] if nook_update.managed(name)}
    new_names = {name for name in target_manifest['sha256'] if nook_update.managed(name)}
    changes, preserve, conflicts = {}, [], []
    for name in sorted(old_names | new_names | {'MANIFEST.json'}):
        old_path = old_source / name
        new_path = target_source / name
        live = home / name
        old_exists = name == 'MANIFEST.json' or name in old_names
        new_exists = name == 'MANIFEST.json' or name in new_names
        old_data = nook_update.rendered(old_path.read_bytes(), home) if old_exists else None
        new_data = nook_update.rendered(new_path.read_bytes(), home) if new_exists else None
        current = live.read_bytes() if live.is_file() and not live.is_symlink() else None
        if current == new_data and new_exists:
            continue
        if old_exists and current == old_data:
            changes[name] = {
                'before': current,
                'after': new_data,
                'mode': (new_path.stat().st_mode & 0o777) if new_exists else None,
            }
            continue
        if not old_exists and current is None:
            changes[name] = {
                'before': None,
                'after': new_data,
                'mode': (new_path.stat().st_mode & 0o777) if new_exists else None,
            }
            continue
        # Preserve a local edit only when upstream leaves that exact file alone.
        if current is not None and new_data == old_data:
            preserve.append(name)
            continue
        if not new_exists and current is None:
            continue
        conflicts.append(name)
    if conflicts:
        raise MigrationError('Сохранены локальные файлы; миграция остановлена из-за конфликтов: '
                             + ', '.join(conflicts[:20]))
    dependencies = any(
        (old_source / name).read_bytes() != (target_source / name).read_bytes()
        for name in ('requirements.txt', 'requirements-voice.txt')
        if (old_source / name).is_file() and (target_source / name).is_file())
    return {
        'schema': 1,
        'source_version': profile.version,
        'source_commit': profile.commit,
        'updater': profile.updater,
        'target_version': target_manifest['version'],
        'changes': changes,
        'preserve_local': preserve,
        'dependency_change_required': dependencies,
        'runtime_paths_untouched': True,
    }


def _inventory(root):
    result = {}
    total = 0
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False):
        parent = Path(base)
        dirs.sort()
        names.sort()
        for name in list(dirs):
            path = parent / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[rel] = {'kind': 'link', 'target': os.readlink(path)}
                dirs.remove(name)
            else:
                info = path.stat(follow_symlinks=False)
                result[rel] = {'kind': 'dir', 'mode': stat.S_IMODE(info.st_mode)}
            if len(result) > MAX_TREE_ENTRIES:
                raise MigrationError('Каталог установки превышает лимит полной резервной копии.')
        for name in names:
            path = parent / name
            rel = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[rel] = {'kind': 'link', 'target': os.readlink(path)}
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if len(result) >= MAX_TREE_ENTRIES or total > MAX_TREE_BYTES:
                    raise MigrationError('Каталог установки превышает лимит полной резервной копии.')
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                result[rel] = {
                    'kind': 'file', 'size': info.st_size,
                    'sha256': digest.hexdigest(),
                    'mode': stat.S_IMODE(info.st_mode),
                }
            else:
                raise MigrationError('Специальный файл не поддерживается резервированием: ' + rel)
    return result


def create_snapshot(home, destination, *, min_free_bytes=0):
    """Copy and verify the complete installation tree; no live write occurs."""
    home, destination = Path(home), Path(destination)
    if home.is_symlink() or not home.is_dir() or destination.exists() or destination.is_symlink():
        raise MigrationError('Небезопасный каталог установки или резервной копии.')
    if home.stat().st_dev != destination.parent.stat().st_dev:
        raise MigrationError('Резервная копия должна находиться на той же файловой системе.')
    before = _inventory(home)
    tree_bytes = sum(item.get('size', 0) for item in before.values())
    required = tree_bytes * 2 + min_free_bytes
    if shutil.disk_usage(destination.parent).free < required:
        raise MigrationError('Недостаточно свободного места для резервной копии и staging-копии.')
    try:
        shutil.copytree(home, destination, symlinks=True, copy_function=shutil.copy2)
        after = _inventory(destination)
        if before != after:
            raise MigrationError('Полная резервная копия не совпала с исходным инстансом.')
        return {'root': str(destination), 'entries': len(after), 'bytes': tree_bytes,
                'sha256': _inventory_digest(after)}
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def stage_source_tree(home, target_source, plan, stage):
    """Prepare a complete side-by-side installation; leaves live files untouched."""
    home, target_source, stage = Path(home), Path(target_source), Path(stage)
    if stage.exists() or stage.is_symlink() or stage.parent.resolve() != home.parent.resolve():
        raise MigrationError('Staging-каталог должен быть новым соседом инстанса.')
    if plan.get('source_commit') not in LEGACY_COMMITS.values():
        raise MigrationError('План миграции не закреплён на поддерживаемом исходнике.')
    if plan.get('target_version') != _source_manifest(target_source)['version']:
        raise MigrationError('Целевой источник изменился после построения плана.')
    for name, item in plan['changes'].items():
        live = home / name
        current = live.read_bytes() if live.is_file() and not live.is_symlink() else None
        if current != item['before']:
            raise MigrationError('Файл изменился после проверки плана: ' + name)
    try:
        shutil.copytree(home, stage, symlinks=True, copy_function=shutil.copy2)
        for name, item in plan['changes'].items():
            target = _safe_target(stage, name)
            after = item['after']
            if after is None:
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name('.' + target.name + '.migration-tmp')
            temporary.write_bytes(after)
            os.chmod(temporary, item['mode'])
            os.replace(temporary, target)
        marker = _safe_target(stage, 'spool/migrations/legacy-source-plan.json')
        if marker.exists():
            raise MigrationError('В staging уже существует файл плана миграции.')
        metadata = marker.parent
        metadata.mkdir(parents=True, exist_ok=True)
        record = {
            'schema': 1, 'id': uuid.uuid4().hex,
            'source_version': plan['source_version'], 'source_commit': plan['source_commit'],
            'target_version': plan['target_version'], 'state': 'staged',
            'dependency_change_required': bool(plan['dependency_change_required']),
        }
        marker.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.chmod(marker, 0o600)
        return record
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def build_staged_environment(home, stage, target_source, *, python=None):
    """Replace only the staged venv; preserve directly installed extension packages."""
    home, stage, target_source = map(Path, (home, stage, target_source))
    interpreter = python or sys.executable
    try:
        version_result = subprocess.run([interpreter, '-c',
            'import sys; print("%d.%d" % sys.version_info[:2])'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        if version_result.returncode or tuple(map(int, version_result.stdout.strip().split('.'))) < (3, 11):
            raise MigrationError('Целевой Nook требует доступный Python 3.11 или новее.')
        target_manifest = _source_manifest(target_source)
        del target_manifest
        requirements = target_source / 'requirements.txt'
        if not requirements.is_file() or requirements.is_symlink():
            raise MigrationError('В целевом исходнике нет обычного requirements.txt.')
        old_requirements = home / 'requirements.txt'
        old_names = _requirement_names(old_requirements)
        target_names = _requirement_names(requirements)
        voice_old = _requirement_names(home / 'requirements-voice.txt')
        voice_new = target_source / 'requirements-voice.txt'
        installed = _requested_packages(home / '.venv')
        installed_names = {_normal_name(item.split('==', 1)[0]) for item in installed}
        enable_voice = bool(voice_old & installed_names)
        preserved = sorted(item for item in installed
                           if _normal_name(item.split('==', 1)[0]) not in old_names | target_names)
        voice_names = _requirement_names(voice_new) if enable_voice and voice_new.is_file() else set()
        preserved = [item for item in preserved if _normal_name(item.split('==', 1)[0]) not in voice_names]

        venv = stage / '.venv'
        if venv.is_symlink() or (venv.exists() and not venv.is_dir()):
            raise MigrationError('Staged .venv не является обычным каталогом.')
        if venv.exists():
            shutil.rmtree(venv)
        p = subprocess.run([interpreter, '-m', 'venv', str(venv)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        if p.returncode:
            raise MigrationError('Не удалось подготовить отдельное виртуальное окружение; текущий инстанс не изменён.')
        new_python = venv / 'bin/python'
        commands = [[str(new_python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                     '-r', str(requirements)]]
        if enable_voice:
            if not voice_new.is_file() or voice_new.is_symlink():
                raise MigrationError('Голосовой модуль установлен, но целевая voice-зависимость отсутствует.')
            commands.append([str(new_python), '-m', 'pip', 'install', '--disable-pip-version-check',
                             '--no-input', '-r', str(voice_new)])
        if preserved:
            commands.append([str(new_python), '-m', 'pip', 'install', '--disable-pip-version-check',
                             '--no-input', *preserved])
        for command in commands:
            p = subprocess.run(command, cwd=stage, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
            if p.returncode:
                raise MigrationError('Не удалось установить закреплённые зависимости в отдельную среду; '
                                     'живой инстанс не изменён.')
        installed_pins = _requirement_pins_from_freeze(new_python)
        wanted_pins = _requirement_pins(requirements)
        if enable_voice:
            wanted_pins.update(_requirement_pins(voice_new))
        if any(installed_pins.get(name) != version for name, version in wanted_pins.items()):
            raise MigrationError('Пользовательские пакеты конфликтуют с закреплёнными зависимостями новой версии.')
        p = subprocess.run([str(new_python), '-m', 'pip', 'check'], cwd=stage,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        if p.returncode:
            raise MigrationError('Новая среда не прошла pip check; живой инстанс не изменён.')
        return {'python': str(new_python), 'voice_preserved': enable_voice,
                'extra_packages_preserved': len(preserved)}
    except MigrationError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        raise MigrationError('Подготовка зависимостей не удалась; действующий инстанс не изменён.') from None


def _normal_name(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _requirement_names(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return set()
    names = set()
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*==\s*[^;\s]+(?:\s*;.*)?$', line)
        if match:
            names.add(_normal_name(match.group(1)))
    return names


def _requirement_pins(path):
    path = Path(path)
    pins = {}
    if not path.is_file():
        return pins
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        match = re.match(r'^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*==\s*([A-Za-z0-9_.+-]+)(?:\s*;.*)?$', line)
        if match:
            pins[_normal_name(match.group(1))] = match.group(2)
    return pins


def _requirement_pins_from_freeze(python):
    try:
        result = subprocess.run([str(python), '-m', 'pip', 'freeze', '--all'],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise MigrationError('Не удалось проверить версии пакетов новой среды.') from None
    if result.returncode:
        raise MigrationError('Не удалось проверить версии пакетов новой среды.')
    pins = {}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', line.strip())
        if match:
            pins[_normal_name(match.group(1))] = match.group(2)
    return pins


def _requested_packages(venv):
    venv = Path(venv)
    python = venv / 'bin/python'
    if not venv.is_dir() or venv.is_symlink() or not python.is_file():
        return set()
    try:
        probe = subprocess.run([str(python), '-c',
            'import sysconfig; print(sysconfig.get_paths()["purelib"])'],
            cwd=venv.parent, capture_output=True, text=True, timeout=10)
        if probe.returncode:
            return set()
        site = Path(probe.stdout.strip())
        result = set()
        for requested in list(site.glob('*.dist-info/REQUESTED'))[:5000]:
            metadata = requested.parent / 'METADATA'
            if metadata.is_symlink() or not metadata.is_file():
                continue
            if (requested.parent / 'direct_url.json').exists():
                raise MigrationError('Обнаружен пакет из локального/VCS-источника; автоматический перенос остановлен.')
            name = version = None
            for line in metadata.read_text(encoding='utf-8', errors='replace').splitlines():
                if line.startswith('Name: ') and name is None:
                    name = line[6:].strip()
                elif line.startswith('Version: ') and version is None:
                    version = line[9:].strip()
                if name and version:
                    break
            if name and version and re.fullmatch(r'[A-Za-z0-9_.+-]+', name) and re.fullmatch(r'[A-Za-z0-9_.+-]+', version):
                result.add(name + '==' + version)
        return result
    except (OSError, subprocess.SubprocessError):
        raise MigrationError('Не удалось инвентаризировать пользовательские Python-пакеты.') from None


def _safe_target(root, name):
    path = root
    for part in nook_update.safe_rel(name).parts:
        path = path / part
        if path.is_symlink():
            raise MigrationError('Ссылка в staging-каталоге: ' + name)
    if path.exists() and not path.is_file():
        raise MigrationError('Целевой путь не является обычным файлом: ' + name)
    return path


def exchange_directories(left, right):
    """Atomically exchange sibling directories on Linux or fail before mutation."""
    left, right = Path(left), Path(right)
    if (left.is_symlink() or right.is_symlink() or not left.is_dir() or not right.is_dir()
            or left.parent.resolve() != right.parent.resolve()
            or left.stat().st_dev != right.stat().st_dev):
        raise MigrationError('Атомарное переключение требует соседние каталоги на одной файловой системе.')
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, 'renameat2', None)
    if renameat2 is None:
        raise MigrationError('ОС не предоставляет атомарную замену каталогов; живой инстанс не изменён.')
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2)  # RENAME_EXCHANGE
    if result:
        code = ctypes.get_errno()
        if code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise MigrationError('Файловая система не поддерживает атомарную замену каталогов.')
        raise OSError(code, os.strerror(code))


def _write_journal(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory_digest(inventory):
    return hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()


def recover_transaction(journal, *, stop_service, start_service):
    """Restore the pre-migration tree after interruption at any switch boundary."""
    journal = Path(journal)
    try:
        state = json.loads(journal.read_text(encoding='utf-8'))
        home, stage, backup = Path(state['home']), Path(state['stage']), Path(state['backup'])
        if state.get('schema') != 1 or home.parent.resolve() != stage.parent.resolve():
            raise ValueError
        if state['status'] in ('complete', 'rolled_back'):
            return state['status']
        if state['status'] == 'prepared':
            return 'prepared'
        if state['status'] not in ('stopping', 'switched', 'starting', 'health_check'):
            raise ValueError
        stop_service()
        # A stage directory exists on both sides of the atomic exchange. The
        # durable state says whether the exchange was recorded, but a power loss
        # can land between exchange and journal write; inspect verified tree IDs.
        live_id = state['target_tree_id']
        stage_id = state['source_tree_id']
        live_marker = home / 'spool/migrations/legacy-source-plan.json'
        stage_marker = stage / 'spool/migrations/legacy-source-plan.json'
        live_is_target = live_marker.is_file() and json.loads(live_marker.read_text()).get('id') == live_id
        stage_is_target = stage_marker.is_file() and json.loads(stage_marker.read_text()).get('id') == live_id
        if live_is_target and stage_is_target:
            raise MigrationError('Невозможно однозначно определить состояние каталогов после сбоя.')
        backup_inventory = _inventory(backup)
        if _inventory_digest(backup_inventory) != state['backup_sha256']:
            raise MigrationError('Полная резервная копия повреждена; каталоги оставлены без переключения.')
        if live_is_target:
            if _inventory(stage) != backup_inventory:
                raise MigrationError('Старый каталог отличается от полной копии; автоматический откат остановлен.')
            exchange_directories(home, stage)
        elif _inventory(home) != backup_inventory:
            raise MigrationError('Рабочий каталог отличается от полной копии; автоматический откат остановлен.')
        start_service()
        state['status'] = 'rolled_back'
        _write_journal(journal, state)
        return state['status']
    except MigrationError:
        raise
    except Exception:
        raise MigrationError('Журнал миграции повреждён; файлы не переключались автоматически.') from None


def migrate_transaction(home, old_source, target_source, *, old_revision, target_revision, backup,
                        stage, journal, stop_service, start_service, health_check,
                        python=None):
    """Build, switch, health-check, and recover a legacy install transactionally."""
    home, stage, backup, journal = map(Path, (home, stage, backup, journal))
    profile = identify_install(home)
    if not re.fullmatch(r'[a-f0-9]{40}', target_revision):
        raise MigrationError('Целевая версия не закреплена за Git commit.')
    provenance = Path(target_source).parent / 'MIGRATION_SOURCE.json'
    try:
        proof = json.loads(provenance.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise MigrationError('Целевой источник не прошёл проверку происхождения релиза.') from None
    if (provenance.is_symlink() or proof.get('schema') != 1
            or proof.get('version') != _source_manifest(target_source)['version']
            or proof.get('revision') != target_revision
            or proof.get('verified_by') != 'nook_update.fetch_release_source'):
        raise MigrationError('Целевая версия не подтверждена механизмом подписи релизов Nook.')
    if journal.exists():
        raise MigrationError('Уже существует журнал миграции; сначала выполните восстановление.')
    plan = plan_source_migration(home, old_source, target_source, old_revision=old_revision)
    source_tree_id = uuid.uuid4().hex
    snapshot = create_snapshot(home, backup, min_free_bytes=64 * 1024 * 1024)
    try:
        record = stage_source_tree(home, target_source, plan, stage)
        record['id'] = source_tree_id
        marker = stage / 'spool/migrations/legacy-source-plan.json'
        marker.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.chmod(marker, 0o600)
        build_staged_environment(home, stage, target_source, python=python)
        if (stage / nook_update.STATE).exists():
            raise MigrationError('Обнаружено состояние современной установки; автоматическая миграция остановлена.')
        installed_manifest = _source_manifest(target_source)
        installed_files = {
            name: nook_update.digest((stage / name).read_bytes())
            for name in installed_manifest['sha256'] if nook_update.managed(name)
        }
        installed_files['MANIFEST.json'] = nook_update.digest((stage / 'MANIFEST.json').read_bytes())
        nook_update.json_write(stage / nook_update.STATE,
                               {'schema': 1, 'revision': target_revision, 'files': installed_files})
        state = {
            'schema': 1, 'home': str(home), 'stage': str(stage), 'backup': str(backup),
            'source_version': profile.version, 'source_commit': profile.commit,
            'target_version': plan['target_version'], 'target_revision': target_revision,
            'source_tree_id': source_tree_id,
            'target_tree_id': source_tree_id, 'backup_sha256': snapshot['sha256'],
            'status': 'prepared',
        }
        _write_journal(journal, state)
        state['status'] = 'stopping'
        _write_journal(journal, state)
        stop_service()
        exchange_directories(home, stage)
        state['status'] = 'switched'
        _write_journal(journal, state)
        state['status'] = 'starting'
        _write_journal(journal, state)
        start_service()
        state['status'] = 'health_check'
        _write_journal(journal, state)
        if not health_check(home, plan['target_version']):
            raise MigrationError('Новая версия не прошла проверку запуска; выполняется откат.')
        state['status'] = 'complete'
        _write_journal(journal, state)
        return state
    except BaseException:
        if journal.exists():
            try:
                recover_transaction(journal, stop_service=stop_service, start_service=start_service)
            except Exception:
                # Keep the complete verified snapshot and journal for the
                # independently invokable recovery command.
                pass
        raise


def _user_service(home, *, require_active=True):
    try:
        listed = subprocess.run(['systemctl', '--user', 'list-units', '--all',
            '--type=service', '--no-legend', '--plain'], capture_output=True,
            text=True, timeout=20)
        if listed.returncode:
            raise ValueError
        candidates = [line.split()[0] for line in listed.stdout.splitlines()
                      if line.split() and line.split()[0].endswith('.service')]
        matches = []
        for unit in candidates[:500]:
            result = subprocess.run(['systemctl', '--user', 'show', unit,
                '--property=WorkingDirectory,ActiveState'], capture_output=True,
                text=True, timeout=15)
            fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            if fields.get('WorkingDirectory') == str(Path(home).resolve()):
                matches.append((unit, fields.get('ActiveState')))
        if len(matches) != 1 or require_active and matches[0][1] != 'active':
            raise ValueError
        return matches[0][0]
    except (OSError, subprocess.SubprocessError, ValueError):
        raise MigrationError('Не найдена ровно одна активная пользовательская systemd-служба этой установки.') from None


def _service_action(unit, action):
    result = subprocess.run(['systemctl', '--user', action, unit],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=45)
    if result.returncode:
        verb = 'остановить' if action == 'stop' else 'запустить'
        raise MigrationError('Не удалось ' + verb + ' исходную systemd-службу.')


def _wait_healthy(home, unit, revision, *, timeout=120, stable=20):
    health_path = nook_update.workspace(home) / 'health.json'
    deadline, good_since = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(['systemctl', '--user', 'show', unit,
                '--property=WorkingDirectory,MainPID,ActiveState'], capture_output=True,
                text=True, timeout=10)
            fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            health = json.loads(health_path.read_text(encoding='utf-8'))
            main_pid = int(fields.get('MainPID', '0'))
            healthy = (result.returncode == 0 and fields.get('ActiveState') == 'active'
                and fields.get('WorkingDirectory') == str(Path(home).resolve())
                and main_pid > 0 and health.get('pid') == main_pid
                and health.get('revision') == revision and not health.get('failed_mods')
                and time.time() - float(health.get('time', 0)) < 10)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            healthy = False
        if healthy:
            if good_since is None:
                good_since = time.monotonic()
            elif time.monotonic() - good_since >= stable:
                return True
        else:
            good_since = None
        time.sleep(2)
    return False


def _find_release(version, home):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?', version):
        raise MigrationError('Целевая версия должна быть точной SemVer без префикса v.')
    try:
        releases = nook_update.installable_releases(nook_update.list_releases())
        settings = nook_update.update_settings(home)
        if settings['channel'] is not None:
            return nook_update.select_release(releases, settings['channel'], version)
        for release in releases:
            if release['version'] == version:
                return release
    except Exception:
        raise MigrationError('Не удалось выбрать точную опубликованную версию без изменения канала.') from None
    raise MigrationError('Для этой версии нет архива с проверяемой Sigstore provenance.')


def cli(argv=None):
    parser = argparse.ArgumentParser(description='Переходный обновлятор Nook 0.1.0/0.1.1')
    parser.add_argument('action', choices=('migrate', 'recover'))
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--target-version')
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    journal = home.parent / ('.' + home.name + '.nook-migration.json')
    unit = _user_service(home, require_active=args.action != 'recover')
    stop = lambda: _service_action(unit, 'stop')
    start = lambda: _service_action(unit, 'start')
    if args.action == 'recover':
        print(recover_transaction(journal, stop_service=stop, start_service=start))
        return 0
    if not args.target_version:
        parser.error('Для миграции укажи --target-version из опубликованных релизов.')
    profile = identify_install(home)
    release = _find_release(args.target_version, home)
    ident = uuid.uuid4().hex
    work = home.parent / ('.' + home.name + '.migration-' + ident)
    stage = home.parent / ('.' + home.name + '.stage-' + ident)
    work.mkdir(mode=0o700)
    try:
        old_source = fetch_legacy_source(profile, work / 'legacy-source')
        target_source, target_revision = fetch_target_release(release, work / 'target-release')
        state = migrate_transaction(home, old_source, target_source,
            old_revision=profile.commit, target_revision=target_revision,
            backup=work / 'complete-backup', stage=stage,
            journal=journal, stop_service=stop, start_service=start,
            health_check=lambda root, _version: _wait_healthy(root, unit, target_revision))
        print(json.dumps({'status': state['status'], 'from': profile.version,
            'to': state['target_version'], 'backup': str(work / 'complete-backup')}, ensure_ascii=False))
        return 0
    except BaseException:
        if not journal.exists():
            shutil.rmtree(work, ignore_errors=True)
        raise


if __name__ == '__main__':
    raise SystemExit(cli())
