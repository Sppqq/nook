"""Обновление Nook — сохраняет личные данные и проверяет запуск."""
import asyncio
import os
from pathlib import Path

from nook_update import (UpdateError, get_plan, launch, prepare, read_json,
                         request, service_matches, worker_active, workspace)


def setup(m):
    t = m.text
    home = Path(__file__).resolve().parents[1]

    @m.button('Обновить Nook', icon='refresh', where='mods', english='Update Nook')
    @m.command('update', 'проверить обновление Nook с GitHub')
    async def check(ctx):
        await ctx.toast(t('Проверяю GitHub…', 'Checking GitHub…'))
        try:
            plan = await asyncio.to_thread(prepare, home)
            if not plan['changes']:
                return await ctx.reply(t('✅ Доступных изменений нет.', '✅ No changes available.'))
            version = plan.get('version') or plan['revision'][:10]
            channel = plan.get('channel') or t('прежний режим main', 'legacy main')
            if plan.get('environment_manifest'):
                backup_note = t(
                    'Будет полная локальная копия и проверка нового окружения.',
                    'Nook will create a full local backup and verify the new environment.')
            else:
                backup_note = t('Будет резервная копия кода.', 'Nook will back up the program files.')
            text = t(
                '🏡 **Обновление Nook**\n\nВерсия: `{version}`\nКанал: {channel}\nФайлов программы: {files}\n\n'
                'Настройки, память, переписка и свои дополнения сохранятся. '
                'При конфликте с местными правками обновление остановится. {backup} '
                'При сбое запуска сработает откат.',
                '🏡 **Nook update**\n\nVersion: `{version}`\nChannel: {channel}\nProgram files: {files}\n\n'
                'Settings, memory, history and custom extensions are preserved. '
                'Local conflicts stop the update. {backup} Nook rolls back the update if startup fails.',
            ).format(version=version, channel=channel, files=len(plan['changes']), backup=backup_note)
            await ctx.reply(text, [
                [m.btn(t('Обновить с резервной копией', 'Update with backup'), 'apply', plan['id'])],
                [m.btn(t('Отмена', 'Cancel'), 'cancel')],
            ])
        except UpdateError as exc:
            await ctx.reply(str(exc))
        except Exception:
            await ctx.reply(t('Не удалось проверить GitHub. Бот не изменён; попробуй позже.',
                              'Could not check GitHub. Try again later.'))

    @m.callback('apply')
    async def apply(ctx):
        try:
            await asyncio.to_thread(service_matches, home, os.getpid())
            await asyncio.to_thread(request, home, ctx.arg)
            await ctx.edit(t(
                'Обновление поставлено в очередь. Дождусь завершения задач, '
                'сделаю резервную копию и перезапущусь. Результат пришлю сюда.',
                'Update queued. Nook will wait for tasks to finish, create a backup and restart. '
                'The result will appear here.'))
        except UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('cancel')
    async def cancel(ctx):
        await ctx.edit(t('Предложение обновления закрыто.', 'Update prompt closed.'))

    @m.every(10)
    async def pending():
        req = workspace(home) / 'request.json'
        if not req.exists():
            return
        lanes = getattr(m.core, 'maintenance_lanes', None)
        if not lanes:
            req.unlink()
            return await m.send(t(
                'Эта версия ядра ещё не поддерживает безопасный перезапуск для обновления. Нужна первоначальная миграция.',
                'This Nook installation needs an initial migration before it can safely restart for an update.'))
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
            await m.send(t(
                'Обновлятор не остановил службу вовремя. Проверь статус обновления перед повтором.',
                'The updater could not stop the service in time. Check the update status before retrying.'))
        except UpdateError as exc:
            if req.exists():
                req.unlink()
            await m.send(str(exc))
        finally:
            for lane in reversed(lanes):
                lane.lock.release()
