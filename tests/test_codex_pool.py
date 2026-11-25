"""Exercise installed routing with private fixture logins and a streaming CLI."""
import asyncio
from base64 import urlsafe_b64encode
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from codex_pool import Pool, quota_for, saved_accounts


def fake_accounts(root):
    folder = root / "accounts"
    folder.mkdir(parents=True)
    entries = []
    for index, name in enumerate(("primary", "reserve", "duplicate"), 1):
        entries.append({"account_key": name, "alias": name, "auth_mode": "chatgpt",
                        "chatgpt_account_id": "primary" if index != 2 else "reserve"})
        path = root / "auth.json" if index == 1 else folder / (urlsafe_b64encode(name.encode()).decode().rstrip("=") + ".auth.json")
        path.write_text(name)
        path.chmod(0o600)
    (folder / "registry.json").write_text(json.dumps({"active_account_key": "primary", "accounts": entries}))


def limits(workspace, used, reset=None):
    return {"accountId": workspace, "ordinaryUsageAllowed": True,
            "rateLimits": {"secondary": {"usedPercent": used, "windowDurationMins": 10080,
                                         "resetsAt": reset or time.time() + 3600}}}


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / ".codex"
        fake_accounts(self.root)
        self.settings = {"enabled": True, "strategy": "least"}
        self.pool = Pool(self.root, self.settings, self.quota_read, asyncio.Lock())
        self.primary, self.reserve = self.pool.accounts()
        self.run = SimpleNamespace(cancelled=False, effects_possible=False)

    async def quota_read(self, env):
        who = (Path(env["CODEX_HOME"]) / "auth.json").read_text()
        return limits(who, 96 if who == "reserve" else 0)

    async def execute(self, runner, timeout=2):
        return await self.pool.run(runner, "prompt", "initial", (), None, self.run, timeout, "fixture")

    async def test_preference_modes_with_deduplicated_workspaces(self):
        self.assertEqual([account.name for account in saved_accounts(self.root)], ["primary", "reserve"])
        for strategy, expected in (("active", "primary"), ("reserve", "reserve"),
                                   ("balance", "primary"), ("least", "reserve")):
            self.settings["strategy"] = strategy
            runner = AsyncMock(return_value=("initial", "done", None))
            self.assertEqual(await self.execute(runner), ("initial", "done", None))
            chosen = Path(runner.await_args.kwargs["account_env"]["CODEX_HOME"])
            self.assertEqual((chosen / "auth.json").read_text(), expected)

    async def test_live_limit_error_resumes_same_thread_once_and_keeps_auth(self):
        runner = AsyncMock(side_effect=[("new-thread", "", "usage_limit_reached"),
                                        ("new-thread", "done", None)])
        self.assertEqual(await self.execute(runner), ("new-thread", "done", None))
        first, second = runner.await_args_list
        self.assertEqual((first.args[1], second.args[1]), ("initial", "new-thread"))
        self.assertEqual((self.root / "auth.json").read_text(), "primary")
        primary_home = Path(second.kwargs["account_env"]["CODEX_HOME"])
        reserve_home = Path(first.kwargs["account_env"]["CODEX_HOME"])
        self.assertEqual(primary_home, self.root)
        self.assertEqual((reserve_home / "sessions").readlink(), self.root / "sessions")

    async def test_no_retry_after_effects_or_unrelated_failure(self):
        for error, effects in (("usage_limit_reached", True), ("generic quota path", False)):
            self.run.effects_possible = effects
            runner = AsyncMock(return_value=("initial", "", error))
            self.assertEqual((await self.execute(runner))[2], error)
            runner.assert_awaited_once()
            self.pool.cooldowns.clear()

    async def test_excluded_budget_and_bad_quota_do_not_use_saved_login(self):
        self.settings["caps"] = {self.reserve.ident: 0}
        self.pool.read_limits = AsyncMock(return_value=limits("different-workspace", 0))
        runner = AsyncMock()
        err = (await self.execute(runner))[2]
        self.assertIn("свежесть", err)
        self.assertNotIn("usage limit", err)
        runner.assert_not_awaited()

    async def test_timeout_bounds_a_quota_refresh_and_cancellation(self):
        self.pool.read_limits = AsyncMock(side_effect=lambda *_: None)
        async def stall(_):
            await asyncio.sleep(10)
        self.pool.read_limits.side_effect = stall
        runner = AsyncMock()
        result = await asyncio.wait_for(self.execute(runner, timeout=.03), 1)
        self.assertEqual(result[2], "таймаут Codex")
        runner.assert_not_awaited()
        self.assertFalse(self.pool.refresh_lock.locked())

    async def test_rejects_symlinked_credentials_and_unsafe_account_home(self):
        self.reserve.auth.unlink()
        self.reserve.auth.symlink_to(self.primary.auth)
        with self.assertRaises(OSError):
            self.pool.environment(self.reserve, {})
        self.reserve.auth.unlink()
        self.reserve.auth.write_text("reserve")
        self.reserve.auth.chmod(0o600)
        (self.root / "accounts/.nook-pool").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.pool.environment(self.reserve, {})

    def test_missing_backend_permission_cannot_be_inferred(self):
        data = limits("primary", 0)
        data["ordinaryUsageAllowed"] = None
        with self.assertRaises(ValueError):
            quota_for(data, "primary", "fixture")
