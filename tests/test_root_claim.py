from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'setup/nook-root'


class RootClaimTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.helper = {'__name__': 'test_root_helper'}
        source = SOURCE.read_text().replace('__NOOK_UID__', str(os.getuid()))
        exec(compile(source, str(SOURCE), 'exec'), self.helper)
        self.helper['CLAIMS'] = str(self.work / 'claims')

    def test_only_one_worker_can_consume_an_id(self):
        claim = self.helper['claim_once']
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: claim('deadbeef'), range(40)))
        self.assertEqual(sum(results), 1)
        self.assertEqual((self.work / 'claims').stat().st_mode & 0o777, 0o700)
        self.assertTrue(claim('a7efc012'))

    def test_claims_reject_world_readable_directory_and_symlink(self):
        claims = self.work / 'claims'
        outside = self.work / 'outside'
        outside.mkdir()
        claims.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.helper['claim_once']('deadbeef')
        self.assertEqual(list(outside.iterdir()), [])
        claims.unlink()
        claims.mkdir()
        claims.chmod(0o755)
        with self.assertRaises(PermissionError):
            self.helper['claim_once']('deadbeef')

    def test_request_rejects_a_hardlink_without_reading_the_payload(self):
        original = self.work / 'original'
        original.write_text('{"approved": true}')
        os.link(original, self.work / 'request.json')
        fd = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(PermissionError):
                self.helper['read_json'](fd, 'request.json')
        finally:
            os.close(fd)
