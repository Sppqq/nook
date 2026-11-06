"""Моды Nook: расширения бота без правки ядра (команды, кнопки, фоновые задачи, перехват сообщений).

Мод = файл `mods/<имя>.py` с функцией `setup(m)`. Подхватываются на лету (`mods reload`), без перезапуска бота.
Пример и API — `mods/_example.py` и навык `.agents/skills/mods`.
"""
import asyncio
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import time
import traceback

HOME = os.path.dirname(os.path.abspath(__file__))
MODS_DIR = os.path.join(HOME, "mods")
SPOOL = os.path.join(HOME, "spool", "mods")
CONF_PATH = os.path.join(SPOOL, "mods.json")      # {"disabled": [...]}
RELOAD_FLAG = os.path.join(SPOOL, "reload")        # CLI → бот: перечитать моды
RESULT_PATH = os.path.join(SPOOL, "reload.json")   # бот → CLI: итог перезагрузки
RESTART_FLAG = os.path.join(SPOOL, "restart")      # CLI → бот: перезапустить ядро, когда освободится
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
CMD_RE = re.compile(r"^[a-z0-9_]{1,32}$")

log = logging.getLogger("nook.mods")


def load_conf():
    try:
        return json.load(open(CONF_PATH))
    except Exception:
        return {"disabled": []}


def save_conf(conf):
    os.makedirs(SPOOL, exist_ok=True)
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(conf, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONF_PATH)


def mod_files():
    if not os.path.isdir(MODS_DIR):
        return []
    return sorted(f[:-3] for f in os.listdir(MODS_DIR)
                  if f.endswith(".py") and not f.startswith("_") and NAME_RE.match(f[:-3]))


class Ctx:
    """Что получает обработчик: аргументы, сообщение/нажатие и способы ответить."""

    def __init__(self, mod, *, msg=None, cb=None, args="", arg="", text=""):
        self.mod, self.msg, self.cb, self.args, self.arg, self.text = mod, msg, cb, args, arg, text
        self.chat = (msg.chat.id if msg else cb.message.chat.id if cb else mod.core.owner())

    async def reply(self, text, buttons=None):
        """Новое сообщение. buttons — строки из m.btn(...): [[b1, b2], [b3]] или kb."""
        return await self.mod.core.send(self.chat, text, markup=self.mod._markup(buttons))

    async def edit(self, text, buttons=None):
        """Заменить сообщение, на котором нажали кнопку (иначе — новое)."""
        if self.cb and await self.mod.core.edit(self.chat, self.cb.message.message_id, text, self.mod._markup(buttons)):
            return self.cb.message.message_id
        return await self.reply(text, buttons)

    async def toast(self, text):
        """Всплывашка над кнопкой."""
        if self.cb:
            try:
                await self.cb.answer(text[:190])
                self._answered = True
            except Exception:
                pass


