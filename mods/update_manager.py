"""Каналы обновлений и проверяемые резервные копии."""
import asyncio
import json
import os
from pathlib import Path
import re
import time

import backup_manager
import nook_update
from release_channels import CHANNEL_LABELS, eligible_releases, installable_releases

AUTO_LABELS = {
    'off': ('Выключено', 'Off'),
    'notify': ('Только уведомления', 'Notify only'),
    'confirm': ('Установка после подтверждения', 'Install after approval'),
    'install': ('Автоматическая установка новой версии', 'Install automatically'),
}
CHECK_INTERVAL = 12 * 60 * 60


def setup(m):
    t = m.text
    home = Path(__file__).resolve().parents[1]

    def settings():
        return nook_update.update_settings(home)

    def local_version():
        try:
            return json.loads((home / 'MANIFEST.json').read_text()).get('version', t('неизвестна', 'unknown'))
        except (OSError, ValueError):
            return t('неизвестна', 'unknown')

    def channel_name(value):
        return CHANNEL_LABELS.get(value, t('Существующий режим (main)', 'Legacy mode (main)'))

    def menu_text():
        current = settings()
        pin = current['version_pin'] or t('не закреплена', 'none')
        mode = t(*AUTO_LABELS[current['auto_mode']])
        status = t('приостановлена', 'paused') if current['paused'] else t('включена', 'enabled')
        return t(
            '🔄 **Обновления Nook**\n\n'
            'Текущая версия: `{version}`\nКанал: **{channel}**\n'
            'Закреплённая версия: `{pin}`\nАвтообновления: **{mode}**\n'
            'Проверка обновлений: **{status}**\n\n'
            'Существующий режим обновления остаётся без изменений, пока владелец не выберет канал.',
            '🔄 **Nook updates**\n\n'
            'Current version: `{version}`\nChannel: **{channel}**\n'
            'Pinned version: `{pin}`\nAutomatic updates: **{mode}**\n'
            'Update checks: **{status}**\n\n'
            'The existing update mode remains active until the owner chooses a channel.',
        ).format(version=local_version(), channel=channel_name(current['channel']),
                 pin=pin, mode=mode, status=status)

    def channel_buttons():
        return [[m.btn(CHANNEL_LABELS[channel], 'choose-channel', channel)]
                for channel in ('stable', 'rc', 'beta', 'alpha', 'nightly')]

    async def show_menu(ctx):
        current = settings()
        pause = (t('Возобновить проверки', 'Resume checks') if current['paused']
                 else t('Приостановить проверки', 'Pause checks'))
        controls = channel_buttons()
        controls.extend([
            [m.btn(t('Проверить обновления', 'Check for updates'), 'check-release')],
            [m.btn(t('Выбрать версию / закрепить', 'Choose or pin a version'), 'list-releases')],
            [m.btn(t('История изменений', 'Release notes'), 'changelog')],
            [m.btn(t('Снять закрепление', 'Unpin version'), 'unpin')],
            [m.btn(t('Автообновления', 'Automatic updates'), 'auto-menu')],
            [m.btn(pause, 'toggle-pause')],
            [m.btn(t('Создать и проверить резервную копию', 'Create and verify backup'), 'backup')],
            [m.btn(t('Как откатиться', 'Rollback instructions'), 'rollback-help')],
        ])
        await ctx.reply(menu_text(), controls)

    @m.button('Обновления и резервные копии', icon='refresh', where='mods', english='Updates and backups')
    async def open_menu(ctx):
        await show_menu(ctx)

    @m.command('channel', 'показать каналы обновлений')
    async def channel_command(ctx):
        await show_menu(ctx)

    @m.command('version', 'показать версию и канал Nook')
    async def version_command(ctx):
        current = settings()
        await ctx.reply(t(
            'Текущая версия: `{version}`\nКанал: **{channel}**\nЗакреплено: `{pin}`',
            'Current version: `{version}`\nChannel: **{channel}**\nPinned version: `{pin}`',
        ).format(version=local_version(), channel=channel_name(current['channel']),
                 pin=current['version_pin'] or t('нет', 'none')))

    @m.command('releases', 'список версий для выбранного канала')
    async def releases_command(ctx):
        await list_release_choices(ctx)

    async def list_release_choices(ctx):
        current = settings()
        if current['channel'] is None:
            return await ctx.reply(t('Сначала выбери канал обновлений. До этого сохраняется прежний режим.', 'Choose an update channel first. The existing mode remains active until then.'))
        try:
            releases = await asyncio.to_thread(nook_update.list_releases)
            choices = eligible_releases(installable_releases(releases), current['channel'])[:8]
        except Exception as exc:
            m.log.warning('Release listing failed: %s', type(exc).__name__)
            return await ctx.reply(t('Не удалось получить релизы GitHub. Настройки не изменены.', 'Could not fetch GitHub releases. Settings are unchanged.'))
        if not choices:
            return await ctx.reply(t('Для выбранного канала пока нет опубликованных версий.', 'This channel has no published releases yet.'))
        buttons = [[m.btn(f"{item['version']} · {CHANNEL_LABELS[item['channel']]}",
                          'pin-version', item['version'])] for item in choices]
        await ctx.reply(t('Выбери версию для закрепления. Это не устанавливает её автоматически:', 'Choose a version to pin for future update checks.'),
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
            return await ctx.reply(t('Не удалось получить историю релизов GitHub.', 'Could not fetch GitHub release notes.'))
        if not latest:
            return await ctx.reply(t('Опубликованных изменений пока нет.', 'No release notes have been published yet.'))
        lines = []
        for item in latest:
            body = str(item.get('body') or t('Описание релиза не заполнено.', 'No release description was provided.'))
            body = re.sub(r'([\\_*`\[\]])', r'\\\1', body[:1800])
            lines.append(f"**{item['version']} · {CHANNEL_LABELS[item['channel']]}**\n{body}")
        await ctx.reply('\n\n'.join(lines))

    @m.callback('choose-channel')
    async def choose_channel(ctx):
        selected = ctx.arg
        if selected not in CHANNEL_LABELS:
            return await ctx.reply(t('Неизвестный канал.', 'Unknown channel.'))
        current = settings()
        warning = (t('⚠️ Это экспериментальный канал: возможны ошибки и несовместимость. '
                   'Текущие файлы пользователя не заменяются при конфликте.', '⚠️ Experimental releases may have bugs or compatibility issues. Conflicts stop the update and preserve user files.'))
        text = t("Выбрать **{}**?", "Choose **{}**?").format(CHANNEL_LABELS[selected])
        if selected != 'stable' and (current['channel'] is None or
                                     ('stable', 'rc', 'beta', 'alpha', 'nightly').index(selected) >
                                     ('stable', 'rc', 'beta', 'alpha', 'nightly').index(current['channel'])):
            text = warning + '\n\n' + text
        await ctx.reply(text, [[m.btn(t('Подтвердить канал', 'Confirm channel'), 'set-channel', selected)],
                               [m.btn(t('Отмена', 'Cancel'), 'back-menu')]])

    @m.callback('set-channel')
    async def set_channel(ctx):
        try:
            current = await asyncio.to_thread(nook_update.save_update_settings, home,
                                              channel=ctx.arg)
            await ctx.edit(t(
                'Канал сохранён: **{}**. Установка появится только после отдельного подтверждения.',
                'Channel saved: **{}**. Choose an update to confirm installation.',
            ).format(channel_name(current['channel'])))
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
            text = t(
                'Закреплена версия `{}`. Для установки сначала проверь обновление и подтверди действие.',
                'Pinned version `{}`. Check for updates and confirm to install it.',
            ).format(current['version_pin'])
            await ctx.edit(text, [
                [m.btn(t('Проверить закреплённую версию', 'Check the pinned version'), 'check-release')],
                [m.btn(t('Меню обновлений', 'Update menu'), 'back-menu')],
            ])
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('unpin')
    async def unpin(ctx):
        current = await asyncio.to_thread(nook_update.save_update_settings, home,
                                          clear_version_pin=True)
        await ctx.edit(t(
            'Закрепление снято. Выбор версии будет следовать каналу **{}**.',
            'Version unpinned. Update checks follow the **{}** channel.',
        ).format(channel_name(current['channel'])))

    @m.callback('check-release')
    async def check_release(ctx):
        current = settings()
        if current['channel'] is None:
            return await ctx.reply(t('Сначала выбери канал. Для существующей установки текущий режим сохранён.', 'Choose a channel first. This installation keeps its existing update mode until then.'))
        await ctx.toast(t('Проверяю GitHub Releases…', 'Checking GitHub Releases…'))
        try:
            plan = await asyncio.to_thread(nook_update.prepare, home, current['version_pin'])
            if not plan['changes']:
                return await ctx.reply(t('✅ Доступных изменений для этой версии нет.', '✅ No changes available for this version.'))
            release_name = plan.get('release_name') or plan.get('version') or plan['revision'][:10]
            body = str(plan.get('release_body') or '').strip()
            body = re.sub(r'([\\_*`\[\]])', r'\\\1', body[:1200])
            text = t(
                '**Обновление Nook · {release}**\nВерсия: `{version}`\n'
                'Канал: {channel}\nФайлов программы: {files}\n'
                'Местные конфликты отменяют обновление; runtime-данные не заменяются.',
                '**Nook update · {release}**\nVersion: `{version}`\n'
                'Channel: {channel}\nProgram files: {files}\n'
                'Local conflicts stop the update and preserve runtime data.',
            ).format(release=release_name, version=plan.get('version', t('неизвестна', 'unknown')),
                     channel=channel_name(plan.get('channel', current['channel'])), files=len(plan['changes']))
            if plan.get('environment_manifest'):
                text += t('\nБудут подготовлены новое окружение и полная локальная резервная копия.', '\nNook will prepare a new environment and a full local backup.')
            if body:
                text += '\n\n' + body
            action = 'install-update'
            label = t('Установить с резервной копией', 'Install with backup')
            if plan.get('downgrade'):
                text += t(
                    '\n\n⚠️ Это downgrade: `{current}` → `{target}`. Возможна несовместимость данных. '
                    'Сначала создай и проверь резервную копию.',
                    '\n\n⚠️ Downgrade from `{current}` to `{target}`. Data may be incompatible. '
                    'Create and verify a backup first.',
                ).format(current=plan['current_version'], target=plan['version'])
                action = 'confirm-downgrade'
                label = t('Я понимаю риск downgrade', 'I understand the downgrade risk')
            await ctx.reply(text, [[m.btn(label, action, plan['id'])],
                                   [m.btn(t('Отмена', 'Cancel'), 'back-menu')]])
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('install-update')
    async def install_update(ctx):
        await queue_install(ctx)

    @m.callback('confirm-downgrade')
    async def confirm_downgrade(ctx):
        await queue_install(ctx, allow_downgrade=True)

    async def queue_install(ctx, *, allow_downgrade=False):
        try:
            await asyncio.to_thread(nook_update.service_matches, home, os.getpid())
            queued = await asyncio.to_thread(nook_update.request, home, ctx.arg, allow_downgrade=allow_downgrade)
            text = t(
                'Обновление поставлено в очередь. Оно начнётся после завершения активных задач; '
                'независимый updater проверит запуск и откатит изменения при ошибке.',
                'Update queued. After active tasks finish, the updater will check startup and roll back if it fails.',
            ) if queued else t('Изменений нет.', 'No changes.')
            await ctx.edit(text)
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('auto-menu')
    async def auto_menu(ctx):
        current = settings()
        rows = [[m.btn(t(*labels), 'set-auto', mode)] for mode, labels in AUTO_LABELS.items()]
        await ctx.reply(t(
            'Проверка выполняется раз в 12 часов. Автоустановка ждёт завершения задач и '
            'проверяет запуск. Для перехода на старую версию нужно отдельное подтверждение.\n'
            'Текущий режим: **{}**',
            'Nook checks every 12 hours. Automatic installation waits for tasks to finish and verifies startup. '
            'Downgrades require separate approval.\nCurrent mode: **{}**',
        ).format(t(*AUTO_LABELS[current['auto_mode']])), rows)

    @m.callback('set-auto')
    async def set_auto(ctx):
        try:
            current = await asyncio.to_thread(nook_update.save_update_settings, home,
                                              auto_mode=ctx.arg)
            await ctx.edit(t('Режим автообновлений: **', 'Automatic updates: **') + t(*AUTO_LABELS[current['auto_mode']]) + '**.')
        except nook_update.UpdateError as exc:
            await ctx.reply(str(exc))

    @m.callback('toggle-pause')
    async def toggle_pause(ctx):
        current = settings()
        await asyncio.to_thread(nook_update.save_update_settings, home,
                                paused=not current['paused'])
        await ctx.edit(t('Проверка обновлений ', 'Update checks ') +
                       (t('приостановлена.', 'paused.') if not current['paused'] else t('возобновлена.', 'resumed.')))

    @m.command('backup', 'создать и проверить локальную резервную копию')
    async def backup_command(ctx):
        lanes = getattr(m.core, 'maintenance_lanes', ())
        if not lanes or any(lane.current or lane.waiting or lane.lock.locked() for lane in lanes):
            return await ctx.reply(t('Дождись завершения задач Nook и повтори создание резервной копии.', 'Wait for Nook tasks to finish and try creating the backup again.'))
        # Reserve both agent lanes before yielding to queued agent work.
        for lane in lanes:
            await lane.lock.acquire()
        m.core.backup_active = True
        try:
            await ctx.toast(t('Создаю и проверяю резервную копию…', 'Creating and verifying a backup…'))
            task = asyncio.create_task(asyncio.to_thread(backup_manager.create_backup, home, online=True))
            try:
                path = await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise
            report = await asyncio.to_thread(backup_manager.verify_backup, path)
            await ctx.reply(t(
                '✅ Проверено: {files} файлов, {size} байт.\n'
                'Архив с секретами хранится локально в `{path}`. '
                'Защити его правами каталога и независимой копией.',
                '✅ Verified {files} files, {size} bytes.\n'
                'The local archive at `{path}` contains secrets. Keep it in a restricted directory '
                'and maintain a separate backup copy.',
            ).format(files=report['files'], size=report['bytes'], path=path.relative_to(home)))
        except backup_manager.BackupError as exc:
            await ctx.reply(t('Резервная копия не создана: ', 'Could not create the backup: ') + str(exc))
        finally:
            m.core.backup_active = False
            for lane in reversed(lanes):
                lane.lock.release()

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
        await ctx.reply(
            t('Откат кода выполняется только при остановленной службе и не заменяет личные данные. '
              'Для установок с `nook.service`:\n\n',
              'Stop the service before rolling back the code. Rollback preserves personal data. '
              'For installations using `nook.service`:\n\n')
            + '`systemctl --user stop nook.service`\n'
              '`bin/nook-update rollback-offline --yes`\n'
              '`systemctl --user start nook.service`\n\n'
            + t('Откат откажется перезаписывать изменения, сделанные после обновления.',
                'Rollback preserves new local changes and stops if those changes conflict.'))

    @m.every(CHECK_INTERVAL)
    async def scheduled_check():
        current = settings()
        if (current['channel'] is None or current['paused']
                or current['auto_mode'] == 'off'):
            return
        now = time.time()
        if now - m.state.get('last_check', 0) < CHECK_INTERVAL:
            return
        try:
            plan = await asyncio.to_thread(nook_update.prepare, home, current['version_pin'])
            m.state['last_check'] = now
            m.save()
            if not plan['changes']:
                return
            text = t(
                'Доступен Nook `{version}` ({channel}), изменено файлов: {files}.',
                'Nook `{version}` is available ({channel}), with {files} changed files.',
            ).format(version=plan.get('version', 'unknown'),
                     channel=channel_name(plan.get('channel', current['channel'])), files=len(plan['changes']))
            buttons = [[m.btn(t('Проверить / установить', 'Review or install'), 'check-release')]]
            if current['auto_mode'] == 'confirm' and not plan.get('downgrade'):
                buttons = [[m.btn(t('Установить с проверкой', 'Install and verify'), 'install-update', plan['id'])]]
            if plan.get('downgrade'):
                text += t(' Это downgrade; требуется ручная проверка и отдельное подтверждение.', ' Downgrades require manual review and separate confirmation.')
            elif current['auto_mode'] == 'install' and m.state.get('last_auto_revision') != plan['revision']:
                await asyncio.to_thread(nook_update.service_matches, home, os.getpid())
                if await asyncio.to_thread(nook_update.request, home, plan['id']):
                    m.state['last_auto_revision'] = plan['revision']
                    m.save()
                    return await m.send(text + t(' Установка поставлена в очередь.', ' Installation queued.'))
            await m.send(text, buttons)
        except nook_update.UpdateError as exc:
            m.log.warning('Scheduled update check refused: %s', exc)
        except Exception as exc:
            m.log.warning('Scheduled update check failed: %s', type(exc).__name__)
