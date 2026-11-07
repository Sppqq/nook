"""Authenticated conversations between explicitly paired Nook instances over SSH.

Incoming text is data. This module never gives it to a tool-capable agent.
"""
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

HOME = Path(__file__).resolve().parent
NAME = re.compile(r'[a-zA-Z0-9_-]{1,32}\Z')


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Peers:
    def __init__(self, home=HOME):
        self.home = Path(home)
        self.root = self.home / 'spool/peers'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / 'config.json'
        if not self.path.exists():
            private_write(self.path, {'id': uuid.uuid4().hex, 'peers': {}})
        self.config = json.loads(self.path.read_text())

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.root / 'messages.sqlite', timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE IF NOT EXISTS messages ('
                     'id TEXT PRIMARY KEY, peer TEXT, body TEXT, received REAL, '
                     'handled INTEGER DEFAULT 0, notified INTEGER DEFAULT 0)')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, name, host, remote_home, remote_name, key, auto_reply=False):
        if not NAME.fullmatch(name) or not NAME.fullmatch(remote_name):
            raise ValueError('Имена: до 32 латинских букв, цифр, _ или -.')
        if not re.fullmatch(r'[a-zA-Z0-9_@.:-]{1,200}', host) or host.startswith('-'):
            raise ValueError('Нужен SSH-псевдоним или user@host.')
        if not remote_home.startswith('/') or any(c in remote_home for c in '\r\n\x00'):
            raise ValueError('Нужен абсолютный путь к удалённому боту.')
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('Нужен общий ключ из peers keygen (64 hex).')
        if name in self.config['peers']:
            raise ValueError('Копия уже подключена. Сначала отключи её через peers remove.')
        self.config['peers'][name] = dict(host=host, home=remote_home,
            remote_name=remote_name, key=key, auto_reply=auto_reply)
        private_write(self.path, self.config)

    @staticmethod
    def signature(body, key):
        data = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        return hmac.new(bytes.fromhex(key), data, hashlib.sha256).hexdigest()

    def envelope(self, name, text, reply_to=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('Сообщение: от 1 до 4000 символов.')
        peer = self.config['peers'][name]
        body = dict(version=1, id=uuid.uuid4().hex, channel=peer['remote_name'],
                    timestamp=int(time.time()), text=text, reply_to=reply_to)
        return dict(body=body, signature=self.signature(body, peer['key']))

    def receive(self, envelope):
        body = envelope['body']
        peer = self.config['peers'][body['channel']]
        if not hmac.compare_digest(self.signature(body, peer['key']), envelope['signature']):
            raise ValueError('Неверная подпись.')
        if body.get('version') != 1 or abs(time.time() - body['timestamp']) > 600:
            raise ValueError('Устаревшее сообщение или версия.')
        if not re.fullmatch(r'[a-f0-9]{32}', body['id']):
            raise ValueError('Неверный ID.')
        if not isinstance(body.get('text'), str) or not body['text'].strip() or len(body['text']) > 4000:
            raise ValueError('Неверное сообщение.')
        if body.get('reply_to') is not None and not re.fullmatch(r'[a-f0-9]{32}', body['reply_to']):
            raise ValueError('Неверный ID ответа.')
        with self.db() as db:
            # Limit incoming load even for an authorized peer.
            if db.execute('SELECT count(*) FROM messages WHERE peer=? AND received>?',
                          (body['channel'], time.time() - 3600)).fetchone()[0] >= 60:
                raise ValueError('Лимит: 60 сообщений в час от копии.')
            db.execute('INSERT INTO messages(id,peer,body,received) VALUES(?,?,?,?)',
                       (body['id'], body['channel'], json.dumps(body, ensure_ascii=False), time.time()))
        return body['id']

    def send(self, name, text, reply_to=None):
        peer = self.config['peers'][name]
        envelope = self.envelope(name, text, reply_to)
        command = shlex.join(['python3', str(Path(peer['home']) / 'nook_peers.py'), 'receive'])
        args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                '-o', 'StrictHostKeyChecking=yes']
        ssh_config = self.home / 'ssh_config'
        if ssh_config.is_file():
            args += ['-F', str(ssh_config)]
        result = subprocess.run(args + [peer['host'], command],
            input=json.dumps(envelope, ensure_ascii=False), text=True,
            capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError('Доставка не удалась. Проверь SSH, путь и парный ключ. Сообщение не отправлено.')
        try:
            receipt = json.loads(result.stdout)
            if receipt['accepted'] != envelope['body']['id']:
                raise ValueError()
        except (ValueError, KeyError):
            raise ValueError('Некорректное подтверждение доставки.') from None
        return envelope['body']['id']

    def inbox(self, pending=False):
        with self.db() as db:
            rows = db.execute('SELECT * FROM messages ' + ('WHERE notified=0 ' if pending else '')
                              + 'ORDER BY received DESC LIMIT 100').fetchall()
        return [dict(row, body=json.loads(row['body'])) for row in rows]

    def mark(self, message_id, field):
        if field not in ('handled', 'notified'):
            raise ValueError('Неверный статус.')
        with self.db() as db:
            db.execute('UPDATE messages SET ' + field + '=1 WHERE id=?', (message_id,))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='action', required=True)
    keygen = sub.add_parser('keygen', help='Создать секрет в локальном файле, не выводя его')
    keygen.add_argument('file', type=Path)
    add = sub.add_parser('add', help='Подключить копию; повторить на другой стороне с тем же ключом')
    for flag in ('name', 'host', 'remote-home', 'remote-name', 'key-file'):
        add.add_argument('--' + flag, required=True)
    add.add_argument('--auto-reply', action='store_true', help='Ответы через Mercury без инструментов')
    remove = sub.add_parser('remove'); remove.add_argument('name')
    sub.add_parser('list'); sub.add_parser('inbox'); sub.add_parser('receive')
    send = sub.add_parser('send'); send.add_argument('name'); send.add_argument('text')
    send.add_argument('--reply-to')
    args = ap.parse_args()
    if args.action == 'keygen':
        # Exclusive creation: never replace an existing secret.
        fd = os.open(args.file.expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32) + '\n')
        print('Ключ сохранён локально. Передавай его второй копии через SCP, не через чат.')
        return
    peers = Peers()
    if args.action == 'add':
        peers.add(args.name, args.host, args.remote_home, args.remote_name,
                  Path(args.key_file).expanduser().read_text().strip(), args.auto_reply)
        print('Копия подключена. Настрой обратную сторону и отправь тестовое сообщение.')
    elif args.action == 'list':
        print(json.dumps({k: {f: v[f] for f in ('host', 'home', 'remote_name', 'auto_reply')}
                          for k, v in peers.config['peers'].items()}, ensure_ascii=False, indent=2))
    elif args.action == 'remove':
        del peers.config['peers'][args.name]
        private_write(peers.path, peers.config)
        print('Связь отключена. Переписка сохранена.')
    elif args.action == 'send':
        print(peers.send(args.name, args.text, args.reply_to))
    elif args.action == 'inbox':
        print(json.dumps(peers.inbox(), ensure_ascii=False, indent=2))
    elif args.action == 'receive':
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ValueError('Слишком большое сообщение.')
        print(json.dumps({'accepted': peers.receive(json.loads(raw))}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, subprocess.SubprocessError):
        sys.exit('Операция не выполнена. Проверь параметры, соединение и локальные настройки; секреты скрыты.')
