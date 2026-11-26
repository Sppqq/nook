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
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import CallbackQuery, Update

from nook_update import STATE as INSTALL_STATE, json_write, read_json, update_settings, workspace
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

    async def test_limits_switch_edits_one_menu_and_replaces_failed_edit(self):
        runtime = self.runtime
        runtime['STATE'].update(view_msgs=[49, 50], ui_language='en', fallback_until=0)
        runtime['CFG'].update(model='gpt-6.1-sol', effort='high')
        runtime['get_limits'] = AsyncMock(return_value=None)
        runtime['antigravity_limits'] = Mock(get_limits=AsyncMock(return_value=None), format_limits=Mock(return_value=''))
        runtime['edit'] = AsyncMock(side_effect=[True, False])
        runtime['post'] = AsyncMock(return_value=51)
        runtime['delete_msgs'] = AsyncMock()

        await self.dispatch('lm:gpt-6.2-sol')
        self.assertEqual((runtime['CFG']['model'], runtime['CFG']['effort']), ('gpt-6.2-sol', 'high'))
        self.assertEqual(runtime['STATE']['view_msgs'], [50])
        runtime['delete_msgs'].assert_awaited_once_with(7, [49])
        runtime['post'].assert_not_awaited()
        self.assertIn('6.2 Sol · high', runtime['edit'].await_args.args[2])

        await self.dispatch('limits!')
        self.assertEqual(runtime['STATE']['view_msgs'], [51])
        runtime['delete_msgs'].assert_awaited_with(7, [50])
        self.assertEqual(runtime['post'].await_args.args[2].inline_keyboard[0][0].text, '✓ 6.2 Sol')
        runtime['antigravity_limits'].get_limits.assert_awaited_with(force=True)

    async def test_pool_five_hour_reset_requires_ninety_percent(self):
        from test_codex_pool import fake_accounts

        registry = self.runtime['REG']
        mod = registry.mods['codex_pool']
        pool = mod.pool
        pool.root = self.home / '.codex'
        fake_accounts(pool.root)
        account = pool.accounts()[0]
        model = registry.core.codex_model()
        now = time.time()
        windows = [SimpleNamespace(key=('codex', key), minutes=minutes, used=0, reset=now + minutes * 60)
                   for key, minutes in (('small', 300), ('full', 300), ('week', 10080))]
        pool.cache[(account, model)] = (time.monotonic(), SimpleNamespace(windows=windows))
        pool.refresh = AsyncMock()
        mod.state.update(enabled=True, notify=True, windows={
            f'{account.ident}/codex/{window.key[1]}/{window.minutes}': [now - 1, usage]
            for window, usage in zip(windows, (89, 90, 1))
        })

        await mod.periodic[0][1]()
        text = registry.core.send.await_args.args[1]
        self.assertEqual(text.count(account.name), 2)
        self.assertEqual(text.count('5 ч'), 1)
        self.assertEqual(text.count('неделя'), 1)

    async def test_dependency_update_reports_health_before_polling_or_background_work(self):
        runtime = self.runtime
        base = workspace(self.home)
        journal = base / 'transaction.json'
        transaction = {'status': 'awaiting_health', 'environment': {'old': [1, 2], 'new': [1, 3]}}
        json_write(journal, transaction)
        json_write(self.home / INSTALL_STATE, {'revision': 'candidate'})
        runtime['mods_reload'] = AsyncMock()
        runtime['set_commands'] = AsyncMock()
        runtime['STATE']['introduced'] = True
        jobs = {name: AsyncMock() for name in (
            'trash_loop', 'watch_loop', 'outbox_loop', 'jobs_loop', 'patrol_loop',
            'guard_loop', 'whisper_unload_loop', 'mods_loop', 'resume_loop', 'ops_loop',
        )}
        runtime.update(jobs)

        async def release_after_heartbeat(delay):
            self.assertEqual(delay, 2)
            health = read_json(base / 'health.json')
            self.assertEqual((health['revision'], health['pid']), ('candidate', os.getpid()))
            start_tasks.assert_not_called()
            polling.assert_not_awaited()
            runtime['set_commands'].assert_not_awaited()
            for job in jobs.values():
                job.assert_not_called()
            json_write(journal, dict(transaction, status='success'))

        with (patch.object(runtime['bot'], 'get_me', new_callable=AsyncMock) as get_me,
              patch.object(runtime['dp'], 'start_polling', new_callable=AsyncMock) as polling,
              patch.object(runtime['REG'], 'start_tasks') as start_tasks,
              patch.object(asyncio, 'sleep', side_effect=release_after_heartbeat)):
            await runtime['main']()
        runtime['mods_reload'].assert_awaited_once_with(start_tasks=False)
        get_me.assert_awaited_once()
        start_tasks.assert_called_once()
        runtime['set_commands'].assert_awaited_once()
        polling.assert_awaited_once_with(runtime['bot'])
        for job in jobs.values():
            job.assert_called_once()

    async def test_candidate_without_telegram_health_never_claims_work(self):
        runtime = self.runtime
        base = workspace(self.home)
        json_write(base / 'transaction.json', {'status': 'awaiting_health', 'environment': {}})
        runtime['mods_reload'] = AsyncMock()
        with (patch.object(runtime['bot'], 'get_me', side_effect=RuntimeError('Telegram unavailable')),
              patch.object(runtime['dp'], 'start_polling', new_callable=AsyncMock) as polling,
              patch.object(runtime['REG'], 'start_tasks') as start_tasks):
            with self.assertRaisesRegex(RuntimeError, 'Telegram unavailable'):
                await runtime['main']()
        runtime['mods_reload'].assert_awaited_once_with(start_tasks=False)
        self.assertFalse((base / 'health.json').exists())
        start_tasks.assert_not_called()
        polling.assert_not_awaited()

    async def test_device_login_only_replaces_auth_after_cli_success(self):
        runtime = self.runtime
        auth_home = self.home / '.codex'
        auth_home.mkdir(mode=0o700)
        auth = auth_home / 'auth.json'
        auth.write_text('{"tokens":{"access_token":"previous-fixture"}}')
        auth.chmod(0o600)
        previous = auth.read_text()
        cli = self.home / 'device-login-fixture'
        cli.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
