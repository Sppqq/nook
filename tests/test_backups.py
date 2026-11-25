import io
from pathlib import Path
import runpy
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import backup_manager as b
from nook_update import runtime_lock


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
        self.write('spool/updates/request.json', '{"id":"pending"}')
        self.write('spool/backups/old.tar.gz', 'previous')
        backup = b.create_backup(self.home)
        with tarfile.open(backup, 'r:gz') as archive:
            names = set(archive.getnames())
        self.assertIn('config.json', names)
        self.assertNotIn('.venv/bin/python', names)
        self.assertNotIn('.git/config', names)
        self.assertNotIn('spool/updates/staged/a/source.tar.gz', names)
        self.assertNotIn('spool/updates/request.json', names)
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

        default_backup = b.create_backup(self.home)
        with tarfile.open(default_backup, 'r:gz') as archive:
            self.assertFalse(any(name.startswith('provider-home/') for name in archive.getnames()))
        backup = b.create_backup(self.home, include_providers=True)
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
            b.create_backup(self.home, include_providers=True)

    def test_live_wal_and_custom_instance_restore(self):
        self.write('.agents/skills/custom/SKILL.md', 'custom tool')
        self.write('secrets.env', 'OWNER_SETTING=private')
        self.write('keys/id_ed25519', 'test private key')
        database = self.home / 'memory' / 'conversation.db'
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE conversations(text)')
        connection.execute("INSERT INTO conversations VALUES ('committed')")
        connection.commit()
        connection.execute("INSERT INTO conversations VALUES ('uncommitted')")
        with runtime_lock(self.home):
            with self.assertRaisesRegex(b.BackupError, 'уже работает'):
                b.create_backup(self.home)
            archive = b.create_backup(self.home, online=True)
        destination = b.restore_to_new_directory(archive, self.home.parent / 'restored')
        with sqlite3.connect(destination / 'memory/conversation.db') as restored:
            self.assertEqual(restored.execute('SELECT text FROM conversations').fetchall(), [('committed',)])
        self.assertEqual((destination / '.agents/skills/custom/SKILL.md').read_text(), 'custom tool')
        self.assertEqual((destination / 'mods/custom.py').read_text(), 'LOCAL=True\n')
        self.assertEqual((destination / 'memory/notes.md').read_text(), 'instance identity\n')
        self.assertEqual((destination / 'keys/id_ed25519').stat().st_mode & 0o777, 0o600)
        self.assertEqual((destination / 'secrets.env').read_text(), 'OWNER_SETTING=private')
        with tarfile.open(archive, 'r:gz') as tar:
            self.assertNotIn('memory/conversation.db-wal', tar.getnames())

    def test_concurrent_file_replacement_aborts_and_keeps_last_good_copy(self):
        first = b.create_backup(self.home, keep=1)
        archive_file = b._archive_file
        original = self.home / 'memory/notes.md'

        def replace_after_read(*args, **kwargs):
            record = archive_file(*args, **kwargs)
            if args[2] == 'memory/notes.md':
                original.rename(self.home.parent / 'old-notes')
                original.write_text('replacement')
            return record

        with patch.object(b, '_archive_file', side_effect=replace_after_read):
            with self.assertRaisesRegex(b.BackupError, 'изменились'):
                b.create_backup(self.home, keep=1)
        self.assertTrue(first.is_file())
        self.assertEqual(b.verify_backup(first)['files'], 5)
        second = b.create_backup(self.home, keep=1)
        self.assertTrue(second.is_file())
        self.assertFalse(first.exists())

    def test_backup_refuses_incomplete_update(self):
        first = b.create_backup(self.home)
        self.write('spool/updates/transaction.json', '{"status":"awaiting_health"}')
        with self.assertRaisesRegex(b.BackupError, 'незавершённое обновление'):
            b.create_backup(self.home, keep=1)
        self.assertTrue(first.is_file())

    def test_restored_tools_use_restored_runtime_paths(self):
        repo = Path(b.__file__).resolve().parent
        paths = {
            'bin/projects': ('BASE', ''),
            'mods/openbot_bridge.py': ('MEMORY', 'memory'),
            'mods/owner_api.py': ('ROOT', 'spool/owner-api'),
            'setup/nook-api/mcp_server.py': ('TOKEN', 'spool/owner-api/token'),
        }
        for name in paths:
            self.write(name, (repo / name).read_text())
        archive = b.create_backup(self.home)
        destination = b.restore_to_new_directory(archive, self.home.parent / 'restored')
        for name, (key, relative) in paths.items():
            with self.subTest(name=name):
                self.assertEqual(runpy.run_path(str(destination / name))[key], destination / relative)

    @unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'age is optional')
    def test_age_archive_requires_identity_and_enforces_limits(self):
        identity = self.home.parent / 'age-key'
        subprocess.run(['age-keygen', '-o', str(identity)], check=True, capture_output=True)
        recipient = subprocess.check_output(['age-keygen', '-y', identity], text=True).strip()
        archive = b.create_backup(self.home, recipient=recipient, identity=identity)
        self.assertTrue(archive.name.endswith('.tar.gz.age'))
        with self.assertRaisesRegex(b.BackupError, 'age-ключ'):
            b.verify_backup(archive)
        self.assertEqual(b.verify_backup(archive, identity=identity)['files'], 5)
        destination = b.restore_to_new_directory(archive, self.home.parent / 'restored', identity=identity)
        self.assertEqual((destination / 'memory/notes.md').read_text(), 'instance identity\n')
        with self.assertRaisesRegex(b.BackupError, 'лимит'):
            b.verify_backup(archive, limits=b.BackupLimits(archive_size=10), identity=identity)

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
