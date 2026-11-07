#!/usr/bin/env bash
# Download a coherent source snapshot; the wizard reads the SSH terminal.
set -euo pipefail
if [[ "$(uname -s)" != Linux || "$EUID" == 0 ]]; then
    printf '%s\n' 'Нужен Linux и обычный пользователь с sudo, не root.'
    exit 1
fi
if ! ( : </dev/tty ) 2>/dev/null; then
    printf '%s\n' 'Открой интерактивный SSH-терминал для установки.'
    exit 1
fi
printf '\n%s\n' '  🏡 Nook · готовим твой новый дом'
if ! command -v python3 >/dev/null; then
    sudo -v </dev/tty
    sudo -n apt-get update </dev/tty
    sudo -n apt-get install -y python3 ca-certificates </dev/tty
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Нужен Python 3.10+.")'
umask 077
nook_stage="$(mktemp -d -t nook-install.XXXXXXXX)"
cleanup() { rm -rf -- "$nook_stage"; }
trap cleanup EXIT
printf '%s\n' '  ↓ Загружаю исходники с GitHub…'
python3 - "$nook_stage" <<'PY_SOURCE'
from pathlib import Path, PurePosixPath
import shutil, sys, tarfile, urllib.request
stage = Path(sys.argv[1])
archive = stage / 'source.tar.gz'
url = 'https://codeload.github.com/Sppqq/nook/tar.gz/refs/heads/main'
with urllib.request.urlopen(url, timeout=120) as response, archive.open('wb') as output:
    shutil.copyfileobj(response, output)
root = stage / 'source'
root.mkdir()
with tarfile.open(archive) as source:
    for member in source:
        parts = PurePosixPath(member.name).parts
        if not parts or '..' in parts or member.name.startswith('/'):
            raise SystemExit('Некорректный путь в архиве.')
        rel = parts[1:]
        if not rel or member.isdir():
            continue
        if not member.isfile():
            raise SystemExit('Неожиданный тип файла в архиве.')
        dest = root.joinpath(*rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with source.extractfile(member) as data, dest.open('wb') as output:
            shutil.copyfileobj(data, output)
        dest.chmod(0o755 if member.mode & 0o111 else 0o644)
print('  ✓ Исходники загружены; мастер проверит контрольные суммы.')
PY_SOURCE
bash "$nook_stage/source/install.sh" "$@" </dev/tty
