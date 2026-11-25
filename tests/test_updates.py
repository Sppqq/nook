"""Update preservation, conflicts, transaction failure and independent recovery."""
import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import nook_update as u


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'installation'
        self.home.mkdir()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.ident = 'a' * 16
        self.stage = u.workspace(self.home) / 'staged' / self.ident
        self.stage.mkdir(parents=True)
        self.files = {'bot.py': 'print("old")\n', 'requirements.txt': 'example==1\n',
                      'requirements-voice.txt': '# optional\n', 'mods/bundled.py': 'VALUE=1\n',
                      'tools/start_guard.py': '# installed guard\n', 'nook_update.py': '# installed updater\n',
                      'AGENTS.md': 'source instructions\n', '.codex/config.toml': '# provider\n',
                      'templates/runtime/state.json': '{}\n', '.agents/skills/demo/SKILL.md': 'demo\n'}
        self.publish()
        shutil.copytree(self.source, self.home, dirs_exist_ok=True)
        u.record_install(self.home)
        self.private = {'config.json': '{"bot_token":"private-local-token","owner_id":1}',
                        'state.json': '{"main_thread":"conversation"}', 'secrets.env': 'PRIVATE=secret\n',
                        'memory/notes.md': 'owner memory', 'spool/jobs.json': '["job"]',
                        'spool/mods/mods.json': '{"disabled":["x"]}',
                        'mods/custom.py': 'LOCAL=True\n', 'keys/pair.key': 'private key',
                        'nodes.json': '["local-node"]', 'ssh_config': '# local',
                        '.codex/auth.json': '{"session":"private"}'}
        for n, data in self.private.items():
            p = self.home / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data)

    def publish(self, **changes):
        self.files.update(changes)
        for n, data in self.files.items():
            p = self.source / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data)
        u.json_write(self.source / 'MANIFEST.json', {'format': 1, 'update_api': 1, 'version': 'test', 'sha256': {
            n: u.digest(data.encode()) for n, data in self.files.items()}})

    def plan(self):
        return u.plan_source(self.home, self.source, 'b' * 40, self.stage)

    def assert_private(self):
        for n, data in self.private.items():
            with self.subTest(n=n):
                self.assertEqual((self.home / n).read_text(), data)

    def test_apply_preserves_all_runtime_and_custom_files(self):
        self.publish(**{'bot.py': 'print("new")\n', 'mods/new_feature.py': 'VALUE=2\n',
                        'AGENTS.md': 'new source policy', '.codex/config.toml': 'new provider'})
        plan = self.plan()
        self.assertNotIn('AGENTS.md', plan['changes'])
        self.assertNotIn('.codex/config.toml', plan['changes'])
        u.apply_files(self.home, self.ident)
        self.assertEqual((self.home / 'bot.py').read_text(), 'print("new")\n')
        self.assertTrue((self.home / 'mods/new_feature.py').is_file())
        self.assertEqual((self.home / 'AGENTS.md').read_text(), 'source instructions\n')
        self.assertEqual((self.home / '.codex/config.toml').read_text(), '# provider\n')
        self.assert_private()
        self.assertEqual(u.baseline(self.home)['revision'], 'b' * 40)
        self.assertEqual((u.workspace(self.home) / 'backups' / self.ident).stat().st_mode & 0o777, 0o700)

    def test_local_conflict_refuses_every_live_write(self):
        (self.home / 'bot.py').write_text('print("owner change")\n')
        self.publish(**{'bot.py': 'print("upstream")\n'})
        with self.assertRaisesRegex(u.UpdateError, 'Конфликты: bot.py'):
            self.plan()
        self.assertEqual((self.home / 'bot.py').read_text(), 'print("owner change")\n')
        self.assert_private()

    def test_unchanged_upstream_preserves_local_modifications(self):
        (self.home / 'mods/bundled.py').write_text('OWNER=True\n')
        self.publish(**{'bot.py': 'print("new")\n'})
        self.assertNotIn('mods/bundled.py', self.plan()['changes'])
        u.apply_files(self.home, self.ident)
        self.assertEqual((self.home / 'mods/bundled.py').read_text(), 'OWNER=True\n')
        self.assertNotEqual(u.baseline(self.home)['files']['mods/bundled.py'], u.digest(b'OWNER=True\n'))

    def test_new_upstream_file_colliding_with_custom_mod_refuses(self):
        self.publish(**{'mods/custom.py': 'UPSTREAM=True\n'})
        with self.assertRaisesRegex(u.UpdateError, 'mods/custom.py'):
            self.plan()
        self.assert_private()

    def test_dependency_changes_and_file_removal_refused(self):
        self.publish(**{'requirements.txt': 'example==2\n'})
        with self.assertRaisesRegex(u.UpdateError, 'зависимости'):
            self.plan()
        self.publish(**{'requirements.txt': 'example==1\n'})
        del self.files['mods/bundled.py']
        (self.source / 'mods/bundled.py').unlink()
        self.publish()
        with self.assertRaisesRegex(u.UpdateError, 'удаляет'):
            self.plan()

    def plan_dependency_update(self):
        installed_manifest = self.home / 'MANIFEST.json'
        u.json_write(installed_manifest, dict(u.read_json(installed_manifest), version='0.9.0'))
        u.record_install(self.home)
        self.publish(**{'requirements.txt': '# offline dependency fixture\n',
                        'tools/start_guard.py': '# fixture\n'})
        self.stage.rmdir()
        settings = u.save_update_settings(self.home, channel='stable')
        release = {'version': '1.0.0', 'channel': 'stable'}

        def fetch(_release, stage):
            source = stage / 'source'
            shutil.copytree(self.source, source)
            manifest = u.read_json(source / 'MANIFEST.json')
            manifest.update(version='1.0.0', environment_update=1)
            u.json_write(source / 'MANIFEST.json', manifest)
            return source, 'b' * 40

        with patch.object(u, 'list_releases', return_value=[]), \
             patch.object(u, 'select_release', return_value=release), \
             patch.object(u, 'fetch_release_source', side_effect=fetch), \
             patch.object(u.uuid, 'uuid4', return_value=SimpleNamespace(hex=self.ident)):
            plan = u.prepare_release(self.home)
        self.assertEqual(plan['release_settings'], settings)
        self.assertIn('environment_manifest', plan)
        return plan

    def test_dependency_update_prepares_real_venv_and_recovers_after_interruption(self):
        from tools.start_guard import recover

        plan = self.plan_dependency_update()
        live = self.home / '.venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(live)], check=True)
        (live / 'local-marker').write_text('owner environment')
        old_identity = u.environment_identity(live)
        u.freeze_supervisor(self.stage)
        u.request(self.home, self.ident)
        environment = u.prepare_environment(self.home, self.stage, plan)
        self.assertEqual(u.environment_identity(live), old_identity)
        txn = u.apply_files(self.home, self.ident, environment=environment)
        self.assertEqual(u.environment_identity(live), environment[0]['new'])
        self.assertFalse((live / 'local-marker').exists())
        self.assertTrue(u.environment_held(self.home))
        snapshot = Path(txn['snapshot']['root'])
        self.assertEqual((snapshot / 'memory/notes.md').read_text(), 'owner memory')
        self.assertEqual((snapshot / '.venv/local-marker').read_text(), 'owner environment')
        self.assertEqual(snapshot.parent.stat().st_mode & 0o777, 0o700)
        self.assert_private()
        self.assertTrue(recover(self.home))
        self.assertEqual(u.environment_identity(live), old_identity)
        self.assertEqual((live / 'local-marker').read_text(), 'owner environment')
        self.assertEqual((self.home / 'requirements.txt').read_text(), 'example==1\n')
        self.assertFalse(u.environment_held(self.home))
        self.assertFalse(recover(self.home))
        self.assert_private()

    def test_dependency_build_failure_leaves_running_bot_unchanged(self):
        from legacy_migration import MigrationError

        self.plan_dependency_update()
        (self.home / '.venv').mkdir()
        u.request(self.home, self.ident)
        with patch.object(u, 'systemctl') as systemctl, \
             patch.object(u, 'service_matches', return_value=123), \
             patch.object(u, 'environment_service_matches'), patch.object(u, 'notify'), \
             patch('legacy_migration.build_staged_environment', side_effect=MigrationError('offline')):
            with self.assertRaisesRegex(u.UpdateError, 'offline'):
                u.worker(self.home, self.ident, 123)
        systemctl.assert_not_called()
        self.assertFalse((u.workspace(self.home) / 'transaction.json').exists())
        self.assertFalse(u.environment_work(self.home, self.ident).exists())
        self.assert_private()

    def test_dependency_update_refuses_modified_local_requirements(self):
        (self.home / 'requirements-voice.txt').write_text('local-voice==1\n')
        self.publish(**{'requirements.txt': 'example==2\n'})
        with self.assertRaisesRegex(u.UpdateError, 'местные правки'):
            u.plan_source(self.home, self.source, 'b' * 40, self.stage, migrate_dependencies=True)

    def test_late_edit_and_tampered_stage_refused(self):
        self.publish(**{'bot.py': 'print("new")\n'})
        self.plan()
        (self.home / 'bot.py').write_text('late owner edit')
        with self.assertRaisesRegex(u.UpdateError, 'изменился'):
            u.get_plan(self.home, self.ident)
        (self.home / 'bot.py').write_text(self.files['bot.py'].replace('new', 'old'))
        (self.stage / 'files/bot.py').write_text('corrupt')
        with self.assertRaisesRegex(u.UpdateError, 'повреждён'):
            u.get_plan(self.home, self.ident)

    def test_rollback_restores_code_and_removes_only_our_added_files(self):
        original = (self.home / u.STATE).read_bytes()
        self.publish(**{'bot.py': 'print("new")\n', 'mods/new.py': 'NEW=True\n'})
        self.plan()
        u.apply_files(self.home, self.ident)
        u.restore(self.home)
        self.assertEqual((self.home / 'bot.py').read_text(), 'print("old")\n')
        self.assertFalse((self.home / 'mods/new.py').exists())
        self.assertEqual((self.home / u.STATE).read_bytes(), original)
        self.assert_private()
        u.restore(self.home)  # Repeating recovery is safe.

    def test_interrupted_write_rolls_back_from_persistent_intent(self):
        self.publish(**{'bot.py': 'print("new")\n', 'mods/new.py': 'NEW=True\n'})
        self.plan()
        real_atomic = u.atomic
        def fail(path, data, mode=0o600):
            if path == self.home / 'mods/new.py':
                raise OSError('injected disk failure')
            return real_atomic(path, data, mode)
        with patch.object(u, 'atomic', side_effect=fail):
            with self.assertRaises(OSError):
                u.apply_files(self.home, self.ident)
        u.restore(self.home)
        self.assertEqual((self.home / 'bot.py').read_text(), 'print("old")\n')
        self.assert_private()

    def test_frozen_worker_recovers_boot_without_loading_updated_source(self):
        from tools.start_guard import recover

        self.publish(**{'bot.py': 'raise RuntimeError("unhealthy release")\n'})
        self.plan()
        u.freeze_supervisor(self.stage)
        u.apply_files(self.home, self.ident)
        self.assertTrue(recover(self.home))
        self.assertEqual((self.home / 'bot.py').read_text(), 'print("old")\n')
        self.assertEqual(u.read_json(u.workspace(self.home) / 'transaction.json')['status'], 'rolled_back')
        self.assertFalse(recover(self.home))
        self.assert_private()

    def test_low_disk_and_offline_fail_without_changing_live_files(self):
        self.publish(**{'bot.py': 'print("new")\n'})
        self.plan()
        with patch.object(u.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024)):
            with self.assertRaisesRegex(u.UpdateError, 'Недостаточно места'):
                u.apply_files(self.home, self.ident)
        self.assertFalse((u.workspace(self.home) / 'transaction.json').exists())
        with patch.object(u, 'download', side_effect=OSError('offline')):
            with self.assertRaisesRegex(OSError, 'offline'):
                u.prepare(self.home)
        self.assertEqual({path.name for path in self.stage.parent.iterdir()}, {self.ident})
        self.assertEqual((self.home / 'bot.py').read_text(), 'print("old")\n')
        self.assert_private()

    def test_downgrade_can_remove_only_unmodified_managed_files(self):
        del self.files['mods/bundled.py']
        (self.source / 'mods/bundled.py').unlink()
        self.publish()
        local = self.home / 'mods/bundled.py'
        local.write_text('OWNER_EDIT = True\n')
        with self.assertRaisesRegex(u.UpdateError, 'Конфликты'):
            u.plan_source(self.home, self.source, 'b' * 40, self.stage, allow_removals=True)
        local.write_text('VALUE=1\n')
        plan = u.plan_source(self.home, self.source, 'b' * 40, self.stage, allow_removals=True)
        plan['downgrade'] = True
        u.json_write(self.stage / 'plan.json', plan)
        with self.assertRaisesRegex(u.UpdateError, 'отдельное подтверждение'):
            u.request(self.home, self.ident)
        self.assertTrue(u.request(self.home, self.ident, allow_downgrade=True))
        u.apply_files(self.home, self.ident)
        self.assertFalse(local.exists())
        self.assertNotIn('mods/bundled.py', u.baseline(self.home)['files'])
        u.restore(self.home)
        self.assertEqual(local.read_text(), 'VALUE=1\n')
        self.assert_private()

    def test_rollback_does_not_overwrite_later_owner_edit(self):
        self.publish(**{'bot.py': 'print("new")\n'})
        self.plan()
        u.apply_files(self.home, self.ident)
        (self.home / 'bot.py').write_text('OWNER=True\n')
        with self.assertRaisesRegex(u.UpdateError, 'местные правки'):
            u.restore(self.home)
        self.assertEqual((self.home / 'bot.py').read_text(), 'OWNER=True\n')

    def test_symlinks_and_private_paths(self):
        for name in ('config.json', 'memory/notes.md', 'spool/state.py', '.codex/config.toml',
                     'AGENTS.md', 'templates/runtime/state.json', u.STATE):
            self.assertFalse(u.managed(name))
        for name in ('../escape', '/absolute', 'mods/../config.json', 'mods//bad'):
            with self.assertRaises(u.UpdateError):
                u.managed(name)
        (self.home / 'bot.py').unlink()
        external = self.root / 'outside'
        external.write_text('external')
        (self.home / 'bot.py').symlink_to(external)
        with self.assertRaisesRegex(u.UpdateError, 'ссылка'):
            self.plan()
        self.assertEqual(external.read_text(), 'external')

    def test_bad_archive_and_manifest(self):
        for name, kind in [('repo/../escape', tarfile.REGTYPE), ('repo/link', tarfile.SYMTYPE)]:
            archive = self.root / 'test.tgz'
            with tarfile.open(archive, 'w:gz') as tf:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.size = 1 if kind == tarfile.REGTYPE else 0
                tf.addfile(info, io.BytesIO(b'x') if info.size else None)
            with self.assertRaises(u.UpdateError):
                u.unpack(archive, self.root / 'out')
        (self.source / 'bot.py').write_text('corrupt')
        with self.assertRaisesRegex(u.UpdateError, 'сумма'):
            self.plan()

    def test_unknown_install_and_lock(self):
        unknown = self.root / 'unknown'
        unknown.mkdir()
        with self.assertRaisesRegex(u.UpdateError, 'Нет эталона'):
            u.baseline(unknown)
        with u.lock(self.home):
            with self.assertRaisesRegex(u.UpdateError, 'уже выполняется'):
                with u.lock(self.home):
                    pass

    def test_worker_unhealthy_rolls_back_and_restarts_old_service(self):
        self.publish(**{'bot.py': 'print("new")\n'})
        self.plan()
        calls = []
        with patch.object(u, 'systemctl', side_effect=lambda *a, **kw: calls.append(a)), \
             patch.object(u, 'service_matches', return_value=123), \
             patch.object(u, 'healthy', return_value=False), patch.object(u, 'notify') as notify:
            with self.assertRaisesRegex(u.UpdateError, 'проверку запуска'):
                u.worker(self.home, self.ident, 123)
        self.assertEqual(calls, [('stop', 'nook.service'), ('start', 'nook.service'),
                                 ('stop', 'nook.service'), ('start', 'nook.service')])
        self.assertIn('Старый код восстановлен', notify.call_args.args[1])
        self.assertEqual((self.home / 'bot.py').read_text(), 'print("old")\n')
        self.assert_private()

    def test_worker_success_and_stale_service_refused(self):
        self.publish(**{'bot.py': 'print("new")\n'})
        self.plan()
        with patch.object(u, 'systemctl'), patch.object(u, 'service_matches', return_value=123), \
             patch.object(u, 'healthy', return_value=True), patch.object(u, 'notify'):
            u.worker(self.home, self.ident, 123)
        self.assertEqual(u.read_json(u.workspace(self.home) / 'transaction.json')['status'], 'success')
        with patch.object(u, 'systemctl', return_value=SimpleNamespace(stdout='WorkingDirectory=/wrong\nMainPID=123\nActiveState=active')):
            with self.assertRaises(u.UpdateError):
                u.service_matches(self.home, 123)

    def test_restarted_worker_rolls_back_interrupted_update_before_start(self):
        self.publish(**{'bot.py': 'print("new")\n'})
        self.plan()
        u.apply_files(self.home, self.ident)
        calls = []

        def service(*args):
            calls.append(args)
            if args[0] == 'show':
                return SimpleNamespace(stdout=str(self.home) + '\n')
            if args[0] == 'start':
                self.assertEqual((self.home / 'bot.py').read_text(), 'print("old")\n')

        with patch.object(u, 'systemctl', side_effect=service), \
             patch.object(u, 'service_matches', return_value=123), patch.object(u, 'notify'):
            u.worker(self.home, self.ident, 123)
        self.assertEqual([call[0] for call in calls], ['show', 'stop', 'start'])
        self.assertEqual(u.read_json(u.workspace(self.home) / 'transaction.json')['status'], 'rolled_back')
        self.assert_private()

    def test_restarted_worker_starts_old_service_after_interrupted_stop(self):
        u.json_write(self.stage / 'stop-intent.json', {'id': self.ident})
        with patch.object(u, 'systemctl', return_value=SimpleNamespace(stdout=str(self.home) + '\n')) as command, \
             patch.object(u, 'service_matches', return_value=123), patch.object(u, 'notify'):
            u.worker(self.home, self.ident, 123)
        self.assertEqual([call.args[0] for call in command.call_args_list], ['show', 'start'])
        self.assertFalse((u.workspace(self.home) / 'transaction.json').exists())
        self.assert_private()

    def test_fresh_health_requires_matching_revision_pid_and_no_failed_mods(self):
        u.json_write(self.home / u.STATE, dict(u.baseline(self.home), revision='new'))
        u.heartbeat(self.home)
        with patch.object(u, 'service_matches', return_value=os.getpid()):
            self.assertTrue(u.healthy(self.home, 'new', 0, timeout=.01, stable=0))
            with patch.object(u.time, 'sleep'):
                self.assertFalse(u.healthy(self.home, 'wrong', 0, timeout=.01, stable=0))
        u.heartbeat(self.home, ['broken_mod'])
        with patch.object(u, 'service_matches', return_value=os.getpid()), patch.object(u.time, 'sleep'):
            self.assertFalse(u.healthy(self.home, 'new', 0, timeout=.01, stable=0))

    def test_offline_refuses_fresh_running_process(self):
        u.heartbeat(self.home)
        with self.assertRaisesRegex(u.UpdateError, 'Бот работает'):
            u.offline(self.home)

    def test_runtime_lock_blocks_source_update_and_double_start(self):
        with u.runtime_lock(self.home):
            with self.assertRaisesRegex(u.UpdateError, 'Бот уже работает'):
                with u.runtime_lock(self.home):
                    self.fail('second runtime lock was acquired')

    def test_incompatible_update_api_refused(self):
        manifest = u.read_json(self.source / 'MANIFEST.json')
        manifest['update_api'] = 2
        u.json_write(self.source / 'MANIFEST.json', manifest)
        with self.assertRaisesRegex(u.UpdateError, 'манифест'):
            self.plan()

    def test_existing_install_defaults_to_legacy_without_writing_settings(self):
        settings_path = u.workspace(self.home) / 'settings.json'
        self.assertEqual(u.update_settings(self.home)['channel'], None)
        self.assertFalse(settings_path.exists())
        settings = u.save_update_settings(self.home, channel='beta', version_pin='1.2.0-beta.1')
        self.assertEqual(settings['channel'], 'beta')
        self.assertEqual(settings['version_pin'], '1.2.0-beta.1')
        self.assertFalse(settings['paused'])
        self.assertEqual(u.save_update_settings(
            self.home, clear_version_pin=True, paused=True)['version_pin'], None)
        self.assertTrue(u.update_settings(self.home)['paused'])

    def test_update_settings_reject_invalid_values_and_symlink(self):
        with self.assertRaisesRegex(u.UpdateError, 'канал'):
            u.save_update_settings(self.home, channel='preview')
        with self.assertRaisesRegex(u.UpdateError, 'версия'):
            u.save_update_settings(self.home, version_pin='1.2.0-preview.1')
        path = u.workspace(self.home) / 'settings.json'
        path.symlink_to(self.root / 'outside')
        with self.assertRaisesRegex(u.UpdateError, 'ссылка'):
            u.update_settings(self.home)

    def test_release_tag_resolution_pins_annotated_tag_to_commit(self):
        commit = 'c' * 40
        responses = [
            {'object': {'type': 'tag', 'sha': 'a' * 40}},
            {'object': {'type': 'commit', 'sha': commit}},
        ]
        with patch.object(u, 'github_json', side_effect=responses) as fetch:
            self.assertEqual(u.release_commit('v1.2.0'), commit)
        self.assertIn('/git/ref/tags/v1.2.0', fetch.call_args_list[0].args[0])

    def test_tag_resolution_rejects_non_commit_and_bad_sha(self):
        with patch.object(u, 'github_json', return_value={'object': {'type': 'blob', 'sha': 'x'}}):
            with self.assertRaisesRegex(u.UpdateError, 'неизменяемый'):
                u.release_commit('v1.2.0')

    def test_release_asset_requires_github_digest_and_pinned_url(self):
        release = {'assets': [{
            'name': 'nook-1.2.0.zip',
            'digest': 'sha256:' + 'a' * 64,
            'browser_download_url': 'https://github.com/Sppqq/nook/releases/download/v1.2.0/nook-1.2.0.zip',
        }]}
        with patch.object(u.urllib.request, 'urlopen') as fetch:
            with self.assertRaisesRegex(u.UpdateError, 'SHA-256'):
                u.download_release_asset({'assets': [{
                    **release['assets'][0], 'digest': None}]}, self.root / 'archive.zip', '1.2.0')
            fetch.assert_not_called()
        with patch.object(u.urllib.request, 'urlopen') as fetch:
            with self.assertRaisesRegex(u.UpdateError, 'Ссылка'):
                u.download_release_asset({'assets': [{
                    **release['assets'][0], 'browser_download_url': 'https://evil.test/archive.zip'
                }]}, self.root / 'archive.zip', '1.2.0')
            fetch.assert_not_called()

    def test_release_asset_checks_bytes_and_redirect_host(self):
        payload = b'archive bytes'
        release = {'assets': [{
            'name': 'nook-1.2.0.zip',
            'digest': 'sha256:' + hashlib.sha256(payload).hexdigest(),
            'browser_download_url': 'https://github.com/Sppqq/nook/releases/download/v1.2.0/nook-1.2.0.zip',
        }]}

        class Response(io.BytesIO):
            def __init__(self, data, final_url):
                super().__init__(data)
                self.final_url = final_url
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
            def geturl(self):
                return self.final_url

        target = self.root / 'asset.zip'
        with patch.object(u.urllib.request, 'urlopen', return_value=Response(
                payload, 'https://release-assets.githubusercontent.com/download')) as fetch:
            u.download_release_asset(release, target, '1.2.0')
        self.assertEqual(target.read_bytes(), payload)
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, release['assets'][0]['browser_download_url'])
        with patch.object(u.urllib.request, 'urlopen', return_value=Response(
                payload, 'https://evil.test/download')):
            with self.assertRaisesRegex(u.UpdateError, 'недоверенный адрес'):
                u.download_release_asset(release, self.root / 'redirect.zip', '1.2.0')

    def test_release_update_selects_channel_and_checks_manifest_version(self):
        version = '1.2.0-beta.1'
        installed_manifest = u.read_json(self.home / 'MANIFEST.json')
        installed_manifest['version'] = '1.3.0-beta.1'
        u.json_write(self.home / 'MANIFEST.json', installed_manifest)
        u.record_install(self.home)
        manifest = {'format': 1, 'update_api': 1, 'version': version,
                    'sha256': {name: u.digest((self.source / name).read_bytes())
                               for name in self.files}}
        u.json_write(self.source / 'MANIFEST.json', manifest)
        archive = self.root / 'release.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            for path in self.source.rglob('*'):
                if path.is_file():
                    bundle.write(path, path.relative_to(self.source).as_posix())
            metadata = zipfile.ZipInfo('NOOK_RELEASE.json')
            metadata.external_attr = 0o100644 << 16
            bundle.writestr(metadata, json.dumps({'version': version, 'revision': 'c' * 40}))
        release = {'tag_name': 'v' + version, 'prerelease': True, 'draft': False,
                   'name': 'test beta', 'body': 'Test changes',
                   'assets': [{'name': 'nook-' + version + '.zip'}, {'name': 'provenance.jsonl'}]}
        u.save_update_settings(self.home, channel='beta')

        def asset_download(item, destination, selected, limit=None, *, name=None):
            self.assertEqual(selected, version)
            if name == 'provenance.jsonl':
                destination.write_text('test bundle')
            else:
                shutil.copyfile(archive, destination)

        with patch.object(u, 'list_releases', return_value=[release]), \
                patch.object(u, 'release_commit', return_value='c' * 40), \
                patch.object(u, 'download_release_asset', side_effect=asset_download), \
                patch.object(u, 'verify_release_provenance') as provenance:
            plan = u.prepare(self.home)
            provenance.assert_called_once()
            provenance.side_effect = u.UpdateError('signature rejected')
            with self.assertRaisesRegex(u.UpdateError, 'signature rejected'):
                u.prepare(self.home)
            self.assertEqual({path.name for path in (u.workspace(self.home) / 'staged').iterdir()},
                             {self.stage.name, plan['id']})
        self.assertEqual(plan['version'], version)
        self.assertEqual(plan['channel'], 'beta')
        self.assertEqual(plan['revision'], 'c' * 40)
        self.assertEqual(plan['release_name'], 'test beta')
        self.assertTrue(plan['downgrade'])
        self.assertEqual(plan['current_version'], '1.3.0-beta.1')
        u.save_update_settings(self.home, channel='stable')
        with self.assertRaisesRegex(u.UpdateError, 'канал изменился'):
            u.request(self.home, plan['id'])
        self.assertFalse((u.workspace(self.home) / 'request.json').exists())

    def test_release_archive_rejects_traversal(self):
        archive = self.root / 'unsafe.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('../escape', 'x')
        with self.assertRaises(u.UpdateError):
            u.unpack_zip(archive, self.root / 'unzipped')

    def test_rendering_helper_survives_installer_render(self):
        # The installer replaces placeholders even in Python source. The helper
        # must retain the ability to render the NEXT update for a custom target.
        original = Path(u.__file__).read_bytes()
        installed = u.rendered(original, self.home)
        namespace = {'__file__': str(self.home / 'nook_update.py'), '__name__': 'render_check'}
        exec(compile(installed, 'nook_update.py', 'exec'), namespace)
        marker = ('__NOOK_' + 'USER_HOME__').encode()
        self.assertEqual(namespace['rendered'](marker, self.home), str(Path.home()).encode())

    def test_launch_freezes_worker_in_independent_systemd_unit(self):
        self.publish(**{'bot.py': 'print("new")\n'})
        self.plan()
        with patch.object(u, 'service_matches', return_value=123), \
             patch.object(u.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            u.launch(self.home, self.ident, 123)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ['systemd-run', '--user', '--collect'])
        self.assertIn('--unit=nook-update-' + self.ident, args)
        self.assertEqual((self.stage / 'worker.py').read_bytes(), Path(u.__file__).read_bytes())
        self.assertIn(str(self.home), args)

    def test_legacy_public_install_conflicts_are_never_adopted_as_clean(self):
        (self.home / u.STATE).unlink()
        (self.home / 'bot.py').write_text('LEGACY_LOCAL_CHANGE=True\n')
        self.publish(**{'bot.py': 'print("new")\n'})
        with self.assertRaisesRegex(u.UpdateError, 'Конфликты'):
            self.plan()

    def test_prepare_fetches_pinned_snapshot(self):
        archive = self.root / 'upstream.tar.gz'
        self.publish(**{'bot.py': 'print("new")\n'})
        with tarfile.open(archive, 'w:gz') as tf:
            for p in self.source.rglob('*'):
                if p.is_file():
                    tf.add(p, arcname='nook-revision/' + str(p.relative_to(self.source)))
        urls = []
        def fetch(url, dest, limit):
            urls.append(url)
            if url.endswith('/commits/main'):
                u.json_write(dest, {'sha': 'c' * 40})
            else:
                dest.write_bytes(archive.read_bytes())
        with patch.object(u, 'download', side_effect=fetch):
            plan = u.prepare(self.home)
        self.assertEqual(plan['revision'], 'c' * 40)
        self.assertEqual(urls[-1], 'https://codeload.github.com/Sppqq/nook/tar.gz/' + 'c' * 40)
        self.assertIn('bot.py', plan['changes'])

    def test_real_release_render_update_and_recovery(self):
        # Exercise actual release files and installer rendering, not just a
        # miniature manifest; no provider login or live service is involved.
        repo = Path(u.__file__).parent
        manifest = u.read_json(repo / 'MANIFEST.json')
        deployed = self.root / 'custom-bot-home'
        upstream = self.root / 'next-release'
        for dest in (deployed, upstream):
            dest.mkdir()
            for name in set(manifest['sha256']) | {'MANIFEST.json'}:
                p = dest / name
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(repo / name, p)
        spec = importlib.util.spec_from_file_location('installer_for_update', repo / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        installer.render(deployed)
        u.record_install(deployed)
        clean_stage = u.workspace(deployed) / 'staged' / ('d' * 16)
        clean_stage.mkdir(parents=True)
        self.assertFalse(u.plan_source(deployed, upstream, 'e' * 40, clean_stage)['changes'])
        private = deployed / 'config.json'
        private.write_text('{"owner_id":123,"bot_token":"local-test-only"}')
        original_bot = (deployed / 'bot.py').read_bytes()
        bot = upstream / 'bot.py'
        bot.write_bytes(bot.read_bytes() + b'\n# Next source release for integration test.\n')
        manifest['sha256']['bot.py'] = u.digest(bot.read_bytes())
        u.json_write(upstream / 'MANIFEST.json', manifest)
        plan = u.plan_source(deployed, upstream, 'f' * 40, clean_stage)
        self.assertEqual(set(plan['changes']), {'bot.py', 'MANIFEST.json'})
        u.apply_files(deployed, 'd' * 16)
        self.assertNotEqual((deployed / 'bot.py').read_bytes(), original_bot)
        u.restore(deployed)
        self.assertEqual((deployed / 'bot.py').read_bytes(), original_bot)
        self.assertEqual(json.loads(private.read_text())['owner_id'], 123)


class UpdateModTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_waits_for_active_work_then_reserves_lanes(self):
        import mods
        from unittest.mock import AsyncMock
        spec = importlib.util.spec_from_file_location('self_update_mod', Path(u.__file__).parent / 'mods/self_update.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / 'mods').mkdir()
            mod.__file__ = str(home / 'mods/self_update.py')
            lanes = [SimpleNamespace(current=object(), waiting=0, lock=asyncio.Lock()),
                     SimpleNamespace(current=None, waiting=0, lock=asyncio.Lock())]
            core = SimpleNamespace(maintenance_lanes=lanes)
            m = mods.Mod('self_update', core)
            m.send = AsyncMock()
            mod.setup(m)
            u.json_write(u.workspace(home) / 'request.json', {'id': 'a' * 16})
            pending = m.periodic[0][1]
            def launch(*args):
                self.assertTrue(all(lane.lock.locked() for lane in lanes))
                raise u.UpdateError('test launch refusal')
            with patch.object(mod, 'get_plan', return_value=(None, {})), \
                 patch.object(mod, 'launch', side_effect=launch) as start:
                await pending()
                start.assert_not_called()
                lanes[0].current = None
                await pending()
                start.assert_called_once()
            self.assertFalse(any(lane.lock.locked() for lane in lanes))
            self.assertFalse((u.workspace(home) / 'request.json').exists())
            self.assertIn('update', m.commands)
            self.assertTrue(any(b[0] == 'Обновить Nook' for b in m.buttons))


if __name__ == '__main__':
    unittest.main()
