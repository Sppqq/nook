"""Pin the small Python loader to Git blobs and SHA-256 in bootstrap.sh."""
import argparse
import hashlib
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('installer_source.py', 'nook_update.py', 'release_channels.py')


def refresh():
    path = ROOT / 'bootstrap.sh'
    original = path.read_text(encoding='utf-8')
    lines = ['# BEGIN PINNED LOADER', 'files = {']
    for name in MODULES:
        data = (ROOT / name).read_bytes().replace(b'\r\n', b'\n')
        blob = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
        lines.append(f"    '{name}': ('{blob}', '{hashlib.sha256(data).hexdigest()}'),")
    lines.extend(['}', '# END PINNED LOADER'])
    updated, count = re.subn(r'# BEGIN PINNED LOADER\n.*?\n# END PINNED LOADER',
                             '\n'.join(lines), original, flags=re.S)
    if count != 1:
        raise SystemExit('Expected exactly one pinned loader block in bootstrap.sh.')
    return path, original, updated


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path, original, updated = refresh()
    if args.check:
        if original != updated:
            sys_exit = 'Bootstrap pins are stale. Run python tools/sync_bootstrap.py.'
            raise SystemExit(sys_exit)
    elif original != updated:
        path.write_text(updated, encoding='utf-8', newline='\n')
