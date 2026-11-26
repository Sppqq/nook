"""Account changes use fake credentials and never touch the real Codex login."""
import asyncio
import importlib.util
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        spec=importlib.util.spec_from_file_location('account_picker',ROOT/'mods/codex_accounts.py')
        self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
        self.real_cli = self.mod.cli
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.target = self.home / '.codex/auth.json'
        self.target.parent.mkdir(mode=0o700)
        self.target.write_text('original')
        self.target.chmod(0o600)
        self.registry = self.target.parent / 'accounts/registry.json'
        self.registry.parent.mkdir(mode=0o700)
        self.registry.write_text('original-index')
        self.registry.chmod(0o600)
        home=patch.object(self.mod.Path,'home',return_value=self.home);home.start();self.addCleanup(home.stop)
        self.calls=[];self.switch_fails=False
        self.stored=[dict(number=1,account_key='first',active=True),dict(number=2,account_key='second',active=False)]
        async def cli(*args,**kw):
            self.calls.append(args)
            if args[0]=='list':return {'accounts':self.stored}
            if args[0]=='switch':
                self.target.write_text('second')
                if self.switch_fails:raise RuntimeError('simulated failure')
                for a in self.stored:a['active']=a['number']==int(args[1])
            return {}
        self.mod.cli=cli
        self.core={'CODEX_LOGIN_LOCK':asyncio.Lock(),'user_lane':SimpleNamespace(current=None),'bg_lane':SimpleNamespace(current=None),
                   'LIMITS_CACHE':{'data':1,'at':1},'save_state':lambda:None, 'win_name':lambda n, *, english=False: f'{n // 60} h' if english else f'{n} min'}
        self.core.update(login_succeeds=True, login_path=self.target)
        exec('''async def login(chat, *, after_login):
    async with CODEX_LOGIN_LOCK:
        if login_succeeds:
            await after_login(login_path)
''', self.core)
        test=self
        class Mod:
            def __init__(self):
                self.handlers={};self.tasks=[];self.english=False
                self.core=SimpleNamespace(codex_login=test.core['login'],codex_pool_runtime=SimpleNamespace(busy=set()),state={'main_thread':'merc-keep','fallback_until':100})
            def button(self,*a,**kw):return self.callback('switch')
            def callback(self,key):
                def register(fn):self.handlers[key]=fn;return fn
                return register
            def btn(self,*a):return a
            def text(self, russian, english):return english if self.english else russian
            async def send(self,text):pass
        class Ctx:
            arg=test.mod.account_id(test.stored[1]);chat=123;cb=None
            async def toast(self,*a):pass
            async def reply(self,*a):pass
            edit=reply
        self.m=Mod();self.ctx=Ctx();self.mod.setup(self.m)

    async def test_switch_preserves_other_provider_and_resets_limits(self):
        await self.m.handlers['select'](self.ctx)
        self.assertEqual(self.target.read_text(),'second')
        self.assertEqual(self.target.stat().st_mode&0o777,0o600)
        self.assertEqual(self.m.core.state['main_thread'],'merc-keep')
        self.assertEqual(self.core['LIMITS_CACHE'],{'data':None,'at':0.0})

    async def test_failed_switch_restores_previous_login(self):
        self.switch_fails=True
        await self.m.handlers['select'](self.ctx)
        self.assertEqual(self.target.read_text(),'original')

    async def test_cancelled_switch_restores_previous_login(self):
        started = asyncio.Event()
        real_cli = self.mod.cli

        async def cli(*args, **kwargs):
            if args[0] == 'switch':
                self.target.write_text('partially replaced')
                self.registry.write_text('partially switched')
                started.set()
                await asyncio.Future()
            return await real_cli(*args, **kwargs)

        self.mod.cli = cli
        task = asyncio.create_task(self.m.handlers['select'](self.ctx))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.target.read_text(), 'original')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.registry.read_text(), 'original-index')
        self.assertEqual(self.m.core.state['fallback_until'], 100)

    async def test_cancelled_cli_stops_the_wrapper_and_its_child(self):
        helper = self.home / 'codex-auth'
        helper.write_text(
            '#!/bin/sh\ncd "$(dirname "$0")" || exit 1\n'
            'sleep 3600 &\nprintf "%s %s\\n" "$$" "$!" > ready.tmp\n'
            'mv ready.tmp ready\nwait\n'
        )
        helper.chmod(0o700)
        group = None
        handle_ready = asyncio.Event()
        spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            await handle_ready.wait()
            return process

        with (patch.object(self.mod.shutil, 'which', return_value=str(helper)),
              patch.object(self.mod.asyncio, 'create_subprocess_exec', delayed_spawn)):
            task = asyncio.create_task(self.real_cli('switch'))
            try:
                async with asyncio.timeout(3):
                    while not (self.home / 'ready').exists():
                        await asyncio.sleep(0.01)
                group, child = map(int, (self.home / 'ready').read_text().split())
                task.cancel()
                await asyncio.sleep(0)
                handle_ready.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 3)
                child_status = Path(f'/proc/{child}/status')
                async with asyncio.timeout(3):
                    while True:
                        try:
                            status = child_status.read_text()
                        except (FileNotFoundError, ProcessLookupError):
                            break
                        if any(line.split()[1] == 'Z' for line in status.splitlines()
                               if line.startswith('State:')):
                            break
                        await asyncio.sleep(0.01)
                group = None
            finally:
                handle_ready.set()
                if group is not None:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_busy_blocks_login_and_switch(self):
        self.core['bg_lane'].current=object()
        await self.m.handlers['select'](self.ctx)
        await self.m.handlers['login'](self.ctx)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.m.tasks,[])

    async def test_pool_probe_blocks_manual_credential_changes(self):
        self.m.core.codex_pool_runtime.busy.add('probe')
        for action in ('limits', 'select', 'login'):
            await self.m.handlers[action](self.ctx)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.m.tasks, [])

    async def test_login_preserves_gemini_thread(self):
        self.m.core.state['main_thread']='agy-keep'
        await self.m.handlers['login'](self.ctx)
        await asyncio.gather(*self.m.tasks)
        self.assertEqual(self.m.core.state['main_thread'],'agy-keep')
        self.assertTrue(any(c[0]=='import' for c in self.calls))

    async def test_unsuccessful_device_login_never_imports_previous_account(self):
        self.core['login_succeeds'] = False
        await self.m.handlers['login'](self.ctx)
        await asyncio.gather(*self.m.tasks)
        self.assertFalse(any(call[0] == 'import' for call in self.calls))
        self.assertEqual(self.core['LIMITS_CACHE'], {'data': 1, 'at': 1})

    async def test_quota_screen_fetches_account_usage_explicitly(self):
        self.stored[0]['usage'] = {
            'source': 'api', 'refresh': {'status': 'ok'},
            'primary': {'used_percent': 42, 'window_minutes': 300},
        }
        messages = []
        async def edit(text, buttons):
            messages.append(text)
            self.assertEqual(buttons[0][0][1], 'open')
        self.ctx.edit = edit
        await self.m.handlers['limits'](self.ctx)
        self.assertIn(('list', '--api', '--json'), self.calls)
        self.assertIn('300 min: **42%**', messages[0])

    async def test_account_picker_and_quota_follow_owner_language(self):
        self.m.english = True
        self.stored[0]['usage'] = {
            'source': 'api', 'refresh': {'status': 'ok'},
            'primary': {'used_percent': 42, 'window_minutes': 300},
        }
        messages = []

        async def reply(text, buttons=None):
            messages.append((text, buttons))

        self.ctx.cb = object()
        self.ctx.edit = reply
        await self.m.handlers['switch'](self.ctx)
        self.assertIn('Choose a Codex account', messages[0][0])
        self.assertEqual(messages[0][1][0][0][0], '✅ Account 1')
        await self.m.handlers['limits'](self.ctx)
        self.assertIn('5 h: **42%** · resets unknown', messages[1][0])