class Mod:
    def __init__(self, name, core):
        self.name, self.core = name, core
        self.title = name
        self.doc = ""
        self.commands = {}     # cmd -> (описание, fn)
        self.buttons = []      # (текст, key, icon, where)
        self.callbacks = {}    # key -> fn
        self.periodic = []     # (секунды, fn)
        self.text_hooks = []   # (regex, fn)
        self.startup = []
        self.tasks = []
        self.errors = 0
        self.log = logging.getLogger(f"nook.mod.{name}")
        self._state_path = os.path.join(SPOOL, f"{name}.state.json")
        try:
            self.state = json.load(open(self._state_path))
        except Exception:
            self.state = {}

    # ---- регистрация
    def command(self, name, desc=""):
        """@m.command("weather", "погода") → /weather. Обработчик: fn(ctx) → str|None, ctx.args — текст после команды."""
        if not CMD_RE.match(name):
            raise ValueError(f"команда {name!r}: только a-z0-9_")

        def deco(fn):
            self.commands[name] = (desc or name, fn)
            return fn
        return deco

    def prompt_command(self, name, desc, prompt):
        """Команда, которая просто отдаёт агенту готовую задачу. {args} — текст после команды."""
        async def run(ctx):
            await self.agent(prompt.replace("{args}", ctx.args or ""))
        self.command(name, desc)(run)

    def button(self, text, icon=None, where="mods", key=None):
        """@m.button("Погода", icon="cal", where="menu"|"mods") — кнопка в главном меню или на экране «Моды»."""
        def deco(fn):
            k = key or fn.__name__
            self.buttons.append((text, k, icon, where))
            self.callbacks[k] = fn
            return fn
        return deco

    def prompt_button(self, text, prompt, icon=None, where="mods", key=None):
        """Кнопка, которая отдаёт агенту готовую задачу."""
        async def run(ctx):
            await ctx.toast("Взялся 👌")
            await self.agent(prompt)
        run.__name__ = key or re.sub(r"\W", "", text.lower())[:16] or "p"
        self.button(text, icon, where, run.__name__)(run)

    def callback(self, key):
        """@m.callback("more") — обработчик для m.btn(..., "more", arg) в своих сообщениях. ctx.arg — аргумент."""
        def deco(fn):
            self.callbacks[key] = fn
            return fn
        return deco

    def every(self, seconds):
        """@m.every(3600) — фоновая задача (первый запуск через seconds)."""
        def deco(fn):
            self.periodic.append((max(int(seconds), 10), fn))
            return fn
        return deco

    def on_text(self, pattern):
        """@m.on_text(r"^курс") — перехват сообщения хозяина ДО агента. Вернуть None/False — пропустить дальше."""
        def deco(fn):
            self.text_hooks.append((re.compile(pattern, re.I | re.S), fn))
            return fn
        return deco

    def on_start(self, fn):
        self.startup.append(fn)
        return fn

    # ---- помощники
    def btn(self, text, key, arg="", icon=None, style=None):
        data = f"mod:{self.name}:{key}" + (f":{arg}" if arg != "" else "")
        if len(data.encode()) > 64:
            raise ValueError(f"callback_data длиннее 64 байт: {data}")
        return self.core.btn(text, data, icon, style)

    def _markup(self, buttons):
        if buttons is None or not isinstance(buttons, (list, tuple)):
            return buttons
        rows = [r if isinstance(r, (list, tuple)) else [r] for r in buttons]
        return self.core.kb(*rows)

    async def send(self, text, buttons=None, silent=None):
        """Написать хозяину."""
        return await self.core.notify(text, markup=self._markup(buttons), silent=silent)

    async def agent(self, prompt, title=None, background=False):
        """Отдать задачу агенту: в основном разговоре (видно хозяину, с прогрессом) или фоном (background=True:
        ответ придёт хозяину, только если агенту есть что сказать — иначе SILENT)."""
        if background:
            return await self.core.bg_task(f"[из мода {self.name}] {prompt}", self.title)
        return await self.core.owner_ask(f"[из мода {self.name}] {prompt}", title or f"{self.core.E('busy')} {self.title}")

    async def llm(self, prompt, system=None):
        """Быстрый ответ модели Mercury 3 без инструментов (сводки, классификация, перевод) — не тратит лимит Codex."""
        return await self.core.llm(prompt, system)

    async def sh(self, cmd, timeout=60):
        """Выполнить shell-команду → (код, вывод)."""
        p = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.STDOUT, cwd=HOME)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return -1, "таймаут"
        return p.returncode, out.decode(errors="replace")

    def save(self):
        """Сохранить m.state (dict) на диск."""
        os.makedirs(SPOOL, exist_ok=True)
        tmp = self._state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._state_path)

    async def call(self, fn, ctx, where):
        """Вызвать обработчик мода с защитой: ошибка мода не роняет бота."""
        try:
            res = fn(ctx) if ctx is not None else fn()
            if inspect.isawaitable(res):
                res = await res
            self.errors = 0
            return res
        except Exception as e:
            self.errors += 1
            tb = traceback.format_exc()[-1500:]
            log.error("mod %s %s failed: %s", self.name, where, tb)
            await self.core.mod_error(self, where, e, tb)
            return None


