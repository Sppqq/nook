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
from urllib.parse import quote, urlparse
import urllib.request
import uuid
import zipfile

from release_channels import (CHANNELS, channel_for_version, compare_release_versions,
                              eligible_releases, installable_releases, select_release)

ROOT = Path(__file__).resolve().parent
REPO = 'Sppqq/nook'
STATE = '.nook-install.json'
UPDATE_SETTINGS = 'spool/updates/settings.json'
AUTO_MODES = {'off', 'notify', 'confirm', 'install'}
TOP = {'MANIFEST.json', 'VERSION', 'README.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'LICENSE',
       'SECURITY.md', '.gitignore', 'config.example.json', 'secrets.env.example',
       'requirements.txt', 'requirements-voice.txt', 'bootstrap.sh', 'install.sh'}
# These paths are source only. In particular: no runtime templates are seeded,
# no policy/configuration directory is updated, no service/provider setup runs.
DIRS = {'bin', 'mods', 'docs', 'tests', 'tools', 'setup'}


class UpdateError(Exception):
    pass


class UpdateBusy(UpdateError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_directory(path, mode=0o700):
    if path.is_dir():
        return
    durable_directory(path.parent)
    path.mkdir(mode=mode, exist_ok=True)
    sync_directory(path.parent)


def atomic(path, data, mode=0o600):
    durable_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix='.nook-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            os.fchmod(f.fileno(), mode)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
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
    durable_directory(base)
    return base


def update_settings(home):
    path = path_at(home, UPDATE_SETTINGS)
    if not path.exists():
        return {'schema': 1, 'channel': None, 'version_pin': None,
                'auto_mode': 'off', 'paused': False}
    settings = read_json(path)
    if (settings.get('schema') != 1
            or settings.get('channel') is not None and settings.get('channel') not in CHANNELS
            or settings.get('version_pin') is not None
            and (not isinstance(settings['version_pin'], str)
                 or _version_channel(settings['version_pin']) is None)
            or settings.get('auto_mode') not in AUTO_MODES
            or type(settings.get('paused')) is not bool):
        raise UpdateError('Некорректные настройки канала обновлений.')
    return settings


def _version_channel(version):
    try:
        return channel_for_version(version)
    except ValueError:
        return None


def save_update_settings(home, *, channel=None, version_pin=None,
                         auto_mode=None, paused=None, clear_version_pin=False):
    with lock(home):
        current = update_settings(home)
        if channel is not None:
            if channel not in CHANNELS:
                raise UpdateError('Неизвестный канал обновлений.')
            current['channel'] = channel
        if clear_version_pin:
            current['version_pin'] = None
        elif version_pin is not None:
            if _version_channel(version_pin) is None:
                raise UpdateError('Некорректная закреплённая версия.')
            current['version_pin'] = version_pin
        if auto_mode is not None:
            if auto_mode not in AUTO_MODES:
                raise UpdateError('Неизвестный режим автообновления.')
            current['auto_mode'] = auto_mode
        if paused is not None:
            if type(paused) is not bool:
                raise UpdateError('Некорректное состояние обновлений.')
            current['paused'] = paused
        path = path_at(home, UPDATE_SETTINGS)
        json_write(path, current)
        return current


@contextlib.contextmanager
def lock(home):
    base = workspace(home)
    with path_at(base, 'lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateBusy('Другое обновление уже выполняется.') from None
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


def github_json(url, limit=2 * 1024 * 1024):
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'Nook-release-updater',
    })
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError('Слишком большой ответ GitHub.')
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UpdateError('GitHub вернул некорректные данные релиза.') from None


def list_releases():
    releases = []
    for page in range(1, 21):
        items = github_json(f'https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}',
                            limit=16 * 1024 * 1024)
        if not isinstance(items, list):
            raise UpdateError('GitHub вернул некорректный список релизов.')
        releases.extend(items)
        if len(items) < 100:
            return releases
    raise UpdateError('Не удалось получить полный список релизов GitHub.')


