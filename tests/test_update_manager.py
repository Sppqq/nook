import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


class LocalizedUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_english_dependency_downgrade_keeps_separate_confirmation(self):
        manager = mods.Mod('update_manager_test', SimpleNamespace(state={'ui_language': 'en'}, btn=lambda text, data, icon, style: (text, data)))
        MODULE.setup(manager)
        ctx = SimpleNamespace(reply=AsyncMock(), toast=AsyncMock())
        settings = {'channel': 'beta', 'version_pin': '0.1.2-beta.1', 'auto_mode': 'off', 'paused': False}
        plan = {'changes': ['bot.py'], 'id': 'plan', 'version': '0.1.2-beta.1',
                'current_version': '0.1.3', 'environment_manifest': {'requirements': []}, 'downgrade': True}
        with patch.object(MODULE.nook_update, 'update_settings', return_value=settings), \
             patch.object(MODULE.nook_update, 'prepare', return_value=plan):
            await manager.callbacks['check-release'](ctx)
        text, buttons = ctx.reply.await_args.args
        self.assertIn('full local backup', text)
        self.assertIn('Downgrade from `0.1.3` to `0.1.2-beta.1`', text)
        self.assertIn('I understand the downgrade risk', str(buttons))
        self.assertIn('confirm-downgrade:plan', str(buttons))


if __name__ == '__main__':
    unittest.main()
