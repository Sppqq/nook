#!/usr/bin/env python3
"""Nook — автономный админ сервера в Telegram на базе Codex CLI."""
import asyncio
import contextvars
import datetime as dt
import fcntl
import html
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import time
import tempfile
import uuid
from base64 import b64encode
from urllib.request import Request, urlopen

import aiohttp

import guard
import antigravity_limits
import mods as modsys

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

HOME = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HOME, "config.json")
STATE_PATH = os.path.join(HOME, "state.json")
OUTBOX = os.path.join(HOME, "spool", "outbox")
JOBS = os.path.join(HOME, "spool", "jobs.json")
INBOX = os.path.join(HOME, "inbox")
CODEX = shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
PM2 = shutil.which("pm2") or os.path.expanduser("~/.npm-global/bin/pm2")
STATUS_BOARD = "http://127.0.0.1:1467/api/status"
STATUS_BOARD_PW = os.path.expanduser("~/status-board/.password")

WATCH_INTERVAL = 120          # секунд между проверками автонадзора
INCIDENT_DAILY_CAP = 12
LIMIT_WARN = 90               # % лимита Codex, при котором предупредить хозяина
DISK_WARN = 90                # % диска, при котором агент сам идёт убираться
USER_TIMEOUT = 40 * 60
BG_TIMEOUT = 20 * 60
TG_LIMIT = 4000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nook")


# ---------------------------------------------------------------- state

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


CFG = load_json(CONFIG_PATH, {})
STATE = load_json(STATE_PATH, {})
STATE.setdefault("owner_id", CFG.get("owner_id"))
STATE.setdefault("main_thread", None)
STATE.setdefault("msg_threads", {})
STATE.setdefault("known_good", [])
STATE.setdefault("reported", {})
STATE.setdefault("watch", True)
STATE.setdefault("last_patrol", "")
STATE.setdefault("introduced", False)
STATE.setdefault("incidents_today", [dt.date.today().isoformat(), 0])


def save_state():
    save_json(STATE_PATH, STATE)


def key_name(key):
    """pm2:vault → vault; mem → mem (ключи мониторинга)."""
    return key.split(":", 1)[-1]


def remember_msg(msg_id, thread_id):
    if not thread_id:
        return
    m = STATE["msg_threads"]
    m[str(msg_id)] = thread_id
    if len(m) > 500:
        for k in list(m)[: len(m) - 500]:
            del m[k]
    save_state()


bot = Bot(CFG["bot_token"], default=DefaultBotProperties(link_preview_is_disabled=True))
dp = Dispatcher()


def is_owner(msg: Message) -> bool:
    return (STATE["owner_id"] is not None and msg.chat.type == "private"
            and msg.from_user and msg.from_user.id == STATE["owner_id"])


# ---------------------------------------------------------------- telegram formatting

# премиум-эмодзи (пак tgiosicons): имя -> [custom_emoji_id, обычный эмодзи-заменитель]
EMOJI = load_json(os.path.join(HOME, "emoji.json"), {})
E_RE = re.compile(r"\{\{e:(\w+)\}\}")
# обычные эмодзи в ответах агента, которые подменяем на премиум
AUTO_EMOJI = {"✅": "ok", "❌": "cross", "⚠️": "warn", "❗": "err", "ℹ️": "info"}


def E(name):
    """Метка премиум-эмодзи для текста; превращается в <tg-emoji> в md_to_html."""
    return "{{e:%s}}" % name


def emoji_html(m):
    e = EMOJI.get(m.group(1))
    return f'<tg-emoji emoji-id="{e[0]}">{e[1]}</tg-emoji>' if e else ""


def plain(text):
    """Текст без разметки: метки эмодзи → обычные эмодзи."""
    return E_RE.sub(lambda m: EMOJI.get(m.group(1), ["", ""])[1], text)


# ---------------------------------------------------------------- rich-сообщения (Bot API 10.3)
# /start — «rich»: сообщения sendRichMessage, кнопки прямо в тексте (<tg-button>), таблицы/заголовки.
# /start2 — «classic»: обычный HTML + inline-клавиатура с простыми эмодзи (для старых клиентов).
# Режим берётся из сообщения, на котором нажали кнопку, иначе — последний выбор /start или /start2.
UI = contextvars.ContextVar("ui", default=None)
EMOJI_BY_ID = {v[0]: v[1] for v in EMOJI.values()}
RICH_LIMIT = 20000  # лимит 32768, оставляем запас на теги эмодзи/кнопок
_http = None


def ui_mode():
    return UI.get() or STATE.get("ui", "rich")


async def tg(method, **params):
    """Прямой вызов Bot API (aiogram пока не знает про rich-сообщения)."""
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    params = {k: v for k, v in params.items() if v is not None}
    async with _http.post(f"https://api.telegram.org/bot{CFG['bot_token']}/{method}", json=params) as r:
        d = await r.json()
    if not d.get("ok"):
        raise RuntimeError(d.get("description", "telegram error"))
    return d["result"]


def to_rich_md(text):
    """Markdown агента → rich markdown: жёсткие переносы строк, экранирование <, премиум-эмодзи."""
    out = []
    for i, block in enumerate(re.split(r"(```.*?```)", text, flags=re.S)):
        if i % 2:
            out.append(block)
            continue
        lines = block.split("\n")
        for j in range(len(lines) - 1):
            a, b = lines[j], lines[j + 1]
            # в rich markdown одиночный \n склеивается в пробел — делаем жёсткий перенос
            if a.strip() and b.strip() and not a.lstrip().startswith("|") and not b.lstrip().startswith("|"):
                lines[j] = a.rstrip() + "  "
        block = "\n".join(lines)
        # ссылки на локальные файлы в Telegram не открываются — показываем как код
        block = re.sub(r"\[`?([^\]\n`]+)`?\]\((?!https?://)[^)\s]+\)", r"`\1`", block)
        parts = re.split(r"(`[^`\n]+`)", block)
        for k in range(0, len(parts), 2):
            t = parts[k].replace("<", "&lt;")
            # notes.md, bot.py — иначе rich markdown превращает их в ссылки
            t = FILE_RE.sub(lambda m: f"`{m.group(1)}`", t)
            t = re.sub(r"(?<![(\[<\"])(https?://[^\s)<>\]]+)", r"[\1](\1)", t)
            for ch, name in AUTO_EMOJI.items():
                if name in EMOJI:
                    t = t.replace(ch + "\ufe0f", E(name)).replace(ch, E(name))
            parts[k] = E_RE.sub(emoji_html, t)
        out.append("".join(parts))
    return "".join(out).strip()


def rich_buttons(markup):
    """InlineKeyboardMarkup → ряды <tg-button> в теле сообщения."""
    if not markup or not markup.inline_keyboard:
        return ""
    rows = []
    for row in markup.inline_keyboard:
        bs = []
        for b in row[:8]:
            label = html.escape(b.text, quote=False)
            icon = getattr(b, "icon_custom_emoji_id", None)
            if icon:
                label = f'<tg-emoji emoji-id="{icon}">{EMOJI_BY_ID.get(icon, "▫️")}</tg-emoji> {label}'
            style = getattr(b, "style", None)
            st = f' style="{style}"' if style else ""
            bs.append(f'<tg-button type="callback_data" data="{html.escape(b.callback_data, quote=True)}"{st}>{label}</tg-button>')
        rows.append("<tg-button-row>" + "".join(bs) + "</tg-button-row>")
    return "\n\n" + "\n".join(rows)


def rich_payload(text, markup):
    return {"markdown": to_rich_md(text) + rich_buttons(markup), "skip_entity_detection": True}


SENT_PATH = os.path.join(HOME, "spool", "sent.json")
SENT = load_json(SENT_PATH, {})  # message_id -> {"t": markdown, "k": [[[текст, data, icon]]]} — для «.»


def sent_put(mid, text, markup):
    if not mid:
        return
    rows = [[[b.text, b.callback_data, getattr(b, "icon_custom_emoji_id", None)] for b in r]
            for r in (markup.inline_keyboard if markup else [])]
    SENT[str(mid)] = {"t": text[:20000], "k": rows}
    for k in list(SENT)[:-200]:
        del SENT[k]
    try:
        save_json(SENT_PATH, SENT)
    except OSError:
        pass


def classic_markup(rows):
    """Кнопки без премиум-иконок и цвета: иконка → обычный эмодзи в тексте."""
    out = []
    for r in rows:
        line = []
        for text, data, icon in r:
            if icon:
                text = f"{EMOJI_BY_ID.get(icon, '')} {text}".strip()
            if data:
                line.append(InlineKeyboardButton(text=text, callback_data=data))
        if line:
            out.append(line)
    return InlineKeyboardMarkup(inline_keyboard=out) if out else None


async def resend_classic(chat_id, mid):
    """«.» в ответ на сообщение: прислать его заново обычным сообщением с обычными кнопками."""
    item = SENT.get(str(mid))
    if not item:
        return False
    tok = UI.set("classic")
    try:
        new = await send(chat_id, item["t"], markup=classic_markup(item["k"]), reply_to=mid)
    finally:
        UI.reset(tok)
    for k in ("msg_threads", "msg_tasks", "incident_msgs"):  # кнопки и реплаи на копии работают как на оригинале
        if str(mid) in STATE.get(k, {}):
            STATE[k][str(new)] = STATE[k][str(mid)]
    save_state()
    return True


async def post(chat_id, text, markup=None, reply_to=None, silent=None, cache=True):
    """Отправить одно сообщение (markdown) в текущем режиме; вернуть message_id."""
    mid = await _post(chat_id, text, markup, reply_to, silent)
    if cache:
        sent_put(mid, text, markup)
    return mid


async def _post(chat_id, text, markup=None, reply_to=None, silent=None):
    if ui_mode() == "rich":
        try:
            m = await tg("sendRichMessage", chat_id=chat_id, rich_message=rich_payload(text, markup),
                         disable_notification=silent,
                         reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                         if reply_to else None)
            return m["message_id"]
        except Exception as e:
            log.warning("rich send failed, fallback to classic: %s", e)
    try:
        m = await bot.send_message(chat_id, md_to_html(text), parse_mode="HTML", disable_notification=silent,
                                   reply_to_message_id=reply_to, reply_markup=markup, allow_sending_without_reply=True)
    except Exception:
        m = await bot.send_message(chat_id, plain(text), reply_to_message_id=reply_to, reply_markup=markup,
                                   allow_sending_without_reply=True)
    return m.message_id


async def edit(chat_id, msg_id, text, markup=None, rich=None, cache=True):
    """Отредактировать сообщение тем же способом, каким оно было отправлено."""
    rich = ui_mode() == "rich" if rich is None else rich
    if cache:
        sent_put(msg_id, text, markup)
    try:
        if rich:
            await tg("editMessageText", chat_id=chat_id, message_id=msg_id, rich_message=rich_payload(text, markup))
        else:
            await bot.edit_message_text(md_to_html(text), chat_id=chat_id, message_id=msg_id,
                                        parse_mode="HTML", reply_markup=markup)
        return True
    except Exception as e:
        if "not modified" in str(e):
            return True
        if "not found" in str(e):  # сообщение удалили — правками его не вернуть
            log.info("edit: message %s gone", msg_id)
            return None
        log.warning("edit failed: %s", e)
        return False

# имена файлов вида notes.md Telegram превращает в ссылки (.md, .py, .sh — домены), поэтому
# голые пути и имена файлов заворачиваем в <code>
FILE_RE = re.compile(r"(?<![\w/~.@-])((?:~|\.{0,2})?/?(?:[\w.-]+/)*[\w-][\w.-]*\.(?:md|py|sh|js|ts|json|log|txt|toml|ya?ml|conf|service|env|sql|db|sqlite|html|css|bak[\w-]*))(?![\w/-])")
PATH_RE = re.compile(r"(?<![\w/:.@<-])(~/[\w.@/-]*[\w@-]/?|/[\w.@-]+/[\w.@/-]*[\w@-]/?)(?![\w/])")


def inline_md(s: str) -> str:
    """Обычный текст (без кода) → HTML."""
    links = []

    def link(m):
        label, url = m.group(1), m.group(2)
        if re.match(r"https?://", url):
            links.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
        else:  # ссылка на локальный файл — в Telegram не кликается, показываем как код
            links.append(f"<code>{html.escape(label)}</code>")
        return f"\x00{len(links) - 1}\x00"

    s = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, s)
    s = html.escape(s, quote=False)
    s = PATH_RE.sub(r"<code>\1</code>", s)
    s = re.sub(r"(<code>.*?</code>)|" + FILE_RE.pattern,
               lambda m: m.group(1) or f"<code>{m.group(2)}</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__(.+?)__", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", s)
    s = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", s)
    s = re.sub(r"(?m)^&gt;\s?(.*)$", r"<blockquote>\1</blockquote>", s)
    s = s.replace("</blockquote>\n<blockquote>", "\n")
    s = re.sub(r"(?m)^\s*(-{3,}|\*{3,}|_{3,})\s*$", "", s)
    for ch, name in AUTO_EMOJI.items():
        if name in EMOJI:
            s = s.replace(ch, E(name)).replace(E(name) + "\ufe0f", E(name))
    s = E_RE.sub(emoji_html, s)
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], s)


def md_to_html(text: str) -> str:
    """Markdown, который пишет агент → Telegram HTML."""
    text = re.sub(r"\[`([^`\]\n]+)`\]\(", r"[\1](", text)
    out = []
    for i, block in enumerate(re.split(r"```[^\n]*\n?(.*?)```", text, flags=re.S)):
        if i % 2:
            out.append("<pre>" + html.escape(block.rstrip()) + "</pre>")
            continue
        for j, part in enumerate(re.split(r"`([^`\n]+)`", block)):
            out.append(f"<code>{html.escape(part)}</code>" if j % 2 else inline_md(part))
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def split_text(text: str, limit=TG_LIMIT):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        # не резать посреди ``` блока
        if text[:cut].count("```") % 2:
            pos = text.rfind("```", 0, cut)
            if pos > 0:
                cut = pos
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks


async def send(chat_id, text, reply_to=None, markup=None, silent=None):
    """Отправить длинный текст (кнопки — под последним куском); вернуть id последнего сообщения."""
    last, ids = None, []
    chunks = split_text(text, RICH_LIMIT if ui_mode() == "rich" else TG_LIMIT) or ["(пусто)"]
    for i, chunk in enumerate(chunks):
        last = await post(chat_id, chunk, markup if i == len(chunks) - 1 else None, reply_to, silent)
        ids.append(last)
        reply_to = None
    if len(ids) > 1:  # длинный текст ушёл кусками — «Закрыть» под последним убирает все
        grp = STATE.setdefault("msg_groups", {})
        grp[str(last)] = ids[:-1]
        for k in list(grp)[:-200]:
            del grp[k]
        save_state()
    return last


def msg_group(mid):
    """Сообщение и все куски, отправленные вместе с ним."""
    return STATE.get("msg_groups", {}).get(str(mid), []) + [mid]


def with_close(markup):
    """Добавить «Закрыть» под уведомление (в ряд с «Меню», если он одинокий)."""
    rows = [list(r) for r in (markup.inline_keyboard if markup else [])]
    if any(b.callback_data == "close" for r in rows for b in r):
        return markup
    close = btn("Закрыть", "close", "cross")
    if rows and len(rows[-1]) == 1 and rows[-1][0].callback_data == "menu":
        rows[-1].append(close)
    else:
        rows.append([close])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify(text, thread_id=None, markup=None, silent=None, kind=None):
    """kind — тема уведомления («apt», «guard:node»…): новое такое же заменяет прошлое, чтобы не копились."""
    if not STATE["owner_id"]:
        return None
    mid = await send(STATE["owner_id"], text, markup=with_close(markup), silent=silent)
    remember_msg(mid, thread_id)
    if kind:
        km = STATE.setdefault("kind_msgs", {})
        old = km.get(kind)
        km[kind] = mid
        save_state()
        if old and old != mid:
            await delete_msgs(STATE["owner_id"], msg_group(old))
    return mid


def trash(chat_id, mid, after=60):
    """Удалить сообщение через after секунд (переживает перезапуск бота)."""
    if mid:
        STATE.setdefault("trash", []).append([chat_id, mid, time.time() + after])
        save_state()


class waiting:
    """«⏳ думаю…» в чате, пока идёт долгое действие по кнопке; потом убирается."""
    def __init__(self, chat, what="думаю"):
        self.chat, self.what, self.mid = chat, what, None

    async def __aenter__(self):
        try:
            self.mid = await send(self.chat, f"{E('busy')} {self.what}…", silent=True)
        except Exception as e:
            log.info("waiting msg failed: %s", e)

    async def __aexit__(self, *exc):
        if self.mid:
            await delete_msgs(self.chat, [self.mid])


async def send_tmp(chat_id, text, after=60, **kw):
    """Служебное сообщение («остановил», «всё ок»…) — само исчезнет из чата."""
    mid = await send(chat_id, text, **kw)
    trash(chat_id, mid, after)
    return mid


async def trash_loop():
    while True:
        try:
            now, items = time.time(), STATE.get("trash", [])
            due = [t for t in items if t[2] <= now]
            if due:
                STATE["trash"] = [t for t in items if t[2] > now]
                save_state()
                for chat in {t[0] for t in due}:
                    await delete_msgs(chat, [t[1] for t in due if t[0] == chat])
        except Exception:
            log.exception("trash loop failed")
        await asyncio.sleep(10)


def quiet():
    return CFG.get("notify_mode", "quiet") == "quiet"


def digest(text, buttons=()):
    """Неважное — не в чат, а в дневную сводку. buttons: [(текст, callback_data, icon)]."""
    if not quiet():
        markup = kb(*[(btn(t, d, i),) for t, d, i in buttons]) if buttons else None
        return asyncio.create_task(notify(text, markup=markup, silent=True))
    d = STATE.setdefault("digest", [])
    d.append({"t": text, "at": time.time(), "b": [list(b) for b in buttons]})
    del d[:-80]
    save_state()


# ---------------------------------------------------------------- codex runner

class Run:
    def __init__(self):
        self.proc = None
        self.cancelled = False
        self.started = time.time()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


RUN_THREADS = {}  # NOOK_RUN -> thread_id: чтобы ответ на `ask` ушёл в тот же разговор
SECRETS = os.path.join(HOME, "secrets.env")


def load_secrets():
    """Ключи сервисов (KEY=value) — в окружение агента и его MCP-серверов (env_vars в .codex/config.toml)."""
    out = {}
    try:
        for line in open(SECRETS):
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                out[m.group(1)] = m.group(2).strip("'\"")
    except OSError:
        pass
    return out


def codex_env(run_id=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_")}
    env["NOOK_HOME"] = HOME
    if run_id:
        env["NOOK_RUN"] = run_id
    env.update(load_secrets())
    env["PATH"] = f"{HOME}/bin:{os.path.expanduser('~/.local/bin')}:{os.path.dirname(PM2)}:" + env.get("PATH", "/usr/bin:/bin")
    return env


def describe_item(item, started):
    t = item.get("type")
    if t == "command_execution":
        cmd = item.get("command", "")
        cmd = re.sub(r"^/bin/(ba|z)?sh -l?c ", "", cmd).strip("'\"")
        cmd = cmd.replace("\n", " ")[:90]
        if started:
            return f"▸ {cmd}"
        code = item.get("exit_code")
        return f"{'✓' if code in (0, None) else '✗'} {cmd}"
    if t == "file_change" and not started:
        files = [os.path.basename(c.get("path", "?")) for c in item.get("changes", [])]
        return "✎ " + ", ".join(files[:4])
    if t == "web_search" and not started:
        return "🔎 " + str(item.get("query", ""))[:80]
    if t == "mcp_tool_call" and not started:
        return f"🔧 {item.get('server', '')}.{item.get('tool', '')}"
    if t == "agent_message" and not started:
        return "💬 " + item.get("text", "").replace("\n", " ")[:120]
    return None


COMPACT_TOKENS = 120_000   # Codex сжимает разговор, когда контекст дорос до этого
IDLE_RESET_H = 6           # столько часов тишины — и новое сообщение начинает разговор заново


def main_thread_for_new_message():
    """Основной разговор для нового (не ответного) сообщения хозяина; после долгой паузы — новый."""
    thread = STATE["main_thread"]
    idle = time.time() - STATE.get("main_at", time.time())
    if thread and idle > CFG.get("idle_reset_hours", IDLE_RESET_H) * 3600:
        log.info("main thread idle %.1f h — new conversation", idle / 3600)
        STATE["main_thread"] = None
        save_state()
        return None, STATE.get("last_answer", {}).get(thread, "")
    return thread, ""


def fresh_prompt(prompt, ctx=""):
    head = "Новый разговор с хозяином. Прочитай memory/notes.md, если ещё не читал.\n\n"
    if ctx:
        head += f"[для контекста: твой последний ответ в прошлом разговоре]\n{ctx[-1500:]}\n\n"
    return head + prompt


def context_tokens():
    """Сколько токенов сейчас занимает основной разговор Codex (по последнему token_count в его журнале)."""
    tid = STATE.get("main_thread")
    if not tid or is_mercury_thread(tid):
        return None
    import glob
    files = sorted(glob.glob(os.path.expanduser(f"~/.codex/sessions/*/*/*/*{tid}.jsonl")))
    if not files:
        return None
    try:
        with open(files[-1], "rb") as f:
            f.seek(max(0, os.path.getsize(files[-1]) - 400_000))
            tail = f.read().decode(errors="replace")
        m = re.findall(r'"last_token_usage":\{"input_tokens":(\d+)', tail)
        return int(m[-1]) if m else None
    except OSError:
        return None


async def run_codex(prompt, thread_id=None, images=(), on_step=None, run: Run = None, timeout=USER_TIMEOUT):
    """Запустить codex exec (или resume); вернуть (thread_id, final_text, error)."""
    out_file = os.path.join(HOME, "spool", f"last-{uuid.uuid4().hex[:8]}.txt")
    cmd = [CODEX, "exec", "--json", "--skip-git-repo-check", "-C", HOME,
           "--dangerously-bypass-approvals-and-sandbox", "-o", out_file]
    if CFG.get("model"):
        cmd += ["-m", CFG["model"]]
    if CFG.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{CFG["effort"]}"']
    # каждое сообщение отправляет модели весь разговор — сжимаем его задолго до потолка окна (475k)
    cmd += ["-c", f"model_auto_compact_token_limit={int(CFG.get('compact_tokens', COMPACT_TOKENS))}"]
    if thread_id:
        cmd.append("resume")  # -i до resume прикрепляется только к первому промпту
    for img in images:
        cmd += ["-i", img]
    cmd += [thread_id, "-"] if thread_id else ["-"]
    run = run or Run()
    run_id = uuid.uuid4().hex[:12]
    if thread_id:
        RUN_THREADS[run_id] = thread_id
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=HOME, env=codex_env(run_id), start_new_session=True, limit=16 * 1024 * 1024,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    run.proc = proc
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    new_thread, last_msg, errors = thread_id, "", []
    stderr_task = asyncio.create_task(proc.stderr.read())

    async def pump():
        nonlocal new_thread, last_msg
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            et = ev.get("type", "")
            if et == "thread.started":
                new_thread = ev.get("thread_id") or new_thread
                RUN_THREADS[run_id] = new_thread
            elif et in ("item.started", "item.completed"):
                item = ev.get("item", {})
                if et == "item.completed" and item.get("type") == "agent_message":
                    last_msg = item.get("text", "")
                if on_step:
                    line = describe_item(item, et == "item.started")
                    if line:
                        await on_step(line)
            elif et in ("error", "turn.failed"):
                errors.append(str(ev.get("message") or ev.get("error")))

    try:
        await asyncio.wait_for(pump(), timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        run.cancel()
        errors.append(f"таймаут {timeout // 60} мин")
    stderr = (await stderr_task).decode(errors="replace")
    try:
        with open(out_file) as f:
            final = f.read().strip() or last_msg
        os.remove(out_file)
    except FileNotFoundError:
        final = last_msg
    if run.cancelled and not errors:
        errors.append("остановлено")
    if proc.returncode not in (0, None) and not final and not errors:
        errors.append(stderr.strip()[-800:] or f"codex exit {proc.returncode}")
    err = "; ".join(e for e in errors if e) or None
    if err and re.search(r"401|unauthori|log ?in|refresh.?token|expired", err + stderr[-2000:], re.I):
        err += "\n\n⚠️ Похоже, слетела авторизация Codex — нажми «Войти в Codex»"
    return new_thread, final, err


# ---------------------------------------------------------------- Mercury: быстрая/запасная модель

# Inception Mercury умеет только Chat Completions, а Codex — только Responses API, поэтому для Mercury
# свой маленький агент: тот же AGENTS.md, один инструмент shell. Разговоры — spool/mercury/<id>.json.
MERCURY_URL = "https://api.inceptionlabs.ai/v1/chat/completions"
MERCURY_MODEL = "mercury-3"
ANTIGRAVITY_MODEL = "antigravity-argon"
ANTIGRAVITY_MODELS = ("antigravity", "antigravity-argon", "antigravity-flash38")
def is_antigravity_model(m):
    return m in ANTIGRAVITY_MODELS
AGY_BIN = os.path.expanduser("~/.local/bin/agy")
MERCURY_DIR = os.path.join(HOME, "spool", "mercury")
MERCURY_STEPS = 30
LIMIT_RE = re.compile(r"usage limit|rate.?limit|429|hit your limit|limit (has been )?reached|quota", re.I)
SHELL_TOOL = {"type": "function", "function": {
    "name": "shell",
    "description": "Выполнить bash-команду на сервере (cwd ~/nook, твои инструменты say/ask/nodes/guard/mods/later/"
                   "rootdo в PATH). Возвращает код выхода и вывод (обрезан до 8000 символов).",
    "parameters": {"type": "object", "properties": {
        "cmd": {"type": "string", "description": "bash-команда"},
        "timeout": {"type": "integer", "description": "секунд, по умолчанию 120, максимум 900"}},
        "required": ["cmd"]}}}


def is_mercury_thread(tid):
    return bool(tid) and str(tid).startswith("merc-")


def mercury_key():
    return load_secrets().get("INCEPTION_API_KEY") or os.environ.get("INCEPTION_API_KEY")


def mercury_system():
    try:
        agents = open(os.path.join(HOME, "AGENTS.md")).read()
    except OSError:
        agents = ""
    return (agents + "\n\n## Сейчас ты работаешь на быстрой модели Mercury 3\n"
            "У тебя один инструмент — `shell`. Файлы читай через cat/sed -n, правь через python3/sed/heredoc "
            "(сначала бэкап). Память — memory/notes.md, журнал — memory/journal.md: прочитай notes.md, если задача "
            "про сервер и ты его ещё не читал в этом разговоре. Действуй, а не рассуждай: короткие шаги, проверяй "
            "результат. Финальный ответ хозяину — обычным текстом без вызова инструмента, коротко, по-русски.")


async def mercury_chat(messages, tools=None, effort=None, timeout=120):
    """Один запрос к Mercury → message (dict) и usage."""
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    body = {"model": MERCURY_MODEL, "messages": messages,
            "reasoning_effort": effort or (CFG.get("effort") if CFG.get("model") == MERCURY_MODEL else None) or "low"}
    if tools:
        body["tools"] = tools
    for attempt in range(3):
        try:
            async with _http.post(MERCURY_URL, json=body, timeout=aiohttp.ClientTimeout(total=timeout),
                                  headers={"Authorization": f"Bearer {mercury_key()}"}) as r:
                d = await r.json(content_type=None)
            if r.status >= 500 or r.status == 429:
                raise RuntimeError(f"Mercury HTTP {r.status}: {str(d)[:200]}")
            if r.status != 200:
                raise ValueError(f"Mercury HTTP {r.status}: {str(d)[:300]}")
            return d["choices"][0]["message"], d.get("usage", {})
        except ValueError:
            raise
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))


async def llm(prompt, system=None, effort="low"):
    """Быстрый ответ Mercury без инструментов — для модов и служебных сводок."""
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    m, _ = await mercury_chat(msgs, effort=effort)
    return (m.get("content") or "").strip()


QUICK_SYSTEM = ("Ты — Nook, админ-бот сервера в Telegram. Пишешь хозяину по-русски, коротко и по-дружески, "
                "простым markdown (**жирный**, списки через -), без таблиц и заголовков. Только текст ответа.")