class Registry:
    def __init__(self, core):
        self.core = core
        self.mods = {}       # name -> Mod (загруженные)
        self.failed = {}     # name -> текст ошибки загрузки

    def load_one(self, name):
        path = os.path.join(MODS_DIR, f"{name}.py")
        m = Mod(name, self.core)
        modname = f"nook_mod_{name}_{int(time.time() * 1000)}"
        spec = importlib.util.spec_from_file_location(modname, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        doc = (module.__doc__ or "").strip()
        m.title = getattr(module, "TITLE", None) or (doc.splitlines()[0] if doc else name)
        m.doc = doc
        if not hasattr(module, "setup"):
            raise RuntimeError("нет функции setup(m)")
        module.setup(m)
        return m

    def stop_all(self):
        for m in self.mods.values():
            for t in m.tasks:
                t.cancel()
        self.mods = {}

    def reload(self):
        """Перечитать все моды. Возвращает {"loaded": [...], "failed": {name: err}, "disabled": [...]}."""
        self.stop_all()
        self.failed = {}
        disabled = set(load_conf().get("disabled", []))
        taken = set(self.core.base_commands)
        for name in mod_files():
            if name in disabled:
                continue
            try:
                m = self.load_one(name)
                clash = (set(m.commands) & taken)
                if clash:
                    raise RuntimeError(f"команды уже заняты: {', '.join('/' + c for c in clash)}")
                taken |= set(m.commands)
                self.mods[name] = m
            except Exception:
                self.failed[name] = traceback.format_exc(limit=3)[-1200:]
                log.error("mod %s failed to load: %s", name, self.failed[name])
        for m in self.mods.values():
            for fn in m.startup:
                m.tasks.append(asyncio.create_task(m.call(fn, None, "on_start")))
            for secs, fn in m.periodic:
                m.tasks.append(asyncio.create_task(self._loop(m, secs, fn)))
        return {"loaded": sorted(self.mods), "failed": self.failed, "disabled": sorted(disabled & set(mod_files()))}

    async def _loop(self, m, secs, fn):
        while True:
            await asyncio.sleep(secs)
            await m.call(fn, None, f"every({secs})")
            if m.errors >= 5:
                await self.core.notify(f"{self.core.E('err')} Мод **{m.name}** падает 5 раз подряд — фоновую задачу остановил.")
                return

    # ---- маршрутизация
    def command(self, cmd):
        for m in self.mods.values():
            if cmd in m.commands:
                return m, m.commands[cmd][1]
        return None, None

    def all_commands(self):
        return [(c, d) for m in self.mods.values() for c, (d, _) in m.commands.items()]

    def buttons(self, where):
        return [(m, text, key, icon) for m in self.mods.values() for text, key, icon, w in m.buttons if w == where]

    async def on_text(self, msg, text):
        for m in self.mods.values():
            for rx, fn in m.text_hooks:
                if rx.search(text):
                    res = await m.call(fn, Ctx(m, msg=msg, text=text), f"on_text({rx.pattern})")
                    if res:
                        if isinstance(res, str):
                            await self.core.send(msg.chat.id, res, reply_to=msg.message_id)
                        return True
        return False

    async def on_command(self, msg, cmd, args):
        m, fn = self.command(cmd)
        if not fn:
            return False
        res = await m.call(fn, Ctx(m, msg=msg, args=args), f"/{cmd}")
        if isinstance(res, str) and res:
            await self.core.send(msg.chat.id, res)
        return True

    async def on_button(self, cb, data):
        _, name, rest = data.split(":", 2)
        key, _, arg = rest.partition(":")
        m = self.mods.get(name)
        fn = m and m.callbacks.get(key)
        if not fn:
            await cb.answer("Мод выключен или изменился", show_alert=False)
            return
        ctx = Ctx(m, cb=cb, arg=arg)
        res = await m.call(fn, ctx, f"кнопка {key}")
        if not getattr(ctx, "_answered", False):
            try:
                await cb.answer()
            except Exception:
                pass
        if isinstance(res, str) and res:
            await ctx.reply(res)


# ---------------------------------------------------------------- CLI-проверка (без бота)

class _Stub:
    """Ядро-заглушка для `mods check`: всё регистрируется, ничего не отправляется."""
    base_commands = ("start", "start2", "menu", "status", "limits", "jobs", "new", "stop", "watch", "login", "mods")

    def __getattr__(self, name):
        async def nothing(*a, **k):
            return None
        return nothing

    def E(self, name):
        return ""

    def btn(self, text, data, icon=None, style=None):
        return (text, data)

    def kb(self, *rows):
        return rows

    def owner(self):
        return 0


def check(name):
    """Загрузить мод с заглушкой и показать, что он регистрирует. Возвращает (ok, отчёт)."""
    path = os.path.join(MODS_DIR, f"{name}.py")
    if not NAME_RE.match(name):
        return False, "имя мода: латиница в нижнем регистре, цифры, _ (до 24 символов), с буквы"
    if not os.path.isfile(path):
        return False, f"нет файла {path}"
    try:
        m = Registry(_Stub()).load_one(name)
    except Exception:
        return False, traceback.format_exc(limit=4)
    clash = set(m.commands) & set(_Stub.base_commands)
    if clash:
        return False, f"команды заняты ядром: {clash}"
    lines = [f"{m.name}: {m.title}"]
    lines += [f"  /{c} — {d}" for c, (d, _) in m.commands.items()]
    lines += [f"  кнопка [{t}] → {k} ({w})" for t, k, _, w in m.buttons]
    lines += [f"  каждые {s} с: {fn.__name__}" for s, fn in m.periodic]
    lines += [f"  перехват текста: {rx.pattern}" for rx, _ in m.text_hooks]
    for k, fn in m.callbacks.items():
        if inspect.signature(fn).parameters.__len__() != 1:
            return False, f"обработчик {k}: должен принимать ровно один аргумент (ctx)"
    return True, "\n".join(lines)


def cli(argv):
    usage = ("mods list | check ИМЯ | reload | enable ИМЯ | disable ИМЯ | restart\n"
             "  check   — загрузить мод вхолостую и показать, что он добавляет\n"
             "  reload  — бот перечитает все моды на лету (ждёт итог до 15 с)\n"
             "  restart — перезапустить ЯДРО бота (после правки bot.py), когда закончится текущая задача")
    if len(argv) < 2:
        print(usage)
        return 1
    cmd = argv[1]
    os.makedirs(SPOOL, exist_ok=True)
    if cmd == "list":
        conf = load_conf()
        for n in mod_files():
            ok, rep = check(n)
            mark = "выкл" if n in conf.get("disabled", []) else ("ok" if ok else "ОШИБКА")
            print(f"[{mark}] " + (rep if ok else f"{n}\n{rep}"))
        if not mod_files():
            print("модов пока нет")
        return 0
    if cmd == "check" and len(argv) > 2:
        ok, rep = check(argv[2])
        print(rep)
        return 0 if ok else 2
    if cmd in ("enable", "disable") and len(argv) > 2:
        conf = load_conf()
        d = set(conf.get("disabled", []))
        (d.discard if cmd == "enable" else d.add)(argv[2])
        conf["disabled"] = sorted(d)
        save_conf(conf)
        cmd = "reload"
    if cmd == "reload":
        try:
            os.remove(RESULT_PATH)
        except OSError:
            pass
        open(RELOAD_FLAG, "w").close()
        for _ in range(30):
            time.sleep(0.5)
            if os.path.exists(RESULT_PATH):
                r = json.load(open(RESULT_PATH))
                print("загружены:", ", ".join(r["loaded"]) or "—")
                if r.get("disabled"):
                    print("выключены:", ", ".join(r["disabled"]))
                for n, e in r["failed"].items():
                    print(f"ОШИБКА {n}:\n{e}")
                return 2 if r["failed"] else 0
        print("бот не ответил за 15 с (не запущен?) — флаг оставлен, подхватит при старте")
        return 1
    if cmd == "restart":
        open(RESTART_FLAG, "w").write(str(time.time()))
        print("ядро перезапустится, когда закончатся текущие задачи (сначала проверю, что bot.py импортируется)")
        return 0
    print(usage)
    return 1


if __name__ == "__main__":
    sys.exit(cli(sys.argv))
