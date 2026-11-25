"""Run actual aiogram dispatch and installed Nook callbacks without Telegram I/O."""
import importlib.util
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import CallbackQuery, Update

from nook_update import update_settings
import job_scheduler
from local_extensions import Store

ROOT = Path(__file__).resolve().parents[1]


class TelegramDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.home = Path(work.name)
        (self.home / 'config.json').write_text(json.dumps({'bot_token': '123456:' + 'X' * 35, 'owner_id': 7}))
        bot_path = ROOT / 'bot.py'
        sandbox = {'__file__': str(bot_path), '__name__': 'disposable_nook'}
        with patch.dict(os.environ, {'NOOK_HOME': str(self.home)}):
            exec(compile(bot_path.read_text(), str(bot_path), 'exec'), sandbox)
        self.runtime = sandbox
        self.addAsyncCleanup(self.runtime['bot'].session.close)
        registry = self.runtime['REG']
        state_dir = patch.object(self.runtime['modsys'], 'SPOOL', str(self.home / 'spool/mods'))
        state_dir.start()
        self.addCleanup(state_dir.stop)
        (self.home / 'mods').mkdir()
        for name in ('update_manager', 'local_extension_ui', 'codex_pool'):
            mod_path = self.home / 'mods' / f'{name}.py'
            shutil.copyfile(ROOT / 'mods' / f'{name}.py', mod_path)
            spec = importlib.util.spec_from_file_location('disposable_' + name, mod_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            instance = self.runtime['modsys'].Mod(name, registry.core)
            mod.setup(instance)
            registry.mods[name] = instance
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

    async def test_owner_pool_strategy_and_cli_resume_after_quota(self):
        from test_codex_pool import fake_accounts
        pool = self.runtime["REG"].mods["codex_pool"].pool
        pool.root = self.home / ".codex"
        fake_accounts(pool.root)
        await self.dispatch("mod:codex_pool:toggle:enabled", sender=8, chat=8)
        self.assertFalse(pool.enabled())
        await self.dispatch("mod:codex_pool:toggle:enabled")
        await self.dispatch("mod:codex_pool:strategy:least")
        self.assertTrue(pool.enabled())
        self.assertEqual(pool.settings["strategy"], "least")
        cli = self.home / "codex-fixture"
        cli.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ["CODEX_HOME"])
who = (home / "auth.json").read_text()
args = sys.argv[1:]
if args[0] == "app-server":
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("id") == 2:
            print(json.dumps({"id": 2, "result": {
                "accountId": who, "ordinaryUsageAllowed": True,
                "rateLimits": {"secondary": {"usedPercent": 96 if who == "reserve" else 0,
                                               "windowDurationMins": 10080, "resetsAt": time.time()+3600}},
            }}), flush=True)
            break
else:
    prompt = sys.stdin.read()
    assert "OPENAI_API_KEY" not in os.environ
    print(json.dumps({"type":"thread.started", "thread_id":"fixture-thread"}))
    if who == "reserve":
        assert "resume" not in args
        print(json.dumps({"type":"turn.failed", "error":{"message":"usage_limit_reached"}}))
    else:
        assert prompt == "finish"
        assert args[-3:] == ["resume", "fixture-thread", "-"]
        print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"done"}}))