async def quick_llm(prompt, system=QUICK_SYSTEM, timeout=45):
    """Служебная мелочь на Mercury (не тратит лимит Codex). None — если не вышло: тогда делаем по-старому."""
    if not mercury_key():
        return None
    try:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        m, _ = await asyncio.wait_for(mercury_chat(msgs, effort="low", timeout=timeout), timeout + 15)
        return (m.get("content") or "").strip() or None
    except Exception as e:
        log.info("quick llm failed: %s", e)
        return None


async def run_mercury(prompt, thread_id=None, images=(), on_step=None, run: Run = None, timeout=USER_TIMEOUT):
    """Как run_codex, но на Mercury: (thread_id, final, error)."""
    os.makedirs(MERCURY_DIR, exist_ok=True)
    if not mercury_key():
        return thread_id, "", "нет INCEPTION_API_KEY в secrets.env"
    tid = thread_id if is_mercury_thread(thread_id) else "merc-" + uuid.uuid4().hex[:12]
    path = os.path.join(MERCURY_DIR, tid + ".json")
    msgs = load_json(path, None) or [{"role": "system", "content": mercury_system()}]
    msgs[0] = {"role": "system", "content": mercury_system()}  # AGENTS.md мог поменяться
    if images:
        prompt += "\n\n[хозяин прислал картинку, но быстрая модель картинки не видит — скажи ему об этом, если важно]"
    msgs.append({"role": "user", "content": prompt})
    run = run or Run()
    run_id = uuid.uuid4().hex[:12]
    RUN_THREADS[run_id] = tid
    env = codex_env(run_id)
    final, err, started = "", None, time.time()
    try:
        for _ in range(MERCURY_STEPS):
            if run.cancelled:
                err = "остановлено"
                break
            if time.time() - started > timeout:
                err = f"таймаут {timeout // 60} мин"
                break
            m, _ = await mercury_chat(msgs, [SHELL_TOOL])
            calls = m.get("tool_calls") or []
            msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls") and v is not None})
            if not calls:
                final = (m.get("content") or "").strip()
                break
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                cmd = str(args.get("cmd", ""))
                if on_step:
                    await on_step("▸ " + cmd.replace("\n", " ")[:120])
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-lc", cmd, cwd=HOME, env=env, start_new_session=True,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                run.proc = proc
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), min(int(args.get("timeout") or 120), 900))
                    out = out.decode(errors="replace")
                    code = proc.returncode
                except asyncio.TimeoutError:
                    run.cancel()
                    run.cancelled = False
                    out, code = "таймаут команды", -1
                if on_step:
                    await on_step(("✓ " if code == 0 else "✗ ") + cmd.replace("\n", " ")[:120])
                if len(out) > 8000:
                    out = out[:3000] + "\n…[обрезано]…\n" + out[-5000:]
                msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": f"exit {code}\n{out}"})
        else:
            err = f"не уложился в {MERCURY_STEPS} шагов"
    except Exception as e:
        err = f"Mercury: {e}"
    # длинные разговоры: системный + хвост не больше ~60 сообщений и ~120k символов (≈35k токенов)
    tail, size = [], 0
    for mm in reversed(msgs[1:]):
        size += len(str(mm.get("content") or "")) + len(json.dumps(mm.get("tool_calls") or ""))
        if len(tail) >= 60 or (size > 120_000 and tail):
            break
        tail.insert(0, mm)
    while tail and tail[0].get("role") == "tool":
        tail = tail[1:]
    msgs = msgs[:1] + tail
    save_json(path, msgs)
    return tid, final, err



def antigravity_result(raw, returncode):
    """CLI может вернуть SUCCESS даже при запрещённом инструменте."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected object")
    except (ValueError, TypeError):
        return "", "Antigravity вернул некорректный ответ", {}
    text = data.get("response")
    text = text.strip() if isinstance(text, str) else ""
    if data.get("denied_actions"):
        return text, "Antigravity: инструменту отказано в доступе; задача не завершена", data
    if returncode or data.get("status") not in (None, "SUCCESS"):
        return text, f"Antigravity: ошибка выполнения (код {returncode})", data
    if not text:
        return "", "Antigravity не прислал ответ; задача не подтверждена", data
    return text, None, data


def antigravity_capacity_error(log_path):
    """CLI retries Google capacity errors for minutes; expose the actual cause."""
    try:
        with open(log_path, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 65536))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    match = re.search(r"No capacity available for model ([a-zA-Z0-9_.-]+)", tail)
    if match:
        return (f"Gemini перегружен: у Google сейчас нет свободных мощностей для {match[1]}. "
                "Попробуй позже или выбери другую глубину кнопкой «Модель».")
    return None

async def wait_antigravity(proc, log_path, timeout):
    """Drain both pipes while watching this invocation's private diagnostic log."""
    output = asyncio.create_task(proc.communicate())

    async def stop():
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            return await asyncio.wait_for(asyncio.shield(output), 5)
        except asyncio.TimeoutError:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return await asyncio.wait_for(asyncio.shield(output), 5)

    deadline = time.monotonic() + timeout
    try:
        while True:
            reason = antigravity_capacity_error(log_path)
            if reason:
                out, diagnostic = await stop()
                return out, diagnostic, reason
            if output.done():
                out, diagnostic = await output
                return out, diagnostic, antigravity_capacity_error(log_path)
            left = deadline - time.monotonic()
            if left <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait({output}, timeout=min(0.5, left))
    finally:
        if not output.done():
            await stop()

async def run_antigravity(prompt, thread_id=None, images=(), on_step=None, run: Run = None, timeout=USER_TIMEOUT):
    """Один автономный вызов CLI-агента Antigravity. (thread_id, final, error)."""
    tid = thread_id if str(thread_id or "").startswith("agy-") else "agy-" + uuid.uuid4().hex[:12]
    if images:
        prompt += "\n\n[хозяин прислал картинку, но CLI-модель картинки не видит — скажи ему об этом, если важно]"
    run = run or Run()
    eff = CFG.get("effort") or "medium"
    if eff not in ("low", "medium", "high"):
        eff = "medium"
    agy_timeout = min(timeout, 180) if eff == "high" else timeout
    mid = f"gemini-3.8-flash-{eff}" if CFG.get("model") == "antigravity-flash38" else f"gemini-4-argon-{eff}"
    # Доверенный агент хозяина работает без интерактивного терминала, как Codex.
    # Системные права при этом по-прежнему ограничены пользователем ОС/rootdo.
    with tempfile.TemporaryDirectory(prefix="agy-run-", dir=os.path.join(HOME, "spool")) as log_dir:
        log_path = os.path.join(log_dir, "cli.log")
        cmd = [AGY_BIN, "--model", mid, "--log-file", log_path, "--dangerously-skip-permissions", "--output-format", "json", "-p", prompt]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=HOME, env=codex_env(), start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        run.proc = proc
        try:
            out, diagnostic, backend_error = await wait_antigravity(proc, log_path, agy_timeout)
            if backend_error:
                final, err, data = "", backend_error, {}
            else:
                final, err, data = antigravity_result(out.decode(errors="replace").strip(), proc.returncode)
            if not err:
                usage = data.get("usage") or {}
                tok = int(usage.get("total_tokens") or 0)
                nowd = dt.date.today().isoformat()
                agy = STATE.setdefault("agy_usage", {"date": nowd, "tokens": 0, "tasks": 0})
                if agy["date"] != nowd:
                    agy["date"], agy["tokens"], agy["tasks"] = nowd, 0, 0
                agy["tokens"] += tok
                agy["tasks"] += 1
                agy["last"] = time.time()
                save_state()
            elif diagnostic:
                log.warning("Antigravity failed: exit=%s, diagnostic_bytes=%s", proc.returncode, len(diagnostic))
        except asyncio.TimeoutError:
            final, err = "", (f"Gemini не отвечает: ожидание {agy_timeout} с истекло, запрос остановлен. "
                              "Попробуй позже или выбери другую глубину кнопкой «Модель».")
    return tid, final, err


def fallback_active():

    if is_antigravity_model(CFG.get("model")):  # принудительная антигравити — без отката на Mercury
        return False
    return STATE.get("fallback_until", 0) > time.time()


def engine_for(thread_id=None):
    """Чем выполнять: Mercury/Antigravity — если выбраны вручную; Mercury — и при лимите Codex."""
    model = CFG.get("model")
    if is_antigravity_model(model):
        return "antigravity"
    if model == MERCURY_MODEL or fallback_active():
        return "mercury"
    return "codex"


async def run_agent(prompt, thread_id=None, images=(), on_step=None, run=None, timeout=USER_TIMEOUT):
    engine = engine_for(thread_id)
    if thread_id and is_mercury_thread(thread_id) != (engine == "mercury"):
        # разговор другого движка продолжить нельзя: свой разговор-двойник (xthreads) + прошлый ответ как контекст
        twin = STATE.get("xthreads", {}).get(thread_id)
        if not twin:
            ctx = STATE.get("last_answer", {}).get(thread_id, "")
            if ctx:
                prompt = f"[контекст: твой прошлый ответ в этом разговоре был]\n{ctx[:3000]}\n\n" + prompt
        thread_id = twin
    if engine == "antigravity":
        return await run_antigravity(prompt, thread_id, images, on_step, run, timeout)
    if engine == "mercury":
        return await run_mercury(prompt, thread_id, images, on_step, run, timeout)
    return await run_codex(prompt, thread_id, images, on_step, run, timeout)


# ---------------------------------------------------------------- лимиты Codex

CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")


async def limits_live():
    """Спросить лимиты у codex app-server (JSON-RPC по stdio)."""
    proc = await asyncio.create_subprocess_exec(
        CODEX, "app-server", env=codex_env(), start_new_session=True, limit=4 * 1024 * 1024,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        for m in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "nook", "version": "1"}}},
                  {"method": "initialized"},
                  {"id": 2, "method": "account/rateLimits/read"}):
            proc.stdin.write((json.dumps(dict(jsonrpc="2.0", **m)) + "\n").encode())
        await proc.stdin.drain()

        async def read():
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("id") == 2:
                    if "error" in ev:
                        raise RuntimeError(ev["error"].get("message", "error"))
                    return ev["result"]
        r = await asyncio.wait_for(read(), 20)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    rl = r["rateLimits"]
    win = lambda w: w and {"used": w["usedPercent"], "mins": w["windowDurationMins"], "reset": w["resetsAt"]}
    return {"primary": win(rl.get("primary")), "secondary": win(rl.get("secondary")),
            "credits": rl.get("credits"), "plan": rl.get("planType"),
            "reached": rl.get("rateLimitReachedType"),
            "resets_avail": (r.get("rateLimitResetCredits") or {}).get("availableCount"),
            "at": time.time(), "live": True}


