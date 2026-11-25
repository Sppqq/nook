from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from extension_sandbox import ExtensionError, execute
import local_extensions as extensions
import mods


class StoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.store = extensions.Store(self.home)
        self.source = self.home / 'extension.py'
        self.policy = patch.object(extensions, 'approval', return_value=True)
        self.addCleanup(self.policy.stop)
        self.allowed = self.policy.start()

    def stage(self, version, data=b'fixture'):
        self.source.write_bytes(data)
        return self.store.stage('tool', 'echo', version, self.source)

    def test_drafts_cannot_run_and_versions_have_immutable_addresses(self):
        rev = self.stage('1.0.0')
        self.assertEqual(self.stage('1.0.0'), rev)
        with self.assertRaisesRegex(ExtensionError, 'другой исходник'):
            self.stage('1.0.0', b'different')
        with self.assertRaisesRegex(ExtensionError, 'Активной версии нет'):
            self.store.run('tool', 'echo', {})
        with self.assertRaisesRegex(ExtensionError, 'Нужны пройденная'):
            self.store.activate('tool', 'echo', '1.0.0')
        self.assertEqual(rev, hashlib.sha256(b'fixture').hexdigest())

    def test_concurrent_staging_keeps_both_versions(self):
        self.source.write_bytes(b'fixture')
        with ThreadPoolExecutor(2) as workers:
            list(workers.map(lambda version: self.store.stage('mod', 'hello', version, self.source),
                             ('1.0.0', '2.0.0')))
        self.assertEqual(set(self.store.status('mod', 'hello')['versions']), {'1.0.0', '2.0.0'})

    def test_failed_runtime_rolls_back_without_repeating_the_call(self):
        self.stage('1.0.0')
        self.stage('2.0.0', b'next')

        def runner(data, payload):
            if payload == {'healthcheck': True}:
                return {'ok': True}
            if data == b'next':
                raise ExtensionError('simulated crash')
            return payload

        with patch.object(extensions, 'execute', side_effect=runner) as backend:
            for version in ('1.0.0', '2.0.0'):
                self.store.test('tool', 'echo', version)
                self.store.activate('tool', 'echo', version)
            self.allowed.return_value = False
            with self.assertRaisesRegex(ExtensionError, 'не разрешил'):
                self.store.run('tool', 'echo', {})
            self.allowed.return_value = True
            with self.assertRaisesRegex(ExtensionError, 'Выполнен откат'):
                self.store.run('tool', 'echo', {'once': True})
            state = self.store.status('tool', 'echo')
            self.assertEqual(state['active'], '1.0.0')
            self.assertEqual(state['history'][-1]['event'], 'rolled_back')
            self.assertNotIn(unittest.mock.call(b'fixture', {'once': True}), backend.call_args_list)
            self.assertEqual(self.store.run('tool', 'echo', {'value': 4}), {'value': 4})

    def test_skill_stays_data_and_can_be_disabled(self):
        self.source.write_text('Example local skill')
        self.store.stage('skill', 'guide', '1.0.0', self.source)
        self.store.test('skill', 'guide', '1.0.0')
        self.store.activate('skill', 'guide', '1.0.0')
        self.assertEqual(self.store.run('skill', 'guide', {}), {'text': 'Example local skill'})
        self.assertIsNone(self.store.rollback('skill', 'guide'))
        self.assertIsNone(self.store.status('skill', 'guide')['active'])

    def test_malformed_or_modified_artifact_is_not_executed(self):
        rev = self.stage('1.0.0')
        (self.home / 'spool/local-extensions/tool-echo' / rev).write_bytes(b'modified')
        with self.assertRaisesRegex(ExtensionError, 'изменён'):
            self.store.test('tool', 'echo', '1.0.0')
        with self.assertRaises(ExtensionError):
            self.store.status('tool', '../outside')
        with self.assertRaises(ExtensionError):
            self.store.run('tool', 'echo', {'value': float('nan')})

    def test_trusted_registry_imports_only_the_approved_bytes_when_pinned(self):
        self.source.write_text('def setup(mod): pass\n')
        approved = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.source.rename(self.home / 'echo.py')
        with patch.object(mods, 'MODS_DIR', str(self.home)), \
                patch.object(extensions, 'read_policy', return_value={'allow': {}, 'legacy': {'echo': approved}}):
            self.assertEqual(mods.Registry(object()).load_one('echo').name, 'echo')
            (self.home / 'echo.py').write_text("raise AssertionError('must never execute')\n")
            with self.assertRaisesRegex(ExtensionError, 'не разрешён'):
                mods.Registry(object()).load_one('echo')


