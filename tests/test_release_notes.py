import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release_notes', ROOT / 'tools/release_notes.py')
NOTES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NOTES)


class ReleaseNotesTests(unittest.TestCase):
    def test_release_notes_include_channel_compatibility_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'VERSION').write_text('0.1.0\n')
            (root / 'CHANGELOG.md').write_text((ROOT / 'CHANGELOG.md').read_text())
            (root / 'MANIFEST.json').write_text(json.dumps(
                {'version': '0.1.0', 'update_api': 1}))
            with patch.object(NOTES, 'ROOT', root):
                text = NOTES.render('v0.1.0')
        self.assertIn('**Channel:** Stable', text)
        self.assertIn('## Compatibility and migration', text)
        self.assertIn('## Rollback', text)
        self.assertIn('provenance.jsonl', text)

    def test_tag_and_version_must_match(self):
        with self.assertRaisesRegex(ValueError, 'start with v'):
            NOTES.render('9.9.9')


if __name__ == '__main__':
    unittest.main()
