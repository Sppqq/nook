#!/usr/bin/env python3
"""Dependency-free MCP stdio adapter. Runs on the VPS, normally over ssh -T."""
import json
from pathlib import Path
import sys
import time
import urllib.request
import urllib.error

BASE = 'http://127.0.0.1:18791'
TOKEN = Path('__NOOK_USER_HOME__/nook/spool/owner-api/token')


def api(path, data=None):
    req = urllib.request.Request(BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + TOKEN.read_text().strip(), 'Content-Type': 'application/json'})
    # Never send the bearer credential via an environment-configured proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
        return json.load(r)


ID_SCHEMA = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
TOOLS = [
    {'name': 'nook_send', 'description': 'Написать Nook на сервере хозяина. Возвращает request_id и conversation_id. Сохраняй conversation_id для продолжения. Ответ асинхронный: опрашивай nook_result; не отправляй запрос повторно. Для безопасного повтора передачи задай тот же request_id. Опасные действия требуют подтверждения хозяина в Telegram.',
     'inputSchema': {'type': 'object', 'properties': {'message': {'type': 'string', 'minLength': 1, 'maxLength': 12000}, 'conversation_id': ID_SCHEMA, 'request_id': ID_SCHEMA}, 'required': ['message'], 'additionalProperties': False}},
    {'name': 'nook_result', 'description': 'Получить ответ/статус уже отправленного запроса. queued/running означают подождать и опросить снова. interrupted/failed не означают, что действие не выполнялось; не повторяй его автоматически.',
     'inputSchema': {'type': 'object', 'properties': {'request_id': ID_SCHEMA, 'wait_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 20}}, 'required': ['request_id'], 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'nook_history', 'description': 'Последние 30 сообщений отдельного разговора с Nook.',
     'inputSchema': {'type': 'object', 'properties': {'conversation_id': ID_SCHEMA}, 'required': ['conversation_id'], 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
]


def valid_id(value):
    import re
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('ID must be 32 lowercase hexadecimal characters')
    return value


def call(name, a):
    if not isinstance(a, dict):
        raise ValueError('arguments must be an object')
    schema = next((t['inputSchema'] for t in TOOLS if t['name'] == name), None)
    if schema is None:
        raise ValueError('Unknown tool')
    if set(a) - set(schema['properties']) or set(schema['required']) - set(a):
        raise ValueError('Unexpected or missing arguments')
    for key in ('request_id', 'conversation_id'):
        if key in a:
            valid_id(a[key])
    if name == 'nook_send':
        return api('/v1/messages', a)
    if name == 'nook_history':
        return api('/v1/conversations/' + a['conversation_id'])
    if name == 'nook_result':
        wait = a.get('wait_seconds', 0)
        if type(wait) is not int or not 0 <= wait <= 20:
            raise ValueError('wait_seconds must be integer 0..20')
        deadline = time.monotonic() + wait
        while True:
            r = api('/v1/requests/' + a['request_id'])
            if r['status'] not in ('queued', 'running') or time.monotonic() >= deadline:
                return r
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise ValueError('Unknown tool')


def handle(q):
    if not isinstance(q, dict) or q.get('jsonrpc') != '2.0' or not isinstance(q.get('method'), str):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    if 'id' not in q:
        return None
    method, p = q['method'], q.get('params', {})
    response = {'jsonrpc': '2.0', 'id': q['id']}
    if not isinstance(p, dict):
        return {**response, 'error': {'code': -32602, 'message': 'params must be an object'}}
    if method == 'initialize':
        offered = p.get('protocolVersion')
        version = offered if offered in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25') else '2025-03-26'
        result = {'protocolVersion': version, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'nook', 'version': '1.0.0'}, 'instructions': 'Сохраняй conversation_id. После отправки опрашивай nook_result до конечного статуса; не дублируй задачи.'}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'tools/call':
        try:
            value = call(p.get('name'), p.get('arguments', {}))
            result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}
        except (ValueError, KeyError, OSError, urllib.error.URLError):
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'Ошибка аргументов или API недоступен. При сбое отправки проверь request_id и не повторяй действие с новым ID автоматически.'}]}
    else:
        return {**response, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {**response, 'result': result}


def main():
    while True:
        raw = sys.stdin.buffer.readline(262145)
        if not raw:
            break
        if len(raw) > 262144:
            break
        try:
            q = json.loads(raw)
            result = handle(q)
        except (ValueError, UnicodeError):
            result = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if result is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