def limits_cached():
    """Лимиты из последнего token_count в логах сессий (на момент последнего запуска)."""
    files = []
    for root, _, names in os.walk(CODEX_SESSIONS):
        files += [os.path.join(root, n) for n in names if n.endswith(".jsonl")]
    for path in sorted(files, key=os.path.getmtime, reverse=True)[:5]:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 2 * 2**20))
            lines = f.read().splitlines()
        for raw in reversed(lines):
            if b'"rate_limits"' not in raw:
                continue
            try:
                ev = json.loads(raw)
                rl = ev["payload"]["rate_limits"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if not rl:
                continue
            win = lambda w: w and {"used": w["used_percent"], "mins": w["window_minutes"], "reset": w["resets_at"]}
            ts = dt.datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00")).timestamp()
            return {"primary": win(rl.get("primary")), "secondary": win(rl.get("secondary")),
                    "credits": rl.get("credits"), "plan": rl.get("plan_type"),
                    "reached": rl.get("rate_limit_reached_type"), "at": ts, "live": False}
    return None


LIMITS_CACHE = {"data": None, "at": 0.0}


async def get_limits(max_age=60):
    """Лимиты: из памяти, если свежие, иначе живой запрос (≈1 с), при ошибке — из логов."""
    if FORCE.get():
        max_age = 0
    if LIMITS_CACHE["data"] and time.time() - LIMITS_CACHE["at"] < max_age:
        return LIMITS_CACHE["data"]
    try:
        L = await limits_live()
        LIMITS_CACHE.update(data=L, at=time.time())
        return L
    except Exception as e:
        log.warning("live limits failed: %s", e)
        return await asyncio.to_thread(limits_cached)


def win_name(mins):
    if mins >= 10080 and mins % 10080 == 0:
        return "неделя" if mins == 10080 else f"{mins // 10080} нед"
    if mins >= 1440 and mins % 1440 == 0:
        return f"{mins // 1440} дн"
    return f"{mins // 60} ч" if mins >= 60 else f"{mins} мин"


def until(ts):
    s = max(0, int(ts - time.time()))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    left = f"{d} д {h} ч" if d else f"{h} ч {m} мин" if h else f"{m} мин"
    when = dt.datetime.fromtimestamp(ts)
    day = "" if when.date() == dt.date.today() else when.strftime("%d.%m ")
    return f"через {left} ({day}{when:%H:%M})"


def bar(pct, n=10):
    full = min(n, round(pct / 100 * n))
    return "▰" * full + "▱" * (n - full)


def fmt_limits(L):
    if not L:
        return f"{E('err')} Не смог узнать лимиты Codex (нет ответа от app-server и нет логов сессий)."
    lines = [f"{E('limits')} **Лимиты Codex**" + (f" · {L['plan']}" if L.get("plan") else "")]
    for key in ("primary", "secondary"):
        w = L.get(key)
        if not w:
            continue
        icon = "🟥" if w["used"] >= 90 else "🟨" if w["used"] >= 70 else "🟩"
        lines.append(f"{icon} {win_name(w['mins'])}: {bar(w['used'])} **{w['used']:.0f}%**\n"
                     f"     сброс {until(w['reset'])}")
    c = L.get("credits") or {}
    if c.get("unlimited"):
        lines.append(f"{E('credits')} кредиты: безлимит")
    elif c.get("has_credits") or c.get("hasCredits"):
        lines.append(f"{E('credits')} кредиты: {float(c.get('balance') or 0):.0f}")
    if L.get("resets_avail"):
        lines.append(f"{E('gift')} бесплатных сбросов лимита: {L['resets_avail']}")
    if L.get("reached"):
        lines.append(f"{E('stop')} лимит исчерпан ({L['reached']})")
    if not L.get("live"):
        ago = int(time.time() - L["at"]) // 60
        lines.append(f"*(по последнему запуску, {ago} мин назад)*")
    return "\n".join(lines)


async def limits_note(engine=None):
    if engine is not None and engine != "codex":
        return ""
    """Строчка про лимиты под ответом — только когда их осталось мало."""
    try:
        L = await get_limits()
    except Exception:
        return ""
    if not L or not any(w and w["used"] >= 70 for w in (L.get("primary"), L.get("secondary"))):
        return ""
    return f"\n\n*{E('limits')} лимит Codex: {limits_short(L)}*"


def limits_short(L):
    if not L:
        return ""
    parts = [f"{win_name(w['mins'])} {w['used']:.0f}%" for w in (L.get("primary"), L.get("secondary")) if w]
    return " · ".join(parts)


# ---------------------------------------------------------------- lanes

class Lane:
    """Одна очередь задач: у хозяина — своя, у фоновых (надзор/планировщик) — своя."""

    def __init__(self, name):
        self.name = name
        self.lock = asyncio.Lock()
        self.current: Run | None = None
        self.waiting = 0


user_lane = Lane("user")
bg_lane = Lane("bg")


class Progress:
    """Статус-сообщение, которое показывает, что агент сейчас делает."""

    def __init__(self, chat_id, title):
        self.chat_id, self.title = chat_id, title
        self.lines, self.msg_id, self.last_edit, self.count = [], None, 0.0, 0
        self.started = time.time()
        self.rich = ui_mode() == "rich"

    async def start(self, reply_to=None):
        self.msg_id = await post(self.chat_id, f"{self.title}…", stop_kb(), reply_to, silent=True, cache=False)

    def render(self):
        el = int(time.time() - self.started)
        body = "\n".join(l.replace("`", "ˋ") for l in self.lines[-8:])[:3000]
        return f"{self.title} · {el // 60}:{el % 60:02d}" + (f"\n\n```\n{body}\n```" if body else "")

    async def step(self, line):
        if line.startswith("✓") or line.startswith("✗"):
            # заменить строку «▸ cmd» на результат
            for i in range(len(self.lines) - 1, -1, -1):
                if self.lines[i][2:] == line[2:]:
                    self.lines[i] = line
                    break
            else:
                self.lines.append(line)
        else:
            self.lines.append(line)
            if line.startswith("▸"):
                self.count += 1
        if time.time() - self.last_edit > 2.5:
            await self.flush()

    async def flush(self):
        if not self.msg_id:
            return
        self.last_edit = time.time()
        if await edit(self.chat_id, self.msg_id, self.render(), stop_kb(), self.rich, cache=False) is None:
            self.msg_id = None  # прогресс удалили (например, «Очистить чат») — больше не трогаем

    async def finish(self):
        """Прогресс нужен только пока идёт работа — потом убираем, чтобы не мусорить в чате."""
        if not self.msg_id:
            return
        try:
            await bot.delete_message(self.chat_id, self.msg_id)
        except Exception:
            pass


SILENT_RE = re.compile(r"^\W*SILENT\W*$", re.I)
PROGRESS_RUNS = {}  # id сообщения (прогресс или сообщение хозяина) -> Run: «отмена» реплаем


async def pause_for_limit(item, partial=""):
    """Отложить задачу до сброса лимита Codex и включить запасную модель."""
    L = await get_limits(0)
    L = L or {}
    resets = [w["reset"] for w in (L.get("primary"), L.get("secondary")) if w and w["used"] >= 99]
    reset = max(resets) if resets else time.time() + 3600
    item.update(at=time.time(), reset=reset)
    q = STATE.setdefault("paused", [])
    q.append(item)
    del q[:-10]
    STATE["fallback_until"] = max(STATE.get("fallback_until", 0), reset)
    save_state()
    if not item["background"] or item.get("incident"):
        await notify(f"{E('stop')} **Лимит Codex кончился посреди задачи.** Продолжу её сам после сброса "
                     f"({until(reset)}). Пока новые задачи делаю на быстрой модели Mercury ⚡."
                     + (f"\n\n*Успел:* {partial[-800:]}" if partial else ""),
                     markup=kb((btn("Лимиты", "limits!", "limits"), btn("Меню", "menu", "home"))), kind="limit")


async def resume_loop():
    """Автопродолжение задач, прерванных лимитом, и выход из запасного режима после сброса."""
    await asyncio.sleep(45)
    while True:
        try:
            q = STATE.get("paused", [])
            fb = STATE.get("fallback_until", 0)
            if (q or fb) and time.time() >= min([i["reset"] for i in q] + ([fb] if fb else [])):
                L = await get_limits(0)
                ok = L and not L.get("reached") and all(w["used"] < 97 for w in (L.get("primary"), L.get("secondary")) if w)
                if ok:
                    was_fb = fb > 0
                    STATE["fallback_until"] = 0
                    items, STATE["paused"] = list(q), []
                    save_state()
                    if was_fb:
                        digest("Лимит Codex сбросился — вернулся на основную модель"
                               + (f", доделал прерванных задач: {len(items)}" if items else ""))
                    for it in items:
                        lane = user_lane if it["lane"] == "user" else bg_lane
                        prompt = ("[АВТОПРОДОЛЖЕНИЕ] Лимит Codex сбросился. Продолжи прерванную задачу с того места, "
                                  "где остановился (проверь, что уже сделано). Если всё готово — коротко отчитайся."
                                  if it.get("thread") else it["prompt"])
                        asyncio.create_task(run_task(lane, prompt, thread_id=it.get("thread"), title=it["title"],
                                                     background=it["background"], header=it["header"],
                                                     task=it.get("task"), set_main=it.get("set_main"),
                                                     images=[i for i in it.get("images", []) if os.path.exists(i)],
                                                     incident=it.get("incident"), show_progress=not it["background"],
                                                     resumed=True))
                else:  # ещё не сбросился — проверим через 10 минут
                    for it in q:
                        it["reset"] = time.time() + 600
                    if fb:
                        STATE["fallback_until"] = time.time() + 600
                    save_state()
        except Exception:
            log.exception("resume loop failed")
        await asyncio.sleep(60)


async def run_task(lane: Lane, prompt, *, thread_id=None, images=(), title=E("busy") + " думаю",
                   reply_to=None, show_progress=True, background=False, set_main=False,
                   header="", incident=None, task=None, resumed=False):
    """Выполнить задачу в очереди lane и отправить результат хозяину."""
    chat = STATE["owner_id"]
    if not chat:
        return
    if lane.lock.locked() and not background:
        lane.waiting += 1
        wait_mid = await send(chat, f"{E('jobs')} закончу текущее и возьмусь", reply_to=reply_to, silent=True,
                              markup=kb((btn("Остановить текущее", "stop:p" if lane is user_lane else "stop:bg",
                                             "stop", "danger"),)))
    else:
        wait_mid = None
    async with lane.lock:
        if wait_mid:
            await delete_msgs(chat, [wait_mid])
        if lane.waiting:
            lane.waiting -= 1
        run = Run()
        lane.current = run
        prog = Progress(chat, title) if show_progress else None
        try:
            engine = engine_for(thread_id)
            if prog:
                if engine == "mercury":
                    prog.title = prog.title + " ⚡"
                await prog.start(reply_to)
                PROGRESS_RUNS[prog.msg_id] = run
            if reply_to:
                PROGRESS_RUNS[reply_to] = run
            tid, final, err = await run_agent(
                prompt, thread_id, images, prog.step if prog else None, run,
                BG_TIMEOUT if background else USER_TIMEOUT)
        finally:
            lane.current = None
            if prog:
                await prog.finish()
                PROGRESS_RUNS.pop(prog.msg_id, None)
            if reply_to:
                PROGRESS_RUNS.pop(reply_to, None)
        if thread_id and tid and tid != thread_id:  # разговор-двойник на другом движке — дальше продолжать его же
            xt = STATE.setdefault("xthreads", {})
            xt[thread_id], xt[tid] = tid, thread_id
            for k in list(xt)[:-100]:
                del xt[k]
        if set_main and tid:
            STATE["main_thread"] = tid
            STATE["main_at"] = time.time()
            save_state()
        if err and engine == "codex" and LIMIT_RE.search(err) and not run.cancelled:
            # лимит кончился посреди задачи: запомнить и продолжить после сброса, а пока — Mercury
            await pause_for_limit(dict(lane=lane.name, prompt=prompt, thread=tid or thread_id, title=title,
                                       background=background, header=header, task=task, reply_to=reply_to,
                                       set_main=set_main, incident=incident, images=list(images)), final)
            return tid
        if background and not err and (not final or SILENT_RE.match(final)):
            log.info("bg task silent: %s", prompt[:80])
            return tid
        text = header + (final or "")
        if err:
            text += f"\n\n{E('err')} {err}"
        text += await limits_note(engine)
        markup = result_kb(final, incident, err, fav=bool(task), mercury=is_mercury_thread(tid)) if tid else kb(back())
        if background or run.cancelled:
            markup = with_close(markup)
        mid = await send(chat, text.strip() or "(нет ответа)", reply_to=reply_to, markup=markup)
        remember_msg(mid, tid)
        if multi_questions(final):
            mq = STATE.setdefault("mq", {})
            mq[str(mid)] = {"q": multi_questions(final), "a": {}}
            for k in list(mq)[:-30]:
                del mq[k]
        if tid and final:
            la = STATE.setdefault("last_answer", {})
            la[tid] = final[-3000:]
            for k in list(la)[:-30]:
                del la[k]
        if task:
            mt = STATE.setdefault("msg_tasks", {})
            mt[str(mid)] = {"p": task, "t": title}
            for k in list(mt)[:-300]:
                del mt[k]
            save_state()
        if incident:
            STATE.setdefault("incident_msgs", {})[str(mid)] = incident
            trim = STATE["incident_msgs"]
            for k in list(trim)[:-100]:
                del trim[k]
            save_state()
        return tid


# ---------------------------------------------------------------- меню

# простые кнопки (/start2): эмодзи в тексте, без премиум-иконок и цвета — для клиентов, где их не видно.
# Режим берётся из сообщения, на котором нажали кнопку, иначе — последний выбор /start или /start2.
FORCE = contextvars.ContextVar("force", default=False)  # «Обновить» — без кеша


def plain_buttons():
    return ui_mode() == "classic"


from ui_settings import option as ui_option, filter_keyboard, settings_view, apply_action


def custom_keyboard(markup, scope):
    return filter_keyboard(STATE, markup, scope, kb, btn)


async def view_settings():
    return settings_view(STATE, btn, kb)


async def view_settings_answers():
    return settings_view(STATE, btn, kb, "answers")


async def view_settings_menu():
    return settings_view(STATE, btn, kb, "menu")


async def view_settings_look():
    return settings_view(STATE, btn, kb, "look")


def btn(text, data, icon=None, style=None):
    """Кнопка с премиум-иконкой (icon — имя из emoji.json) и цветом (primary/success/danger)."""
    extra = {}
    if not ui_option(STATE, "icons"):
        icon = None
    if not ui_option(STATE, "colors"):
        style = None
    if not plain_buttons() and ui_option(STATE, "premium_icons") and icon in EMOJI:
        extra["icon_custom_emoji_id"] = EMOJI[icon][0]
    elif icon:
        text = f"{EMOJI.get(icon, ['', ''])[1]} {text}".strip()
    if style and not plain_buttons():
        extra["style"] = style
    return InlineKeyboardButton(text=text, callback_data=data, **extra)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def back(to="menu"):
    return (btn("Назад", to, "back"), btn("Меню", "menu", "home")) if to != "menu" else (btn("Меню", "menu", "back"),)


def stop_kb():
    return kb((btn("Остановить", "stop:p", "stop", "danger"),))


# «Перезапустить postgres?» — да/нет; «основной или новая нода?», «какой порт?» — нет (тут нужен ответ словами/ask)
WH_RE = re.compile(r"(?i)(?<!\w)(или|либо|какой|какая|какое|какие|какую|каким|что|чего|где|куда|откуда|"
                   r"сколько|когда|кто|кого|как|почему|зачем|чей|which|what|where|when|how|or)(?!\w)")


def yes_no_question(final):
    """Последний вопрос в ответе — простой да/нет?"""
    tail = (final or "")[-600:].rstrip()
    end = tail.rfind("?")
    if end < 0 or tail[end + 1:].strip(" *_)»\"'"):  # после вопроса ещё текст — это не финальный вопрос
        return False
    # начало последнего предложения: после «. », «! », «? » или перевода строки (точки в IP/версиях не считаем)
    start = max(m.end() for m in re.finditer(r"[.!?]\s|\n|^", tail[:end]))
    return not WH_RE.search(tail[start:end + 1])


MQ_RE = re.compile(r"(?m)^\s*(\d{1,2})[.)]\s+(.+\?)\s*\**\s*$")


def multi_questions(final):
    """Пронумерованные вопросы в ответе («1. … ?», «2. … ?») → номера, если их ≥ 2."""
    nums = [m.group(1) for m in MQ_RE.finditer(final or "")]
    return nums if len(nums) >= 2 and len(set(nums)) == len(nums) else []


def mq_kb(nums, answers, extra_rows=()):
    rows = []
    for n in nums:
        a = answers.get(n)
        rows.append((btn(("✓ " if a == "y" else "") + f"{n}. Да", f"mq:{n}:y", None, "success" if a == "y" else None),
                     btn(("✓ " if a == "n" else "") + f"{n}. Нет", f"mq:{n}:n", None, "danger" if a == "n" else None)))
    return custom_keyboard(kb(*rows, *extra_rows), "answer")


def result_kb(final, incident=None, err=None, fav=False, mercury=False):
    """Кнопки под ответом агента: продолжение разговора одним нажатием."""
    rows = []
    if mercury:
        rows.append((btn("Продолжить", "th:continue", "repeat", "primary"),))
    nums = multi_questions(final)
    if nums:
        return mq_kb(nums, {}, rows + [(btn("Меню", "menu", "home"),)])
    if yes_no_question(final):
        rows.append((btn("Да, делай", "th:yes", "ok", "success"), btn("Не надо", "th:no", "cross", "danger")))
    rows.append((btn("Меню", "menu", "home"),) + ((btn("В избранное", "fav", "spark"),) if fav and not err else ()))
    if incident:
        rows.append((btn("Не следить за этим", "mute", "eyeoff"),))
    if err and any(x in err for x in ("Gemini перегружен", "Gemini не отвечает")):
        rows.insert(0, (btn("Модель", "model", "robot", "primary"),))
        rows.append((btn("Закрыть", "close", "cross"),))
    if err and ("login" in err or "авторизация Codex" in err):
        rows.insert(0, (btn("Войти в Codex", "login", "login", "primary"),))
    return custom_keyboard(kb(*rows), "answer")


THREAD_REPLIES = {
    "continue": "Продолжи предыдущий ответ или незавершённую задачу с того места, где остановился. Не повторяй уже сказанное.",
    "yes": "Да, делай, как предлагаешь.",
    "no": "Нет, не надо. Оставь как есть.",
    "more": "Расскажи подробнее: что именно видишь, почему так думаешь, какие варианты.",
    "undo": "Отмени то, что ты сделал в этой задаче: верни файлы из бэкапов, откати изменения, проверь, что всё "
            "работает как до задачи. Если что-то откатить нельзя — скажи что.",
}

QUICK = {
    "check": (E("check") + " проверяю дом",
              "Проверь, как дела на сервере: что лежит или падало, диск, память, свежие ошибки в логах. "
              "Ничего не удаляй. Ответь коротко: что хорошо, что плохо, что предлагаешь (списком)."),
    "clean": (E("clean") + " убираюсь",
              "Сделай уборку по правилам: измерь диск, почисти очевидный мусор (кэши, pm2 flush, гигантские логи, "
              "docker image/builder prune без volumes). Скажи, сколько освободил. То, что можно удалить только "
              "с моего разрешения, перечисли списком с размерами — я выберу."),
}


def busy_line():
    parts = []
    for lane, who in ((user_lane, "твоя задача"), (bg_lane, "фоновая задача")):
        if lane.current:
            el = int(time.time() - lane.current.started)
            parts.append(f"{who} {el // 60}:{el % 60:02d}")
    return f"{E('busy')} работаю: " + ", ".join(parts) if parts else f"{E('ok')} свободен"


def jobs_list():
    return sorted(load_json(JOBS, []), key=lambda j: j["at"])


async def view_menu():
    du = shutil.disk_usage("/")
    L = await get_limits()
    short = limits_short(L)
    text = (f"{E('home')} **Nook**\n\n{busy_line()}\n"
            f"{E('disk')} диск {du.used / du.total * 100:.0f}%  ·  "
            f"{E('eye' if STATE['watch'] else 'eyeoff')} надзор {'вкл' if STATE['watch'] else 'выкл'}"
            + f"\n{E('robot')} {model_label()}" + (f"  ·  {short}" if short else "")
            + (f"\n{E('warn')} **полный root до {dt.datetime.fromtimestamp(full_until()):%H:%M}**"
               if full_until() else "")
            + "\n\n*Просто напиши, что сделать, — или выбери ниже.*")
    busy = user_lane.current or bg_lane.current
    n = len(jobs_list())
    return text, custom_keyboard(kb(
        (btn("Статус", "status", "status"), btn("Графики", "graph", "load"), btn("Лимиты", "limits", "limits")),
        (btn("Проверь всё", "q:check", "check", "success"), btn("Уборка", "q:clean", "clean", "success")),
        (btn(f"Задачи{f' · {n}' if n else ''}", "jobs", "jobs"),
         btn(f"Ноды{f' · {len(nodes())}' if nodes() else ''}", "nodes", "node"), btn("Ещё", "more", "more")),
        (btn(f"Избранное · {len(favs())}", "favs", "spark"),) if favs() else None,
        (btn(f"Сводка · {len(STATE.get('digest', []))}", "digest", "info"),) if STATE.get("digest") else None,
        (btn("Настройки", "settings", "more"),),
        *mod_rows("menu"),
        (btn("Остановить", "stop", "stop", "danger"),) if busy else None,
    ), "menu")


async def view_status():
    return f"{E('status')} **Статус сервера**\n\n" + await quick_status(), kb((btn("Обновить", "status!", "refresh"),) + back())


async def view_limits():
    codex, agy = await asyncio.gather(
        get_limits(), antigravity_limits.get_limits(force=bool(FORCE.get())))
    text = fmt_limits(codex) + "\n\n" + antigravity_limits.format_limits(agy, bar)
    return text, kb((btn("Обновить", "limits!", "refresh"),) + back())


async def view_jobs():
    jobs = jobs_list()
    if not jobs:
        return (f"{E('jobs')} **Отложенные задачи**\n\nПока пусто. Попроси, например: "
                "«каждые 6 часов смотри, не растёт ли диск» или «завтра в 10 напомни про бэкап»."), kb(back())
    lines, rows = [f"{E('jobs')} **Отложенные задачи**"], []
    for j in jobs:
        when = dt.datetime.fromtimestamp(j["at"]).strftime("%d.%m %H:%M")
        ev = f" · {E('repeat')} каждые {win_name(j['every'] // 60)}" if j.get("every") else ""
        lines.append(f"\n**{when}**{ev}\n{j['prompt'][:200]}")
        rows.append((btn(f"{when} · {j['prompt'][:25]}", f"jc:{j['id']}", "cross"),))
    return "\n".join(lines), kb(*rows, back())


MODELS = {  # из `codex app-server` model/list
    "gpt-6-luna": ("6 Luna", ["low", "medium", "high", "xhigh", "max"], "medium"),
    "gpt-6.1-sol": ("6.1 Sol", ["low", "medium", "high", "xhigh", "max", "ultra"], "low"),
    "mercury-3": ("Mercury 3 ⚡", ["low", "medium", "high"], "low"),  # не Codex: свой агент run_mercury
    "antigravity-argon": ("4 Argon", ["low", "medium", "high"], "medium"),
    "antigravity-flash38": ("3.8 Flash", ["low", "medium", "high"], "medium"),
}


def codex_default(key):
    try:
        m = re.search(rf'(?m)^{key}\s*=\s*"([^"]+)"', open(os.path.expanduser("~/.codex/config.toml")).read())
        return m.group(1) if m else None
    except OSError:
        return None


def current_model():
    model = CFG.get("model") or codex_default("model") or "gpt-6-luna"
    effort = CFG.get("effort") or codex_default("model_reasoning_effort") or MODELS.get(model, ("", [], "medium"))[2]
    return model, effort


def model_label():
    model, effort = current_model()
    if fallback_active() and model != MERCURY_MODEL:
        return "Mercury 3 ⚡ (лимит)"
    return f"{MODELS.get(model, (model,))[0]} · {effort}"


def save_config():
    save_json(CONFIG_PATH, CFG)
    os.chmod(CONFIG_PATH, 0o600)


async def view_model():
    model, effort = current_model()
    efforts = MODELS.get(model, ("", ["low", "medium", "high"], ""))[1]
    text = (f"{E('robot')} **Модель**\n\n"
            f"Сейчас: **{MODELS.get(model, (model,))[0]}** (`{model}`), effort **{effort}**"
            + (f"\n{E('warn')} лимит Codex кончился — до {dt.datetime.fromtimestamp(STATE['fallback_until']):%H:%M} "
               "работаю на Mercury 3 ⚡" if fallback_active() else "") +
            "\n\n*Effort — сколько модель думает перед ответом: выше — умнее, но дольше и быстрее съедает лимит.*\n"
            "*Mercury 3 ⚡ — очень быстрая модель Inception, не тратит лимит Codex; попроще в сложных задачах. "
            "Она же — запасная, когда лимит Codex кончается.*")
    items = [(m, name) for m, (name, _, _) in MODELS.items()]
    mrow = tuple(tuple(btn(("✓ " if m == model else "") + name, f"m:{m}", None, "primary" if m == model else None)
                       for m, name in items[i:i + 3]) for i in range(0, len(items), 3))
    erows = [tuple(btn(("✓ " if e == effort else "") + e, f"ef:{e}", None, "primary" if e == effort else None)
                   for e in efforts[i:i + 3]) for i in range(0, len(efforts), 3)]
    return text, kb(*mrow, *erows, back("more"))


async def view_muted():
    muted = STATE.get("muted", [])
    if not muted:
        return f"{E('eyeoff')} **Не слежу**\n\nСписок пуст — слежу за всем.", kb(back("more"))
    rows = [(btn(key_name(k)[:40], f"um:{i}", "eye"),) for i, k in enumerate(muted)]
    return (f"{E('eyeoff')} **Не слежу** за этим ({len(muted)})\n\nНажми, чтобы снова следить."), kb(*rows, back("more"))


# ---------------------------------------------------------------- ноды (другие серверы по SSH)
# Хозяин присылает адрес/логин/пароль один раз → ставим свой ключ → пароль забываем (на диск не пишется).

KEYS = os.path.join(HOME, "keys")
NODE_KEY = os.path.join(KEYS, "id_ed25519")
NODES_PATH = os.path.join(HOME, "nodes.json")
SSH_CONFIG = os.path.join(HOME, "ssh_config")
PENDING = {}  # chat_id -> {"kind": "node", "until": ts, "mid": id сообщения-подсказки}
FACTS_SH = ("echo host=$(hostname); . /etc/os-release 2>/dev/null; echo os=$PRETTY_NAME; echo cpu=$(nproc); "
            "free -m | awk '/Mem/{print \"ram=\"$2\" MB\"}'; df -h / | awk 'NR==2{print \"disk=\"$2\", занято \"$5}'; "
            "echo user=$(id -un); [ $(id -u) = 0 ] && echo root=yes || (sudo -n true 2>/dev/null && echo root=sudo || echo root=no)")


def nodes():
    return load_json(NODES_PATH, [])


def node_label(n):
    """Как ноду называет хозяин: своё название, иначе имя-алиас."""
    return n.get("title") or n["name"]


def node_ref(n):
    """Для агента: «название» (`алиас`) — чтобы понимал и как зовёт хозяин, и как вызывать."""
    return f"«{n['title']}» (`{n['name']}`)" if n.get("title") else f"`{n['name']}`"


def ssh_opts():
    return ["-F", SSH_CONFIG, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def write_nodes(items):
    save_json(NODES_PATH, items)
    lines = ["# генерируется ботом из nodes.json — не править руками", ""]
    for n in items:
        lines += ([f"# {n['title']}"] if n.get("title") else []) + [f"Host {n['name']}", f"  HostName {n['host']}", f"  User {n['user']}", f"  Port {n['port']}",
                  f"  IdentityFile {NODE_KEY}", "  IdentitiesOnly yes",
                  f"  UserKnownHostsFile {os.path.join(KEYS, 'known_hosts')}", "  StrictHostKeyChecking accept-new",
                  "  ServerAliveInterval 30", ""]
    with open(SSH_CONFIG, "w") as f:
        f.write("\n".join(lines))


async def ensure_node_key():
    os.makedirs(KEYS, mode=0o700, exist_ok=True)
    if not os.path.exists(NODE_KEY):
        p = await asyncio.create_subprocess_exec(
            "ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"nook@{os.uname().nodename}", "-f", NODE_KEY,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()


def node_pubkey():
    try:
        return open(NODE_KEY + ".pub").read().strip()
    except OSError:
        return "(ключ появится после первой ноды)"


def parse_node(text):
    """root@192.0.2.1[:22] пароль | 192.0.2.1 root пароль | адрес\\nлогин\\nпароль[\\nимя] → dict или None."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    name = None
    if len(lines) >= 3:
        addr, user, password = lines[0], lines[1], lines[2]
        name = lines[3] if len(lines) > 3 else None
        if "@" in addr:
            user, addr = addr.split("@", 1)
    else:
        parts = text.split()
        if len(parts) == 1 and "@" in parts[0]:  # без пароля: ключ Nook уже положен на ноду руками
            user, addr = parts[0].split("@", 1)
            password = None
        elif len(parts) < 2:
            return None
        elif "@" in parts[0]:
            user, addr = parts[0].split("@", 1)
            password = text.strip().split(None, 1)[1]
        elif len(parts) >= 3:
            addr, user = parts[0], parts[1]
            password = text.strip().split(None, 2)[2]
        else:
            return None
    # только латиница и точка в адресе: иначе «проверь диск на нодах» распознаётся как адрес+логин+пароль
    m = re.fullmatch(r"([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)(?::(\d+))?", addr)
    if not m or not re.fullmatch(r"[A-Za-z0-9._-]+", user):
        return None
    return {"host": m.group(1), "port": int(m.group(2) or 22), "user": user, "password": password,
            "title": name.strip()[:40] if name else None}


def looks_like_creds(text):
    """Похоже на попытку прислать адрес/пароль (тогда удаляем и переспрашиваем), а не на обычную просьбу."""
    first = (text.split() or [""])[0]
    return "@" in first or bool(re.fullmatch(r"[A-Za-z0-9.:-]+", first) and "." in first)


async def add_node(chat_id, spec, status=None):
    """Зайти по паролю, поставить ключ, проверить вход по ключу, сохранить. Пароль живёт только здесь.
    status — сообщение-подсказка «Новая нода»: дальше весь процесс показываем в нём же."""
    rich = ui_mode() == "rich"
    text = f"{E('node')} Подключаюсь к `{spec['user']}@{spec['host']}:{spec['port']}`…"
    if not (status and await edit(chat_id, status, text, None, rich)):
        status = await post(chat_id, text, silent=True)
    await ensure_node_key()
    base = ["-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={os.path.join(KEYS, 'known_hosts')}",
            "-o", "ConnectTimeout=15", "-p", str(spec["port"])]
    # хозяин явно добавляет этот сервер — забываем старый ключ хоста (сервер могли переустановить)
    kh = os.path.join(KEYS, "known_hosts")
    target = spec["host"] if spec["port"] == 22 else f"[{spec['host']}]:{spec['port']}"
    if os.path.exists(kh):
        p = await asyncio.create_subprocess_exec("ssh-keygen", "-R", target, "-f", kh,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()
    password = spec.pop("password")
    if password and not await copy_key(chat_id, status, spec, password, base, rich):
        return
    del password
    # вход по ключу + факты о машине
    p = await asyncio.create_subprocess_exec(
        "ssh", "-i", NODE_KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", *base,
        f"{spec['user']}@{spec['host']}", FACTS_SH, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), 30)
    if p.returncode != 0:
        await edit(chat_id, status, f"{E('err')} Не пускает по ключу Nook:\n```\n"
                   f"{out.decode(errors='replace')[-500:]}\n```\nКлюч для `authorized_keys` — в «Ноды» → «Добавить».",
                   None, rich)
        return
    facts = dict(l.split("=", 1) for l in out.decode(errors="replace").splitlines() if "=" in l)
    await save_node(chat_id, status, spec, facts, rich)


async def copy_key(chat_id, status, spec, password, base, rich):
    """Зайти по паролю и положить ключ Nook в authorized_keys. Пароль — только в env дочернего процесса."""
    env = dict(os.environ, SSHPASS=password)
    p = await asyncio.create_subprocess_exec(
        "sshpass", "-e", "ssh-copy-id", "-i", NODE_KEY + ".pub", "-o", "PubkeyAuthentication=no", *base,
        f"{spec['user']}@{spec['host']}", env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), 60)
    except asyncio.TimeoutError:
        p.kill()
        out = b"timeout"
    env.pop("SSHPASS", None)
    del env
    out = out.decode(errors="replace")
    if p.returncode != 0:
        why = ("неверный логин или пароль (или вход по паролю запрещён)" if p.returncode == 5 or "denied" in out
               else "не достучался (адрес/порт/файрвол?)" if re.search(r"timed out|refused|resolve|timeout|unreachable", out, re.I)
               else out.strip().splitlines()[-1][:200] if out.strip() else f"код {p.returncode}")
        await edit(chat_id, status, f"{E('err')} Не вышло подключиться к `{spec['host']}`: {why}", None, rich)
        return False
    return True


async def save_node(chat_id, status, spec, facts, rich):
    items = [n for n in nodes() if not (n["host"] == spec["host"] and n["port"] == spec["port"] and n["user"] == spec["user"])]
    name = re.sub(r"[^A-Za-z0-9_-]", "-", facts.get("host") or spec["host"])[:30]
    taken = {n["name"] for n in items}
    base_name, i = name, 2
    while name in taken:
        name, i = f"{base_name}-{i}", i + 1
    node = {"name": name, "title": spec.get("title"), "host": spec["host"], "port": spec["port"], "user": spec["user"],
            "added": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "facts": facts}
    write_nodes(items + [node])
    await edit(chat_id, status, f"{E('ok')} Нода **{spec.get('title') or name}** добавлена — вход по ключу, пароль забыт.\n"
               f"{facts.get('os', '?')} · {facts.get('cpu', '?')} CPU · {facts.get('ram', '?')} · диск {facts.get('disk', '?')}"
               f" · root: {facts.get('root', '?')}\n\n*Сейчас осмотрюсь там и запомню, что это за машина.*",
               kb((btn("Ноды", "nodes", "node"), btn("Меню", "menu", "home"))), rich)
    prompt = (f"[НОВАЯ НОДА] Хозяин добавил сервер **{name}** ({spec['user']}@{spec['host']}:{spec['port']}), "
              f"вход по твоему ключу настроен: `nodes {name} 'команда'` (навык `nodes`).\n"
              f"Факты: {json.dumps(facts, ensure_ascii=False)}\n\n"
              "Осмотри её (ТОЛЬКО чтение, ничего не меняй): что запущено (docker/systemd/pm2/порты), диск, память, "
              "что это за машина и зачем она. Запиши в memory/notes.md раздел «Ноды» → подраздел про неё. "
              "Хозяину — короткая сводка в 3–6 строк, и что предлагаешь (если есть).")
    asyncio.create_task(run_task(bg_lane, prompt, background=True, show_progress=False,
                                 header=E("node") + " "))


async def node_alive(n):
    try:
        p = await asyncio.create_subprocess_exec("ssh", *ssh_opts(), n["name"], "true",
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await asyncio.wait_for(p.wait(), 20) == 0
    except Exception:
        return False


async def remove_node(name):
    """Убрать свой ключ с ноды (если достучимся) и забыть её."""
    items = nodes()
    n = next((x for x in items if x["name"] == name), None)
    if not n:
        return False
    try:
        pub = open(NODE_KEY + ".pub").read().split()[1]
        p = await asyncio.create_subprocess_exec(
            "ssh", *ssh_opts(), name, f"sed -i.bak-nook '/{pub.replace('/', chr(92) + '/')}/d' ~/.ssh/authorized_keys",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p.wait(), 20)
    except Exception:
        pass
    write_nodes([x for x in items if x["name"] != name])
    return True


async def view_nodes():
    items = nodes()
    ent = dict((SNAPSHOT.get("data") or ({}, {}))[0])
    unknown = [n for n in items if f"node:{n['name']}" not in ent]
    for n, ok in zip(unknown, await asyncio.gather(*(node_alive(n) for n in unknown))):
        ent[f"node:{n['name']}"] = (ok, "")
    lines = [f"{E('node')} **Ноды** ({len(items)})", ""]
    rows = []
    for n in items:
        ok = ent.get(f"node:{n['name']}", (None,))[0]
        dot = "🟢" if ok else "🔴" if ok is False else "⚪"
        f = n.get("facts", {})
        lines.append(f"{dot} **{node_label(n)}** — `{n['user']}@{n['host']}`  ·  {f.get('os', '')}")
        rows.append((btn(node_label(n), f"nd:{n['name']}", None),))
    if not items:
        lines.append("Пока нет. Добавь сервер — я поставлю на него свой ключ и буду за ним присматривать.")
    return "\n".join(lines), kb((btn("Добавить ноду", "nodeadd", "plus", "success"),), *rows, back())


async def view_node(name):
    n = next((x for x in nodes() if x["name"] == name), None)
    if not n:
        return f"{E('err')} Ноды {name} больше нет.", kb(back("nodes"))
    f = n.get("facts", {})
    ok = await node_alive(n)
    text = (f"{E('node')} **{node_label(n)}** {'🟢 на связи' if ok else '🔴 не отвечает'}\n\n"
            + (f"имя для команд: `{n['name']}`\n" if n.get("title") else "") +
            f"`{n['user']}@{n['host']}:{n['port']}`\n{f.get('os', '?')} · {f.get('cpu', '?')} CPU · {f.get('ram', '?')}"
            f" · диск {f.get('disk', '?')} · root: {f.get('root', '?')}\nдобавлена {n.get('added', '?')}")
    return text, kb((btn("Проверь её", f"nq:{name}", "check", "success"), btn("Название", f"nren:{name}", "new")),
                    (btn("Удалить", f"ndel:{name}", "cross", "danger"),),
                    back("nodes"))


SKILLS_DIR = os.path.join(HOME, ".agents", "skills")
MCP_CONFIG = os.path.join(HOME, ".codex", "config.toml")


def list_skills():
    out = []
    for name in sorted(os.listdir(SKILLS_DIR)) if os.path.isdir(SKILLS_DIR) else []:
        try:
            head = open(os.path.join(SKILLS_DIR, name, "SKILL.md")).read(2000)
        except OSError:
            continue
        m = re.search(r"(?m)^description:\s*(.+)$", head)
        out.append((name, (m.group(1).strip() if m else "")))
    return out


def list_mcp():
    try:
        raw = open(MCP_CONFIG).read()
    except OSError:
        return []
    out = []
    for m in re.finditer(r"(?m)^\[mcp_servers\.([\w-]+)\]\s*$", raw):
        body = raw[m.end():].split("\n[", 1)[0]
        off = re.search(r"(?m)^enabled\s*=\s*false", body)
        out.append((m.group(1), not off))
    return out


async def view_skills():
    skills, mcp = list_skills(), list_mcp()
    lines = [f"{E('spark')} **Навыки и сервисы**", ""]
    lines.append(f"**Навыки** ({len(skills)}) — инструкции, как делать конкретные вещи:")
    lines += [f"- **{n}** — {d.split(' — ')[0][:90]}" for n, d in skills] or ["- пока нет"]
    lines += ["", f"**Сервисы** ({len(mcp)}) — подключённые MCP-серверы:"]
    lines += [f"- {'🟢' if on else '⚪'} **{n}**" for n, on in mcp] or ["- пока нет"]
    lines += ["", "*Научить новому: напиши «научись …» — сделаю навык. "
              "Подключить сервис: «подключи GitHub / Notion / …» — найду MCP и настрою.*"]
    return "\n".join(lines), kb(back("more"))


async def view_root():
    until_ts = full_until()
    installed = os.path.exists(ROOT_HELPER)
    state = (f"{E('warn')} **включён** до {dt.datetime.fromtimestamp(until_ts):%d.%m %H:%M}" if until_ts
             else f"{E('ok')} выключен — на каждую root-команду спрошу тебя кнопками")
    text = (f"{E('login')} **Root-доступ**\n\n{state}\n\n"
            "Без пароля всегда можно: службы, логи, apt. Для остального я присылаю команду с кнопками "
            "«Выполнить / Отказать». В режиме полного доступа выполняю сразу, без вопросов.\n\n"
            "*Всё, что выполнено от root, записывается в `/var/log/nook-root.log`.*")
    if not installed:
        text += "\n\n" + f"{E('err')} Помощник не установлен: `sudo bash ~/nook/setup/install-root.sh`"
    rows = [(btn("Полный на 1 час", "full:3600", "warn"), btn("Полный на 24 часа", "full:86400", "warn"))]
    if until_ts:
        rows = [(btn("Выключить полный доступ", "full:0", "ok", "success"),)]
    return text, kb(*rows, back("more"))


async def view_more():
    w = STATE["watch"]
    return (f"{E('more')} **Ещё**\n\n"
            f"{E('eye')} **Автонадзор** — каждые {win_name(WATCH_INTERVAL // 60)} смотрю за сервисами, сам чиню упавшее и пишу тебе; "
            f"в {CFG.get('patrol_time', '12:00')} — ежедневный обход.\n\n"
            f"{E('new')} **Новый разговор** — забыть контекст переписки (заметки о сервере остаются)."
            + (f" Сейчас в разговоре ~{ctx // 1000}k токенов, сжимаю на {CFG.get('compact_tokens', COMPACT_TOKENS) // 1000}k; "
               f"после {CFG.get('idle_reset_hours', IDLE_RESET_H)} ч тишины начинаю заново сам." if (ctx := context_tokens()) else "")
            + "\n\n"
            f"{E('login')} **Войти в Codex** — если слетела авторизация."), kb(
        (btn("Настройки интерфейса", "settings", "more"),),
        (btn(f"Модель: {model_label()}", "model", "robot", "primary"),),
        (btn(f"Навыки и сервисы · {len(list_skills()) + len(list_mcp())}", "skills", "spark"),),
        (btn(f"Охрана нод: {'вкл' if STATE.get('guard', True) else 'выкл'}", "guard", "shield"),),
        (btn(f"Моды · {len(REG.mods)}", "mods", "plus"), btn("Что я делал", "journal", "jobs")),
        (btn("Бэкапы", "backups", "disk"), btn("Обновления", "updcheck", "refresh")),
        (btn("Оплата серверов", "pay", "cal"), btn(f"Деплой{f' · {len(deploys())}' if deploys() else ''}", "deploy", "repeat")),
        (btn(f"Гости{f' · {len(guests())}' if guests() else ''}", "guests", "eye"),
         btn("Уведомления: " + ("тихо" if quiet() else "всё"), "nmode", "bell")),
        (btn("Root: " + (f"полный до {dt.datetime.fromtimestamp(full_until()):%H:%M}" if full_until()
                         else "по кнопке"), "root", "login", "danger" if full_until() else None),),
        (btn(f"Надзор: {'включён' if w else 'выключен'}", "watch",
             "eye" if w else "eyeoff", "success" if w else "danger"),),
        (btn(f"Не слежу · {len(STATE.get('muted', []))}", "muted", "eyeoff"),) if STATE.get("muted") else None,
        (btn("Новый разговор", "new", "new"), btn("Войти в Codex", "login", "login")),
        *mod_rows("more"),
        *(mod_rows("menu") if not ui_option(STATE, "menu_mods") else []),
        (btn("Статус", "status", "status"), btn("Графики", "graph", "load"), btn("Лимиты", "limits", "limits")),
        (btn("Проверь всё", "q:check", "check"), btn("Уборка", "q:clean", "clean")),
        (btn("Задачи", "jobs", "jobs"), btn("Ноды", "nodes", "node")),
        (btn("Избранное", "favs", "spark"), btn("Сводка", "digest", "info")),
        (btn("Очистить чат", "wipe", "clean"),),
        back())


VIEWS = {"settings": view_settings, "settings_answers": view_settings_answers,
         "settings_menu": view_settings_menu, "settings_look": view_settings_look, "menu": view_menu, "status": view_status, "limits": view_limits,
         "jobs": view_jobs, "more": view_more, "model": view_model, "muted": view_muted,
         "skills": view_skills, "root": view_root, "nodes": view_nodes}


def is_view_msg(mid):
    return mid in STATE.get("view_msgs", [])


async def show(chat_id, view, edit_id=None):
    """Показать экран меню. Редактируем только сообщения-меню; из уведомлений — новым сообщением."""
    text, markup = await VIEWS[view]()
    if edit_id and is_view_msg(edit_id) and await edit(chat_id, edit_id, text, markup):
        return
    mid = await post(chat_id, text, markup)
    old = [m for m in STATE.get("view_msgs", []) if m != edit_id]
    STATE["view_msgs"] = [m for m in STATE.get("view_msgs", []) if m == edit_id] + [mid]
    save_state()
    await delete_msgs(chat_id, old)  # одно меню в чате — старые убираем


async def delete_msgs(chat_id, ids):
    """Удалить сообщения пачками (бот может удалять только за последние 48 ч; остальные пропускаются)."""
    ids = sorted({i for i in ids if i})
    for k in range(0, len(ids), 100):
        try:
            await tg("deleteMessages", chat_id=chat_id, message_ids=ids[k:k + 100])
        except Exception as e:
            log.info("delete messages: %s", e)


def cancel_job(jid):
    with open(JOBS + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        jobs = load_json(JOBS, [])
        keep = [j for j in jobs if j["id"] != jid]
        save_json(JOBS, keep)
        return len(keep) != len(jobs)


def stop_all(lanes=None):
    stopped = []
    for lane in lanes or (user_lane, bg_lane):
        if lane.current:
            lane.current.cancel()
            stopped.append("твою задачу" if lane is user_lane else "фоновую задачу")
    return "⛔ Остановил " + " и ".join(stopped) if stopped else "Сейчас ничего не делаю"


def new_conversation():
    STATE["main_thread"] = None
    STATE["main_at"] = time.time()
    save_state()


async def owner_ask(text, title=E("busy") + " думаю", task=None):
    """Задача от хозяина в основном разговоре (из сообщения или кнопки)."""
    thread, ctx = main_thread_for_new_message()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    prompt = f"[сообщение хозяина из Telegram, {now}]\n{text}"
    if not thread:
        prompt = fresh_prompt(prompt, ctx)
    await run_task(user_lane, prompt, thread_id=thread, title=title, set_main=True, task=task)


CANCEL_WORDS = {"отмена", "отмени", "стоп", "stop", "cancel", "хватит"}
MORE_WORDS = {"?", "??", "подробнее", "поясни", "почему?"}
REPEAT_WORDS = {"повтори", "повтор", "ещё раз", "еще раз", "заново", "again"}


async def reply_command(msg, word):
    """Короткие команды ответом на сообщение бота: «.», «отмена», «?», «повтори». True — обработано."""
    target = msg.reply_to_message.message_id
    w = word.lower().rstrip("!. ") if word != "." else "."
    chat = msg.chat.id
    if w == ".":
        await delete_msgs(chat, [msg.message_id])
        if not await resend_classic(chat, target):
            await send_tmp(chat, f"{E('err')} Это сообщение я уже не помню (храню последние 200).", 30, reply_to=target)
        return True
    if w in CANCEL_WORDS:
        run = PROGRESS_RUNS.get(target)
        if run:
            run.cancel()
            await send_tmp(chat, f"{E('stop')} Остановил.", 30, reply_to=target, silent=True)
            return True
        thread = STATE["msg_threads"].get(str(target))
        if not thread:
            return False
        q = await send(chat, "Эта задача уже закончена. **Откатить то, что я в ней сделал?**", reply_to=target,
                       markup=kb((btn("Да, откати", "th:undo", "repeat", "danger"), btn("Нет", "menu", "cross"))))
        remember_msg(q, thread)  # кнопка th:undo найдёт разговор по этому сообщению
        save_state()
        return True
    if w in MORE_WORDS:
        thread = STATE["msg_threads"].get(str(target))
        if not thread:
            return False
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        await run_task(user_lane, f"[ответ хозяина в Telegram, {now}]\n{THREAD_REPLIES['more']}", thread_id=thread,
                       reply_to=msg.message_id, set_main=thread == STATE["main_thread"])
        return True
    if w in REPEAT_WORDS:
        t = STATE.get("msg_tasks", {}).get(str(target))
        if not t:
            return False
        await owner_ask(t["p"], t.get("t") or E("repeat") + " повторяю", task=t["p"])
        return True
    return False



# кнопки, после которых уведомление остаётся: переключатели и просмотр внутри самого сообщения
SLOW_VIEWS = ("status", "limits")
KEEP_AFTER = ("mq:", "gr:", "gg:", "fav", "close", "rshow:", "wipe")


def is_notice(msg):
    """Уведомление = сообщение с кнопкой «Закрыть» (её ставит notify)."""
    rich = getattr(msg, "rich_message", None)
    if rich:
        return bool(re.search(r"""['"]callback_data['"]:\s*['"]close['"]""", str(rich)))
    rm = getattr(msg, "reply_markup", None)
    return bool(rm and any(b.callback_data == "close" for r in rm.inline_keyboard for b in r))


@dp.callback_query()
async def on_button(cb: CallbackQuery):
    """После действия на уведомлении (Оплатил, Разберись, Обновить, Да…) оно больше не нужно — убираем из чата."""
    notice = cb.message and is_notice(cb.message) and not (cb.data or "").startswith(KEEP_AFTER)
    try:
        return await handle_button(cb)
    finally:
        if notice and STATE["owner_id"] and cb.from_user.id == STATE["owner_id"]:
            trash(cb.message.chat.id, cb.message.message_id, 1)
            for extra in STATE.get("msg_groups", {}).get(str(cb.message.message_id), []):
                trash(cb.message.chat.id, extra, 1)


async def handle_button(cb: CallbackQuery):
    if STATE["owner_id"] and cb.from_user.id != STATE["owner_id"] and is_guest(cb.from_user.id):
        return await guest_button(cb)
    if not STATE["owner_id"] or cb.from_user.id != STATE["owner_id"]:
        return await cb.answer()
    data, chat, mid = cb.data or "", cb.message.chat.id, cb.message.message_id
    # любая кнопка («Отмена», «Меню»…) отменяет ожидание ввода; кнопки, которые его ждут, ставят заново
    PENDING.pop(chat, None)
    # отвечаем в том же виде, что и нажатое сообщение
    UI.set("rich" if getattr(cb.message, "rich_message", None) else "classic")
    if data.startswith("custom:"):
        try:
            section = apply_action(STATE, data)
        except ValueError:
            return await cb.answer("Эта настройка уже недоступна")
        save_state()
        await cb.answer("Сохранено")
        return await show(chat, "settings" if section == "home" else "settings_" + section, mid)
    if data.endswith("!") and data[:-1] in VIEWS:
        FORCE.set(True)
        await cb.answer()
        async with waiting(chat, "обновляю"):
            return await show(chat, data[:-1], mid)
    if data in VIEWS:
        await cb.answer()
        if data in SLOW_VIEWS:
            async with waiting(chat, "собираю данные"):
                return await show(chat, data, mid)
        return await show(chat, data, mid)
    if data == "watch":
        STATE["watch"] = not STATE["watch"]
        save_state()
        await cb.answer("👀 Автонадзор включён" if STATE["watch"] else "😴 Автонадзор выключен")
        return await show(chat, "more", mid)
    if data == "close":
        await cb.answer()
        ids = msg_group(mid)
        STATE["view_msgs"] = [m for m in STATE.get("view_msgs", []) if m not in ids]
        save_state()
        return await delete_msgs(chat, ids)
    if data == "wipe":
        await cb.answer()
        return await edit(chat, mid, f"{E('warn')} **Очистить чат?**\n\nУдалю всю переписку со мной за последние 48 часов "
                          "(старше Telegram удалять не даёт). Память и заметки не трогаю.",
                          kb((btn("Да, очистить", "wipeok", "clean", "danger"), btn("Нет", "more", "back"))))
    if data == "wipeok":
        await cb.answer("Чищу…")
        await delete_msgs(chat, range(max(1, mid - 3000), mid + 1))
        STATE["view_msgs"] = []
        save_state()
        return await show(chat, "menu")
    if data == "new":
        new_conversation()
        await cb.answer("🆕 Начал новый разговор")
        return await show(chat, "menu", mid)
    if data == "stop":
        await cb.answer(stop_all())
        return await show(chat, "menu", mid) if is_view_msg(mid) else None
    if data == "stop:p":  # кнопка под прогрессом — останавливает только задачу хозяина
        return await cb.answer(stop_all([user_lane]))
    if data == "stop:bg":
        return await cb.answer(stop_all([bg_lane]))
    if data.startswith("m:") and data[2:] in MODELS:
        CFG["model"] = data[2:]
        if CFG.get("effort") not in MODELS[data[2:]][1]:
            CFG["effort"] = MODELS[data[2:]][2]
        save_config()
        await cb.answer(f"Модель: {model_label()}")
        return await show(chat, "model", mid)
    if data.startswith("ef:"):
        CFG["effort"] = data[3:]
        save_config()
        await cb.answer(f"Effort: {data[3:]}")
        return await show(chat, "model", mid)
    if data.startswith("rshow:"):
        rid = data[6:]
        req = load_json(os.path.join(ROOT_DIR, rid + ".json"), None)
        if not req:
            return await cb.answer("Запрос уже неактуален")
        await cb.answer()
        from aiogram.types import BufferedInputFile
        return await bot.send_document(chat, BufferedInputFile(req["cmd"].encode(), f"root-{rid[:8]}.sh"),
                                       caption="Команда целиком", reply_to_message_id=mid)
    if data.startswith("mq:"):
        _, n, a = data.split(":")
        q = STATE.get("mq", {}).get(str(mid))
        thread = STATE["msg_threads"].get(str(mid))
        if not q or not thread:
            return await cb.answer("Этот разговор уже забыт — ответь текстом", show_alert=True)
        q["a"][n] = a
        save_state()
        left = [x for x in q["q"] if x not in q["a"]]
        item = SENT.get(str(mid))
        if left:
            await cb.answer(f"Пункт {n}: {'да' if a == 'y' else 'нет'}. Осталось: {', '.join(left)}")
            if item:
                await edit(chat, mid, item["t"], mq_kb(q["q"], q["a"], [(btn("Меню", "menu", "home"),)]),
                           rich=bool(getattr(cb.message, "rich_message", None)) or None)
            return
        await cb.answer("Ок, передал ответы 👌")
        await drop_buttons(cb)
        answer = "; ".join(f"по пункту {x} — {'да, делай' if q['a'][x] == 'y' else 'нет, не надо'}" for x in q["q"])
        STATE["mq"].pop(str(mid), None)
        save_state()
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        return asyncio.create_task(run_task(user_lane, f"[ответ хозяина кнопками в Telegram, {now}]\n{answer}",
                                            thread_id=thread, reply_to=mid, set_main=thread == STATE["main_thread"]))
    if data.startswith("th:") and data[3:] in THREAD_REPLIES:
        thread = STATE["msg_threads"].get(str(mid))
        if not thread:
            return await cb.answer("Этот разговор уже забыт — напиши текстом", show_alert=True)
        await cb.answer("Ок 👌")
        await drop_buttons(cb)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = f"[ответ хозяина кнопкой в Telegram, {now}]\n{THREAD_REPLIES[data[3:]]}"
        return asyncio.create_task(run_task(user_lane, prompt, thread_id=thread, reply_to=mid,
                                            set_main=thread == STATE["main_thread"]))
    if data == "mute":
        keys = STATE.get("incident_msgs", {}).get(str(mid)) or []
        muted = STATE.setdefault("muted", [])
        muted += [k for k in keys if k not in muted]
        save_state()
        await cb.answer("Больше не буду про это писать (вернуть: Меню → Ещё)", show_alert=True)
        return await drop_buttons(cb, keep_menu=True)
    if data.startswith("um:"):
        muted = STATE.get("muted", [])
        i = int(data[3:])
        if i < len(muted):
            muted.pop(i)
            save_state()
        await cb.answer("Снова слежу")
        return await show(chat, "muted", mid)
    if data.startswith("jc:"):
        j = next((j for j in jobs_list() if j["id"] == data[3:]), None)
        if not j:
            await cb.answer("Уже нет такой")
            return await show(chat, "jobs", mid)
        await cb.answer()
        when = dt.datetime.fromtimestamp(j["at"]).strftime("%d.%m %H:%M")
        if await edit(chat, mid, f"{E('warn')} **Отменить задачу?**\n\n**{when}**"
                      + (f" · {E('repeat')} каждые {win_name(j['every'] // 60)}" if j.get("every") else "")
                      + f"\n{j['prompt'][:500]}",
                      kb((btn("Да, отменить", f"jcy:{j['id']}", "cross", "danger"), btn("Нет", "jobs", "back")))):
            return
        return await show(chat, "jobs", mid)
    if data.startswith("jcy:"):
        await cb.answer("Отменил" if cancel_job(data[4:]) else "Уже нет такой")
        return await show(chat, "jobs", mid)
    if data.startswith("q:") and data[2:] in QUICK:
        title, prompt = QUICK[data[2:]]
        await cb.answer("Взялся 👌")
        if is_view_msg(mid):
            await show(chat, "menu", mid)
        else:
            await drop_buttons(cb)
        return asyncio.create_task(owner_ask(prompt, title))
    if data == "login":
        await cb.answer()
        return asyncio.create_task(do_login(chat))
    if data.startswith(("rok:", "rno:")):
        rid = data[4:]
        r = STATE.get("root_reqs", {}).pop(rid, None)
        save_state()
        if not r:
            return await cb.answer("Запрос уже обработан", show_alert=True)
        await drop_buttons(cb)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        if data.startswith("rno:"):
            await cb.answer("Отказал")
            await send_tmp(chat, f"{E('cross')} отказал", 600, reply_to=mid, silent=True)
            trash(chat, mid, 600)
            prompt = f"[{now}] Хозяин ОТКАЗАЛ в root-команде {rid}. Не выполняй её; предложи другой путь или спроси, что он хочет."
        else:
            await cb.answer("Выполняю…")
            code, out = await run_root(rid)
            short = (out or "").strip()[-1500:] or "(пусто)"
            await send_tmp(chat, f"{E('ok' if code == 0 else 'err')} выполнено, код {code}\n```\n{short}\n```",
                           120, reply_to=mid, silent=True, markup=with_close(None))
            trash(chat, mid, 600)
            prompt = (f"[{now}] Хозяин одобрил root-команду {rid}, она выполнена. Код возврата: {code}.\n"
                      f"Вывод:\n```\n{(out or '').strip()[-6000:]}\n```\nПроверь результат и продолжай задачу.")
        return asyncio.create_task(run_task(user_lane, prompt, thread_id=r["thread"],
                                            set_main=r["thread"] == STATE["main_thread"]))
    if data == "nodeadd":
        await cb.answer()
        PENDING[chat] = {"kind": "node", "until": time.time() + 600, "mid": mid}
        text = (f"{E('plus')} **Новая нода**\n\nПришли одним сообщением:\n"
                "`root@192.0.2.1 пароль`  (порт: `root@192.0.2.1:2222 пароль`)\n"
                "или строками: адрес, логин, пароль и (необязательно) имя.\n\n"
                "*Сообщение с паролем я сразу удалю из чата, зайду, поставлю свой ключ и пароль забуду — "
                "он нигде не сохраняется.*\n\n"
                "**Вход по паролю на ноде запрещён?** Выполни на ней:\n"
                f"```bash\nmkdir -p ~/.ssh && echo '{node_pubkey()}' >> ~/.ssh/authorized_keys\n```\n"
                "и пришли просто `root@192.0.2.1` — без пароля.")
        if not await edit(chat, mid, text, kb((btn("Отмена", "nodecancel", "cross"),))):
            PENDING[chat]["mid"] = await post(chat, text, kb((btn("Отмена", "nodecancel", "cross"),)))
        return
    if data == "nodecancel":
        PENDING.pop(chat, None)
        await cb.answer("Отменил")
        return await show(chat, "nodes", mid)
    if data.startswith("nd:"):
        await cb.answer()
        PENDING.pop(chat, None)
        text, markup = await view_node(data[3:])
        if not (is_view_msg(mid) and await edit(chat, mid, text, markup)):
            await post(chat, text, markup)
        return
    if data.startswith("ndel:"):
        await cb.answer()
        name = data[5:]
        return await edit(chat, mid, f"{E('warn')} Удалить ноду **{name}**? Уберу свой ключ с неё (если достучусь) "
                          "и забуду её. Сам сервер не трогаю.",
                          kb((btn("Да, удалить", f"ndelok:{name}", "cross", "danger"), btn("Нет", f"nd:{name}", "back"))))
    if data.startswith("ndelok:"):
        await cb.answer("Удаляю…")
        await remove_node(data[7:])
        return await show(chat, "nodes", mid)
    if data.startswith("nren:"):
        await cb.answer()
        name = data[5:]
        PENDING[chat] = {"kind": "rename", "node": name, "until": time.time() + 600, "mid": mid}
        text = (f"{E('new')} **Название для ноды** `{name}`\n\nНапиши, как её называть — что угодно: "
                "«Германия VPN», «прод база»… Я и в разговорах буду её так понимать.")
        if not await edit(chat, mid, text, kb((btn("Отмена", f"nd:{name}", "cross"),))):
            PENDING[chat]["mid"] = await post(chat, text, kb((btn("Отмена", f"nd:{name}", "cross"),)))
        return
    if data.startswith("mod:"):
        return await REG.on_button(cb, data)
    if data.startswith("gr:"):
        await cb.answer()
        _, i, p = data.split(":")
        rich = bool(getattr(cb.message, "rich_message", None))
        async with waiting(chat, "рисую графики"):
            return await send_graph(chat, int(i), p, mid if cb.message.photo or rich else None)
    if data == "graph":
        await cb.answer()
        async with waiting(chat, "рисую графики"):
            return await send_graph(chat, edit_mid=mid if is_view_msg(mid) else None)
    if data.startswith("ap:"):
        tg_ = guard.targets()
        targets = list(ops().get("apt", {})) if data == "ap:all" else [tg_[int(data[3:])]] if int(data[3:]) < len(tg_) else []
        await cb.answer("Взялся 👌")
        await drop_buttons(cb, keep_menu=True)
        return asyncio.create_task(owner_ask(apt_prompt(targets), E("refresh") + " обновляю пакеты"))
    if data.startswith(("dn:", "du:")):
        tg_ = guard.targets()
        i = int(data[3:])
        if i >= len(tg_):
            return await cb.answer("Нода изменилась")
        await cb.answer("Взялся 👌")
        what = "news" if data.startswith("dn:") else "update"
        return asyncio.create_task(owner_ask(docker_prompt(tg_[i], what),
                                             E("info" if what == "news" else "refresh")
                                             + (" смотрю, что нового" if what == "news" else " обновляю образы")))
    if data == "digest":
        await cb.answer()
        return await show(chat, "digest", mid)
    if data == "nmode":
        CFG["notify_mode"] = "loud" if quiet() else "quiet"
        save_config()
        await cb.answer("Тихо: неважное — в сводку раз в день" if quiet() else "Всё сразу в чат")
        return await show(chat, "more", mid)
    if data == "updcheck":
        await cb.answer("Проверяю обновления…")
        await apt_notify(await apt_scan(), force=True)
        await docker_notify(await docker_scan(), force=True)
        return
    if data.startswith("bk:"):
        what = data[3:]
        if what == "now":
            await host_sh("local", "systemctl --user start --no-block nook-backup.service nook-backup-nodes.service")
            await cb.answer("Запустил бэкап сервера и нод — напишу, если что-то пойдёт не так", show_alert=True)
            return await show(chat, "backups", mid)
        if what == "test":
            # по очереди: restic check держит эксклюзивную блокировку репозитория
            await host_sh("local", "(systemctl --user start nook-backup-check.service; "
                                   "systemctl --user start nook-backup-nodes-check.service) >/dev/null 2>&1 &")
            await cb.answer("Запустил проверку — пришлю результат", show_alert=True)
            return
        await cb.answer("Взялся 👌")
        await drop_buttons(cb)
        return asyncio.create_task(owner_ask(
            "Бэкап (~/server-backup: backup.py — сервер, nodes.py — ноды; статусы ~/.local/state/nook-backup/*.json, "
            "логи там же; systemd --user nook-backup*) сломался или давно не проходил. Разберись и почини, "
            "запусти заново, проверь. Снимки не удаляй.", E("disk") + " чиню бэкап"))
    if data.startswith("pe:"):
        i = int(data[3:])
        t = guard.targets()[i] if i < len(guard.targets()) else None
        if not t:
            return await cb.answer()
        await cb.answer()
        PENDING[chat] = {"kind": "pay", "t": t, "until": time.time() + 600, "mid": mid}
        return await edit(chat, mid, f"{E('cal')} **Оплата · {guard_label(t)}**\n\nСейчас: {pay_line(t)}\n\n"
                          "Пришли дату следующей оплаты и сумму, например:\n`25.11 300₽` · `25 числа 5$` · "
                          "`01.12 1200₽ год` · `25.11 900₽ 3 мес`\n\n`нет` — убрать.", kb((btn("Отмена", "pay", "cross"),)))
    if data.startswith(("pp:", "pz:")):
        i = int(data[3:])
        t = guard.targets()[i] if i < len(guard.targets()) else None
        if not t or t not in pay():
            return await cb.answer()
        if data.startswith("pp:"):
            p = pay()[t]
            d = add_period(dt.date.fromisoformat(p["date"]), p["period"])  # оплатил заранее — тоже следующий период
            while d <= dt.date.today():
                d = add_period(d, p["period"])
            p["date"] = d.isoformat()
            save_state()
            await cb.answer(f"Записал. Следующая оплата {d:%d.%m}", show_alert=True)
        else:
            await cb.answer("Ок, напомню завтра")
        return trash(chat, mid, 1)  # напоминание отработало — убрать из чата
    if data == "dpadd":
        await cb.answer()
        return await show(chat, "deploy_add", mid)
    if data.startswith("dpa:"):
        i = int(data[4:])
        if i >= len(DEPLOY_CANDS):
            return await cb.answer()
        await cb.answer("Подключаю…")
        err = await deploy_add(DEPLOY_CANDS[i])
        if err:
            await send(chat, f"{E('err')} {DEPLOY_CANDS[i]['name']}: {err}", silent=True)
        return await show(chat, "deploy", mid)
    if data.startswith("dpd:"):
        i = int(data[4:])
        if i < len(deploys()):
            deploys().pop(i)
            save_state()
        await cb.answer("Убрал")
        return await show(chat, "deploy", mid)
    if data == "guadd":
        await cb.answer()
        PENDING[chat] = {"kind": "guest", "until": time.time() + 600, "mid": mid}
        return await edit(chat, mid, f"{E('plus')} **Новый гость**\n\nПерешли мне любое сообщение этого человека, "
                          "пришли его контакт или числовой Telegram ID.", kb((btn("Отмена", "guests", "cross"),)))
    if data.startswith("gu:"):
        uid = int(data[3:])
        STATE["guests"] = [g for g in guests() if g["id"] != uid]
        save_state()
        await cb.answer("Убрал")
        return await show(chat, "guests", mid)
    if data == "fav":
        t = STATE.get("msg_tasks", {}).get(str(mid))
        if not t:
            return await cb.answer("Не помню, что это была за задача", show_alert=True)
        if any(f["p"] == t["p"] for f in favs()):
            return await cb.answer("Уже в избранном ⭐")
        favs().append({"t": fav_title(t["p"]), "p": t["p"]})
        asyncio.create_task(fav_autoname(t["p"]))
        save_state()
        return await cb.answer("⭐ Добавил в избранное (Меню → Избранное)")
    if data.startswith("fv:"):
        i = int(data[3:])
        if i >= len(favs()):
            return await cb.answer("Такой больше нет")
        await cb.answer("Взялся 👌")
        f = favs()[i]
        return asyncio.create_task(owner_ask(f["p"], E("spark") + " " + f["t"], task=f["p"]))
    if data.startswith("fd:"):
        i = int(data[3:])
        if i < len(favs()):
            favs().pop(i)
            save_state()
        await cb.answer("Удалил")
        return await show(chat, "favedit" if favs() else "favs", mid)
    if data.startswith("fr:"):
        await cb.answer()
        PENDING[chat] = {"kind": "favren", "i": int(data[3:]), "until": time.time() + 600, "mid": mid}
        return await edit(chat, mid, f"{E('new')} Пришли новое название одним сообщением.",
                          kb((btn("Отмена", "favedit", "cross"),)))
    if data.startswith("jr:"):
        _, flt, page = data.split(":")
        await cb.answer()
        text, markup = view_journal_page(flt, int(page))
        if not await edit(chat, mid, text, markup):
            await post(chat, text, markup)
        return
    if data.startswith("mt:"):
        name = data[3:]
        conf = modsys.load_conf()
        dis = set(conf.get("disabled", []))
        (dis.discard if name in dis else dis.add)(name)
        conf["disabled"] = sorted(dis)
        modsys.save_conf(conf)
        await mods_reload()
        await cb.answer(f"Мод {name} " + ("выключен" if name in dis else "включён"))
        return await show(chat, "mods", mid)
    if data == "mreload":
        r = await mods_reload()
        await cb.answer(f"Загружено: {len(r['loaded'])}" + (f", с ошибкой: {len(r['failed'])}" if r["failed"] else ""))
        return await show(chat, "mods", mid)
    if data == "mrestart":
        await cb.answer("Проверяю ядро и перезапускаюсь…")
        open(modsys.RESTART_FLAG, "w").write(str(time.time()))
        return
    if data.startswith("mfix:"):
        name = data[5:]
        await cb.answer("Взялся 👌")
        await drop_buttons(cb)
        err = MOD_ERRS.get(name, ("", ""))
        if name == "__core__":
            return asyncio.create_task(owner_ask(
                f"После твоей правки bot.py не импортируется:\n```\n{err[1]}\n```\nПочини bot.py (или верни "
                "`spool/mods/bot.py.good`), проверь `python3 -m py_compile bot.py`, затем `mods restart`.",
                E("spark") + " чиню ядро"))
        return asyncio.create_task(owner_ask(
            f"Мод `{name}` (mods/{name}.py) упал в «{err[0]}»:\n```\n{err[1]}\n```\nПочини по навыку mods: "
            f"поправь файл, `mods check {name}`, `mods reload`. Коротко отчитайся.", E("spark") + f" чиню мод {name}"))
    if data == "gtoggle":
        STATE["guard"] = not STATE.get("guard", True)
        save_state()
        await cb.answer("🛡 Охрана включена" if STATE["guard"] else "Охрана выключена")
        return await show(chat, "guard", mid)
    if data == "gscan":
        await cb.answer("Проверяю ноды…")
        n = await guard_tick(force=True)
        if not n:
            await send_tmp(chat, f"{E('shield')} Охрана: всё как в эталоне.", 60, silent=True)
        return await show(chat, "guard", mid)
    if data.startswith("ga:"):
        t = data[3:]
        ok = await asyncio.to_thread(guard.accept, t)
        STATE.setdefault("guard_findings", {}).pop(t, None)  # guard_alerted не трогаем: о принятом больше не пишем
        save_state()
        await cb.answer("Принял как норму 👌" if ok else "Нечего принимать")
        if STATE.get("kind_msgs", {}).get(f"guard:{t}") == mid:
            return trash(chat, mid, 1)  # тревога снята — убрать её из чата
        return await drop_buttons(cb)
    if data.startswith("gq:"):
        t = data[3:]
        await cb.answer("Взялся 👌")
        await drop_buttons(cb)
        text = "\n".join(STATE.get("guard_findings", {}).get(t, []))[:3000]
        return asyncio.create_task(owner_ask(
            f"Охрана нашла изменения на {t}:\n{text}\n\nРазберись: взлом или легитимно (`guard scan {t}`, journal.md). "
            "Ничего не меняй без спроса, кроме остановки явного майнера с копией в карантин. Если всё легитимно — "
            f"`guard accept {t}`.", E("shield") + " проверяю"))
    if data.startswith("nq:"):
        await cb.answer("Взялся 👌")
        name = data[3:]
        return asyncio.create_task(owner_ask(
            f"Проверь ноду {name} (`nodes {name} …`): что запущено, диск, память, ошибки. Ничего не меняй без спроса. "
            "Коротко: что хорошо, что плохо, что предлагаешь.", E("node") + f" смотрю {name}"))
    if data.startswith("full:"):
        secs = int(data[5:])
        set_full(secs)
        await cb.answer(f"Полный доступ на {secs // 3600} ч" if secs else "Полный доступ выключен")
        return await show(chat, "root", mid)
    if data.startswith("ask:"):
        _, aid, i = data.split(":")
        a = STATE.get("asks", {}).pop(aid, None)
        if not a:
            return await cb.answer("Этот вопрос уже не актуален", show_alert=True)
        save_state()
        choice = a["options"][int(i)]
        await cb.answer(f"Ответил: {choice}")
        await drop_buttons(cb)
        await send_tmp(chat, f"{E('ok')} {choice}", 600, reply_to=mid, silent=True)
        trash(chat, mid, 600)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = f"[ответ хозяина кнопкой, {now}] На твой вопрос «{a['q']}» он выбрал: {choice}"
        return asyncio.create_task(run_task(user_lane, prompt, thread_id=a["thread"],
                                            set_main=a["thread"] == STATE["main_thread"]))
    await cb.answer()


async def drop_buttons(cb, keep_menu=False):
    """Убрать кнопки с сообщения, на котором уже ответили."""
    markup = kb(((btn("Меню", "menu", "home"),) if keep_menu else ()) + (btn("Закрыть", "close", "cross"),))
    rich = getattr(cb.message, "rich_message", None)
    try:
        if rich:  # кнопки — часть текста: пересобираем блоки без них
            blocks = [b for b in rich.get("blocks", []) if b.get("type") != "buttons"]
            blocks.append({"type": "buttons", "buttons": ([{"text": "Меню", "callback_data": "menu"}] if keep_menu else [])
                           + [{"text": "Закрыть", "callback_data": "close"}]})
            await tg("editMessageText", chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                     rich_message={"blocks": blocks})
        else:
            await cb.message.edit_reply_markup(reply_markup=markup)
    except Exception as e:
        log.warning("drop buttons failed: %s", e)


# ---------------------------------------------------------------- команды (меню — главное, остальное — для привычки)

@dp.message(Command("start2"))
async def cmd_start2(msg: Message):
    """Классическое меню: inline-кнопки под сообщением, простые эмодзи (для клиентов без rich-сообщений)."""
    if is_owner(msg):
        STATE["ui"] = "classic"
        save_state()
        UI.set("classic")
        await show(msg.chat.id, "menu")
        await delete_msgs(msg.chat.id, [msg.message_id])


@dp.message(Command("start", "menu"))
async def cmd_start(msg: Message, command: CommandObject):
    if STATE["owner_id"] is None:
        if (command.args or "").strip() != CFG.get("pair_code"):
            return await msg.answer("Привет. Я живу на одном сервере и слушаюсь только хозяина. "
                                    "Пришли /start <код привязки>.")
        STATE["owner_id"] = msg.from_user.id
        save_state()
        log.info("paired with owner %s", msg.from_user.id)
        await send(msg.chat.id, f"{E('home')} Привязался. Теперь я твой Nook. Дай минутку — осмотрюсь в доме и отпишусь.")
        asyncio.create_task(introduce())
        return
    if not is_owner(msg):
        if msg.from_user and is_guest(msg.from_user.id) and msg.chat.type == "private":
            await guest_home(msg)
        return
    STATE["ui"] = "rich"
    save_state()
    UI.set("rich")
    await show(msg.chat.id, "menu")
    await delete_msgs(msg.chat.id, [msg.message_id])


@dp.message(Command("status", "limits", "jobs"))
async def cmd_view(msg: Message, command: CommandObject):
    if is_owner(msg):
        await show(msg.chat.id, command.command)
        await delete_msgs(msg.chat.id, [msg.message_id])


@dp.message(Command("graph"))
async def cmd_graph(msg: Message):
    if is_owner(msg):
        await delete_msgs(msg.chat.id, [msg.message_id])
        await send_graph(msg.chat.id)
    elif msg.from_user and is_guest(msg.from_user.id):
        await send_graph(msg.chat.id, prefix="gg")


@dp.message(Command("mods"))
async def cmd_mods(msg: Message):
    if is_owner(msg):
        await show(msg.chat.id, "mods")
        await delete_msgs(msg.chat.id, [msg.message_id])


@dp.message(Command("new"))
async def cmd_new(msg: Message):
    if is_owner(msg):
        new_conversation()
        await delete_msgs(msg.chat.id, [msg.message_id])
        await send_tmp(msg.chat.id, f"{E('new')} Начал новый разговор (заметки о сервере сохраняются).", 20)


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    if is_owner(msg):
        await delete_msgs(msg.chat.id, [msg.message_id])
        await send_tmp(msg.chat.id, stop_all(), 20)


@dp.message(Command("watch"))
async def cmd_watch(msg: Message, command: CommandObject):
    if not is_owner(msg):
        return
    arg = (command.args or "").strip().lower()
    STATE["watch"] = (arg in ("on", "вкл", "1")) if arg else not STATE["watch"]
    save_state()
    await delete_msgs(msg.chat.id, [msg.message_id])
    await send_tmp(msg.chat.id, f"{E('eye')} Автонадзор включён" if STATE["watch"]
                   else f"{E('eyeoff')} Автонадзор выключен (только по запросу)", 20)


@dp.message(Command("login"))
async def cmd_login(msg: Message):
    if is_owner(msg):
        await do_login(msg.chat.id)


CODEX_LOGIN_LOCK = asyncio.Lock()


async def do_login(chat):
    if CODEX_LOGIN_LOCK.locked():
        return await send(chat, "Вход в Codex уже открыт — заверши его по предыдущей ссылке.")
    async with CODEX_LOGIN_LOCK:
        # Вход в отдельном каталоге: неудача или отмена не трогает текущий аккаунт.
        auth_home = os.path.expanduser("~/.codex")
        os.makedirs(auth_home, mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".nook-login-", dir=auth_home) as stage:
            env = codex_env()
            env["CODEX_HOME"] = stage
            proc = await asyncio.create_subprocess_exec(
                CODEX, "login", "--device-auth", env=env, start_new_session=True,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            buf, sent = "", False
            try:
                async def reader():
                    nonlocal buf, sent
                    async for line in proc.stdout:
                        buf += re.sub(r"\x1b\[[0-9;]*m", "", line.decode(errors="replace"))
                        if not sent and re.search(r"https?://\S+", buf) and re.search(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b", buf):
                            sent = await send(chat, f"{E('login')} **Смена аккаунта Codex:**\n\n" + buf.strip()[-1500:]
                                              + "\n\nОткрой ссылку и войди в нужный аккаунт. До завершения входа текущий аккаунт сохранён.")
                await asyncio.wait_for(reader(), 15 * 60)
                await proc.wait()
            except asyncio.TimeoutError:
                pass
            finally:
                if proc.returncode is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await proc.wait()
                if sent:
                    trash(chat, sent, 5)
            new_auth = os.path.join(stage, "auth.json")
            if proc.returncode != 0 or not os.path.isfile(new_auth):
                return await send(chat, "Не удалось войти в новый аккаунт Codex. Текущий вход сохранён.")
            try:
                auth = load_json(new_auth, {})
                if not (auth.get("tokens") or auth.get("OPENAI_API_KEY")):
                    raise ValueError("empty credentials")
                target = os.path.join(auth_home, "auth.json")
                if os.path.exists(target):
                    backup = target + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                    shutil.copy2(target, backup)
                    os.chmod(backup, 0o600)
                os.chmod(new_auth, 0o600)
                os.replace(new_auth, target)
            except (OSError, ValueError):
                return await send(chat, "Не удалось сохранить новый вход Codex. Предыдущий аккаунт сохранён.")
            STATE["main_thread"] = None
            STATE["fallback_until"] = 0
            save_state()
            await send_tmp(chat, "✅ Аккаунт Codex подключён. Следующий разговор начнётся заново. Лимиты можно посмотреть кнопкой «Лимиты».", 120)


# ---------------------------------------------------------------- голосовые

WHISPER = {"model": None, "used": 0.0, "lock": asyncio.Lock()}
WHISPER_IDLE = 10 * 60  # выгрузить модель из памяти после 10 минут без голосовых


def _transcribe(path):
    from faster_whisper import WhisperModel  # тяжёлый импорт — только когда пришёл голос
    if WHISPER["model"] is None:
        WHISPER["model"] = WhisperModel(CFG.get("whisper_model", "small"), device="cpu",
                                        compute_type="int8", cpu_threads=4)
    lang = None if CFG.get("whisper_autolang") else "ru"
    segs, info = WHISPER["model"].transcribe(path, beam_size=5, vad_filter=True, language=lang)
    text = " ".join(s.text.strip() for s in segs).strip()
    if not text:  # тихое/короткое голосовое VAD иногда целиком считает тишиной — пробуем без него
        segs, info = WHISPER["model"].transcribe(path, beam_size=5, vad_filter=False, language=lang)
        text = " ".join(s.text.strip() for s in segs).strip()
    return text


async def transcribe(path):
    async with WHISPER["lock"]:
        try:
            return await asyncio.to_thread(_transcribe, path)
        finally:
            WHISPER["used"] = time.time()


async def whisper_unload_loop():
    while True:
        await asyncio.sleep(60)
        if WHISPER["model"] is not None and time.time() - WHISPER["used"] > WHISPER_IDLE and not WHISPER["lock"].locked():
            WHISPER["model"] = None
            import gc
            gc.collect()
            log.info("whisper unloaded")


async def voice_text(msg: Message):
    """Скачать голосовое/кружок/аудио, распознать и показать текст. None — не вышло."""
    media = msg.voice or msg.video_note or msg.audio
    ext = "mp4" if msg.video_note else "ogg"
    path = os.path.join(INBOX, f"{msg.message_id}-voice.{ext}")
    await bot.download(media, destination=path)
    await bot.send_chat_action(msg.chat.id, "typing")
    try:
        text = await transcribe(path)
    except Exception as e:
        log.exception("transcribe failed")
        await send_tmp(msg.chat.id, f"{E('err')} Не смог распознать голос: {e}", 120, reply_to=msg.message_id)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if not text:
        await send_tmp(msg.chat.id, f"{E('err')} Ничего не расслышал — повтори или напиши текстом.", 60, reply_to=msg.message_id)
        return None
    await send(msg.chat.id, f"🎤 _{text}_", reply_to=msg.message_id, silent=True)
    return text


@dp.message(F.sticker | F.video)
async def on_unsupported(msg: Message):
    if msg.media_group_id and is_owner(msg):  # видео в альбоме с фото — часть общего запроса
        return await on_message(msg)
    if is_owner(msg):
        await msg.answer("Такое я пока не понимаю — напиши текстом, голосом или пришли файл/скрин 🙂")


ALBUMS = {}  # media_group_id -> {"msgs": [...], "at": время последней части}
ALBUM_WAIT = 1.5  # Telegram шлёт альбом отдельными сообщениями подряд — ждём, пока придут все


async def collect_album(msg):
    """Части одного альбома → один запрос. Первая часть ждёт остальные и возвращает их все, прочие — None."""
    grp = ALBUMS.setdefault(msg.media_group_id, {"msgs": [], "at": 0.0})
    grp["msgs"].append(msg)
    grp["at"] = time.monotonic()
    if len(grp["msgs"]) > 1:
        return None
    while time.monotonic() - grp["at"] < ALBUM_WAIT:
        await asyncio.sleep(0.3)
    ALBUMS.pop(msg.media_group_id, None)
    return sorted(grp["msgs"], key=lambda m: m.message_id)


@dp.message(F.text | F.photo | F.document | F.caption | F.voice | F.video_note | F.audio | F.contact)
async def on_message(msg: Message):
    if not is_owner(msg):
        if msg.from_user and is_guest(msg.from_user.id) and msg.chat.type == "private":
            await guest_home(msg)
        return
    album = [msg]
    if msg.media_group_id:
        album = await collect_album(msg)
        if not album:
            return
        msg = next((m for m in album if m.caption), album[0])  # подпись альбома — у одной из частей
    voice = None
    if msg.voice or msg.video_note or msg.audio:
        voice = await voice_text(msg)
        if not voice:
            return
    pend = PENDING.get(msg.chat.id)
    if pend and pend["until"] > time.time() and pend["kind"] in PENDING_HANDLERS and (
            (msg.text and not msg.text.startswith("/")) or pend["kind"] == "guest"):
        PENDING.pop(msg.chat.id, None)
        return await PENDING_HANDLERS[pend["kind"]](msg, pend)
    if msg.reply_to_message and await reply_command(msg, (voice or msg.text or "").strip()):
        return
    pend = PENDING.get(msg.chat.id)
    if pend and pend["until"] > time.time() and pend["kind"] == "rename" and msg.text and not msg.text.startswith("/"):
        PENDING.pop(msg.chat.id, None)
        items = nodes()
        for n in items:
            if n["name"] == pend["node"]:
                n["title"] = msg.text.strip()[:40]
        write_nodes(items)
        await delete_msgs(msg.chat.id, [msg.message_id])
        text, markup = await view_node(pend["node"])
        if not await edit(msg.chat.id, pend["mid"], text, markup):
            await post(msg.chat.id, text, markup)
        return
    if (pend and pend["kind"] == "node" and pend["until"] > time.time() and msg.text and not msg.text.startswith("/")
            and not parse_node(msg.text) and not looks_like_creds(msg.text)):
        # хозяин передумал и пишет обычную задачу — не глотаем её, добавление ноды отменяем
        PENDING.pop(msg.chat.id, None)
        if pend.get("mid"):
            await edit(msg.chat.id, pend["mid"], f"{E('cross')} Добавление ноды отменено.", None)
    elif pend and pend["kind"] == "node" and pend["until"] > time.time() and msg.text and not msg.text.startswith("/"):
        PENDING.pop(msg.chat.id, None)
        try:  # сообщение с паролем — сразу из чата
            await bot.delete_message(msg.chat.id, msg.message_id)
        except Exception:
            pass
        spec = parse_node(msg.text)
        if not spec:
            PENDING[msg.chat.id] = dict(pend, until=time.time() + 600)
            text = f"{E('err')} Не понял формат. Пример: `root@192.0.2.1 пароль` — пришли ещё раз."
            if not await edit(msg.chat.id, pend["mid"], text, kb((btn("Отмена", "nodecancel", "cross"),))):
                await send(msg.chat.id, text, markup=kb((btn("Отмена", "nodecancel", "cross"),)))
            return
        return await add_node(msg.chat.id, spec, pend.get("mid"))
    text = msg.text or msg.caption or ""
    if msg.document and (msg.document.file_size or 0) > 20 * 2**20:
        return await send(msg.chat.id, f"{E('err')} Файл больше 20 МБ — бот Telegram такие скачать не может. "
                          "Залей его куда-нибудь (gdrive, ссылка) и пришли ссылку — скачаю сам.",
                          reply_to=msg.message_id)
    raw = (voice or text).strip()
    if raw.startswith("/"):
        cmd, _, args = raw[1:].partition(" ")
        if await REG.on_command(msg, cmd.split("@")[0].lower(), args.strip()):
            return
    elif raw and await REG.on_text(msg, raw):
        return
    if voice:
        text = (text + "\n" if text else "") + f"[голосовое, распознано автоматически — возможны ошибки в словах]\n{voice}"
    images, files, videos = [], [], 0
    for part in album:
        if part.photo:
            path = os.path.join(INBOX, f"{part.message_id}.jpg")
            await bot.download(part.photo[-1], destination=path)
            images.append(path)
        if part.document:
            if (part.document.file_size or 0) > 20 * 2**20:
                continue
            name = re.sub(r"[^\w.\-]", "_", part.document.file_name or "file")
            path = os.path.join(INBOX, f"{part.message_id}-{name}")
            await bot.download(part.document, destination=path)
            files.append(path)
        if part.video:
            videos += 1
    if files:
        text += "\n\n[хозяин прислал файл(ы): " + ", ".join(files) + "]"
    if videos:
        text += f"\n\n[в альбоме ещё видео: {videos} — их я не смотрю, только фото и файлы]"
    if not text.strip() and images:
        text = "Посмотри на картинку." if len(images) == 1 else f"Посмотри на картинки ({len(images)})."

    reply_to = msg.reply_to_message.message_id if msg.reply_to_message else None
    thread = STATE["msg_threads"].get(str(reply_to)) if reply_to else None
    set_main, ctx = False, ""
    if not thread:
        thread, ctx = main_thread_for_new_message()
        set_main = True
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    prompt = f"[сообщение хозяина из Telegram, {now}]\n{text}"
    if not thread:
        prompt = fresh_prompt(prompt, ctx)
    await bot.send_chat_action(msg.chat.id, "typing")
    await run_task(user_lane, prompt, thread_id=thread, images=images,
                   reply_to=msg.message_id, set_main=set_main, task=text if not images and not files else None)


# ---------------------------------------------------------------- monitoring (без LLM)

def http_json(url, auth=None, timeout=20):
    req = Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + b64encode(auth.encode()).decode())
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


async def sh(*cmd, timeout=30):
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode(errors="replace")
    except Exception:
        return ""


def meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k] = int(v.split()[0]) * 1024
    return info


HOT = 90  # %: CPU/память/диск «под сотку» → агент разбирается сам
_cpu_prev = None


def cpu_percent():
    """Загрузка CPU между двумя вызовами (т.е. средняя за интервал автонадзора), по /proc/stat."""
    global _cpu_prev
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle, total = v[3] + v[4], sum(v[:8])
    now = time.monotonic()
    if _cpu_prev and now - _cpu_prev[2] < 30:  # вызвали сразу после замера (кнопка «Статус») — отдаём прошлый
        return _cpu_prev[3]
    prev = _cpu_prev
    val = 100 * (1 - (idle - prev[0]) / (total - prev[1])) if prev and total != prev[1] else None
    _cpu_prev = (idle, total, now, val)
    return val


async def top_snapshot(by="cpu"):
    """Кто ест ресурсы прямо сейчас — для агента, чтобы видел, что было в момент пика."""
    if by == "cpu":
        out = await sh("top", "-b", "-n", "2", "-d", "1", "-o", "%CPU", "-w", "160")
        out = out[out.rfind("top -"):]  # второй замер точнее первого
        lines = [l for l in out.splitlines()[6:13] if l.strip()]
    else:
        lines = (await sh("ps", "-eo", "pid,user,%cpu,%mem,rss,etime,args", "--sort=-rss", "--cols", "160")).splitlines()[:8]
    return "```\n" + "\n".join(l[:160] for l in lines) + "\n```"


NODE_STATS_SH = (
    "c=$(top -b -n2 -d1 2>/dev/null | grep -m2 -i '^%\\?cpu' | tail -1 | tr ',' '\\n' | grep -i ' id' | grep -o '[0-9.]*' | head -1); "
    "echo cpu=$(awk -v i=\"${c:-100}\" 'BEGIN{printf \"%d\", 100-i}'); "
    "free | awk '/Mem/{printf \"mem=%d\\n\", ($2-$7)*100/$2}'; "
    "df -P / | awk 'NR==2{gsub(\"%\",\"\",$5); print \"disk=\"$5}'; "
    "free | awk '/Swap/{printf \"swap=%d\\n\", ($2>0 ? $3*100/$2 : 0)}'; "
    "touch ~/.nook-hb 2>/dev/null; "  # пульс для сторожа на ноде (watchdog)
    "echo ---top; (top -b -n1 -o %CPU -w 160 2>/dev/null | sed -n '7,13p' | grep . ) || "
    "ps -eo pid,user,%cpu,%mem,rss,etime,args --sort=-%cpu --cols 160 2>/dev/null | head -7; "
    "echo ---mem; ps -eo pid,user,%cpu,%mem,rss,etime,args --sort=-rss --cols 160 2>/dev/null | head -7")


async def node_stats(n):
    """(жива?, {cpu, mem, disk}, снимок процессов) — одной SSH-сессией."""
    try:
        p = await asyncio.create_subprocess_exec("ssh", *ssh_opts(), n["name"], NODE_STATS_SH,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 25)
    except Exception:
        return False, {}, {}
    if p.returncode != 0:
        return False, {}, {}
    text = out.decode(errors="replace")
    head, _, rest = text.partition("---top")
    top, _, mem = rest.partition("---mem")
    vals = {k: int(v) for k, v in re.findall(r"(cpu|mem|disk|swap)=(\d+)", head)}
    return True, vals, {"cpu": top.strip(), "mem": mem.strip()}


METRICS_DB = os.path.join(HOME, "spool", "metrics.db")
METRICS_NOW = {}  # target -> {cpu, mem, disk, swap} из последнего collect()


def metrics_db():
    import sqlite3
    db = sqlite3.connect(METRICS_DB, timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS m (ts INTEGER, host TEXT, cpu INT, mem INT, disk INT, swap INT)")
    db.execute("CREATE INDEX IF NOT EXISTS m_host_ts ON m(host, ts)")
    return db


def metrics_put(now):
    if not now:
        return
    db = metrics_db()
    ts = int(time.time())
    with db:
        db.executemany("INSERT INTO m VALUES (?,?,?,?,?,?)",
                       [(ts, h, v.get("cpu"), v.get("mem"), v.get("disk"), v.get("swap")) for h, v in now.items()])
        if ts % 3600 < WATCH_INTERVAL:  # раз в час — подрезать историю старше 40 дней
            db.execute("DELETE FROM m WHERE ts < ?", (ts - 40 * 86400,))
    db.close()


def metrics_get(host, since):
    db = metrics_db()
    rows = db.execute("SELECT ts, cpu, mem, disk, swap FROM m WHERE host=? AND ts>=? ORDER BY ts", (host, since)).fetchall()
    db.close()
    return rows


PERIODS = {"h": ("6 часов", 6 * 3600), "d": ("сутки", 86400), "w": ("неделя", 7 * 86400), "m": ("месяц", 30 * 86400)}
METRICS = (("cpu", "CPU"), ("mem", "Память"), ("disk", "Диск"), ("swap", "Swap"))
# тёмная тема под Telegram; одна серия на панель → один цвет, статус — только у «горячих» значений
GC = {"bg": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#8a897f", "grid": "#2e2e2c",
      "line": "#3987e5", "hot": "#d03b3b", "warm": "#fab219"}


def metrics_series(host, period):
    """[(datetime, {cpu, mem, disk, swap})] за период: усреднено по корзинам, с None на месте провалов (бот не работал)."""
    span = PERIODS[period][1]
    now = int(time.time())
    rows = metrics_get(host, now - span)
    if len(rows) < 2:
        return []
    bucket = max(WATCH_INTERVAL, span // 360)  # ~360 точек на линию: гладко и без шума
    acc = {}
    for r in rows:
        b = acc.setdefault(r[0] // bucket, [[], [], [], []])
        for k in range(4):
            if r[k + 1] is not None:
                b[k].append(r[k + 1])
    out, prev = [], None
    for key in sorted(acc):
        if prev is not None and key - prev > max(3, 3 * WATCH_INTERVAL // bucket):
            out.append((dt.datetime.fromtimestamp((prev + 1) * bucket), None))  # разрыв линии
        vals = {m: (sum(v) / len(v) if v else None) for (m, _), v in zip(METRICS, acc[key])}
        out.append((dt.datetime.fromtimestamp(key * bucket + bucket // 2), vals))
        prev = key
    return out


def series_stats(series, m):
    vals = [v[m] for _, v in series if v and v.get(m) is not None]
    if not vals:
        return None
    return {"now": vals[-1], "avg": sum(vals) / len(vals), "max": max(vals)}


def period_label(period, series_list):
    """«сутки», а если данных меньше — «сутки · данные с 11:20»."""
    starts = [s[0][0] for s in series_list if s]
    if not starts:
        return PERIODS[period][0]
    first = min(starts)
    if (dt.datetime.now() - first).total_seconds() < PERIODS[period][1] * 0.9:
        return f"{PERIODS[period][0]} · данные с {first:%H:%M}" + ("" if first.date() == dt.date.today() else f" {first:%d.%m}")
    return PERIODS[period][0]


def _mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.edgecolor": GC["grid"], "axes.linewidth": 0.8})
    return plt


def _area(ax, series, m, color, lw=2.0):
    import math
    xs = [t for t, _ in series]
    ys = [v[m] if v and v.get(m) is not None else math.nan for _, v in series]
    ax.fill_between(xs, ys, 0, color=color, alpha=0.18, lw=0)
    ax.plot(xs, ys, color=color, lw=lw, solid_capstyle="round")
    ax.set_ylim(0, 100)
    ax.set_xlim(xs[0], xs[-1])


def _png(fig):
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=GC["bg"])
    return buf.getvalue()


def heat(v):
    return GC["hot"] if v >= HOT else GC["warm"] if v >= 75 else GC["line"]


def render_graph(host, period):
    """PNG подробно по одному хосту: 4 панели одна под другой (CPU / память / диск / swap). None — нет данных."""
    import matplotlib.dates as mdates
    series = metrics_series(host, period)
    if not series:
        return None
    plt = _mpl()
    fig, axes = plt.subplots(4, 1, figsize=(8, 9), dpi=150, sharex=True)
    fig.patch.set_facecolor(GC["bg"])
    fig.text(0.035, 0.975, guard_label(host), color=GC["ink"], fontsize=17, fontweight="bold", va="top")
    fig.text(0.965, 0.975, period_label(period, [series]), color=GC["ink2"], fontsize=13, va="top", ha="right")
    for ax, (m, name) in zip(axes, METRICS):
        ax.set_facecolor(GC["bg"])
        st = series_stats(series, m)
        color = heat(st["now"]) if st else GC["line"]
        _area(ax, series, m, color)
        ax.axhline(HOT, color=GC["hot"], lw=0.8, ls=(0, (4, 4)), alpha=0.6)
        ax.set_yticks([0, 50, 100])
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
        ax.grid(axis="y", color=GC["grid"], lw=0.6)
        for sp in ("top", "right", "left"):
            ax.spines[sp].set_visible(False)
        ax.tick_params(colors=GC["muted"], labelsize=9, length=0)
        ax.text(0, 1.06, name, transform=ax.transAxes, color=GC["ink2"], fontsize=12, va="bottom")
        if st:
            ax.text(0.13, 1.06, f"{st['now']:.0f}%", transform=ax.transAxes, color=GC["ink"], fontsize=15,
                    fontweight="bold", va="bottom")
            ax.text(1, 1.06, f"сред {st['avg']:.0f}% · макс {st['max']:.0f}%", transform=ax.transAxes,
                    color=GC["muted"], fontsize=10, va="bottom", ha="right")
    fmt = "%H:%M" if period in ("h", "d") else "%d.%m"
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    axes[-1].xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=7))
    fig.subplots_adjust(left=0.1, right=0.965, top=0.9, bottom=0.05, hspace=0.55)
    png = _png(fig)
    plt.close(fig)
    return png


def render_overview(period):
    """PNG: все хосты сразу — строка на сервер, в ней 4 мини-графика с крупной текущей цифрой."""
    hosts = [(h, metrics_series(h, period)) for h in guard.targets()]
    hosts = [(h, s) for h, s in hosts if s]
    if not hosts:
        return None
    plt = _mpl()
    n = len(hosts)
    H = 1.0 + 2.1 * n  # высота в дюймах: шапка + строка на сервер
    fig, axes = plt.subplots(n, 4, figsize=(10, H), dpi=150, squeeze=False)
    fig.patch.set_facecolor(GC["bg"])
    fig.text(0.03, 1 - 0.35 / H, "Серверы", color=GC["ink"], fontsize=17, fontweight="bold", va="center")
    fig.text(0.97, 1 - 0.35 / H, period_label(period, [s for _, s in hosts]), color=GC["ink2"], fontsize=13,
             va="center", ha="right")
    for r, (h, series) in enumerate(hosts):
        for c, (m, name) in enumerate(METRICS):
            ax = axes[r][c]
            ax.set_facecolor(GC["bg"])
            st = series_stats(series, m)
            color = heat(st["now"]) if st else GC["line"]
            if st:
                _area(ax, series, m, color, lw=1.6)
            ax.set_xticks([])
            ax.set_yticks([])
            for sp in ax.spines.values():
                sp.set_visible(False)
            ax.axhline(0, color=GC["grid"], lw=0.8)
            ax.text(0, 1.04, name, transform=ax.transAxes, color=GC["muted"], fontsize=10, va="bottom")
            ax.text(1, 1.04, f"{st['now']:.0f}%" if st else "—", transform=ax.transAxes, color=GC["ink"],
                    fontsize=15, fontweight="bold", va="bottom", ha="right")
        axes[r][0].text(0, 1.42, guard_label(h), transform=axes[r][0].transAxes, color=GC["ink"], fontsize=13,
                        fontweight="bold", va="bottom")
    fig.subplots_adjust(left=0.03, right=0.97, top=1 - 1.25 / H, bottom=0.15 / H, wspace=0.18, hspace=1.0)
    png = _png(fig)
    plt.close(fig)
    return png


def graph_caption(host_i, period):
    """Подпись с цифрами — чтобы главное читалось и без картинки."""
    hosts = guard.targets()
    pick = hosts if host_i < 0 else [hosts[host_i]]
    lines = [f"📈 **{'Все серверы' if host_i < 0 else guard_label(hosts[host_i])}** · "
             + period_label(period, [metrics_series(h, period) for h in pick])]
    for h in pick:
        series = metrics_series(h, period)
        parts = []
        for m, name in METRICS:
            st = series_stats(series, m)
            if st:
                parts.append(f"{name} {st['now']:.0f}%" + (" ❗" if st["now"] >= HOT else ""))
        lines.append((f"{guard_label(h)}: " if host_i < 0 else "") + (" · ".join(parts) or "данных пока нет"))
    return "\n".join(lines)


def render_disk_all(days=7):
    """PNG: диск всех хостов за неделю — для недельного отчёта."""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 3.6), dpi=110)
    fig.patch.set_facecolor("#16181d")
    ax.set_facecolor("#16181d")
    colors = ["#5b9cf6", "#f2a33a", "#49c27a", "#c46ae0", "#e05a5a", "#3fc1c9"]
    drawn = 0
    for i, host in enumerate(guard.targets()):
        rows = metrics_get(host, int(time.time()) - days * 86400)
        if len(rows) < 2:
            continue
        rows = rows[::max(1, len(rows) // 800)]
        ax.plot([dt.datetime.fromtimestamp(r[0]) for r in rows], [r[3] for r in rows], lw=1.5,
                color=colors[i % len(colors)], label=f"{guard_label(host)} {rows[-1][3]}%")
        drawn += 1
    if not drawn:
        plt.close(fig)
        return None
    ax.set_ylim(0, 100)
    ax.grid(color="#2c2f36", lw=0.6)
    for sp in ax.spines.values():
        sp.set_color("#2c2f36")
    ax.tick_params(colors="#a8adb7", labelsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.set_title("Диск за неделю", color="#e6e8eb", fontsize=11, loc="left")
    leg = ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=3)
    for t in leg.get_texts():
        t.set_color("#e6e8eb")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def graph_kb(host_i, period, prefix="gr"):
    hosts = guard.targets()
    sel = lambda i, p, label, on: btn(("✓ " if on else "") + label, f"{prefix}:{i}:{p}", None, "primary" if on else None)
    items = [sel(-1, period, "Все", host_i < 0)] + [sel(i, period, guard_label(h)[:16], i == host_i)
                                                     for i, h in enumerate(hosts)]
    hrows = [tuple(items[j:j + 3]) for j in range(0, len(items), 3)]
    prow = tuple(sel(host_i, p, name, p == period) for p, (name, _) in PERIODS.items())
    return kb(*hrows, prow, (btn("Меню", "menu", "home"),) if prefix == "gr" else None)


async def tg_upload(method, files, **params):
    """Bot API с загрузкой файлов (multipart): files = {имя: bytes}, в params на них — attach://имя."""
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    form = aiohttp.FormData()
    for k, v in params.items():
        if v is not None:
            form.add_field(k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    for k, data in files.items():
        form.add_field(k, data, filename=f"{k}.png", content_type="image/png")
    async with _http.post(f"https://api.telegram.org/bot{CFG['bot_token']}/{method}", data=form,
                          timeout=aiohttp.ClientTimeout(total=90)) as r:
        d = await r.json()
    if not d.get("ok"):
        raise RuntimeError(d.get("description", "telegram error"))
    return d["result"]


def graph_slides(period):
    """Картинки для листалки: сначала все серверы, потом каждый подробно."""
    out = [("all", render_overview(period))]
    out += [(f"h{i}", render_graph(h, period)) for i, h in enumerate(guard.targets())]
    return [(k, png) for k, png in out if png]


async def send_graph_rich(chat, period="d", edit_mid=None):
    """Экран графиков rich-сообщением: фото листаются (tg-slideshow), кнопки — в теле, как в остальном меню."""
    slides = await asyncio.to_thread(graph_slides, period)
    cap = await asyncio.to_thread(graph_caption, -1, period)
    title, _, rest = cap.partition("\n")
    title = title.replace("📈 ", "").replace("**Все серверы**", f"{E('load')} **Графики**")
    prow = tuple(btn(name, f"gr:-1:{p}", None, "primary" if p == period else None) for p, (name, _) in PERIODS.items())
    markup = kb(prow, (btn("Обновить", f"gr:-1:{period}", "refresh"), btn("Меню", "menu", "home")))
    if slides:
        gallery = ("\n\n<tg-slideshow>\n\n" + "\n".join(f"![](tg://photo?id={k})" for k, _ in slides)
                   + "\n\n</tg-slideshow>\n\n")
        hint = f"\n\n*Листай фото → подробно по каждому серверу.*" if len(slides) > 1 else ""
    else:
        gallery, hint = "\n\n", f"\n\n*Данных пока нет — снимаю каждые {win_name(WATCH_INTERVAL // 60)}.*"
    body = to_rich_md(title) + gallery + to_rich_md(rest + hint) + rich_buttons(markup)
    rm = {"markdown": body, "skip_entity_detection": True,
          "media": [{"id": k, "media": {"type": "photo", "media": f"attach://{k}"}} for k, _ in slides]}
    files = dict(slides)
    if edit_mid:
        try:
            await tg_upload("editMessageText", files, chat_id=str(chat), message_id=str(edit_mid), rich_message=rm)
            return edit_mid
        except Exception as e:
            log.info("graph rich edit failed: %s", e)
    m = await tg_upload("sendRichMessage", files, chat_id=str(chat), rich_message=rm)
    mid = m["message_id"]
    old = [x for x in STATE.get("view_msgs", []) if x != mid]  # один экран меню в чате, как у show()
    STATE["view_msgs"] = [mid]
    save_state()
    await delete_msgs(chat, old)
    return mid


async def send_graph(chat, host_i=-1, period="d", edit_mid=None, prefix="gr"):
    from aiogram.types import BufferedInputFile, InputMediaPhoto
    if prefix == "gr" and ui_mode() == "rich":
        try:
            return await send_graph_rich(chat, period if period in PERIODS else "d", edit_mid)
        except Exception as e:
            log.warning("rich graphs failed, fallback to classic: %s", e)
            edit_mid = None
    hosts = guard.targets()
    host_i = host_i if -1 <= host_i < len(hosts) else -1
    period = period if period in PERIODS else "d"
    png = await asyncio.to_thread(render_overview if host_i < 0 else render_graph,
                                  period if host_i < 0 else hosts[host_i], *(() if host_i < 0 else (period,)))
    markup = graph_kb(host_i, period, prefix)
    cap = md_to_html(await asyncio.to_thread(graph_caption, host_i, period))
    if not png:
        text = cap + f"\n\nДанных за этот период ещё нет — снимаю каждые {win_name(WATCH_INTERVAL // 60)}."
        if edit_mid:
            try:
                return await bot.edit_message_caption(chat_id=chat, message_id=edit_mid, caption=text,
                                                      parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
        return await bot.send_message(chat, text, parse_mode="HTML", reply_markup=markup)
    photo = BufferedInputFile(png, "graph.png")
    if edit_mid:
        try:
            return await bot.edit_message_media(InputMediaPhoto(media=photo, caption=cap, parse_mode="HTML"),
                                                chat_id=chat, message_id=edit_mid, reply_markup=markup)
        except Exception as e:
            if "not modified" in str(e):
                return
            log.info("graph edit failed: %s", e)
    await bot.send_photo(chat, photo, caption=cap, parse_mode="HTML", reply_markup=markup)



async def collect():
    """Снять состояние: (entities{key: (ok, описание)}, thresholds{key: описание})."""
    ent, alerts = {}, {}

    du = shutil.disk_usage("/")
    pct = du.used / du.total * 100
    if pct >= DISK_WARN or du.free < 5 * 2**30:
        alerts["disk:/"] = f"диск / заполнен на {pct:.0f}% (свободно {du.free / 2**30:.1f} ГБ)"
    mi = meminfo()
    swap_used = mi["SwapTotal"] - mi["SwapFree"]
    mem_pct = 100 - mi["MemAvailable"] * 100 / mi["MemTotal"]
    if mem_pct >= HOT or (mi["MemAvailable"] < 250 * 2**20 and mi["SwapTotal"] and swap_used / mi["SwapTotal"] > 0.85):
        alerts["mem"] = (f"память занята на {mem_pct:.0f}% (доступно {mi['MemAvailable'] // 2**20} МБ, swap "
                         f"{swap_used * 100 // max(mi['SwapTotal'], 1)}%). Снимок в этот момент:\n" + await top_snapshot("rss"))
    cpu = cpu_percent()
    METRICS_NOW["local"] = {"cpu": None if cpu is None else int(cpu), "mem": int(mem_pct), "disk": int(pct),
                            "swap": int(swap_used * 100 / mi["SwapTotal"]) if mi["SwapTotal"] else 0}
    if cpu is not None and cpu >= HOT:
        alerts["cpu"] = (f"процессор загружен на {cpu:.0f}% (среднее за {WATCH_INTERVAL // 60} мин). "
                         "Снимок в этот момент:\n" + await top_snapshot("cpu"))

    try:
        pw = open(STATUS_BOARD_PW).read().strip()
        data = await asyncio.to_thread(http_json, STATUS_BOARD, f"{CFG.get('status_board_user', os.environ.get('USER', ''))}:{pw}")
        for p in data["projects"]:
            if p.get("state") == "unknown" or p.get("expected") is False:
                continue  # разовые/плановые задачи (бэкапы и т.п.) между запусками «offline» — это норма
            bad = [c["label"] for c in p.get("checks", []) if not c["ok"] and not c.get("optional")]
            ent[f"proj:{p['id']}"] = (p["state"] == "online",
                                      f"проект {p['name']} ({p['state']}; не отвечает: {', '.join(bad)})")
    except Exception as e:
        if CFG.get("status_board_enabled", False):
            alerts["status-board"] = f"status-board не отвечает ({type(e).__name__})"

    raw = await sh(PM2, "jlist")
    try:
        for p in json.loads(raw[raw.find("["):]):
            env = p.get("pm2_env", {})
            name = p.get("name")
            if name == "nook":
                continue
            ent[f"pm2:{name}"] = (env.get("status") == "online",
                                  f"pm2 {name}: {env.get('status')}, рестартов {env.get('restart_time')}")
            ent[f"pm2r:{name}"] = (None, env.get("restart_time", 0))
    except Exception:
        pass

    raw = await sh("docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}")
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            name, state, status = parts
            ent[f"docker:{name}"] = (state == "running" and "unhealthy" not in status,
                                     f"docker {name}: {status}")

    for unit_scope in (("systemctl",), ("systemctl", "--user")):
        raw = await sh(*unit_scope, "list-units", "--failed", "--no-legend", "--plain")
        failed = {l.split()[0] for l in raw.splitlines() if l.strip()}
        raw = await sh(*unit_scope, "list-units", "--type=service", "--state=active", "--no-legend", "--plain")
        for l in raw.splitlines():
            if l.strip():
                ent[f"unit:{l.split()[0]}"] = (True, "")
        for u in failed:
            ent[f"unit:{u}"] = (False, f"systemd-юнит {u} упал" + (" (--user)" if len(unit_scope) > 1 else ""))
    items = nodes()
    if items:
        stats = await asyncio.gather(*(node_stats(n) for n in items))
        alive = [st[0] for st in stats]
        for n, (ok, vals, snap) in zip(items, stats):
            what = {"cpu": "процессор", "mem": "память", "disk": "диск"}
            METRICS_NOW[n["name"]] = vals
            for k, v in vals.items():
                if k == "swap":
                    continue
                if v >= HOT:
                    shot = snap.get("mem" if k == "mem" else "cpu", "")
                    alerts[f"node-{k}:{n['name']}"] = (
                        f"нода {node_ref(n)}: {what[k]} {v}%"
                        + (f". Снимок в этот момент:\n```\n{shot}\n```" if k != "disk" and shot else ""))
        for n, ok in zip(items, alive):
            ent[f"node:{n['name']}"] = (ok, f"нода {node_ref(n)} ({n['user']}@{n['host']}) не отвечает по SSH")
    return ent, alerts


class Watcher:
    def __init__(self):
        self.prev_restarts = {}
        self.pending = {}      # key -> сколько проверок подряд плохо
        self.ok_ticks = {}     # key -> сколько проверок подряд снова хорошо (для «снова в норме»)

    async def tick(self):
        ent, alerts = await collect()
        try:
            await asyncio.to_thread(metrics_put, dict(METRICS_NOW))
        except Exception:
            log.exception("metrics put failed")
        SNAPSHOT.update(data=(ent, alerts), at=time.time())
        known_good = set(STATE["known_good"])
        problems = dict(alerts)
        recovered = []

        for key, (ok, desc) in ent.items():
            if key.startswith("pm2r:"):
                name = key[5:]
                prev = self.prev_restarts.get(name)
                self.prev_restarts[name] = desc
                if prev is not None and desc - prev >= 3:
                    problems[f"crashloop:{name}"] = f"pm2 {name} перезапускался {desc - prev} раз за {WATCH_INTERVAL // 60} мин (crash loop)"
                continue
            if ok:
                known_good.add(key)
            elif key in known_good:
                # раньше работало — теперь нет
                problems[key] = desc
        # исчезнувшие (контейнер удалён / pm2 delete) не считаем проблемой — забываем
        known_good &= set(ent)

        # подтверждение: проблема должна держаться 2 проверки подряд
        confirmed = {}
        muted = set(STATE.get("muted", []))
        for key, desc in problems.items():
            if key in muted:
                continue
            self.pending[key] = self.pending.get(key, 0) + 1
            if self.pending[key] >= 2:
                confirmed[key] = desc
        for key in list(self.pending):
            if key not in problems or key in muted:
                del self.pending[key]
        # восстановление: о чём сообщали, того нет в проблемах 2 проверки подряд (переживает рестарт бота)
        for key in list(STATE["reported"]):
            if key.split(":")[0] in ("proj", "pm2", "docker", "unit", "node") and key not in ent:
                STATE["reported"].pop(key)  # объект больше не наблюдаем (удалён/плановый) — забыть молча
                continue
            if key in problems and key not in muted:
                self.ok_ticks.pop(key, None)
                continue
            self.ok_ticks[key] = self.ok_ticks.get(key, 0) + 1
            if self.ok_ticks[key] >= 2:
                self.ok_ticks.pop(key)
                recovered.append(key)

        # то, что было known_good и стало плохим, остаётся в known_good, чтобы ловить восстановление
        STATE["known_good"] = sorted(known_good | {k for k in problems if k in ent})
        save_state()
        return confirmed, recovered, ent

    async def handle(self, confirmed, recovered, ent):
        for key in recovered:
            STATE["reported"].pop(key, None)
        if recovered:  # без звука: обычно агент уже отчитался о починке
            names = ", ".join(key_name(k) for k in recovered)
            digest(f"Снова в норме: {names}")
            guests_notify(f"✅ Снова в норме: {names}")
        save_state()

        # по одной поломке — один разбор, пока она не починится (а не каждый час заново)
        now = time.time()
        new = {k: d for k, d in confirmed.items() if k not in STATE["reported"]}
        if not new:
            return
        today = dt.date.today().isoformat()
        if STATE["incidents_today"][0] != today:
            STATE["incidents_today"] = [today, 0]
        for k in new:
            STATE["reported"][k] = now
        st = STATE.setdefault("stats", {}).setdefault("incidents", [])
        st.append(now)
        del st[:-200]
        save_state()
        guests_notify("⚠️ Проблема на сервере:\n" + "\n".join(f"• {d.split('Снимок')[0].strip()}" for d in new.values()))
        if STATE["incidents_today"][1] >= INCIDENT_DAILY_CAP:
            mid = await notify(f"{E('warn')} **Проблемы** (лимит авто-разборов на сегодня исчерпан, сам не лезу):\n" +
                               "\n".join(f"• {d}" for d in new.values()),
                               markup=kb((btn("Разберись", "q:check", "check", "success"),),
                                         (btn("Не следить за этим", "mute", "eyeoff"),)))
            STATE.setdefault("incident_msgs", {})[str(mid)] = list(new)
            save_state()
            return
        STATE["incidents_today"][1] += 1
        save_state()
        lines = "\n".join(f"- {d}" for d in new.values())
        prompt = (
            "[АВТОНАДЗОР] Мой мониторинг заметил новые проблемы (раньше всё работало, теперь нет, "
            "подтверждено двумя проверками подряд):\n" + lines + "\n\n"
            "Разберись: найди причину (логи, статус, registry.json; если есть снимок процессов — это то, что было "
            "в момент проблемы, проверь, что сейчас). Некритичное чини САМ, не спрашивая: перезапуск упавшего/"
            "зависшего сервиса, убить зависший или утекающий процесс (не базу данных), почистить мусор/кэши/логи, "
            "поднять упавшее. Проверь, что помогло. Спрашивай хозяина только про рискованное: удаление данных, базы, "
            "сеть/ssh/файрвол, остановку важного надолго, root-команды (через rootdo). Запиши в journal.md. "
            "Потом коротко напиши хозяину: что было (кто жрал/что упало), что сделал, нужно ли что-то от него. "
            "Если это ложная тревога или всё уже прошло само и ничего не менял — ответь SILENT."
        )
        asyncio.create_task(run_task(bg_lane, prompt, background=True, show_progress=False,
                                     header=E("warn") + " ", incident=list(new)))


watcher = Watcher()
SNAPSHOT = {"data": None, "at": 0.0}  # последний collect() автонадзора — чтобы статус открывался мгновенно


# ---------------------------------------------------------------- охрана нод (guard.py)

GUARD_DAILY_CAP = 4  # авто-разборов охраны в сутки
GUARD_LAST = {}      # target -> {"at", "ok", "crit", "note", "ports"} — для экрана «Охрана»


def guard_label(target):
    if target == "local":
        return "этот сервер"
    n = next((x for x in nodes() if x["name"] == target), None)
    return node_label(n) if n else target


async def guard_tick(force=False):
    """Снять снимки всех нод и сообщить о новом. Возвращает число находок."""
    base = await asyncio.to_thread(guard.load_base)
    targets = guard.targets()
    results = await asyncio.gather(*(asyncio.to_thread(guard.scan, t, base) for t in targets))
    await asyncio.to_thread(guard.save_base, base)
    alerted = STATE.setdefault("guard_alerted", {})
    total = 0
    for t, r in zip(targets, results):
        if not r["ok"]:
            GUARD_LAST[t] = {"at": time.time(), "ok": False}
            continue
        GUARD_LAST[t] = {"at": time.time(), "ok": True, "crit": len(r["critical"]), "note": len(r["notable"]),
                         "ports": len(r["snap"]["port"])}
        seen = set(alerted.get(t, []))
        crit = [c for c in r["critical"] if force or c not in seen]
        note = [c for c in r["notable"] if force or c not in seen]
        alerted[t] = sorted(seen | set(r["critical"]) | set(r["notable"]))[-300:]
        if not crit and not note:
            continue
        total += len(crit) + len(note)
        await guard_alert(t, crit, note, auto=not force)
    for t in list(alerted):
        if t not in targets:
            del alerted[t]
    STATE["guard_last"] = time.time()
    save_state()
    return total


async def guard_alert(target, crit, note, auto=True):
    who = guard_label(target)
    lines = [f"{E('shield')} **Охрана · {who}**", ""]
    lines += [f"‼️ {c}" for c in crit] + [f"• {c}" for c in note]
    STATE.setdefault("guard_findings", {})[target] = [f"КРИТИЧНО: {c}" for c in crit] + note
    rows = [(btn("Это я, норм", f"ga:{target}", "ok", "success"), btn("Разберись", f"gq:{target}", "check", "danger"))]
    today = dt.date.today().isoformat()
    cnt = STATE.setdefault("guard_runs", [today, 0])
    if cnt[0] != today:
        STATE["guard_runs"] = cnt = [today, 0]
    gs = STATE.setdefault("stats", {}).setdefault("guard", [])
    gs.append(time.time())
    del gs[:-200]
    if crit:
        guests_notify(f"🛡 Охрана · {who}: подозрение на взлом, разбираемся.")
    if crit and auto and cnt[1] < GUARD_DAILY_CAP:
        cnt[1] += 1
        lines += ["", f"{E('busy')} _Похоже на взлом — уже разбираюсь сам._"]
        rows = [(btn("Это я, норм", f"ga:{target}", "ok", "success"),)]
        asyncio.create_task(run_task(bg_lane, guard_prompt(target, crit, note), background=True,
                                     show_progress=False, header=E("shield") + " "))
    urgent = crit or any(c.startswith("❗") or c.startswith("SSH-ключ") or "UID 0" in c for c in note)
    if not urgent:  # знакомые входы, новые юниты/порты после обновлений и т.п. — в сводку
        for c in note:
            info = c.startswith(("обновлялись пакеты", "вход по SSH"))  # уже принято / просто к сведению
            digest(f"Охрана · {who}: {c}", [] if info else [(f"Охрана, {who[:18]}: это я, норм", f"ga:{target}", "ok")])
        return
    await notify("\n".join(lines), markup=kb(*rows), silent=not crit, kind=f"guard:{target}")


def guard_prompt(target, crit, note):
    where = "на ЭТОМ сервере (local)" if target == "local" else f"на ноде `{target}` (`nodes {target} '…'`)"
    return (f"[ОХРАНА] Сторож (guard.py) заметил {where}:\n"
            + "\n".join(f"- КРИТИЧНО: {c}" for c in crit) + ("\n" if crit else "")
            + "\n".join(f"- {c}" for c in note) + "\n\n"
            "Разберись, взлом это или легитимное изменение (обновление пакетов, деплой хозяина, твои собственные "
            "действия — сверься с journal.md). Подробный снимок: `guard scan " + target + "`.\n"
            "Если это явный майнер/бэкдор (бинарник в /tmp, /var/tmp, /dev/shm, маскировка под [kworker], "
            "соединения на пулы) — МОЖЕШЬ САМ: скопировать бинарник в /root/nook-quarantine/<дата> (700/600) "
            "и остановить процесс (kill по точному exe). НЕ запускай найденные файлы и не скачивай их скрипты. "
            "Cron/юниты/ключи/пользователей/конфиги не меняй — предложи хозяину точные шаги. "
            "Если изменение легитимное — выполни `guard accept " + target + "` и ответь SILENT. "
            "Запиши в journal.md. Хозяину коротко: что нашёл, что сделал, что нужно от него.")


async def guard_loop():
    await asyncio.sleep(60)
    while True:
        try:
            if STATE["owner_id"] and STATE.get("guard", True):
                await guard_tick()
        except Exception:
            log.exception("guard tick failed")
        await asyncio.sleep(CFG.get("guard_interval", 600))


async def view_guard():
    on = STATE.get("guard", True)
    lines = [f"{E('shield')} **Охрана нод**", "",
             f"Каждые {CFG.get('guard_interval', 600) // 60} мин сверяю с эталоном: cron, systemd-юниты, SSH-ключи, "
             "пользователей, открытые наружу порты, бинарники в /tmp, AutoRun qBittorrent, ld.so.preload; "
             "ловлю майнеры и входы по SSH с новых адресов. Взлом — сразу разбираюсь сам.", ""]
    for t in guard.targets():
        g = GUARD_LAST.get(t)
        if not g:
            lines.append(f"⚪ **{guard_label(t)}** — ещё не проверял")
        elif not g["ok"]:
            lines.append(f"🔴 **{guard_label(t)}** — нет связи")
        else:
            dot = "🔴" if g["crit"] else "🟡" if g["note"] else "🟢"
            lines.append(f"{dot} **{guard_label(t)}** — портов наружу {g['ports']}"
                         + (f", тревог {g['crit']}" if g["crit"] else "") + (f", изменений {g['note']}" if g["note"] else "")
                         + f" · {dt.datetime.fromtimestamp(g['at']):%H:%M}")
    return "\n".join(lines), kb(
        (btn("Проверить сейчас", "gscan", "refresh", "primary"),),
        (btn(f"Охрана: {'включена' if on else 'выключена'}", "gtoggle", "eye" if on else "eyeoff",
             "success" if on else "danger"),),
        back("more"))


VIEWS["guard"] = view_guard


# ---------------------------------------------------------------- избранное, журнал

PENDING_HANDLERS = {}  # kind -> async fn(msg, pend): ввод текста после кнопки (переименовать, оплата, гость…)


def favs():
    return STATE.setdefault("favs", [])


def fav_title(text):
    t = re.sub(r"\s+", " ", text).strip()
    return t[:34] + ("…" if len(t) > 34 else "")


async def fav_autoname(prompt):
    """Mercury придумывает имя избранному («Проверить диск»), пока висит обрезанный текст."""
    name = await quick_llm("Придумай название кнопки для этой задачи: 1–4 слова, без кавычек, точки и эмодзи, "
                           f"с большой буквы. Только название.\n\nЗадача: {prompt[:1500]}", system="Ты отвечаешь одним коротким названием.")
    name = re.sub(r"[\"«»*`.]", "", (name or "").splitlines()[0] if name else "").strip()
    if not name or len(name) > 34:
        return
    for f in favs():
        if f["p"] == prompt and f["t"] == fav_title(prompt):  # хозяин ещё не переименовал сам
            f["t"] = name
            save_state()


async def view_favs():
    items = favs()
    lines = [f"{E('spark')} **Избранное**", ""]
    if not items:
        lines.append("Пусто. Нажми «В избранное» под моим ответом — задача сохранится сюда, и её можно будет "
                     "запустить снова одним нажатием.")
    rows = [(btn(f["t"], f"fv:{i}", None),) for i, f in enumerate(items)]
    return "\n".join(lines), kb(*rows, (btn("Править", "favedit", "new"),) if items else None, back())


async def view_favedit():
    rows = [(btn(f"✏️ {f['t']}", f"fr:{i}"), btn("Удалить", f"fd:{i}", "cross", "danger")) for i, f in enumerate(favs())]
    return f"{E('new')} **Править избранное**\n\nПереименовать — нажми на название.", kb(*rows, back("favs"))


async def fav_rename(msg, pend):
    i = pend["i"]
    if i < len(favs()):
        favs()[i]["t"] = fav_title(msg.text)
        save_state()
    await delete_msgs(msg.chat.id, [msg.message_id])
    await show(msg.chat.id, "favedit", pend.get("mid"))


PENDING_HANDLERS["favren"] = fav_rename
VIEWS.update(favs=view_favs, favedit=view_favedit)

JOURNAL = os.path.join(HOME, "memory", "journal.md")
JOURNAL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) — (.+)")
JR_PAGE = 8


def journal_entries():
    try:
        lines = open(JOURNAL, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return []
    out = []
    for l in lines:
        m = JOURNAL_RE.match(l.strip())
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out[::-1]


def journal_filter(entries, flt):
    today = dt.date.today()
    if flt == "today":
        return [e for e in entries if e[0] == today.isoformat()]
    if flt == "yday":
        return [e for e in entries if e[0] == (today - dt.timedelta(days=1)).isoformat()]
    if flt == "week":
        return [e for e in entries if e[0] >= (today - dt.timedelta(days=7)).isoformat()]
    if flt.startswith("n"):
        t = guard.targets()[int(flt[1:])] if flt[1:].isdigit() and int(flt[1:]) < len(guard.targets()) else ""
        if t == "local":
            names = [x for x in guard.targets() if x != "local"]
            return [e for e in entries if not any(n in e[2] for n in names)]
        n = next((x for x in nodes() if x["name"] == t), {})
        keys = [k for k in (t, n.get("title"), n.get("host")) if k]
        return [e for e in entries if any(k.lower() in e[2].lower() for k in keys)]
    return entries


def view_journal_page(flt="all", page=0):
    items = journal_filter(journal_entries(), flt)
    pages = max(1, (len(items) + JR_PAGE - 1) // JR_PAGE)
    page = max(0, min(page, pages - 1))
    names = {"all": "всё", "today": "сегодня", "yday": "вчера", "week": "неделя"}
    for i, t in enumerate(guard.targets()):
        names[f"n{i}"] = guard_label(t)
    lines = [f"{E('jobs')} **Что я делал** · {names.get(flt, flt)} · {len(items)}", ""]
    for d, hm, text in items[page * JR_PAGE:(page + 1) * JR_PAGE]:
        day = "сегодня" if d == dt.date.today().isoformat() else dt.date.fromisoformat(d).strftime("%d.%m")
        lines.append(f"**{day} {hm}** — {text[:300]}")
        lines.append("")
    if not items:
        lines.append("*Пусто.*")
    sel = lambda k, label: btn(("✓ " if k == flt else "") + label, f"jr:{k}:0")
    rows = [(sel("today", "Сегодня"), sel("yday", "Вчера"), sel("week", "Неделя"), sel("all", "Всё")),
            tuple(sel(f"n{i}", guard_label(t)[:14]) for i, t in enumerate(guard.targets()))]
    nav = []
    if page > 0:
        nav.append(btn("← Новее", f"jr:{flt}:{page - 1}"))
    if page < pages - 1:
        nav.append(btn(f"Старше → ({page + 2}/{pages})", f"jr:{flt}:{page + 1}"))
    return "\n".join(lines).strip(), kb(*rows, tuple(nav) if nav else None, back("more"))


async def view_journal():
    return view_journal_page()


VIEWS["journal"] = view_journal


# ---------------------------------------------------------------- хосты: локальный сервер + ноды

async def host_sh(target, cmd, timeout=120):
    """Команда на хосте: 'local' — здесь (с окружением агента: bin/ в PATH), иначе — на ноде по SSH. → (код, вывод)."""
    args = ["bash", "-c", cmd] if target == "local" else ["ssh", *ssh_opts(), target, cmd]
    try:
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                 env=codex_env(), cwd=HOME)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace")
    except Exception as e:
        return -1, str(e)


def host_ref(target):
    return "этот сервер (local, текущий пользователь без root — через rootdo)" if target == "local" else f"нода `{target}` (`nodes {target} '…'`)"


def ops():
    return STATE.setdefault("ops", {"apt": {}, "docker": {}})


def daily_due(key, hhmm):
    """Пора ли сегодняшнему ежедневному делу key (после hh:mm, раз в день)."""
    hh, mm = map(int, hhmm.split(":"))
    now = dt.datetime.now()
    done = STATE.setdefault("daily", {})
    if done.get(key) == now.date().isoformat() or (now.hour, now.minute) < (hh, mm):
        return False
    done[key] = now.date().isoformat()
    save_state()
    return True


# ---- обновления безопасности apt

APT_SH = ("export LC_ALL=C DEBIAN_FRONTEND=noninteractive; "
          "[ \"$(id -u)\" = 0 ] && timeout 240 apt-get update -qq >/dev/null 2>&1; "
          "apt list --upgradable 2>/dev/null | grep -v '^Listing' ; "
          "[ -f /var/run/reboot-required ] && echo REBOOT-REQUIRED")


async def apt_scan():
    res = {}
    for t in guard.targets():
        code, out = await host_sh(t, APT_SH, 300)
        if code == -1:
            continue
        lines = [l for l in out.splitlines() if "/" in l]
        sec = sorted({l.split("/")[0] for l in lines if "-security" in l})
        res[t] = {"sec": sec, "all": len(lines), "reboot": "REBOOT-REQUIRED" in out, "at": time.time()}
    ops()["apt"] = res
    save_state()
    return res


async def apt_notify(res, force=False):
    seen = STATE.setdefault("apt_seen", {})
    lines, rows = [], []
    for i, (t, r) in enumerate(res.items()):
        new = set(r["sec"]) - set(seen.get(t, []))
        if (new or force) and (r["sec"] or force):
            lines.append(f"**{guard_label(t)}** — безопасности: {len(r['sec'])}, всего обновлений {r['all']}"
                         + (" · ⚠️ нужна перезагрузка" if r["reboot"] else "")
                         + (f"\n`{', '.join(r['sec'][:10])}{' …' if len(r['sec']) > 10 else ''}`" if r["sec"] else ""))
            if r["sec"] or r["all"]:
                rows.append((btn(f"Обновить: {guard_label(t)[:20]}", f"ap:{i}", "refresh", "primary"),))
        seen[t] = r["sec"]
    save_state()
    if lines:
        if not force:
            n = sum(len(r["sec"]) for r in res.values())
            return digest(f"Обновления безопасности: {n} пакетов — " + ", ".join(
                f"{guard_label(t)} {len(r['sec'])}" for t, r in res.items() if r["sec"]),
                [("Обновить пакеты везде", "ap:all", "refresh")])
        await notify(f"{E('shield')} **Обновления пакетов**\n\n" + "\n\n".join(lines), markup=kb(
            *rows, (btn("Обновить всё", "ap:all", "refresh", "success"),) if len(rows) > 1 else None), silent=True,
            kind="apt")
    elif force:
        trash(STATE["owner_id"], await notify(f"{E('ok')} Обновлений безопасности нет нигде.", silent=True, kind="apt"), 120)


def apt_prompt(targets):
    hosts = "\n".join(f"- {host_ref(t)}" for t in targets)
    return ("Обнови пакеты (сначала security, затем остальные обычные — без dist-upgrade/смены релиза) на:\n" + hosts +
            "\n\nНа нодах: `DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confold upgrade`. "
            "Здесь (local) — через rootdo одной командой. Перед этим запомни, что работает (docker ps, systemctl "
            "--failed, pm2), после — проверь, что всё поднялось; упавшее подними. Если нужна перезагрузка — НЕ "
            "перезагружай, скажи мне. Если изменились конфиги/юниты — `guard accept <нода>`. Запиши в journal.md. "
            "Коротко отчитайся.")


# ---- обновления Docker-образов

DOCKER_SH = ("docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | while IFS=\"$(printf '\\t')\" read n i; do "
             "d=$(docker image inspect --format '{{join .RepoDigests \" \"}}' \"$i\" 2>/dev/null); "
             "printf '%s\\t%s\\t%s\\n' \"$n\" \"$i\" \"$d\"; done")


def parse_image(ref):
    if "@" in ref:
        return None
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        reg, repo = parts[0], "/".join(parts[1:])
    else:
        reg, repo = "registry-1.docker.io", name if "/" in name else "library/" + name
    return reg, repo, tag


async def registry_digest(ref):
    """Digest образа в реестре (без скачивания). None — не удалось."""
    p = parse_image(ref)
    if not p:
        return None
    reg, repo, tag = p
    url = f"https://{reg}/v2/{repo}/manifests/{tag}"
    accept = ", ".join(["application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.docker.distribution.manifest.v2+json",
                        "application/vnd.oci.image.manifest.v1+json"])
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as h:
        async with h.head(url, headers={"Accept": accept}) as r:
            if r.status == 401:
                m = dict(re.findall(r'(\w+)="([^"]*)"', r.headers.get("WWW-Authenticate", "")))
                if not m.get("realm"):
                    return None
                q = {"service": m.get("service", ""), "scope": m.get("scope") or f"repository:{repo}:pull"}
                async with h.get(m["realm"], params=q) as tr:
                    tok = (await tr.json(content_type=None)).get("token") or ""
                async with h.head(url, headers={"Accept": accept, "Authorization": f"Bearer {tok}"}) as r2:
                    return r2.headers.get("Docker-Content-Digest") if r2.status == 200 else None
            return r.headers.get("Docker-Content-Digest") if r.status == 200 else None


async def docker_scan():
    skip = CFG.get("docker_skip", ["remnawave"])
    res = {}
    for t in guard.targets():
        code, out = await host_sh(t, DOCKER_SH, 60)
        if code != 0:
            continue
        upd = []
        cache = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or any(k in line for k in skip):
                continue
            name, image = parts[0], parts[1]
            local = (parts[2] if len(parts) > 2 else "").split()
            if not local:
                continue  # собран локально — не из реестра
            if image not in cache:
                try:
                    cache[image] = await registry_digest(image)
                except Exception:
                    cache[image] = None
            remote = cache[image]
            if remote and not any(d.endswith(remote) for d in local):
                upd.append({"c": name, "i": image, "d": remote})
        res[t] = {"upd": upd, "at": time.time()}
    ops()["docker"] = res
    save_state()
    return res


async def docker_notify(res, force=False):
    seen = STATE.setdefault("docker_seen", {})
    lines, rows = [], []
    for i, (t, r) in enumerate(res.items()):
        fresh = [u for u in r["upd"] if seen.get(f"{t}|{u['i']}") != u["d"]]
        if fresh or (force and r["upd"]):
            imgs = sorted({u["i"] for u in r["upd"]})
            lines.append(f"**{guard_label(t)}**: " + ", ".join(f"`{x}`" for x in imgs))
            rows.append((btn(f"Что нового · {guard_label(t)[:14]}", f"dn:{i}", "info"),
                         btn("Обновить", f"du:{i}", "refresh", "primary")))
        for u in r["upd"]:
            seen[f"{t}|{u['i']}"] = u["d"]
    save_state()
    if lines and not force:
        return digest("Новые версии Docker-образов: " + "; ".join(l.replace("**", "") for l in lines),
                      [("Docker: что нового и обновить", "updcheck", "spark")])
    if lines:
        await notify(f"{E('spark')} **Вышли новые версии Docker-образов**\n\n" + "\n".join(lines), markup=kb(*rows),
                     silent=True, kind="docker")
    elif force:
        trash(STATE["owner_id"], await notify(f"{E('ok')} Все Docker-образы свежие.", silent=True, kind="docker"), 120)


def docker_prompt(t, what):
    imgs = sorted({u["i"] for u in ops()["docker"].get(t, {}).get("upd", [])})
    if what == "news":
        return (f"На {host_ref(t)} вышли новые версии образов: {', '.join(imgs)}. Найди, что в них нового (релизы на "
                "GitHub/Docker Hub, changelog), коротко: важное, ломающие изменения, безопасность. Ничего не обновляй.")
    return (f"Обнови Docker-образы на {host_ref(t)}: {', '.join(imgs)}.\n"
            "1) Найди compose-проект (docker inspect label com.docker.compose.project.working_dir). 2) Снимок для отката: "
            "запиши текущие digest'ы (`docker image inspect`), бэкап compose/.env рядом, тегни старые образы "
            "`<repo>:rollback-YYYYMMDD`; базы данных (postgres/mysql) — сначала дамп. 3) `docker compose pull` + `up -d` "
            "только этих сервисов. 4) Проверь health/логи 2–3 минуты. 5) Если хуже — откати на rollback-теги и подними. "
            "Мажорные версии баз данных НЕ обновляй сам — спроси. Запиши в journal.md, отчитайся коротко.")


# ---- бэкапы: статус существующего server-backup + бэкап нод

BACKUP_STATE = os.path.expanduser("~/.local/state/nook-backup")


def backup_status():
    rd = lambda n: load_json(os.path.join(BACKUP_STATE, n), {})
    return {"run": rd("last-run.json"), "success": rd("last-success.json"), "check": rd("last-check.json"),
            "nodes": rd("nodes-last.json"), "ncheck": rd("nodes-check.json")}


def ago(iso):
    try:
        t = dt.datetime.fromisoformat(iso).timestamp()
    except Exception:
        return "никогда"
    h = int(time.time() - t) // 3600
    return f"{h // 24} д назад" if h >= 48 else f"{h} ч назад" if h else "только что"


async def backup_watch():
    """Сообщить о провале/успехе проверок бэкапа; тревога, если давно не было успешного."""
    b = backup_status()
    seen = STATE.setdefault("backup_seen", {})
    for key, label in (("check", "Еженедельная проверка бэкапа сервера"), ("ncheck", "Пробное восстановление бэкапа нод"),
                       ("nodes", "Бэкап нод")):
        r = b[key]
        fin = r.get("finished_at")
        if not fin or seen.get(key) == fin:
            continue
        seen[key] = fin
        if r.get("status") == "ok":
            if key != "nodes":
                extra = ""
                if key == "ncheck":
                    extra = " — " + ", ".join(f"{guard_label(n)}: {v.get('files', 0)} файлов"
                                              for n, v in r.get("nodes", {}).items())
                digest(f"{label}: бэкап живой{extra}")
        else:
            bad = {n: v for n, v in r.get("nodes", {}).items() if v.get("status") != "ok"}
            await notify(f"{E('err')} **{label} не удалась**: "
                         + ("; ".join(f"{guard_label(n)} — `{(v.get('error') or '')[:150]}`" for n, v in bad.items())
                            or f"`{r.get('error', '')[:200]}`"),
                         markup=kb((btn("Разберись", "bk:fix", "check", "danger"),)), kind="backup")
            guests_notify(f"⚠️ {label} не удалась")
    ok_at = b["success"].get("finished_at")
    if ok_at:
        age = time.time() - dt.datetime.fromisoformat(ok_at).timestamp()
        if age > 30 * 3600 and seen.get("stale") != ok_at:
            seen["stale"] = ok_at
            await notify(f"{E('warn')} Последний успешный бэкап сервера был {ago(ok_at)} — что-то не так с ночным бэкапом.",
                         markup=kb((btn("Разберись", "bk:fix", "check", "danger"),)), kind="backup")
    save_state()


async def view_backups():
    b = backup_status()
    mark = lambda r: "🟢" if r.get("status") == "ok" else "🟡" if r.get("status") == "running" else "🔴" if r else "⚪"
    run, chk, nd, nc = b["run"], b["check"], b["nodes"], b["ncheck"]
    summ = run.get("summary", {})
    lines = [f"{E('disk')} **Бэкапы** → зашифрованный restic в Google Drive", "",
             f"{mark(run)} **Сервер** — {ago(run.get('finished_at'))}"
             + (f", {summ.get('total_bytes_processed', 0) / 1e9:.1f} ГБ, +{summ.get('data_added_packed', 0) / 1e6:.0f} МБ"
                if summ else "") + "  ·  ежедневно 03:20",
             f"{mark(chk)} проверка архива — {ago(chk.get('finished_at'))}  ·  по воскресеньям 06:20"]
    for n, v in nd.get("nodes", {}).items():
        lines.append(f"{mark(v)} **{guard_label(n)}** — {ago(nd.get('finished_at'))}"
                     + (f", {(v.get('bytes') or 0) / 1e6:.0f} МБ" if v.get("bytes") else ""))
    if not nd:
        lines.append("⚪ **Ноды** — ещё не было  ·  ежедневно 04:10")
    lines.append(f"{mark(nc)} пробное восстановление нод — {ago(nc.get('finished_at'))}  ·  по воскресеньям 06:50")
    lines += ["", "*Снимки не удаляются. Каждый ночной бэкап сам восстанавливает пробный файл и SQLite-базу для проверки. "
              "Remnawave не бэкаплю (пустой); vault и аудио kip уже лежат в gdrive и копируются оттуда.*"]
    return "\n".join(lines), kb((btn("Сделать сейчас", "bk:now", "refresh", "primary"),
                                 btn("Проверить восстановление", "bk:test", "check")), back("more"))


# ---- оплата серверов

def pay():
    return STATE.setdefault("pay", {})


def add_period(d, per):
    if per.endswith("d"):
        return d + dt.timedelta(days=int(per[:-1]))
    months = int(per[:-1]) if per.endswith("m") else 1
    y, m = divmod(d.month - 1 + months, 12)
    import calendar
    day = min(d.day, calendar.monthrange(d.year + y, m + 1)[1])
    return d.replace(year=d.year + y, month=m + 1, day=day)


def parse_pay(text):
    t = text.lower().strip()
    if t in ("нет", "убрать", "удалить", "-"):
        return "clear"
    today = dt.date.today()
    m = date = None
    for c in re.finditer(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", t):  # «5.99$» — цена, а не дата
        d, mo, y = int(c.group(1)), int(c.group(2)), c.group(3)
        y = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
        try:
            date, m = dt.date(y, mo, d), c
            break
        except ValueError:
            continue
    if m:
        if not m.group(3) and date < today:
            date = date.replace(year=date.year + 1)
        rest = t[:m.start()] + " " + t[m.end():]
    else:
        m = re.search(r"\b(\d{1,2})\s*(?:числа|число|-?го)?\b", t)
        if not m or not 1 <= int(m.group(1)) <= 31:
            return None
        d = int(m.group(1))
        import calendar
        date = today.replace(day=min(d, calendar.monthrange(today.year, today.month)[1]))
        if date < today:
            date = add_period(date.replace(day=1), "1m")
            date = date.replace(day=min(d, calendar.monthrange(date.year, date.month)[1]))
        rest = t[:m.start()] + " " + t[m.end():]
    per = "1m"
    pm = re.search(r"(\d+)\s*(мес|month)|\b(год|year|ежегодно)|\b(недел|week)", rest)
    if pm:
        per = f"{pm.group(1)}m" if pm.group(1) else "12m" if pm.group(3) else "7d"
        rest = rest[:pm.start()] + rest[pm.end():]
    amount = re.sub(r"\s+", " ", rest).strip(" ,.;—-")
    return {"date": date.isoformat(), "amount": amount[:30], "period": per}


def pay_line(t):
    p = pay().get(t)
    if not p:
        return "оплата не задана"
    d = dt.date.fromisoformat(p["date"])
    left = (d - dt.date.today()).days
    per = {"1m": "/мес", "12m": "/год", "7d": "/нед"}.get(p["period"], f"/{p['period']}")
    when = "сегодня" if left == 0 else f"через {left} дн" if left > 0 else f"просрочено на {-left} дн"
    return f"{d:%d.%m} ({when})" + (f" · {p['amount']}{per}" if p.get("amount") else "")


async def view_pay():
    lines = [f"{E('cal')} **Оплата серверов**", "", "Напомню за 3 дня до оплаты.", ""]
    rows = []
    for i, t in enumerate(guard.targets()):
        lines.append(f"**{guard_label(t)}** — {pay_line(t)}")
        rows.append((btn(f"Изменить · {guard_label(t)[:22]}", f"pe:{i}", "new"),))
    return "\n".join(lines), kb(*rows, back("more"))


async def pay_input(msg, pend):
    t = pend["t"]
    r = parse_pay(msg.text)
    await delete_msgs(msg.chat.id, [msg.message_id])
    if r is None:
        PENDING[msg.chat.id] = dict(pend, until=time.time() + 600)
        return await edit(msg.chat.id, pend["mid"], f"{E('err')} Не понял. Пример: `25.11 300₽` или `25 числа 5$ год`. "
                          "Пришли ещё раз.", kb((btn("Отмена", "pay", "cross"),)))
    if r == "clear":
        pay().pop(t, None)
    else:
        pay()[t] = r
        STATE.setdefault("pay_reminded", {}).pop(t, None)
    save_state()
    await show(msg.chat.id, "pay", pend["mid"])


PENDING_HANDLERS["pay"] = pay_input


async def pay_check():
    today = dt.date.today()
    rem = STATE.setdefault("pay_reminded", {})
    for i, t in enumerate(guard.targets()):
        p = pay().get(t)
        if not p or rem.get(t) == today.isoformat():
            continue
        left = (dt.date.fromisoformat(p["date"]) - today).days
        if left > 3:
            continue
        rem[t] = today.isoformat()
        save_state()
        when = "сегодня" if left == 0 else f"через {left} дн ({dt.date.fromisoformat(p['date']):%d.%m})" if left > 0 \
            else f"**просрочено на {-left} дн**"
        await notify(f"{E('cal')} **Оплата сервера** {guard_label(t)} — {when}" + (f", {p['amount']}" if p.get("amount") else ""),
                     markup=kb((btn("Оплатил", f"pp:{i}", "ok", "success"), btn("Напомнить завтра", f"pz:{i}", "bell"))),
                     kind=f"pay:{t}")


# ---- деплой по push

def deploys():
    return STATE.setdefault("deploys", [])


def deploy_candidates():
    """Проекты из registry.json с git-репозиторием и remote."""
    try:
        reg = json.load(open(os.path.expanduser("~/status-board/registry.json")))
    except Exception:
        return []
    have = {d["path"] for d in deploys()}
    out = []
    for p in reg.get("projects", []):
        path = p.get("path") or ""
        if path and path not in have and os.path.isdir(os.path.join(path, ".git")) and path not in [c["path"] for c in out]:
            out.append({"path": path, "name": p.get("name") or os.path.basename(path), "id": p.get("id")})
    return out


async def git(path, *args, timeout=40):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="ssh -o BatchMode=yes")
    try:
        p = await asyncio.create_subprocess_exec("git", "-C", path, *args, env=env, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode(errors="replace").strip()
    except Exception as e:
        return -1, str(e)


async def deploy_add(c):
    code, branch = await git(c["path"], "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or branch == "HEAD":
        return f"не понял ветку ({branch[:100]})"
    code, out = await git(c["path"], "ls-remote", "origin", f"refs/heads/{branch}")
    if code != 0 or not out:
        return f"не достучался до origin: `{out[-150:]}`"
    deploys().append({"path": c["path"], "name": c["name"], "id": c.get("id"), "branch": branch, "last": out.split()[0]})
    save_state()
    return None


async def deploy_check():
    for d in deploys():
        code, out = await git(d["path"], "ls-remote", "origin", f"refs/heads/{d['branch']}")
        if code != 0 or not out:
            continue
        sha = out.split()[0]
        if sha == d["last"]:
            continue
        old = d["last"]
        d["last"] = sha
        save_state()
        _, head = await git(d["path"], "rev-parse", "HEAD")
        if head == sha:
            continue  # уже стоит (деплоили руками)
        log.info("deploy %s %s -> %s", d["name"], old[:7], sha[:7])
        prompt = (f"[ДЕПЛОЙ] В репозиторий проекта «{d['name']}» ({d['path']}, registry id {d.get('id')}) запушили "
                  f"ветку {d['branch']}: {old[:7]} → {sha[:7]}. Сейчас локально HEAD {head[:7]}.\n"
                  "Задеплой: 1) если есть незакоммиченные локальные изменения — НЕ трогай, напиши мне. "
                  "2) `git pull --ff-only`. 3) если поменялись зависимости (package.json/lock, requirements, pyproject) — "
                  "поставь; если есть сборка — собери. 4) перезапусти проект так, как он запускается (registry.json: pm2/"
                  "systemd/docker). 5) проверь, что живой (status-board/health/логи). 6) если сломалось — "
                  f"`git reset --hard {head[:12]}`, пересобери/перезапусти, проверь. Запиши в journal.md. "
                  "Отчитайся коротко: что пришло (git log --oneline), удалось ли.")
        asyncio.create_task(run_task(bg_lane, prompt, background=True, show_progress=False, header=E("refresh") + " "))


async def view_deploy():
    lines = [f"{E('refresh')} **Деплой по push**", "",
             "Слежу за GitHub: как только запушишь в ветку — сам подтяну, соберу, перезапущу и проверю. "
             "Сломалось — откачу на прошлую версию.", ""]
    rows = []
    for i, d in enumerate(deploys()):
        lines.append(f"🟢 **{d['name']}** — `{d['branch']}` @ `{d['last'][:7]}`")
        rows.append((btn(f"Убрать {d['name'][:24]}", f"dpd:{i}", "cross"),))
    if not deploys():
        lines.append("*Пока ни одного проекта.*")
    return "\n".join(lines), kb((btn("Добавить проект", "dpadd", "plus", "success"),), *rows, back("more"))


DEPLOY_CANDS = []


async def view_deploy_add():
    DEPLOY_CANDS[:] = deploy_candidates()
    rows = [(btn(c["name"][:40], f"dpa:{i}"),) for i, c in enumerate(DEPLOY_CANDS[:40])]
    text = (f"{E('plus')} **Какой проект деплоить по push?**\n\nПроекты из реестра, у которых есть git-репозиторий."
            if rows else f"{E('info')} Подходящих проектов нет — у проектов в реестре нет git-репозиториев.")
    return text, kb(*rows, back("deploy"))


# ---- гости

def guests():
    return STATE.setdefault("guests", [])


def is_guest(uid):
    return any(g["id"] == uid for g in guests())


def guests_notify(text):
    """Тревоги гостям: коротко, без кнопок управления."""
    for g in guests():
        asyncio.create_task(_guest_send(g["id"], text))


async def _guest_send(uid, text, markup=None):
    tok = UI.set("classic")
    try:
        await send(uid, text, markup=markup)
    except Exception as e:
        log.warning("guest %s send failed: %s", uid, e)
    finally:
        UI.reset(tok)


def guest_kb():
    return kb((btn("Обновить", "gv:status", "refresh"), btn("Графики", "gg:0:d", "status")))


async def guest_view():
    ent = (SNAPSHOT.get("data") or ({}, {}))[0]
    lines = [f"{E('home')} **Статус серверов** (только просмотр)", "", await quick_status(), ""]
    for t in guard.targets()[1:]:
        ok = ent.get(f"node:{t}", (None,))[0]
        m = METRICS_NOW.get(t, {})
        lines.append(f"{'🟢' if ok else '🔴' if ok is False else '⚪'} **{guard_label(t)}**"
                     + (f" — CPU {m.get('cpu')}% · RAM {m.get('mem')}% · диск {m.get('disk')}%" if m else ""))
    bad = [d for k, (ok, d) in ent.items() if ok is False and k in STATE.get("reported", {})]
    if bad:
        lines += ["", f"{E('warn')} **Сейчас проблемы:**"] + [f"• {d}" for d in bad[:10]]
    return "\n".join(lines)


async def guest_home(msg):
    await _guest_send(msg.chat.id, await guest_view(), guest_kb())


async def guest_button(cb):
    data = cb.data or ""
    if data == "gv:status":
        await cb.answer()
        tok = UI.set("classic")
        try:
            if not await edit(cb.message.chat.id, cb.message.message_id, await guest_view(), guest_kb(), rich=False):
                await send(cb.message.chat.id, await guest_view(), markup=guest_kb())
        finally:
            UI.reset(tok)
        return
    if data.startswith("gg:"):
        await cb.answer()
        _, i, p = data.split(":")
        return await send_graph(cb.message.chat.id, int(i), p,
                                cb.message.message_id if cb.message.photo else None, prefix="gg")
    await cb.answer("Гостю доступен только просмотр")


async def view_guests():
    lines = [f"{E('eye')} **Гости**", "", "Гость видит статус, ноды и графики и получает тревоги, но ничем не управляет "
             "и не говорит с агентом.", ""]
    rows = []
    for g in guests():
        lines.append(f"• **{g.get('name') or g['id']}** (`{g['id']}`)")
        rows.append((btn(f"Убрать {str(g.get('name') or g['id'])[:24]}", f"gu:{g['id']}", "cross", "danger"),))
    if not guests():
        lines.append("*Пока никого.*")
    return "\n".join(lines), kb((btn("Добавить гостя", "guadd", "plus", "success"),), *rows, back("more"))


async def guest_input(msg, pend):
    uid, name = None, None
    origin = getattr(msg, "forward_origin", None)
    if origin is not None and getattr(origin, "sender_user", None):
        uid, name = origin.sender_user.id, origin.sender_user.full_name
    elif msg.contact and msg.contact.user_id:
        uid, name = msg.contact.user_id, " ".join(x for x in (msg.contact.first_name, msg.contact.last_name) if x)
    elif msg.text and re.fullmatch(r"\s*\d{5,15}\s*", msg.text):
        uid = int(msg.text)
    await delete_msgs(msg.chat.id, [msg.message_id])
    if not uid:
        PENDING[msg.chat.id] = dict(pend, until=time.time() + 600)
        return await edit(msg.chat.id, pend["mid"], f"{E('err')} Не вышло узнать ID (у человека может быть скрыта пересылка). "
                          "Пришли его контакт или числовой ID (его можно узнать у @userinfobot).",
                          kb((btn("Отмена", "guests", "cross"),)))
    if uid == STATE["owner_id"]:
        return await show(msg.chat.id, "guests", pend["mid"])
    if not is_guest(uid):
        guests().append({"id": uid, "name": name})
        save_state()
    await show(msg.chat.id, "guests", pend["mid"])
    await send_tmp(msg.chat.id, f"{E('ok')} Гость добавлен. Пусть откроет бота и нажмёт «Запустить» — без этого Telegram не даст "
               "мне ему писать.", 120, silent=True)


PENDING_HANDLERS["guest"] = guest_input


# ---- автооткат своих правок (bin/watchfix)

WATCHFIX = os.path.join(HOME, "spool", "watchfix.json")


async def watchfix_tick():
    items = load_json(WATCHFIX, [])
    if not items:
        return
    ent = (SNAPSHOT.get("data") or ({}, {}))[0]
    keep, now = [], time.time()
    for it in items:
        if it.get("key"):
            ok = ent.get(it["key"], (None,))[0]
        else:
            code, _ = await host_sh("local", it["check"], 60)
            ok = code == 0
        it["bad"] = 0 if ok or ok is None else it.get("bad", 0) + 1
        it["checks"] = it.get("checks", 0) + 1
        if it["bad"] >= 3 and now - it["at"] > 120:
            restore = "\n".join(f"- `{r['bak']}` → `{r['orig']}`" for r in it.get("restore", []))
            prompt = ("[АВТООТКАТ] После твоей правки стало хуже: " + (f"`{it['key']}` не в норме" if it.get("key")
                      else f"проверка `{it['check']}` падает") + " 3 проверки подряд.\n"
                      f"Что ты менял: {it.get('note') or '—'}\n" + (f"Верни бэкапы:\n{restore}\n" if restore else "") +
                      "Сначала убедись, что хуже стало на самом деле. Если тревога ложная (проверка кривая, сервис в порядке) — "
                      "ничего не откатывай и ответь ровно SILENT. Иначе откати свою правку (верни файлы, перезапусти "
                      "сервис), проверь, что стало как было, запиши в journal.md и коротко напиши хозяину: что менял, "
                      "почему откатил, что дальше предлагаешь.")
            log.info("watchfix failing: %s", it.get("note") or it.get("key") or it["check"])
            asyncio.create_task(run_task(bg_lane, prompt, background=True, show_progress=False, header=E("repeat") + " "))
            continue
        if now - it["at"] < it.get("minutes", 10) * 60:
            keep.append(it)
        else:
            log.info("watchfix ok: %s", it.get("note") or it.get("key"))
    # пока шли проверки, агент мог добавить новую правку (или сделать --clear) — не затираем
    cur = load_json(WATCHFIX, [])
    seen, now_ats = {it["at"] for it in items}, {it["at"] for it in cur}
    save_json(WATCHFIX, [it for it in keep if it["at"] in now_ats] + [it for it in cur if it["at"] not in seen])


# ---- дневная сводка (тихий режим)

def digest_text(items):
    lines = [f"{E('info')} **Сводка** — всё спокойное за сутки, по одной строке", ""]
    seen = set()
    for it in items:
        t = re.sub(r"\s+", " ", it["t"]).strip()
        if t in seen:
            continue
        seen.add(t)
        lines.append(f"• {dt.datetime.fromtimestamp(it['at']):%H:%M} {t[:260]}")
    return "\n".join(lines)


def digest_kb(items):
    rows, seen = [], set()
    for it in items:
        for t, d, i in it.get("b", []):
            if d not in seen and len(rows) < 6:
                seen.add(d)
                rows.append((btn(t, d, i),))
    return kb(*rows, (btn("Меню", "menu", "home"),))


async def view_digest():
    items = STATE.get("digest", [])
    STATE["digest"] = []  # прочитано
    save_state()
    if not items:
        return f"{E('ok')} **Сводка** — пусто, ничего не происходило.", kb(back())
    markup = digest_kb(items)
    rows = [r for r in markup.inline_keyboard if r[0].callback_data != "menu"]
    return digest_text(items), kb(*rows, back())


VIEWS["digest"] = view_digest


async def digest_send(force=False):
    items = STATE.get("digest", [])
    if not items:
        if force:
            trash(STATE["owner_id"], await notify(f"{E('ok')} В сводке пусто — ничего не происходило.", silent=True), 60)
        return
    STATE["digest"] = []
    save_state()
    await notify(digest_text(items), markup=digest_kb(items), silent=True, kind="digest")


# ---- общий цикл ежедневных дел

async def ops_loop():
    async def step(name, fn):  # упавшее дело не должно отменять остальные
        try:
            await fn()
        except Exception:
            log.exception("ops %s failed", name)

    async def deploy_due():
        if deploys() and time.time() - STATE.get("deploy_at", 0) > CFG.get("deploy_interval", 180):
            STATE["deploy_at"] = time.time()
            await deploy_check()

    async def weekly_due():
        now = dt.datetime.now()
        week = now.strftime("%G-W%V")
        hh, mm = map(int, CFG.get("weekly_time", "20:00").split(":"))
        if now.weekday() == 6 and (now.hour, now.minute) >= (hh, mm) and STATE.get("weekly") != week:
            STATE["weekly"] = week
            save_state()
            await weekly_report()

    async def apt_daily():
        await apt_notify(await apt_scan())

    async def docker_daily():
        await docker_notify(await docker_scan())

    daily = (("digest", lambda: CFG.get("digest_time", "10:45"), digest_send),
             ("pay", lambda: "10:00", pay_check),
             ("apt", lambda: CFG.get("updates_time", "10:30"), apt_daily),
             ("docker", lambda: CFG.get("updates_time", "10:30"), docker_daily))

    await asyncio.sleep(90)
    while True:
        if STATE["owner_id"]:
            await step("watchfix", watchfix_tick)
            await step("backups", backup_watch)
            await step("deploy", deploy_due)
            for key, at, fn in daily:
                if daily_due(key, at()):
                    await step(key, fn)
            await step("weekly", weekly_due)
        await asyncio.sleep(60)


# ---- недельный отчёт

def week_facts():
    lines = []
    for t in guard.targets():
        rows = metrics_get(t, int(time.time()) - 7 * 86400)
        if len(rows) < 10:
            continue
        first = [r for r in rows[:30] if r[3] is not None]
        last = [r for r in rows[-30:] if r[3] is not None]
        if not first or not last:
            continue
        d0 = sum(r[3] for r in first) / len(first)
        d1 = sum(r[3] for r in last) / len(last)
        days = max((rows[-1][0] - rows[0][0]) / 86400, 0.5)
        growth = (d1 - d0) / days
        left = f"≈{(100 - d1) / growth:.0f} дн до заполнения" if growth > 0.05 else "не растёт"
        cpu = [r[1] for r in rows if r[1] is not None]
        mem = [r[2] for r in rows if r[2] is not None]
        lines.append(f"- {guard_label(t)}: диск {d0:.0f}% → {d1:.0f}% ({growth:+.2f} п.п./день, {left}); "
                     f"CPU ср {sum(cpu) / len(cpu):.0f}% макс {max(cpu)}%; RAM ср {sum(mem) / len(mem):.0f}% макс {max(mem)}%")
    st = STATE.setdefault("stats", {})
    wk = lambda k: len([x for x in st.get(k, []) if x > time.time() - 7 * 86400])
    lines.append(f"- инцидентов автонадзора: {wk('incidents')}, тревог охраны: {wk('guard')}")
    b = backup_status()
    lines.append(f"- бэкап сервера: {b['run'].get('status', '—')} ({ago(b['run'].get('finished_at'))}), проверка: "
                 f"{b['check'].get('status', '—')} ({ago(b['check'].get('finished_at'))}); ноды: "
                 f"{b['nodes'].get('status', '—')}, пробное восстановление нод: {b['ncheck'].get('status', '—')}")
    apt_n = sum(len(r["sec"]) for r in ops().get("apt", {}).values())
    dk_n = sum(len(r["upd"]) for r in ops().get("docker", {}).values())
    lines.append(f"- ждут обновления: security-пакетов {apt_n}, Docker-образов {dk_n}")
    pays = [f"{guard_label(t)} {pay_line(t)}" for t in guard.targets() if t in pay()]
    if pays:
        lines.append("- оплата: " + "; ".join(pays))
    return "\n".join(lines)


VIEWS.update(backups=view_backups, pay=view_pay, deploy=view_deploy, deploy_add=view_deploy_add, guests=view_guests)


async def weekly_report():
    from aiogram.types import BufferedInputFile
    png = await asyncio.to_thread(render_disk_all, 7)
    if png:
        await bot.send_photo(STATE["owner_id"], BufferedInputFile(png, "disk-week.png"), caption="📊 Диск за неделю",
                             disable_notification=True)
    facts = await asyncio.to_thread(week_facts)
    since = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    journal = [f"{d} {t} — {w}" for d, t, w in journal_entries() if d >= since][:60][::-1]  # новые первыми → хронология
    text = await quick_llm(
        "Напиши хозяину короткий воскресный отчёт о доме за неделю: 5–10 строк, по делу, по-дружески. "
        "Что ломалось и что чинил/чистил (сколько места освободил, если есть), в конце — что предлагаешь на "
        "следующую неделю (если есть). Не выдумывай: только то, что ниже.\n\nЦифры:\n" + facts
        + "\n\nЖурнал за 7 дней:\n" + ("\n".join(journal) or "(пусто)"), timeout=90)
    if text:
        return await notify("📊 " + text, silent=True, kind="weekly")
    prompt = ("[НЕДЕЛЬНЫЙ ОТЧЁТ] Напиши хозяину короткий воскресный отчёт о доме за неделю. Цифры уже посчитаны:\n"
              + facts + "\n\nДобавь из memory/journal.md за последние 7 дней: что ломалось и что ты чинил/чистил "
              "(сколько места освободил, если есть). Формат: 5–10 строк, по делу, по-дружески; в конце — что "
              "предлагаешь сделать на следующей неделе (если есть). Ничего не меняй на серверах.")
    asyncio.create_task(run_task(bg_lane, prompt, background=True, show_progress=False, header="📊 "))


# ---------------------------------------------------------------- моды (mods.py): расширения без правки ядра

BASE_COMMANDS = ("start", "start2", "menu", "status", "limits", "jobs", "new", "stop", "watch", "login", "mods", "graph")
MOD_ERRS = {}  # имя -> (где, traceback) — последняя ошибка, для кнопки «Почини»
MOD_ERR_AT = {}


def mod_rows(where):
    items = REG.buttons(where)
    rows = [tuple(m.btn(text, key, icon=icon) for m, text, key, icon in items[i:i + 2]) for i in range(0, len(items), 2)]
    return rows


async def mod_error(mod, where, e, tb):
    MOD_ERRS[mod.name] = (where, tb)
    if time.time() - MOD_ERR_AT.get(mod.name, 0) < 600:  # не чаще раза в 10 минут на мод
        return
    MOD_ERR_AT[mod.name] = time.time()
    await notify(f"{E('err')} Мод **{mod.name}** упал ({where}): `{str(e)[:200]}`",
                 markup=kb((btn("Почини", f"mfix:{mod.name}", "spark", "primary"),)), silent=True, kind=f"mod:{mod.name}")


core = type("Core", (), {})()
core.send, core.edit, core.notify, core.btn, core.kb, core.E = send, edit, notify, btn, kb, E
core.owner_ask, core.mod_error, core.base_commands = owner_ask, mod_error, BASE_COMMANDS
core.owner = lambda: STATE["owner_id"]
core.llm = llm
core.digest = lambda text, buttons=(): digest(text, buttons)
core.bg_task = lambda prompt, title: run_task(bg_lane, prompt, background=True, show_progress=False, header=E("plus") + " ")
core.codex_login = do_login
core.bot, core.tg, core.state = bot, tg, STATE
core.maintenance_lanes = (user_lane, bg_lane)
REG = modsys.Registry(core)


async def set_commands():
    """Хозяин не любит /команды — меню команд в Telegram держим пустым, всё через кнопки.
    Сами команды (и команды модов) продолжают работать, если их набрать."""
    try:
        await bot.delete_my_commands()
    except Exception as e:
        log.warning("delete_my_commands failed: %s", e)


async def mods_reload():
    r = REG.reload()
    save_json(modsys.RESULT_PATH, r)
    await set_commands()
    log.info("mods reloaded: %s, failed: %s", r["loaded"], list(r["failed"]))
    return r


async def core_import_ok():
    """Проверить, что bot.py после правок вообще импортируется (без запуска)."""
    code = ("import re,sys;src=open(sys.argv[1]).read();"
            "src=src.replace('asyncio.run(main())','pass');"
            "exec(compile(src,sys.argv[1],'exec'),{'__file__':sys.argv[1],'__name__':'check'})")
    p = await asyncio.create_subprocess_exec(sys.executable, "-c", code, os.path.join(HOME, "bot.py"),
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), 60)
    return p.returncode == 0, out.decode(errors="replace")[-1500:]


async def mods_loop():
    from nook_update import heartbeat
    good_saved = False
    started = time.time()
    while True:
        try:
            heartbeat(Path(HOME), REG.failed)
            if os.path.exists(modsys.RELOAD_FLAG):
                os.remove(modsys.RELOAD_FLAG)
                await mods_reload()
            if not good_saved and time.time() - started > 60:  # прожили минуту — эту версию ядра считаем рабочей
                shutil.copy2(os.path.join(HOME, "bot.py"), os.path.join(modsys.SPOOL, "bot.py.good"))
                good_saved = True
            if os.path.exists(modsys.RESTART_FLAG) and not (user_lane.current or bg_lane.current
                                                            or user_lane.lock.locked() or bg_lane.lock.locked()):
                os.remove(modsys.RESTART_FLAG)
                ok, out = await core_import_ok()
                if ok:
                    pass
                    log.info("core restart requested")
                    save_state()
                    os._exit(0)  # pm2 поднимет заново
                await notify(f"{E('err')} Не перезапускаюсь: bot.py с ошибкой, работаю на старой версии.\n```\n{out}\n```\n"
                             f"Рабочая копия: `spool/mods/bot.py.good`.",
                             markup=kb((btn("Почини ядро", "mfix:__core__", "spark", "primary"),)))
                MOD_ERRS["__core__"] = ("bot.py", out)
        except Exception:
            log.exception("mods loop failed")
        await asyncio.sleep(2)


async def view_mods():
    files = modsys.mod_files()
    dis = set(modsys.load_conf().get("disabled", []))
    lines = [f"{E('plus')} **Моды**", "",
             "Мои расширения: команды, кнопки, фоновые задачи. Попроси «сделай мод, который …» — напишу и подключу "
             "на лету, без перезапуска.", ""]
    rows = []
    for n in files:
        m = REG.mods.get(n)
        if n in dis:
            lines.append(f"⚪ **{n}** — выключен")
        elif n in REG.failed:
            lines.append(f"🔴 **{n}** — не загрузился: `{REG.failed[n].strip().splitlines()[-1][:120]}`")
        elif m:
            extra = [f"/{c}" for c in m.commands] + [f"[{t}]" for t, *_ in m.buttons]
            lines.append(f"🟢 **{n}** — {m.title}" + (f"\n    {' '.join(extra)}" if extra else ""))
        rows.append((btn(("Включить " if n in dis else "Выключить ") + n, f"mt:{n}",
                         "ok" if n in dis else "eyeoff"),))
    if not files:
        lines.append("*Пока ни одного.*")
    return "\n".join(lines), kb(*mod_rows("mods"), *rows,
                                (btn("Перечитать моды", "mreload", "refresh"), btn("Перезапуск ядра", "mrestart", "repeat")),
                                back("more"))


VIEWS["mods"] = view_mods


async def check_limits():
    """Предупредить хозяина один раз за окно, когда лимит Codex перевалил за 90%."""
    L = await get_limits(0)
    if not L:
        return
    # если живые данные говорят, что лимит уже сброшен, а откат ещё активирован (по старому состояу) — снять его
    if not L.get("reached") and not any(w and w["used"] >= 97 for w in (L.get("primary"), L.get("secondary"))):
        if STATE.get("fallback_until"):
            STATE["fallback_until"] = 0
            save_state()
            digest("Лимит Codex сбросился — вернулся на основную модель")
    sent = STATE.setdefault("limit_alerts", {})
    near = [w for w in (L.get("primary"), L.get("secondary")) if w and w["used"] >= 98 and w["reset"] > time.time()]
    if (near or L.get("reached")) and CFG.get("model") != MERCURY_MODEL and not is_antigravity_model(CFG.get("model")) and not fallback_active() and mercury_key():
        STATE["fallback_until"] = max([w["reset"] for w in near] or [time.time() + 3600])
        save_state()
        digest(f"Лимит Codex почти исчерпан — работал на быстрой модели Mercury 3 до сброса "
               f"({dt.datetime.fromtimestamp(STATE['fallback_until']):%d.%m %H:%M})")
    for key in ("primary", "secondary"):
        w = L.get(key)
        if w and w["used"] >= LIMIT_WARN and sent.get(key) != w["reset"] and w["reset"] > time.time():
            sent[key] = w["reset"]
            save_state()
            await notify(f"⚠️ Лимит Codex ({win_name(w['mins'])}) использован на **{w['used']:.0f}%**, "
                         f"сброс {until(w['reset'])}. Могу не успеть доделать большие задачи.",
                         markup=kb((btn("Лимиты", "limits!", "limits"), btn("Модель", "model", "robot"))), kind="limit")


async def watch_loop():
    await asyncio.sleep(20)
    while True:
        try:
            confirmed, recovered, ent = await watcher.tick()
            if STATE["watch"] and STATE["owner_id"]:
                await watcher.handle(confirmed, recovered, ent)
                await check_limits()
        except Exception:
            log.exception("watch tick failed")
        await asyncio.sleep(WATCH_INTERVAL)


async def quick_status():
    du = shutil.disk_usage("/")
    mi = meminfo()
    l1, l5, _ = os.getloadavg()
    up = time.time() - float(open("/proc/uptime").read().split()[0])
    lines = [
        f"{E('disk')} диск: {du.used / du.total * 100:.0f}% (свободно {du.free / 2**30:.1f} ГБ)",
        f"{E('ram')} RAM: доступно {mi['MemAvailable'] / 2**30:.1f} из {mi['MemTotal'] / 2**30:.1f} ГБ, "
        f"swap {(mi['SwapTotal'] - mi['SwapFree']) / 2**30:.1f} ГБ",
        f"{E('load')} load: {l1:.2f} / {l5:.2f} ({os.cpu_count()} CPU)",
        f"{E('uptime')} аптайм: {(time.time() - up) / 86400:.1f} дн",
    ]
    try:
        if not FORCE.get() and SNAPSHOT["data"] and time.time() - SNAPSHOT["at"] < WATCH_INTERVAL + 60:
            ent, alerts = SNAPSHOT["data"]
        else:
            ent, alerts = await collect()
            SNAPSHOT.update(data=(ent, alerts), at=time.time())
        down = [d for k, (ok, d) in ent.items() if ok is False and k in STATE["known_good"]]
        lines += [f"{E('warn')} {a}" for a in alerts.values()]
        lines.append("")
        if down:
            lines.append(f"{E('down')} **упало (раньше работало): {len(down)}**")
            lines += [f"- {d}" for d in down[:10]] + [""]
        else:
            lines.append(f"{E('ok')} всё, что работало, работает")
    except Exception as e:
        lines.append(f"(мониторинг: {e})")
    try:
        short = limits_short(await get_limits())
        if short:
            lines.append(f"{E('robot')} Codex: {short}")
    except Exception:
        pass
    lines.append(f"{E('eye' if STATE['watch'] else 'eyeoff')} автонадзор: {'вкл' if STATE['watch'] else 'выкл'}"
                 f"{' · занят' if user_lane.current or bg_lane.current else ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------- outbox, jobs, patrol

async def outbox_loop():
    while True:
        try:
            if STATE["owner_id"]:
                for name in sorted(os.listdir(OUTBOX)):
                    if not name.endswith(".json"):
                        continue
                    path = os.path.join(OUTBOX, name)
                    item = load_json(path, {})
                    os.remove(path)
                    try:  # одно неудачное сообщение не должно ронять остальные
                        await outbox_send(item)
                    except Exception as e:
                        log.exception("outbox item failed")
                        await notify(f"{E('err')} Не смог отправить сообщение агента: {str(e)[:300]}"
                                     + (f"\n\n{item['text'][:3000]}" if item.get("text") else ""))
        except Exception:
            log.exception("outbox failed")
        await asyncio.sleep(3)


async def outbox_send(item):
    text = item.get("text") or ""
    if item.get("file") and os.path.isfile(item["file"]):
        caption = text if len(text) <= 1000 else None  # подпись в Telegram — до 1024 символов
        if text and not caption:
            await notify(E("say") + " " + text)
        f = FSInputFile(item["file"])
        if item.get("photo"):
            try:
                return await bot.send_photo(STATE["owner_id"], f, caption=caption)
            except Exception as e:  # слишком большое/длинное фото — Telegram не берёт; шлём файлом
                log.info("send_photo failed (%s), sending as document", e)
                f = FSInputFile(item["file"])
        await bot.send_document(STATE["owner_id"], f, caption=caption)
    elif item.get("file"):
        await notify(f"{E('err')} Агент хотел прислать файл `{item['file']}`, но его нет."
                     + (f"\n\n{text}" if text else ""))
    elif item.get("options"):
        await send_ask(item)
    elif item.get("root"):
        await send_root_request(item)
    elif text:
        await notify(E("say") + " " + text)


# ---------------------------------------------------------------- root по кнопке / полный доступ

ROOT_DIR = os.path.join(HOME, "spool", "root")
ROOT_HELPER = "/usr/local/sbin/nook-root"
FULL_PATH = os.path.join(ROOT_DIR, "full.json")


def full_until():
    t = load_json(FULL_PATH, {}).get("until", 0)
    return t if t > time.time() else 0


def set_full(seconds):
    os.makedirs(ROOT_DIR, mode=0o700, exist_ok=True)
    save_json(FULL_PATH, {"until": time.time() + seconds if seconds else 0})


async def send_root_request(item):
    """Запрос агента на root-команду: показать хозяину команду и кнопки."""
    rid = item["root"]
    req = load_json(os.path.join(ROOT_DIR, rid + ".json"), None)
    if not req:
        return
    STATE.setdefault("root_reqs", {})[rid] = {"thread": RUN_THREADS.get(item.get("run")) or STATE["main_thread"]}
    for k in list(STATE["root_reqs"])[:-50]:
        del STATE["root_reqs"][k]
    save_state()
    cmd = req["cmd"]
    long = len(cmd) > 400 or cmd.count("\n") > 6
    shown = "\n".join(cmd.splitlines()[:4])[:300] + ("\n…" if long else "")
    text = (f"{E('login')} **Нужен root**\n\n{req['reason']}\n\n```bash\n{shown}\n```"
            + (f"\n*Команда длинная ({len(cmd.splitlines())} строк) — целиком по кнопке «Показать».*" if long else ""))
    await notify(text, markup=kb((btn("Выполнить", f"rok:{rid}", "ok", "danger"),
                                  btn("Отказать", f"rno:{rid}", "cross")),
                                 (btn("Показать команду", f"rshow:{rid}", "info"),) if long else None))


async def run_root(rid):
    """Выполнить одобренный запрос через root-помощника; вернуть (код, вывод)."""
    path = os.path.join(ROOT_DIR, rid + ".json")
    req = load_json(path, None)
    if not req or req.get("done"):
        return None, "запрос не найден или уже выполнен"
    req.update(approved=True, approved_at=time.time())
    save_json(path, req)
    p = await asyncio.create_subprocess_exec("sudo", "-n", ROOT_HELPER, rid, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), int(req.get("timeout", 600)) + 30)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "таймаут"
    return p.returncode, out.decode(errors="replace")


async def send_ask(item):
    """Вопрос агента хозяину с кнопками-вариантами (bin/ask)."""
    aid = uuid.uuid4().hex[:8]
    asks = STATE.setdefault("asks", {})
    asks[aid] = {"q": item["text"], "options": item["options"],
                 "thread": RUN_THREADS.get(item.get("run")) or STATE["main_thread"]}
    for k in list(asks)[:-50]:
        del asks[k]
    save_state()
    rows = [(btn(o, f"ask:{aid}:{i}", None, "primary"),) for i, o in enumerate(item["options"])]
    await notify(f"{E('info')} {item['text']}", markup=kb(*rows))


def take_due_jobs():
    os.makedirs(os.path.dirname(JOBS), exist_ok=True)
    with open(JOBS + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        jobs = load_json(JOBS, [])
        now, due, keep = time.time(), [], []
        for j in jobs:
            if j["at"] <= now:
                due.append(j)
                if j.get("every"):
                    j = dict(j, at=max(j["at"] + j["every"], now + 60))
                    keep.append(j)
            else:
                keep.append(j)
        if due:
            save_json(JOBS, keep)
        return due


REMIND_RE = re.compile(r"(?is)^\W*(напомни|напоминание|напомнить|remind)")
REMIND_WORK_RE = re.compile(r"(?i)\b(провер|посмотр|сделай|запусти|почист|обнов|перезапус|установ|удали|собери|"
                            r"почини|настрой|глянь|выясни|узнай|отчёт|сводк)")


async def remind(j):
    """Чистое напоминание: фразу пишет Mercury, иначе — сам текст задачи."""
    text = await quick_llm("Хозяин просил напомнить. Напиши само напоминание одной-двумя фразами, обращаясь к нему "
                           f"(без «Напоминаю, что ты просил…»).\n\nЧто напомнить: {j['prompt'][:1500]}", timeout=30)
    text = text or re.sub(r"(?i)^\W*(напомни(ть)?|напоминание)\W*(мне|хозяину)?\W*", "", j["prompt"]).strip() or j["prompt"]
    await notify(f"{E('bell')} {text}", markup=kb((btn("Меню", "menu", "home"),)))


async def jobs_loop():
    while True:
        try:
            if STATE["owner_id"]:
                for j in take_due_jobs():
                    if REMIND_RE.match(j["prompt"]) and not REMIND_WORK_RE.search(j["prompt"]):
                        asyncio.create_task(remind(j))
                        continue
                    prompt = (f"[ОТЛОЖЕННАЯ ЗАДАЧА {j['id']}, поставлена "
                              f"{dt.datetime.fromtimestamp(j['created']):%d.%m %H:%M}"
                              f"{', повторяется' if j.get('every') else ''}]\n{j['prompt']}\n\n"
                              "Выполни. Если это напоминание или поручение хозяина — обязательно напиши ему "
                              "(коротко, по делу). Если это твоя служебная проверка и сказать нечего — ответь SILENT.")
                    asyncio.create_task(run_task(bg_lane, prompt, background=True, show_progress=False,
                                                 header=E("jobs") + " "))
        except Exception:
            log.exception("jobs failed")
        await asyncio.sleep(20)


def housekeeping():
    """Уборка за собой: старые загрузки, обрывки запусков, свои логи. Без LLM, раз в сутки."""
    now, freed = time.time(), 0
    for d, age in ((INBOX, 7 * 86400), (os.path.join(HOME, "spool"), 86400)):
        for name in os.listdir(d):
            path = os.path.join(d, name)
            if os.path.isfile(path) and (d == INBOX or name.startswith("last-")) \
                    and now - os.path.getmtime(path) > age:
                freed += os.path.getsize(path)
                os.remove(path)
    for name in ("out.log", "err.log"):
        path = os.path.join(HOME, "logs", name)
        if os.path.isfile(path) and os.path.getsize(path) > 20 * 2**20:
            with open(path, "rb+") as f:  # оставить хвост 2 МБ, файл открыт pm2 — не удаляем
                f.seek(-2 * 2**20, os.SEEK_END)
                tail = f.read()
                f.seek(0)
                f.write(tail)
                f.truncate()
            freed += 1
    for k in ("msg_threads", "incident_msgs"):  # не держать память о старых сообщениях вечно
        m = STATE.get(k, {})
        for key in list(m)[:-500]:
            del m[key]
    save_state()
    if freed:
        log.info("housekeeping: cleaned own files")


PATROL_PROMPT = """[ЕЖЕДНЕВНЫЙ ОБХОД] Сделай утренний обход дома:
- диск (что растёт — сравни с прошлыми заметками), память, swap, нагрузка;
- pm2/docker/systemd: что падало или часто перезапускается, ошибки в свежих логах;
- подозрительное: неудачные входы по ssh (если доступно), незнакомые процессы, переполненные логи;
- что можно безопасно почистить — почисти и скажи, сколько освободил;
- сверь с registry.json: что с expected=true не работает — если явно упало (а не выключено намеренно,
  см. notes.md), подними; если не уверен — спроси списком;
- проверь свои отложенные задачи (`later --list`): лишние отмени.
Обнови memory/notes.md (снимок состояния и тренды) и journal.md.
О проблемах, которые хозяин уже знает и не решил, напоминай не чаще раза в неделю.
Хозяину напиши короткую сводку, только если есть что-то полезное или требующее его решения
(можно по-дружески). Если всё ровно и ничего не делал — ответь SILENT."""


async def patrol_loop():
    while True:
        try:
            hh, mm = map(int, CFG.get("patrol_time", "12:00").split(":"))
            now = dt.datetime.now()
            today = now.date().isoformat()
            if STATE.get("last_housekeeping") != today:
                STATE["last_housekeeping"] = today
                await asyncio.to_thread(housekeeping)
            if (STATE["owner_id"] and STATE["watch"] and STATE["last_patrol"] != today
                    and (now.hour, now.minute) >= (hh, mm)):
                STATE["last_patrol"] = today
                save_state()
                asyncio.create_task(run_task(bg_lane, PATROL_PROMPT, background=True,
                                             show_progress=False, header=E("clean") + " "))
        except Exception:
            log.exception("patrol failed")
        await asyncio.sleep(60)


async def introduce():
    if STATE["introduced"]:
        return
    STATE["introduced"] = True
    STATE["last_patrol"] = dt.date.today().isoformat()
    save_state()
    prompt = """[ЗАСЕЛЕНИЕ] Тебя только что поселили на этот сервер и привязали к хозяину в Telegram.
Сделай первый осмотр дома: прочитай AGENTS.md (уже в контексте), memory/notes.md, memory/registry.json,
память других агентов, если она есть. Посмотри pm2, docker, systemd, диск/память,
что упало и почему (кратко), что занимает диск. НИЧЕГО не чини и не удаляй в этот раз — только смотри.
Дополни memory/notes.md тем, что узнал.
Потом напиши хозяину первое сообщение: коротко представься (кто ты и что умеешь — чинить, чистить,
следить, писать первым, ставить себе напоминания), и дай короткую сводку: что в доме хорошо, что лежит,
что предлагаешь сделать в первую очередь (списком, чтобы он мог ответить «да, делай 1 и 3»)."""
    await run_task(user_lane, prompt, title=E("home") + " осматриваю дом", set_main=True)


# ---------------------------------------------------------------- main

async def main():
    from nook_update import runtime_lock
    # Keep the context alive for the whole polling lifetime.
    with runtime_lock(Path(HOME)):
        await run_main()


async def run_main():
    for d in (OUTBOX, INBOX):
        os.makedirs(d, exist_ok=True)
    os.makedirs(modsys.SPOOL, exist_ok=True)
    await mods_reload()
    me = await bot.get_me()
    log.info("started as @%s, owner=%s", me.username, STATE["owner_id"])
    if not STATE["owner_id"]:
        log.info("pair with: /start %s", CFG.get("pair_code"))
    for coro in (trash_loop(), watch_loop(), outbox_loop(), jobs_loop(), patrol_loop(), guard_loop(), whisper_unload_loop(), mods_loop(), resume_loop(), ops_loop()):
        asyncio.create_task(coro)
    if STATE["owner_id"] and not STATE["introduced"]:
        # хозяин задан в config.json — привязка не нужна, сразу осмотреться
        asyncio.create_task(introduce())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