def release_commit(tag):
    ref = github_json(
        f'https://api.github.com/repos/{REPO}/git/ref/tags/{quote(tag, safe="")}')
    obj = ref.get('object') if isinstance(ref, dict) else None
    for _ in range(5):
        if not isinstance(obj, dict) or not re.fullmatch('[a-f0-9]{40}', str(obj.get('sha', ''))):
            break
        if obj.get('type') == 'commit':
            return obj['sha']
        if obj.get('type') != 'tag':
            break
        tagged = github_json(f'https://api.github.com/repos/{REPO}/git/tags/{obj["sha"]}')
        obj = tagged.get('object') if isinstance(tagged, dict) else None
    raise UpdateError('Не удалось разрешить тег релиза в неизменяемый commit.')


def download_release_asset(release, dest, version, limit=10 * 1024 * 1024, *, name=None):
    assets = release.get('assets')
    expected_name = name or 'nook-' + version + '.zip'
    if not isinstance(assets, list):
        raise UpdateError('GitHub Release не содержит проверяемых артефактов.')
    asset = next((item for item in assets if isinstance(item, dict)
                  and item.get('name') == expected_name), None)
    if asset is None:
        raise UpdateError('В GitHub Release отсутствует архив исходников: ' + expected_name)
    digest_value = asset.get('digest')
    if not isinstance(digest_value, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest_value):
        raise UpdateError('GitHub не предоставляет SHA-256 артефакта; обновление отменено.')
    expected_url = f'https://github.com/{REPO}/releases/download/v{version}/{expected_name}'
    if asset.get('browser_download_url') != expected_url:
        raise UpdateError('Ссылка на артефакт не соответствует выбранному релизу.')
    req = urllib.request.Request(expected_url, headers={
        'Accept': 'application/octet-stream',
        'User-Agent': 'Nook-release-updater',
    })
    size = 0
    hasher = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=45) as response, Path(dest).open('wb') as out:
            final_url = urlparse(response.geturl())
            if final_url.scheme != 'https' or final_url.hostname not in {
                    'github.com', 'release-assets.githubusercontent.com',
                    'objects.githubusercontent.com'}:
                raise UpdateError('GitHub перенаправил загрузку на недоверенный адрес.')
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise UpdateError('Артефакт релиза слишком большой.')
                hasher.update(chunk)
                out.write(chunk)
    except UpdateError:
        raise
    except OSError:
        raise UpdateError('Не удалось загрузить артефакт GitHub Release.') from None
    if 'sha256:' + hasher.hexdigest() != digest_value:
        Path(dest).unlink(missing_ok=True)
        raise UpdateError('Контрольная сумма артефакта GitHub Release не совпала.')


