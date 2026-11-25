"""Exercise historical and dependency-changing updates in disposable user systemd services."""
import argparse
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from hashlib import sha256
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import legacy_migration as migration
import nook_update

ROOT = Path(__file__).resolve().parents[1]
SERVICE = """\
import json
from pathlib import Path
import signal
import sys
import time

home = Path.cwd()
sys.path.insert(0, str(home))
manifest = json.loads((home / 'MANIFEST.json').read_text())
version = manifest['version']
is_legacy = version in ('0.1.0', '0.1.1')
def stop(*_):
    if is_legacy:
        (home / 'memory/last-write').write_text(version)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
if not is_legacy and sys.argv[1] in ('upgrade', 'button'):
    import asyncio
    from contextlib import suppress

    from tools.start_guard import recover
    assert not recover(home)

    import bot as nook_bot
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import DeleteMyCommands, GetMe, GetUpdates, SendMessage
    from aiogram.types import Message, Update, User

    class TestTelegram(BaseSession):
        sent_update = False
        saw_reminder = False

        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            raise AssertionError('unexpected Telegram download')
            yield b''

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetMe):
                return User(id=123456, is_bot=True, first_name='Fixture', username='fixture_bot')
            if isinstance(method, DeleteMyCommands):
                return True
            if isinstance(method, GetUpdates):
                if self.sent_update:
                    await asyncio.sleep(0.25)
                    return []
                self.sent_update = True
                return [Update.model_validate({
                    'update_id': 1, 'message': {
                        'message_id': 1, 'date': int(time.time()), 'text': '/jobs',
                        'chat': {'id': 77, 'type': 'private'},
                        'from': {'id': 77, 'is_bot': False, 'first_name': 'Fixture owner'},
                        'entities': [{'type': 'bot_command', 'offset': 0, 'length': 5}],
                    },
                })]
            if isinstance(method, SendMessage):
                self.saw_reminder = 'migration-reminder' in method.text
                return Message.model_validate({
                    'message_id': 2, 'date': int(time.time()),
                    'chat': {'id': 77, 'type': 'private'}, 'text': method.text,
                })
            raise AssertionError(f'unexpected Telegram method {type(method).__name__}')

    session = TestTelegram()

    async def direct_telegram(method, **params):
        if method == 'deleteMessages':
            if session.saw_reminder and params['message_ids'] == [1]:
                (home / 'memory/started').write_text(version)
            return True
        raise AssertionError(f'unexpected direct Telegram method {method}')

    nook_bot.bot.session = session
    nook_bot.tg = direct_telegram
    with suppress(KeyboardInterrupt):
        asyncio.run(nook_bot.main())
    raise SystemExit(0)
if not is_legacy:
    import legacy_migration
    import nook_update

    while legacy_migration.startup_held(home):
        nook_update.heartbeat(home, ['fixture'] if sys.argv[1] == 'rollback' else [])
        time.sleep(0.25)
(home / 'memory/started').write_text(version)
while True:
    time.sleep(0.25)
"""


def run(*command, timeout=45, **kwargs):
    return subprocess.run(command, check=True, timeout=timeout, text=True, **kwargs)


