#!/usr/bin/env python3
"""Build a reproducible release from Git's explicit file list, never runtime data."""
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.0'
PRIVATE = {'memory', 'spool', 'keys', 'logs', 'inbox', 'tmp', 'dist', '.git', '.venv'}
PRIVATE_FILES = {'.nook-runtime.lock', '.nook-install.json', 'config.json', 'state.json', 'nodes.json', 'ssh_config', 'secrets.env', '.env'}
PATTERNS = (
    re.compile(r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----'),
    re.compile(r'\b\d{6,12}:[A-Za-z0-9_-]{30,}\b'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{30,}\b'),
    re.compile(r'\bsk-[A-Za-z0-9_-]{24,}\b'),
)

def validate_file(root, name):
    rel = PurePosixPath(name)
    if not rel.parts or rel.is_absolute() or '..' in rel.parts or '\\' in name:
        raise ValueError('Unsafe path: ' + name)
    if rel.parts[0] in PRIVATE or (len(rel.parts) == 1 and rel.name in PRIVATE_FILES):
        raise ValueError('Private runtime path: ' + name)
    if name.startswith('.codex/') and name != '.codex/config.toml':
        raise ValueError('Private model data: ' + name)
    if any('.bak' in p or p == '__pycache__' for p in rel.parts) or rel.suffix in {'.pyc', '.log', '.db', '.sqlite'}:
        raise ValueError('Generated/backup path: ' + name)
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('Symlink: ' + name)
    data = path.read_bytes()
    try:
        content = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Review binary manually: ' + name) from None
    if '\x00' in content or any(p.search(content) for p in PATTERNS):
        raise ValueError('Possible credential/binary: ' + name)
    for address in re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', content):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_global:
            raise ValueError('Review public IP in: ' + name)
    return data

def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()

def build(root=ROOT):
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True)
    names = sorted(set(result.stdout.decode().split('\0')) - {''})
    if 'LICENSE' not in names or 'install.py' not in names:
        raise SystemExit('Stage the reviewed source files with git add before building.')
    files = {name: validate_file(root, name) for name in names if name != 'MANIFEST.json'}
    manifest = encode({'format': 1, 'update_api': 1, 'version': VERSION, 'sha256': {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()}})
    (root / 'MANIFEST.json').write_bytes(manifest)
    files['MANIFEST.json'] = manifest
    dist = root / 'dist'
    dist.mkdir(exist_ok=True)
    archive = dist / ('nook-' + VERSION + '.zip')
    bundle = {'format': 1, 'version': VERSION, 'files': {}}
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(files.items()):
            executable = bool((root / name).stat().st_mode & 0o111)
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            output.writestr(info, data)
            bundle['files'][name] = {'content': data.decode(), 'sha256': hashlib.sha256(data).hexdigest(), 'executable': executable}
    (dist / 'source.json').write_bytes(encode(bundle))
    (dist / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256((dist / name).read_bytes()).hexdigest() + '  ' + name + '\n'
        for name in [archive.name, 'source.json']))
    print(f'{len(files)} files; {archive.name}; source.json; SHA256SUMS')
    return files

if __name__ == '__main__':
    build()