def verify_release_provenance(archive, bundle):
    gh = shutil.which('gh')
    if gh is None:
        raise UpdateError('Для проверки подписи релиза установи актуальный GitHub CLI (gh). Вход не нужен.')
    with tempfile.TemporaryDirectory(prefix='nook-gh-') as config:
        env = dict(os.environ, GH_CONFIG_DIR=config, GH_PROMPT_DISABLED='1', GH_HOST='github.com')
        for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
            env.pop(key, None)
        try:
            subprocess.run(
                [gh, 'attestation', 'verify', str(archive), '--bundle', str(bundle),
                 '--repo', REPO, '--signer-workflow', REPO + '/.github/workflows/release.yml',
                 '--source-ref', 'refs/heads/main', '--deny-self-hosted-runners'],
                env=env, check=True, timeout=90, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            raise UpdateError('Не удалось подтвердить происхождение релиза из workflow Nook.') from None


def fetch_release_source(release, stage):
    version = release['version']
    revision = release_commit('v' + version)
    asset = stage / ('nook-' + version + '.zip')
    bundle = stage / 'provenance.jsonl'
    download_release_asset(release, asset, version)
    download_release_asset(release, bundle, version, 1024 * 1024, name=bundle.name)
    verify_release_provenance(asset, bundle)
    source = stage / 'source'
    source.mkdir()
    unpack_zip(asset, source)
    metadata = source / 'NOOK_RELEASE.json'
    try:
        recorded = read_json(metadata)
        if recorded != {'version': version, 'revision': revision}:
            raise ValueError
        if read_json(source / 'MANIFEST.json')['version'] != version:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise UpdateError('Версия или commit подписанного архива не соответствует тегу.') from None
    metadata.unlink()
    verify_source(source)
    return source, revision


def unpack_zip(archive, dest):
    total, seen = 0, set()
    try:
        with zipfile.ZipFile(archive) as source:
            items = source.infolist()
            if len(items) > 2000:
                raise UpdateError('В архиве слишком много файлов.')
            for item in items:
                parts = safe_rel(item.filename.rstrip('/')).parts
                if item.is_dir():
                    continue
                mode = item.external_attr >> 16
                if (len(parts) < 1 or item.flag_bits & 1
                        or mode and not (mode & 0o170000) == 0o100000):
                    raise UpdateError('Ссылки, специальные и зашифрованные файлы запрещены.')
                name = '/'.join(parts)
                if name in seen:
                    raise UpdateError('Повтор файла в архиве.')
                seen.add(name)
                total += item.file_size
                if item.file_size > 5 * 1024 * 1024 or total > 50 * 1024 * 1024:
                    raise UpdateError('Слишком большой архив релиза.')
                target = path_at(dest, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(target, flags, 0o600)
                with os.fdopen(fd, 'wb') as out, source.open(item) as data:
                    shutil.copyfileobj(data, out)
                target.chmod(0o755 if mode & 0o111 else 0o644)
    except UpdateError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile, RuntimeError):
        raise UpdateError('Архив GitHub Release повреждён.') from None


def prepare_stage(home, stage, revision):
    archive = stage / 'source.tar.gz'
    download('https://codeload.github.com/' + REPO + '/tar.gz/' + revision,
             archive, 10 * 1024 * 1024)
    source = stage / 'source'
    source.mkdir()
    unpack(archive, source)
    plan = plan_source(home, source, revision, stage)
    plan['version'] = read_json(source / 'MANIFEST.json')['version']
    json_write(stage / 'plan.json', plan)
    return plan


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
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    if actual != expected:
        raise UpdateError('Состав архива не совпадает с манифестом.')
    for name, wanted in manifest['sha256'].items():
        p = path_at(source, name)
        if not re.fullmatch('[a-f0-9]{64}', wanted) or digest(p.read_bytes()) != wanted:
            raise UpdateError('Не совпала контрольная сумма: ' + name)
        if p.suffix == '.py':
            ast.parse(p.read_text(), filename=name)
    return manifest


def plan_source(home, source, revision, stage, *, allow_removals=False):
    base = baseline(home)
    manifest = verify_source(source)
    names = {n for n in manifest['sha256'] if managed(n)} | {'MANIFEST.json'}
    missing = set(base['files']) - names
    if missing and not allow_removals:
        raise UpdateError('Обновление удаляет старые файлы; нужна отдельная миграция.')
    for name in ('requirements.txt', 'requirements-voice.txt'):
        if path_at(home, name).read_bytes() != (source / name).read_bytes():
            raise UpdateError('Изменились зависимости. Нужна отдельная миграция окружения; бот сохранён.')
    changes, conflicts = {}, []
    for name in sorted(names | missing):
        src = path_at(source, name)
        data = rendered(src.read_bytes(), home) if name in names else None
        wanted = digest(data) if data is not None else None
        dst = path_at(home, name)
        current = digest(dst.read_bytes()) if dst.is_file() else None
        old = base['files'].get(name)
        if name == 'VERSION' and old is None and current is not None:
            installed_version = read_json(path_at(home, 'MANIFEST.json')).get('version')
            if isinstance(installed_version, str) and digest((installed_version + '\n').encode()) == current:
                old = current
        if current == wanted and name not in missing:
            continue
        if old is not None and current != old and not (name in missing and not dst.exists()):
            # Preserve a local edit when upstream did not change that file.
            if wanted == old:
                continue
            conflicts.append(name)
            continue
        if old is None and dst.exists():
            conflicts.append(name)
            continue
        mode = src.stat().st_mode & 0o777 if data is not None else 0
        if data is not None:
            atomic(path_at(stage / 'files', name), data, mode)
        changes[name] = {'before': current, 'after': wanted,
                         'mode': mode}
    if conflicts:
        raise UpdateError('Сохранены местные правки; обновление отменено. Конфликты: ' + ', '.join(conflicts[:12]))
    plan = {'schema': 1, 'id': stage.name, 'revision': revision, 'changes': changes,
            'baseline': base, 'created': time.time()}
    json_write(stage / 'plan.json', plan)
    return plan


def prepare(home, version=None):
    settings = update_settings(home)
    if settings['paused']:
        raise UpdateError('Проверка обновлений приостановлена владельцем.')
    if settings['channel'] is not None:
        return prepare_release(home, version or settings['version_pin'])
    if version is not None:
        raise UpdateError('Сначала выбери канал обновлений для выбора версии.')
    with lock(home):
        if update_settings(home) != settings:
            raise UpdateError('Настройки обновления изменились. Повтори проверку.')
        baseline(home)  # Refuse private/unknown installs before downloading anything.
        base = workspace(home)
        if (base / 'transaction.json').exists():
            txn = read_json(base / 'transaction.json')
            if txn['status'] not in ('success', 'rolled_back'):
                raise UpdateError('Есть незавершённое обновление. Сначала проверь статус и выполни откат.')
        ident = uuid.uuid4().hex[:16]
        stage = base / 'staged' / ident
        stage.mkdir(parents=True, mode=0o700)
        try:
            download('https://api.github.com/repos/' + REPO + '/commits/main', stage / 'commit.json', 2 * 1024 * 1024)
            revision = read_json(stage / 'commit.json')['sha']
            if not re.fullmatch('[a-f0-9]{40}', revision):
                raise UpdateError('Некорректная версия GitHub.')
            return prepare_stage(home, stage, revision)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise


def prepare_release(home, version=None):
    with lock(home):
        baseline(home)
        settings = update_settings(home)
        if settings['channel'] is None:
            raise UpdateError('Сначала выбери канал обновлений.')
        if settings['paused']:
            raise UpdateError('Проверка обновлений приостановлена владельцем.')
        if version and settings['version_pin'] and version != settings['version_pin']:
            raise UpdateError('Чтобы выбрать другую версию, сначала сними закрепление.')
        version = version or settings['version_pin']
        base = workspace(home)
        if (base / 'transaction.json').exists():
            txn = read_json(base / 'transaction.json')
            if txn['status'] not in ('success', 'rolled_back'):
                raise UpdateError('Есть незавершённое обновление. Сначала проверь статус и выполни откат.')
        try:
            release = select_release(installable_releases(list_releases()), settings['channel'], version)
        except ValueError as exc:
            raise UpdateError(str(exc)) from None
        ident = uuid.uuid4().hex[:16]
        stage = base / 'staged' / ident
        stage.mkdir(parents=True, mode=0o700)
        try:
            source, revision = fetch_release_source(release, stage)
            current_version = read_json(path_at(home, 'MANIFEST.json')).get('version')
            try:
                downgrade = compare_release_versions(release['version'], current_version) < 0
            except (TypeError, ValueError):
                raise UpdateError('Не удалось безопасно сравнить текущую и выбранную версии.') from None
            plan = plan_source(home, source, revision, stage, allow_removals=downgrade)
            plan['downgrade'] = downgrade
            plan['release_settings'] = settings
            plan['current_version'] = current_version
            plan['version'] = release['version']
            plan['release_name'] = str(release.get('name') or '')[:200]
            plan['release_body'] = str(release.get('body') or '')[:12000]
            plan['channel'] = release['channel']
            json_write(stage / 'plan.json', plan)
            return plan
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise


def get_plan(home, ident):
    if not re.fullmatch('[a-f0-9]{16}', ident):
        raise UpdateError('Некорректный номер обновления.')
    base = workspace(home)
    txn_path = path_at(base, 'transaction.json')
    if txn_path.exists() and read_json(txn_path)['status'] not in {'success', 'rolled_back'}:
        raise UpdateError('Сначала восстанови незавершённое обновление.')
    stage = path_at(base, 'staged/' + ident)
    plan = read_json(path_at(stage, 'plan.json'))
    if plan['id'] != ident or time.time() - plan['created'] > 3600:
        raise UpdateError('Проверка устарела. Сначала проверь обновления ещё раз.')
    current_settings = update_settings(home)
    if current_settings['paused']:
        raise UpdateError('Проверка обновлений приостановлена владельцем.')
    if plan.get('channel') is None:
        if current_settings['channel'] is not None:
            raise UpdateError('Канал изменился после проверки. Проверь обновление заново.')
    else:
        if current_settings['channel'] != (plan.get('release_settings') or {}).get('channel'):
            raise UpdateError('Выбранный канал изменился после проверки. Проверь обновление заново.')
        if current_settings != plan['release_settings']:
            raise UpdateError('Настройки обновления изменились после проверки.')
    for name, item in plan['changes'].items():
        if not managed(name):
            raise UpdateError('Защищённый путь в плане обновления.')
        current = path_at(home, name)
        actual = digest(current.read_bytes()) if current.is_file() else None
        if actual != item['before']:
            raise UpdateError('После проверки файл изменился: ' + name)
        if item['after'] is not None and digest(path_at(stage / 'files', name).read_bytes()) != item['after']:
            raise UpdateError('Подготовленный файл повреждён: ' + name)
    if baseline(home) != plan['baseline']:
        raise UpdateError('Установленная версия изменилась после проверки.')
    return stage, plan


def request(home, ident, *, allow_downgrade=False):
    with lock(home):
        _, plan = get_plan(home, ident)
        if plan.get('downgrade') and not allow_downgrade:
            raise UpdateError('Для перехода на старую версию нужно отдельное подтверждение.')
        if not plan['changes']:
            return False
        pending = path_at(workspace(home), 'request.json')
        if pending.exists() and read_json(pending)['id'] != ident:
            raise UpdateError('Другое обновление уже поставлено в очередь.')
        json_write(pending, {'id': ident, 'allow_downgrade': allow_downgrade})
        return True


def apply_files(home, ident):
    """Caller holds lock; bot MUST be stopped. Persistent intent precedes writes."""
    stage, plan = get_plan(home, ident)
    base = workspace(home)
    backup = base / 'backups' / ident
    changes = dict(plan['changes'])
    state = dict(plan['baseline'], files=dict(plan['baseline']['files']), revision=plan['revision'])
    for name, item in changes.items():
        if item['after'] is None:
            state['files'].pop(name, None)
        else:
            state['files'][name] = item['after']
    state_data = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()
    old_state = path_at(home, STATE)
    changes[STATE] = {'before': digest(old_state.read_bytes()) if old_state.exists() else None,
                      'after': digest(state_data), 'mode': 0o600}
    txn = {'id': ident, 'revision': plan['revision'], 'status': 'applying', 'files': changes, 'written': []}
    required = len(state_data) + 16 * 1024 * 1024
    for name, item in changes.items():
        if item['before'] is not None:
            required += path_at(home, name).stat().st_size
        if name != STATE and item['after'] is not None:
            required += path_at(stage / 'files', name).stat().st_size
    if shutil.disk_usage(home).free < required:
        raise UpdateError('Недостаточно места для резервной копии кода и обновления.')
    durable_directory(backup.parent)
    backup.mkdir(mode=0o700)
    sync_directory(backup.parent)
    # Complete backups before the first live write.
    try:
        for name, item in changes.items():
            if item['before'] is not None:
                p = path_at(home, name)
                saved = path_at(backup, name)
                atomic(saved, p.read_bytes(), p.stat().st_mode & 0o777)
        json_write(base / 'transaction.json', txn)
    except Exception:
        if not (base / 'transaction.json').exists() or read_json(base / 'transaction.json')['id'] != ident:
            shutil.rmtree(backup, ignore_errors=True)
        raise
    for name, item in changes.items():
        txn['written'].append(name)
        json_write(base / 'transaction.json', txn)
        target = path_at(home, name)
        if item['after'] is None:
            target.unlink(missing_ok=True)
            sync_directory(target.parent)
        else:
            data = state_data if name == STATE else path_at(stage / 'files', name).read_bytes()
            atomic(target, data, item['mode'])
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
                sync_directory(p.parent)
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
        _, plan = get_plan(home, ident)
        service_matches(home, pid)
        # Everything required for rollback is in this independent transient unit.
        pending = path_at(workspace(home), 'request.json')
        if pending.exists():
            approval = read_json(pending)
            if approval.get('id') != ident:
                raise UpdateError('Запрос на обновление изменился.')
        else:
            approval = {}
        if plan.get('downgrade') and approval.get('allow_downgrade') is not True:
            raise UpdateError('Для перехода на старую версию нужно отдельное подтверждение.')
        if pending.exists():
            pending.unlink()
            sync_directory(pending.parent)
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


def freeze_supervisor(stage):
    # Freeze supervisor code outside the files being replaced. User systemd keeps
    # this process alive while stopping/restarting the bot's own service.
    supervisor = stage / 'worker.py'
    atomic(supervisor, Path(__file__).read_bytes())
    atomic(stage / 'release_channels.py', Path(__file__).with_name('release_channels.py').read_bytes())
    return supervisor


def launch(home, ident, pid):
    service_matches(home, pid)
    stage, _ = get_plan(home, ident)
    supervisor = freeze_supervisor(stage)
    p = subprocess.run(['systemd-run', '--user', '--collect', '--quiet',
        '--unit=nook-update-' + ident, '--property=UMask=0077', sys.executable,
        str(supervisor), '--home', str(home), '_worker', ident, '--pid', str(pid)],
        capture_output=True, timeout=20)
    if p.returncode:
        raise UpdateError('Не удалось запустить независимый обновлятор. Бот не изменён.')


def recover_interrupted(home):
    txn_path = path_at(workspace(home), 'transaction.json')
    try:
        with lock(home):
            if txn_path.exists() and read_json(txn_path)['status'] in ('applying', 'awaiting_health'):
                with runtime_lock(home):
                    restore(home)
    except UpdateBusy:
        # The live supervisor legitimately starts the service during its health check.
        if not txn_path.exists() or read_json(txn_path)['status'] == 'applying':
            raise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--home', type=Path, default=ROOT)
    sub = ap.add_subparsers(dest='command', required=True)
    check = sub.add_parser('check')
    check.add_argument('--version', help='проверить конкретную версию выбранного канала')
    sub.add_parser('releases', help='список релизов, допустимых выбранному каналу')
    req = sub.add_parser('request')
    req.add_argument('id')
    req.add_argument('--yes', action='store_true', required=True)
    req.add_argument('--allow-downgrade', action='store_true')
    apply = sub.add_parser('apply-offline')
    apply.add_argument('id')
    apply.add_argument('--yes', action='store_true', required=True)
    apply.add_argument('--allow-downgrade', action='store_true')
    sub.add_parser('status')
    roll = sub.add_parser('rollback-offline')
    roll.add_argument('--yes', action='store_true', required=True)
    work = sub.add_parser('_worker')
    work.add_argument('id')
    work.add_argument('--pid', type=int, required=True)
    sub.add_parser('_recover')
    args = ap.parse_args()
    home = args.home.expanduser().resolve()
    os.umask(0o077)
    if args.command == 'check':
        plan = prepare(home, args.version)
        print(json.dumps({'id': plan['id'], 'revision': plan['revision'],
                          'version': plan.get('version'),
                          'channel': plan.get('channel'),
                          'downgrade': plan.get('downgrade', False),
                          'changed': list(plan['changes'])}, ensure_ascii=False))
    elif args.command == 'releases':
        settings = update_settings(home)
        if settings['channel'] is None:
            raise UpdateError('Сначала выбери канал обновлений.')
        releases = eligible_releases(installable_releases(list_releases()), settings['channel'])
        print(json.dumps([{'version': item['version'], 'channel': item['channel'],
                           'name': item.get('name'), 'published_at': item.get('published_at')}
                          for item in releases[:20]], ensure_ascii=False))
    elif args.command == 'request':
        queued = request(home, args.id, allow_downgrade=args.allow_downgrade)
        print('Обновление запрошено; мод дождётся завершения задач.' if queued else 'Обновлений нет.')
    elif args.command == '_worker':
        worker(home, args.id, args.pid)
    elif args.command == '_recover':
        recover_interrupted(home)
    elif args.command in ('apply-offline', 'rollback-offline'):
        with lock(home), runtime_lock(home):
            offline(home)
            if args.command == 'apply-offline':
                stage, plan = get_plan(home, args.id)
                if plan.get('downgrade') and not args.allow_downgrade:
                    raise UpdateError('Для перехода на старую версию нужен флаг --allow-downgrade.')
                freeze_supervisor(stage)
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
        txn = read_json(txn_path) if txn_path.exists() else {'status': 'no_updates'}
        txn['update_settings'] = update_settings(home)
        print(json.dumps(txn, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc) if isinstance(exc, UpdateError) else 'Обновление остановлено (' + type(exc).__name__ + '). Личные данные не заменялись.', file=sys.stderr)
        sys.exit(1)
