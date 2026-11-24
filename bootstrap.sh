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
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Нужен Python 3.11+.")'
umask 077
nook_stage="$(mktemp -d -t nook-install.XXXXXXXX)"
cleanup() { rm -rf -- "$nook_stage"; }
trap cleanup EXIT
printf '%s\n' '  ↓ Загружаю исходники с GitHub…'
python3 - "$nook_stage" "$@" <<'PY_SOURCE'
from pathlib import Path, PurePosixPath
import re, shutil, sys, tarfile, urllib.parse, urllib.request
stage = Path(sys.argv[1])
args = sys.argv[2:]
version = None
i = 0
while i < len(args):
    arg = args[i]
    if arg == '--version':
        i += 1
        if i >= len(args) or version is not None:
            raise SystemExit('--version requires one version and may be used only once.')
        version = args[i]
    elif arg.startswith('--version='):
        if version is not None:
            raise SystemExit('--version may be used only once.')
        version = arg.split('=', 1)[1]
    i += 1
if version is not None and not re.fullmatch(
        r'(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)'
        r'(?:-(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)'
        r'(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?'
        r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?', version):
    raise SystemExit('Некорректная SemVer-версия.')
archive = stage / 'source.tar.gz'
ref = 'refs/heads/main' if version is None else 'refs/tags/v' + version
url = 'https://codeload.github.com/Sppqq/nook/tar.gz/' + urllib.parse.quote(ref, safe='/')
with urllib.request.urlopen(url, timeout=120) as response, archive.open('wb') as output:
    size = 0
    while chunk := response.read(65536):
        size += len(chunk)
        if size > 10 * 1024 * 1024:
            raise SystemExit('Архив исходников слишком большой.')
        output.write(chunk)
root = stage / 'source'
root.mkdir()
total, seen = 0, set()
with tarfile.open(archive) as source:
    for count, member in enumerate(source):
        if count >= 2000:
            raise SystemExit('Слишком много файлов в архиве.')
        parts = PurePosixPath(member.name).parts
        if not parts or '..' in parts or member.name.startswith('/'):
            raise SystemExit('Некорректный путь в архиве.')
        rel = parts[1:]
        if not rel or member.isdir():
            continue
        if not member.isfile():
            raise SystemExit('Неожиданный тип файла в архиве.')
        name = '/'.join(rel)
        total += member.size
        if name in seen or member.size > 5 * 1024 * 1024 or total > 50 * 1024 * 1024:
            raise SystemExit('Архив слишком большой или содержит повтор файла.')
        seen.add(name)
        dest = root.joinpath(*rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with source.extractfile(member) as data, dest.open('wb') as output:
            shutil.copyfileobj(data, output)
        dest.chmod(0o755 if member.mode & 0o111 else 0o644)
print('  ✓ Исходники загружены; мастер проверит контрольные суммы.')
PY_SOURCE
bash "$nook_stage/source/install.sh" "$@" </dev/tty
