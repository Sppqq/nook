"""Каталог локальных расширений с проверкой, активацией и откатом."""
import asyncio
import hashlib
from pathlib import Path

from local_extensions import ExtensionError, Store
from nook_update import UpdateError


def version_key(version):
    return hashlib.sha256(version.encode()).hexdigest()[:12]


def setup(mod):
    store = Store(Path(__file__).resolve().parents[1])

    @mod.button('Локальные расширения', icon='mods', where='mods')
    @mod.command('extensions', 'версии локальных расширений')
    async def catalog(ctx):
        entries = await asyncio.to_thread(store.catalog)
        if not entries:
            return await ctx.reply('Локальных расширений пока нет.')
        rows = [[mod.btn(f'{kind} · {name}', 'v', f'{kind}:{name}')] for kind, name in entries[:12]]
        await ctx.reply('Локальные расширения', rows)

    @mod.callback('v')
    async def versions(ctx):
        kind, name = ctx.arg.split(':', 1)
        state = await asyncio.to_thread(store.status, kind, name)
        rows = []
        for version in list(reversed(state['versions']))[:5]:
            op, label = ('a', 'Активировать') if state['versions'][version]['checked'] else ('t', 'Проверить')
            rows.append([mod.btn(f'{label} {version}', op, f'{kind}:{name}:{version_key(version)}')])
        if state['active']:
            rows.append([mod.btn('Откатить', 'r', ctx.arg)])
        active = state['active'] or 'нет'
        await ctx.reply(f'{kind} · {name}\nАктивная версия: {active}', rows)

    async def apply(ctx, operation):
        try:
            kind, name, selected = ctx.arg.split(':', 2)
            state = await asyncio.to_thread(store.status, kind, name)
            matches = [v for v in state['versions'] if version_key(v) == selected]
            if len(matches) != 1:
                raise ExtensionError('Версия больше недоступна.')
            await asyncio.to_thread(operation, kind, name, matches[0])
            await ctx.edit(f'Готово · {kind} · {name} · {matches[0]}')
        except (ExtensionError, UpdateError, OSError, ValueError):
            await ctx.reply('Действие отклонено. Проверь версию, результаты проверки и политику владельца.')

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
            await ctx.edit(f'Активная версия: {active or "нет"}')
        except (ExtensionError, UpdateError, OSError, ValueError):
            await ctx.reply('Откат отклонён. Проверь состояние расширения.')
