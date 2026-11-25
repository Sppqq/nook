"""Compatibility-planner tests; never activate a copy or contact a provider."""
import ctypes
import hashlib

import nook_update
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import legacy_migration as migration


class LegacyMigrationPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.old = self.root / 'old-source'
        self.new = self.root / 'new-source'
        self.old.mkdir()
        self.new.mkdir()
        self.old_files = {
            'bot.py': 'VERSION_LABEL = "old"\n',
            'requirements.txt': 'aiogram==3.20.0\n',
            'mods/agent.py': 'VALUE = 1\n',
        }
        self.target_files = {
            'bot.py': 'VERSION_LABEL = "new"\n',
            'requirements.txt': 'aiogram==3.31.0\n',
            'mods/agent.py': 'VALUE = 1\n',
            'mods/self_update.py': 'def setup(mod):\n    pass\n',
            'VERSION': '0.1.2-alpha.1\n',
        }
        self._source(self.old, self.old_files, '0.1.0', legacy=True)
        self._source(self.new, self.target_files, '0.1.2-alpha.1', legacy=False)
        self.home.mkdir()
        self._copy_files(self.old_files, self.home)
        (self.home / 'MANIFEST.json').write_bytes((self.old / 'MANIFEST.json').read_bytes())
        self._copy_files({
            'config.json': '{"owner_id": 77, "bot_token": "local-test-only"}',
            'state.json': '{"identity":"local"}',
            'memory/notes.md': 'private memory',
            '.codex/auth.json': '{"session":"local"}',
            'mods/custom.py': 'LOCAL = True\n',
        }, self.home)

    @staticmethod
    def _copy_files(files, root):
        for name, value in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding='utf-8')

    def _source(self, root, files, version, *, legacy):
        self._copy_files(files, root)
        manifest = {
            'format': 1,
            'version': version,
            'sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                       for name in files},
        }
        if not legacy:
            manifest['update_api'] = 1
        (root / 'MANIFEST.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_identifies_both_historical_update_paths(self):
        self.assertEqual(migration.identify_install(self.home).updater, 'one-shot')
        old = self.home / 'MANIFEST.json'
        manifest = json.loads(old.read_text())
        manifest['version'] = '0.1.1'
        (self.home / 'mods/self_update.py').write_text('def setup(mod): pass\n')
        old.write_text(json.dumps(manifest))
        profile = migration.identify_install(self.home)
        self.assertEqual(profile.version, '0.1.1')
        self.assertEqual(profile.updater, 'telegram')

    def test_real_v011_installer_baseline_accepted_without_rebasing_edits(self):
        files = dict(self.old_files, **{'mods/self_update.py': 'def setup(mod): pass\n'})
        self._source(self.old, files, '0.1.1', legacy=True)
        self._copy_files(files, self.home)
        manifest = self.old / 'MANIFEST.json'
        (self.home / manifest.name).write_bytes(manifest.read_bytes())
        baseline = {name: nook_update.digest((self.home / name).read_bytes())
                    for name in set(files) | {manifest.name}}
        state = self.home / nook_update.STATE
        state.write_text(json.dumps({'schema': 1, 'revision': 'installed', 'files': baseline}))
        plan = migration.plan_source_migration(
            self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.1'])
        self.assertTrue(plan['dependency_change_required'])
        baseline['bot.py'] = 'b' * 64
        state.write_text(json.dumps({'schema': 1, 'revision': 'installed', 'files': baseline}))
        with self.assertRaisesRegex(migration.MigrationError, 'Эталон'):
            migration.plan_source_migration(
                self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.1'])

    def test_plan_updates_code_and_dependencies_without_touching_runtime(self):
        plan = migration.plan_source_migration(
            self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.0'])
        self.assertEqual(plan['source_version'], '0.1.0')
        self.assertTrue(plan['dependency_change_required'])
        self.assertEqual(plan['updater'], 'one-shot')
        self.assertIn('bot.py', plan['changes'])
        self.assertIn('requirements.txt', plan['changes'])
        self.assertEqual(plan['preserve_local'], [])
        self.assertTrue(plan['runtime_paths_untouched'])
        self.assertEqual((self.home / 'memory/notes.md').read_text(), 'private memory')
        self.assertEqual((self.home / '.codex/auth.json').read_text(), '{"session":"local"}')

    def test_unchanged_upstream_preserves_local_source_edit(self):
        (self.home / 'mods/agent.py').write_text('OWNER_VALUE = 2\n')
        plan = migration.plan_source_migration(
            self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.0'])
        self.assertIn('mods/agent.py', plan['preserve_local'])
        self.assertNotIn('mods/agent.py', plan['changes'])

    def test_conflicting_core_edit_stops_without_live_writes(self):
        original = 'OWNER_CORE = True\n'
        (self.home / 'bot.py').write_text(original)
        with self.assertRaisesRegex(migration.MigrationError, 'конфликт'):
            migration.plan_source_migration(
                self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.0'])
        self.assertEqual((self.home / 'bot.py').read_text(), original)

    def test_user_file_collision_with_new_module_stops(self):
        (self.home / 'mods/new_feature.py').write_text('PRIVATE = True\n')
        self.target_files['mods/new_feature.py'] = 'UPSTREAM = True\n'
        self._source(self.new, self.target_files, '0.1.2-alpha.1', legacy=False)
        with self.assertRaisesRegex(migration.MigrationError, 'mods/new_feature.py'):
            migration.plan_source_migration(
                self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.0'])
        self.assertEqual((self.home / 'mods/new_feature.py').read_text(), 'PRIVATE = True\n')

    def test_wrong_historical_revision_and_corrupt_source_fail_closed(self):
        with self.assertRaisesRegex(migration.MigrationError, 'commit'):
            migration.plan_source_migration(self.home, self.old, self.new, old_revision='0' * 40)
        (self.old / 'bot.py').write_text('changed after manifest\n')
        with self.assertRaisesRegex(migration.MigrationError, 'хеш'):
            migration.plan_source_migration(
                self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.0'])

    def test_complete_snapshot_and_side_by_side_stage_preserve_runtime(self):
        plan = migration.plan_source_migration(
            self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.0'])
        backup = self.root / 'complete-backup'
        result = migration.create_snapshot(self.home, backup)
        self.assertEqual(result['entries'], len(migration._inventory(self.home)))
        self.assertEqual(migration._inventory(self.home), migration._inventory(backup))

        stage = self.root / 'home.stage'
        marker = migration.stage_source_tree(self.home, self.new, plan, stage)
        self.assertEqual(marker['state'], 'staged')
        self.assertEqual((self.home / 'bot.py').read_text(), self.old_files['bot.py'])
        self.assertEqual((stage / 'bot.py').read_text(), self.target_files['bot.py'])
        self.assertEqual((stage / 'config.json').read_text(), '{"owner_id": 77, "bot_token": "local-test-only"}')
        self.assertEqual((stage / 'state.json').read_text(), '{"identity":"local"}')
        self.assertEqual((stage / 'memory/notes.md').read_text(), 'private memory')
        self.assertEqual((stage / '.codex/auth.json').read_text(), '{"session":"local"}')
        self.assertTrue((stage / 'mods/custom.py').is_file())

    @unittest.skipUnless(hasattr(ctypes.CDLL(None), 'renameat2'), 'Linux directory exchange required')
    def test_atomic_directory_exchange_and_rollback(self):
        live = self.root / 'live'
        stage = self.root / 'live.stage'
        live.mkdir(); stage.mkdir()
        (live / 'identity').write_text('old')
        (stage / 'identity').write_text('new')
        migration.exchange_directories(live, stage)
        self.assertEqual((live / 'identity').read_text(), 'new')
        self.assertEqual((stage / 'identity').read_text(), 'old')
        migration.exchange_directories(live, stage)
        self.assertEqual((live / 'identity').read_text(), 'old')

    def test_snapshot_space_failure_does_not_create_backup(self):
        backup = self.root / 'too-large-backup'
        with self.assertRaisesRegex(migration.MigrationError, 'свободного места'):
            migration.create_snapshot(self.home, backup, min_free_bytes=10**18)
        self.assertFalse(backup.exists())

    def test_new_virtual_environment_is_prepared_only_in_the_staged_copy(self):
        self.target_files['requirements.txt'] = '# no third-party packages in this fixture\n'
        self._source(self.new, self.target_files, '0.1.2-alpha.1', legacy=False)
        plan = migration.plan_source_migration(
            self.home, self.old, self.new, old_revision=migration.LEGACY_COMMITS['0.1.0'])
        stage = self.root / 'home.stage'
        migration.stage_source_tree(self.home, self.new, plan, stage)
        result = migration.build_staged_environment(self.home, stage, self.new, python=sys.executable)
        self.assertEqual(result['extra_packages_preserved'], 0)
        self.assertTrue((stage / '.venv/bin/python').is_file())
        self.assertFalse((self.home / '.venv').exists())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux migration required')
    def test_failed_health_check_restores_the_complete_old_tree(self):
        self.target_files['requirements.txt'] = '# empty fixture\n'
        self._source(self.new, self.target_files, '0.1.2-alpha.1', legacy=False)
        (self.root / 'MIGRATION_SOURCE.json').write_text(json.dumps({
            'schema': 1, 'version': '0.1.2-alpha.1', 'revision': 'b' * 40,
            'verified_by': 'nook_update.fetch_release_source'}))
        stage = self.root / 'home.stage'
        backup = self.root / 'home.backup'
        journal = self.root / 'migration.json'
        service = []
        with self.assertRaisesRegex(migration.MigrationError, 'откат'):
            migration.migrate_transaction(
                self.home, self.old, self.new,
                old_revision=migration.LEGACY_COMMITS['0.1.0'], target_revision='b' * 40,
                stage=stage, backup=backup, journal=journal,
                stop_service=lambda: service.append('stop'),
                start_service=lambda: service.append('start'),
                health_check=lambda *_: False, python=sys.executable)
        self.assertEqual(json.loads(journal.read_text())['status'], 'rolled_back')
        self.assertEqual((self.home / 'bot.py').read_text(), self.old_files['bot.py'])
        self.assertEqual((self.home / 'config.json').read_text(),
                         '{"owner_id": 77, "bot_token": "local-test-only"}')
        self.assertEqual((self.home / '.codex/auth.json').read_text(), '{"session":"local"}')
        self.assertTrue(backup.is_dir())
        self.assertEqual(service, ['stop', 'start', 'stop', 'start'])

    @unittest.skipUnless(sys.platform == 'linux', 'Linux migration required')
    def test_recovery_handles_power_loss_after_atomic_switch_before_journal_write(self):
        self.target_files['requirements.txt'] = '# empty fixture\n'
        self._source(self.new, self.target_files, '0.1.2-alpha.1', legacy=False)
        (self.root / 'MIGRATION_SOURCE.json').write_text(json.dumps({
            'schema': 1, 'version': '0.1.2-alpha.1', 'revision': 'b' * 40,
            'verified_by': 'nook_update.fetch_release_source'}))
        real_exchange = migration.exchange_directories
        calls = []

        def exchange_then_interrupt(left, right):
            real_exchange(left, right)
            calls.append('exchange')
            if len(calls) == 1:
                raise OSError('simulated process loss after renameat2')

        journal = self.root / 'migration.json'
        service = []
        with patch.object(migration, 'exchange_directories', side_effect=exchange_then_interrupt):
            with self.assertRaisesRegex(OSError, 'simulated process loss'):
                migration.migrate_transaction(
                    self.home, self.old, self.new,
                    old_revision=migration.LEGACY_COMMITS['0.1.0'], target_revision='b' * 40,
                    stage=self.root / 'home.stage', backup=self.root / 'home.backup',
                    journal=journal, stop_service=lambda: service.append('stop'),
                    start_service=lambda: service.append('start'),
                    health_check=lambda *_: True, python=sys.executable)
        state = migration.recover_transaction(
            journal, stop_service=lambda: service.append('recover-stop'),
            start_service=lambda: service.append('recover-start'))
        self.assertEqual(state, 'rolled_back')
        self.assertEqual((self.home / 'bot.py').read_text(), self.old_files['bot.py'])
        self.assertEqual((self.home / '.codex/auth.json').read_text(), '{"session":"local"}')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux migration required')
    def test_final_runtime_write_on_service_stop_is_preserved(self):
        self.target_files['requirements.txt'] = '# empty fixture\n'
        self._source(self.new, self.target_files, '0.1.2-alpha.1', legacy=False)
        (self.root / 'MIGRATION_SOURCE.json').write_text(json.dumps({
            'schema': 1, 'version': '0.1.2-alpha.1', 'revision': 'b' * 40,
            'verified_by': 'nook_update.fetch_release_source'}))
        service = []

        def stop_and_mutate():
            service.append('stop')
            (self.home / 'state.json').write_text('{"identity":"latest"}')

        journal = self.root / 'migration.json'
        with patch.object(migration, 'build_staged_environment') as build:
            def build_fixture(_home, build_root, _target, **kwargs):
                scripts = build_root / '.venv/bin'
                scripts.mkdir(parents=True)
                (scripts / 'test-command').write_text(f'#!{scripts}/python\n')
            build.side_effect = build_fixture
            migration.migrate_transaction(
                self.home, self.old, self.new,
                old_revision=migration.LEGACY_COMMITS['0.1.0'], target_revision='b' * 40,
                stage=self.root / 'home.stage', backup=self.root / 'home.backup',
                journal=journal, stop_service=stop_and_mutate,
                start_service=lambda: service.append('start'),
                health_check=lambda *_: True, python=sys.executable)
        self.assertEqual((self.home / 'bot.py').read_text(), self.target_files['bot.py'])
        expected = '{"identity":"latest"}'
        self.assertEqual((self.home / 'state.json').read_text(), expected)
        self.assertEqual((self.root / 'home.backup/state.json').read_text(), expected)
        self.assertEqual((self.home / '.venv/bin/test-command').read_text(),
                         f'#!{self.home}/.venv/bin/python\n')
        self.assertEqual(json.loads(journal.read_text())['status'], 'complete')
        self.assertEqual(service, ['stop', 'start'])


if __name__ == '__main__':
    unittest.main()
