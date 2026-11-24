"""Каналы обновлений и проверяемые резервные копии."""
import asyncio
import json
import os
from pathlib import Path
import re
import time

import backup_manager
import nook_update
from release_channels import CHANNEL_LABELS, eligible_releases


def setup(m):
    home = Path(__file__).resolve().parents[1]

    def settings():
        return nook_update.update_settings(home)

    def local_version():
        try:
            return json.loads((home / 'MANIFEST.json').read_text()).get('version', 'неизвестна')
        except (OSError, ValueError):
            return 'неизвестна'

    def channel_name(value):
        return CHANNEL_LABELS.get(value, 'Существующий режим (main)')

    def menu_text():
        current = settings()
        pin = current['version_pin'] or 'не закреплена'
        mode = {
            'off': 'выключено',
            'notify': 'только уведомления',
            'confirm': 'установка после подтверждения',
        }[current['auto_mode']]
        return ('🔄 **Обновления Nook**\n\n'
                f"Текущая версия: `{local_version()}`\n"
                f"Канал: **{channel_name(current['channel'])}**\n"
                f"Закреплённая версия: `{pin}`\n"
                f"Автообновления: **{mode}**\n"
                f"Проверка обновлений: **{'приостановлена' if current['paused'] else 'включена'}**\n\n"
                'Существующий режим обновления остаётся без изменений, пока владелец не выберет канал.')

    def channel_buttons():
        return [[m.btn(CHANNEL_LABELS[channel], 'choose-channel', channel)]
                for channel in ('stable', 'rc', 'beta', 'alpha', 'nightly')]

    async def show_menu(ctx):
        current = settings()
        controls = channel_buttons()
        controls.extend([
            [m.btn('Проверить обновления', 'check-release')],
            [m.btn('Выбрать версию / закрепить', 'list-releases')],
            [m.btn('История изменений', 'changelog')],
            [m.btn('Снять закрепление', 'unpin')],
            [m.btn('Автообновления', 'auto-menu')],
            [m.btn('Приостановить проверки' if not current['paused'] else 'Возобновить проверки', 'toggle-pause')],
            [m.btn('Создать и проверить резервную копию', 'backup')],
            [m.btn('Как откатиться', 'rollback-help')],
        ])
        await ctx.reply(menu_text(), controls)

    @m.button('Обновления и резервные копии', icon='refresh', where='mods')
    async def open_menu(ctx):
        await show_menu(ctx)

    @m.command('channel', 'показать каналы обновлений')
    async def channel_command(ctx):
        await show_menu(ctx)

    @m.command('version', 'показать версию и канал Nook')
    async def version_command(ctx):
        current = settings()
        await ctx.reply(f"Текущая версия: `{local_version()}`\n"
                        f"Канал: **{channel_name(current['channel'])}**\n"
                        f"Закреплено: `{current['version_pin'] or 'нет'}`")

    @m.command('releases', 'список версий для выбранного канала')
    async def releases_command(ctx):
        await list_release_choices(ctx)

    async def list_release_choices(ctx):
        current = settings()
        if current['channel'] is None:
            return await ctx.reply('Сначала выбери канал обновлений. До этого сохраняется прежний режим.')
        try:
            releases = await asyncio.to_thread(nook_update.list_releases)
            choices = eligible_releases(releases, current['channel'])[:8]
        except Exception as exc:
            m.log.warning('Release listing failed: %s', type(exc).__name__)
            return await ctx.reply('Не удалось получить релизы GitHub. Настройки не изменены.')
        if not choices:
            return await ctx.reply('Для выбранного канала пока нет опубликованных версий.')
        buttons = [[m.btn(f"{item['version']} · {CHANNEL_LABELS[item['channel']]}",
                          'pin-version', item['version'])] for item in choices]
        await ctx.reply('Выбери версию для закрепления. Это не устанавливает её автоматически:',
                        buttons)

    @m.command('changelog', 'показать изменения опубликованных релизов')
    async def changelog_command(ctx):
        await show_changelog(ctx)

    async def show_changelog(ctx):
        try:
            releases = await asyncio.to_thread(nook_update.list_releases)
            current = settings()
            channel = current['channel'] or 'nightly'
            latest = eligible_releases(releases, channel)[:5]
        except Exception as exc:
            m.log.warning('Changelog fetch failed: %s', type(exc).__name__)
            return await ctx.reply('Не удалось получить историю релизов GitHub.')
        if not latest:
            return await ctx.reply('Опубликованных изменений пока нет.')
        lines = []
        for item in latest:
            body = str(item.get('body') or 'Описание релиза не заполнено.')
            body = re.sub(r'([\\_*`\[\]])', r'\\\1', body[:1800])
            lines.append(f"**{item['version']} · {CHANNEL_LABELS[item['channel']]}**\n{body}")
        await ctx.reply('\n\n'.join(lines))

    @m.callback('choose-channel')
    async def choose_channel(ctx):
        selected = ctx.arg
        if selected not in CHANNEL_LABELS:
            return await ctx.reply('Неизвестный канал.')
        current = settings()
        warning = ('⚠️ Это экспериментальный канал: возможны ошибки и несовместимость. '
                   'Текущие файлы пользователя не заменяются при конфликте.')
        text = f"Выбрать **{CHANNEL_LABELS[selected]}**?"
        if selected != 'stable' and (current['channel'] is None or
                                     ('stable', 'rc', 'beta', 'alpha', 'nightly').index(selected) >
                                     ('stable', 'rc', 'beta', 'alpha', 'nightly').index(current['channel'])):
            text = warning + '\n\n' + text
        await ctx.reply(text, [[m.btn('Подтвердить канал', 'set-channel', selected)],
                               [m.btn('Отмена', 'back-menu')]])

    @m.callback('set-channel')
    async def set_channel(ctx):
        try:
            current = await asyncio.to_thread(nook_update.save_update_settings, home,
                                              channel=ctx.arg)
            await ctx.edit('Канал сохранён: **' + channel_name(current['channel']) +
                           '**. Установка появится только после отдельного подтверждения.')
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('back-menu')
    async def back_menu(ctx):
        await show_menu(ctx)

    @m.callback('list-releases')
    async def list_releases_callback(ctx):
        await list_release_choices(ctx)

    @m.callback('pin-version')
    async def pin_version(ctx):
        try:
            current = await asyncio.to_thread(nook_update.save_update_settings, home,
                                              version_pin=ctx.arg)
            await ctx.edit('Закреплена версия `' + current['version_pin'] +
                           '`. Для установки сначала проверь обновление и подтверди действие.',
                           [[m.btn('Проверить закреплённую версию', 'check-release')],
                            [m.btn('Меню обновлений', 'back-menu')]])
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('unpin')
    async def unpin(ctx):
        current = await asyncio.to_thread(nook_update.save_update_settings, home,
                                          clear_version_pin=True)
        await ctx.edit('Закрепление снято. Выбор версии будет следовать каналу **'
                       + channel_name(current['channel']) + '**.')

    @m.callback('check-release')
    async def check_release(ctx):
        current = settings()
        if current['channel'] is None:
            return await ctx.reply('Сначала выбери канал. Для существующей установки текущий режим сохранён.')
        await ctx.toast('Проверяю GitHub Releases…')
        try:
            plan = await asyncio.to_thread(nook_update.prepare, home, current['version_pin'])
            if not plan['changes']:
                return await ctx.reply('✅ Доступных изменений для этой версии нет.')
            release_name = plan.get('release_name') or plan.get('version') or plan['revision'][:10]
            body = str(plan.get('release_body') or '').strip()
            body = re.sub(r'([\\_*`\[\]])', r'\\\1', body[:1200])
            text = (f"**Обновление Nook · {release_name}**\n"
                    f"Версия: `{plan.get('version', 'неизвестна')}`\n"
                    f"Канал: {channel_name(plan.get('channel', current['channel']))}\n"
                    f"Файлов программы: {len(plan['changes'])}\n"
                    'Местные конфликты отменяют обновление; runtime-данные не заменяются.')
            if body:
                text += '\n\n' + body
            action = 'install-update'
            label = 'Установить с резервной копией'
            if plan.get('downgrade'):
                text += (f"\n\n⚠️ Это downgrade: `{plan['current_version']}` → "
                         f"`{plan['version']}`. Возможна несовместимость данных. "
                         'Сначала создай и проверь резервную копию.')
                action = 'confirm-downgrade'
                label = 'Я понимаю риск downgrade'
            await ctx.reply(text, [[m.btn(label, action, plan['id'])],
                                   [m.btn('Отмена', 'back-menu')]])
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('install-update')
    async def install_update(ctx):
        await queue_install(ctx)

    @m.callback('confirm-downgrade')
    async def confirm_downgrade(ctx):
        await queue_install(ctx)

    async def queue_install(ctx):
        try:
            await asyncio.to_thread(nook_update.service_matches, home, os.getpid())
            queued = await asyncio.to_thread(nook_update.request, home, ctx.arg)
            await ctx.edit('Обновление поставлено в очередь. Оно начнётся после завершения активных задач; '
                           'независимый updater проверит запуск и восстановит предыдущий код при ошибке.'
                           if queued else 'Изменений нет.')
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('auto-menu')
    async def auto_menu(ctx):
        current = settings()
        labels = {'off': 'Выключено', 'notify': 'Только уведомления',
                  'confirm': 'Установка после подтверждения'}
        rows = [[m.btn(label, 'set-auto', mode)] for mode, label in labels.items()]
        await ctx.reply('Автоматическая проверка выполняется раз в 12 часов. Установка всегда требует '
                        'явного подтверждения и проверку активных задач.\nТекущий режим: **'
                        + labels.get(current['auto_mode'], 'неподдерживается') + '**', rows)

    @m.callback('set-auto')
    async def set_auto(ctx):
        try:
            current = await asyncio.to_thread(nook_update.save_update_settings, home,
                                              auto_mode=ctx.arg)
            labels = {'off': 'выключено', 'notify': 'только уведомления',
                      'confirm': 'установка после подтверждения'}
            await ctx.edit('Режим автообновлений: **' + labels[current['auto_mode']] + '**.')
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('toggle-pause')
    async def toggle_pause(ctx):
        current = settings()
        await asyncio.to_thread(nook_update.save_update_settings, home,
                                paused=not current['paused'])
        await ctx.edit('Проверка обновлений ' +
                       ('приостановлена.' if not current['paused'] else 'возобновлена.'))

    @m.command('backup', 'создать и проверить локальную резервную копию')
    async def backup_command(ctx):
        await ctx.toast('Создаю и проверяю резервную копию…')
        try:
            path = await asyncio.to_thread(backup_manager.create_backup, home)
            report = await asyncio.to_thread(backup_manager.verify_backup, path)
            await ctx.reply(f"✅ Проверено: {report['files']} файлов, {report['bytes']} байт.\n"
                            f"Архив с секретами хранится локально в `{path.relative_to(home)}`. "
                            'Защити его правами каталога и независимой копией.')
        except backup_manager.BackupError as exc:
            await ctx.reply('Резервная копия не создана: ' + str(exc))

    @m.callback('backup')
    async def backup_callback(ctx):
        await backup_command(ctx)

    @m.command('rollback', 'инструкция по безопасному откату обновления')
    async def rollback_command(ctx):
        await show_rollback(ctx)

    @m.callback('rollback-help')
    async def rollback_callback(ctx):
        await show_rollback(ctx)

    async def show_rollback(ctx):
        await ctx.reply('Откат кода выполняется только при остановленной службе и не заменяет личные данные. '
                        'Для установок с `nook.service`:\n\n'
                        '`systemctl --user stop nook.service`\n'
                        '`bin/nook-update rollback-offline --yes`\n'
                        '`systemctl --user start nook.service`\n\n'
                        'Откат откажется перезаписывать изменения, сделанные после обновления.')

    @m.every(12 * 60 * 60)
    async def scheduled_check():
        current = settings()
        if (current['channel'] is None or current['paused']
                or current['auto_mode'] == 'off'):
            return
        today = time.strftime('%Y-%m-%d')
        if m.state.get('last_check_day') == today:
            return
        try:
            plan = await asyncio.to_thread(nook_update.prepare, home, current['version_pin'])
            m.state['last_check_day'] = today
            m.save()
            if not plan['changes']:
                return
            text = (f"Доступен Nook `{plan.get('version', 'unknown')}` "
                    f"({channel_name(plan.get('channel', current['channel']))}), "
                    f"изменено файлов: {len(plan['changes'])}.")
            buttons = [[m.btn('Проверить / установить', 'check-release')]]
            if current['auto_mode'] == 'confirm' and not plan.get('downgrade'):
                buttons = [[m.btn('Установить с проверкой', 'install-update', plan['id'])]]
            if plan.get('downgrade'):
                text += ' Это downgrade; требуется ручная проверка и отдельное подтверждение.'
            await m.send(text, buttons)
        except nook_update.UpdateError as exc:
            m.log.warning('Scheduled update check refused: %s', exc)
        except Exception as exc:
            m.log.warning('Scheduled update check failed: %s', type(exc).__name__)