def seed_runtime(home):
    # Use the layouts produced by the old installer and CLI, with synthetic
    # credentials and data. Verify every seeded entry and its permissions.
    now = int(time.time())
    jobs = [{'id': 'abc123', 'at': now + 86400, 'every': 3600, 'created': now,
             'prompt': 'migration-reminder'}]
    config = {'owner_id': 77, 'bot_token': '123456:' + 'X' * 35}
    state = {'owner_id': 77, 'main_thread': 'legacy-thread', 'msg_threads': {'12': 'legacy-thread'},
             'watch': False, 'guard': False, 'introduced': True, 'ui': 'classic',
             'last_housekeeping': time.strftime('%Y-%m-%d')}
    files = {
        'config.json': json.dumps(config), 'state.json': json.dumps(state),
        'secrets.env': 'MIGRATION_FIXTURE=synthetic-secret\n',
        'memory/notes.md': 'private fixture', 'memory/journal.md': 'old journal',
        '.codex/auth.json': '{"fixture":"provider credential"}',
        'keys/id_fixture': 'synthetic private key',
        'nodes.json': '[]', 'ssh_config': '# owner SSH configuration',
        'spool/jobs.json': json.dumps(jobs),
        'spool/mods/mods.json': '{"disabled":["owner_api","openbot_bridge","codex_accounts"]}',
        'spool/mods/owner_custom.state.json': '{"counter":42}',
        'mods/owner_custom.py': 'def setup(mod):\n    pass\n',
        'inbox/attachment.bin': b'\x00\xfffixture\x00',
    }
    for name, data in files.items():
        path = home / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        path.chmod(0o600)
    database = home / 'memory/history.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE history (message TEXT)')
        db.execute("INSERT INTO history VALUES ('prior conversation')")
    database.chmod(0o600)
    (home / 'memory/current-notes').symlink_to('notes.md')
    names = set(files) | {'memory/history.sqlite3', 'memory/current-notes'}
    return {name: entry for name, entry in migration._inventory(home).items() if name in names}


def assert_runtime(home, expected):
    inventory = migration._inventory(home)
    for name, entry in expected.items():
        assert inventory.get(name) == entry, f'runtime data changed: {name}'
    with sqlite3.connect(home / 'memory/history.sqlite3') as db:
        assert db.execute('SELECT message FROM history').fetchall() == [('prior conversation',)]


def stage_legacy(base, version, commit):
    archive = base / 'legacy.tar.gz'
    run('git', '-C', str(ROOT), 'archive', '--format=tar.gz', '-o', str(archive), commit)
    source = base / 'legacy-source'
    source.mkdir()
    # The downloader expects GitHub's common archive prefix. A git archive
    # produced here has none, so extract directly from the pinned Git object.
    run('tar', '-xzf', str(archive), '-C', str(source))
    home = base / 'instance'
    shutil.copytree(source, home)
    for path in home.rglob('*'):
        if path.is_file():
            path.write_bytes(nook_update.rendered(path.read_bytes(), home))
    run(sys.executable, '-m', 'venv', '--without-pip', str(home / '.venv'))
    if version == '0.1.1':
        run(sys.executable, '-c', 'from pathlib import Path; from nook_update import record_install; '
            'record_install(Path.cwd())', cwd=home)
    return home, source


def check_legacy_button(home, source, target, release, service):
    """Run the published v0.1.1 Telegram updater against an isolated service."""
    spec = spec_from_file_location('nook_v011_fixture', source / 'nook_update.py')
    old = module_from_spec(spec)
    spec.loader.exec_module(old)
    from tools.start_guard import recover

    revision = release['revision']
    state = home / 'state.json'
    original = json.loads(state.read_text())
    local_mod = (home / 'mods/owner_custom.py').read_bytes()
    settings_path = nook_update.workspace(home) / 'settings.json'
    service_systemctl = old.systemctl
    systemctl = lambda *args, **kwargs: service_systemctl(
        *(service if arg == 'nook.service' else arg for arg in args), **kwargs)
    health = old.healthy
    stage = old.workspace(home) / 'staged' / '0123456789abcdef'
    old.plan_source(home, target, revision, stage)
    old.atomic(stage / 'worker.py', (source / 'nook_update.py').read_bytes())
    assert old.request(home, stage.name)
    with (patch.object(old, 'systemctl', systemctl),
          patch.object(old, 'healthy', lambda *args: health(*args, timeout=30, stable=2)),
          patch.object(old, 'notify') as notify):
        old.worker(home, stage.name, old.service_matches(home))
        notify.assert_called_once()
    assert old.read_json(old.workspace(home) / 'transaction.json')['status'] == 'success'
    assert settings_path.is_file() is False
    assert nook_update.update_settings(home)['channel'] is None
    assert not (home / 'VERSION').exists()  # This was outside the old source allowlist.
    assert (home / 'mods/owner_custom.py').read_bytes() == local_mod
    run('timeout', '10', 'bash', '-c', 'until [[ "$(cat "$1")" == "$2" ]]; do sleep .1; done',
        'nook-fixture', str(home / 'memory/started'), release['version'])
    current = json.loads(state.read_text())
    assert all(current.get(key) == value for key, value in original.items())
    migration._service_action(service, 'stop')
    old.restore(home)

    # Simulate the old supervisor disappearing during cutover. The new
    # startup guard must restore the old code instead of invoking _recover.
    old.plan_source(home, target, revision, stage.with_name('0123456789abcdee'))
    stage = stage.with_name('0123456789abcdee')
    old.atomic(stage / 'worker.py', (source / 'nook_update.py').read_bytes())
    old.apply_files(home, stage.name)
    assert recover(home)
    assert not recover(home)
    assert (home / 'MANIFEST.json').read_bytes() == (source / 'MANIFEST.json').read_bytes()
    print(f'v0.1.1: original Telegram worker and interrupted rollback passed with {service}', flush=True)


