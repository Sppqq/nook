"""Обновление Nook — сохраняет личные данные и проверяет запуск."""
import asyncio
import os
from pathlib import Path

from nook_update import (UpdateError, get_plan, launch, prepare, read_json,
                         request, service_matches, worker_active, workspace)


def setup(m):
    home = Path(__file__).resolve().parents[1]

    @m.button('Обновить Nook', icon='refresh', where='mods')
    @m.command('update', 'проверить обновление Nook с GitHub')
    async def check(ctx):
        await ctx.toast('Проверяю GitHub…')
        try:
            plan = await asyncio.to_thread(prepare, home)
            if not plan['changes']:
                return await ctx.reply('✅ Доступных изменений нет.')
            version = plan.get('version') or plan['revision'][:10]
            channel = plan.get('channel')
            channel_note = ('\nКанал: ' + channel) if channel else '\nКанал: прежний режим main'
            backup_note = ('Будет полная локальная копия и проверка нового окружения.'
                           if plan.get('environment_manifest') else 'Будет резервная копия кода.')
            await ctx.reply(f'🏡 **Обновление Nook**\n\nВерсия: `{version}`{channel_note}'
                f'\nФайлов программы: {len(plan["changes"])}' +
                '\n\nНастройки, память, переписка и свои дополнения сохранятся. '
                'При конфликте с местными правками обновление остановится. '
                + backup_note + ' При сбое запуска сработает откат.',
                [[m.btn('Обновить с резервной копией', 'apply', plan['id'])],
                 [m.btn('Отмена', 'cancel')]])
        except UpdateError as exc:
            await ctx.reply(str(exc))
        except Exception:
            await ctx.reply('Не удалось проверить GitHub. Бот не изменён; попробуй позже.')

    @m.callback('apply')
    async def apply(ctx):
        try:
            await asyncio.to_thread(service_matches, home, os.getpid())
            await asyncio.to_thread(request, home, ctx.arg)
            await ctx.edit('Обновление поставлено в очередь. Дождусь завершения задач, '
                           'сделаю резервную копию и перезапущусь. Результат пришлю сюда.')
        except UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('cancel')
    async def cancel(ctx):
        await ctx.edit('Предложение обновления закрыто.')

    @m.every(10)
    async def pending():
        req = workspace(home) / 'request.json'
        if not req.exists():
            return
        lanes = getattr(m.core, 'maintenance_lanes', None)
        if not lanes:
            req.unlink()
            return await m.send('Эта версия ядра ещё не поддерживает безопасный перезапуск для обновления. Нужна первоначальная миграция.')
        if any(lane.current or lane.waiting or lane.lock.locked() for lane in lanes):
            return
        # Both acquire calls complete immediately on this event loop. New agent
        # work cannot start between the idle check and acquiring these locks.
        for lane in lanes:
            await lane.lock.acquire()
        try:
            ident = read_json(req)['id']
            _, plan = await asyncio.to_thread(get_plan, home, ident)
            await asyncio.to_thread(launch, home, ident, os.getpid())
            # Keep both agent lanes reserved until the independent worker stops
            # this service. If it cannot stop us, release after a bounded wait.
            await asyncio.sleep(30)
            if plan.get('environment_manifest'):
                while await asyncio.to_thread(worker_active, ident):
                    await asyncio.sleep(10)
                return
            await m.send('Обновлятор не остановил службу вовремя. Проверь статус обновления перед повтором.')
        except UpdateError as exc:
            if req.exists():
                req.unlink()
            await m.send(str(exc))
        finally:
            for lane in reversed(lanes):
                lane.lock.release()
