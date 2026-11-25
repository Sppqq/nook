import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tool_executor import ShellCalls, excerpt


class ToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_keeps_both_ends_under_a_fixed_limit(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b'begin' + b'x' * 100000 + b'end')
        stream.feed_eof()
        result = await excerpt(stream)
        self.assertTrue(result.startswith('begin'))
        self.assertTrue(result.endswith('end'))
        self.assertIn('обрезано', result)
        self.assertLess(len(result), 8100)

    async def test_timed_out_command_is_held_even_under_a_new_tool_id(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = ShellCalls()
            run = SimpleNamespace(cancelled=False, proc=None)
            loop = asyncio.get_running_loop()

            async def invoke(ident):
                return await calls.execute(
                    {'id': ident, 'function': {'name': 'shell', 'arguments':
                     json.dumps({'cmd': 'printf x >> calls; sleep 5', 'timeout': 1})}},
                    cwd=directory, env=os.environ, run=run, deadline=loop.time() + 3,
                )

            self.assertTrue((await invoke('first')).uncertain)
            self.assertIn('Исход предыдущего', (await invoke('second')).output)
            self.assertIsNone(run.proc)
            self.assertEqual((Path(directory) / 'calls').read_text(), 'x')