def check(base, version, mode, artifact):
    commit = migration.LEGACY_COMMITS[version]
    home, source = stage_legacy(base, version, commit)
    runtime = seed_runtime(home)
    if mode == 'upgrade':
        python = str(home / '.venv/bin/python')
        run(python, '-m', 'ensurepip')
        run(python, '-m', 'pip', 'install', '--disable-pip-version-check', 'colorama==0.4.6', timeout=120)
    target = base / 'release'
    target.mkdir()
    nook_update.unpack_zip(artifact, target)
    metadata = target / 'NOOK_RELEASE.json'
    release = json.loads(metadata.read_text())
    metadata.unlink()
    (base / 'MIGRATION_SOURCE.json').write_text(json.dumps({
        'schema': 1, **release, 'verified_by': 'nook_update.fetch_release_source',
    }))
    service = base / 'service.py'
    service.write_text(SERVICE)
    unit = 'nook-migration-fixture-' + sha256(str(base).encode()).hexdigest()[:16] + '.service'
    config = Path.home() / '.config/systemd/user'
    config.mkdir(parents=True, exist_ok=True)
    unit_file = config / unit
    python = sys.executable if mode == 'button' else home / '.venv/bin/python'
    unit_contents = (f'[Service]\nWorkingDirectory={home}\n'
                     f'ExecStart="{python}" "{service}" {mode}\n')
    unit_file.write_text(unit_contents)
    try:
        run('systemctl', '--user', 'daemon-reload')
        migration._service_action(unit, 'start')
        assert migration._user_service(home) == unit
        # Wait until SIGTERM handling is installed before exercising cutover.
        run('timeout', '10', 'bash', '-c', 'until test -s "$1"; do sleep .1; done',
            'nook-fixture', str(home / 'memory/started'))
        if mode == 'button':
            check_legacy_button(home, source, target, release, unit)
            return
        backup = base / 'backup'
        journal = migration._journal_path(home)
        def check_health(root, _version):
            assert_runtime(root, runtime)
            return migration._wait_healthy(root, unit, release['revision'],
                                           timeout=25 if mode == 'upgrade' else 12, stable=2)

        transaction = lambda: migration.migrate_transaction(
            home, source, target, old_revision=commit, target_revision=release['revision'],
            backup=backup, stage=base / 'stage', journal=journal,
            stop_service=lambda: migration._service_action(unit, 'stop'),
            start_service=lambda: migration._service_action(unit, 'start'),
            health_check=check_health,
        )
        if mode == 'offline':
            with (patch.dict(os.environ, {'PIP_NO_INDEX': '1'}),
                  unittest.TestCase().assertRaisesRegex(migration.MigrationError, 'отдельную среду')):
                transaction()
            assert not journal.exists()
            assert not backup.exists()
            assert not (home / 'memory/last-write').exists()
            assert migration._user_service(home) == unit
            assert_runtime(home, runtime)
            print(f'v{version}: dependency fetch failure left {unit} running', flush=True)
            return
        if mode == 'interrupt':
            exchange = migration.exchange_directories

            def exchange_then_exit(left, right):
                exchange(left, right)
                os.kill(os.getpid(), signal.SIGKILL)

            pid = os.fork()
            if pid == 0:
                with patch.object(migration, 'exchange_directories', exchange_then_exit):
                    transaction()
                os._exit(1)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
            assert json.loads(journal.read_text())['status'] == 'ready'
            assert json.loads((home / 'MANIFEST.json').read_text())['version'] == release['version']
            # Remove access to the old stage without touching its files. This
            # forces the independent recovery path to use the full backup.
            (base / 'stage').rename(base / 'original-stage')
            assert migration.recover_transaction(
                journal, stop_service=lambda: migration._service_action(unit, 'stop'),
                start_service=lambda: migration._service_action(unit, 'start'),
            ) == 'rolled_back'
            expected = version
        elif mode == 'rollback':
            with unittest.TestCase().assertRaisesRegex(migration.MigrationError, 'откат'):
                transaction()
            assert json.loads(journal.read_text())['status'] == 'rolled_back'
            expected = version
        else:
            assert transaction()['status'] == 'complete'
            expected = release['version']
            result = run(str(home / '.venv/bin/pip'), '--version', capture_output=True)
            assert str(home / '.venv') in result.stdout, result.stdout
            run(str(home / '.venv/bin/python'), '-c',
                "import colorama; assert colorama.__version__ == '0.4.6'")
        run('timeout', '10', 'bash', '-c', 'until [[ "$(cat "$1")" == "$2" ]]; do sleep .1; done',
            'nook-fixture', str(home / 'memory/started'), expected)
        assert json.loads((home / 'MANIFEST.json').read_text())['version'] == expected
        assert_runtime(backup, runtime)
        assert unit_file.read_text() == unit_contents
        if mode == 'upgrade':
            original = json.loads((backup / 'state.json').read_text())
            active = json.loads((home / 'state.json').read_text())
            assert all(active.get(key) == value for key, value in original.items())
            assert active['view_msgs'] == [2]
            assert_runtime(home, {name: entry for name, entry in runtime.items() if name != 'state.json'})
            journal_output = run('journalctl', '--user', '-u', unit, '--no-pager',
                                 '-o', 'cat', capture_output=True).stdout
            assert 'Traceback' not in journal_output, journal_output
            assert 'ERROR' not in journal_output, journal_output
        else:
            assert_runtime(home, runtime)
        assert (home / 'memory/last-write').read_text() == version
        assert (backup / 'memory/last-write').read_text() == version
        assert migration._user_service(home) == unit
        print(f'v{version}: {mode} passed with {unit}', flush=True)
    except BaseException:
        subprocess.run(['journalctl', '--user', '-u', unit, '--no-pager', '-n', '50'],
                       timeout=10, check=False)
        raise
    finally:
        migration._service_action(unit, 'stop')
        unit_file.unlink(missing_ok=True)
        run('systemctl', '--user', 'daemon-reload')


