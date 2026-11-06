"""Read Google quota without spending model tokens or changing CLI credentials."""
import asyncio
import datetime as dt
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TOKEN_PATH = os.path.expanduser('~/.gemini/antigravity-cli/antigravity-oauth-token')
API = 'https://cloudcode-pa.googleapis.com/v1internal:'
CACHE = {'data': None, 'at': 0.0}
LOCK = asyncio.Lock()
log = logging.getLogger('nook.antigravity_limits')


def parse_time(value):
    # Go's RFC3339Nano timestamps need truncation on Python 3.10.
    value = re.sub(r'(\.\d{6})\d+', r'\1', value)
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def read_token():
    with open(TOKEN_PATH) as f:
        return json.load(f)['token']


def refresh_token():
    # Let the official CLI handle its own OAuth refresh. This lists models;
    # it does not start an agent or send a prompt. Never log CLI output.
    subprocess.run([shutil.which('agy') or os.path.expanduser('~/.local/bin/agy'), 'models'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=20, check=True)
    return read_token()


def request(method, token):
    req = Request(API + method, data=b'{}', headers={
        'Authorization': 'Bearer ' + token['access_token'],
        'Content-Type': 'application/json', 'User-Agent': 'antigravity'})
    with urlopen(req, timeout=8) as response:
        return json.load(response)


def bucket(value, label):
    if 'remainingFraction' not in value:
        return None
    fraction = float(value['remainingFraction'])
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        return None
    return {'label': label, 'used': (1 - fraction) * 100,
            'reset': value.get('resetTime')}


def fetch():
    token = read_token()
    expiry = token.get('expiry')
    if expiry and parse_time(expiry).timestamp() < time.time() + 30:
        token = refresh_token()
    try:
        raw = request('retrieveUserQuota', token)
    except HTTPError as exc:
        if exc.code != 401:
            raise
        token = refresh_token()
        raw = request('retrieveUserQuota', token)
    # The summary supplies named weekly / five-hour windows where available.
    # The older endpoint alone must not be labelled as a daily/weekly limit.
    try:
        summary = request('retrieveUserQuotaSummary', token)
    except Exception:
        summary = {}
    groups = []
    covered = set()
    for group in summary.get('groups', []):
        name = group.get('displayName', '')
        family = 'gemini' if 'gemini' in name.lower() else '3p' if any(
            word in name.lower() for word in ('claude', 'gpt')) else None
        if not family:
            continue
        rows = []
        for entry in group.get('buckets', []):
            window = entry.get('window')
            row = bucket(entry, {'weekly': 'неделя', '5h': '5 ч'}.get(window, window or 'квота'))
            if row:
                rows.append(row)
        if rows:
            covered.add(family)
            groups.append({'name': 'Gemini' if family == 'gemini' else 'Claude / GPT', 'rows': rows})
    for family, name, prefixes in [('gemini', 'Gemini', ('gemini-',)),
                                    ('3p', 'Claude / GPT', ('claude-', 'gpt-'))]:
        if family in covered:
            continue
        rows = [bucket(x, 'квота') for x in raw.get('buckets', [])
                if x.get('modelId', '').startswith(prefixes)]
        rows = [x for x in rows if x]
        if rows:
            # Same family can contain distinct pools; don't merge unequal quotas.
            unique = {(round(x['used'], 5), x['reset']) for x in rows}
            if len(unique) == 1:
                groups.append({'name': name, 'rows': rows[:1]})
            else:
                for entry in raw.get('buckets', []):
                    if entry.get('modelId', '').startswith(prefixes):
                        row = bucket(entry, 'квота')
                        if row:
                            groups.append({'name': entry['modelId'], 'rows': [row]})
    if not groups:
        raise ValueError('No quota returned')
    return {'groups': sorted(groups, key=lambda x: x['name'] != 'Gemini'), 'at': time.time()}


async def get_limits(force=False):
    async with LOCK:
        if not force and time.time() - CACHE['at'] < 60 and CACHE['data']:
            return CACHE['data']
        try:
            data = await asyncio.to_thread(fetch)
            CACHE.update(data=data, at=time.time())
            return data
        except Exception as exc:
            log.warning('Antigravity quota unavailable (%s)', type(exc).__name__)
            if CACHE['data']:
                return dict(CACHE['data'], stale=True)
            return None


def format_limits(data, bar):
    lines = ['🤖 **Лимиты Antigravity**']
    if not data:
        return '\n'.join(lines + ['Не удалось получить лимиты Google. Попробуй обновить позже.'])
    lines.append('Проценты — использовано, как у Codex.')
    for group in data['groups']:
        lines.append('**' + group['name'] + '**')
        for row in group['rows']:
            used = row['used']
            icon = '🟥' if used >= 90 else '🟨' if used >= 70 else '🟩'
            lines.append(f"{icon} {row['label']}: {bar(used)} **{used:.1f}%**")
            if row['reset']:
                try:
                    reset = parse_time(row['reset'])
                    seconds = max(0, int(reset.timestamp() - time.time()))
                    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
                    left = f'{days} д {hours} ч' if days else f'{hours} ч {minutes} мин' if hours else f'{minutes} мин'
                    when = reset.astimezone(ZoneInfo('Europe/Moscow'))
                    lines.append(f'     сброс через {left} ({when:%d.%m %H:%M} МСК)')
                except (ValueError, TypeError):
                    pass
    if data.get('stale'):
        age = max(0, int(time.time() - data['at']) // 60)
        lines.append(f'*(обновление недоступно; последние данные {age} мин назад)*')
    return '\n'.join(lines)