class LinuxSandboxTests(unittest.TestCase):
    def test_isolation_denies_environment_host_files_and_host_loopback(self):
        probe = b'NOOK_EXTENSION_API = 1\ndef run(payload): return payload\n'
        try:
            self.assertEqual(execute(probe, {'ready': True}), {'ready': True})
        except ExtensionError as error:
            if os.environ.get('NOOK_REQUIRE_SANDBOX') == '1':
                raise
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            private = Path(directory) / 'private'
            private.write_text('fixture-only')
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            source = b"""
import os
import socket
NOOK_EXTENSION_API = 1
def run(payload):
    connected = False
    try:
        with socket.create_connection(('127.0.0.1', payload['port']), .2):
            connected = True
    except OSError:
        pass
    with open('/tmp/fixture', 'w') as f:
        f.write('allowed')
    return {'host_file': os.path.exists(payload['file']), 'host_loopback': connected,
            'secret_env': 'NOOK_FIXTURE_SECRET' in os.environ,
            'home': os.path.exists('/home'), 'etc': os.path.exists('/etc')}
"""
            with patch.dict(os.environ, {'NOOK_FIXTURE_SECRET': 'fixture-only'}):
                response = execute(source, {'file': str(private), 'port': listener.getsockname()[1]})
            self.assertEqual(response, dict.fromkeys(('host_file', 'host_loopback', 'secret_env', 'home', 'etc'), False))
        with self.assertRaises(ExtensionError):
            execute(b'NOOK_EXTENSION_API = 2\ndef run(payload): return payload\n', {})

    def test_owner_moves_data_only_behavior_and_can_roll_back(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = extensions.Store(home)
            saved = home / 'legacy_state.json'
            original = '{"title":"Owner reminder","count":2}'
            saved.write_text(original)
            payload = {'title': 'Owner reminder', 'count': 2}
            expected = {'label': 'Owner reminder ×2'}
            source = home / 'formatter.py'
            body = """\
NOOK_EXTENSION_API = 1

def run(payload):
    if payload == {'healthcheck': True}:
        return {'ok': True}
    return {'label': '{} ×{}'.format(payload['title'], payload['count'])}
"""
            source.write_text(body)
            with patch.object(extensions, 'approval', return_value=False) as approved:
                store.stage('mod', 'formatter', '1.0.0', source)
                try:
                    store.test('mod', 'formatter', '1.0.0')
                except ExtensionError as error:
                    if os.environ.get('NOOK_REQUIRE_SANDBOX') == '1':
                        raise
                    self.skipTest(str(error))
                with self.assertRaisesRegex(ExtensionError, 'разрешение владельца'):
                    store.activate('mod', 'formatter', '1.0.0')

                approved.return_value = True
                store.activate('mod', 'formatter', '1.0.0')
                self.assertEqual(store.run('mod', 'formatter', payload), expected)
                source.write_text(body.replace("'{} ×{}'", "'{} ({})'"))
                store.stage('mod', 'formatter', '1.0.1', source)
                store.test('mod', 'formatter', '1.0.1')
                store.activate('mod', 'formatter', '1.0.1')
                self.assertEqual(store.run('mod', 'formatter', payload), {'label': 'Owner reminder (2)'})
                self.assertEqual(store.rollback('mod', 'formatter'), '1.0.0')
                self.assertEqual(store.run('mod', 'formatter', payload), expected)
                self.assertEqual(saved.read_text(), original)
