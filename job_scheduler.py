"""Private, crash-aware queue storage compatible with existing jobs.json."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time
import uuid

from nook_update import atomic

MAX_CLAIMS = 500
CLAIM_BATCH_SIZE = 16


def read(path, default):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return default
    with os.fdopen(fd, encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Небезопасный файл очереди.')
        return json.load(stream)


def write(path, document):
    atomic(path, (json.dumps(document, ensure_ascii=False, indent=1) + '\n').encode())


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def list_jobs(path):
    jobs = read(path, [])
    if not isinstance(jobs, list) or any(
        not isinstance(job, dict) or not isinstance(job.get('id'), str)
        or not finite(job.get('at')) or not isinstance(job.get('prompt'), str)
        or (job.get('every') is not None and (not finite(job['every']) or job['every'] < 0))
        for job in jobs
    ):
        raise ValueError('Некорректный формат очереди. Исходный файл сохранён.')
    if len({job['id'] for job in jobs}) != len(jobs):
        raise ValueError('Повторяющийся ID в очереди. Исходный файл сохранён.')
    return sorted(jobs, key=lambda job: job['at'])


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError('Небезопасный каталог очереди.')
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Небезопасная блокировка очереди.')
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def modify_jobs(path, change):
    with locked(path) as queue:
        jobs = list_jobs(queue)
        updated, result = change(jobs)
        if updated != jobs:
            write(queue, updated)
        return result


def cancel_job(path, ident):
    def remove(jobs):
        kept = [job for job in jobs if job['id'] != ident]
        return kept, len(kept) != len(jobs)

    return modify_jobs(path, remove)


def journal_path(path):
    return path.with_name(path.stem + '-claims.json')


def claims(path):
    entries = read(journal_path(path), {})
    if not isinstance(entries, dict) or any(
        not isinstance(row, dict) or not isinstance(row.get('job'), str)
        or not finite(row.get('at')) or not finite(row.get('claimed'))
        for row in entries.values()
    ):
        raise ValueError('Некорректный журнал выполнения. Повтор заблокирован.')
    return entries


def take_due(path, now=None):
    now = time.time() if now is None else now
    with locked(path) as queue:
        jobs, entries = list_jobs(queue), claims(queue)
        already_claimed = {(row['job'], row['at']) for row in entries.values()}
        held = {row['job'] for row in entries.values()}
        kept, due = [], []
        available = min(CLAIM_BATCH_SIZE, MAX_CLAIMS - len(entries))
        for job in jobs:
            occurrence = (job['id'], job['at'])
            if occurrence not in already_claimed:
                if job['at'] > now or job['id'] in held or len(due) >= available:
                    kept.append(job)
                    continue
                ticket = uuid.uuid4().hex
                entries[ticket] = {'job': job['id'], 'at': job['at'], 'claimed': now, 'prompt': job['prompt']}
                already_claimed.add(occurrence)
                due.append(dict(job, claim_id=ticket))
            if job.get('every'):
                kept.append(dict(job, at=max(job['at'] + job['every'], now + 60)))
        if due:
            write(journal_path(queue), entries)
        if kept != jobs:
            write(queue, kept)
        return due


def complete(path, ticket):
    with locked(path) as queue:
        entries = claims(queue)
        if ticket in entries:
            row = entries[ticket]
            if any(job['id'] == row['job'] and job['at'] == row['at'] for job in list_jobs(queue)):
                return False  # Keep deduplication until queue recovery has removed this occurrence.
            del entries[ticket]
            write(journal_path(queue), entries)
            return True
        return False


def unresolved(path):
    return claims(Path(path))
