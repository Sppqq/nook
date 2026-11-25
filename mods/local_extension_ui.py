"""Каталог локальных расширений с проверкой, активацией и откатом."""
import asyncio
import hashlib
from pathlib import Path

from local_extensions import ExtensionError, Store
from nook_update import UpdateError


def version_key(version):
    return hashlib.sha256(version.encode()).hexdigest()[:12]


def setup(mod):
    t = mod.text
    store = Store(Path(__file__).resolve().parents[1])

    @mod.button('Локальные расширения', icon='mods', where='mods', english='Local extensions')
    @mod.command('extensions', 'версии локальных расширений')
    async def catalog(ctx):
        entries = await asyncio.to_thread(store.catalog)
        if not entries:
            return await ctx.reply(t('Локальных расширений пока нет.', 'No local extensions yet.'))
        rows = [[mod.btn(f'{kind} · {name}', 'v', f'{kind}:{name}')] for kind, name in entries[:12]]
        await ctx.reply(t('Локальные расширения', 'Local extensions'), rows)

    @mod.callback('v')
    async def versions(ctx):
        kind, name = ctx.arg.split(':', 1)
        state = await asyncio.to_thread(store.status, kind, name)
        rows = []
        for version in list(reversed(state['versions']))[:5]:
            op, label = (('a', t('Активировать', 'Activate')) if state['versions'][version]['checked']
                         else ('t', t('Проверить', 'Test')))
            rows.append([mod.btn(f'{label} {version}', op, f'{kind}:{name}:{version_key(version)}')])
        if state['active']:
            rows.append([mod.btn(t('Откатить', 'Roll back'), 'r', ctx.arg)])
        active = state['active'] or t('нет', 'none')
        status = t('Активная версия', 'Active version')
        await ctx.reply(f'{kind} · {name}\n{status}: {active}', rows)

    async def apply(ctx, operation):
        try:
            kind, name, selected = ctx.arg.split(':', 2)
            state = await asyncio.to_thread(store.status, kind, name)
            matches = [v for v in state['versions'] if version_key(v) == selected]
            if len(matches) != 1:
                raise ExtensionError('Версия больше недоступна.')
            await asyncio.to_thread(operation, kind, name, matches[0])
            await ctx.edit(f"{t('Готово', 'Done')} · {kind} · {name} · {matches[0]}")
        except (ExtensionError, UpdateError, OSError, ValueError):
            await ctx.reply(t('Действие отклонено. Проверь версию, результаты проверки и политику владельца.',
                              'Action denied. Check the version, test results and owner policy.'))

    @mod.callback('t')
    async def test(ctx):
        await apply(ctx, store.test)

    @mod.callback('a')
    async def activate(ctx):
        await apply(ctx, store.activate)

    @mod.callback('r')
    async def rollback(ctx):
        try:
            kind, name = ctx.arg.split(':', 1)
            active = await asyncio.to_thread(store.rollback, kind, name)
            active = active or t('нет', 'none')
            await ctx.edit(f"{t('Активная версия', 'Active version')}: {active}")
        except (ExtensionError, UpdateError, OSError, ValueError):
            await ctx.reply(t('Откат отклонён. Проверь состояние расширения.',
                              'Rollback denied. Check the extension status.'))
