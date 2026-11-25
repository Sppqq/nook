"""Recover an incomplete update before starting the installed bot."""
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType


# The v0.1.1 worker predates the _recover command. Use only its exact
# published bytes and the same locks it uses for its code-only transaction.
V011_WORKER = '105b44c1bdeb7b400f887a648c4eaf0e50fac5a283e0963a2bb97565b426a966'


def recover_v011(home, worker, source):
    legacy = ModuleType('nook_v011_updater')
    legacy.__file__ = str(worker)
    exec(compile(source, str(worker), 'exec'), vars(legacy))
    journal = legacy.path_at(legacy.workspace(home), 'transaction.json')
    with ExitStack() as locks:
        try:
            locks.enter_context(legacy.lock(home))
        except legacy.UpdateError:
            # The live worker holds the update lock while waiting for the
            # new process to start and report health.
            if legacy.read_json(journal)['status'] in {'awaiting_health', 'success'}:
                return False
            raise
        if legacy.read_json(journal)['status'] in {'applying', 'awaiting_health'}:
            locks.enter_context(legacy.runtime_lock(home))
            legacy.restore(home)
    return legacy.read_json(journal)['status'] == 'rolled_back'


def recover(home):
    base = home / 'spool' / 'updates'
    journal = base / 'transaction.json'
    try:
        transaction = json.loads(journal.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return False
    if transaction['status'] in {'success', 'rolled_back'}:
        return False
    ident = transaction['id']
    if transaction['status'] not in {'applying', 'awaiting_health'} or not re.fullmatch('[a-f0-9]{16}', ident):
        raise ValueError('Unknown Nook update transaction.')
    worker = base / 'staged' / ident / 'worker.py'
    if worker != worker.resolve() or not worker.is_file():
        raise ValueError('The frozen updater is unavailable. Recover the previous source offline.')
    source = worker.read_bytes()
    if sha256(source).hexdigest() == V011_WORKER:
        return recover_v011(home, worker, source)
    subprocess.run([sys.executable, str(worker), '--home', str(home), '_recover'], check=True, timeout=120)
    return json.loads(journal.read_text(encoding='utf-8'))['status'] == 'rolled_back'


if __name__ == '__main__':
    recover(Path(sys.argv[1]).resolve())
