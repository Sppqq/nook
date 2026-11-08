import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from tools import github_setup as g

ROOT = Path(__file__).resolve().parents[1]


class GitHubTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / 'config.json').write_text('{"owner_id":7,"model":"codex"}\n')
        (self.home / 'secrets.env').write_text('OTHER=preserve\nGITHUB_PERSONAL_ACCESS_TOKEN=old\n')
        self.output = io.StringIO()
        cm = contextlib.redirect_stdout(self.output)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def connect(self, mode='agent', user=None, error=None):
        opener = g.urllib.request.build_opener()
        result = io.BytesIO(json.dumps(user or {'login': 'test-agent', 'id': 123}).encode())
        with patch.object(g.getpass, 'getpass', return_value='test_secret_value'), \
             patch.object(g.urllib.request, 'build_opener', return_value=opener), \
             patch.object(opener, 'open', return_value=result, side_effect=error):
            return g.setup_github(self.home, mode, expected='test-agent')

    def test_verified_identity_and_private_files(self):
        self.assertTrue(self.connect())
        cfg = json.loads((self.home / 'config.json').read_text())
        self.assertEqual(cfg['owner_id'], 7)
        self.assertEqual(cfg['github'], {'mode': 'agent', 'login': 'test-agent', 'id': 123,
                         'email': '123+test-agent@users.noreply.github.com'})
        self.assertEqual(g.read_token(self.home), 'test_secret_value')
        self.assertIn('OTHER=preserve', (self.home / 'secrets.env').read_text())
        for filename in ('config.json', 'secrets.env'):
            self.assertEqual((self.home / filename).stat().st_mode & 0o777, 0o600)
        self.assertNotIn('test_secret_value', self.output.getvalue())
        self.assertTrue(self.connect(mode='owner'))
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['github']['mode'], 'owner')
        self.assertEqual((self.home / 'secrets.env').read_text().count('GITHUB_PERSONAL_ACCESS_TOKEN='), 1)

    def test_wrong_account_and_network_error_preserve_existing(self):
        before = {p: p.read_bytes() for p in self.home.iterdir()}
        self.assertFalse(self.connect(user={'login': 'owner', 'id': 456}))
        self.assertFalse(self.connect(error=urllib.error.HTTPError('url', 401, 'denied', {}, None)))
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.iterdir()})
        self.assertNotIn('test_secret_value', self.output.getvalue())

    def test_skip_does_not_disconnect(self):
        self.assertTrue(self.connect())
        before = {p: p.read_bytes() for p in self.home.iterdir()}
        self.assertFalse(g.setup_github(self.home, 'skip'))
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.iterdir()})

    def test_git_identity_and_credentials_are_local(self):
        self.assertTrue(self.connect())
        for directory in ('bin', 'tools'):
            (self.home / directory).mkdir()
        for name in ('github-git', 'github-credential'):
            shutil.copy2(ROOT / 'bin' / name, self.home / 'bin' / name)
        shutil.copy2(ROOT / 'tools/github_setup.py', self.home / 'tools/github_setup.py')
        env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL=str(self.home / 'global.gitconfig'),
                   GIT_AUTHOR_NAME='Owner', GIT_AUTHOR_EMAIL='owner@example.test')
        wrapper = self.home / 'bin/github-git'
        def run(*args, **kwargs):
            return subprocess.run([str(wrapper), *args], env=env, cwd=self.home,
                                  check=True, capture_output=True, text=True, **kwargs).stdout.strip()
        run('init', '-q', 'repo')
        run('-C', 'repo', 'commit', '--allow-empty', '-m', 'Identity check')
        self.assertEqual(run('-C', 'repo', 'log', '-1', '--format=%an <%ae>'),
                         'test-agent <123+test-agent@users.noreply.github.com>')
        self.assertFalse((self.home / 'global.gitconfig').exists())
        self.assertFalse((self.home / '.git-credentials').exists())
        helper = self.home / 'bin/github-credential'
        def credential(host, action='get'):
            return subprocess.run([str(helper), action], input=f'protocol=https\nhost={host}\n\n',
                                  text=True, capture_output=True, check=True).stdout
        self.assertIn('password=test_secret_value', credential('github.com'))
        self.assertEqual(credential('github.com.example.test'), '')
        self.assertEqual(credential('github.com', 'store'), '')
        self.assertNotIn('test_secret_value', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