''')
        cli.chmod(0o700)
        self.runtime["CODEX"] = str(cli)
        (self.home / "spool").mkdir(exist_ok=True)
        (self.home / "secrets.env").write_text("OPENAI_API_KEY=fixture-only\n")
        run = self.runtime["run_codex"]
        self.assertEqual(await run("finish", timeout=8), ("fixture-thread", "done", None))
        self.assertEqual((pool.root / "auth.json").read_text(), "primary")
        self.assertEqual(set(self.runtime["RUN_THREADS"].values()), {"fixture-thread"})

    async def test_pool_pause_rechecks_unknown_quota_before_another_accounts_reset(self):
        from test_codex_pool import fake_accounts, limits
        pool = self.runtime["REG"].mods["codex_pool"].pool
        pool.root = self.home / ".codex"
        fake_accounts(pool.root)
        await self.dispatch("mod:codex_pool:toggle:enabled")

        reset = time.time() + 3600

        async def quota(env):
            account = (Path(env["CODEX_HOME"]) / "auth.json").read_text()
            if account == "primary":
                raise RuntimeError("temporary app-server error")
            return limits(account, 100, reset)

        pool.read_limits = quota
        self.runtime["notify"] = AsyncMock()
        await self.runtime["pause_for_limit"]({
            "lane": "user", "thread": "fixture-thread", "prompt": "continue",
            "title": "Test turn", "background": False, "header": "",
        })
        paused = self.runtime["STATE"]["paused"][0]
        self.assertAlmostEqual(paused["reset"] - paused["at"], 60, delta=2)
        message = self.runtime["notify"].await_args.args[0]
        self.assertIn("Проверю пул", message)
        self.assertNotIn("после сброса", message)

        async def available(env):
            account = (Path(env["CODEX_HOME"]) / "auth.json").read_text()
            return limits(account, 5 if account == "primary" else 100, reset)

        pool.read_limits = available
        status = await self.runtime["get_limits"](0)
        self.assertIsNone(status["reached"])
        self.assertLess(status["pool_reset"], time.time() + 2)

        paused["reset"] = time.time() - 1
        run_task = self.runtime["run_task"] = AsyncMock()
        digest = self.runtime["digest"] = Mock()
        completed = asyncio.Event()

        async def sleep(delay):
            if delay == 60:
                completed.set()
                await asyncio.Future()

        with patch.object(self.runtime["asyncio"], "sleep", new=sleep):
            loop = asyncio.create_task(self.runtime["resume_loop"]())
            try:
                await asyncio.wait_for(completed.wait(), 2)
            finally:
                loop.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await loop
        run_task.assert_awaited_once()
        self.assertEqual(run_task.await_args.kwargs["thread_id"], "fixture-thread")
        self.assertTrue(run_task.await_args.kwargs["resumed"])
        self.assertEqual(self.runtime["STATE"]["paused"], [])
        self.assertEqual(self.runtime["STATE"]["fallback_until"], 0)
        self.assertIn("к продолжению: 1", digest.call_args.args[0])

    async def test_pool_pauses_only_on_recognized_quota_error(self):
        from test_codex_pool import fake_accounts
        pool = self.runtime["REG"].mods["codex_pool"].pool
        pool.root = self.home / ".codex"
        fake_accounts(pool.root)
        await self.dispatch("mod:codex_pool:toggle:enabled")
        runtime = self.runtime
        runtime["engine_for"] = lambda _: "codex"
        runtime["send"] = AsyncMock(return_value=1)
        runtime["limits_note"] = AsyncMock(return_value="")
        runtime["pause_for_limit"] = AsyncMock()
        runtime["run_agent"] = AsyncMock(side_effect=[
            (None, "", "invalid quota config path"),
            ("conversation", "", "usage_limit_reached"),
        ])
        await runtime["run_task"](runtime["user_lane"], "first", show_progress=False)
        runtime["pause_for_limit"].assert_not_awaited()
        runtime["send"].assert_awaited_once()
        await runtime["run_task"](runtime["user_lane"], "second", show_progress=False)
        runtime["pause_for_limit"].assert_awaited_once()
        recorded = runtime["pause_for_limit"].await_args.args[0]
        self.assertEqual(recorded["thread"], "conversation")

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

    async def test_real_mercury_runner_deduplicates_execution_and_saves_session(self):
        self.runtime['mercury_key'] = lambda: 'fixture'
        self.runtime['mercury_system'] = lambda: 'fixture system instructions'
        invocation = {'id': 'same-call', 'type': 'function', 'function': {
            'name': 'shell', 'arguments': '{"cmd":"printf x >> completed"}',
        }}
        reply = {'role': 'assistant', 'tool_calls': [invocation]}
        provider = AsyncMock(side_effect=[(reply, {}), (reply, {}), ({'role': 'assistant', 'content': 'done'}, {})])
        self.runtime['mercury_chat'] = provider
        thread, final, error = await self.runtime['run_mercury']('test', timeout=5)
        self.assertEqual((final, error), ('done', None))
        self.assertEqual((self.home / 'completed').read_text(), 'x')
        self.assertTrue((self.home / 'spool/mercury' / (thread + '.json')).is_file())

    async def test_mercury_provider_wait_cannot_exceed_request_deadline(self):
        self.runtime['mercury_key'] = lambda: 'fixture'
        self.runtime['mercury_system'] = lambda: 'fixture system instructions'

        async def stalled(*args):
            await asyncio.sleep(10)

        self.runtime['mercury_chat'] = stalled
        _, _, error = await asyncio.wait_for(self.runtime['run_mercury']('test', timeout=0.04), 1)
        self.assertIn('таймаут', error)

    async def test_scheduled_provider_error_keeps_claim_and_prevents_fallback(self):
        queue = self.home / 'spool/jobs.json'
        job_scheduler.write(queue, [{'id': 'task', 'at': 1, 'every': 120, 'prompt': 'check'}])
        job, = job_scheduler.take_due(queue, now=1)
        self.runtime['run_agent'] = AsyncMock(return_value=(None, '', 'Codex rate limit'))
        self.runtime['pause_for_limit'] = AsyncMock()
        self.runtime['limits_note'] = AsyncMock(return_value='')
        self.runtime['send'] = AsyncMock(return_value=50)
        await self.runtime['execute_scheduled'](job)
        self.runtime['pause_for_limit'].assert_not_awaited()
        self.assertIn(job['claim_id'], job_scheduler.unresolved(queue))
        self.assertEqual(job_scheduler.take_due(queue, now=122), [])

    async def test_owner_can_check_activate_and_rollback_a_staged_skill(self):
        import hashlib

        store = Store(self.home)
        source = self.home / 'skill.md'
        source.write_text('fixture skill')
        store.stage('skill', 'example', '1.0.0', source)
        version = hashlib.sha256(b'1.0.0').hexdigest()[:12]
        key = f'skill:example:{version}'
        await self.dispatch('mod:local_extension_ui:t:' + key)
        self.assertTrue(store.status('skill', 'example')['versions']['1.0.0']['checked'])
        with patch('local_extensions.approval', return_value=False):
            await self.dispatch('mod:local_extension_ui:a:' + key)
        self.assertIsNone(store.status('skill', 'example')['active'])
        with patch('local_extensions.approval', return_value=True):
            await self.dispatch('mod:local_extension_ui:a:' + key, sender=8, chat=8)
            self.assertIsNone(store.status('skill', 'example')['active'])
            await self.dispatch('mod:local_extension_ui:a:' + key)
        self.assertEqual(store.status('skill', 'example')['active'], '1.0.0')
        await self.dispatch('mod:local_extension_ui:r:skill:example')
        self.assertIsNone(store.status('skill', 'example')['active'])

    async def test_real_codex_stream_stops_failover_after_tool_activity(self):
        cli = self.home / 'codex-fixture'
        cli.write_text("""#!/usr/bin/python3
import json
import sys
prompt = sys.stdin.read()
print(json.dumps({'type': 'thread.started', 'thread_id': 'codex-fixture'}))
if prompt == 'after-command':
    print(json.dumps({'type': 'item.started', 'item': {'type': 'command_execution'}}))
print(json.dumps({'type': 'turn.failed', 'message': 'usage limit'}))
""")
        cli.chmod(0o700)
        (self.home / 'spool').mkdir(exist_ok=True)
        self.runtime['CODEX'] = str(cli)
        self.runtime['pause_for_limit'] = AsyncMock()
        self.runtime['limits_note'] = AsyncMock(return_value='')
        self.runtime['send'] = AsyncMock(return_value=50)
        await self.runtime['run_task'](self.runtime['user_lane'], 'after-command', show_progress=False)
        self.runtime['pause_for_limit'].assert_not_awaited()
        self.assertIn('могла уже изменить состояние', str(self.runtime['send'].call_args))
        await self.runtime['run_task'](self.runtime['user_lane'], 'before-command', show_progress=False)
        self.runtime['pause_for_limit'].assert_awaited_once()

    async def test_real_antigravity_runner_keeps_provider_conversation(self):
        cli = self.home / 'agy-fixture'
        cli.write_text("""#!/usr/bin/python3
import json
import sys
resumed = '--conversation' in sys.argv and 'conversation-fixture' in sys.argv
print(json.dumps({'event': 'init', 'conversation_id': 'conversation-fixture'}))
print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS',
      'response': 'resumed' if resumed else 'fresh'}}))
""")
        cli.chmod(0o700)
        (self.home / 'spool').mkdir(exist_ok=True)
        self.runtime['AGY_BIN'] = str(cli)
        self.runtime['CFG']['model'] = 'antigravity-argon'
        ident, answer, error = await self.runtime['run_agent']('hello', timeout=5)
        self.assertEqual((answer, error), ('fresh', None))
        self.assertEqual(await self.runtime['run_agent']('follow-up', ident, timeout=5), (ident, 'resumed', None))
