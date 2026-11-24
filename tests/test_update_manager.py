import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace

import mods

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('update_manager_mod', ROOT / 'mods/update_manager.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class UpdateManagerModTests(unittest.TestCase):
    def test_controls_register_once_and_use_owner_mod_contract(self):
        manager = mods.Mod('update_manager_test', SimpleNamespace())
        MODULE.setup(manager)
        expected = {'channel', 'version', 'releases', 'changelog', 'backup', 'rollback'}
        self.assertTrue(expected.issubset(manager.commands))
        self.assertEqual(len(manager.commands), len(set(manager.commands)))
        self.assertIn(('Обновления и резервные копии', 'open_menu', 'refresh', 'mods'),
                      manager.buttons)
        self.assertTrue({
            'choose-channel', 'set-channel', 'list-releases', 'pin-version',
            'unpin', 'check-release', 'install-update', 'confirm-downgrade',
            'set-auto', 'toggle-pause', 'backup', 'rollback-help',
        }.issubset(manager.callbacks))


if __name__ == '__main__':
    unittest.main()
