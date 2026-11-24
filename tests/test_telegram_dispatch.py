"""Run actual aiogram dispatch and installed Nook callbacks without Telegram I/O."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Update

from nook_update import update_settings

ROOT = Path(__file__).resolve().parents[1]


class TelegramDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.home = Path(work.name)
        (self.home / 'config.json').write_text(json.dumps({'bot_token': '123456:' + 'X' * 35, 'owner_id': 7}))
        sandbox = {'__file__': str(self.home / 'bot.py'), '__name__': 'disposable_nook'}
        exec(compile((ROOT / 'bot.py').read_text(), str(ROOT / 'bot.py'), 'exec'), sandbox)
        self.runtime = sandbox
        self.addAsyncCleanup(self.runtime['bot'].session.close)
        mod_path = self.home / 'mods' / 'update_manager.py'
        mod_path.parent.mkdir()
        shutil.copyfile(ROOT / 'mods/update_manager.py', mod_path)
        spec = importlib.util.spec_from_file_location('disposable_update_manager', mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        registry = self.runtime['REG']
        instance = self.runtime['modsys'].Mod('update_manager', registry.core)
        mod.setup(instance)
        registry.mods['update_manager'] = instance
        registry.core.edit = AsyncMock(return_value=True)
        registry.core.send = AsyncMock()

    async def dispatch(self, data, sender=7, chat=7, kind='private'):
        update = Update.model_validate({
            'update_id': 1, 'callback_query': {
                'id': 'qa', 'from': {'id': sender, 'is_bot': False, 'first_name': 'Nook QA'},
                'chat_instance': 'qa', 'data': data, 'message': {
                    'message_id': 50, 'date': 1700000000, 'chat': {'id': chat, 'type': kind}, 'text': 'Nook',
                },
            },
        })
        with patch.object(CallbackQuery, 'answer', new_callable=AsyncMock):
            await self.runtime['dp'].feed_update(self.runtime['bot'], update)

    async def test_owner_channel_confirmation_and_empty_release_list(self):
        await self.dispatch('mod:update_manager:choose-channel:beta')
        self.assertIsNone(update_settings(self.home)['channel'])
        await self.dispatch('mod:update_manager:set-channel:beta')
        self.assertEqual(update_settings(self.home)['channel'], 'beta')
        self.assertEqual(update_settings(self.home)['auto_mode'], 'off')
        with patch('nook_update.list_releases', return_value=[]):
            await self.dispatch('mod:update_manager:list-releases')
        self.assertIn('нет опубликованных версий', str(self.runtime['REG'].core.send.call_args))
        self.assertFalse((self.home / 'spool/updates/request.json').exists())

    async def test_foreign_and_group_callbacks_cannot_change_channel(self):
        for sender, chat, kind in ((8, 8, 'private'), (7, 8, 'private'), (7, -1007, 'group')):
            await self.dispatch('mod:update_manager:set-channel:nightly', sender, chat, kind)
        self.assertIsNone(update_settings(self.home)['channel'])
        self.runtime['REG'].core.edit.assert_not_awaited()
