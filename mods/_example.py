"""Пример мода (файлы с _ не загружаются) — шпаргалка по API. Первая строка docstring = название мода."""


def setup(m):
    # /uptime — команда; вернуть строку = ответить ею
    @m.command("uptime", "сколько работает сервер")
    async def uptime(ctx):
        code, out = await m.sh("uptime -p")
        return f"⏱ {out.strip()}"

    # команда-«промпт»: просто отдаёт задачу агенту ({args} — текст после команды)
    m.prompt_command("backup", "сделать бэкап проекта", "Сделай бэкап проекта {args} по memory/backup-plan.md")

    # кнопка в главном меню (where="menu") или на экране «Моды» (по умолчанию)
    @m.button("Пинг", icon="refresh", where="mods")
    async def ping(ctx):
        n = m.state.get("pings", 0) + 1
        m.state["pings"] = n          # m.state — dict мода, сохраняется на диск через m.save()
        m.save()
        await ctx.toast(f"понг #{n}")  # всплывашка над кнопкой

    # свои кнопки внутри своих сообщений: m.btn(текст, ключ, аргумент) → @m.callback(ключ), ctx.arg
    @m.command("dice", "бросить кубик")
    async def dice(ctx):
        await ctx.reply("Бросить?", [[m.btn("🎲 Бросить", "roll", "6")]])

    @m.callback("roll")
    async def roll(ctx):
        import random
        await ctx.edit(f"Выпало {random.randint(1, int(ctx.arg))}", [[m.btn("Ещё", "roll", ctx.arg)]])

    # фоновая задача: каждые N секунд (первый запуск через N)
    @m.every(3600)
    async def hourly():
        pass  # await m.send("текст хозяину", silent=True)

    # перехват сообщений хозяина ДО агента: вернуть строку/True — съесть, None — пропустить к агенту
    @m.on_text(r"^пинг$")
    async def pong(ctx):
        return "понг"

    # отдать задачу агенту из кода: await m.agent("проверь X и отчитайся")
