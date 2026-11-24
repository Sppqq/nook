"""Semantic-version parsing and GitHub release-channel eligibility."""
from dataclasses import dataclass
import re

CHANNELS = ('stable', 'rc', 'beta', 'alpha', 'nightly')
CHANNEL_LABELS = {
    'stable': 'Stable',
    'rc': 'Release Candidate',
    'beta': 'Beta',
    'alpha': 'Alpha',
    'nightly': 'Nightly / Dev',
}
IDENTIFIER = r'(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)'
SEMVER = re.compile(
    r'^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)'
    r'(?:-' + IDENTIFIER + r'(?:\.' + IDENTIFIER + r')*)?'
    r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$'
)
CHANNEL_SUFFIX = {
    'rc': re.compile(r'^rc\.(?:0|[1-9]\d*)$'),
    'beta': re.compile(r'^beta\.(?:0|[1-9]\d*)$'),
    'alpha': re.compile(r'^alpha\.(?:0|[1-9]\d*)$'),
    'nightly': re.compile(r'^nightly\.\d{8}$'),
}


@dataclass(frozen=True)
class Version:
    text: str
    major: int
    minor: int
    patch: int
    prerelease: tuple


def parse_version(value):
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise ValueError('Некорректная SemVer-версия.')
    core, separator, metadata = value.partition('+')
    core, dash, prerelease = core.partition('-')
    major, minor, patch = (int(part) for part in core.split('.'))
    identifiers = tuple(prerelease.split('.')) if dash else ()
    return Version(value, major, minor, patch, identifiers)


def _compare_prerelease(left, right):
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for a, b in zip(left, right):
        if a == b:
            continue
        a_numeric, b_numeric = a.isdigit(), b.isdigit()
        if a_numeric and b_numeric:
            return (int(a) > int(b)) - (int(a) < int(b))
        if a_numeric != b_numeric:
            return -1 if a_numeric else 1
        return (a > b) - (a < b)
    return (len(left) > len(right)) - (len(left) < len(right))


def compare_versions(left, right):
    """Return -1, 0 or 1 using SemVer precedence (build metadata is ignored)."""
    a, b = parse_version(left), parse_version(right)
    ac, bc = (a.major, a.minor, a.patch), (b.major, b.minor, b.patch)
    if ac != bc:
        return (ac > bc) - (ac < bc)
    return _compare_prerelease(a.prerelease, b.prerelease)


def channel_for_version(value):
    version = parse_version(value)
    if not version.prerelease:
        return 'stable'
    prefix = version.prerelease[0]
    matcher = CHANNEL_SUFFIX.get(prefix)
    suffix = '.'.join(version.prerelease)
    if matcher and matcher.fullmatch(suffix):
        return prefix
    raise ValueError('Недопустимый формат предварительной версии.')


def normalize_release(release):
    if not isinstance(release, dict) or release.get('draft') is True:
        return None
    tag = release.get('tag_name')
    if not isinstance(tag, str) or not tag.startswith('v'):
        return None
    version = tag[1:]
    try:
        channel = channel_for_version(version)
    except ValueError:
        return None
    if release.get('prerelease') is not (channel != 'stable'):
        return None
    return {**release, 'version': version, 'channel': channel}


def eligible_releases(releases, channel):
    if channel not in CHANNELS:
        raise ValueError('Неизвестный канал обновлений.')
    maximum = CHANNELS.index(channel)
    found = {}
    for release in releases:
        item = normalize_release(release)
        if item is None or CHANNELS.index(item['channel']) > maximum:
            continue
        found[item['version']] = item
    return sorted(found.values(), key=lambda item: _VersionKey(item['version']), reverse=True)


def select_release(releases, channel, version=None):
    eligible = eligible_releases(releases, channel)
    if version is not None:
        if not isinstance(version, str) or version.startswith('v'):
            raise ValueError('Укажи версию без префикса v.')
        for release in eligible:
            if release['version'] == version:
                return release
        raise ValueError('Эта версия не подходит выбранному каналу.')
    if not eligible:
        raise ValueError('Для выбранного канала нет опубликованных версий.')
    return eligible[0]


class _VersionKey:
    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return compare_versions(self.value, other.value) < 0
