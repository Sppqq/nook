"""Выбор сохранённых аккаунтов Codex и вход в новый."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import time


def account_id(account):
    return hashlib.sha256(account['account_key'].encode()).hexdigest()[:16]


async def cli(*args, json_output=True):
    proc = await asyncio.create_subprocess_exec(
        'codex-auth', *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode:
        raise RuntimeError('codex-auth command failed')
    return json.loads(out) if json_output else out.decode(errors="replace")


async def accounts():
    return (await cli('list', '--skip-api', '--json')).get('accounts', [])


def setup(m):
    lock = asyncio.Lock()
    # Core exports its login handler; use that handler's lock/state persistence
    # so device login and saved-account switching cannot overwrite each other.
    core = m.core.codex_login.__globals__

    async def show(ctx, *, edit=False):
        try:
            stored = await accounts()
        except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
            return await ctx.toast('Не удалось прочитать аккаунты Codex. Попробуй позже.')
        rows = []
        for a in stored:
            name = a.get('alias') or a.get('email') or f"Аккаунт {a['number']}"
            label = ('✅ ' if a.get('active') else '') + name[:46]
            rows.append([m.btn(label, 'select', account_id(a))])
        rows.append([m.btn('Войти в новый аккаунт', 'login')])
        rows.append([m.btn('Закрыть', 'close')])
        text = 'Выбери аккаунт Codex. ✅ — текущий. Основная модель бота при этом не меняется.' if stored else 'Сохранённых аккаунтов пока нет.'
        await (ctx.edit if edit else ctx.reply)(text, rows)

    @m.button('Сменить аккаунт Codex', icon='refresh', where='menu')
    async def switch(ctx):
        await show(ctx)

    @m.callback('select')
    async def select(ctx):
        login_lock = core['CODEX_LOGIN_LOCK']
        if lock.locked() or login_lock.locked():
            return await ctx.toast('Смена аккаунта уже выполняется. Дождись её завершения.')
        if any(getattr(core.get(name), 'current', None) for name in ('user_lane', 'bg_lane')):
            return await ctx.toast('Сначала дождись завершения текущей задачи, затем выбери аккаунт снова.')
        async with lock, login_lock:
            backup = None
            target = Path.home()/'.codex/auth.json'
            try:
                stored = await accounts()
                chosen = next((a for a in stored if account_id(a) == ctx.arg), None)
                if chosen is None:
                    return await ctx.toast('Список аккаунтов изменился — открой его снова.')
                if chosen.get('active'):
                    await show(ctx, edit=True)
                    return await ctx.toast('Этот аккаунт Codex уже выбран.')
                if target.exists():
                    backup = target.with_name('auth.json.bak-switch-' + str(time.time_ns()))
                    shutil.copy2(target, backup)
                    backup.chmod(0o600)
                await cli('switch', str(chosen['number']), '--json')
                current = await accounts()
                if not any(a.get('active') and account_id(a) == ctx.arg for a in current):
                    raise RuntimeError('account switch not confirmed')
                target.chmod(0o600)
                thread = str(m.core.state.get('main_thread') or '')
                if not thread.startswith(('merc-', 'agy-')):
                    m.core.state['main_thread'] = None
                m.core.state['fallback_until'] = 0
                core['LIMITS_CACHE'].update(data=None, at=0.0)
                core['save_state']()
            except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
                if backup is not None:
                    temp = target.with_name('.auth-restore-' + str(time.time_ns()))
                    shutil.copy2(backup, temp)
                    temp.chmod(0o600)
                    os.replace(temp, target)
                return await ctx.toast('Не удалось переключить Codex. Предыдущий вход сохранён.')
        await show(ctx, edit=True)
        await ctx.toast('Аккаунт Codex переключён')

    @m.callback('close')
    async def close(ctx):
        await ctx.toast('Закрыто')
        if ctx.cb:
            await ctx.cb.message.delete()

    @m.callback('login')
    async def login(ctx):
        async def run():
            await m.core.codex_login(ctx.chat)
            try:
                await cli('import', str(Path.home()/'.codex/auth.json'), json_output=False)
            except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
                return await ctx.toast('Не удалось добавить вход в список аккаунтов. Сам вход Codex сохранён.')
            await show(ctx, edit=True)
        await ctx.toast('Открываю вход в новый аккаунт')
        asyncio.create_task(run())
