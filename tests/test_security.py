"""Regression checks for owner boundaries, token errors and installer inputs."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import urllib.error

from nook_peers import Peers

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bot_functions(*names, **namespace):
    tree = ast.parse((ROOT / 'bot.py').read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    env = {'Message': object, 'CallbackQuery': object, 'CommandObject': object, 'hmac': hmac, **namespace}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), env)
    return env


def message(uid=7, chat=7, kind='private'):
    return NS(from_user=NS(id=uid), chat=NS(id=chat, type=kind))


class OwnerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_owner_requires_private_matching_sender(self):
        env = bot_functions('private_sender', 'is_owner', STATE={'owner_id': 7})
        self.assertTrue(env['is_owner'](message()))
        for msg in (message(8, 8), message(7, -1, 'group'), message(7, 8), None):
            self.assertFalse(env['is_owner'](msg))

    async def test_group_inline_and_foreign_callback_cannot_dispatch(self):
        guests = []
        env = bot_functions('handle_button', STATE={'owner_id': 7}, is_guest=lambda uid: False,
                            guest_button=lambda cb: guests.append(cb))
        for msg, uid in ((NS(chat=NS(id=-1, type='group')), 7), (None, 7),
                         (NS(chat=NS(id=8, type='private')), 7),
                         (NS(chat=NS(id=8, type='private')), 8)):
            cb = NS(message=msg, from_user=NS(id=uid), data='rok:anything', answer=AsyncMock())
            await env['handle_button'](cb)
            cb.answer.assert_awaited_once()
        self.assertFalse(guests)

    async def test_pairing_missing_empty_wrong_code_and_group_fail_closed(self):
        for code, args, msg in ((None, '', message()), ('', '', message()),
                               ('secret-code', 'wrong', message()),
                               ('secret-code', 'secret-code', message(7, -1, 'group'))):
            state = {'owner_id': None}
            msg.answer = AsyncMock()
            env = bot_functions('private_sender', 'cmd_start', STATE=state, CFG={'pair_code': code})
            await env['cmd_start'](msg, NS(args=args))
            self.assertIsNone(state['owner_id'])


class InstallerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.mod = load('security_installer', ROOT / 'install.py')
        (self.home / 'install.sh').write_text('#!/bin/bash\n')
        self.manifest({'install.sh': hashlib.sha256((self.home/'install.sh').read_bytes()).hexdigest()})

    def manifest(self, hashes):
        (self.home / 'MANIFEST.json').write_text(json.dumps({'format': 1, 'sha256': hashes}))

    def test_unlisted_executable_source_refused(self):
        (self.home / 'mods').mkdir()
        (self.home / 'mods/injected.py').write_text('raise RuntimeError("must never run")')
        with self.assertRaisesRegex(SystemExit, 'Состав'):
            self.mod.validate(self.home)

    def test_manifest_traversal_and_parent_symlink_refused(self):
        self.manifest({'../outside': '0'*64})
        with self.assertRaisesRegex(SystemExit, 'Небезопасный'):
            self.mod.validate(self.home)
        (self.home / 'mods').symlink_to(ROOT / 'mods', target_is_directory=True)
        data = (ROOT / 'mods/peer_chat.py').read_bytes()
        self.manifest({'mods/peer_chat.py': hashlib.sha256(data).hexdigest()})
        with self.assertRaisesRegex(SystemExit, 'Ссылка'):
            self.mod.validate(self.home)

    def test_resume_service_rejects_unit_directive_in_target(self):
        argv = ['install.py', '--finish-service', '--target', str(self.home/'bad\nExecStart=bad')]
        with patch('sys.argv', argv), patch.object(self.mod.os, 'getuid', return_value=1001), \
                patch.object(self.mod, 'setup_service') as service, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'Путь'):
                self.mod.main()
            service.assert_not_called()


class TokenErrorTests(unittest.TestCase):
    def test_network_and_bad_json_errors_do_not_print_token_or_url(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            token = '123456:' + 'X'*35
            (home/'config.json').write_text(json.dumps({'bot_token': token}))
            for error in (urllib.error.URLError('https://api.telegram.org/bot'+token+'/getMe'),
                          ValueError('provider echoed '+token)):
                out, err = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, {'NOOK_HOME': name}), patch('sys.argv', ['tgapi', 'getMe']), \
                        patch('urllib.request.urlopen', side_effect=error), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as raised:
                        runpy.run_path(str(ROOT/'bin/tgapi'), run_name='__main__')
                rendered = out.getvalue()+err.getvalue()+str(raised.exception)
                self.assertNotIn(token, rendered)
                self.assertNotIn('https://api.telegram.org', rendered)
                self.assertNotIn('Traceback', rendered)

    def test_owner_api_empty_existing_token_is_rejected(self):
        mod = load('security_owner_api', ROOT/'mods/owner_api.py')
        with tempfile.TemporaryDirectory() as name:
            mod.ROOT = Path(name)
            (mod.ROOT/'token').write_text('')
            with self.assertRaisesRegex(ValueError, 'token'):
                mod.setup(NS())


class PeerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.a = Peers(Path(self.temp.name)/'a')
        self.b = Peers(Path(self.temp.name)/'b')
        key = 'ab'*32
        self.a.add('b', 'host', '/srv/nook', 'a', key)
        self.b.add('a', 'host', '/srv/nook', 'b', key)

    def test_signed_non_integer_timestamp_cannot_bypass_expiry(self):
        for value in (float('nan'), float('inf'), True, '0', None):
            env = self.a.envelope('b', 'test')
            env['body']['timestamp'] = value
            env['signature'] = self.a.signature(env['body'], 'ab'*32)
            with self.assertRaises(ValueError):
                self.b.receive(env)
        self.assertEqual(self.b.inbox(), [])

    def test_concurrent_receivers_cannot_exceed_quota(self):
        for i in range(59):
            self.b.receive(self.a.envelope('b', str(i)))
        def receive(env):
            try:
                self.b.receive(env)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(receive, [self.a.envelope('b', 'one'), self.a.envelope('b', 'two')]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.b.inbox()), 60)
        self.assertEqual((self.b.root/'messages.sqlite').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
