"""Provider routing, keyboard behavior and real streaming CLI lifecycle."""
import ast
import asyncio
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import signal
import tempfile
from types import SimpleNamespace
import unittest
import uuid
import time

ROOT = Path(__file__).resolve().parents[1]

def functions(names, **values):
    ns = dict(globals(), **values)
    tree = ast.parse((ROOT / 'bot.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<provider-functions>', 'exec'), ns)
    return ns

class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / 'spool').mkdir()
        cli = self.home / 'fake-cli'
        cli.write_text('''#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
args=sys.argv[1:]
prompt=args[args.index('-p')+1]
if prompt == 'hang': time.sleep(60)
if prompt == 'overload':
    Path(args[args.index('--log-file')+1]).write_text('No capacity available for model gemini-example-high')
    time.sleep(60)
print(json.dumps({'event':'init','conversation_id':'conversation-example'}), flush=True)
print(json.dumps({'event':'step_update','step_update':{'step_type':'tool','state':'ACTIVE','tool_name':'shell','arguments':'secret-value'}}), flush=True)
if prompt == 'wait': time.sleep(60)
resumed='--conversation' in args and args[args.index('--conversation')+1]=='conversation-example'
print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'resumed' if resumed else 'fresh','usage':{'total_tokens':12}}}), flush=True)
''')
        cli.chmod(0o700)
        self.state = {}
        self.run = SimpleNamespace(proc=None, cancelled=False)
        self.ns = functions({'antigravity_result','antigravity_capacity_error','wait_antigravity','run_antigravity','_stream_antigravity'},
            Run=lambda:self.run, USER_TIMEOUT=5, CFG={'model':'antigravity-flash38','effort':'high'}, STATE=self.state,
            RUN_THREADS={}, AGY_BIN=str(cli), HOME=str(self.home), codex_env=lambda *a:os.environ.copy(), save_state=lambda:None, log=logging.getLogger('test'))

    async def test_resume_progress_usage_and_run_cleanup(self):
        steps=[]
        async def on_step(text): steps.append(text)
        tid, text, err = await self.ns['run_antigravity']('hello', on_step=on_step)
        self.assertEqual((text,err),('fresh',None))
        self.assertEqual(self.state['agy_threads'][tid], 'conversation-example')
        self.assertTrue(steps)
        self.assertNotIn('secret-value', ''.join(steps))
        self.assertEqual(await self.ns['run_antigravity']('hello', tid), (tid,'resumed',None))
        self.assertEqual(self.state['agy_usage']['tasks'],2)
        self.assertEqual(self.state['agy_usage']['tokens'],24)
        self.assertEqual(self.ns['RUN_THREADS'],{})

    async def test_progress_failure_reaps_child(self):
        async def broken_progress(text): raise RuntimeError('test')
        _, _, err = await self.ns['run_antigravity']('wait', on_step=broken_progress)
        self.assertIn('не удалось выполнить запрос',err)
        self.assertIsNotNone(self.run.proc.returncode)
        self.assertEqual(self.ns['RUN_THREADS'],{})

    async def test_stream_timeout_reaps_child(self):
        _, text, err = await self.ns['run_antigravity']('hang', timeout=.1)
        self.assertIn('Gemini не отвечает',err)
        self.assertEqual(text,'')
        self.assertIsNotNone(self.run.proc.returncode)
        self.assertEqual(self.ns['RUN_THREADS'],{})

    async def test_stream_overload_reaps_child(self):
        _, _, err = await self.ns['run_antigravity']('overload', timeout=5)
        self.assertIn('Gemini перегружен',err)
        self.assertIsNotNone(self.run.proc.returncode)

    async def test_stream_cancellation_reaps_child(self):
        task=asyncio.create_task(self.ns['run_antigravity']('wait'))
        while not self.state.get('agy_threads'): await asyncio.sleep(.01)
        self.run.cancelled=True
        os.killpg(self.run.proc.pid, signal.SIGTERM)
        _, _, err=await task
        self.assertEqual(err,'остановлено')
        self.assertIsNotNone(self.run.proc.returncode)

    async def test_external_task_cancellation_reaps_child(self):
        task=asyncio.create_task(self.ns['run_antigravity']('wait'))
        while not self.state.get('agy_threads'): await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertIsNotNone(self.run.proc.returncode)
        self.assertEqual(self.ns['RUN_THREADS'],{})

class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_engine_pairs_and_invalid_twins(self):
        tids={'mercury':'merc-old','antigravity':'agy-old','codex':'codex-old'}
        for target in tids:
            for source,tid in tids.items():
                calls=[]
                async def runner(prompt, thread,*args): calls.append((prompt,thread));return thread,'ok',None
                ns=functions({'run_agent'},engine_for=lambda _:target,is_mercury_thread=lambda t:str(t).startswith('merc-'),
                    USER_TIMEOUT=5,STATE={'xthreads':{tid:'unrelated-codex'},'last_answer':{tid:'context'}},
                    run_mercury=runner,run_antigravity=runner,run_codex=runner)
                await ns['run_agent']('request',tid)
                expected=tid if target==source else ('unrelated-codex' if target=='codex' else None)
                self.assertEqual(calls[0][1],expected,(source,target))

class KeyboardTests(unittest.TestCase):
    def test_continue_only_for_step_limit(self):
        ns=functions({'result_kb'},MERCURY_STEPS=30,multi_questions=lambda _:[],yes_no_question=lambda _:False,btn=lambda text,action,*a:action,
            kb=lambda *rows:rows,custom_keyboard=lambda markup,*a:markup)
        for err in (None,'остановлено','таймаут','HTTP 500','не уложился в 30 шагов'):
            for mercury in (False,True):
                rows=ns['result_kb']('answer',err=err,mercury=mercury)
                self.assertEqual(any('th:continue' in row for row in rows),mercury and err=='не уложился в 30 шагов')
