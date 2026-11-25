"""Exercise both historical source trees against a disposable user systemd unit."""
import argparse
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
if not is_legacy and sys.argv[1] == 'upgrade':
    import asyncio
    from contextlib import suppress

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
    unit_contents = (f'[Service]\nWorkingDirectory={home}\n'
                     f'ExecStart="{home / ".venv/bin/python"}" "{service}" {mode}\n')
    unit_file.write_text(unit_contents)
    try:
        run('systemctl', '--user', 'daemon-reload')
        migration._service_action(unit, 'start')
        assert migration._user_service(home) == unit
        # Wait until SIGTERM handling is installed before exercising cutover.
        run('timeout', '10', 'bash', '-c', 'until test -s "$1"; do sleep .1; done',
            'nook-fixture', str(home / 'memory/started'))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', type=Path)
    args = parser.parse_args()
    for version, mode in (('0.1.0', 'upgrade'), ('0.1.1', 'upgrade'), ('0.1.1', 'rollback'),
                          ('0.1.1', 'offline'), ('0.1.1', 'interrupt')):
        with tempfile.TemporaryDirectory(prefix='nook-systemd-') as base:
            check(Path(base), version, mode, args.artifact.resolve())


if __name__ == '__main__':
    main()
