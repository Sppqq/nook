"""Require a published, attested predecessor with identical application source."""
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nook_update import UpdateError, fetch_release_source, list_releases
from release_channels import CHANNELS, channel_for_version, normalize_release, parse_version


def required_predecessor(tag, previous):
    version = parse_version(tag[1:])
    channel = channel_for_version(version.text)
    if not previous and (channel == 'nightly' or tag == 'v0.1.2-alpha.1'):
        return None
    if channel == 'nightly' or not previous.startswith('v'):
        raise ValueError('Specify a published tag from the preceding channel.')
    prior = parse_version(previous[1:])
    if (channel_for_version(prior.text) != CHANNELS[CHANNELS.index(channel) + 1]
            or (version.major, version.minor, version.patch) != (prior.major, prior.minor, prior.patch)):
        raise ValueError('Promotion must follow Nightly → Alpha → Beta → RC → Stable within one version series.')
    return prior.text


def verify_promotion(tag, revision, previous):
    predecessor = required_predecessor(tag, previous)
    if predecessor is None:
        return
    release = next((item for item in map(normalize_release, list_releases())
                    if item and item['version'] == predecessor), None)
    if release is None:
        raise ValueError('The promotion predecessor has no published release.')
    with tempfile.TemporaryDirectory(prefix='nook-promotion-') as stage:
        _, prior_revision = fetch_release_source(release, Path(stage))
    subprocess.run(['git', 'merge-base', '--is-ancestor', prior_revision, revision], check=True)
    # Release metadata can change at each promotion. Application bytes and modes must match.
    subprocess.run(['git', 'diff', '--exit-code', prior_revision, revision, '--',
                    '.', ':(exclude)VERSION', ':(exclude)MANIFEST.json',
                    ':(exclude)CHANGELOG.md', ':(exclude)docs/CHANGELOG.md'], check=True)


if __name__ == '__main__':
    try:
        verify_promotion(*sys.argv[1:])
    except (OSError, ValueError, UpdateError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
