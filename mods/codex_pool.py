"""Пул Codex с выбором аккаунта, бюджетами и уведомлениями о сбросе."""
import asyncio
from datetime import datetime
import time

from codex_pool import STRATEGIES

STATUS = {
    "excluded": "Вне пула",
    "unknown": "Квота неизвестна",
    "ready": "Доступен",
    "quota": "Бюджет исчерпан",
    "auth": "Требуется проверка входа",
}


def setup(m):
    pool = m.core.codex_pool_runtime
    pool.settings = m.state
    m.pool = pool

    @m.button("Пул Codex", icon="chatgpt", where="mods", key="open")
    async def show(ctx):
        accounts = pool.accounts()
        rows = [[m.btn(a.name, "account", a.ident, icon="spark" if a.active else "chatgpt")] for a in accounts]
        rows.append([m.btn("Настройки пула", "settings", icon="more")])
        text = "**Пул Codex** · " + ("включён" if pool.enabled() else "выключен")
        if not accounts:
            text += "\nДобавь сохранённые аккаунты через совместимую утилиту codex-auth."
        await (ctx.edit if ctx.cb else ctx.reply)(text, rows)

    async def settings(ctx):
        strategy = m.state.get("strategy", "active")
        rows = [[m.btn("Пул вкл" if pool.enabled() else "Пул выкл", "toggle", "enabled"),
                 m.btn("Сбросы вкл" if m.state.get("notify", False) else "Сбросы выкл", "toggle", "notify")]]
        rows.append([m.btn("Недельный прогрев вкл" if m.state.get("warm_weekly", False)
                           else "Недельный прогрев выкл", "toggle", "warm_weekly")])
        for ident, label in STRATEGIES.items():
            rows.append([m.btn(label, "strategy", ident, icon="ok" if ident == strategy else None)])
        rows.append([m.btn("Пул Codex", "open", icon="back")])
        await ctx.edit("**Порядок выбора аккаунтов**\n" + STRATEGIES.get(strategy, STRATEGIES["active"]), rows)

    m.callback("settings")(settings)

    @m.callback("toggle")
    async def toggle(ctx):
        if ctx.arg in ("enabled", "notify", "warm_weekly"):
            if ctx.arg == "enabled" and not pool.enabled() and not pool.accounts():
                return await ctx.toast("Сначала добавь сохранённые аккаунты.")
            m.state[ctx.arg] = not (m.state.get(ctx.arg, False) is True)
            m.save()
        await settings(ctx)

    @m.callback("strategy")
    async def strategy(ctx):
        if ctx.arg in STRATEGIES:
            m.state["strategy"] = ctx.arg
            m.save()
        await settings(ctx)

    @m.callback("account")
    async def details(ctx):
        ident, _, action = ctx.arg.partition(",")
        account = next((a for a in pool.accounts() if a.ident == ident), None)
        if account is None:
            return await ctx.toast("Аккаунт пропал из списка.")
        if action == "refresh":
            await ctx.toast("Обновляю квоту")
        model = m.core.codex_model()
        await pool.refresh([account], model, force=action == "refresh")
        status, _, _ = pool.status(account, model, time.time())
        cap = pool.cap(account)
        lines = [f"`{account.name}`", f"{STATUS[status]} · бюджет {cap}%"]
        cached = pool.cache.get((account, model))
        if cached and status != "unknown":
            for window in cached[1].windows:
                label = m.core.codex_window_name(window.minutes)
                remaining = max(0, cap - window.used)
                lines.append(f"**{label} · {remaining:g}% осталось**")
                if window.reset:
                    reset = datetime.fromtimestamp(window.reset).astimezone()
                    lines.append(f"Сброс · {reset:%d.%m %H:%M %Z}")
        rows = [[m.btn("Обновить", "account", ident + ",refresh"), m.btn("Бюджет", "budget", ident)],
                [m.btn("Пул Codex", "open", icon="back")]]
        await ctx.edit("\n".join(lines), rows)

    @m.callback("budget")
    async def budget(ctx):
        if not any(a.ident == ctx.arg for a in pool.accounts()):
            return await ctx.toast("Аккаунт пропал из списка.")
        rows = [[m.btn(f"{n}%", "cap", f"{ctx.arg},{n}") for n in group]
                for group in ((0, 25, 50, 70), (80, 90, 95, 100))]
        rows.append([m.btn("Аккаунт", "account", ctx.arg, icon="back")])
        await ctx.edit("**Бюджет Codex**\nПотолок расхода для каждого окна. 0% исключает аккаунт. "
                       "Новый бюджет действует на следующие запуски.", rows)

    @m.callback("cap")
    async def cap(ctx):
        ident, _, percent = ctx.arg.partition(",")
        if percent not in {"0", "25", "50", "70", "80", "90", "95", "100"}:
            return await ctx.toast("Выбери бюджет ещё раз.")
        if not any(a.ident == ident for a in pool.accounts()):
            return await ctx.toast("Аккаунт пропал из списка.")
        caps = m.state.get("caps")
        if not isinstance(caps, dict):
            caps = m.state["caps"] = {}
        caps[ident] = int(percent)
        m.save()
        await ctx.toast(f"Бюджет · {percent}%")
        await ctx.edit("Бюджет сохранён.", [[m.btn("Аккаунт", "account", ident, icon="back")]])

    @m.every(120)
    async def resets():
        notify = m.state.get("notify", False)
        warm = m.state.get("warm_weekly", False)
        if not pool.enabled() or not (notify or warm):
            return
        accounts = pool.accounts()
        model = m.core.codex_model()
        await pool.refresh(accounts, model, force=True)
        watched = m.state.get("windows", {})
        if not isinstance(watched, dict):
            watched = {}
        current = {}
        warmups = m.state.get("weekly_warmups")
        if not isinstance(warmups, dict):
            warmups = m.state["weekly_warmups"] = {}
        changed = False
        notices = []
        now = time.time()
        for account in accounts:
            cached = pool.cache.get((account, model))
            if not cached:
                prefix = account.ident + "/"
                current.update((key, value) for key, value in watched.items() if key.startswith(prefix))
                continue
            for window in cached[1].windows:
                key = f"{account.ident}/{'/'.join(window.key)}/{window.minutes}"
                previous = watched.get(key)
                if not (isinstance(previous, list) and len(previous) == 2
                        and all(type(value) in (int, float) for value in previous)):
                    previous = None
                if window.reset and window.reset <= now:
                    if previous:
                        current[key] = previous
                    continue
                current[key] = [window.reset, window.used]
                if previous and 0 < previous[0] <= now and previous[1] > 0 and previous[0] != window.reset:
                    label = m.core.codex_window_name(window.minutes)
                    if notify:
                        notices.append((key, f"`{account.name}` · {label}"))
                    if warm and window.minutes == 10080 and window.key[0] == "codex":
                        known = warmups.get(account.ident)
                        if not known or previous[0] > known[0]:
                            warmups[account.ident] = [previous[0], "pending"]
                            changed = True
        for start in range(0, len(notices), 20):
            batch = notices[start:start + 20]
            delivered = await m.send("**Лимиты Codex обновились**\n\n" + "\n".join(text for _, text in batch),
                                     [[m.btn("Пул Codex", "open")]])
            if not delivered:
                for key, _ in batch:
                    current[key] = watched[key]
        if changed or watched != current:
            m.state["windows"] = current
            m.save()

        for account in accounts:
            todo = warmups.get(account.ident)
            # Reserve the attempt before starting a paid turn. An interrupted
            # attempt stays recorded and is never sent automatically again.
            if not (isinstance(todo, list) and len(todo) == 2 and todo[1] == "pending"):
                continue
            if not pool.enabled() or not m.state.get("warm_weekly", False):
                break
            if time.time() >= todo[0] + 7 * 86400:
                todo[1] = "expired"
                m.save()
                continue
            if (pool.login_lock.locked() or account.ident in pool.busy
                    or any(lane.current or lane.waiting for lane in m.core.maintenance_lanes)
                    or pool.status(account, model, time.time())[0] != "ready"):
                continue
            async with pool.login_lock:
                pool.busy.add(account.ident)
                try:
                    todo[1] = "attempted"
                    m.save()
                    ok = await m.core.codex_pool_warmup(pool.environment(account, {}), model)
                    todo[1] = "ok" if ok else "failed"
                except (OSError, asyncio.TimeoutError):
                    todo[1] = "failed"
                finally:
                    pool.cache.pop((account, model), None)
                    pool.busy.discard(account.ident)
                    pool.changed.set()
                m.save()
