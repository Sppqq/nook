"""Exercise cleanup with real local subprocesses, without provider credentials."""
import ast
import asyncio
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time
import unittest

namespace = dict(globals())
tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text())
names = {'antigravity_capacity_error', 'wait_antigravity'}
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names], type_ignores=[]), '<gemini-helpers>', 'exec'), namespace)

class GeminiProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'cli.log'
        self.log.write_text('')

    async def child(self, code):
        return await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(self.log), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)

    async def test_overload_stops_retrying_process(self):
        proc = await self.child('import sys,time; from pathlib import Path; Path(sys.argv[1]).write_text("No capacity available for model gemini-example-high"); time.sleep(60)')
        _, _, reason = await namespace['wait_antigravity'](proc, self.log, 5)
        self.assertIn('Gemini перегружен', reason)
        self.assertIsNotNone(proc.returncode)

    async def test_success_ignores_unrelated_diagnostic(self):
        self.log.write_text('initial auth warning')
        proc = await self.child('print("SUCCESS")')
        out, _, reason = await namespace['wait_antigravity'](proc, self.log, 5)
        self.assertEqual(out.strip(), b'SUCCESS')
        self.assertIsNone(reason)

    async def test_timeout_reaps_process(self):
        proc = await self.child('import time; time.sleep(60)')
        with self.assertRaises(asyncio.TimeoutError):
            await namespace['wait_antigravity'](proc, self.log, 0.1)
        self.assertIsNotNone(proc.returncode)

    async def test_cancellation_reaps_process(self):
        proc = await self.child('import time; time.sleep(60)')
        task = asyncio.create_task(namespace['wait_antigravity'](proc, self.log, 60))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(proc.returncode)
