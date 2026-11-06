"""Exercise the real HTTP middleware and queue consumer with an isolated fake agent."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from aiohttp import ClientSession

spec = importlib.util.spec_from_file_location('owner_api', '__NOOK_USER_HOME__/nook/mods/owner_api.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeMod:
    def on_start(self, fn):
        self.start = fn
        return fn

    def every(self, secs):
        def register(fn):
            self.consume = fn
            return fn
        return register

    async def agent(self, prompt, **kwargs):
        import re
        reply = Path(re.search(r'запиши ответ в (\S+) как JSON', prompt)[1])
        mod.save(reply, {'status': 'completed', 'answer': 'test answer'})


class API(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        mod.ROOT = Path(self.temp.name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            mod.PORT = sock.getsockname()[1]
        self.m = FakeMod()
        mod.setup(self.m)
        self.server = asyncio.create_task(self.m.start())
        self.client = ClientSession()
        self.base = f'http://127.0.0.1:{mod.PORT}'
        self.auth = {'Authorization': 'Bearer ' + (mod.ROOT / 'token').read_text()}
        for i in range(50):
            try:
                async with self.client.get(self.base + '/health', headers=self.auth) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            await asyncio.sleep(.01)
        else:
            raise AssertionError('Server did not start')

    async def asyncTearDown(self):
        self.server.cancel()
        try:
            await self.server
        except asyncio.CancelledError:
            pass
        await self.client.close()
        self.temp.cleanup()

    async def post(self, data):
        async with self.client.post(self.base + '/v1/messages', json=data, headers=self.auth) as r:
            return r.status, await r.text()

    async def test_auth_and_validation(self):
        async with self.client.get(self.base + '/health') as r:
            self.assertEqual(r.status, 401)
        async with self.client.get(self.base + '/health', headers={**self.auth, 'Origin': 'https://example.com'}) as r:
            self.assertEqual(r.status, 403)
        for value in ([], {}, {'message': ''}, {'message': 'x', 'conversation_id': '../../a'}, {'message': 'x' * 12001}):
            self.assertEqual((await self.post(value))[0], 400)
        self.assertEqual((mod.ROOT / 'token').stat().st_mode & 0o777, 0o600)

    async def test_idempotency_history_and_delivery(self):
        data = {'message': 'hello', 'request_id': 'a' * 32}
        code, body = await self.post(data)
        self.assertEqual(code, 202)
        cid = json.loads(body)['conversation_id']
        self.assertEqual((await self.post(data))[0], 200)
        self.assertEqual((await self.post({**data, 'message': 'different'}))[0], 409)
        await self.m.consume()
        r = mod.status(data['request_id'])
        self.assertEqual((r['status'], r['answer']), ('completed', 'test answer'))
        self.assertEqual(len(mod.history(cid)), 1)
        # A second poll/consumer pass must not execute the task again.
        async def fail(*args, **kwargs):
            raise AssertionError('Unexpected replay')
        self.m.agent = fail
        await self.m.consume()
        async with self.client.get(self.base + '/v1/requests/' + 'b' * 32, headers=self.auth) as r:
            self.assertEqual(r.status, 404)

    async def test_reload_does_not_replay(self):
        await self.post({'message': 'possibly executed', 'request_id': 'c' * 32})
        mod.save(mod.ROOT / 'states' / ('c' * 32 + '.json'), {'status': 'running'})
        self.server.cancel()
        try:
            await self.server
        except asyncio.CancelledError:
            pass
        self.server = asyncio.create_task(self.m.start())
        await asyncio.sleep(.05)
        self.assertEqual(mod.status('c' * 32)['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
