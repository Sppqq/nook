import unittest

from release_channels import (CHANNELS, channel_for_version, compare_versions,
                              eligible_releases, select_release)


class ReleaseChannelTests(unittest.TestCase):
    def releases(self):
        return [
            {'tag_name': 'v1.2.0', 'prerelease': False, 'draft': False, 'name': 'stable'},
            {'tag_name': 'v1.3.0-rc.1', 'prerelease': True, 'draft': False, 'name': 'rc'},
            {'tag_name': 'v1.4.0-beta.2', 'prerelease': True, 'draft': False, 'name': 'beta'},
            {'tag_name': 'v1.5.0-alpha.4', 'prerelease': True, 'draft': False, 'name': 'alpha'},
            {'tag_name': 'v1.6.0-nightly.20261124', 'prerelease': True, 'draft': False, 'name': 'nightly'},
            {'tag_name': 'v9.0.0-alpha.1', 'prerelease': True, 'draft': True, 'name': 'draft'},
            {'tag_name': 'v8.0.0-alpha.1', 'prerelease': False, 'draft': False, 'name': 'bad metadata'},
        ]

    def test_channels_are_independent_and_include_more_stable_releases(self):
        expected = {
            'stable': ['1.2.0'],
            'rc': ['1.3.0-rc.1', '1.2.0'],
            'beta': ['1.4.0-beta.2', '1.3.0-rc.1', '1.2.0'],
            'alpha': ['1.5.0-alpha.4', '1.4.0-beta.2', '1.3.0-rc.1', '1.2.0'],
            'nightly': ['1.6.0-nightly.20261124', '1.5.0-alpha.4', '1.4.0-beta.2',
                        '1.3.0-rc.1', '1.2.0'],
        }
        for channel in CHANNELS:
            with self.subTest(channel=channel):
                versions = [item['version'] for item in eligible_releases(self.releases(), channel)]
                self.assertEqual(versions, expected[channel])

    def test_semver_prerelease_order_and_build_metadata(self):
        versions = ['1.0.0-alpha.2', '1.0.0-alpha.10', '1.0.0-beta.1',
                    '1.0.0-rc.1', '1.0.0']
        for lower, higher in zip(versions, versions[1:]):
            self.assertLess(compare_versions(lower, higher), 0)
        self.assertEqual(compare_versions('1.0.0+one', '1.0.0+two'), 0)

    def test_channel_versions_and_pinned_selection(self):
        self.assertEqual(channel_for_version('1.2.0-alpha.1'), 'alpha')
        self.assertEqual(select_release(self.releases(), 'beta', '1.3.0-rc.1')['name'], 'rc')
        with self.assertRaises(ValueError):
            select_release(self.releases(), 'stable', '1.3.0-rc.1')
        with self.assertRaises(ValueError):
            channel_for_version('1.2.0-preview.1')

    def test_invalid_channel_and_leading_zero_rejected(self):
        with self.assertRaises(ValueError):
            eligible_releases(self.releases(), 'preview')
        with self.assertRaises(ValueError):
            compare_versions('1.2.0-alpha.01', '1.2.0-alpha.2')


if __name__ == '__main__':
    unittest.main()
