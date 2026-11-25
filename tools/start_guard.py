"""Recover an incomplete update before starting the installed bot."""
import json
from pathlib import Path
import re
import subprocess
import sys


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
    subprocess.run([sys.executable, str(worker), '--home', str(home), '_recover'], check=True, timeout=120)
    return json.loads(journal.read_text(encoding='utf-8'))['status'] == 'rolled_back'


if __name__ == '__main__':
    recover(Path(sys.argv[1]).resolve())
