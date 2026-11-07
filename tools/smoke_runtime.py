#!/usr/bin/env python3
"""Import real bot handlers and render a chart without starting services or polling."""
import asyncio
import datetime as dt
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
with tempfile.TemporaryDirectory(prefix='nook-smoke-') as name:
    home = Path(name)
    token = '123456:' + 'X' * 35
    (home / 'config.json').write_text(json.dumps({'bot_token': token, 'owner_id': 7}))
    env = {'__file__': str(home / 'bot.py'), '__name__': 'nook_runtime_smoke'}
    exec(compile((ROOT / 'bot.py').read_text(), str(ROOT / 'bot.py'), 'exec'), env)
    assert env['STATE']['owner_id'] == 7
    asyncio.run(env['bot'].session.close())
    from PIL import Image
    # Exercise Nook's actual chart APIs, including Cyrillic labels, gaps,
    # thresholds, date formatting and multi-host rendering. Never access a host.
    now = dt.datetime.now()
    series = [(now - dt.timedelta(minutes=10 - i),
               {metric: 95 if i == 9 else i * 8 for metric, _ in env['METRICS']})
              for i in range(10)]
    series[4] = (series[4][0], None)
    rows = [(int(t.timestamp()), 20, 40, 60, 0) for t, _ in series]
    with patch.dict(env, metrics_series=lambda host, period: series,
                    metrics_get=lambda host, since: rows,
                    guard_label=lambda host: 'Тестовый сервер ' + host), \
            patch.object(env['guard'], 'targets', return_value=['local', 'test']):
        images = [env['render_graph']('local', 'h'),
                  env['render_overview']('d'), env['render_disk_all']()]
        for png in images:
            assert png and png.startswith(bytes.fromhex('89504e470d0a1a0a'))
            image = Image.open(BytesIO(png))
            assert image.width >= 800 and image.height >= 300
            image.verify()
print('Bot import and all three real chart renderers OK; no polling or provider calls.')
