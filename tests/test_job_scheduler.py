from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import job_scheduler as scheduler

ROOT = Path(__file__).resolve().parents[1]


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.home = Path(work.name)
        self.queue = self.home / 'spool' / 'jobs.json'

    def later(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'bin/later'), *args],
                              env=dict(os.environ, NOOK_HOME=str(self.home)),
                              text=True, capture_output=True)

    def test_cli_and_competing_workers_share_one_claim(self):
        self.assertEqual(self.later('2020-01-01T09:00:00Z', 'task').returncode, 0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(lambda _: scheduler.take_due(self.queue), range(2))
        self.assertEqual(len(first + second), 1)
        job = (first + second)[0]
        self.assertEqual(json.loads(self.later('--claims').stdout)[job['claim_id']]['prompt'], 'task')
        self.assertEqual(self.later('--ack-claim', job['claim_id']).returncode, 0)
        self.assertEqual(scheduler.take_due(self.queue), [])

    def test_crash_after_journaling_does_not_repeat_recurring_work(self):
        scheduler.write(self.queue, [{'id': 'daily', 'at': 10, 'every': 60, 'prompt': 'task'}])
        write = scheduler.write

        def fail_queue(path, document):
            if path == self.queue:
                raise OSError('simulated crash before replacing queue')
            write(path, document)

        with patch.object(scheduler, 'write', side_effect=fail_queue):
            with self.assertRaises(OSError):
                scheduler.take_due(self.queue, now=10)
        ticket = next(iter(scheduler.unresolved(self.queue)))
        self.assertFalse(scheduler.complete(self.queue, ticket))
        self.assertEqual(scheduler.take_due(self.queue, now=10), [])
        self.assertEqual(scheduler.take_due(self.queue, now=80), [])
        self.assertTrue(scheduler.complete(self.queue, ticket))
        self.assertEqual(scheduler.take_due(self.queue, now=80)[0]['id'], 'daily')

    def test_corrupt_queue_and_symlink_are_preserved(self):
        self.queue.parent.mkdir()
        self.queue.write_text('{')
        self.assertNotEqual(self.later('30m', 'new task').returncode, 0)
        self.assertEqual(self.queue.read_text(), '{')
        self.queue.unlink()
        outside = self.home / 'outside'
        outside.write_text('[]')
        self.queue.symlink_to(outside)
        self.assertNotEqual(self.later('30m', 'new task').returncode, 0)
        self.assertEqual(outside.read_text(), '[]')
