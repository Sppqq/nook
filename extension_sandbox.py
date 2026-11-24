"""Linux isolation for data-only Nook extension API v1."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

BWRAP = '/usr/bin/bwrap'
PYTHON = '/usr/bin/python3'
MAX_SOURCE = 128 * 1024
MAX_JSON = 32 * 1024
RUNNER = """
import json
import resource
import runpy
import sys

resource.setrlimit(resource.RLIMIT_AS, (192 * 1024**2,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (3, 4))
resource.setrlimit(resource.RLIMIT_FSIZE, (65536,) * 2)
resource.setrlimit(resource.RLIMIT_NOFILE, (32,) * 2)
resource.setrlimit(resource.RLIMIT_NPROC, (64,) * 2)
resource.setrlimit(resource.RLIMIT_CORE, (0,) * 2)
payload = json.load(sys.stdin)
scope = runpy.run_path('/extension.py')
if type(scope.get('NOOK_EXTENSION_API')) is not int or scope['NOOK_EXTENSION_API'] != 1:
    raise ValueError('incompatible extension API')
result = scope['run'](payload)
encoded = json.dumps(result, ensure_ascii=True, allow_nan=False)
if len(encoded) > 32768:
    raise ValueError('oversize output')
sys.stdout.write(encoded)
"""


class ExtensionError(Exception):
    pass


def reject_constant(value):
    raise ValueError(value)


def encode_payload(payload):
    try:
        data = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise ExtensionError('Вход расширения должен быть JSON.') from None
    if len(data) > MAX_JSON:
        raise ExtensionError('Вход расширения слишком большой.')
    return data


def execute(source, payload):
    if not Path(BWRAP).is_file() or not Path(PYTHON).is_file():
        raise ExtensionError('Требуется системный Bubblewrap и Python 3.')
    data = encode_payload(payload)
    with tempfile.TemporaryFile() as snapshot, tempfile.TemporaryFile() as output:
        snapshot.write(source)
        snapshot.flush()
        snapshot.seek(0)
        command = [BWRAP, '--unshare-all', '--unshare-user', '--disable-userns',
                   '--assert-userns-disabled', '--new-session', '--die-with-parent',
                   '--cap-drop', 'ALL', '--clearenv', '--setenv', 'LANG', 'C.UTF-8',
                   '--setenv', 'HOME', '/tmp', '--setenv', 'PATH', '/usr/bin',
                   '--ro-bind', '/usr', '/usr', '--tmpfs', '/usr/local']
        for name in ('/lib', '/lib64'):
            if os.path.islink(name):
                command += ['--symlink', os.readlink(name), name]
            elif os.path.isdir(name):
                command += ['--ro-bind', name, name]
        command += ['--dev', '/dev', '--proc', '/proc', '--size', str(8 * 1024**2), '--tmpfs', '/tmp',
                    '--chdir', '/tmp', '--ro-bind-data', str(snapshot.fileno()), '/extension.py',
                    '--', PYTHON, '-I', '-B', '-S', '-c', RUNNER]
        try:
            completed = subprocess.run(command, input=data, stdout=output, stderr=subprocess.DEVNULL,
                                       pass_fds=(snapshot.fileno(),), timeout=12, env={'PATH': '/usr/bin'})
        except (OSError, subprocess.TimeoutExpired):
            raise ExtensionError('Ошибка запуска или таймаут изолированного расширения.') from None
        if completed.returncode:
            raise ExtensionError('Bubblewrap или расширение завершилось с ошибкой.')
        output.seek(0)
        result = output.read(MAX_JSON + 1)
    try:
        if len(result) > MAX_JSON:
            raise ValueError
        return json.loads(result, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ExtensionError('Расширение должно вернуть один ограниченный JSON-результат.') from None
