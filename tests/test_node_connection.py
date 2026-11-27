"""Node enrollment failures must finish the status and clean up SSH children."""
import ast
import asyncio
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agent_process import stop_process_group


ROOT = Path(__file__).resolve().parents[1]


def node_functions(**extra):
    names = {"add_node", "connect_node", "copy_key", "node_output", "core_busy", "mods_loop"}
    tree = ast.parse((ROOT / "bot.py").read_text())
    body = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    namespace = dict(asyncio=asyncio, os=os, re=re, shutil=shutil, stop_process_group=stop_process_group,
                     ui_mode=lambda: "plain", E=lambda _: "",
                     edit=AsyncMock(return_value=True), post=AsyncMock(return_value=20),
                     ensure_node_key=AsyncMock(), save_node=AsyncMock(), log=Mock(),
                     NODE_KEY="/unused/key", KEYS="/unused", FACTS_SH="facts", NODE_CONNECTIONS=set(),
                     user_lane=SimpleNamespace(current=None, lock=asyncio.Lock()),
                     bg_lane=SimpleNamespace(current=None, lock=asyncio.Lock()))
    namespace.update(extra)
    exec(compile(ast.Module(body=body, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


def spec(password="test-password"):
    return dict(host="192.0.2.1", user="user", port=2203, password=password)


class NodeConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_dependency_reports_failure_before_touching_keys(self):
        ns = node_functions()
        target = spec()
        with patch.object(shutil, "which", side_effect=lambda name: None if name == "sshpass" else name):
            await ns["add_node"](1, target, 20)
        self.assertIn("sshpass", ns["edit"].call_args.args[2])
        self.assertNotIn("password", target)
        self.assertNotIn("test-password", str(ns["edit"].call_args_list))
        ns["ensure_node_key"].assert_not_awaited()
        ns["save_node"].assert_not_awaited()

    async def test_timeout_and_os_error_replace_connecting_status(self):
        for error in (asyncio.TimeoutError(), PermissionError("private details")):
            ns = node_functions()
            ns["connect_node"] = AsyncMock(side_effect=error)
            await ns["add_node"](1, spec(), 20)
            message = ns["edit"].call_args.args[2]
            self.assertNotIn("Подключаюсь", message)
            self.assertIn("Повтори" if isinstance(error, OSError) and not isinstance(error, TimeoutError)
                          else "повтори", message)
            self.assertNotIn("private details", message)

    async def test_launch_failure_clears_password_environment(self):
        ns = node_functions()
        captured = []

        async def fail(*args, **kwargs):
            captured.append(kwargs["env"])
            self.assertEqual(kwargs["env"]["SSHPASS"], "test-password")
            raise FileNotFoundError(2, "missing", "sshpass")

        with patch.object(asyncio, "create_subprocess_exec", side_effect=fail):
            with self.assertRaises(FileNotFoundError):
                await ns["copy_key"](1, 20, spec(), "test-password", [], False)
        self.assertNotIn("SSHPASS", captured[0])

    async def test_key_only_connection_does_not_require_sshpass(self):
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"host=demo\nos=Ubuntu\n", None)))
        with tempfile.TemporaryDirectory() as folder:
            ns = node_functions(KEYS=folder)
            ns["copy_key"] = AsyncMock()
            with patch.object(shutil, "which", side_effect=lambda name: None if name == "sshpass" else name), \
                    patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                await ns["add_node"](1, spec(None), 20)
        ns["copy_key"].assert_not_awaited()
        self.assertEqual(ns["save_node"].call_args.args[3], {"host": "demo", "os": "Ubuntu"})

    async def test_password_connection_saves_only_after_key_verification(self):
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"host=demo\n", None)))
        with tempfile.TemporaryDirectory() as folder:
            ns = node_functions(KEYS=folder)
            ns["copy_key"] = AsyncMock(return_value=True)
            target = spec()
            with patch.object(shutil, "which", return_value="available"), \
                    patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                await ns["add_node"](1, target, 20)
        ns["copy_key"].assert_awaited_once()
        ns["save_node"].assert_awaited_once()
        self.assertNotIn("password", ns["save_node"].call_args.args[2])

    async def test_timeout_kills_and_reaps_child(self):
        ns = node_functions()
        # Like sshpass -> ssh-copy-id -> ssh: descendants inherit the output pipe.
        script = ("import subprocess, sys; "
                  "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                  "print('ready', flush=True); p.wait()")
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
                                                       stdout=asyncio.subprocess.PIPE, start_new_session=True)
        await asyncio.wait_for(process.stdout.readline(), 3)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(ns["node_output"](process, 0.03), 3)
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdout.at_eof())

    async def test_cancellation_kills_and_reaps_child(self):
        ns = node_functions()
        # Like sshpass -> ssh-copy-id -> ssh: descendants inherit the output pipe.
        script = ("import subprocess, sys; "
                  "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                  "print('ready', flush=True); p.wait()")
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
                                                       stdout=asyncio.subprocess.PIPE, start_new_session=True)
        await asyncio.wait_for(process.stdout.readline(), 3)
        task = asyncio.create_task(ns["node_output"](process, 60))
        await asyncio.sleep(0.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdout.at_eof())

    async def test_enrollment_blocks_restart_and_releases_on_completion(self):
        for cancel in (False, True):
            ns = node_functions()
            started, finish = asyncio.Event(), asyncio.Event()

            async def connect(*args):
                started.set()
                await finish.wait()

            ns["connect_node"] = connect
            task = asyncio.create_task(ns["add_node"](1, spec(), 20))
            await started.wait()
            self.assertTrue(ns["core_busy"]())
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                finish.set()
                await task
            self.assertFalse(ns["core_busy"]())

    async def test_restart_rechecks_for_enrollment_started_during_import(self):
        ns = node_functions()
        with tempfile.TemporaryDirectory() as folder:
            flag = Path(folder) / "restart"
            flag.touch()
            ns.update(HOME=folder, REG=SimpleNamespace(failed={}), MOD_ERRS={},
                      modsys=SimpleNamespace(RESTART_FLAG=str(flag), RELOAD_FLAG=folder+"/reload"),
                      time=SimpleNamespace(time=lambda: 0), Path=Path, save_state=Mock(),
                      core=SimpleNamespace(backup_active=False))

            async def importing():
                ns["NODE_CONNECTIONS"].add("enrollment")
                return True, ""

            ns["core_import_ok"] = importing
            with patch("nook_update.heartbeat"), \
                    patch.object(asyncio, "sleep", side_effect=asyncio.CancelledError), \
                    patch.object(os, "_exit") as exit_process:
                with self.assertRaises(asyncio.CancelledError):
                    await ns["mods_loop"]()
            exit_process.assert_not_called()
            self.assertTrue(flag.exists())


if __name__ == "__main__":
    unittest.main()
