import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT / 'tools/build_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, name, value='example'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def test_private_runtime_even_if_tracked(self):
        for name in ('secrets.env', 'config.json', 'memory/notes.md', 'keys/id', '.codex/auth.json', 'spool/tasks.json'):
            self.put(name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.validate_file(self.root, name)

    def test_symlink_and_traversal(self):
        self.put('actual')
        (self.root / 'linked').symlink_to(self.root / 'actual')
        for name in ('linked', '../outside', '/absolute'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.validate_file(self.root, name)

    def test_credentials_and_backups(self):
        samples = {'bot.py.bak': 'old code', 'token.txt': '123456:' + 'Z' * 35,
                   'key.txt': '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----'}
        for name, value in samples.items():
            self.put(name, value)
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.validate_file(self.root, name)

    def test_templates_are_public(self):
        self.put('templates/runtime/nodes.json', '{}\n')
        self.assertEqual(release.validate_file(self.root, 'templates/runtime/nodes.json'), b'{}\n')

    def test_python_sources_parse(self):
        import ast
        for path in ROOT.rglob('*.py'):
            if '.git' not in path.parts and 'dist' not in path.parts:
                with self.subTest(path=str(path.relative_to(ROOT))):
                    ast.parse(path.read_text())
