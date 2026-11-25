"""Пул Codex с выбором аккаунта, бюджетами и уведомлениями о сбросе."""
import asyncio
from datetime import datetime
import time

from codex_pool import STRATEGIES

STATUS = {
    "excluded": ("Вне пула", "Excluded from pool"),
    "unknown": ("Квота неизвестна", "Quota unavailable"),
    "ready": ("Доступен", "Available"),
    "quota": ("Бюджет исчерпан", "Budget exhausted"),
    "auth": ("Требуется проверка входа", "Sign-in needs attention"),
}
ENGLISH_STRATEGIES = {
    "active": "Primary first",
    "reserve": "Primary as backup",
    "balance": "Most quota remaining first",
    "least": "Least quota remaining first",
}


def setup(m):
    pool = m.core.codex_pool_runtime
    pool.settings = m.state
    m.pool = pool
    tr = m.text

    def strategy_name(key):
        key = key if isinstance(key, str) and key in STRATEGIES else "active"
        return tr(STRATEGIES[key], ENGLISH_STRATEGIES[key])

    def window_name(minutes):
        return m.core.codex_window_name(minutes, english=tr(False, True))

    @m.button("Пул Codex", icon="chatgpt", where="mods", key="open", english="Codex pool")
    async def show(ctx):
        accounts = pool.accounts()
        rows = [[m.btn(a.name, "account", a.ident, icon="spark" if a.active else "chatgpt")] for a in accounts]
        rows.append([m.btn(tr("Настройки пула", "Pool settings"), "settings", icon="more")])
        text = (tr("**Пул Codex** · включён", "**Codex pool** · enabled") if pool.enabled()
                else tr("**Пул Codex** · выключен", "**Codex pool** · disabled"))
        if not accounts:
            text += tr("\nДобавь сохранённые аккаунты через совместимую утилиту codex-auth.",
                       "\nAdd saved accounts with a compatible codex-auth CLI.")
        await (ctx.edit if ctx.cb else ctx.reply)(text, rows)

    async def settings(ctx):
        strategy = m.state.get("strategy", "active")
        rows = [[m.btn(tr("Пул вкл", "Pool on") if pool.enabled() else tr("Пул выкл", "Pool off"),
                       "toggle", "enabled"),
                 m.btn(tr("Сбросы вкл", "Reset alerts on") if m.state.get("notify", False)
                       else tr("Сбросы выкл", "Reset alerts off"), "toggle", "notify")]]
        rows.append([m.btn(tr("Недельный прогрев вкл", "Weekly warmup on") if m.state.get("warm_weekly", False)
                           else tr("Недельный прогрев выкл", "Weekly warmup off"), "toggle", "warm_weekly")])
        for ident in STRATEGIES:
            rows.append([m.btn(strategy_name(ident), "strategy", ident, icon="ok" if ident == strategy else None)])
        rows.append([m.btn(tr("Пул Codex", "Codex pool"), "open", icon="back")])
        await ctx.edit(tr("**Порядок выбора аккаунтов**\n", "**Account selection order**\n")
                       + strategy_name(strategy), rows)

    m.callback("settings")(settings)

    @m.callback("toggle")
    async def toggle(ctx):
        if ctx.arg in ("enabled", "notify", "warm_weekly"):
            if ctx.arg == "enabled" and not pool.enabled() and not pool.accounts():
                return await ctx.toast(tr("Сначала добавь сохранённые аккаунты.", "Add saved accounts first."))
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
            return await ctx.toast(tr("Аккаунт пропал из списка.", "This account is no longer in the list."))
        if action == "refresh":
            await ctx.toast(tr("Обновляю квоту", "Refreshing quota"))
        model = m.core.codex_model()
        await pool.refresh([account], model, force=action == "refresh")
        status, _, _ = pool.status(account, model, time.time())
        cap = pool.cap(account)
        lines = [f"`{account.name}`", f"{tr(*STATUS[status])} · {tr('бюджет', 'budget')} {cap}%"]
        cached = pool.cache.get((account, model))
        if cached and status != "unknown":
            for window in cached[1].windows:
                label = window_name(window.minutes)
                remaining = max(0, cap - window.used)
                lines.append(f"**{label} · {remaining:g}% {tr('осталось', 'remaining')}**")
                if window.reset:
                    reset = datetime.fromtimestamp(window.reset).astimezone()
                    lines.append(f"{tr('Сброс', 'Reset')} · {reset:%d.%m %H:%M %Z}")
        rows = [[m.btn(tr("Обновить", "Refresh"), "account", ident + ",refresh"),
                 m.btn(tr("Бюджет", "Budget"), "budget", ident)],
                [m.btn(tr("Пул Codex", "Codex pool"), "open", icon="back")]]
        await ctx.edit("\n".join(lines), rows)

    @m.callback("budget")
    async def budget(ctx):
        if not any(a.ident == ctx.arg for a in pool.accounts()):
            return await ctx.toast(tr("Аккаунт пропал из списка.", "This account is no longer in the list."))
        rows = [[m.btn(f"{n}%", "cap", f"{ctx.arg},{n}") for n in group]
                for group in ((0, 25, 50, 70), (80, 90, 95, 100))]
        rows.append([m.btn(tr("Аккаунт", "Account"), "account", ctx.arg, icon="back")])
        await ctx.edit(tr("**Бюджет Codex**\nПотолок расхода для каждого окна. 0% исключает аккаунт. "
                          "Новый бюджет действует на следующие запуски.",
                          "**Codex budget**\nUsage cap for each quota window. 0% excludes the account. "
                          "The new budget applies to future runs."), rows)

    @m.callback("cap")
    async def cap(ctx):
        ident, _, percent = ctx.arg.partition(",")
        if percent not in {"0", "25", "50", "70", "80", "90", "95", "100"}:
            return await ctx.toast(tr("Выбери бюджет ещё раз.", "Choose the budget again."))
        if not any(a.ident == ident for a in pool.accounts()):
            return await ctx.toast(tr("Аккаунт пропал из списка.", "This account is no longer in the list."))
        caps = m.state.get("caps")
        if not isinstance(caps, dict):
            caps = m.state["caps"] = {}
        caps[ident] = int(percent)
        m.save()
        await ctx.toast(f"{tr('Бюджет', 'Budget')} · {percent}%")
        await ctx.edit(tr("Бюджет сохранён.", "Budget saved."),
                       [[m.btn(tr("Аккаунт", "Account"), "account", ident, icon="back")]])

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
                    label = window_name(window.minutes)
                    if notify:
                        notices.append((key, f"`{account.name}` · {label}"))
                    if warm and window.minutes == 10080 and window.key[0] == "codex":
                        known = warmups.get(account.ident)
                        if not known or previous[0] > known[0]:
                            warmups[account.ident] = [previous[0], "pending"]
                            changed = True
        for start in range(0, len(notices), 20):
            batch = notices[start:start + 20]
            delivered = await m.send(tr("**Лимиты Codex обновились**\n\n", "**Codex quota reset**\n\n")
                                     + "\n".join(text for _, text in batch),
                                     [[m.btn(tr("Пул Codex", "Codex pool"), "open")]])
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
