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
    'installer_source.py': ('586728e8f21268d42ec802a13faa099fadfa92aa', 'baa0a9a0ef0181cbea8c6cdf4fb4a04527b5c930d25eb1c135f28c6ffe924b5b'),
    'nook_update.py': ('fb07491100714cce52dcdbf8bc4a36c0b100b894', 'fb6e7bc620cd09c19a6a8f8a634f4249da0080df89c65ccd6975171a8bc2c8fb'),
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
