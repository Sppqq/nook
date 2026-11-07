"""Update preservation, conflicts, transaction failure and independent recovery."""
import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
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
            with patch.object(mod, 'get_plan'), patch.object(mod, 'launch', side_effect=launch) as start:
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
