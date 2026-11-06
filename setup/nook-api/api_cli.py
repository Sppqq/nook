#!/usr/bin/env python3
"""HTTP API through SSH without exporting the server-side token.

ssh ... python3 __NOOK_USER_HOME__/nook/setup/nook-api/api_cli.py /health
POST: JSON body on stdin, path /v1/messages.
"""
import json
import re
import sys
from mcp_server import api

if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) == 2 else '/health'
    if not re.fullmatch(r'/health|/v1/messages|/v1/(?:requests|conversations)/[a-f0-9]{32}', path):
        sys.exit('Unsupported endpoint')
    data = None
    if path == '/v1/messages':
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            sys.exit('Body too large')
        data = json.loads(raw)
    try:
        print(json.dumps(api(path, data), ensure_ascii=False))
    except Exception:
        sys.exit('API request failed; check endpoint, input and server availability. Do not retry mutations with a new request_id automatically.')
