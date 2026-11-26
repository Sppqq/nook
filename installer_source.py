"""Resolve and verify an installation source before invoking its wizard."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from release_channels import (CHANNELS, CHANNEL_LABELS, channel_for_version, eligible_releases,
                              installable_releases, select_release)


def choose_channel(channel=None, version=None, yes=False):
    if channel is not None:
        return channel
    if version is not None:
        return channel_for_version(version)
    if yes or not sys.stdin.isatty():
        return 'stable'
    print('  Канал установки: ' + '; '.join(
        f'{i} — {CHANNEL_LABELS[name]}' for i, name in enumerate(CHANNELS, 1)))
    while True:
        choice = input('  Канал [1–5, Enter = 1]: ').strip() or '1'
        if choice in ('1', '2', '3', '4', '5'):
            return CHANNELS[int(choice) - 1]
        print('  Введи число от 1 до 5.')


def published(releases, channel, version):
    releases = installable_releases(releases)
    try:
        return select_release(releases, channel, version)
    except ValueError as error:
        available = ', '.join(CHANNEL_LABELS[name] for name in CHANNELS
                              if name != channel and eligible_releases(releases, name))
        alternatives = (' Выбери другой канал: ' + available + '.' if available else '')
        raise ValueError(f'{error} Канал: {CHANNEL_LABELS[channel]}.{alternatives} '
                         'Для исходников ветки доступен явный режим --dev.') from None


def install_from_source(argv, *, channel=None, version=None, yes=False, dev=False):
    from nook_update import (REPO, UpdateError, download, fetch_release_source,
                             list_releases, unpack, verify_source)

    try:
        if dev and version is not None:
            raise ValueError('--dev и --version несовместимы.')
        with tempfile.TemporaryDirectory(prefix='nook-install-') as stage_dir:
            stage = Path(stage_dir)
            if dev:
                archive = stage / 'dev.tar.gz'
                download(f'https://codeload.github.com/{REPO}/tar.gz/refs/heads/dev',
                         archive, 10 * 1024 * 1024)
                source = stage / 'source'
                source.mkdir()
                unpack(archive, source)
                verify_source(source)
                if not (source / 'VERSION').is_file():
                    if channel is not None:
                        raise ValueError('Эта версия ветки dev ещё не поддерживает каналы.')
                    argv = [arg for arg in argv if arg != '--dev']
            else:
                selected = choose_channel(channel, version, yes)
                release = published(list_releases(), selected, version)
                source, _ = fetch_release_source(release, stage)
                if channel is None:
                    argv = [*argv, '--channel', selected]
            env = dict(os.environ, NOOK_STAGED_INSTALL=str(source))
            return subprocess.run([sys.executable, str(source / 'install.py'), *argv], env=env).returncode
    except (OSError, EOFError, ValueError, UpdateError) as error:
        raise SystemExit(str(error)) from None


def migrate_from_release(home, version=None, *, channel=None):
    from nook_update import UpdateError, fetch_release_source, list_releases

    try:
        release = published(list_releases(), choose_channel(channel, version), version)
        version = release['version']
        with tempfile.TemporaryDirectory(prefix='nook-migrate-') as stage:
            source, _ = fetch_release_source(release, Path(stage))
            supervisor = source / 'legacy_migration.py'
            if not supervisor.is_file():
                raise ValueError('В этом релизе нет переходного обновлятора Nook.')
            return subprocess.run([sys.executable, str(supervisor), 'migrate',
                                   '--home', home, '--target-version', version]).returncode
    except (OSError, EOFError, ValueError, UpdateError) as error:
        raise SystemExit(str(error)) from None


def main():
    if sys.argv[1:2] == ['--migrate']:
        parser = argparse.ArgumentParser(prog='bootstrap.sh --migrate', allow_abbrev=False)
        parser.add_argument('--home', required=True)
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--version')
        target.add_argument('--channel', choices=CHANNELS)
        parser.add_argument('--yes', action='store_true', required=True)
        args = parser.parse_args(sys.argv[2:])
        sys.exit(migrate_from_release(args.home, args.version, channel=args.channel))
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--channel', choices=CHANNELS)
    parser.add_argument('--version')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--dev', action='store_true')
    args, _ = parser.parse_known_args()
    sys.exit(install_from_source(sys.argv[1:], **vars(args)))


if __name__ == '__main__':
    main()