assert sys.argv[1:] == ['login', '--device-auth']
pathlib.Path(os.environ['CODEX_HOME'], 'auth.json').write_text(
    '{"tokens":{"access_token":"new-fixture"}}')
sys.exit(0 if pathlib.Path(__file__).with_suffix('.approve').exists() else 1)
''')
        cli.chmod(0o700)
        runtime['CODEX'] = str(cli)
        runtime['send'] = AsyncMock()
        runtime['send_tmp'] = AsyncMock()
        runtime['STATE'].update(main_thread='old-thread', fallback_until=100, ui_language='en')
        imported = []

        async def after_login(path):
            self.assertTrue(runtime['CODEX_LOGIN_LOCK'].locked())
            imported.append(json.loads(path.read_text())['tokens']['access_token'])

        with patch.object(runtime['os'].path, 'expanduser', return_value=str(auth_home)):
            await runtime['do_login'](7, after_login=after_login)
            self.assertEqual(auth.read_text(), previous)
            self.assertEqual(runtime['STATE']['main_thread'], 'old-thread')
            self.assertIn('Could not sign in', runtime['send'].await_args.args[1])
            self.assertEqual(imported, [])
            cli.with_suffix('.approve').touch()
            await runtime['do_login'](7, after_login=after_login)

        self.assertEqual(json.loads(auth.read_text())['tokens']['access_token'], 'new-fixture')
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
        backup, = auth_home.glob('auth.json.bak-*')
        self.assertEqual(backup.read_text(), previous)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(runtime['STATE']['main_thread'])
        self.assertEqual(runtime['STATE']['fallback_until'], 0)
        self.assertIn('Codex account connected', runtime['send_tmp'].await_args.args[1])
        self.assertEqual(imported, ['new-fixture'])
        runtime['STATE']['main_thread'] = 'agy-existing'
        with patch.object(runtime['os'].path, 'expanduser', return_value=str(auth_home)):
            await runtime['do_login'](7)
        self.assertEqual(runtime['STATE']['main_thread'], 'agy-existing')

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
        self.runtime["mercury_key"] = Mock(return_value=None)
        await self.runtime["pause_for_limit"]({
            "lane": "user", "thread": "fixture-thread", "prompt": "continue",
            "title": "Test turn", "background": False, "header": "",
        })
        paused = self.runtime["STATE"]["paused"][0]
        self.assertAlmostEqual(paused["reset"] - paused["at"], 60, delta=2)
        message = self.runtime["notify"].await_args.args[0]
        self.assertIn("Проверю пул", message)
        self.assertNotIn("после сброса", message)
        self.assertNotIn("Mercury", message)
        self.assertEqual(self.runtime["engine_for"](), "codex")
        self.runtime["mercury_key"].return_value = "fixture-only"
        self.assertEqual(self.runtime["engine_for"](), "mercury")

        self.runtime["post"] = AsyncMock(return_value=60)
        self.runtime["delete_msgs"] = AsyncMock()
        await self.dispatch("custom:language:en")
        self.runtime["STATE"]["paused"].clear()
        await self.runtime["pause_for_limit"](dict(paused), partial="fixture progress")
        paused = self.runtime["STATE"]["paused"][0]
        notice = self.runtime["notify"].await_args
        self.assertIn("check the pool in ", notice.args[0])
        self.assertIn("New tasks run on Mercury", notice.args[0])
        self.assertIn("Progress so far", notice.args[0])
        self.assertEqual(
            [(button.text, button.callback_data) for button in notice.kwargs["markup"].inline_keyboard[0]],
            [("Limits", "limits!"), ("Menu", "menu")],
        )
        unverified = await self.runtime["get_limits"](0)
        self.assertEqual(unverified["pool_label"], "Codex pool · 0 of 2 available · 1 awaiting verification")

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
        self.assertEqual(digest.call_args.args[0], "Codex quota is available · 1 to resume")

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

    async def test_owner_switches_language_without_changing_update_settings(self):
        runtime = self.runtime
        runtime['post'] = AsyncMock(return_value=60)
        runtime['delete_msgs'] = AsyncMock()
        runtime['model_quota_status'] = AsyncMock(return_value=('', ''))
        registry = runtime['REG']

        for sender, chat, kind in ((8, 8, 'private'), (7, 8, 'private'), (7, -1007, 'group')):
            await self.dispatch('custom:language:en', sender, chat, kind)
        self.assertNotIn('ui_language', runtime['STATE'])

        await self.dispatch('custom:language:en')
        saved = read_json(Path(runtime['STATE_PATH']))
        self.assertEqual(saved['ui_language'], 'en')
        self.assertIn('Interface settings', runtime['post'].await_args.args[1])
        for view, next_button in (('menu', 'More'), ('more', 'Mods'), ('mods', 'Updates and backups')):
            await self.dispatch(view)
            markup = runtime['post'].await_args.args[2]
            self.assertTrue(any(next_button in button.text
                                for row in markup.inline_keyboard for button in row))
        await self.dispatch('mod:update_manager:open_menu')
        self.assertIn('Legacy mode (main)', registry.core.send.await_args.args[1])
        self.assertIn('Check for updates', str(registry.core.send.await_args.kwargs['markup']))
        self.assertIsNone(update_settings(self.home)['channel'])
        self.assertEqual(update_settings(self.home)['auto_mode'], 'off')

        from test_codex_pool import fake_accounts, limits
        pool = registry.mods["codex_pool"].pool
        runtime['CFG']['model'] = 'gpt-6.1-sol'
        pool.root = self.home / ".codex"
        fake_accounts(pool.root)
        account = pool.accounts()[0]
        pool.read_limits = AsyncMock(return_value=limits("primary", 96))
        await self.dispatch("mod:codex_pool:strategy:least")
        self.assertIn("Least quota remaining first", registry.core.edit.await_args.args[2])
        self.assertEqual(pool.settings["strategy"], "least")
        await self.dispatch(f"mod:codex_pool:account:{account.ident}")
        self.assertIn("Week · 4% remaining", registry.core.edit.await_args.args[2])
        await self.dispatch(f"mod:codex_pool:cap:{account.ident},95")
        self.assertEqual(pool.settings["caps"][account.ident], 95)
        self.assertEqual(registry.core.edit.await_args.args[2], "Budget saved.")
        sample = {
            "secondary": {"used": 96, "mins": 10080, "reset": time.time() + 3600},
            "plan": "plus", "live": True, "credits": {"unlimited": True},
        }
        runtime["get_limits"] = AsyncMock(return_value=sample)
        quota = runtime["fmt_limits"](sample)
        self.assertIn("Codex quota", quota)
        self.assertIn("Week:", quota)
        self.assertIn("resets in", quota)
        self.assertIn("credits: unlimited", quota)
        self.assertIn("Week 96%", await runtime["limits_note"]("codex"))

        await self.dispatch('custom:preset:minimal')
        self.assertEqual(read_json(Path(runtime['STATE_PATH']))['ui_language'], 'en')
        await self.dispatch('custom:language:ru')
        self.assertEqual(read_json(Path(runtime['STATE_PATH']))['ui_language'], 'ru')
        self.assertIn("Лимиты Codex", runtime["fmt_limits"](sample))
        await self.dispatch('mod:update_manager:open_menu')
        self.assertIn('Существующий режим (main)', registry.core.send.await_args.args[1])

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

    async def test_english_tasks_status_and_model_views(self):
        from contextlib import nullcontext

        runtime = self.runtime
        runtime['STATE']['ui_language'] = 'en'
        runtime['post'] = AsyncMock(return_value=60)
        runtime['edit'] = AsyncMock(return_value=True)
        runtime['delete_msgs'] = AsyncMock()
        runtime['waiting'] = lambda *args: nullcontext()
        runtime['model_quota_status'] = AsyncMock(return_value=('', ''))
        runtime['codex_default'] = lambda key: None
        runtime['mercury_key'] = Mock(return_value=None)
        runtime['collect'] = AsyncMock(return_value=({}, {}))
        queue = self.home / 'spool/jobs.json'
        job_scheduler.write(queue, [{'id': 'task', 'at': 1700000000, 'every': 3600, 'prompt': 'Custom reminder'}])

        await self.dispatch('jobs')
        self.assertIn('Scheduled tasks', runtime['post'].await_args.args[1])
        self.assertIn('every 1 h', runtime['post'].await_args.args[1])
        await self.dispatch('jc:task')
        self.assertIn('Cancel this task?', runtime['edit'].await_args.args[2])
        self.assertEqual(runtime['edit'].await_args.args[3].inline_keyboard[0][0].callback_data, 'jcy:task')
        await self.dispatch('jcy:task')
        self.assertEqual(job_scheduler.list_jobs(queue), [])
        self.assertIn('No tasks yet', runtime['post'].await_args.args[1])
        await self.dispatch('jcy:task')
        self.assertEqual(job_scheduler.list_jobs(queue), [])
        await self.dispatch('status')
        for line in ('Server status', 'Uptime', 'Previously healthy services are running'):
            self.assertIn(line, runtime['post'].await_args.args[1])
        runtime['CFG']['model'] = 'gpt-6.1-sol'
        runtime['STATE']['fallback_until'] = time.time() + 60
        await self.dispatch('model')
        self.assertNotIn('Codex quota is exhausted', runtime['post'].await_args.args[1])
        runtime['mercury_key'].return_value = 'fixture-only'
        await self.dispatch('model')
        self.assertIn('Codex quota is exhausted', runtime['post'].await_args.args[1])
        await self.dispatch('m:mercury-3')
        self.assertEqual(runtime['CFG']['model'], 'mercury-3')
        self.assertIn('**Model**', runtime['post'].await_args.args[1])

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
elif prompt == 'malformed-stream':
    print('{unparseable')
elif prompt == 'completed-turn':
    print(json.dumps({'type': 'turn.completed'}))
print(json.dumps({'type': 'turn.failed', 'message': 'usage limit'}))
""")
        cli.chmod(0o700)
        (self.home / 'spool').mkdir(exist_ok=True)
        self.runtime['CODEX'] = str(cli)
        self.runtime['pause_for_limit'] = AsyncMock()
        self.runtime['limits_note'] = AsyncMock(return_value='')
        self.runtime['send'] = AsyncMock(return_value=50)
        for prompt in ('after-command', 'malformed-stream', 'completed-turn'):
            await self.runtime['run_task'](self.runtime['user_lane'], prompt, show_progress=False)
            self.runtime['pause_for_limit'].assert_not_awaited()
            self.assertIn('могла уже изменить состояние', str(self.runtime['send'].call_args))
        tid, _, error = await self.runtime['_run_codex']('mismatched-thread', 'original-thread', timeout=5)
        self.assertEqual(tid, 'original-thread')
        self.assertIn('некорректный сеанс', error)
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
