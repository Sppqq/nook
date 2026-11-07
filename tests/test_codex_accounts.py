"""Account changes use fake credentials and never touch the real Codex login."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        spec=importlib.util.spec_from_file_location('account_picker',ROOT/'mods/codex_accounts.py')
        self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.target=self.home/'.codex/auth.json';self.target.parent.mkdir();self.target.write_text('original')
        home=patch.object(self.mod.Path,'home',return_value=self.home);home.start();self.addCleanup(home.stop)
        self.calls=[];self.fail=False
        self.stored=[dict(number=1,account_key='first',active=True),dict(number=2,account_key='second',active=False)]
        async def cli(*args,**kw):
            self.calls.append(args)
            if args[0]=='list':return {'accounts':self.stored}
            if args[0]=='switch':
                self.target.write_text('second')
                if self.fail:raise RuntimeError('simulated failure')
                for a in self.stored:a['active']=a['number']==int(args[1])
            return {}
        self.mod.cli=cli
        self.core={'CODEX_LOGIN_LOCK':asyncio.Lock(),'user_lane':SimpleNamespace(current=None),'bg_lane':SimpleNamespace(current=None),
                   'LIMITS_CACHE':{'data':1,'at':1},'save_state':lambda:None}
        exec('async def login(chat): pass',self.core)
        test=self
        class Mod:
            def __init__(self):
                self.handlers={};self.tasks=[]
                self.core=SimpleNamespace(codex_login=test.core['login'],state={'main_thread':'merc-keep','fallback_until':100})
            def button(self,*a,**kw):return self.callback('switch')
            def callback(self,key):
                def register(fn):self.handlers[key]=fn;return fn
                return register
            def btn(self,*a):return a
            async def send(self,text):pass
        class Ctx:
            arg=test.mod.account_id(test.stored[1]);chat=123
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
        self.fail=True
        await self.m.handlers['select'](self.ctx)
        self.assertEqual(self.target.read_text(),'original')

    async def test_busy_blocks_login_and_switch(self):
        self.core['bg_lane'].current=object()
        await self.m.handlers['select'](self.ctx)
        await self.m.handlers['login'](self.ctx)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.m.tasks,[])

    async def test_login_preserves_gemini_thread(self):
        self.m.core.state['main_thread']='agy-keep'
        await self.m.handlers['login'](self.ctx)
        await asyncio.gather(*self.m.tasks)
        self.assertEqual(self.m.core.state['main_thread'],'agy-keep')
        self.assertTrue(any(c[0]=='import' for c in self.calls))
