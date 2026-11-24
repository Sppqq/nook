import asyncio
import signal
import sys
import unittest

from agent_process import read_tail, stop_process_group


class AgentProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_keeps_only_the_configured_tail(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b'a' * 80_000 + b'tail')
        stream.feed_eof()
        self.assertEqual(await read_tail(stream, 8), b'aaaatail')

    async def test_stop_escalates_if_child_ignores_termination(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-u', '-c',
            'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            'print("ready"); time.sleep(60)',
            start_new_session=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            self.assertEqual(await asyncio.wait_for(proc.stdout.readline(), 5), b'ready\n')
            await stop_process_group(proc, grace=0.2)
            self.assertEqual(proc.returncode, -signal.SIGKILL)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
