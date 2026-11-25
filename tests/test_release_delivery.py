from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import installer_source as installer
from nook_update import UpdateError, verify_release_provenance
from tools.release_promotion import required_predecessor


def release(version, signed=True):
    assets = [{'name': f'nook-{version}.zip'}]
    if signed:
        assets.append({'name': 'provenance.jsonl'})
    return {'tag_name': f'v{version}', 'prerelease': '-' in version, 'assets': assets}


class ReleaseDeliveryTests(unittest.TestCase):
    def test_old_cli_does_not_retry_with_weaker_provenance_flags(self):
        with tempfile.TemporaryDirectory() as name:
            cli = Path(name) / 'gh'
            cli.write_text('#!/bin/sh\nprintf "%s\\n" --bundle --signer-workflow\n')
            cli.chmod(0o700)
            with patch('nook_update.shutil.which', return_value=str(cli)):
                with self.assertRaisesRegex(UpdateError, 'GitHub CLI устарел'):
                    verify_release_provenance(Path(name) / 'release.zip', Path(name) / 'provenance.jsonl')

    def test_channel_selects_before_invoking_staged_install(self):
        versions = [release('1.0.0'), release('1.1.0-beta.1'), release('1.2.0-alpha.1')]
        with patch('nook_update.list_releases', return_value=versions), \
                patch('nook_update.fetch_release_source', return_value=(Path('/tmp/verified'), 'a' * 40)) as fetch, \
                patch.object(installer.subprocess, 'run') as run:
            run.return_value.returncode = 0
            result = installer.install_from_source(['--yes', '--channel', 'beta'], channel='beta')
        self.assertEqual(result, 0)
        self.assertEqual(fetch.call_args.args[0]['version'], '1.1.0-beta.1')
        self.assertEqual(run.call_args.args[0][1], '/tmp/verified/install.py')

    def test_migration_bootstrap_runs_only_the_verified_supervisor(self):
        version = '0.1.2-alpha.1'
        with tempfile.TemporaryDirectory() as name:
            source = Path(name)
            supervisor = source / 'legacy_migration.py'
            supervisor.touch()
            with (patch('nook_update.list_releases', return_value=[release(version)]),
                  patch('nook_update.fetch_release_source',
                        return_value=(source, 'a' * 40)) as fetch,
                  patch.object(installer.subprocess, 'run') as run):
                run.return_value.returncode = 0
                self.assertEqual(installer.migrate_from_release('/legacy home', version), 0)
                self.assertEqual(fetch.call_args.args[0]['version'], version)
                self.assertEqual(run.call_args.args[0][1:],
                                 [str(supervisor), 'migrate', '--home', '/legacy home',
                                  '--target-version', version])
                fetch.side_effect = UpdateError('signature rejected')
                run.reset_mock()
                with self.assertRaisesRegex(SystemExit, 'signature rejected'):
                    installer.migrate_from_release('/legacy home', version)
                run.assert_not_called()

    def test_exact_release_infers_channel_and_unsigned_versions_are_unavailable(self):
        version = '1.1.0-beta.1'
        self.assertEqual(installer.choose_channel(version=version), 'beta')
        self.assertEqual(installer.published([release('1.0.0'), release(version)], 'beta', version)['version'], version)
        with self.assertRaisesRegex(ValueError, 'Канал: Stable.*Beta.*--dev'):
            installer.published([release('1.0.0', signed=False), release(version)], 'stable', None)

    def test_promotion_rejects_skipped_tiers_or_versions(self):
        versions = ['v1.0.0-nightly.20261124', 'v1.0.0-alpha.1', 'v1.0.0-beta.1', 'v1.0.0-rc.1', 'v1.0.0']
        for candidate, prior in zip(versions[1:], versions):
            self.assertEqual(required_predecessor(candidate, prior), prior[1:])
        for candidate, prior in [('v1.0.0', ''), ('v1.0.0', 'v1.0.0-beta.1'),
                                 ('v1.0.0', 'v1.1.0-rc.1')]:
            with self.subTest(candidate=candidate, prior=prior), self.assertRaises(ValueError):
                required_predecessor(candidate, prior)
