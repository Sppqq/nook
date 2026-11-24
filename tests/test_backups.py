import io
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import backup_manager as b


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'instance'
        self.home.mkdir()
        self.provider_home = Path(self.temp.name) / 'provider-home'
        self.provider_home.mkdir()
        home_patch = patch.object(Path, 'home', return_value=self.provider_home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.write('config.json', '{"owner_id":123}\n', 0o600)
        self.write('memory/notes.md', 'instance identity\n')
        self.write('mods/custom.py', 'LOCAL=True\n')
        self.write('spool/jobs.json', '[]\n')
        self.write('spool/updates/settings.json', '{"channel":"beta"}\n')

    def write(self, name, data, mode=0o644):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        path.chmod(mode)
        return path

    def test_backup_verifies_and_restores_to_new_directory(self):
        backup = b.create_backup(self.home)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        report = b.verify_backup(backup)
        self.assertEqual(report['files'], 5)
        restored = b.restore_to_new_directory(backup, self.home.parent / 'restored')
        self.assertEqual((restored / 'config.json').read_text(), '{"owner_id":123}\n')
        self.assertEqual((restored / 'memory/notes.md').read_text(), 'instance identity\n')
        self.assertEqual((restored / 'mods/custom.py').read_text(), 'LOCAL=True\n')
        self.assertEqual((restored / 'config.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(b.BackupError, 'новый'):
            b.restore_to_new_directory(backup, restored)

    def test_backup_excludes_environment_and_its_own_archive(self):
        self.write('.venv/bin/python', 'not shipped')
        self.write('.git/config', 'private git data')
        self.write('spool/updates/staged/a/source.tar.gz', 'staging')
        self.write('spool/backups/old.tar.gz', 'previous')
        backup = b.create_backup(self.home)
        with tarfile.open(backup, 'r:gz') as archive:
            names = set(archive.getnames())
        self.assertIn('config.json', names)
        self.assertNotIn('.venv/bin/python', names)
        self.assertNotIn('.git/config', names)
        self.assertNotIn('spool/updates/staged/a/source.tar.gz', names)
        self.assertNotIn('spool/backups/old.tar.gz', names)
        self.assertEqual(len([n for n in names if n.startswith('spool/backups/')]), 0)

    def test_backup_includes_known_external_provider_auth_and_codex_sessions(self):
        private_files = {
            '.codex/auth.json': '{"tokens":"private"}\n',
            '.codex/config.toml': 'model = "test"\n',
            '.codex/sessions/2026/session.jsonl': '{"conversation":"private"}\n',
            '.gemini/antigravity-cli/antigravity-oauth-token': 'oauth-private\n',
        }
        for name, content in private_files.items():
            path = self.provider_home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o600)
        ignored = self.provider_home / '.codex/sessions/cache.tmp'
        ignored.write_text('temporary')

        backup = b.create_backup(self.home)
        restored = b.restore_to_new_directory(backup, self.home.parent / 'restored')
        for name, content in private_files.items():
            rel = Path('provider-home') / name
            self.assertEqual((restored / rel).read_text(), content)
            self.assertEqual((restored / rel).stat().st_mode & 0o777, 0o600)
        self.assertFalse((restored / 'provider-home/.codex/sessions/cache.tmp').exists())

    def test_provider_auth_symlink_fails_closed(self):
        external = self.home.parent / 'external-auth'
        external.write_text('private')
        auth = self.provider_home / '.codex/auth.json'
        auth.parent.mkdir()
        auth.symlink_to(external)
        with self.assertRaisesRegex(b.BackupError, 'ссылка в данных провайдера'):
            b.create_backup(self.home)

    def test_corrupt_content_and_unsafe_members_are_refused(self):
        backup = b.create_backup(self.home)
        broken = self.home.parent / 'corrupt.tar.gz'
        with tarfile.open(backup, 'r:gz') as source, tarfile.open(broken, 'w:gz') as target:
            for member in source.getmembers():
                data = source.extractfile(member).read() if member.isfile() else None
                if member.name == 'memory/notes.md':
                    data = b'X' * len(data)
                target.addfile(member, io.BytesIO(data) if data is not None else None)
        with self.assertRaisesRegex(b.BackupError, 'Контрольная сумма'):
            b.verify_backup(broken)

        unsafe = self.home.parent / 'unsafe.tar.gz'
        with tarfile.open(unsafe, 'w:gz') as archive:
            member = tarfile.TarInfo('../escape')
            member.size = 1
            archive.addfile(member, io.BytesIO(b'x'))
        with self.assertRaises(b.BackupError):
            b.verify_backup(unsafe)

    def test_symlink_in_instance_fails_closed(self):
        external = self.home.parent / 'outside'
        external.write_text('external')
        (self.home / 'linked').symlink_to(external)
        with self.assertRaisesRegex(b.BackupError, 'ссылка'):
            b.create_backup(self.home)
        self.assertEqual(external.read_text(), 'external')

    def test_symlink_directory_is_not_silently_skipped(self):
        outside = self.home.parent / 'outside-dir'
        outside.mkdir()
        (outside / 'secret.txt').write_text('private')
        (self.home / 'linked-dir').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(b.BackupError, 'символическая ссылка'):
            b.create_backup(self.home)

    def test_spool_symlink_is_rejected_before_backup_directory_creation(self):
        separate = self.home.parent / 'separate'
        separate.mkdir()
        outside = self.home.parent / 'outside-spool'
        outside.mkdir()
        (separate / 'spool').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(b.BackupError, 'spool'):
            b.create_backup(separate)
        self.assertFalse((outside / 'backups').exists())


if __name__ == '__main__':
    unittest.main()
