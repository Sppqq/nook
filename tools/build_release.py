#!/usr/bin/env python3
"""Build a reproducible release from Git's explicit file list, never runtime data."""
import hashlib
import ipaddress
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_channels import SEMVER
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

def project_version(root=ROOT):
    version = (Path(root) / 'VERSION').read_text(encoding='ascii').strip()
    if not SEMVER.fullmatch(version):
        raise ValueError('VERSION must contain a valid SemVer version')
    return version

def build_sbom(root, version):
    root = Path(root)
    packages = {}
    requirement = re.compile(
        r'^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s;]+)$')
    for filename in ('requirements.txt', 'requirements-voice.txt'):
        path = root / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            match = requirement.fullmatch(line)
            if not match:
                raise ValueError('Unsupported dependency declaration in ' + filename)
            name, package_version = match.groups()
            key = name.casefold().replace('_', '-')
            previous = packages.get(key)
            if previous and previous != package_version:
                raise ValueError('Conflicting dependency versions: ' + name)
            packages[key] = package_version
    revision = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
        capture_output=True, text=True).stdout.strip()
    epoch = int(subprocess.run(
        ['git', 'log', '-1', '--format=%ct'], cwd=root, check=True,
        capture_output=True, text=True).stdout.strip())
    spdx_packages, relationships = [], []
    for key, package_version in sorted(packages.items()):
        ident = 'SPDXRef-Package-' + re.sub(r'[^A-Za-z0-9.-]', '-', key)
        purl = f'pkg:pypi/{key}@{package_version}'
        spdx_packages.append({
            'name': key,
            'SPDXID': ident,
            'versionInfo': package_version,
            'downloadLocation': 'NOASSERTION',
            'filesAnalyzed': False,
            'licenseConcluded': 'NOASSERTION',
            'licenseDeclared': 'NOASSERTION',
            'copyrightText': 'NOASSERTION',
            'externalRefs': [{
                'referenceCategory': 'PACKAGE-MANAGER',
                'referenceType': 'purl',
                'referenceLocator': purl,
            }],
        })
        relationships.append({
            'spdxElementId': 'SPDXRef-DOCUMENT',
            'relationshipType': 'DESCRIBES',
            'relatedSpdxElement': ident,
        })
    document = {
        'spdxVersion': 'SPDX-2.3',
        'dataLicense': 'CC0-1.0',
        'SPDXID': 'SPDXRef-DOCUMENT',
        'name': f'Nook {version} declared direct Python dependencies',
        'documentNamespace': f'https://spdx.org/spdxdocs/nook-{version}-{revision}',
        'creationInfo': {
            'created': datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'creators': ['Tool: Nook release builder'],
        },
        'packages': spdx_packages,
        'relationships': relationships,
        'comment': ('Direct, exactly pinned requirements from requirements*.txt only. '
                    'This SBOM does not include transitive dependencies or OS packages.'),
    }
    return encode(document)

def build(root=ROOT):
    version = project_version(root)
    result = subprocess.run(['git', 'ls-files', '--stage', '-z'],
                            cwd=root, check=True, capture_output=True)
    modes = {}
    for entry in filter(None, result.stdout.decode().split('\0')):
        metadata, name = entry.split('\t', 1)
        mode, _, stage = metadata.split()
        if stage != '0' or mode not in {'100644', '100755'}:
            raise ValueError('Unsupported Git index entry: ' + name)
        modes[name] = mode
    names = sorted(modes)
    if 'NOOK_RELEASE.json' in names:
        raise ValueError('NOOK_RELEASE.json is reserved for build provenance')
    if 'LICENSE' not in names or 'install.py' not in names:
        raise SystemExit('Stage the reviewed source files with git add before building.')
    files = {name: validate_file(root, name) for name in names if name != 'MANIFEST.json'}
    manifest = encode({'format': 1, 'update_api': 1, 'environment_update': 1, 'version': version, 'sha256': {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()}})
    (root / 'MANIFEST.json').write_bytes(manifest)
    files['MANIFEST.json'] = manifest
    dist = root / 'dist'
    dist.mkdir(exist_ok=True)
    archive = dist / ('nook-' + version + '.zip')
    bundle = {'format': 1, 'version': version, 'files': {}}
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(files.items()):
            executable = modes[name] == '100755'
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            output.writestr(info, data)
            bundle['files'][name] = {'content': data.decode(), 'sha256': hashlib.sha256(data).hexdigest(), 'executable': executable}
        info = zipfile.ZipInfo('NOOK_RELEASE.json', date_time=(2026, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        output.writestr(info, encode({'version': version, 'revision': revision}))
    (dist / 'source.json').write_bytes(encode(bundle))
    (dist / 'SBOM.spdx.json').write_bytes(build_sbom(root, version))
    (dist / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256((dist / name).read_bytes()).hexdigest() + '  ' + name + '\n'
        for name in [archive.name, 'source.json', 'SBOM.spdx.json']), encoding='ascii', newline='\n')
    print(f'{len(files)} files; {archive.name}; source.json; SBOM.spdx.json; SHA256SUMS')
    return files

if __name__ == '__main__':
    build()
