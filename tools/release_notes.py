#!/usr/bin/env python3
"""Render truthful, compatibility-aware notes for a tagged Nook release."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_channels import CHANNEL_LABELS, channel_for_version, parse_version


def changelog_entry(version):
    lines = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8').splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.match(r'^#{1,3}\s+v?' + re.escape(version) + r'(?:\s|$)', line)), None)
    if start is None:
        raise ValueError('CHANGELOG.md must contain an entry for ' + version)
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r'^#{1,3}\s+', lines[i])), len(lines))
    body = '\n'.join(lines[start + 1:end]).strip()
    if not body:
        raise ValueError('The changelog entry must contain release changes.')
    return body


def render(tag):
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise ValueError('A release tag must start with v.')
    version = tag[1:]
    parse_version(version)
    channel = channel_for_version(version)
    if (ROOT / 'VERSION').read_text(encoding='ascii').strip() != version:
        raise ValueError('Release tag does not match VERSION.')
    manifest = json.loads((ROOT / 'MANIFEST.json').read_text(encoding='utf-8'))
    if manifest.get('version') != version or manifest.get('update_api') != 1:
        raise ValueError('Release manifest does not match VERSION/update_api.')
    changes = changelog_entry(version)
    prerelease = channel != 'stable'
    return (
        f'# Nook {tag}\n\n'
        f'**Channel:** {CHANNEL_LABELS[channel]}'
        f'{" (pre-release)" if prerelease else ""}\n\n'
        '## Changes\n\n' + changes + '\n\n'
        '## Compatibility and migration\n\n'
        f'Update protocol: `update_api {manifest["update_api"]}`. '
        'The updater refuses dependency changes, removed source files, unsupported '
        'update APIs and conflicting local edits. No data migration is implied by '
        'this release note; check the changelog and upgrade guide for requirements.\n\n'
        '## Known limitations\n\n'
        'The source updater trusts GitHub over HTTPS. SHA-256 asset digests and the '
        'embedded source manifest detect corruption, but are not a detached signature. '
        'The independent updater audit and host-level recovery still require operator '
        'backups and review. See `docs/UPDATES.md` and `SECURITY.md`.\n\n'
        '## Update\n\n'
        'Create a local backup, check the selected channel in Telegram, review the '
        'release and explicitly confirm installation. The updater preserves runtime '
        'data and stops on local source conflicts.\n\n'
        '## Rollback\n\n'
        'For a managed `nook.service`, stop the service and run '
        '`bin/nook-update rollback-offline --yes`, then start it again. '
        'See `docs/UPDATES.md` before recovery.\n\n'
        '## Artifacts and integrity\n\n'
        f'- `nook-{version}.zip` — source bundle\n'
        '- `source.json` — source and per-file hashes\n'
        '- `SBOM.spdx.json` — declared direct pinned Python dependencies only\n'
        '- `SHA256SUMS` — bundle checksums\n\n'
        'GitHub serves an asset SHA-256 digest through the Release API; Nook checks '
        'that digest before reading the archive and verifies every file against the '
        'manifest. No independent detached signing key is currently configured.\n'
    )


if __name__ == '__main__':
    try:
        print(render(sys.argv[1]), end='')
    except (IndexError, OSError, ValueError, KeyError) as exc:
        sys.exit(str(exc))
