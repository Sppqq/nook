#!/usr/bin/env python3
"""Import real bot handlers and render a chart without starting services or polling."""
import asyncio
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile

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
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot
    from PIL import Image
    fig, ax = pyplot.subplots()
    ax.plot([1, 2, 3], [1, 4, 2])
    image = BytesIO()
    fig.savefig(image, format='png')
    pyplot.close(fig)
    image.seek(0)
    Image.open(image).verify()
print('Bot handlers import and chart rendering OK; no polling or provider calls.')
