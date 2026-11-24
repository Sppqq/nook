#!/usr/bin/env bash
set -euo pipefail
if [[ "$(uname -s)" != Linux || "$EUID" == 0 ]]; then
    printf '%s\n' 'Нужен Linux и обычный пользователь с sudo, не root.'
    exit 1
fi
if ! ( : </dev/tty ) 2>/dev/null; then
    printf '%s\n' 'Открой интерактивный SSH-терминал для установки.'
    exit 1
fi
if ! command -v python3 >/dev/null; then
    sudo -v </dev/tty
    sudo -n apt-get update </dev/tty
    sudo -n apt-get install -y python3 ca-certificates </dev/tty
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Нужен Python 3.11+.")'

umask 077
nook_stage="$(mktemp -d -t nook-bootstrap.XXXXXXXX)"
trap 'rm -rf -- "$nook_stage"' EXIT
python3 - "$nook_stage" <<'PY_SOURCE'
from pathlib import Path
import base64
import hashlib
import json
import sys
import urllib.request

# BEGIN PINNED LOADER
files = {
    'installer_source.py': ('6e567ca99a158bf39bd77f37e98676a0546849fb', 'c71e74089fce93f4432c32a7b216211964c6f812fa09d9ee8b789b04ea2fa1fa'),
    'nook_update.py': ('a66d59a28e82af2ad3375631569622413de390a9', 'c823081c87846887670391f6cd0a4561a2eff5f3cfdbad2366aa193bc41196a0'),
    'release_channels.py': ('9cdcc152efa696fb2d6d0d4ab0798d253ead723e', '7bdda296f51f59ff61e25dc8a8b6fb9b784620ca2fb6bf119895e34faae501f6'),
}
# END PINNED LOADER

stage = Path(sys.argv[1])
for name, (blob, checksum) in files.items():
    url = f'https://api.github.com/repos/Sppqq/nook/git/blobs/{blob}'
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = response.read(256 * 1024 + 1)
    if len(payload) > 256 * 1024:
        raise SystemExit('Загрузчик установки слишком большой.')
    data = base64.b64decode(json.loads(payload)['content'])
    if hashlib.sha256(data).hexdigest() != checksum:
        raise SystemExit('Контрольная сумма загрузчика не совпала.')
    (stage / name).write_bytes(data)
PY_SOURCE
python3 "$nook_stage/installer_source.py" "$@" </dev/tty
