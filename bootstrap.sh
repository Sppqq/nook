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
    'nook_update.py': ('0e20bdbfebef11994953bbb63f81cc4d0bb57e8a', '6117144486fe00bff3018931cb47784a06a3dcc54ea63a515418dfaefa6ae746'),
    'release_channels.py': ('ce5ca3b34543de957466017f503737b2b16d17ff', '63b1d1524783a978ad4cf61e57e62d97627a44e7e0b594a3166cc278a371032c'),
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
