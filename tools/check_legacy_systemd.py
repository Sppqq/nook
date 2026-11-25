"""Exercise both historical source trees against a disposable user systemd unit."""
import argparse
import json
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

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
def stop(*_):
    if version in ('0.1.0', '0.1.1'):
        (home / 'memory/last-write').write_text(version)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
(home / 'memory/started').write_text(version)
while True:
    if 'update_api' in manifest:
        import nook_update
        nook_update.heartbeat(home, ['fixture'] if sys.argv[1] == 'rollback' else [])
    time.sleep(0.25)
"""


def run(*command, **kwargs):
    return subprocess.run(command, check=True, timeout=45, text=True, **kwargs)


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
    (home / 'memory').mkdir()
    (home / 'memory/notes.md').write_text('private fixture')
    if version == '0.1.1':
        run(sys.executable, '-c', 'from pathlib import Path; from nook_update import record_install; '
            'record_install(Path.cwd())', cwd=home)
    return home, source


def check(base, version, mode, artifact):
    commit = migration.LEGACY_COMMITS[version]
    home, source = stage_legacy(base, version, commit)
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
    unit_file.write_text(f'[Service]\nWorkingDirectory={home}\n'
                         f'ExecStart="{sys.executable}" "{service}" {mode}\n')
    try:
        run('systemctl', '--user', 'daemon-reload')
        migration._service_action(unit, 'start')
        assert migration._user_service(home) == unit
        # Wait until SIGTERM handling is installed before exercising cutover.
        run('timeout', '10', 'bash', '-c', 'until test -s "$1"; do sleep .1; done',
            'nook-fixture', str(home / 'memory/started'))
        backup = base / 'backup'
        journal = base / 'journal.json'
        transaction = lambda: migration.migrate_transaction(
            home, source, target, old_revision=commit, target_revision=release['revision'],
            backup=backup, stage=base / 'stage', journal=journal,
            stop_service=lambda: migration._service_action(unit, 'stop'),
            start_service=lambda: migration._service_action(unit, 'start'),
            health_check=lambda root, _version: migration._wait_healthy(
                root, unit, release['revision'], timeout=12, stable=2),
        )
        if mode == 'rollback':
            with unittest.TestCase().assertRaisesRegex(migration.MigrationError, 'откат'):
                transaction()
            assert json.loads(journal.read_text())['status'] == 'rolled_back'
            expected = version
        else:
            assert transaction()['status'] == 'complete'
            expected = release['version']
            result = run(str(home / '.venv/bin/pip'), '--version', capture_output=True)
            assert str(home / '.venv') in result.stdout, result.stdout
        assert json.loads((home / 'MANIFEST.json').read_text())['version'] == expected
        assert (home / 'memory/notes.md').read_text() == 'private fixture'
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
    for version, mode in (('0.1.0', 'upgrade'), ('0.1.1', 'upgrade'), ('0.1.1', 'rollback')):
        with tempfile.TemporaryDirectory(prefix='nook-systemd-') as base:
            check(Path(base), version, mode, args.artifact.resolve())


if __name__ == '__main__':
    main()