def check_dependency_update(base, artifact):
    home = base / 'instance'
    home.mkdir()
    nook_update.unpack_zip(artifact, home)
    (home / 'NOOK_RELEASE.json').unlink()
    runtime = seed_runtime(home)
    service = """\
import os
from pathlib import Path
import sys
import time
from tools.start_guard import recover
home = Path.cwd()
if recover(home):
    os.execv(sys.executable, [sys.executable, *sys.argv])
from nook_update import environment_held, heartbeat
if VERSION == 'new':
    import colorama
    assert colorama.__version__ == '0.4.6'
while environment_held(home):
    heartbeat(home)
    time.sleep(.1)
(home / 'memory/started').write_text(VERSION)
while True:
    time.sleep(.1)
"""
    (home / 'bot.py').write_text('VERSION = "old"\n' + service)
    (home / 'requirements.txt').write_text('# old fixture\n')
    (home / 'requirements-voice.txt').write_text('# old voice fixture\n')
    python = home / '.venv/bin/python'
    run(sys.executable, '-m', 'venv', '--without-pip', str(home / '.venv'))
    nook_update.record_install(home)
    old_environment = nook_update.environment_identity(home / '.venv')
    settings = nook_update.save_update_settings(home, channel='stable')
    ident = 'fedcba9876543210'
    stage = nook_update.workspace(home) / 'staged' / ident
    source = stage / 'source'
    source.mkdir(parents=True)
    nook_update.unpack_zip(artifact, source)
    (source / 'NOOK_RELEASE.json').unlink()
    (source / 'bot.py').write_text('VERSION = "new"\n' + service)
    (source / 'requirements.txt').write_text('colorama==0.4.6\n')
    (source / 'requirements-voice.txt').write_text('# old voice fixture\n')
    manifest = nook_update.read_json(source / 'MANIFEST.json')
    for name in ('bot.py', *nook_update.REQUIREMENTS):
        manifest['sha256'][name] = sha256((source / name).read_bytes()).hexdigest()
    nook_update.json_write(source / 'MANIFEST.json', manifest)
    plan = nook_update.plan_source(home, source, 'b' * 40, stage, migrate_dependencies=True)
    plan.update(channel='stable', release_settings=settings)
    nook_update.json_write(stage / 'plan.json', plan)
    nook_update.freeze_supervisor(stage)
    assert nook_update.request(home, ident)

    unit = 'nook-update-fixture-' + sha256(str(base).encode()).hexdigest()[:16] + '.service'
    unit_path = Path.home() / '.config/systemd/user' / unit
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_contents = f'[Service]\nWorkingDirectory={home}\nExecStart="{python}" "{home / "bot.py"}"\n'
    unit_path.write_text(unit_contents)
    systemctl = nook_update.systemctl
    health = nook_update.healthy

    def fixture_systemctl(*args, **kwargs):
        return systemctl(*(unit if arg == 'nook.service' else arg for arg in args), **kwargs)

    try:
        # Use the v0.1.1 unit layout without ExecStartPre. The bot entry point
        # invokes its guard before loading the Python dependencies.
        fixture_systemctl('daemon-reload')
        fixture_systemctl('start', 'nook.service')
        started = home / 'memory/started'
        run('timeout', '10', 'bash', '-c', 'until test -s "$1"; do sleep .1; done', 'fixture', str(started))
        with (patch.object(nook_update, 'systemctl', fixture_systemctl),
              patch.object(nook_update, 'healthy', lambda *args: health(*args, timeout=25, stable=2)),
              patch.object(nook_update, 'notify') as notify):
            nook_update.worker(home, ident, nook_update.service_matches(home))
            notify.assert_called_once()
        run('timeout', '10', 'bash', '-c', 'until [[ "$(cat "$1")" == "new" ]]; do sleep .1; done',
            'fixture', str(started))
        assert nook_update.environment_identity(home / '.venv') != old_environment
        journal = nook_update.read_json(nook_update.workspace(home) / 'transaction.json')
        assert journal['status'] == 'success'
        assert_runtime(Path(journal['snapshot']['root']), runtime)
        assert_runtime(home, runtime)
        fixture_systemctl('stop', 'nook.service')
        nook_update.restore(home)
        assert nook_update.environment_identity(home / '.venv') == old_environment
        fixture_systemctl('start', 'nook.service')
        run('timeout', '10', 'bash', '-c', 'until [[ "$(cat "$1")" == "old" ]]; do sleep .1; done',
            'fixture', str(started))
        assert_runtime(home, runtime)
        assert unit_path.read_text() == unit_contents
        print('Dependency-changing update, startup gate and rollback passed with user systemd', flush=True)
    finally:
        fixture_systemctl('stop', 'nook.service', check=False)
        unit_path.unlink(missing_ok=True)
        fixture_systemctl('daemon-reload')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', type=Path)
    args = parser.parse_args()
    for version, mode in (('0.1.0', 'upgrade'), ('0.1.1', 'upgrade'), ('0.1.1', 'rollback'),
                          ('0.1.1', 'offline'), ('0.1.1', 'interrupt'), ('0.1.1', 'button')):
        with tempfile.TemporaryDirectory(prefix='nook-systemd-') as base:
            check(Path(base), version, mode, args.artifact.resolve())
    with tempfile.TemporaryDirectory(prefix='nook-update-systemd-') as base:
        check_dependency_update(Path(base), args.artifact.resolve())


if __name__ == '__main__':
    main()
