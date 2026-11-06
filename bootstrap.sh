#!/usr/bin/env bash
# One-command entry point. Interactive child reads /dev/tty even when piped.
set -euo pipefail

if [[ "$(uname -s)" != Linux ]]; then
    printf '%s\n' 'Нужен сервер Linux (Ubuntu 22.04/24.04 или Debian).'
    exit 1
fi
if [[ "$EUID" == 0 ]]; then
    printf '%s\n' 'Запусти от обычного пользователя с sudo, не от root.'
    exit 1
fi
if ! ( : </dev/tty ) 2>/dev/null; then
    printf '%s\n' 'Открой интерактивный SSH-терминал: установщик будет спрашивать доступы.'
    exit 1
fi
printf '\n%s\n' '  Nook · подготовка мастера установки'
if ! command -v python3 >/dev/null; then
    printf '%s\n' '  Нужен Python. Установлю из репозиториев системы.'
    sudo -v </dev/tty
    sudo -n apt-get update </dev/tty
    sudo -n apt-get install -y python3 ca-certificates </dev/tty
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Нужен Python 3.10+.")'

umask 077
nook_stage="$(mktemp -d -t nook-install.XXXXXXXX)"
cleanup() { rm -rf -- "$nook_stage"; }
trap cleanup EXIT
printf '%s\n' '  ↓ Скачиваю чистую версию Nook…'
: "${NOOK_SOURCE_URL:?Set NOOK_SOURCE_URL to the HTTPS release source.json URL}"
[[ "$NOOK_SOURCE_URL" == https://* ]] || { echo "Source URL must use HTTPS"; exit 1; }
curl -fsSL --retry 2 --max-time 120 "$NOOK_SOURCE_URL" -o "$nook_stage/source.json"
python3 - "$nook_stage" <<'PY_SOURCE'
from pathlib import Path, PurePosixPath
import hashlib,json,sys
stage=Path(sys.argv[1]);root=stage/'source';root.mkdir()
bundle=json.loads((stage/'source.json').read_text())
for name,entry in bundle['files'].items():
    rel=PurePosixPath(name)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise SystemExit('Некорректный путь в пакете.')
    content=entry['content'].encode()
    if hashlib.sha256(content).hexdigest()!=entry['sha256']:
        raise SystemExit('Не совпала контрольная сумма: '+name)
    dest=root/Path(*rel.parts);dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_bytes(content);dest.chmod(0o755 if entry['executable'] else 0o644)
print('  ✓ Контрольные суммы исходников проверены.')
PY_SOURCE
printf '%s\n' '  ✓ Исходники загружены. Запускаю мастер.'
bash "$nook_stage/source/install.sh" "$@" </dev/tty
